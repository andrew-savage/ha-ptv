"""Binary sensors for PTV."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .sensor import _device, _ic, _is_tram
from .const import CONF_LINES, DOMAIN
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
    async_add_entities(entities)


def _ld(coordinator, line):
    return coordinator.data.get(line, {}) if coordinator.data else {}



class PTVDelayBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """ON when delays ≥ 5 min."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_delays"
        self._attr_name = f"PTV {line} Line Delays"

    @property
    def is_on(self): return _ld(self.coordinator, self._line).get("has_delays", False)
    @property
    def icon(self): return _ic(self.coordinator, "mdi:train-alert" if self.is_on else "mdi:train")
    @property
    def extra_state_attributes(self):
        d = _ld(self.coordinator, self._line)
        return {
            "delay_count": d.get("delay_count", 0),
            "max_delay_minutes": round(d.get("max_delay_seconds", 0) / 60, 1),
        }
    @property
    def device_info(self): return _device(self._line, _is_tram(self.coordinator))


class PTVCancellationBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """ON when services are cancelled."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    def __init__(self, coordinator, line):
        super().__init__(coordinator)
        self._line = line
        slug = line.lower().replace(" ","_")
        self._attr_unique_id = f"ptv_{slug}_cancellations"
        self._attr_name = f"PTV {line} Line Cancellations"

    @property
    def is_on(self): return _ld(self.coordinator, self._line).get("has_cancellations", False)
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
        self._attr_unique_id = f"ptv_{slug}_service_alert"
        self._attr_name = f"PTV {line} Line Service Alert"

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
        self._attr_unique_id = f"ptv_{slug}_any_problem"
        self._attr_name = f"PTV {line} Line Any Problem"

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
