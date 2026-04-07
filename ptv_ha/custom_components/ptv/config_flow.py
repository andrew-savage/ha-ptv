"""Config flow for PTV."""
from __future__ import annotations
import logging
from typing import Any

import requests
import voluptuous as vol
from google.transit import gtfs_realtime_pb2

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector, NumberSelectorConfig, NumberSelectorMode,
    SelectSelector, SelectSelectorConfig, SelectSelectorMode,
    TextSelector, TextSelectorConfig, TextSelectorType,
)

# HA 2023.4+ supports SelectSelector with custom_value=True for searchable dropdowns

from .const import (
    API_BASE_URL, API_HEADER_KEY, API_TRIP_UPDATES_PATH,
    CONF_API_KEY, CONF_LINES, CONF_SCAN_INTERVAL, CONF_STATIONS,
    CONF_STATION_NAME, CONF_STATION_DIRECTIONS, CONF_STATION_COUNT,
    CONF_ALERT_EFFECTS, ALERT_EFFECT_OPTIONS, DEFAULT_ALERT_EFFECTS,
    DEFAULT_SCAN_INTERVAL, DEFAULT_STATION_COUNT,
    DIR_CITYBOUND, DIR_OUTBOUND, DIR_BOTH,
    DOMAIN, METRO_LINES,
)
from .gtfs_static import download_and_cache_gtfs, get_all_station_names

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

    def __init__(self):
        self._api_key = ""
        self._lines = []
        self._scan_interval = DEFAULT_SCAN_INTERVAL
        self._alert_effects = DEFAULT_ALERT_EFFECTS
        self._stations: list[dict] = []
        self._gtfs_cache: dict = {}
        self._station_names: list[str] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            error = await self.hass.async_add_executor_job(_validate_api_key, api_key)
            if error:
                errors["base"] = error
            else:
                self._api_key = api_key
                return await self.async_step_lines()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
            description_placeholders={"portal_url": "https://opendata.transport.vic.gov.au"},
        )

    async def async_step_lines(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        sorted_lines = sorted(METRO_LINES.keys())

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

    async def async_step_station(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add a station (can be repeated)."""
        errors: dict[str, str] = {}

        # Load station names from GTFS if we haven't already
        if not self._station_names:
            route_ids = {METRO_LINES[l] for l in self._lines if l in METRO_LINES}
            try:
                self._gtfs_cache = await self.hass.async_add_executor_job(
                    download_and_cache_gtfs,
                    self.hass.config.config_dir,
                    [],   # no stations yet — just need the stops list
                    route_ids,
                )
                self._station_names = get_all_station_names(self._gtfs_cache)
            except Exception as err:
                _LOGGER.warning("Could not load station names: %s", err)
                self._station_names = []

        # "(done)" sentinel at top; rest sorted alphabetically via sort=True in selector
        options = ["(Done — no more stations)"] + (self._station_names or ["Ivanhoe", "Melbourne Central"])

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
                    title=f"PTV – {', '.join(self._lines)}",
                    data={
                        CONF_API_KEY: self._api_key,
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
                        options=[o["value"] for o in DIRECTION_OPTIONS],
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
