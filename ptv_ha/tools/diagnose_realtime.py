#!/usr/bin/env python3
"""
PTV Realtime Feed Diagnostic
Inspects actual trip_update entries for the Hurstbridge line
to understand stop_id vs stop_sequence usage and departure time formats.
"""
import sys
from datetime import datetime, timezone

try:
    import requests
    from google.transit import gtfs_realtime_pb2
except ImportError:
    print("pip install requests gtfs-realtime-bindings")
    sys.exit(1)

API_KEY = input("Enter your PTV API key: ").strip()
IVANHOE_STOP_IDS = {"13754", "13755"}
HBE_ROUTE = "aus:vic:vic-02-HBE:"

URL = ("https://api.opendata.transport.vic.gov.au"
       "/opendata/public-transport/gtfs/realtime/v1/metro/trip-updates")

r = requests.get(URL, headers={"KeyID": API_KEY}, timeout=30)
r.raise_for_status()
feed = gtfs_realtime_pb2.FeedMessage()
feed.ParseFromString(r.content)

now = datetime.now(timezone.utc)
print(f"\nFeed timestamp: {datetime.fromtimestamp(feed.header.timestamp, tz=timezone.utc)}")
print(f"Current time:  {now}")
print(f"Total entities: {len(feed.entity)}\n")

hbe_trips = [e for e in feed.entity
             if e.HasField("trip_update")
             and e.trip_update.trip.route_id == HBE_ROUTE]

print(f"Hurstbridge trips in feed: {len(hbe_trips)}")

# Show all HBE trips - their stop_time_updates and whether they include stop_id
print("\n--- All Hurstbridge trip_updates ---")
for entity in hbe_trips:
    tu = entity.trip_update
    dir_id = tu.trip.direction_id
    trip_id = tu.trip.trip_id
    sched = tu.trip.schedule_relationship
    stus = tu.stop_time_update

    has_stop_ids = any(s.stop_id for s in stus)
    has_future = any(
        (s.departure.time if s.HasField("departure") else
         s.arrival.time if s.HasField("arrival") else 0) > now.timestamp()
        for s in stus
    )
    ivanhoe_in_trip = any(s.stop_id in IVANHOE_STOP_IDS for s in stus)

    print(f"\n  trip_id={trip_id}")
    print(f"  direction_id={dir_id}  sched_rel={sched}  stop_updates={len(stus)}")
    print(f"  has_stop_ids={has_stop_ids}  has_future_times={has_future}  ivanhoe_present={ivanhoe_in_trip}")

    # Show the stop_time_updates
    for i, stu in enumerate(stus):
        dep_ts = stu.departure.time if stu.HasField("departure") else None
        arr_ts = stu.arrival.time if stu.HasField("arrival") else None
        ts = dep_ts or arr_ts or 0
        dep_dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S") if ts else "none"
        future = "FUTURE" if ts > now.timestamp() else "past"
        print(f"    [{i}] stop_id={stu.stop_id!r:8s} seq={stu.stop_sequence:4d}  "
              f"dep={dep_dt} ({future})  delay={stu.departure.delay if stu.HasField('departure') else 'n/a'}")
        if i >= 6:
            print(f"    ... ({len(stus)-7} more stops)")
            break

# Summary: which stop_ids appear in future departures
print("\n\n--- Future departure stop_ids across ALL Hurstbridge trips ---")
future_stop_ids = {}
for entity in hbe_trips:
    tu = entity.trip_update
    for stu in tu.stop_time_update:
        dep_ts = (stu.departure.time if stu.HasField("departure") else
                  stu.arrival.time if stu.HasField("arrival") else 0)
        if dep_ts > now.timestamp() and stu.stop_id:
            future_stop_ids[stu.stop_id] = future_stop_ids.get(stu.stop_id, 0) + 1

print(f"Unique stop_ids with future departures: {len(future_stop_ids)}")
print("Top 20:", sorted(future_stop_ids.items(), key=lambda x: -x[1])[:20])
print("\nIvanhoe stop_ids in future departures:")
for sid in IVANHOE_STOP_IDS:
    print(f"  {sid}: {future_stop_ids.get(sid, 0)} trips")
