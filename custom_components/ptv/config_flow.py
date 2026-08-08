"""Config flow for PTV."""
from __future__ import annotations
import logging
from typing import Any

import requests
import voluptuous as vol
from google.transit import gtfs_realtime_pb2

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector, NumberSelectorConfig, NumberSelectorMode,
    SelectSelector, SelectSelectorConfig, SelectSelectorMode,
    TextSelector, TextSelectorConfig, TextSelectorType,
    TimeSelector,
)

# HA 2023.4+ supports SelectSelector with custom_value=True for searchable dropdowns

from .const import (
    API_BASE_URL, API_HEADER_KEY, API_TRIP_UPDATES_PATH,
    CONF_API_KEY, CONF_LINES, CONF_SCAN_INTERVAL, CONF_STATIONS,
    CONF_STATION_NAME, CONF_STATION_DIRECTIONS, CONF_STATION_COUNT,
    CONF_ALERT_EFFECTS, ALERT_EFFECT_OPTIONS, DEFAULT_ALERT_EFFECTS,
    CONF_MODE, DEFAULT_MODE, MODES, MODE_TRAM, mode_info, routes_for_mode,
    CONF_OFFPEAK_INTERVAL, CONF_OVERNIGHT_INTERVAL,
    CONF_PEAK_AM_START, CONF_PEAK_AM_END, CONF_PEAK_PM_START, CONF_PEAK_PM_END,
    CONF_OVERNIGHT_START, CONF_OVERNIGHT_END, CONF_PEAK_ON_WEEKENDS,
    DEFAULT_OFFPEAK_INTERVAL, DEFAULT_OVERNIGHT_INTERVAL,
    DEFAULT_PEAK_AM_START, DEFAULT_PEAK_AM_END,
    DEFAULT_PEAK_PM_START, DEFAULT_PEAK_PM_END,
    DEFAULT_OVERNIGHT_START, DEFAULT_OVERNIGHT_END, DEFAULT_PEAK_ON_WEEKENDS,
    DEFAULT_SCAN_INTERVAL, DEFAULT_STATION_COUNT,
    DIR_CITYBOUND, DIR_OUTBOUND, DIR_BOTH,
    DOMAIN, METRO_LINES,
)
from .gtfs_static import download_and_cache_gtfs, get_all_station_names, get_direction_label

_LOGGER = logging.getLogger(__name__)

DIRECTION_OPTIONS = [
    {"value": DIR_BOTH,      "label": "Both directions"},
    {"value": DIR_CITYBOUND, "label": "Citybound only (towards city)"},
    {"value": DIR_OUTBOUND,  "label": "Outbound only (away from city)"},
]


def _validate_api_key(api_key: str) -> str | None:
    try:
        resp = requests.get(
            f"{API_BASE_URL}{API_TRIP_UPDATES_PATH}",
            headers={API_HEADER_KEY: api_key}, timeout=15,
        )
        if resp.status_code in (401, 403):
            return "invalid_auth"
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        return None
    except requests.RequestException:
        return "cannot_connect"
    except Exception:
        return "unknown"


class PTVTrainDelaysConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> "PTVOptionsFlow":
        return PTVOptionsFlow(config_entry)

    def __init__(self):
        self._api_key = ""
        self._lines = []
        self._scan_interval = DEFAULT_SCAN_INTERVAL
        self._alert_effects = DEFAULT_ALERT_EFFECTS
        self._stations: list[dict] = []
        self._gtfs_cache: dict = {}
        self._station_names: list[str] = []
        self._mode = DEFAULT_MODE

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            error = await self.hass.async_add_executor_job(_validate_api_key, api_key)
            if error:
                errors["base"] = error
            else:
                self._api_key = api_key
                self._mode = user_input.get(CONF_MODE, DEFAULT_MODE)
                return await self.async_step_lines()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_MODE, default=DEFAULT_MODE): SelectSelector(
                    SelectSelectorConfig(
                        options=[{"value": m, "label": i["label"]} for m, i in MODES.items()],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
            errors=errors,
            description_placeholders={"portal_url": "https://opendata.transport.vic.gov.au"},
        )

    def _route_options(self) -> list:
        """Route picker entries, numerically ordered for tram route numbers."""
        routes = routes_for_mode(self._mode)
        if self._mode == MODE_TRAM:
            return sorted(routes, key=lambda n: int(n.split()[-1]))
        return sorted(routes)

    async def async_step_lines(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        sorted_lines = self._route_options()

        if user_input is not None:
            selected = user_input.get(CONF_LINES, [])
            if isinstance(selected, str):
                selected = [selected]
            if not selected:
                errors[CONF_LINES] = "no_lines_selected"
            else:
                self._lines = selected
                self._scan_interval = int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
                return await self.async_step_alerts()

        return self.async_show_form(
            step_id="lines",
            data_schema=vol.Schema({
                vol.Required(CONF_LINES): SelectSelector(
                    SelectSelectorConfig(options=sorted_lines, multiple=True, mode=SelectSelectorMode.LIST)
                ),
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
                    NumberSelectorConfig(min=30, max=300, step=10, mode=NumberSelectorMode.BOX, unit_of_measurement="seconds")
                ),
            }),
            errors=errors,
        )

    async def async_step_alerts(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose which service alert effect types to include."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input.get(CONF_ALERT_EFFECTS, [])
            if isinstance(selected, str):
                selected = [selected]
            if not selected:
                errors[CONF_ALERT_EFFECTS] = "no_alerts_selected"
            else:
                self._alert_effects = selected
                return await self.async_step_station()

        effect_options = [
            {"value": k, "label": v} for k, v in ALERT_EFFECT_OPTIONS.items()
        ]

        return self.async_show_form(
            step_id="alerts",
            data_schema=vol.Schema({
                vol.Required(CONF_ALERT_EFFECTS, default=list(DEFAULT_ALERT_EFFECTS)): SelectSelector(
                    SelectSelectorConfig(
                        options=effect_options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
            errors=errors,
        )

    def _direction_options(self) -> list:
        """Direction picker entries.

        Trains are citybound/outbound; trams have no such notion, so label each
        direction with where it is headed (from the GTFS headsigns).
        """
        if self._mode != MODE_TRAM:
            return [{"value": o["value"], "label": o["label"]} for o in DIRECTION_OPTIONS]

        routes = routes_for_mode(self._mode)
        dests: dict[str, list[str]] = {DIR_CITYBOUND: [], DIR_OUTBOUND: []}
        for line in self._lines:
            rid = routes.get(line)
            if not rid:
                continue
            # direction_id 1 shares the citybound slot, 0 the outbound slot
            for key, did in ((DIR_CITYBOUND, 1), (DIR_OUTBOUND, 0)):
                label = get_direction_label(self._gtfs_cache, rid, did)
                if label and label not in dests[key]:
                    dests[key].append(label)

        def label_for(key, fallback):
            return f"Towards {' / '.join(dests[key])}" if dests[key] else fallback

        return [
            {"value": DIR_BOTH, "label": "Both directions"},
            {"value": DIR_CITYBOUND, "label": label_for(DIR_CITYBOUND, "Direction 1")},
            {"value": DIR_OUTBOUND, "label": label_for(DIR_OUTBOUND, "Direction 0")},
        ]

    async def async_step_station(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add a station (can be repeated)."""
        errors: dict[str, str] = {}

        # Load station names from GTFS if we haven't already
        if not self._station_names:
            routes = routes_for_mode(self._mode)
            route_ids = {routes[l] for l in self._lines if l in routes}
            try:
                self._gtfs_cache = await self.hass.async_add_executor_job(
                    download_and_cache_gtfs,
                    self.hass.config.config_dir,
                    [],   # no stations yet — just need the stops list
                    route_ids,
                    self._mode,
                )
                self._station_names = get_all_station_names(self._gtfs_cache)
            except Exception as err:
                _LOGGER.warning("Could not load station names: %s", err)
                self._station_names = []
            if not self._station_names:
                # Never substitute a placeholder list here — it looks like the
                # real station list and hides the failure. custom_value=True
                # still lets the station be typed in by hand.
                errors["base"] = "station_list_unavailable"

        # "(done)" sentinel at top; rest sorted alphabetically via sort=True in selector
        options = ["(Done — no more stations)"] + self._station_names

        if user_input is not None:
            station = user_input.get(CONF_STATION_NAME, "").strip()
            if station and station not in ("(Done — no more stations)", ""):
                self._stations.append({
                    CONF_STATION_NAME: station,
                    CONF_STATION_DIRECTIONS: user_input.get(CONF_STATION_DIRECTIONS, DIR_BOTH),
                    CONF_STATION_COUNT: int(user_input.get(CONF_STATION_COUNT, DEFAULT_STATION_COUNT)),
                })
                # Offer to add another station
                return await self.async_step_station()
            else:
                # Done adding stations — save entry
                station_list = ", ".join(s[CONF_STATION_NAME] for s in self._stations) if self._stations else "no station"
                return self.async_create_entry(
                    title=f"PTV {mode_info(self._mode)['label']} – {', '.join(self._lines)}",
                    data={
                        CONF_API_KEY: self._api_key,
                        CONF_MODE: self._mode,
                        CONF_LINES: self._lines,
                        CONF_SCAN_INTERVAL: self._scan_interval,
                        CONF_ALERT_EFFECTS: self._alert_effects,
                        CONF_STATIONS: self._stations,
                    },
                )

        added = f" ({len(self._stations)} added so far)" if self._stations else ""
        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema({
                vol.Optional(CONF_STATION_NAME): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,   # enables type-to-search combo box
                        sort=True,
                    )
                ),
                vol.Optional(CONF_STATION_DIRECTIONS, default=DIR_BOTH): SelectSelector(
                    SelectSelectorConfig(
                        options=self._direction_options(),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_STATION_COUNT, default=DEFAULT_STATION_COUNT): NumberSelector(
                    NumberSelectorConfig(min=1, max=4, step=1, mode=NumberSelectorMode.BOX)
                ),
            }),
            errors=errors,
            description_placeholders={
                "lines": ", ".join(self._lines),
                "added": added,
            },
        )


class PTVOptionsFlow(config_entries.OptionsFlow):
    """Adjust polling rates without re-adding the integration."""

    def __init__(self, config_entry) -> None:
        # Deliberately not `self.config_entry` — assigning that is deprecated
        # in newer HA, where the framework supplies it.
        self._entry = config_entry

    def _current(self, key: str, default: Any) -> Any:
        return self._entry.options.get(key, self._entry.data.get(key, default))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input[CONF_PEAK_AM_START] == user_input[CONF_PEAK_AM_END] or \
                    user_input[CONF_PEAK_PM_START] == user_input[CONF_PEAK_PM_END]:
                errors["base"] = "empty_peak_window"
            else:
                for key in (CONF_SCAN_INTERVAL, CONF_OFFPEAK_INTERVAL, CONF_OVERNIGHT_INTERVAL):
                    user_input[key] = int(user_input[key])
                return self.async_create_entry(title="", data=user_input)

        def field(key, default):
            return vol.Optional(key, default=self._current(key, default))

        interval = NumberSelector(NumberSelectorConfig(
            min=30, max=3600, step=10, mode=NumberSelectorMode.BOX,
            unit_of_measurement="seconds",
        ))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                field(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL): interval,
                field(CONF_OFFPEAK_INTERVAL, DEFAULT_OFFPEAK_INTERVAL): interval,
                field(CONF_OVERNIGHT_INTERVAL, DEFAULT_OVERNIGHT_INTERVAL): interval,
                field(CONF_PEAK_AM_START, DEFAULT_PEAK_AM_START): TimeSelector(),
                field(CONF_PEAK_AM_END, DEFAULT_PEAK_AM_END): TimeSelector(),
                field(CONF_PEAK_PM_START, DEFAULT_PEAK_PM_START): TimeSelector(),
                field(CONF_PEAK_PM_END, DEFAULT_PEAK_PM_END): TimeSelector(),
                field(CONF_OVERNIGHT_START, DEFAULT_OVERNIGHT_START): TimeSelector(),
                field(CONF_OVERNIGHT_END, DEFAULT_OVERNIGHT_END): TimeSelector(),
                field(CONF_PEAK_ON_WEEKENDS, DEFAULT_PEAK_ON_WEEKENDS): BooleanSelector(),
            }),
            errors=errors,
        )
