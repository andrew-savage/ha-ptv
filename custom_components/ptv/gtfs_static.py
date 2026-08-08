"""GTFS Static schedule loader for PTV."""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .const import (
    GTFS_CACHE_FILE,
    GTFS_CACHE_TTL_DAYS,
    GTFS_DOWNLOAD_TIMEOUT,
    GTFS_METRO_INNER_ZIP,
    GTFS_STATIC_URL,
    METRO_LINES,
    MODE_TRAIN,
    MODE_TRAM,
    mode_info,
)

_LOGGER = logging.getLogger(__name__)

_METRO_CODES = {
    "ALM","BEG","CBE","CGB","FKN","GWY","HBE",
    "LIL","MDD","PKM","SHM","STY","SUY","UFD","WER","WIL",
}


def _open_mode_zip(outer: zipfile.ZipFile, mode: str = MODE_TRAIN) -> zipfile.ZipFile:
    """Open the inner GTFS zip for a transport mode."""
    info = mode_info(mode)
    known = info["gtfs_inner_zip"]
    # Route IDs are "aus:vic:vic-02-..." for train, "vic-03-" for tram
    marker = f"vic-{known.split('/')[0].zfill(2)}-"

    if known in outer.namelist():
        try:
            return zipfile.ZipFile(io.BytesIO(outer.read(known)))
        except Exception as err:
            _LOGGER.warning("Known %s zip path failed: %s, scanning...", mode, err)
    for entry in sorted(outer.namelist()):
        if not entry.endswith("google_transit.zip"):
            continue
        try:
            inner_z = zipfile.ZipFile(io.BytesIO(outer.read(entry)))
            if "routes.txt" not in inner_z.namelist():
                continue
            with inner_z.open("routes.txt") as f:
                content = f.read().decode("utf-8-sig")
            if mode == MODE_TRAIN:
                if len([c for c in _METRO_CODES if f"-{c}:" in content]) >= 3:
                    return inner_z
            elif marker in content:
                return inner_z
        except Exception:
            pass
    raise RuntimeError(f"Could not locate {mode} data inside GTFS zip.")


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


def _load_scheduled_departures(
    metro_z: zipfile.ZipFile,
    stop_ids: set[str],
    route_ids: set[str],
    today_str: str,
    dow: str,
) -> list[dict]:
    """Return all departures at stop_ids today, sorted by time."""
    # Active service IDs
    active_sids: set[str] = set()
    with metro_z.open("calendar.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if (row.get(dow) == "1"
                    and row.get("start_date","") <= today_str <= row.get("end_date","")):
                active_sids.add(row["service_id"])
    with metro_z.open("calendar_dates.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("date") == today_str:
                if row.get("exception_type") == "1":
                    active_sids.add(row["service_id"])
                elif row.get("exception_type") == "2":
                    active_sids.discard(row["service_id"])

    # Trip -> direction
    # Prefix match so route ID variants (e.g. "aus:vic:vic-02-HBE:E" express)
    # are included; base IDs end with ":" so "-R:" replacement buses never match.
    route_prefixes = tuple(route_ids)
    trip_dirs: dict[str, int] = {}
    with metro_z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("route_id", "").startswith(route_prefixes) and row.get("service_id") in active_sids:
                trip_dirs[row["trip_id"]] = int(row.get("direction_id", 0))

    # Departures at our stops
    deps = []
    with metro_z.open("stop_times.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("stop_id") in stop_ids and row.get("trip_id") in trip_dirs:
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
                    "direction_id": trip_dirs[row["trip_id"]],
                    "trip_id": row["trip_id"],
                    "stop_id": row["stop_id"],
                    "dep_str": dep_str,
                })
    deps.sort(key=lambda x: x["dep_seconds"])
    return deps


def download_and_cache_gtfs(
    config_dir: str,
    station_names: list[str],
    route_ids: set[str],
    mode: str = MODE_TRAIN,
) -> dict:
    """Download GTFS static data, cache it, return cache dict."""
    # Train and tram are separate config entries, so they need separate caches.
    cache_name = GTFS_CACHE_FILE if mode == MODE_TRAIN else \
        GTFS_CACHE_FILE.replace(".json", f"_{mode}.json")
    cache_path = Path(config_dir) / cache_name
    today_str = datetime.now().astimezone().strftime("%Y%m%d")

    stale_cache: dict | None = None

    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            cached_at = datetime.fromisoformat(cache.get("cached_at","2000-01-01"))
            age_days = (datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc)).days
            # Return immediately if cache is fresh and covers all stations
            if age_days < GTFS_CACHE_TTL_DAYS and cache.get("cached_date") == today_str:
                cached_stations = set(cache.get("scheduled_by_station", {}).keys())
                if all(s.lower() in cached_stations for s in station_names):
                    _LOGGER.debug("Using cached GTFS data (age %d days)", age_days)
                    return cache
            # Keep stale cache as a fallback in case the download fails
            stale_cache = cache
        except Exception as err:
            _LOGGER.warning("Could not read GTFS cache: %s", err)

    _LOGGER.info("Downloading GTFS static schedule...")
    try:
        resp = requests.get(
            GTFS_STATIC_URL,
            # ~280 MB; 180s needs a sustained 12 Mbit/s, which times out on
            # slower connections and leaves setup with no station list.
            timeout=GTFS_DOWNLOAD_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HomeAssistant/PTV)"},
        )
        resp.raise_for_status()
    except Exception as err:
        if stale_cache:
            _LOGGER.warning(
                "GTFS download failed (%s) — using stale cache from %s as fallback",
                err, stale_cache.get("cached_date","unknown"),
            )
            return stale_cache
        raise
    _LOGGER.info("Downloaded %d KB", len(resp.content)//1024)

    outer = zipfile.ZipFile(io.BytesIO(resp.content))
    metro_z = _open_mode_zip(outer, mode)
    stops = _load_stops(metro_z, mode)
    direction_labels = _load_direction_labels(metro_z, route_ids)

    dow = datetime.now().astimezone().strftime("%A").lower()

    # Load scheduled departures for each requested station
    scheduled_by_station: dict[str, list] = {}
    for station_name in station_names:
        key = station_name.lower()
        entry = stops.get(key)
        if not entry:
            for k, v in stops.items():
                if key in k:
                    entry = v
                    break
        if entry:
            stop_ids = set(entry["stop_ids"])
            deps = _load_scheduled_departures(metro_z, stop_ids, route_ids, today_str, dow)
            scheduled_by_station[key] = deps
            _LOGGER.info("Loaded %d departures for %s", len(deps), station_name)
        else:
            _LOGGER.warning("Station '%s' not found in GTFS static data", station_name)
            scheduled_by_station[key] = []

    cache = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "cached_date": today_str,
        "mode": mode,
        "stops": stops,
        "direction_labels": direction_labels,
        "scheduled_by_station": scheduled_by_station,
    }

    try:
        with open(cache_path, "w") as f:
            json.dump(cache, f)
        _LOGGER.info("GTFS cache saved")
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


def get_all_station_names(cache: dict) -> list[str]:
    return sorted(v["stop_name"] for v in cache.get("stops", {}).values())


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
) -> list[dict]:
    """Return up to `count` scheduled departures in given direction after after_seconds."""
    key = station_name.lower()
    all_deps = cache.get("scheduled_by_station", {}).get(key, [])
    results = []
    for dep in all_deps:
        if dep["direction_id"] == direction_id and dep["dep_seconds"] > after_seconds:
            results.append(dep)
            if len(results) >= count:
                break
    return results
