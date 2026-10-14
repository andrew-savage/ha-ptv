"""Constants for the PTV integration."""

DOMAIN = "ptv"

# API - Realtime feeds
API_ROOT = "https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1"
# Kept for backwards compatibility; train (metro) is the original mode.
API_BASE_URL = f"{API_ROOT}/metro"
API_TRIP_UPDATES_PATH = "/trip-updates"
API_SERVICE_ALERTS_PATH = "/service-alerts"
API_VEHICLE_POSITIONS_PATH = "/vehicle-positions"
API_HEADER_KEY = "KeyID"

# GTFS Static
GTFS_STATIC_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
    "fb152201-859f-4882-9206-b768060b50ad/download/gtfs.zip"
)
GTFS_CACHE_FILE = "ptv_gtfs_cache.json"
GTFS_CACHE_TTL_DAYS = 3
# Bump when cached content changes meaning, so old caches are rebuilt.
# 2: Metro Tunnel through-running departures.
GTFS_CACHE_SCHEMA = 2
GTFS_DOWNLOAD_TIMEOUT = 420
GTFS_METRO_INNER_ZIP = "2/google_transit.zip"
GTFS_TRAM_INNER_ZIP = "3/google_transit.zip"

# Config keys
CONF_API_KEY = "api_key"
CONF_LINES = "lines"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_STATIONS = "stations"          # list of station config dicts
CONF_ALERT_EFFECTS = "alert_effects"  # list of GTFS effect int values (as strings) to include

# Adaptive polling. CONF_SCAN_INTERVAL is the peak-hour rate; the other two
# tiers apply outside the peak windows. All are editable via the options flow.
CONF_OFFPEAK_INTERVAL = "offpeak_interval"
CONF_OVERNIGHT_INTERVAL = "overnight_interval"
CONF_PEAK_AM_START = "peak_am_start"
CONF_PEAK_AM_END = "peak_am_end"
CONF_PEAK_PM_START = "peak_pm_start"
CONF_PEAK_PM_END = "peak_pm_end"
CONF_OVERNIGHT_START = "overnight_start"
CONF_OVERNIGHT_END = "overnight_end"
CONF_PEAK_ON_WEEKENDS = "peak_on_weekends"

# GTFS alert effect values → human-readable labels
# Value 0 = unknown/unset; 1-11 match the GTFS spec
ALERT_EFFECT_OPTIONS = {
    "1":  "No service — full line or station suspension",
    "2":  "Reduced service — fewer services than normal",
    "3":  "Significant delays — major disruption in progress",
    "4":  "Detour — services taking an alternative route",
    "5":  "Additional service — extra services added",
    "6":  "Modified service — timetable or stopping pattern changed",
    "7":  "Other effect — e.g. car park closures, lift outages",
    "8":  "Unknown effect — unclassified alert",
    "9":  "Stop moved — platform or stop location change",
    "10": "No effect — informational notice only",
    "11": "Accessibility issue — lifts, ramps or accessibility equipment",
}

# Default: include meaningful service disruptions only (excludes "Other", "Unknown", "No effect")
DEFAULT_ALERT_EFFECTS = ["1", "2", "3", "4", "6"]

# Per-station config keys
CONF_STATION_NAME = "name"
CONF_STATION_DIRECTIONS = "directions"   # "citybound", "outbound", "both"
CONF_STATION_COUNT = "count"            # 1-4

# Direction values
DIR_CITYBOUND = "citybound"
DIR_OUTBOUND = "outbound"
DIR_BOTH = "both"

# Defaults
DEFAULT_SCAN_INTERVAL = 60
DEFAULT_OFFPEAK_INTERVAL = 300      # 5 min
DEFAULT_OVERNIGHT_INTERVAL = 900    # 15 min
DEFAULT_PEAK_AM_START = "06:00:00"
DEFAULT_PEAK_AM_END = "10:00:00"
DEFAULT_PEAK_PM_START = "16:00:00"
DEFAULT_PEAK_PM_END = "20:00:00"
DEFAULT_OVERNIGHT_START = "00:00:00"
DEFAULT_OVERNIGHT_END = "06:00:00"
DEFAULT_PEAK_ON_WEEKENDS = False
DEFAULT_DELAY_THRESHOLD = 300   # 5 min
DEFAULT_STATION_COUNT = 4
DEFAULT_DIRECTIONS = DIR_BOTH

# GTFS Route IDs — confirmed 2026-04-05
METRO_LINES = {
    "Alamein":       "aus:vic:vic-02-ALM:",
    "Belgrave":      "aus:vic:vic-02-BEG:",
    "Cranbourne":    "aus:vic:vic-02-CBE:",
    "Craigieburn":   "aus:vic:vic-02-CGB:",
    "Frankston":     "aus:vic:vic-02-FKN:",
    "Glen Waverley": "aus:vic:vic-02-GWY:",
    "Hurstbridge":   "aus:vic:vic-02-HBE:",
    "Lilydale":      "aus:vic:vic-02-LIL:",
    "Mernda":        "aus:vic:vic-02-MDD:",
    "Pakenham":      "aus:vic:vic-02-PKM:",
    "Sandringham":   "aus:vic:vic-02-SHM:",
    "Stony Point":   "aus:vic:vic-02-STY:",
    "Sunbury":       "aus:vic:vic-02-SUY:",
    "Upfield":       "aus:vic:vic-02-UFD:",
    "Werribee":      "aus:vic:vic-02-WER:",
    "Williamstown":  "aus:vic:vic-02-WIL:",
}

# Metro Tunnel through-running. Each train runs Sunbury <-> Pakenham/Cranbourne,
# but the timetable splits it at Town Hall into two trips: citybound (dir 1)
# into Town Hall on one line, then outbound (dir 0) on a line from the other
# side. So Pakenham trips never stop at Arden/Parkville/State Library and
# Sunbury trips never stop at Anzac. Confirmed 2026-10-14.
TUNNEL_SIDES = {
    "aus:vic:vic-02-SUY:": "west",
    "aus:vic:vic-02-PKM:": "east",
    "aus:vic:vic-02-CBE:": "east",
}


def through_running_routes(route_ids) -> set[str]:
    """Routes whose trips continue the given routes' trains through the tunnel.

    Their direction is reversed relative to the given routes: a Sunbury
    citybound trip at State Library is a train heading out to Pakenham.
    Empty when the selection already spans both sides (nothing is missing)
    or touches neither.
    """
    sides = {TUNNEL_SIDES[r] for r in route_ids if r in TUNNEL_SIDES}
    if len(sides) != 1:
        return set()
    (side,) = sides
    return {r for r, s in TUNNEL_SIDES.items() if s != side}


# Tram route IDs — confirmed 2026-08-05
TRAM_ROUTES = {
    "Route 1":   "aus:vic:vic-03-1:",
    "Route 3":   "aus:vic:vic-03-3:",
    "Route 5":   "aus:vic:vic-03-5:",
    "Route 6":   "aus:vic:vic-03-6:",
    "Route 11":  "aus:vic:vic-03-11:",
    "Route 12":  "aus:vic:vic-03-12:",
    "Route 16":  "aus:vic:vic-03-16:",
    "Route 19":  "aus:vic:vic-03-19:",
    "Route 30":  "aus:vic:vic-03-30:",
    "Route 35":  "aus:vic:vic-03-35:",
    "Route 48":  "aus:vic:vic-03-48:",
    "Route 57":  "aus:vic:vic-03-57:",
    "Route 58":  "aus:vic:vic-03-58:",
    "Route 59":  "aus:vic:vic-03-59:",
    "Route 64":  "aus:vic:vic-03-64:",
    "Route 67":  "aus:vic:vic-03-67:",
    "Route 70":  "aus:vic:vic-03-70:",
    "Route 72":  "aus:vic:vic-03-72:",
    "Route 75":  "aus:vic:vic-03-75:",
    "Route 78":  "aus:vic:vic-03-78:",
    "Route 82":  "aus:vic:vic-03-82:",
    "Route 86":  "aus:vic:vic-03-86:",
    "Route 96":  "aus:vic:vic-03-96:",
    "Route 109": "aus:vic:vic-03-109:",
}

# Transport modes. Each is a separate config entry.
CONF_MODE = "mode"
MODE_TRAIN = "train"
MODE_TRAM = "tram"
DEFAULT_MODE = MODE_TRAIN

MODES = {
    MODE_TRAIN: {
        "label": "Metro Train",
        "api_path": "metro",
        "gtfs_inner_zip": GTFS_METRO_INNER_ZIP,
        "routes": METRO_LINES,
        "route_word": "line",
        "stop_word": "station",
    },
    MODE_TRAM: {
        "label": "Tram",
        "api_path": "tram",
        "gtfs_inner_zip": GTFS_TRAM_INNER_ZIP,
        "routes": TRAM_ROUTES,
        "route_word": "route",
        "stop_word": "stop",
    },
}


def mode_info(mode: str) -> dict:
    """Mode table entry, defaulting to train for pre-mode config entries."""
    return MODES.get(mode or DEFAULT_MODE, MODES[DEFAULT_MODE])


def line_phrase(mode: str, line: str) -> str:
    """"Hurstbridge line" for trains; tram names already read "Route 58"."""
    return line if mode == MODE_TRAM else f"{line} line"


def vehicle_word(mode: str, plural: bool = False) -> str:
    return ("trams" if plural else "tram") if mode == MODE_TRAM else (
        "trains" if plural else "train")


def routes_for_mode(mode: str) -> dict:
    return mode_info(mode)["routes"]


def api_base_for_mode(mode: str) -> str:
    return f"{API_ROOT}/{mode_info(mode)['api_path']}"

# Hardcoded stop IDs for known stations (fast path, no GTFS download needed)
# Confirmed from GTFS static 2026-04-05
# Format: station_name_lower -> [stop_id, ...]
# direction_id=1 (citybound) always uses the first stop_id listed
# direction_id=0 (outbound) always uses the second
KNOWN_STOP_IDS = {
    "ivanhoe": {
        "stop_ids": ["13754", "13755"],
        "citybound_stop_ids": ["13754"],   # confirmed dir=1 uses 13754
        "outbound_stop_ids": ["13755"],    # confirmed dir=0 uses 13755
    },
}

# Problem level values (ordered by severity)
PROBLEM_NONE = "none"
PROBLEM_DELAYS = "delays"
PROBLEM_ALERT = "alert"
PROBLEM_CANCELLATIONS = "cancellations"
PROBLEM_MAJOR = "major"

# GTFS occupancy -> human readable
OCCUPANCY_LABELS = {
    0: "Empty",
    1: "Many seats available",
    2: "Few seats available",
    3: "Standing room only",
    4: "Crushed standing",
    5: "Full",
    6: "Not accepting passengers",
}
