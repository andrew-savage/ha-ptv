"""GTFS Static schedule loader for PTV."""
from __future__ import annotations

import csv
import io
import json
import logging
import shutil
import tempfile
import threading
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .const import (
    GTFS_CACHE_FILE,
    GTFS_CACHE_SCHEMA,
    GTFS_CACHE_TTL_DAYS,
    GTFS_DOWNLOAD_TIMEOUT,
    GTFS_LEGACY_CACHE_FILES,
    GTFS_LOOKUP_TIMEOUT,
    GTFS_METRO_INNER_ZIP,
    GTFS_PACKAGE_API_URL,
    GTFS_RANGE_BLOCK,
    GTFS_RESOURCE_NAME,
    GTFS_SHARE_SECONDS,
    GTFS_STATIC_URL,
    METRO_LINES,
    MODE_TRAIN,
    MODE_TRAM,
    TUNNEL_SIDES,
    TUNNEL_STATIONS,
    mode_info,
    through_running_routes,
)

_LOGGER = logging.getLogger(__name__)

_METRO_CODES = {
    "ALM","BEG","CBE","CGB","FKN","GWY","HBE",
    "LIL","MDD","PKM","SHM","STY","SUY","UFD","WER","WIL",
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HomeAssistant/PTV)"}


def resolve_gtfs_static_url() -> str:
    """Return the current download URL of the GTFS Schedule zip.

    PTV republishes the schedule under a new CKAN resource id (twice in
    November 2026 alone), and the old URL then 404s. The dataset id is
    stable, so ask CKAN for its current resources. Falls back to the
    hardcoded GTFS_STATIC_URL if the lookup fails.
    """
    try:
        resp = requests.get(
            GTFS_PACKAGE_API_URL, timeout=GTFS_LOOKUP_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        resources = resp.json()["result"]["resources"]
        zips = [
            r for r in resources
            if (r.get("format") or "").lower() == "zip" and r.get("url")
        ]
        named = [
            r for r in zips
            if (r.get("name") or "").strip().lower() == GTFS_RESOURCE_NAME.lower()
        ]
        chosen = (named or zips or [None])[0]
        if chosen is None:
            raise ValueError("no ZIP resource in the dataset")
        if not named:
            _LOGGER.warning(
                "No resource named '%s' in the GTFS dataset; using '%s'",
                GTFS_RESOURCE_NAME, chosen.get("name"),
            )
        return chosen["url"]
    except Exception as err:
        _LOGGER.warning(
            "Could not look up the GTFS Schedule URL (%s); using built-in %s",
            err, GTFS_STATIC_URL,
        )
        return GTFS_STATIC_URL


class _RangeFile(io.RawIOBase):
    """Remote file read with HTTP range requests.

    The schedule zip is ~300 MB but holds one inner zip per transport mode,
    and ours is 25-40 MB of it. Seeking lets zipfile fetch just that.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._pos = 0
        self._size, self._etag = 0, None
        try:
            resp = self._get(0, 0)
            self._size = int(resp.headers["Content-Range"].rsplit("/", 1)[1])
            self._etag = resp.headers.get("ETag")
        except Exception:
            self._session.close()
            raise

    def _get(self, start: int, end: int) -> requests.Response:
        # Streamed so that a server ignoring the range can't send it all
        resp = self._session.get(
            self._url, headers={"Range": f"bytes={start}-{end}"},
            timeout=(10, 60), stream=True)
        resp.raise_for_status()
        if resp.status_code != 206:
            resp.close()
            raise OSError("server does not support range requests")
        if self._etag and resp.headers.get("ETag") != self._etag:
            resp.close()
            raise OSError("schedule changed during download")
        return resp

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, buf) -> int:
        want = min(len(buf), self._size - self._pos)
        if want <= 0:
            return 0
        data = self._get(self._pos, self._pos + want - 1).content
        if len(data) != want:
            raise OSError("short range response")
        buf[:want] = data
        self._pos += want
        return want

    def close(self) -> None:
        if not self.closed:
            self._session.close()
        super().close()


def _extract_to_temp(outer: zipfile.ZipFile, name: str):
    """Copy one inner zip to an unnamed temporary file."""
    tmp = tempfile.TemporaryFile()
    try:
        with outer.open(name) as src:
            shutil.copyfileobj(src, tmp, 1024 * 1024)
        tmp.seek(0)
    except BaseException:
        tmp.close()
        raise
    return tmp


def _extract_mode_zip(outer: zipfile.ZipFile, mode: str = MODE_TRAIN):
    """Extract the inner GTFS zip for a transport mode to a temporary file."""
    info = mode_info(mode)
    known = info["gtfs_inner_zip"]
    # Route IDs are "aus:vic:vic-02-..." for train, "vic-03-" for tram
    marker = f"vic-{known.split('/')[0].zfill(2)}-"

    if known in outer.namelist():
        return _extract_to_temp(outer, known)
    for entry in sorted(outer.namelist()):
        if not entry.endswith("google_transit.zip"):
            continue
        tmp = None
        try:
            tmp = _extract_to_temp(outer, entry)
            with zipfile.ZipFile(tmp) as inner_z, inner_z.open("routes.txt") as f:
                content = f.read().decode("utf-8-sig")
        except (KeyError, zipfile.BadZipFile):
            if tmp:
                tmp.close()
            continue
        if (len([c for c in _METRO_CODES if f"-{c}:" in content]) >= 3
                if mode == MODE_TRAIN else marker in content):
            return tmp
        tmp.close()
    raise RuntimeError(f"Could not locate {mode} data inside GTFS zip.")


def _download_mode_zip(url: str, mode: str):
    """Download a mode's inner zip, whole-file only if ranges don't work."""
    try:
        with io.BufferedReader(_RangeFile(url), GTFS_RANGE_BLOCK) as remote, \
                zipfile.ZipFile(remote) as outer:
            return _extract_mode_zip(outer, mode)
    except Exception as err:
        _LOGGER.info("Partial download failed (%s); downloading the whole schedule", err)
    with tempfile.TemporaryFile() as full:
        with requests.get(
            url,
            # ~300 MB; 180s needs a sustained 13 Mbit/s, which times out on
            # slower connections and leaves setup with no station list.
            timeout=(10, GTFS_DOWNLOAD_TIMEOUT),
            headers=_HEADERS,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(1024 * 1024):
                full.write(chunk)
        full.seek(0)
        with zipfile.ZipFile(full) as outer:
            return _extract_mode_zip(outer, mode)


# Entries load one at a time, so that several starting together (or refreshing
# at midnight) share one download per mode and don't add up in memory.
_load_lock = threading.Lock()
_shared_zips: dict[str, dict] = {}  # mode -> {"file", "timer"}


def _drop_shared_zip(mode: str, item: dict) -> None:
    with _load_lock:
        if _shared_zips.get(mode) is item:
            del _shared_zips[mode]
        item["file"].close()


def _get_mode_zip(mode: str):
    """The mode's zip, from a recent download if there is one.

    Call with _load_lock held.
    """
    item = _shared_zips.get(mode)
    if item is None:
        url = resolve_gtfs_static_url()
        _LOGGER.info("Downloading GTFS static schedule from %s", url)
        item = {"file": _download_mode_zip(url, mode)}
        timer = threading.Timer(GTFS_SHARE_SECONDS, _drop_shared_zip, (mode, item))
        timer.daemon = True
        _shared_zips[mode] = item
        timer.start()
    item["file"].seek(0)
    return item["file"]


def _clean_stop_name(name: str, mode: str) -> str:
    """Strip mode-specific noise from a stop name.

    Train stops are "Ivanhoe Railway Station"; tram stops are
    "Casino/Southbank/Queens Bridge St #115", where the number identifies
    the stop and must be kept.
    """
    if mode == MODE_TRAM:
        return name.strip()
    return (
        name
        .replace(" Railway Station","")
        .replace(" Train Station","")
        .replace(" Station","")
        .split("(")[0].strip()
    )


def _load_stops(z: zipfile.ZipFile, mode: str = MODE_TRAIN) -> dict[str, dict]:
    """Group stops by name. Each direction is its own stop_id under one name."""
    grouped: dict[str, dict] = {}
    with z.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            sid = row.get("stop_id", "").strip()
            name = row.get("stop_name", "").strip()
            if not sid or not name:
                continue
            if mode == MODE_TRAIN and ("Bus" in name or "Rail Replacement" in name):
                continue
            if not sid.replace("-","").replace("_","").isdigit():
                continue
            clean = _clean_stop_name(name, mode)
            # A few rows carry the platform number as the stop name instead of
            # the station's (e.g. Berwick stops 27882/27883, named "1" and "2").
            # They never appear in stop_times, so drop them.
            if not clean or clean.isdigit():
                continue
            key = clean.lower()
            if key not in grouped:
                grouped[key] = {"stop_name": clean, "stop_ids": [sid]}
            elif sid not in grouped[key]["stop_ids"]:
                grouped[key]["stop_ids"].append(sid)
    return grouped


def _load_direction_labels(z: zipfile.ZipFile, route_ids: set[str]) -> dict[str, dict]:
    """Map route_id -> {"0": headsign, "1": headsign}.

    Trams have no citybound/outbound; the destination is the useful label.
    Short workings produce minority headsigns, so take the most common one.
    """
    counts: dict[str, dict[str, Counter]] = {}
    with z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            rid = row.get("route_id", "")
            if not any(rid.startswith(r) for r in route_ids):
                continue
            head = (row.get("trip_headsign") or "").strip()
            if not head:
                continue
            counts.setdefault(rid, {}).setdefault(row.get("direction_id", "0"), Counter())[head] += 1
    return {
        rid: {d: c.most_common(1)[0][0] for d, c in dirs.items()}
        for rid, dirs in counts.items()
    }


def _active_service_ids(z: zipfile.ZipFile, today_str: str, dow: str) -> set[str]:
    active: set[str] = set()
    with z.open("calendar.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if (row.get(dow) == "1"
                    and row.get("start_date","") <= today_str <= row.get("end_date","")):
                active.add(row["service_id"])
    with z.open("calendar_dates.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("date") == today_str:
                if row.get("exception_type") == "1":
                    active.add(row["service_id"])
                elif row.get("exception_type") == "2":
                    active.discard(row["service_id"])
    return active


def _through_trips(z: zipfile.ZipFile, route_ids: set[str], active_sids: set[str]) -> dict[str, str]:
    """Trips that continue the selected lines' trains through the Metro Tunnel.

    Maps each such trip_id to the selected route it continues as. The two
    halves of a train share a block_id, and every tunnel block pairs Sunbury
    with exactly one of Pakenham or Cranbourne, so a Pakenham rider gets the
    Sunbury trips that become Pakenham trains — not Cranbourne ones.
    """
    through = tuple(through_running_routes(route_ids))
    if not through:
        return {}
    own = tuple(r for r in route_ids if r in TUNNEL_SIDES)
    own_blocks: dict[tuple, str] = {}
    candidates: list[tuple[str, tuple]] = []
    with z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            block = row.get("block_id")
            if not block or row.get("service_id") not in active_sids:
                continue
            key = (row["service_id"], block)
            rid = row.get("route_id", "")
            for r in own:
                if rid.startswith(r):
                    own_blocks[key] = r
            if rid.startswith(through):
                candidates.append((row["trip_id"], key))
    return {tid: own_blocks[key] for tid, key in candidates if key in own_blocks}


def _filtered_lines(lines, trip_ids: set[str]):
    """The header, then only the lines of the given trips.

    stop_times.txt is millions of rows, nearly all of them other routes'.
    Dropping those before the csv module sees them is several times faster.
    """
    header = next(lines)
    yield header
    if header.split(",", 1)[0].strip().strip('"') != "trip_id":
        yield from lines
        return
    for line in lines:
        if line.split(",", 1)[0].strip('"') in trip_ids:
            yield line


def _scan_stop_times(
    z: zipfile.ZipFile,
    route_trips: set[str],
    dep_trips: dict[str, int],
    stop_ids: set[str],
    tunnel_trips: set[str],
) -> tuple[set[str], list[dict], dict[str, int]]:
    """Everything needed from stop_times.txt, in one pass.

    Returns the stop IDs route_trips serve, the rows of dep_trips at stop_ids,
    and the last stop_sequence of each tunnel trip.
    """
    served: set[str] = set()
    rows: list[dict] = []
    last_seq: dict[str, int] = {}
    with z.open("stop_times.txt") as f:
        lines = io.TextIOWrapper(f, encoding="utf-8-sig")
        for row in csv.DictReader(_filtered_lines(lines, route_trips | dep_trips.keys())):
            tid = row.get("trip_id")
            if tid in route_trips:
                served.add(row.get("stop_id"))
            if tid in tunnel_trips:
                seq = int(row.get("stop_sequence") or 0)
                if seq > last_seq.get(tid, -1):
                    last_seq[tid] = seq
            if row.get("stop_id") in stop_ids and tid in dep_trips:
                rows.append(row)
    return served, rows, last_seq


def _route_stops(route_ids: set[str], stops: dict[str, dict], served: set[str]) -> list[str]:
    """Keys of the stops served by the given routes, on any day.

    Not limited to today's services, so a stop only served on other days can
    still be chosen.
    """
    keys = {k for k, v in stops.items() if not served.isdisjoint(v["stop_ids"])}
    if any(r in TUNNEL_SIDES for r in route_ids):
        keys |= TUNNEL_STATIONS & stops.keys()
    return sorted(keys)


def _trip_directions(
    z: zipfile.ZipFile,
    route_ids: set[str],
    through_trips: dict[str, str],
    active_sids: set[str],
) -> dict[str, int]:
    """Map trip_id -> the trip's own GTFS direction_id.

    PTV's realtime trip updates leave trip.direction_id unset, and protobuf
    then reads it as 0 (outbound), so the realtime parser looks directions up
    here instead. Covers every trip of the selected routes and their tunnel
    continuations, not just those calling at configured stations. Directions
    are as published: through trips are not flipped.
    """
    prefixes = tuple(route_ids)
    dirs: dict[str, int] = {}
    with z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("service_id") not in active_sids:
                continue
            tid = row["trip_id"]
            if row.get("route_id", "").startswith(prefixes) or tid in through_trips:
                try:
                    dirs[tid] = int(row.get("direction_id") or 0)
                except ValueError:
                    continue
    return dirs


def _departure_trips(
    metro_z: zipfile.ZipFile,
    route_ids: set[str],
    active_sids: set[str],
    through_trips: dict[str, str],
) -> tuple[set[str], dict[str, int], set[str]]:
    """Trips of the selected routes.

    Returns all their trip IDs on any day, the direction of each trip running
    today, and which of those are tunnel trips. through_trips (from
    _through_trips) adds the tunnel continuations of the selected lines'
    trains, with direction flipped to read relative to them.
    """
    # Prefix match so route ID variants (e.g. "aus:vic:vic-02-HBE:E" express)
    # are included; base IDs end with ":" so "-R:" replacement buses never match.
    route_prefixes = tuple(route_ids)
    route_trips: set[str] = set()
    trip_dirs: dict[str, int] = {}
    tunnel_trips: set[str] = set()
    with metro_z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            rid = row.get("route_id", "")
            own = rid.startswith(route_prefixes)
            if own:
                route_trips.add(row["trip_id"])
            if row.get("service_id") not in active_sids:
                continue
            direction = int(row.get("direction_id", 0))
            if own:
                trip_dirs[row["trip_id"]] = direction
            elif row["trip_id"] in through_trips:
                trip_dirs[row["trip_id"]] = 1 - direction
            else:
                continue
            if any(rid.startswith(t) for t in TUNNEL_SIDES):
                tunnel_trips.add(row["trip_id"])
    return route_trips, trip_dirs, tunnel_trips


def _scheduled_departures(
    rows: list[dict],
    stop_ids: set[str],
    trip_dirs: dict[str, int],
    tunnel_trips: set[str],
    last_seq: dict[str, int],
) -> list[dict]:
    """Departures at stop_ids, sorted by time.

    A tunnel trip's last stop is the Town Hall handover, where the train
    carries on as the next trip — that row is an arrival, and keeping it would
    list every train there twice.
    """
    deps = []
    for row in rows:
        if row["stop_id"] not in stop_ids:
            continue
        tid = row["trip_id"]
        if tid in tunnel_trips and int(row.get("stop_sequence") or 0) == last_seq.get(tid):
            continue
        dep_str = row.get("departure_time","") or row.get("arrival_time","")
        if not dep_str:
            continue
        try:
            h, m, s = dep_str.split(":")
            dep_sec = int(h)*3600 + int(m)*60 + int(s)
        except ValueError:
            continue
        deps.append({
            "dep_seconds": dep_sec,
            "direction_id": trip_dirs[tid],
            "trip_id": tid,
            "stop_id": row["stop_id"],
            "stop_sequence": int(row.get("stop_sequence") or 0),
            "dep_str": dep_str,
        })
    deps.sort(key=lambda x: x["dep_seconds"])
    return deps


def gtfs_cache_path(config_dir: str, entry_id: str) -> Path:
    return Path(config_dir) / GTFS_CACHE_FILE.format(entry_id=entry_id)


def remove_gtfs_cache(config_dir: str, entry_id: str) -> None:
    """Delete a removed entry's cached timetable."""
    try:
        gtfs_cache_path(config_dir, entry_id).unlink()
    except FileNotFoundError:
        pass
    except OSError as err:
        _LOGGER.warning("Could not delete GTFS cache: %s", err)


def download_and_cache_gtfs(
    config_dir: str,
    station_names: list[str],
    route_ids: set[str],
    mode: str = MODE_TRAIN,
    entry_id: str = "",
) -> dict:
    """Download GTFS static data, cache it, return cache dict.

    Without an entry_id (during setup, before there is one) nothing is cached.
    """
    with _load_lock:
        return _load_gtfs(config_dir, station_names, route_ids, mode, entry_id)


def _load_gtfs(
    config_dir: str,
    station_names: list[str],
    route_ids: set[str],
    mode: str,
    entry_id: str,
) -> dict:
    cache_path = gtfs_cache_path(config_dir, entry_id) if entry_id else None
    today_str = datetime.now().astimezone().strftime("%Y%m%d")

    stale_cache: dict | None = None

    if cache_path and cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            cached_at = datetime.fromisoformat(cache.get("cached_at","2000-01-01"))
            age_days = (datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc)).days
            # Return immediately if cache is fresh and covers all stations
            if (age_days < GTFS_CACHE_TTL_DAYS and cache.get("cached_date") == today_str
                    and cache.get("schema") == GTFS_CACHE_SCHEMA):
                cached_stations = set(cache.get("scheduled_by_station", {}).keys())
                if (cache.get("route_ids") == sorted(route_ids)
                        and all(s.lower() in cached_stations for s in station_names)):
                    _LOGGER.debug("Using cached GTFS data (age %d days)", age_days)
                    return cache
            # Keep stale cache as a fallback in case the download fails
            stale_cache = cache
        except Exception as err:
            _LOGGER.warning("Could not read GTFS cache: %s", err)

    try:
        metro_z = zipfile.ZipFile(_get_mode_zip(mode))
    except Exception as err:
        if stale_cache:
            _LOGGER.warning(
                "GTFS download failed (%s) — using stale cache from %s as fallback",
                err, stale_cache.get("cached_date","unknown"),
            )
            return stale_cache
        raise
    stops = _load_stops(metro_z, mode)
    direction_labels = _load_direction_labels(metro_z, route_ids)

    now = datetime.now().astimezone()
    dow = now.strftime("%A").lower()
    yesterday = now - timedelta(days=1)
    # Yesterday's services too: trips that run past midnight (times beyond
    # 24:00:00) belong to the previous service day but are still in the feed.
    today_sids = _active_service_ids(metro_z, today_str, dow)
    active_sids = today_sids | _active_service_ids(
        metro_z, yesterday.strftime("%Y%m%d"), yesterday.strftime("%A").lower())
    through_trips = _through_trips(metro_z, route_ids, active_sids)
    trip_directions = _trip_directions(metro_z, route_ids, through_trips, active_sids)

    # Resolve each requested station to its stops
    station_stops: dict[str, set[str]] = {}
    for station_name in station_names:
        key = station_name.lower()
        entry = stops.get(key)
        if not entry:
            for k, v in stops.items():
                if key in k:
                    entry = v
                    break
        if not entry:
            _LOGGER.warning("Station '%s' not found in GTFS static data", station_name)
        station_stops[key] = set(entry["stop_ids"]) if entry else set()

    route_trips, trip_dirs, tunnel_trips = _departure_trips(
        metro_z, route_ids, today_sids, through_trips)
    served, rows, last_seq = _scan_stop_times(
        metro_z, route_trips, trip_dirs,
        set().union(*station_stops.values()), tunnel_trips)
    route_stops = _route_stops(route_ids, stops, served)
    scheduled_by_station = {
        key: _scheduled_departures(rows, ids, trip_dirs, tunnel_trips, last_seq)
        for key, ids in station_stops.items()
    }

    cache = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "cached_date": today_str,
        "schema": GTFS_CACHE_SCHEMA,
        "mode": mode,
        "route_ids": sorted(route_ids),
        "stops": stops,
        "route_stops": route_stops,
        "direction_labels": direction_labels,
        "through_trips": through_trips,
        "trip_directions": trip_directions,
        "scheduled_by_station": scheduled_by_station,
    }

    if cache_path:
        try:
            with open(cache_path, "w") as f:
                json.dump(cache, f)
            _LOGGER.info("GTFS cache saved")
            (Path(config_dir) / GTFS_LEGACY_CACHE_FILES[mode]).unlink(missing_ok=True)
        except Exception as err:
            _LOGGER.warning("Could not save GTFS cache: %s", err)

    return cache


def get_stop_ids(cache: dict, station_name: str) -> list[str]:
    stops = cache.get("stops", {})
    key = station_name.lower()
    if key in stops:
        return stops[key]["stop_ids"]
    for k, v in stops.items():
        if key in k:
            return v["stop_ids"]
    return []


def get_station_names(cache: dict, route_ids: set[str]) -> list[str]:
    """Names of the stops on the given routes.

    Falls back to every stop if the cache is an old copy for other routes.
    """
    stops = cache.get("stops", {})
    keys = cache.get("route_stops") if cache.get("route_ids") == sorted(route_ids) else None
    return sorted(stops[k]["stop_name"] for k in (keys or stops))


def get_direction_label(cache: dict, route_id: str, direction_id, fallback: str = "") -> str:
    """Destination for a route's direction, e.g. "Box Hill" for tram 109 dir 0."""
    labels = cache.get("direction_labels", {})
    entry = labels.get(route_id)
    if entry is None:
        for rid, val in labels.items():
            if rid.startswith(route_id):
                entry = val
                break
    if not entry:
        return fallback
    return entry.get(str(direction_id), fallback)


def get_next_n_scheduled(
    cache: dict,
    station_name: str,
    direction_id: int,
    after_seconds: int,
    count: int = 4,
    exclude_trips: set[str] = frozenset(),
) -> list[dict]:
    """Return up to `count` scheduled departures in given direction after after_seconds."""
    key = station_name.lower()
    all_deps = cache.get("scheduled_by_station", {}).get(key, [])
    results = []
    for dep in all_deps:
        if (dep["direction_id"] == direction_id and dep["dep_seconds"] > after_seconds
                and dep["trip_id"] not in exclude_trips):
            results.append(dep)
            if len(results) >= count:
                break
    return results
