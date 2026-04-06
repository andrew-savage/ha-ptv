#!/usr/bin/env python3
"""
PTV GTFS Diagnostic Script - v3
Scans ALL inner zips to find the metro train one specifically.
"""
import csv, io, json, sys, zipfile

try:
    import requests
except ImportError:
    print("ERROR: pip install requests"); sys.exit(1)

API_KEY = input("Enter your PTV API key (KeyID): ").strip()
STATION_NAME = "ivanhoe"

GTFS_STATIC_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
    "fb152201-859f-4882-9206-b768060b50ad/download/gtfs.zip"
)
REALTIME_URL = (
    "https://api.opendata.transport.vic.gov.au"
    "/opendata/public-transport/gtfs/realtime/v1/metro/trip-updates"
)

# ── Step 1: Realtime feed ──────────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 1: Realtime feed stop_ids")
print("="*60)
stop_ids_in_feed = set()
try:
    from google.transit import gtfs_realtime_pb2
    r = requests.get(REALTIME_URL, headers={"KeyID": API_KEY}, timeout=30)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    for entity in feed.entity:
        if entity.HasField("trip_update"):
            for stu in entity.trip_update.stop_time_update:
                stop_ids_in_feed.add(stu.stop_id)
    print(f"Total stop_ids in realtime feed: {len(stop_ids_in_feed)}")
    print("Sample:", sorted(stop_ids_in_feed)[:15])
except Exception as e:
    print(f"WARNING: {e}")

# ── Step 2: Download ───────────────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 2: Downloading GTFS static zip...")
print("="*60)
r2 = requests.get(GTFS_STATIC_URL, timeout=180)
r2.raise_for_status()
print(f"Downloaded {len(r2.content)//1024} KB")
outer = zipfile.ZipFile(io.BytesIO(r2.content))

# ── Step 3: Scan ALL inner zips ────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 3: Scanning ALL inner zips for metro train routes")
print("="*60)

# Metro-specific route codes from live feed
METRO_CODES = {"ALM", "BEG", "CBE", "CGB", "FKN", "GWY", "HBE",
               "LIL", "MDD", "PKM", "SHM", "STY", "SUY", "UFD", "WER", "WIL"}

metro_z = None
metro_entry = None

for entry in sorted(outer.namelist()):
    if not entry.endswith("google_transit.zip"):
        continue
    try:
        inner_bytes = outer.read(entry)
        inner_z = zipfile.ZipFile(io.BytesIO(inner_bytes))
        if "routes.txt" not in inner_z.namelist():
            print(f"  {entry}: no routes.txt, skipping")
            continue
        with inner_z.open("routes.txt") as f:
            content = f.read().decode("utf-8-sig")
        # Check for any metro-specific route code
        found_codes = [c for c in METRO_CODES if f"-{c}:" in content]
        print(f"  {entry}: metro codes found = {found_codes}")
        if found_codes and metro_z is None:
            metro_z = inner_z
            metro_entry = entry
            print(f"    *** Selected as metro zip ***")
    except Exception as e:
        print(f"  {entry}: ERROR {e}")

if not metro_z:
    print("\nERROR: Could not find metro inner zip")
    sys.exit(1)

print(f"\nUsing: {metro_entry}")
print(f"Files: {metro_z.namelist()}")

# ── Step 4: Routes in metro zip ────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 4: Routes in metro zip")
print("="*60)
with metro_z.open("routes.txt") as f:
    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
    for row in reader:
        rid = row.get("route_id","")
        if any(f"-{c}:" in rid for c in METRO_CODES):
            print(f"  {rid!r:45s}  {row.get('route_long_name') or row.get('route_short_name')!r}")

# ── Step 5: Find Ivanhoe ───────────────────────────────────────────────────
print("\n" + "="*60)
print(f"STEP 5: Stops matching '{STATION_NAME}'")
print("="*60)
matched = []
with metro_z.open("stops.txt") as f:
    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
    for row in reader:
        if STATION_NAME in row.get("stop_name","").lower():
            matched.append(dict(row))

if not matched:
    print("  No matches found. Printing first 20 stop names for reference:")
    with metro_z.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for i, row in enumerate(reader):
            if i >= 20: break
            print(f"    stop_id={row.get('stop_id')!r:15s}  name={row.get('stop_name')!r}")
else:
    for s in matched:
        in_feed = "✓ IN REALTIME FEED" if s.get("stop_id") in stop_ids_in_feed else "✗ not in feed"
        print(f"  stop_id={s.get('stop_id')!r:15s}  name={s.get('stop_name')!r:50s}  {in_feed}")

# ── Step 6: Sample stop_times ──────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 6: Sample stop_times for Ivanhoe stop_ids")
print("="*60)
ivanhoe_ids = {s["stop_id"] for s in matched}
found_rows = {}
if ivanhoe_ids:
    with metro_z.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            sid = row.get("stop_id","")
            if sid in ivanhoe_ids and sid not in found_rows:
                found_rows[sid] = row
            if len(found_rows) == len(ivanhoe_ids):
                break
    for sid, row in found_rows.items():
        print(f"  stop_id={sid!r}  trip_id={row.get('trip_id')!r}  arr={row.get('arrival_time')}  dep={row.get('departure_time')}")
else:
    # Cross-reference: which stop_ids from feed appear in stops.txt?
    print("  No Ivanhoe stops found. Cross-referencing realtime stop_ids with stops.txt...")
    feed_stops_in_static = {}
    with metro_z.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row.get("stop_id") in stop_ids_in_feed:
                feed_stops_in_static[row["stop_id"]] = row.get("stop_name","")
    print(f"  {len(feed_stops_in_static)} realtime stop_ids found in static stops.txt")
    print("  Sample matches:")
    for sid, name in list(feed_stops_in_static.items())[:20]:
        print(f"    stop_id={sid!r:10s}  name={name!r}")

# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("SUMMARY")
print("="*60)
print(json.dumps({
    "metro_zip": metro_entry,
    "ivanhoe_stop_ids": [s["stop_id"] for s in matched],
    "ivanhoe_stop_names": [s["stop_name"] for s in matched],
    "in_realtime_feed": [s["stop_id"] for s in matched if s["stop_id"] in stop_ids_in_feed],
}, indent=2))
