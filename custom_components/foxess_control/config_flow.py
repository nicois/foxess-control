"""Config flow for FoxESS Control integration."""

from __future__ import annotations

import logging
from typing import Any

import requests
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_API_KEY,
    CONF_CHARGE_POWER_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_FEEDIN_ENERGY_ENTITY,
    CONF_LOADS_POWER_ENTITY,
    CONF_MIN_SOC_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SOC_ENTITY,
    CONF_WORK_MODE_ENTITY,
    DOMAIN,
)
from .foxess import FoxESSClient, Inverter
from .foxess.client import FoxESSApiError
from .smart_battery.config_flow_base import (
    ENTITY_KEYS,
    battery_options_schema,
    detect_entities,
    entity_mapping_schema,
)

# Map foxess_modbus original_name → our CONF_* key.
_MODBUS_NAME_MAP: dict[str, str] = {
    "Work Mode": CONF_WORK_MODE_ENTITY,
    "Force Charge Power": CONF_CHARGE_POWER_ENTITY,
    "Force Discharge Power": CONF_DISCHARGE_POWER_ENTITY,
    "Min SoC": CONF_MIN_SOC_ENTITY,
    "Battery SoC": CONF_SOC_ENTITY,
    "Load Power": CONF_LOADS_POWER_ENTITY,
    "PV Power": CONF_PV_POWER_ENTITY,
    "Feed-in Total": CONF_FEEDIN_ENERGY_ENTITY,
}


def _detect_foxess_modbus_entities(
    hass: HomeAssistant,
) -> dict[str, str]:
    """Auto-detect foxess_modbus entities from the entity registry."""
    return detect_entities(hass, "foxess_modbus", _MODBUS_NAME_MAP)


_LOGGER = logging.getLogger(__name__)


def _validate_credentials(api_key: str, device_serial: str) -> None:
    """Validate API credentials by fetching device detail (blocking)."""
    client = FoxESSClient(api_key)
    inverter = Inverter(client, device_serial)
    inverter.get_detail()


class FoxessControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for FoxESS Control."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    _validate_credentials,
                    user_input[CONF_API_KEY],
                    user_input[CONF_DEVICE_SERIAL],
                )
            except FoxESSApiError as err:
                _LOGGER.warning("FoxESS API rejected credentials: %s", err)
                errors["base"] = "invalid_auth"
            except requests.RequestException as err:
                _LOGGER.warning("Could not reach FoxESS Cloud API: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_SERIAL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"FoxESS {user_input[CONF_DEVICE_SERIAL]}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Required(CONF_DEVICE_SERIAL): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow handler."""
        return FoxessControlOptionsFlow(config_entry)


class FoxessControlOptionsFlow(OptionsFlow):
    """Handle options for FoxESS Control."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._init_data: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the core options."""
        if user_input is not None:
            # Check if foxess_modbus is installed — if so, continue to entity step.
            if self.hass.config_entries.async_entries("foxess_modbus"):
                self._init_data = user_input
                return await self.async_step_modbus()
            # No foxess_modbus — clear any stale entity options and save.
            for key in ENTITY_KEYS:
                user_input.pop(key, None)
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=battery_options_schema(self._config_entry),
        )

    async def async_step_modbus(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure foxess_modbus entity mappings."""
        if user_input is not None:
            return self.async_create_entry(data={**self._init_data, **user_input})

        detected = _detect_foxess_modbus_entities(self.hass)
        schema = entity_mapping_schema(self._config_entry, detected)

        return self.async_show_form(
            step_id="modbus",
            data_schema=schema,
        )
