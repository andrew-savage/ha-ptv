# PTV Home Assistant Integration — Claude Code Context

## What this project is

A Home Assistant custom integration for Public Transport Victoria (PTV) metro train services in Melbourne, Australia. It polls three GTFS Realtime feeds and a static GTFS timetable to provide departure times, delays, service alerts, and occupancy data as HA sensors.

## Architecture

```
custom_components/ptv/
├── __init__.py        # Entry setup + PTVDataCoordinator (main data fetching/parsing)
├── const.py           # All constants: domain, route IDs, config keys, problem levels
├── gtfs_static.py     # Downloads/caches GTFS static zip; provides scheduled departures
├── config_flow.py     # HA UI setup wizard (3 steps: API key → lines → stations)
├── sensor.py          # All sensor entities
├── binary_sensor.py   # Binary sensor entities
├── strings.json       # UI strings
└── translations/en.json
```

## Key concepts

### Feeds polled (every 60s by default)
- `/trip-updates` — per-trip arrival/departure times and delays
- `/service-alerts` — published disruption messages
- `/vehicle-positions` — live GPS + occupancy per train

### API authentication
Header: `KeyID: <api_key>`
Base URL: `https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/metro`

### Route IDs (confirmed from live feed 2026-04-05)
Format: `aus:vic:vic-02-{CODE}:` e.g. `aus:vic:vic-02-HBE:` for Hurstbridge

### Stop IDs (confirmed from GTFS static 2026-04-05)
Ivanhoe: `13754` (citybound, dir=1), `13755` (outbound, dir=0)
The GTFS static zip structure: outer zip → `2/google_transit.zip` → standard GTFS files

### Static timetable
Downloaded once from Transport Victoria, cached to `config/ptv_gtfs_cache.json`.
Invalidated daily (new day) or every 3 days.
Used as fallback when realtime feed has no upcoming departure for a station.

### Problem levels
`none` → `delays` → `alert` → `cancellations` → `major`
Determined in `_build_problem_summary()` in `__init__.py`

### Direction IDs
`direction_id=1` = citybound (towards Flinders St / City Loop)
`direction_id=0` = outbound (away from city)

## Config structure

Stored in HA config entry `data`:
```python
{
  "api_key": "...",
  "lines": ["Hurstbridge"],
  "scan_interval": 60,
  "stations": [
    {
      "name": "Ivanhoe",
      "directions": "both",   # "both" | "citybound" | "outbound"
      "count": 4              # 1-4
    }
  ]
}
```

## Diagnostic tools

`tools/diagnose_gtfs.py` — inspects the GTFS static zip structure and stop IDs
`tools/diagnose_realtime.py` — inspects live trip_update feed entries
`tools/diagnose_static_times.py` — shows today's full timetable for a station

Run with: `python3 tools/diagnose_realtime.py` (prompts for API key)

## Known quirks

- PTV wraps GTFS data in a nested zip: `gtfs.zip` → `2/google_transit.zip` → CSV files
- The `DELETED` schedule_relationship (value=5) is not defined in older versions of gtfs-realtime-bindings — always use integer comparisons: `if sched_rel in (3, 5)`
- Both Ivanhoe platforms use the same stop name in stops.txt; platform 13754 = citybound, 13755 = outbound
- The realtime feed only contains trips currently in progress — no advance entries. Static timetable fallback is essential for showing upcoming trains.
- Australia DST: AEST +10 (Apr–Oct), AEDT +11 (Oct–Apr). Always use `datetime.now().astimezone()` — never hardcode offsets.

## Development workflow

```bash
# Install dependencies
pip install gtfs-realtime-bindings requests

# Syntax check all integration files
for f in custom_components/ptv/*.py; do python3 -m py_compile $f && echo "OK $f"; done

# Run diagnostics against live API
python3 tools/diagnose_realtime.py

# Deploy to HA (adjust path to your HA config directory)
cp -r custom_components/ptv/ /path/to/ha/config/custom_components/
# Then restart HA
```

## Entity naming pattern

`{domain}_{line_slug}_{station_slug}_{direction}_{index}`

Examples:
- `sensor.ptv_hurstbridge_ivanhoe_citybound_1`
- `binary_sensor.ptv_hurstbridge_any_problem`
- `sensor.ptv_hurstbridge_line_problem_level`
