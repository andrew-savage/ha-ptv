#!/usr/bin/env python3
"""
PTV Tram Realtime Diagnostic

Answers the questions that decide how tram support can work:

  1. Do tram trip_updates carry stop_id, so per-stop departures are possible?
  2. Are there future departure times, or only delays against a schedule?
  3. What do tram route IDs look like in realtime (any variants like metro's ":E")?
  4. Do vehicle positions / occupancy / alerts exist for trams?

Usage: python3 diagnose_tram.py [gtfs_stop_id]
       e.g. python3 diagnose_tram.py 18215      (Casino/Southbank #115)
"""
import sys
from collections import Counter
from datetime import datetime, timezone

try:
    import requests
    from google.transit import gtfs_realtime_pb2
except ImportError:
    print("pip install requests gtfs-realtime-bindings")
    sys.exit(1)

STOP_ID = sys.argv[1] if len(sys.argv) > 1 else "18215"
API_KEY = input("Enter your PTV API key: ").strip()

BASE = ("https://api.opendata.transport.vic.gov.au"
        "/opendata/public-transport/gtfs/realtime/v1/tram")


def fetch(path):
    r = requests.get(f"{BASE}/{path}", headers={"KeyID": API_KEY}, timeout=30)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed


now = datetime.now(timezone.utc)
print(f"\nLocal now: {now.astimezone()}")
print(f"Looking for GTFS stop_id {STOP_ID!r}\n")

# ---------------------------------------------------------------- trip updates
trips = fetch("trip-updates")
tus = [e.trip_update for e in trips.entity if e.HasField("trip_update")]
print("=" * 68)
print(f"TRIP UPDATES — {len(tus)} trips")

routes = Counter(tu.trip.route_id for tu in tus)
print(f"  distinct route IDs: {len(routes)}")
for rid, n in routes.most_common(8):
    print(f"    {rid!r}: {n}")
variants = [r for r in routes if not r.endswith(":")]
print(f"  route IDs not ending in ':' (variants): {variants or 'none'}")

total_stus = sum(len(tu.stop_time_update) for tu in tus)
with_stop_id = sum(1 for tu in tus for s in tu.stop_time_update if s.stop_id)
with_time = sum(1 for tu in tus for s in tu.stop_time_update
                if (s.HasField("departure") and s.departure.time)
                or (s.HasField("arrival") and s.arrival.time))
print(f"\n  stop_time_updates: {total_stus}")
print(f"    carrying stop_id:        {with_stop_id}")
print(f"    carrying absolute times: {with_time}")

if total_stus == 0:
    print("\n  >> No stop_time_updates at all: per-stop realtime departures are")
    print("     NOT possible; only the static timetable could be used.")
elif with_stop_id == 0:
    print("\n  >> stop_time_updates carry NO stop_id (stop_sequence only).")
    print("     Per-stop departures would need trip_id -> static schedule mapping.")
else:
    print("\n  >> stop_id present: per-stop realtime departures work like metro.")

# Departures at the requested stop
hits = []
for tu in tus:
    for s in tu.stop_time_update:
        if s.stop_id == STOP_ID:
            ts = (s.departure.time if s.HasField("departure") else
                  s.arrival.time if s.HasField("arrival") else 0)
            hits.append((ts, tu.trip.route_id, tu.trip.direction_id, tu.trip.trip_id))
hits.sort()
future = [h for h in hits if h[0] > now.timestamp()]
print(f"\n  departures found at stop {STOP_ID}: {len(hits)} ({len(future)} in the future)")
for ts, rid, d, tid in future[:8]:
    when = datetime.fromtimestamp(ts).astimezone().strftime("%H:%M:%S") if ts else "no time"
    print(f"    {when}  route={rid}  dir={d}  trip={tid[:28]}")

# ----------------------------------------------------------- vehicle positions
try:
    pos = fetch("vehicle-positions")
    vps = [e.vehicle for e in pos.entity if e.HasField("vehicle")]
    occ = sum(1 for v in vps if v.occupancy_status)
    print("\n" + "=" * 68)
    print(f"VEHICLE POSITIONS — {len(vps)} vehicles, {occ} reporting occupancy")
except Exception as err:
    print(f"\nVEHICLE POSITIONS — unavailable: {err}")

# ------------------------------------------------------------------- alerts
try:
    al = fetch("service-alerts")
    alerts = [e.alert for e in al.entity if e.HasField("alert")]
    print("\n" + "=" * 68)
    print(f"SERVICE ALERTS — {len(alerts)} alerts")
    for a in alerts[:5]:
        hdr = a.header_text.translation[0].text if a.header_text.translation else "(no header)"
        rids = sorted({ie.route_id for ie in a.informed_entity if ie.route_id})
        print(f"    effect={a.effect}  routes={rids or '(line-wide)'}  {hdr[:60]}")
except Exception as err:
    print(f"\nSERVICE ALERTS — unavailable: {err}")
