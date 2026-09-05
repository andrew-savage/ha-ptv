<p align="center">
  <img src="custom_components/ptv/home_assistant_ptv_logo.png" alt="PTV Home Assistant Integration" width="600">
</p>

> **AI Disclosure:** This project was written entirely by AI. It has been tested on multiple HA installs. 


# PTV – Home Assistant Integration

A Home Assistant custom integration for **Public Transport Victoria (PTV)** metro trains and trams. Provides real-time delays, cancellations, service alerts, vehicle positions, and scheduled departure times for any Melbourne Metro station or tram stop.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

---

## Features

- **Trains and trams** — all 16 Metro lines and all 24 tram routes, each added as its own entry
- **Real-time trip updates** — delays, cancellations, schedule relationships
- **Service alerts** — published PTV disruption messages (signal faults, planned works, etc.)
- **Vehicle positions** — count of services currently running, plus worst occupancy level (trains only)
- **Next N departures** per station per direction — realtime where available, static timetable fallback otherwise
- **Express services flagged** — departures running express are marked and labelled in the displayed time
- **Overnight works handled sensibly** — planned nightly works are listed with their actual hours instead of reporting the line as disrupted all day
- **Adaptive polling** — polls fast during commute peaks and slowly overnight and at weekends, cutting API usage by around 64%
- **Problem level sensor** — `none` / `delays` / `alert` / `cancellations` / `major` for easy automation branching
- **Problem summary sensor** — TTS-ready plain English sentence
- **Multiple stations** — monitor any number of stations, each with configurable directions and departure count
- **Static timetable fallback** — always shows a departure time even when the realtime feed has nothing upcoming

## Sensors created per line or route

`<line>` is the line name for trains (`hurstbridge`) or the route for trams (`route_58`).

| Entity | Description |
|--------|-------------|
| `sensor.ptv_<line>_problem_level` | `none` / `delays` / `alert` / `cancellations` / `major` |
| `sensor.ptv_<line>_problem_summary` | Plain English status, suitable for TTS |
| `sensor.ptv_<line>_alert` | Current PTV alert message |
| `sensor.ptv_<line>_active_alerts` | Count of active alerts |
| `sensor.ptv_<line>_max_delay` | Worst current delay in minutes |
| `sensor.ptv_<line>_active_trains` | Services currently running. Displayed as "Active Trams" on tram entries, though the entity ID keeps `active_trains` |
| `sensor.ptv_<line>_occupancy` | Worst occupancy (Empty → Full). **Trains only** — trams don't report occupancy, so this sensor isn't created |
| `sensor.ptv_<line>_status` | Combined status text |
| `binary_sensor.ptv_<line>_any_problem` | ON when anything is wrong |
| `binary_sensor.ptv_<line>_delays` | ON when delays ≥ 5 min |
| `binary_sensor.ptv_<line>_cancellations` | ON when services cancelled |
| `binary_sensor.ptv_<line>_service_alert` | ON when alert published |

## Sensors created per station or stop

For each configured station or stop (e.g. Ivanhoe, citybound, count=4):

| Entity | Description |
|--------|-------------|
| `sensor.ptv_<line>_<stop>_citybound_1` | Next citybound departure (timestamp) |
| `sensor.ptv_<line>_<stop>_citybound_2` | 2nd citybound departure |
| `sensor.ptv_<line>_<stop>_citybound_3` | 3rd citybound departure |
| `sensor.ptv_<line>_<stop>_citybound_4` | 4th citybound departure |
| `sensor.ptv_<line>_<stop>_outbound_1` | Next outbound departure |
| ... | ... |

> **Tram direction naming.** Entity IDs keep `citybound` and `outbound` for both modes, but tram *display names* use the destination. So `sensor.ptv_route_58_..._citybound_1` is named "PTV Route 58 … To Toorak 1". Use the entity ID in automations; the friendly name is what you'll see on a dashboard.

Each sensor has attributes: `formatted`, `delay_minutes`, `on_time`, `is_realtime`, `is_express`, `source`.

`is_express` is `true` when PTV's realtime feed reports the service as running express. The `formatted` string is tagged to match, e.g. `9:39 AM (on time) · Express`. Expresses that skip your station never appear as departures there in the first place. Note that departures coming from the static timetable fallback always report `is_express: false` — the scheduled data carries no express marker, so it is only known once a service appears in the realtime feed.

The alert sensors (`sensor.ptv_<line>_alert` and `_active_alerts`) additionally expose `live_alert_count`, plus `alert_<n>_is_overnight_works`, `alert_<n>_window_from`, `alert_<n>_window_to` and `alert_<n>_active_now` — see [Overnight and planned works](#overnight-and-planned-works).

## Prerequisites

1. A free account at [opendata.transport.vic.gov.au](https://opendata.transport.vic.gov.au)
2. Your API key (found in your portal profile after registering)

> ⚠️ Keep your API key secret. Never commit it to version control.

## Installation

### HACS (recommended)

1. In Home Assistant go to **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/andrew-savage/ha-ptv`, category **Integration**
3. Click **Download**, restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration → PTV**

### Manual

Copy `custom_components/ptv/` into your HA `config/custom_components/` directory, restart.

## Setup

The config flow has four steps:

1. **API Key and mode** — paste your Transport Victoria portal key, and choose Metro Train or Tram. Add the integration a second time to monitor the other mode (see [Trams](#trams))
2. **Lines / Routes** — select which lines or tram routes to monitor (multi-select), and set the peak-hour polling interval (default 60 seconds — see [Polling and API usage](#polling-and-api-usage))
3. **Service Alert Filters** — choose which alert types trigger your sensors. The recommended defaults include meaningful disruptions (no service, reduced service, significant delays, detour, modified service) and exclude noise like car park closures and informational notices. You can adjust this to taste:
   - **Include:** No service, Reduced service, Significant delays, Detour, Modified service
   - **Exclude by default:** Other effect (car parks, lift outages), No effect (informational only), Unknown effect, Stop moved, Accessibility issue, Additional service
4. **Stations / Stops** — add them one at a time. For each, choose:
   - Name (searchable dropdown; tram stops include their stop number, e.g. `#115`)
   - Directions: Both, or one only — labelled Citybound/Outbound for trains and by destination for trams
   - Count: 1–4 upcoming services to track
   - Select "(Done — no more stations)" when finished

> If the station dropdown appears empty, the schedule download (~280 MB) did not complete. You can type any station name in manually and it will still resolve, or press Back and retry the step.

## Trams

Trams are set up exactly like trains: add the integration again and choose **Tram** on the first step. Each mode gets its own entry, its own polling settings and its own schedule cache, so train and tram config never interfere.

Two differences follow from how the data is published:

- **Directions are destinations, not citybound/outbound.** A tram stop's sensors read *To Toorak* and *To West Coburg* rather than Citybound/Outbound, taken from the route's GTFS headsigns.
- **A stop can be served by several routes.** `Casino/Southbank/Queens Bridge St #115` is on route 58, while a nearby stop with a similar name is on 96 and 109 — so pick the routes first, then the stop. Stops are listed with their tram stop number (`#115`), which is the number shown on the physical stop. Note this is *not* the TramTracker ID.

Entity naming follows the route, e.g. `sensor.ptv_route_58_casino_southbank_queens_bridge_st_115_citybound_1`.

### Tram limitations

These are properties of PTV's feed, not the integration:

| | Behaviour |
|---|---|
| Realtime departures | Trams publish only their next ~2 stops, so a given stop usually has realtime data only when a tram is close. Everything further out comes from the timetable (`is_realtime: false`) |
| Occupancy | Not reported for trams, so the occupancy sensor is not created |
| Service alerts | The tram alerts feed exists but was empty when tested; alert sensors are created and will populate if PTV publishes any |

## Options

After setup, use **Settings → Devices & Services → PTV → Configure** to change polling rates without re-adding the integration. Changes apply immediately — no restart needed.

| Option | Default |
|--------|---------|
| Peak interval | 60 seconds |
| Off-peak interval | 5 minutes |
| Overnight interval | 15 minutes |
| Morning peak | 06:00 – 10:00 |
| Evening peak | 16:00 – 20:00 |
| Overnight | 00:00 – 06:00 |
| Use peak rates on weekends too | Off |

## Automation Blueprint

A ready-to-use blueprint is included at `blueprints/ptv_commute_notifications.yaml`. Import it into Home Assistant and configure:

| Input | Description |
|-------|-------------|
| Line problem level sensor | `sensor.ptv_<line>_line_problem_level` |
| Line problem summary sensor | `sensor.ptv_<line>_line_problem_summary` |
| Next trains 1–4 | `sensor.ptv_<line>_<station>_citybound_1` through `_4` |
| Station name | Displayed in the notification title |
| Notify service | e.g. `notify.mobile_app_your_phone` |
| Morning status time | When to send the line status (e.g. 07:30) |
| Departure reminder time | When to send the next-trains list (e.g. 07:50) |
| Active days | Which days of the week to fire |
| Mention tonight's planned works | Show overnight works under a "🌙 Tonight" line (default on) |
| Service type | Train, Tram or Bus — sets the wording and icons (default Train) |
| Also trigger on these entities | Optional helpers/entities that also send a notification |
| What those entities send | Departure reminder, line status, or both |

The blueprint sends two notifications daily:

- **Morning status** — fires at your chosen time with an emoji-coded title (✅ on time / ⏱️ delays / ⚠️ alert / 🚫 cancelled / 🚨 major) and a plain-English summary sentence
- **Departure reminder** — fires closer to when you leave, listing the next 4 trains with times and delay info. If there's an active disruption the summary is prepended so you always have context. Includes a tap action to open the PTV website.

### Triggering it from something other than a time

Set **Also trigger on these entities** to any helper or entity, and pick what it sends under **What those entities send** (departure reminder, line status, or both). It fires when the entity turns on or is pressed, so leaving an `input_boolean` switched on won't re-trigger it.

Typical uses: an `input_button` on a dashboard, or an `input_boolean` your alarm automation switches on so the commute lands with your alarm rather than at a fixed time.

```yaml
# Example helper — Settings → Devices & Services → Helpers
input_button:
  ptv_check_now:
    name: Check my commute
```

The two scheduled times keep working alongside this; leave them as they are, or set the days filter to no days if you only want entity triggers.

### Testing it without waiting

Open the automation and choose **Run actions** from the ⋮ menu. Both notifications are sent immediately using live sensor data, and the active-days filter is ignored — so you can check it works on a Sunday afternoon.

For a button on a dashboard, call the same service:

```yaml
type: button
name: Test PTV notification
tap_action:
  action: call-service
  service: automation.trigger
  target:
    entity_id: automation.your_ptv_automation
```

At their scheduled times each notification still fires on its own, exactly as before.

### Wording for trams

Set **Service type** to Tram and the notifications follow suit — "🚊 Next trams from Casino/Southbank", "Your Route is On Time", "Trams Cancelled". Bus wording is available too, for when bus support lands. It defaults to Train, so existing automations read exactly as before.

Both notifications describe only what is happening now. Works scheduled for later tonight are listed separately, so a morning notification reads:

```
✅ Your Line is On Time
Werribee line is running normally.

🌙 Tonight: 11:50pm–last service: Buses replace trains from Newport to Werribee
```

Once those works begin, they become an ordinary alert and the title changes accordingly. Set **Mention tonight's planned works** to off to leave them out entirely.

## Example automation

```yaml
automation:
  - alias: "Morning commute briefing"
    trigger:
      - platform: time
        at: "07:45:00"
    condition:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: >
            {% set level = states('sensor.ptv_hurstbridge_line_problem_level') %}
            {% if level == 'none' %}✅ Hurstbridge On Time
            {% elif level == 'delays' %}⏱️ Hurstbridge Delays
            {% elif level == 'alert' %}⚠️ Hurstbridge Alert
            {% else %}🚨 Hurstbridge Disruption{% endif %}
          message: >
            {{ states('sensor.ptv_hurstbridge_line_problem_summary') }}
            Next trains from Ivanhoe:
            1. {{ state_attr('sensor.ptv_hurstbridge_ivanhoe_citybound_1', 'formatted') }}
            2. {{ state_attr('sensor.ptv_hurstbridge_ivanhoe_citybound_2', 'formatted') }}
```

See `example_automations.yaml` for more ready-to-use automations.

## Dashboard card

See `dashboard_card.yaml` for a Lovelace markdown card showing next trains with colour-coded status indicators.

## Polling and API usage

The Transport Victoria API allows 24 calls per 60 seconds per feed. The integration reads three feeds (trip updates, service alerts, vehicle positions), so each poll costs 3 API calls. Even the fastest tier is well within the limit.

Departure times only need to be fresh when you are actually travelling, so polling rate follows the commute:

| When | Interval |
|------|----------|
| Weekday peaks — 06:00–10:00 and 16:00–20:00 | 60 seconds |
| Other daytime hours | 5 minutes |
| Overnight — 00:00–06:00 | 15 minutes |
| Weekends | Off-peak and overnight rates only, no peak windows |

That comes to 624 polls on a weekday and 240 at the weekend, against 1,440 per day at a flat 60 seconds — about **64% fewer API calls per week**. The interval is re-evaluated after every poll, so the integration moves between tiers on its own without a restart.

All intervals and window times are editable under [Options](#options). To restore the old flat-rate behaviour, set all three intervals to the same value.

## Overnight and planned works

PTV publishes overnight works as a single window spanning the whole campaign. "Buses replace trains from Newport to Werribee, 11.50pm to last service each night, Monday to Thursday" arrives as one block running from Monday 23:50 straight through to Thursday 23:59. Taken literally, that marks the line as disrupted around the clock, so a 7:50am commute notification would warn about works that finished hours earlier.

The integration detects these and treats them as what they are:

- They stay listed, prefixed with their real hours — `Tonight 11:50pm–last service: Buses replace trains from Newport to Werribee`
- They do **not** raise `problem_level` outside those hours, so the line reads as running normally in the morning
- Once the window opens, they behave as ordinary active alerts with no prefix

Nothing is hidden, so a trip planned for late tonight is still visible in the morning. Genuinely continuous disruptions — a months-long car park closure, for instance — are unaffected and always count as active.

Use `live_alert_count` for "is something wrong right now", and `alert_count` for "is anything published at all".

## Notes

- The trip updates feed does **not** include rail replacement buses, platform changes, or route deviations
- The static timetable cache refreshes daily at midnight and every 3 days regardless
- Express services are identified from the realtime feed's route ID; the static timetable has no equivalent marker
- A few PTV stop records are named after their platform number rather than their station. These are filtered out, so the station list matches PTV's own count of 226 stations

## License

[MIT](LICENSE)
