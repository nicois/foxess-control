"""Config flow for GoodWe Battery Control integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_CHARGE_POWER_ENTITY,
    CONF_FEEDIN_ENERGY_ENTITY,
    CONF_LOADS_POWER_ENTITY,
    CONF_MIN_SOC_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SOC_ENTITY,
    CONF_WORK_MODE_ENTITY,
    DEFAULT_GOODWE_INVERTER_POWER,
    DOMAIN,
)
from .smart_battery.config_flow_base import (
    battery_options_schema,
    detect_entities,
    entity_mapping_schema,
)

_LOGGER = logging.getLogger(__name__)

# Map GoodWe entity original_name → our CONF_* key.
_GOODWE_NAME_MAP: dict[str, str] = {
    "Operation Mode": CONF_WORK_MODE_ENTITY,
    "EMS Power Limit": CONF_CHARGE_POWER_ENTITY,
    "Battery Discharge Depth": CONF_MIN_SOC_ENTITY,
    "Battery State of Charge": CONF_SOC_ENTITY,
    "House Consumption": CONF_LOADS_POWER_ENTITY,
    "PV Power": CONF_PV_POWER_ENTITY,
    "Total Export": CONF_FEEDIN_ENERGY_ENTITY,
}


class GoodWeBatteryControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GoodWe Battery Control."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — entity-only, no API credentials needed."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="GoodWe Battery Control",
                data={},
            )

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow handler."""
        return GoodWeOptionsFlow(config_entry)


class GoodWeOptionsFlow(OptionsFlow):
    """Handle options for GoodWe Battery Control."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._init_data: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the core options."""
        if user_input is not None:
            self._init_data = user_input
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="init",
            data_schema=battery_options_schema(self._config_entry),
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure GoodWe entity mappings."""
        if user_input is not None:
            return self.async_create_entry(data={**self._init_data, **user_input})

        detected = detect_entities(self.hass, "goodwe", _GOODWE_NAME_MAP)
        schema = entity_mapping_schema(
            self._config_entry,
            detected,
            default_inverter_power=DEFAULT_GOODWE_INVERTER_POWER,
        )

        return self.async_show_form(
            step_id="entities",
            data_schema=schema,
        )
