"""GTFS Static schedule loader for PTV."""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .const import (
    GTFS_CACHE_FILE,
    GTFS_CACHE_TTL_DAYS,
    GTFS_METRO_INNER_ZIP,
    GTFS_STATIC_URL,
    METRO_LINES,
)

_LOGGER = logging.getLogger(__name__)

_METRO_CODES = {
    "ALM","BEG","CBE","CGB","FKN","GWY","HBE",
    "LIL","MDD","PKM","SHM","STY","SUY","UFD","WER","WIL",
}


def _open_metro_zip(outer: zipfile.ZipFile) -> zipfile.ZipFile:
    if GTFS_METRO_INNER_ZIP in outer.namelist():
        try:
            return zipfile.ZipFile(io.BytesIO(outer.read(GTFS_METRO_INNER_ZIP)))
        except Exception as err:
            _LOGGER.warning("Known metro zip path failed: %s, scanning...", err)
    for entry in sorted(outer.namelist()):
        if not entry.endswith("google_transit.zip"):
            continue
        try:
            inner_z = zipfile.ZipFile(io.BytesIO(outer.read(entry)))
            if "routes.txt" not in inner_z.namelist():
                continue
            with inner_z.open("routes.txt") as f:
                content = f.read().decode("utf-8-sig")
            if len([c for c in _METRO_CODES if f"-{c}:" in content]) >= 3:
                return inner_z
        except Exception:
            pass
    raise RuntimeError("Could not locate metro train data inside GTFS zip.")


def _load_stops(metro_z: zipfile.ZipFile) -> dict[str, dict]:
    grouped: dict[str, dict] = {}
    with metro_z.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            sid = row.get("stop_id", "").strip()
            name = row.get("stop_name", "").strip()
            if not sid or not name:
                continue
            if "Bus" in name or "Rail Replacement" in name:
                continue
            if not sid.replace("-","").replace("_","").isdigit():
                continue
            clean = (
                name
                .replace(" Railway Station","")
                .replace(" Train Station","")
                .replace(" Station","")
                .split("(")[0].strip()
            )
            key = clean.lower()
            if key not in grouped:
                grouped[key] = {"stop_name": clean, "stop_ids": [sid]}
            elif sid not in grouped[key]["stop_ids"]:
                grouped[key]["stop_ids"].append(sid)
    return grouped


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
    trip_dirs: dict[str, int] = {}
    with metro_z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("route_id") in route_ids and row.get("service_id") in active_sids:
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
) -> dict:
    """Download GTFS static data, cache it, return cache dict."""
    cache_path = Path(config_dir) / GTFS_CACHE_FILE
    today_str = datetime.now().astimezone().strftime("%Y%m%d")

    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            cached_at = datetime.fromisoformat(cache.get("cached_at","2000-01-01"))
            age_days = (datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc)).days
            # Invalidate if stale OR if it's a new day (timetable changes)
            if age_days < GTFS_CACHE_TTL_DAYS and cache.get("cached_date") == today_str:
                # Also check all requested stations are cached
                cached_stations = set(cache.get("scheduled_by_station", {}).keys())
                if all(s.lower() in cached_stations for s in station_names):
                    _LOGGER.debug("Using cached GTFS data (age %d days)", age_days)
                    return cache
        except Exception as err:
            _LOGGER.warning("Could not read GTFS cache: %s", err)

    _LOGGER.info("Downloading GTFS static schedule...")
    resp = requests.get(GTFS_STATIC_URL, timeout=180)
    resp.raise_for_status()
    _LOGGER.info("Downloaded %d KB", len(resp.content)//1024)

    outer = zipfile.ZipFile(io.BytesIO(resp.content))
    metro_z = _open_metro_zip(outer)
    stops = _load_stops(metro_z)

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
        "stops": stops,
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
