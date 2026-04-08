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
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_API_KEY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DOMAIN,
)
from .foxess import FoxESSClient, Inverter
from .foxess.client import FoxESSApiError

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

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self._config_entry.options.get(
            CONF_MIN_SOC_ON_GRID, DEFAULT_MIN_SOC_ON_GRID
        )
        current_entity = self._config_entry.options.get(CONF_BATTERY_SOC_ENTITY, "")
        current_capacity = self._config_entry.options.get(
            CONF_BATTERY_CAPACITY_KWH, 0.0
        )
        current_min_power = self._config_entry.options.get(
            CONF_MIN_POWER_CHANGE, DEFAULT_MIN_POWER_CHANGE
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_MIN_SOC_ON_GRID, default=current): vol.All(
                        int, vol.Range(min=11, max=100)
                    ),
                    vol.Optional(
                        CONF_BATTERY_SOC_ENTITY, default=current_entity
                    ): EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(
                        CONF_BATTERY_CAPACITY_KWH, default=current_capacity
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0.0,
                            max=100.0,
                            step=0.1,
                            unit_of_measurement="kWh",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_MIN_POWER_CHANGE, default=current_min_power
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=5000,
                            step=50,
                            unit_of_measurement="W",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
