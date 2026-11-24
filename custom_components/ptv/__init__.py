"""PTV - Home Assistant Integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests
from google.transit import gtfs_realtime_pb2

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_BASE_URL, API_HEADER_KEY,
    API_SERVICE_ALERTS_PATH, API_TRIP_UPDATES_PATH, API_VEHICLE_POSITIONS_PATH,
    CONF_API_KEY, CONF_LINES, CONF_SCAN_INTERVAL, CONF_STATIONS,
    CONF_STATION_NAME, CONF_STATION_DIRECTIONS, CONF_STATION_COUNT,
    CONF_ALERT_EFFECTS, DEFAULT_ALERT_EFFECTS,
    CONF_OFFPEAK_INTERVAL, CONF_OVERNIGHT_INTERVAL,
    CONF_PEAK_AM_START, CONF_PEAK_AM_END, CONF_PEAK_PM_START, CONF_PEAK_PM_END,
    CONF_OVERNIGHT_START, CONF_OVERNIGHT_END, CONF_PEAK_ON_WEEKENDS,
    DEFAULT_OFFPEAK_INTERVAL, DEFAULT_OVERNIGHT_INTERVAL,
    DEFAULT_PEAK_AM_START, DEFAULT_PEAK_AM_END,
    DEFAULT_PEAK_PM_START, DEFAULT_PEAK_PM_END,
    DEFAULT_OVERNIGHT_START, DEFAULT_OVERNIGHT_END, DEFAULT_PEAK_ON_WEEKENDS,
    DEFAULT_DELAY_THRESHOLD, DEFAULT_SCAN_INTERVAL, DEFAULT_STATION_COUNT,
    DIR_CITYBOUND, DIR_OUTBOUND, DIR_BOTH,
    CONF_MODE, DEFAULT_MODE, MODE_TRAIN, api_base_for_mode, mode_info, routes_for_mode,
    line_phrase, TUNNEL_SIDES,
    DOMAIN, KNOWN_STOP_IDS, METRO_LINES, OCCUPANCY_LABELS,
    PROBLEM_NONE, PROBLEM_DELAYS, PROBLEM_ALERT, PROBLEM_CANCELLATIONS, PROBLEM_MAJOR,
)
from .gtfs_static import download_and_cache_gtfs, get_next_n_scheduled, get_stop_ids, remove_gtfs_cache

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "sensor"]

# Overnight works are published as one active_period spanning the whole
# campaign (e.g. Mon 23:50 -> Thu 23:59) even though the disruption only runs
# from that start time until the last service each night. The start's
# time-of-day is the nightly start; the text says "each night".
OVERNIGHT_MIN_SPAN = timedelta(hours=24)
OVERNIGHT_PHRASES = ("each night", "every night", "each evening")
# Trains have finished by this hour, so the nightly window is start -> 4am.
END_OF_SERVICE_HOUR = 4
# How old a feed may be before a failed request fails the update.
FEED_MAX_STALE = timedelta(minutes=15)
# Wait between attempts when the daily timetable refresh fails.
GTFS_REFRESH_RETRY = timedelta(hours=1)


def _overnight_window(alert_text: str, start_ts: int, end_ts: int) -> str | None:
    """Return the nightly start time (e.g. "11:50pm") for overnight works.

    Returns None when the alert is a genuinely continuous disruption.
    """
    if not start_ts or not end_ts:
        return None
    if (datetime.fromtimestamp(end_ts) - datetime.fromtimestamp(start_ts)) < OVERNIGHT_MIN_SPAN:
        return None
    if not any(p in alert_text.lower() for p in OVERNIGHT_PHRASES):
        return None
    start = datetime.fromtimestamp(start_ts).astimezone()
    return start.strftime("%-I:%M%p").lower()


def _in_overnight_window(now: datetime, start_ts: int) -> bool:
    """True when `now` falls inside tonight's works window."""
    start = datetime.fromtimestamp(start_ts).astimezone()
    start_minutes = start.hour * 60 + start.minute
    now_minutes = now.hour * 60 + now.minute
    return now_minutes >= start_minutes or now_minutes < END_OF_SERVICE_HOUR * 60


def _minutes(hhmmss: str) -> int:
    """Parse a "HH:MM:SS" selector value into minutes since midnight."""
    h, m, *_ = (hhmmss or "0:0:0").split(":")
    return int(h) * 60 + int(m)


def _within(now_min: int, start_min: int, end_min: int) -> bool:
    """True if now_min is in [start, end), handling windows spanning midnight."""
    if start_min <= end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


POLLING_DEFAULTS = {
    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    CONF_OFFPEAK_INTERVAL: DEFAULT_OFFPEAK_INTERVAL,
    CONF_OVERNIGHT_INTERVAL: DEFAULT_OVERNIGHT_INTERVAL,
    CONF_PEAK_AM_START: DEFAULT_PEAK_AM_START,
    CONF_PEAK_AM_END: DEFAULT_PEAK_AM_END,
    CONF_PEAK_PM_START: DEFAULT_PEAK_PM_START,
    CONF_PEAK_PM_END: DEFAULT_PEAK_PM_END,
    CONF_OVERNIGHT_START: DEFAULT_OVERNIGHT_START,
    CONF_OVERNIGHT_END: DEFAULT_OVERNIGHT_END,
    CONF_PEAK_ON_WEEKENDS: DEFAULT_PEAK_ON_WEEKENDS,
}


def polling_config(entry: ConfigEntry) -> dict:
    """Options override setup data, which overrides the defaults."""
    return {
        key: entry.options.get(key, entry.data.get(key, default))
        for key, default in POLLING_DEFAULTS.items()
    }


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an older config entry.

    Version 1 -> 2: prefix entity unique IDs with the entry ID, so two entries
    for the same line no longer collide. Entity IDs and history are kept.
    """
    if entry.version > 2:
        # Downgraded from a newer version of the integration.
        return False
    if entry.version == 1:
        prefix = f"{entry.entry_id}_"

        @callback
        def _add_prefix(reg_entry: er.RegistryEntry) -> dict | None:
            if reg_entry.unique_id.startswith(prefix):
                return None
            return {"new_unique_id": prefix + reg_entry.unique_id}

        await er.async_migrate_entries(hass, entry.entry_id, _add_prefix)
        try:
            hass.config_entries.async_update_entry(entry, version=2)
        except TypeError:
            # Home Assistant before 2024.1 has no version argument.
            entry.version = 2
            hass.config_entries.async_update_entry(entry)
        _LOGGER.info("Migrated PTV entry '%s' to version 2", entry.title)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api_key = entry.data[CONF_API_KEY]
    monitored_lines = entry.data[CONF_LINES]
    mode = entry.data.get(CONF_MODE, DEFAULT_MODE)
    polling = polling_config(entry)
    scan_interval = polling[CONF_SCAN_INTERVAL]
    alert_effects = set(int(e) for e in entry.data.get(CONF_ALERT_EFFECTS, DEFAULT_ALERT_EFFECTS))
    stations_config = entry.data.get(CONF_STATIONS, [])

    routes = routes_for_mode(mode)
    route_ids = {routes[l] for l in monitored_lines if l in routes}
    station_names = [s[CONF_STATION_NAME] for s in stations_config]

    # Resolve stop IDs for each station
    station_stop_ids: dict[str, dict] = {}
    gtfs_cache: dict = {}

    if station_names:
        # Hardcoded fast path for known stations (train only)
        if mode == MODE_TRAIN:
            for name in station_names:
                key = name.lower()
                if key in KNOWN_STOP_IDS:
                    station_stop_ids[key] = KNOWN_STOP_IDS[key]
                    _LOGGER.info("Using hardcoded stop_ids for '%s'", name)

        # Download GTFS static for timetable + any unknown stations
        try:
            gtfs_cache = await hass.async_add_executor_job(
                download_and_cache_gtfs,
                hass.config.config_dir,
                station_names,
                route_ids,
                mode,
                entry.entry_id,
            )
            # Fill in stop IDs for any stations not hardcoded
            for name in station_names:
                key = name.lower()
                if key not in station_stop_ids:
                    ids = get_stop_ids(gtfs_cache, name)
                    if ids:
                        station_stop_ids[key] = {"stop_ids": ids}
                        _LOGGER.info("Resolved '%s' stop_ids from GTFS: %s", name, ids)
                    else:
                        _LOGGER.warning("Could not resolve stop_ids for '%s'", name)
        except Exception as err:
            _LOGGER.warning("GTFS static load failed: %s", err)

    coordinator = PTVDataCoordinator(
        hass,
        api_key=api_key,
        monitored_lines=monitored_lines,
        scan_interval=scan_interval,
        alert_effects=alert_effects,
        stations_config=stations_config,
        station_stop_ids=station_stop_ids,
        gtfs_cache=gtfs_cache,
        polling=polling,
        mode=mode,
        entry_id=entry.entry_id,
    )

    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.async_add_executor_job(
        remove_gtfs_cache, hass.config.config_dir, entry.entry_id)


class PTVDataCoordinator(DataUpdateCoordinator):

    def __init__(self, hass, api_key, monitored_lines, scan_interval,
                 alert_effects, stations_config, station_stop_ids, gtfs_cache,
                 polling=None, mode=DEFAULT_MODE, entry_id=""):
        self.api_key = api_key
        self.entry_id = entry_id  # prefixes entity unique IDs
        self.mode = mode
        self.api_base = api_base_for_mode(mode)
        self._last_feeds: dict[str, tuple[bytes, datetime]] = {}
        self._stale_feeds: set[str] = set()
        self.monitored_lines = monitored_lines
        self.alert_effects = alert_effects        # set of int effect values to include
        self.stations_config = stations_config   # list of {name, directions, count}
        self.station_stop_ids = station_stop_ids  # key -> {stop_ids, citybound_stop_ids, outbound_stop_ids}
        self.gtfs_cache = gtfs_cache
        self._gtfs_retry_after: datetime | None = None
        self.polling = {
            CONF_SCAN_INTERVAL: scan_interval,
            CONF_OFFPEAK_INTERVAL: DEFAULT_OFFPEAK_INTERVAL,
            CONF_OVERNIGHT_INTERVAL: DEFAULT_OVERNIGHT_INTERVAL,
            CONF_PEAK_AM_START: DEFAULT_PEAK_AM_START,
            CONF_PEAK_AM_END: DEFAULT_PEAK_AM_END,
            CONF_PEAK_PM_START: DEFAULT_PEAK_PM_START,
            CONF_PEAK_PM_END: DEFAULT_PEAK_PM_END,
            CONF_OVERNIGHT_START: DEFAULT_OVERNIGHT_START,
            CONF_OVERNIGHT_END: DEFAULT_OVERNIGHT_END,
            CONF_PEAK_ON_WEEKENDS: DEFAULT_PEAK_ON_WEEKENDS,
            **(polling or {}),
        }
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=self._interval_for_now(),
        )

    def _interval_for_now(self) -> timedelta:
        """Poll fast during commute peaks, slowly overnight."""
        p = self.polling
        now = datetime.now().astimezone()
        now_min = now.hour * 60 + now.minute

        peak_applies = p[CONF_PEAK_ON_WEEKENDS] or now.weekday() < 5
        if peak_applies and (
            _within(now_min, _minutes(p[CONF_PEAK_AM_START]), _minutes(p[CONF_PEAK_AM_END]))
            or _within(now_min, _minutes(p[CONF_PEAK_PM_START]), _minutes(p[CONF_PEAK_PM_END]))
        ):
            return timedelta(seconds=int(p[CONF_SCAN_INTERVAL]))

        if _within(now_min, _minutes(p[CONF_OVERNIGHT_START]), _minutes(p[CONF_OVERNIGHT_END])):
            return timedelta(seconds=int(p[CONF_OVERNIGHT_INTERVAL]))

        return timedelta(seconds=int(p[CONF_OFFPEAK_INTERVAL]))

    def _secs_since_midnight(self) -> int:
        n = datetime.now().astimezone()
        return n.hour * 3600 + n.minute * 60 + n.second

    def _secs_to_dt(self, secs: int, service_date: str = "") -> datetime:
        """Timetable seconds as a time on service_date (YYYYMMDD), or today.

        Just after midnight the feed still carries yesterday's trips, whose
        times run past 24:00:00.
        """
        midnight = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0)
        if service_date:
            try:
                midnight = datetime.strptime(service_date, "%Y%m%d").astimezone()
            except ValueError:
                pass
        return midnight + timedelta(seconds=secs)

    def _fetch_feed(self, path: str) -> bytes:
        """Fetch a feed, falling back to its last good copy on a blip.

        The PTV API occasionally stalls on a single request. Failing the
        whole update for that marks every entity unavailable, which breaks
        any automation that happens to run then. A copy up to
        FEED_MAX_STALE old is still accurate enough: the parsers drop
        departures already in the past.
        """
        try:
            resp = requests.get(
                f"{self.api_base}{path}",
                headers={API_HEADER_KEY: self.api_key},
                timeout=(10, 25),
            )
            resp.raise_for_status()
        except requests.RequestException as err:
            cached = self._last_feeds.get(path)
            if cached and datetime.now(timezone.utc) - cached[1] < FEED_MAX_STALE:
                if path not in self._stale_feeds:
                    _LOGGER.warning(
                        "PTV %s request failed (%s); using data from %s ago",
                        path, err, datetime.now(timezone.utc) - cached[1],
                    )
                    self._stale_feeds.add(path)
                return cached[0]
            raise
        if path in self._stale_feeds:
            _LOGGER.info("PTV %s recovered", path)
            self._stale_feeds.discard(path)
        self._last_feeds[path] = (resp.content, datetime.now(timezone.utc))
        return resp.content

    async def _async_refresh_timetable(self) -> None:
        """Reload the static timetable once the day has rolled over.

        It is loaded at setup and holds that day's services only, so without
        this, departures after midnight would keep coming from the day Home
        Assistant started — a weekday timetable on a Saturday, for instance.
        """
        if not self.gtfs_cache or not self.stations_config:
            return
        now = datetime.now().astimezone()
        if self.gtfs_cache.get("cached_date") == now.strftime("%Y%m%d"):
            return
        if self._gtfs_retry_after and now < self._gtfs_retry_after:
            return
        routes = routes_for_mode(self.mode)
        try:
            cache = await self.hass.async_add_executor_job(
                download_and_cache_gtfs,
                self.hass.config.config_dir,
                [s[CONF_STATION_NAME] for s in self.stations_config],
                {routes[l] for l in self.monitored_lines if l in routes},
                self.mode,
                self.entry_id,
            )
        except Exception as err:
            # Keep yesterday's timetable rather than none, and don't retry the
            # download on every poll.
            _LOGGER.warning("Daily timetable refresh failed: %s", err)
            self._gtfs_retry_after = now + GTFS_REFRESH_RETRY
            return
        self._gtfs_retry_after = None
        if cache.get("cached_date") != now.strftime("%Y%m%d"):
            # download_and_cache_gtfs fell back to a stale copy
            self._gtfs_retry_after = now + GTFS_REFRESH_RETRY
        self.gtfs_cache = cache
        _LOGGER.info("Timetable refreshed for %s", cache.get("cached_date"))

    async def _async_update_data(self) -> dict:
        # _schedule_refresh re-reads this after every poll, so retuning here
        # takes effect from the next one.
        interval = self._interval_for_now()
        if interval != self.update_interval:
            _LOGGER.debug("Polling interval now %s", interval)
            self.update_interval = interval
        await self._async_refresh_timetable()
        try:
            trip_raw, alerts_raw, pos_raw = await self.hass.async_add_executor_job(
                lambda: (
                    self._fetch_feed(API_TRIP_UPDATES_PATH),
                    self._fetch_feed(API_SERVICE_ALERTS_PATH),
                    self._fetch_feed(API_VEHICLE_POSITIONS_PATH),
                )
            )
            return self._parse_all(trip_raw, alerts_raw, pos_raw)
        except requests.RequestException as err:
            raise UpdateFailed(f"PTV API error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Feed parse error: {err}") from err

    # ------------------------------------------------------------------ #
    # Data structure helpers
    # ------------------------------------------------------------------ #

    def _empty_station_data(self, station_cfg: dict) -> dict:
        name = station_cfg[CONF_STATION_NAME]
        directions = station_cfg.get(CONF_STATION_DIRECTIONS, DIR_BOTH)
        count = station_cfg.get(CONF_STATION_COUNT, DEFAULT_STATION_COUNT)
        return {
            "name": name,
            "directions": directions,
            "count": count,
            # Next N trains per direction: list of {dt_iso, delay_sec, is_realtime}
            "citybound": [],
            "outbound": [],
        }

    def _empty_line_data(self) -> dict:
        return {
            # Trip-level
            "delayed_trips": [],
            "cancelled_trips": [],
            "max_delay_seconds": 0,
            "delay_count": 0,
            "has_delays": False,
            "has_cancellations": False,
            # Alerts
            "alerts": [],
            "alert_count": 0,
            "live_alert_count": 0,
            "alert_text": "No active alerts",
            "live_alert_text": "",
            "overnight_works_text": "",
            # Vehicles
            "train_count": 0,
            "worst_occupancy": "Unknown",
            "worst_occupancy_int": -1,
            # Problem summary
            "problem_level": PROBLEM_NONE,
            "problem_summary": "Running normally",
            # Per-station data
            "stations": {},
        }

    # ------------------------------------------------------------------ #
    # Main parse
    # ------------------------------------------------------------------ #

    def _parse_all(self, trip_raw, alerts_raw, pos_raw) -> dict:
        routes = routes_for_mode(self.mode)
        route_map = {routes[l]: l for l in self.monitored_lines if l in routes}

        results = {line: self._empty_line_data() for line in self.monitored_lines}

        # Initialise per-station buckets
        for line in self.monitored_lines:
            for scfg in self.stations_config:
                key = scfg[CONF_STATION_NAME].lower()
                results[line]["stations"][key] = self._empty_station_data(scfg)

        self._parse_trip_updates(trip_raw, route_map, results)
        self._parse_service_alerts(alerts_raw, route_map, results)
        self._parse_vehicle_positions(pos_raw, route_map, results)
        self._apply_static_fallback(results)
        self._build_problem_summary(results)

        return results

    # ------------------------------------------------------------------ #
    # Trip updates
    # ------------------------------------------------------------------ #

    @staticmethod
    def _stu_delay(stu) -> int:
        if stu.HasField("departure"):
            return stu.departure.delay or 0
        if stu.HasField("arrival"):
            return stu.arrival.delay or 0
        return 0

    def _platform_directions(self) -> dict[str, int]:
        """Map platform stop_id -> direction, for platforms the timetable
        shows serving one direction only (terminus platforms are left out).

        Last resort for a realtime trip whose direction is in neither the feed
        nor trip_directions, e.g. one added on the day.
        """
        cache = self.gtfs_cache or {}
        through = cache.get("through_trips", {})
        seen: dict[str, set[int]] = {}
        for deps in cache.get("scheduled_by_station", {}).values():
            for dep in deps:
                # Through trips are stored with direction flipped to read
                # relative to the selected line; only use the line's own trips.
                if dep.get("trip_id") in through:
                    continue
                seen.setdefault(dep["stop_id"], set()).add(dep["direction_id"])
        return {sid: next(iter(d)) for sid, d in seen.items() if len(d) == 1}

    def _trip_direction(self, tu, trip_dirs, platform_dirs) -> int | None:
        """The trip's direction_id, or None if it can't be determined.

        PTV's metro trip updates omit trip.direction_id, which protobuf then
        reads as 0 (outbound), so only trust it when it is actually set.
        """
        if tu.trip.HasField("direction_id"):
            return tu.trip.direction_id
        direction = trip_dirs.get(tu.trip.trip_id)
        if direction is not None:
            return direction
        for stu in tu.stop_time_update:
            if stu.stop_id in platform_dirs:
                return platform_dirs[stu.stop_id]
        return None

    def _station_hits(self, stus, direction_id, is_express, now_utc,
                      trip_id: str = "", scheduled: dict | None = None,
                      start_date: str = ""):
        """First future stop at each configured station for one trip.

        scheduled is {station_key: {trip_id: timetabled departure}}, used to
        fill stations the trip's update leaves out (see _estimated_hit).
        Returns {station_key: hit or None}.
        """
        hits: dict[str, dict | None] = {
            scfg[CONF_STATION_NAME].lower(): None for scfg in self.stations_config
        }
        for stu in stus:
            delay = self._stu_delay(stu)
            dep_time = None
            if stu.HasField("departure"):
                if stu.departure.time:
                    dep_time = datetime.fromtimestamp(stu.departure.time, tz=timezone.utc)
            # Arrival only is where the trip terminates, so nothing departs.
            if not dep_time or dep_time <= now_utc:
                continue
            for key in hits:
                if hits[key] is not None:
                    continue  # already found for this trip
                info = self.station_stop_ids.get(key, {})
                # Use direction-specific stop IDs if available
                if direction_id == 1:
                    stop_ids = info.get("citybound_stop_ids", info.get("stop_ids", []))
                else:
                    stop_ids = info.get("outbound_stop_ids", info.get("stop_ids", []))
                if stu.stop_id in stop_ids:
                    hits[key] = {"actual": dep_time, "delay_sec": delay, "is_express": is_express}
        for key, hit in hits.items():
            if hit is None and scheduled:
                dep = scheduled.get(key, {}).get(trip_id)
                if dep and dep["direction_id"] == direction_id:
                    hits[key] = self._estimated_hit(
                        stus, dep, is_express, now_utc, start_date)
        return hits

    def _estimated_hit(self, stus, dep, is_express, now_utc,
                       start_date: str = "") -> dict | None:
        """Realtime departure at a stop the trip update leaves out.

        PTV's trip updates sometimes skip intermediate stops (stop_sequence
        gaps), so a running train would drop back to the timetable at that
        station. Per GTFS-realtime, a stop with no update takes the delay of
        the nearest earlier stop that has one. Only fill a stop that has
        updates on both sides, so a train that no longer calls there isn't
        invented.
        """
        seq = dep.get("stop_sequence") or 0
        if not seq:
            return None
        # The update covers this stop after all: departed, skipped or no data.
        if any(s.stop_sequence == seq or s.stop_id == dep["stop_id"] for s in stus):
            return None
        # 1=SKIPPED and 2=NO_DATA carry no delay to go by
        timed = [s for s in stus if s.schedule_relationship not in (1, 2)]
        before = [s for s in timed if s.stop_sequence and s.stop_sequence < seq]
        after = [s for s in timed if s.stop_sequence and s.stop_sequence > seq]
        if not before or not after:
            return None
        delay = self._stu_delay(max(before, key=lambda s: s.stop_sequence))
        actual = self._secs_to_dt(
            dep["dep_seconds"] + delay, start_date).astimezone(timezone.utc)
        if actual <= now_utc:
            return None
        return {"actual": actual, "delay_sec": delay, "is_express": is_express}

    @staticmethod
    def _is_other_day(tu) -> bool:
        """Whether the trip belongs to another service day than today's.

        Trip IDs repeat daily, so yesterday's late trips must not be mistaken
        for today's entries of the same ID in the timetable.
        """
        today = datetime.now().astimezone().strftime("%Y%m%d")
        return bool(tu.trip.start_date) and tu.trip.start_date != today

    def _no_longer_calls(self, stus, dep, now_utc) -> bool:
        """Whether the trip has departed, passed or will skip a station."""
        if dep is None:
            return False
        seq = dep.get("stop_sequence") or 0
        for s in stus:
            if s.stop_id == dep["stop_id"] or (seq and s.stop_sequence == seq):
                when = s.departure.time or s.arrival.time
                return s.schedule_relationship == 1 or bool(
                    when and when <= now_utc.timestamp())
        seqs = [s.stop_sequence for s in stus if s.stop_sequence]
        return bool(seq and seqs) and min(seqs) > seq

    def _parse_trip_updates(self, raw, route_map, results):
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(raw)
        now_utc = datetime.now(timezone.utc)

        # Buckets: line -> station_key -> direction_id -> list of {actual, delay_sec}
        rt_buckets: dict[str, dict[str, dict[int, list]]] = {}
        for line in self.monitored_lines:
            rt_buckets[line] = {}
            for scfg in self.stations_config:
                key = scfg[CONF_STATION_NAME].lower()
                rt_buckets[line][key] = {0: [], 1: []}

        # Tunnel trains are split at Town Hall: a Pakenham line's trains carry
        # on as Sunbury trips (and vice versa). Those continuations, linked by
        # block in the timetable, feed the line's station departures with
        # direction flipped — but not its delay or cancellation stats.
        through_trips = (self.gtfs_cache or {}).get("through_trips", {})
        trip_dirs = (self.gtfs_cache or {}).get("trip_directions", {})
        handover_stops = set(get_stop_ids(self.gtfs_cache or {}, "Town Hall"))
        # station_key -> trip_id -> timetabled departure, to fill stations
        # missing from a trip's update
        scheduled = {
            key: {d["trip_id"]: d for d in deps}
            for key, deps in (self.gtfs_cache or {}).get("scheduled_by_station", {}).items()
        }
        platform_dirs = self._platform_directions()
        unknown_direction = 0
        # station_key -> trips the timetable fallback must not list: already
        # shown live, cancelled, or past the station
        self._handled_trips: dict[str, set[str]] = {
            scfg[CONF_STATION_NAME].lower(): set() for scfg in self.stations_config
        }

        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu = entity.trip_update
            route_id = tu.trip.route_id
            line = route_map.get(route_id)
            is_express = False
            if line is None:
                # Also match route ID variants e.g. "aus:vic:vic-02-HBE:E" (express)
                for base_id, lname in route_map.items():
                    if route_id.startswith(base_id):
                        line = lname
                        is_express = True
                        break
            trip_id = tu.trip.trip_id
            continues_as = route_map.get(through_trips.get(trip_id, ""))
            continuing = [continues_as] if continues_as and continues_as != line else []
            if line is None and not continuing:
                continue
            direction_id = self._trip_direction(tu, trip_dirs, platform_dirs)

            # 3=CANCELED, 5=DELETED
            if tu.trip.schedule_relationship in (3, 5):
                if line is not None:
                    results[line]["cancelled_trips"].append(trip_id)
                    results[line]["has_cancellations"] = True
                if not self._is_other_day(tu):
                    for handled in self._handled_trips.values():
                        handled.add(trip_id)
                continue

            # A citybound tunnel trip ends at the Town Hall handover, where the
            # train continues as another trip; that final stop is an arrival.
            # Updates often stop short of it (at State Library, or further out
            # before the train has full predictions), so only drop a last
            # stop that really is Town Hall.
            stus = list(tu.stop_time_update)
            if (direction_id == 1 and stus and stus[-1].stop_id in handover_stops
                    and any(route_id.startswith(r) for r in TUNNEL_SIDES)):
                stus = stus[:-1]
            trip_max_delay = max((self._stu_delay(stu) for stu in stus), default=0)

            if direction_id is None:
                # Guessing would put the train under the wrong direction's
                # departures; it still counts towards the line's delays.
                unknown_direction += 1
                targets = []
            else:
                targets = ([(line, direction_id)] if line is not None else []) + \
                    [(l, 1 - direction_id) for l in continuing]
            for tline, tdir in targets:
                hits = self._station_hits(
                    stus, tdir, is_express, now_utc, trip_id, scheduled, tu.trip.start_date)
                for key, hit in hits.items():
                    if hit:
                        rt_buckets[tline][key][tdir].append(hit)
                    elif not self._no_longer_calls(
                            stus, scheduled.get(key, {}).get(trip_id), now_utc):
                        continue
                    if not self._is_other_day(tu):
                        self._handled_trips[key].add(trip_id)

            if line is None:
                continue
            data = results[line]
            # Record line-level delay
            if trip_max_delay >= DEFAULT_DELAY_THRESHOLD:
                data["delayed_trips"].append({"trip_id": trip_id, "delay_seconds": trip_max_delay})
                data["delay_count"] += 1
                data["max_delay_seconds"] = max(data["max_delay_seconds"], trip_max_delay)
                data["has_delays"] = True

        if unknown_direction:
            _LOGGER.debug(
                "%d realtime trips had no known direction; left out of departures",
                unknown_direction,
            )

        # Sort and store realtime departures per station/direction
        for line in self.monitored_lines:
            for scfg in self.stations_config:
                key = scfg[CONF_STATION_NAME].lower()
                count = scfg.get(CONF_STATION_COUNT, DEFAULT_STATION_COUNT)
                for dir_id, dir_key in [(1, "citybound"), (0, "outbound")]:
                    hits = sorted(rt_buckets[line][key][dir_id], key=lambda x: x["actual"])
                    results[line]["stations"][key][dir_key] = [
                        {
                            "dt_iso": h["actual"].isoformat(),
                            "delay_sec": h["delay_sec"],
                            "is_realtime": True,
                            "is_express": h["is_express"],
                        }
                        for h in hits[:count]
                    ]

    # ------------------------------------------------------------------ #
    # Static timetable fallback
    # ------------------------------------------------------------------ #

    def _apply_static_fallback(self, results):
        if not self.gtfs_cache:
            return
        now_sec = self._secs_since_midnight()

        for line in self.monitored_lines:
            for scfg in self.stations_config:
                key = scfg[CONF_STATION_NAME].lower()
                count = scfg.get(CONF_STATION_COUNT, DEFAULT_STATION_COUNT)
                directions = scfg.get(CONF_STATION_DIRECTIONS, DIR_BOTH)
                station_data = results[line]["stations"][key]

                for dir_id, dir_key in [(1, "citybound"), (0, "outbound")]:
                    if directions == DIR_CITYBOUND and dir_id == 0:
                        continue
                    if directions == DIR_OUTBOUND and dir_id == 1:
                        continue

                    existing = station_data[dir_key]
                    needed = count - len(existing)
                    if needed <= 0:
                        continue

                    # Trips already accounted for by the live feed are left
                    # out by ID; cutting off at the last live time instead
                    # would hide any earlier service the feed has no time for.
                    scheduled = get_next_n_scheduled(
                        self.gtfs_cache, key, dir_id, now_sec, needed,
                        self._handled_trips.get(key, set()),
                    )
                    for sched in scheduled:
                        dt = self._secs_to_dt(sched["dep_seconds"])
                        station_data[dir_key].append({
                            "dt_iso": dt.isoformat(),
                            "delay_sec": 0,
                            "is_realtime": False,
                            "is_express": False,  # not derivable from static schedule
                        })
                    existing.sort(key=lambda d: datetime.fromisoformat(d["dt_iso"]))

    # ------------------------------------------------------------------ #
    # Service alerts
    # ------------------------------------------------------------------ #

    def _parse_service_alerts(self, raw, route_map, results):
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(raw)
        now_ts = datetime.now(timezone.utc).timestamp()

        cause_labels = {
            1:"Unknown cause",2:"Other cause",3:"Technical problem",
            4:"Strike",5:"Demonstration",6:"Accident",7:"Holiday",
            8:"Weather",9:"Maintenance",10:"Construction",
            11:"Police activity",12:"Medical emergency",
        }
        effect_labels = {
            1:"No service",2:"Reduced service",3:"Significant delays",
            4:"Detour",5:"Additional service",6:"Modified service",
            7:"Other effect",8:"Unknown effect",9:"Stop moved",
            10:"No effect",11:"Accessibility issue",
        }

        for entity in feed.entity:
            if not entity.HasField("alert"):
                continue
            alert = entity.alert
            active = not alert.active_period or any(
                (p.start or 0) <= now_ts <= (p.end or float("inf"))
                for p in alert.active_period
            )
            if not active:
                continue

            if alert.effect not in self.alert_effects:
                continue

            def best_text(t):
                if not t.translation: return ""
                for tr in t.translation:
                    if tr.language in ("en","en-AU",""): return tr.text
                return t.translation[0].text

            header = best_text(alert.header_text)
            description = best_text(alert.description_text)

            # Overnight works stay listed outside their hours, but tagged with
            # the window and not counted as a live disruption.
            window_from = None
            in_window = True
            for p in alert.active_period:
                window_from = _overnight_window(
                    f"{header} {description}", p.start, p.end
                )
                if window_from:
                    in_window = _in_overnight_window(
                        datetime.now().astimezone(), p.start
                    )
                    break

            alert_dict = {
                "header": header,
                "description": description,
                "cause": cause_labels.get(alert.cause,""),
                "effect": effect_labels.get(alert.effect,""),
                "is_overnight_works": bool(window_from),
                "window_from": window_from or "",
                "window_to": "last service" if window_from else "",
                "active_now": in_window,
            }

            matched: set[str] = set()
            for informed in alert.informed_entity:
                rid = informed.route_id
                if rid in route_map:
                    matched.add(route_map[rid])
                elif rid:
                    for base_id, lname in route_map.items():
                        if rid.startswith(base_id):
                            matched.add(lname)
                elif not informed.trip.trip_id:
                    matched.update(self.monitored_lines)

            for line in matched:
                if line in results:
                    results[line]["alerts"].append(alert_dict)

        for line in self.monitored_lines:
            data = results[line]
            alerts = data["alerts"]
            data["alert_count"] = len(alerts)
            # Overnight works outside their hours shouldn't flag the line as
            # disrupted; they stay in alert_text, tagged with their window.
            data["live_alert_count"] = sum(1 for a in alerts if a["active_now"])

            texts: list[str] = []          # everything, works tagged with hours
            live_texts: list[str] = []     # happening now
            tonight_texts: list[str] = []  # works starting later tonight
            for a in alerts:
                text = a["header"] or a["description"]
                if a["effect"] and a["effect"] not in text:
                    text = f"{a['effect']}: {text}" if text else a["effect"]
                if not text:
                    continue
                if a["is_overnight_works"] and not a["active_now"]:
                    window = f"{a['window_from']}–{a['window_to']}"
                    tonight_texts.append(f"{window}: {text}")
                    texts.append(f"Tonight {window}: {text}")
                else:
                    live_texts.append(text)
                    texts.append(text)

            if alerts:
                data["alert_text"] = " | ".join(texts) if texts else "Active disruption"
            else:
                data["alert_text"] = "No active alerts"
            data["live_alert_text"] = " | ".join(live_texts)
            data["overnight_works_text"] = " | ".join(tonight_texts)

    # ------------------------------------------------------------------ #
    # Vehicle positions
    # ------------------------------------------------------------------ #

    def _parse_vehicle_positions(self, raw, route_map, results):
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(raw)
        counts = {l: 0 for l in self.monitored_lines}
        worst = {l: -1 for l in self.monitored_lines}

        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            vp = entity.vehicle
            rid = vp.trip.route_id
            line = route_map.get(rid)
            if line is None:
                for base_id, lname in route_map.items():
                    if rid.startswith(base_id):
                        line = lname
                        break
            if line is None:
                continue
            counts[line] += 1
            if vp.occupancy_status > worst[line]:
                worst[line] = vp.occupancy_status

        for line in self.monitored_lines:
            results[line]["train_count"] = counts[line]
            occ = worst[line]
            results[line]["worst_occupancy"] = OCCUPANCY_LABELS.get(occ,"Unknown") if occ >= 0 else "Unknown"
            results[line]["worst_occupancy_int"] = occ

    # ------------------------------------------------------------------ #
    # Problem summary
    # ------------------------------------------------------------------ #

    def _build_problem_summary(self, results):
        for line in self.monitored_lines:
            data = results[line]
            has_alerts = data.get("live_alert_count", data["alert_count"]) > 0
            has_cancellations = data["has_cancellations"]
            has_delays = data["has_delays"]
            max_min = round(data["max_delay_seconds"] / 60)

            # Determine level
            if has_cancellations and (has_alerts or max_min >= 15):
                level = PROBLEM_MAJOR
            elif has_cancellations:
                level = PROBLEM_CANCELLATIONS
            elif has_alerts:
                level = PROBLEM_ALERT
            elif has_delays:
                level = PROBLEM_DELAYS
            else:
                level = PROBLEM_NONE

            data["problem_level"] = level

            # Build TTS-friendly summaries. problem_summary lists overnight
            # works even out of hours (tagged with their window);
            # problem_summary_live covers only what is happening right now.
            disruptions = []
            if has_cancellations:
                n = len(data["cancelled_trips"])
                disruptions.append(f"{n} service{'s' if n!=1 else ''} cancelled")
            if has_delays:
                disruptions.append(
                    f"{data['delay_count']} service{'s' if data['delay_count']!=1 else ''} "
                    f"delayed up to {max_min} minute{'s' if max_min!=1 else ''}"
                )

            def summarise(alert_part: str) -> str:
                parts = ([alert_part] if alert_part else []) + disruptions
                if not parts:
                    return f"{line_phrase(self.mode, line)} is running normally."
                return f"{line_phrase(self.mode, line)}: " + "; ".join(parts) + "."

            data["problem_summary"] = summarise(
                data["alert_text"] if data["alert_count"] > 0 else ""
            )
            data["problem_summary_live"] = summarise(data["live_alert_text"])
