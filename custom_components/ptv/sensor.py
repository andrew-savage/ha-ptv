"""Sensors for PTV."""
from __future__ import annotations
import re
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_LINES, CONF_STATIONS, CONF_STATION_NAME,
    CONF_STATION_DIRECTIONS, CONF_STATION_COUNT,
    DEFAULT_STATION_COUNT, DIR_CITYBOUND, DIR_OUTBOUND, DIR_BOTH, DOMAIN,
    MODE_TRAIN, MODE_TRAM, routes_for_mode, line_phrase,
)
from .gtfs_static import get_direction_label
from . import PTVDataCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PTVDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    lines = entry.data[CONF_LINES]
    stations_config = entry.data.get(CONF_STATIONS, [])

    entities = []
    for line in lines:
        # Line-level sensors
        entities += [
            PTVMaxDelaySensor(coordinator, line),
            PTVStatusSensor(coordinator, line),
            PTVAlertCountSensor(coordinator, line),
            PTVAlertTextSensor(coordinator, line),
            PTVTrainCountSensor(coordinator, line),
            PTVProblemLevelSensor(coordinator, line),
            PTVProblemSummarySensor(coordinator, line),
        ]
        # Trams never populate occupancy_status, so the sensor would sit on
        # "Unknown" forever — omit it rather than ship a dead entity.
        if not _is_tram(coordinator):
            entities.append(PTVOccupancySensor(coordinator, line))
        # Per-station sensors
        for scfg in stations_config:
            name = scfg[CONF_STATION_NAME]
            directions = scfg.get(CONF_STATION_DIRECTIONS, DIR_BOTH)
            count = scfg.get(CONF_STATION_COUNT, DEFAULT_STATION_COUNT)

            if directions in (DIR_CITYBOUND, DIR_BOTH):
                for i in range(1, count + 1):
                    entities.append(PTVUpcomingTrainSensor(coordinator, line, name, "citybound", i))
            if directions in (DIR_OUTBOUND, DIR_BOTH):
                for i in range(1, count + 1):
                    entities.append(PTVUpcomingTrainSensor(coordinator, line, name, "outbound", i))

    async_add_entities(entities)


# ── Helpers ──────────────────────────────────────────────────────────────

def _ld(coordinator, line):
    return coordinator.data.get(line, {}) if coordinator.data else {}

def _sd(coordinator, line, station_key):
    return _ld(coordinator, line).get("stations", {}).get(station_key, {})

def _device(line, tram: bool = False):
    return {
        "identifiers": {(DOMAIN, f"ptv_{line.lower().replace(' ','_')}")},
        "name": f"PTV {line}" if tram else f"PTV {line} Line",
        "manufacturer": "Transport Victoria",
        "model": "GTFS Realtime",
    }

def _is_tram(coordinator) -> bool:
    return getattr(coordinator, "mode", MODE_TRAIN) == MODE_TRAM


def _slug(name: str, tram: bool) -> str:
    """Identifier fragment for a stop name.

    Train station IDs keep the original scheme so existing entities survive
    (only Jolimont-MCG would differ). Tram stop names carry "/" and "#"
    (e.g. "Clyde St/Raleigh Rd #42"), which need stripping.
    """
    if not tram:
        return name.lower().replace(" ", "_")
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


_TRAM_ICONS = {
    "mdi:train": "mdi:tram",
    "mdi:train-variant": "mdi:tram-side",
    "mdi:train-car": "mdi:tram",
    "mdi:train-car-flatbed": "mdi:tram-side",
    "mdi:train-alert": "mdi:alert-circle",
    "mdi:train-car-remove": "mdi:close-octagon",
}


def _ic(coordinator, icon: str) -> str:
    """Swap rail icons for tram equivalents on tram entries."""
    return _TRAM_ICONS.get(icon, icon) if _is_tram(coordinator) else icon


def _line_label(coordinator, line) -> str:
    """Entity-name prefix, with trailing space.

    Trains read "Hurstbridge Line Max Delay"; tram route names already carry
    the word Route, so "Route 58 Max Delay".
    """
    return f"{line} " if _is_tram(coordinator) else f"{line} Line "


def _direction_label(coordinator, line, direction: str) -> str:
    """"Citybound"/"Outbound" for trains, the destination for trams."""
    if not _is_tram(coordinator):
        return "Citybound" if direction == DIR_CITYBOUND else "Outbound"
    route_id = routes_for_mode(coordinator.mode).get(line, "")
    label = get_direction_label(
        getattr(coordinator, "gtfs_cache", {}) or {},
        route_id,
        1 if direction == DIR_CITYBOUND else 0,
    )
    return f"To {label}" if label else (
        "Direction 1" if direction == DIR_CITYBOUND else "Direction 0"
    )


def _fmt(iso: str | None, delay_sec: int, is_realtime: bool, is_express: bool = False) -> str:
    if not iso:
        return "Unknown"
    try:
        dep = datetime.fromisoformat(iso).astimezone()
        time_str = dep.strftime("%-I:%M %p")
        suffix = " · Express" if is_express else ""
        if delay_sec >= 60:
            return f"{time_str} ({round(delay_sec/60)} min late){suffix}"
        return f"{time_str} ({'on time' if is_realtime else 'scheduled'}){suffix}"
    except Exception:
        return "Unknown"


# ── Line-level sensors ────────────────────────────────────────────────────

class PTVMaxDelaySensor(CoordinatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_max_delay"
        self._attr_name = f"PTV {_line_label(coordinator, line)}Max Delay"

    @property
    def native_value(self):
        return round(_ld(self.coordinator, self._line).get("max_delay_seconds", 0) / 60, 1)

    @property
    def icon(self):
        v = self.native_value or 0
        if v >= 10: return "mdi:clock-alert"
        if v >= 5:  return "mdi:clock-remove"
        return "mdi:clock-check"

    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVStatusSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_status"
        self._attr_name = f"PTV {_line_label(coordinator, line)}Status"

    @property
    def native_value(self):
        data = _ld(self.coordinator, self._line)
        # Build inline status (alerts + delays + next citybound from first station)
        parts = []
        if data.get("alert_count", 0):
            parts.append(data.get("alert_text",""))
        if data.get("has_cancellations"):
            parts.append(f"{len(data.get('cancelled_trips',[]))} cancelled")
        if data.get("has_delays"):
            parts.append(f"delays up to {round(data.get('max_delay_seconds',0)/60)} min")
        # First citybound from first station
        stations = data.get("stations", {})
        if stations:
            first_station = next(iter(stations.values()), {})
            cb = first_station.get("citybound", [])
            if cb:
                dep = cb[0]
                parts.append(_fmt(dep["dt_iso"], dep["delay_sec"], dep["is_realtime"], dep.get("is_express", False)))
        if not parts:
            return f"{line_phrase(getattr(self.coordinator, 'mode', MODE_TRAIN), self._line)} running normally"
        return f"{self._line}: " + "; ".join(parts)

    @property
    def icon(self):
        level = _ld(self.coordinator, self._line).get("problem_level","none")
        if level in ("major","cancellations"): return _ic(self.coordinator, "mdi:train-car-remove")
        if level in ("alert","delays"):        return _ic(self.coordinator, "mdi:train-alert")
        return "mdi:train"

    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVProblemLevelSensor(CoordinatorEntity, SensorEntity):
    """none / delays / alert / cancellations / major — use in automation conditions."""
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_problem_level"
        self._attr_name = f"PTV {_line_label(coordinator, line)}Problem Level"

    @property
    def native_value(self):
        return _ld(self.coordinator, self._line).get("problem_level", "none")

    @property
    def icon(self):
        v = self.native_value
        if v == "major":         return "mdi:alert-octagon"
        if v == "cancellations": return _ic(self.coordinator, "mdi:train-car-remove")
        if v == "alert":         return "mdi:alert-circle"
        if v == "delays":        return "mdi:clock-alert"
        return "mdi:check-circle-outline"

    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVProblemSummarySensor(CoordinatorEntity, SensorEntity):
    """TTS-ready plain English sentence about the current situation."""
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_problem_summary"
        self._attr_name = f"PTV {_line_label(coordinator, line)}Problem Summary"

    @property
    def native_value(self):
        return _ld(self.coordinator, self._line).get(
            "problem_summary",
            f"{line_phrase(getattr(self.coordinator, 'mode', MODE_TRAIN), self._line)} is running normally."
        )

    @property
    def icon(self):
        return "mdi:text-box-outline"

    @property
    def extra_state_attributes(self):
        d = _ld(self.coordinator, self._line)
        works = d.get("overnight_works_text", "")
        return {
            # Same sentence minus works that haven't started yet
            "live_summary": d.get("problem_summary_live", self.native_value),
            "overnight_works": works,
            "has_overnight_works": bool(works),
        }

    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVAlertCountSensor(CoordinatorEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_alert_count"
        self._attr_name = f"PTV {_line_label(coordinator, line)}Active Alerts"

    @property
    def native_value(self): return _ld(self.coordinator, self._line).get("alert_count", 0)
    @property
    def icon(self): return "mdi:alert-circle" if (self.native_value or 0) > 0 else "mdi:alert-circle-outline"
    @property
    def extra_state_attributes(self): return {"alerts": _ld(self.coordinator, self._line).get("alerts", [])}
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVAlertTextSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_alert_text"
        self._attr_name = f"PTV {_line_label(coordinator, line)}Alert"

    @property
    def native_value(self): return _ld(self.coordinator, self._line).get("alert_text","No active alerts")
    @property
    def icon(self): return "mdi:alert" if self.native_value != "No active alerts" else "mdi:check-circle-outline"
    @property
    def extra_state_attributes(self):
        data = _ld(self.coordinator, self._line)
        alerts = data.get("alerts", [])
        attrs = {
            "alert_count": len(alerts),
            "live_alert_count": data.get("live_alert_count", len(alerts)),
        }
        for i, a in enumerate(alerts, 1):
            for k in ("header","description","cause","effect",
                      "is_overnight_works","window_from","window_to","active_now"):
                attrs[f"alert_{i}_{k}"] = a.get(k,"")
        return attrs
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVTrainCountSensor(CoordinatorEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:train"
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_train_count"
        self._attr_name = (f"PTV {_line_label(coordinator, line)}"
                           f"Active {'Trams' if _is_tram(coordinator) else 'Trains'}")

    @property
    def native_value(self): return _ld(self.coordinator, self._line).get("train_count", 0)
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVOccupancySensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_occupancy"
        self._attr_name = f"PTV {_line_label(coordinator, line)}Occupancy"

    @property
    def native_value(self): return _ld(self.coordinator, self._line).get("worst_occupancy","Unknown")
    @property
    def extra_state_attributes(self):
        return {"occupancy_level": _ld(self.coordinator, self._line).get("worst_occupancy_int",-1)}
    @property
    def icon(self):
        lvl = _ld(self.coordinator, self._line).get("worst_occupancy_int",-1)
        if lvl >= 5: return "mdi:seat-passenger"
        if lvl >= 3: return "mdi:human-queue"
        if lvl >= 1: return "mdi:seat"
        return "mdi:seat-outline"
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


# ── Per-station upcoming train sensors ───────────────────────────────────

class PTVUpcomingTrainSensor(CoordinatorEntity, SensorEntity):
    """
    Timestamp sensor for the Nth upcoming train in a given direction.
    Attributes include delay, on_time, is_realtime, source, formatted.
    """
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, line, station, direction, index):
        super().__init__(coordinator)
        self._line = line
        self._station = station
        self._station_key = station.lower()
        self._direction = direction   # "citybound" or "outbound"
        self._index = index           # 1-based

        slug_line = line.lower().replace(" ","_")
        slug_st = _slug(station, _is_tram(coordinator))
        dir_label = _direction_label(coordinator, line, direction)

        self._attr_unique_id = f"ptv_{slug_line}_{slug_st}_{direction}_{index}"
        self._attr_name = f"PTV {_line_label(coordinator, line)}{station} {dir_label} {index}"

    @property
    def _entry(self) -> dict | None:
        deps = _sd(self.coordinator, self._line, self._station_key).get(self._direction, [])
        idx = self._index - 1
        return deps[idx] if idx < len(deps) else None

    @property
    def native_value(self) -> datetime | None:
        entry = self._entry
        if not entry:
            return None
        try:
            return datetime.fromisoformat(entry["dt_iso"])
        except Exception:
            return None

    @property
    def extra_state_attributes(self) -> dict:
        entry = self._entry
        if not entry:
            return {"station": self._station, "direction": self._direction, "index": self._index}
        delay_sec = entry.get("delay_sec", 0)
        is_rt = entry.get("is_realtime", False)
        return {
            "station": self._station,
            "direction": "Towards City" if self._direction == "citybound" else "Towards Hurstbridge/Eltham",
            "index": self._index,
            "delay_seconds": delay_sec,
            "delay_minutes": round(delay_sec / 60, 1),
            "on_time": delay_sec < 60,
            "is_realtime": is_rt,
            "is_express": entry.get("is_express", False),
            "source": "realtime" if is_rt else "scheduled",
            "formatted": _fmt(entry["dt_iso"], delay_sec, is_rt, entry.get("is_express", False)),
        }

    @property
    def icon(self) -> str:
        entry = self._entry
        if not entry:
            return "mdi:train"
        if entry.get("delay_sec", 0) >= 300:
            return "mdi:train-alert"
        if self._direction == "citybound":
            return "mdi:train" if self._index == 1 else "mdi:train-variant"
        return "mdi:train-car" if self._index == 1 else "mdi:train-car-flatbed"

    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))
