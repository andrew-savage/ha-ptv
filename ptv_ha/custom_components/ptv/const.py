"""Constants for the PTV integration."""

DOMAIN = "ptv"

# API - Realtime feeds
API_BASE_URL = "https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/metro"
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
GTFS_METRO_INNER_ZIP = "2/google_transit.zip"

# Config keys
CONF_API_KEY = "api_key"
CONF_LINES = "lines"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_STATIONS = "stations"          # list of station config dicts
CONF_ALERT_EFFECTS = "alert_effects"  # list of GTFS effect int values (as strings) to include

# GTFS alert effect values → human-readable labels
# Value 0 = unknown/unset; 1-11 match the GTFS spec
ALERT_EFFECT_OPTIONS = {
    "1":  "No service — full line or station suspension",
    "2":  "Reduced service — fewer trains than normal",
    "3":  "Significant delays — major disruption in progress",
    "4":  "Detour — trains taking an alternative route",
    "5":  "Additional service — extra trains added",
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
