"""Binary sensors for PTV."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .sensor import (
    _device, _dir_suffix, _direction_label, _ic, _is_tram, _line_label, _stats, _uid,
    configured_directions,
)
from .const import CONF_LINES, CONF_STATIONS, DOMAIN
from . import PTVDataCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PTVDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    lines = entry.data[CONF_LINES]
    entities = []
    for line in lines:
        entities += [
            PTVDelayBinarySensor(coordinator, line),
            PTVCancellationBinarySensor(coordinator, line),
            PTVAlertBinarySensor(coordinator, line),
            PTVProblemBinarySensor(coordinator, line),
        ]
        for direction in configured_directions(entry.data.get(CONF_STATIONS, [])):
            entities += [
                PTVDelayBinarySensor(coordinator, line, direction),
                PTVCancellationBinarySensor(coordinator, line, direction),
            ]
    async_add_entities(entities)


def _name(coordinator, line, direction, what: str) -> str:
    """"PTV Sunbury Line Citybound Delays"; trams "PTV Route 96 To Box Hill Delays"."""
    dir_name = _direction_label(coordinator, line, direction)
    if _is_tram(coordinator):
        return f"PTV {line} {dir_name} {what}"
    return f"PTV {line} Line {dir_name} {what}"


def _ld(coordinator, line):
    return coordinator.data.get(line, {}) if coordinator.data else {}



class PTVDelayBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """ON when delays ≥ 5 min."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    def __init__(self, coordinator, line, direction=None):
        super().__init__(coordinator)
        self._line = line
        self._direction = direction  # None = whole line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = _uid(coordinator, f"ptv_{slug}{_dir_suffix(direction)}_delays")
        self._attr_name = _name(coordinator, line, direction, "Delays") if direction \
            else f"PTV {_line_label(coordinator, line)}Delays"

    @property
    def is_on(self): return _stats(self.coordinator, self._line, self._direction).get("has_delays", False)
    @property
    def icon(self): return _ic(self.coordinator, "mdi:train-alert" if self.is_on else "mdi:train")
    @property
    def extra_state_attributes(self):
        d = _stats(self.coordinator, self._line, self._direction)
        return {
            "delay_count": d.get("delay_count", 0),
            "max_delay_minutes": round(d.get("max_delay_seconds", 0) / 60, 1),
        }
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVCancellationBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """ON when services are cancelled."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    def __init__(self, coordinator, line, direction=None):
        super().__init__(coordinator)
        self._line = line
        self._direction = direction  # None = whole line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = _uid(coordinator, f"ptv_{slug}{_dir_suffix(direction)}_cancellations")
        self._attr_name = _name(coordinator, line, direction, "Cancellations") if direction \
            else f"PTV {_line_label(coordinator, line)}Cancellations"

    @property
    def is_on(self): return _stats(self.coordinator, self._line, self._direction).get("has_cancellations", False)
    @property
    def icon(self): return _ic(self.coordinator, "mdi:train-car-remove" if self.is_on else "mdi:train")
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVAlertBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """ON when PTV has published a service alert."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = _uid(coordinator, f"ptv_{slug}_service_alert")
        self._attr_name = f"PTV {_line_label(coordinator, line)}Service Alert"

    @property
    def is_on(self): return _ld(self.coordinator, self._line).get("alert_count", 0) > 0
    @property
    def icon(self): return "mdi:alert" if self.is_on else "mdi:check-circle-outline"
    @property
    def extra_state_attributes(self):
        d = _ld(self.coordinator, self._line)
        return {"alert_count": d.get("alert_count",0), "alert_text": d.get("alert_text","")}
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVProblemBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """ON when there is ANY problem — delays, alerts, or cancellations.
    The problem_level attribute tells you exactly how bad it is.
    Ideal trigger for the morning automation."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = _uid(coordinator, f"ptv_{slug}_any_problem")
        self._attr_name = f"PTV {_line_label(coordinator, line)}Any Problem"

    @property
    def is_on(self):
        return _ld(self.coordinator, self._line).get("problem_level","none") != "none"
    @property
    def icon(self):
        level = _ld(self.coordinator, self._line).get("problem_level","none")
        if level == "major":         return "mdi:alert-octagon"
        if level == "cancellations": return _ic(self.coordinator, "mdi:train-car-remove")
        if level == "alert":         return "mdi:alert-circle"
        if level == "delays":        return "mdi:clock-alert"
        return "mdi:check-circle-outline"
    @property
    def extra_state_attributes(self):
        d = _ld(self.coordinator, self._line)
        return {
            "problem_level": d.get("problem_level","none"),
            "problem_summary": d.get("problem_summary",""),
        }
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))
