#!/usr/bin/env python3
"""
Inspects the static timetable to find scheduled departure times
at Ivanhoe (stop_ids 13754, 13755) for today, and checks direction.
"""
import csv, io, sys, zipfile
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("pip install requests"); sys.exit(1)

GTFS_STATIC_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
    "fb152201-859f-4882-9206-b768060b50ad/download/gtfs.zip"
)
IVANHOE_IDS = {"13754", "13755"}
HBE_ROUTE = "aus:vic:vic-02-HBE:"

print("Downloading GTFS static zip...")
r = requests.get(GTFS_STATIC_URL, timeout=180)
r.raise_for_status()
outer = zipfile.ZipFile(io.BytesIO(r.content))
inner = zipfile.ZipFile(io.BytesIO(outer.read("2/google_transit.zip")))

# Get today's service_ids from calendar/calendar_dates
today = datetime.now(timezone(timedelta(hours=11))).strftime("%Y%m%d")
dow = datetime.now(timezone(timedelta(hours=11))).strftime("%A").lower()
print(f"Today: {today} ({dow})")

active_service_ids = set()
with inner.open("calendar.txt") as f:
    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
        if (row.get(dow) == "1"
                and row.get("start_date","") <= today <= row.get("end_date","")):
            active_service_ids.add(row["service_id"])

with inner.open("calendar_dates.txt") as f:
    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
        if row.get("date") == today:
            if row.get("exception_type") == "1":
                active_service_ids.add(row["service_id"])
            elif row.get("exception_type") == "2":
                active_service_ids.discard(row["service_id"])

print(f"Active service_ids today: {len(active_service_ids)}")

# Get HBE trip_ids for today with their direction_id
hbe_trips = {}  # trip_id -> direction_id
with inner.open("trips.txt") as f:
    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
        if row.get("route_id") == HBE_ROUTE and row.get("service_id") in active_service_ids:
            hbe_trips[row["trip_id"]] = int(row.get("direction_id", 0))

print(f"HBE trips running today: {len(hbe_trips)}")

# Find Ivanhoe departure times from stop_times
ivanhoe_deps = []
with inner.open("stop_times.txt") as f:
    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
        if row.get("stop_id") in IVANHOE_IDS and row.get("trip_id") in hbe_trips:
            dep = row.get("departure_time","")
            ivanhoe_deps.append({
                "trip_id": row["trip_id"],
                "stop_id": row["stop_id"],
                "stop_seq": row.get("stop_sequence"),
                "dep": dep,
                "direction_id": hbe_trips[row["trip_id"]],
            })

# Sort by departure time (handle >24h times for after-midnight services)
def norm_time(t):
    parts = t.split(":")
    return int(parts[0])*60 + int(parts[1]) if len(parts) >= 2 else 9999

ivanhoe_deps.sort(key=lambda x: norm_time(x["dep"]))

print(f"\nIvanhoe departures today ({len(ivanhoe_deps)} total):")
print(f"{'time':8s}  {'dir':4s}  {'stop_id':8s}  trip_id")
for d in ivanhoe_deps:
    print(f"  {d['dep']:8s}  dir={d['direction_id']}  stop={d['stop_id']:6s}  {d['trip_id']}")
