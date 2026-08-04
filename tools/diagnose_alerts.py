#!/usr/bin/env python3
"""
PTV Service Alerts Diagnostic

Dumps active_period windows for each alert so you can see whether a
"nightly works" alert is published as per-night windows or as one block
spanning the whole campaign. That distinction decides whether the
integration can filter it out during the morning commute.

Usage: python3 diagnose_alerts.py [line-code]
       e.g. python3 diagnose_alerts.py WER
"""
import sys
from datetime import datetime, timezone

try:
    import requests
    from google.transit import gtfs_realtime_pb2
except ImportError:
    print("pip install requests gtfs-realtime-bindings")
    sys.exit(1)

LINE_CODE = (sys.argv[1] if len(sys.argv) > 1 else "").upper()
API_KEY = input("Enter your PTV API key: ").strip()

URL = ("https://api.opendata.transport.vic.gov.au"
       "/opendata/public-transport/gtfs/realtime/v1/metro/service-alerts")

r = requests.get(URL, headers={"KeyID": API_KEY}, timeout=30)
r.raise_for_status()
feed = gtfs_realtime_pb2.FeedMessage()
feed.ParseFromString(r.content)

now = datetime.now().astimezone()
print(f"\nFeed timestamp: {datetime.fromtimestamp(feed.header.timestamp).astimezone()}")
print(f"Local now:      {now}")
print(f"Total entities: {len(feed.entity)}")
if LINE_CODE:
    print(f"Filtering to route IDs containing: {LINE_CODE}")

EFFECTS = {
    1: "No service", 2: "Reduced service", 3: "Significant delays",
    4: "Detour", 5: "Additional service", 6: "Modified service",
    7: "Other effect", 8: "Unknown effect", 9: "Stop moved",
    10: "No effect", 11: "Accessibility issue",
}


def text_of(t):
    if not t.translation:
        return ""
    for tr in t.translation:
        if tr.language in ("en", "en-AU", ""):
            return tr.text
    return t.translation[0].text


def fmt_ts(ts):
    if not ts:
        return "unset"
    return datetime.fromtimestamp(ts).astimezone().strftime("%a %d %b %H:%M")


shown = 0
for entity in feed.entity:
    if not entity.HasField("alert"):
        continue
    alert = entity.alert

    routes = [ie.route_id for ie in alert.informed_entity if ie.route_id]
    if LINE_CODE and not any(LINE_CODE in rid.upper() for rid in routes):
        continue

    shown += 1
    header = text_of(alert.header_text)
    print(f"\n{'='*72}\n[{entity.id}] {header}")
    desc = text_of(alert.description_text)
    if desc and desc != header:
        print(f"  description: {desc[:300]}")
    print(f"  effect: {alert.effect} ({EFFECTS.get(alert.effect,'?')})")
    print(f"  informed routes: {sorted(set(routes)) or '(none — line-wide)'}")

    periods = alert.active_period
    print(f"  active_period entries: {len(periods)}")
    if not periods:
        print("    (none — integration treats this as always active)")
    for p in periods:
        start, end = p.start or 0, p.end or 0
        now_ts = now.timestamp()
        live = start <= now_ts <= (end or float("inf"))
        span_h = ((end - start) / 3600) if (start and end) else None
        span = f"{span_h:.1f}h" if span_h is not None else "open-ended"
        print(f"    {fmt_ts(start)}  ->  {fmt_ts(end)}   ({span}) "
              f"{'<== ACTIVE NOW' if live else ''}")

    # The verdict that matters
    if periods:
        long_windows = [p for p in periods
                        if p.start and p.end and (p.end - p.start) > 16 * 3600]
        if long_windows:
            print("  >> Windows exceed 16h: nightly-only works cannot be "
                  "filtered by active_period alone.")
        else:
            print("  >> Windows look time-boxed: filtering by active_period "
                  "would correctly hide this outside its hours.")

print(f"\n{'='*72}\nAlerts shown: {shown}")
