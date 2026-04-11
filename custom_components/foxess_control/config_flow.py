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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_API_KEY,
    CONF_API_MIN_SOC,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGE_POWER_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_FEEDIN_ENERGY_ENTITY,
    CONF_INVERTER_POWER,
    CONF_LOADS_POWER_ENTITY,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ENTITY,
    CONF_MIN_SOC_ON_GRID,
    CONF_POLLING_INTERVAL,
    CONF_PV_POWER_ENTITY,
    CONF_SMART_HEADROOM,
    CONF_SOC_ENTITY,
    CONF_WORK_MODE_ENTITY,
    DEFAULT_API_MIN_SOC,
    DEFAULT_INVERTER_POWER,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_SMART_HEADROOM,
    DOMAIN,
)
from .foxess import FoxESSClient, Inverter
from .foxess.client import FoxESSApiError

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
    """Auto-detect foxess_modbus entities from the entity registry.

    Returns a dict of {CONF_*_ENTITY: entity_id} for each detected entity.
    """
    detected: dict[str, str] = {}
    registry = er.async_get(hass)

    for entry in hass.config_entries.async_entries("foxess_modbus"):
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            conf_key = _MODBUS_NAME_MAP.get(entity.original_name or "")
            if conf_key and conf_key not in detected:
                detected[conf_key] = entity.entity_id

    return detected


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
            for key in (
                CONF_WORK_MODE_ENTITY,
                CONF_CHARGE_POWER_ENTITY,
                CONF_DISCHARGE_POWER_ENTITY,
                CONF_MIN_SOC_ENTITY,
                CONF_SOC_ENTITY,
                CONF_LOADS_POWER_ENTITY,
                CONF_PV_POWER_ENTITY,
                CONF_FEEDIN_ENERGY_ENTITY,
                CONF_INVERTER_POWER,
            ):
                user_input.pop(key, None)
            return self.async_create_entry(data=user_input)

        current = self._config_entry.options.get(
            CONF_MIN_SOC_ON_GRID, DEFAULT_MIN_SOC_ON_GRID
        )
        current_capacity = self._config_entry.options.get(
            CONF_BATTERY_CAPACITY_KWH, 0.0
        )
        current_min_power = self._config_entry.options.get(
            CONF_MIN_POWER_CHANGE, DEFAULT_MIN_POWER_CHANGE
        )
        current_api_min_soc = self._config_entry.options.get(
            CONF_API_MIN_SOC, DEFAULT_API_MIN_SOC
        )
        current_polling = self._config_entry.options.get(
            CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL
        )
        current_headroom = self._config_entry.options.get(
            CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_MIN_SOC_ON_GRID, default=current): vol.All(
                        int, vol.Range(min=5, max=100)
                    ),
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
                    vol.Optional(
                        CONF_API_MIN_SOC, default=current_api_min_soc
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=11,
                            step=1,
                            unit_of_measurement="%",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_POLLING_INTERVAL, default=current_polling
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=60,
                            max=600,
                            step=10,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        CONF_SMART_HEADROOM, default=current_headroom
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=25,
                            step=1,
                            unit_of_measurement="%",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )

    async def async_step_modbus(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure foxess_modbus entity mappings."""
        if user_input is not None:
            return self.async_create_entry(data={**self._init_data, **user_input})

        opts = self._config_entry.options
        detected = _detect_foxess_modbus_entities(self.hass)

        def _default(conf_key: str) -> str:
            """Return current option, falling back to auto-detected entity."""
            return opts.get(conf_key) or detected.get(conf_key, "")

        select_selector = EntitySelector(EntitySelectorConfig(domain="select"))
        number_selector = EntitySelector(EntitySelectorConfig(domain="number"))
        sensor_selector = EntitySelector(EntitySelectorConfig(domain="sensor"))

        return self.async_show_form(
            step_id="modbus",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_WORK_MODE_ENTITY,
                        default=_default(CONF_WORK_MODE_ENTITY),
                    ): select_selector,
                    vol.Optional(
                        CONF_CHARGE_POWER_ENTITY,
                        default=_default(CONF_CHARGE_POWER_ENTITY),
                    ): number_selector,
                    vol.Optional(
                        CONF_DISCHARGE_POWER_ENTITY,
                        default=_default(CONF_DISCHARGE_POWER_ENTITY),
                    ): number_selector,
                    vol.Optional(
                        CONF_MIN_SOC_ENTITY,
                        default=_default(CONF_MIN_SOC_ENTITY),
                    ): number_selector,
                    vol.Optional(
                        CONF_SOC_ENTITY,
                        default=_default(CONF_SOC_ENTITY),
                    ): sensor_selector,
                    vol.Optional(
                        CONF_LOADS_POWER_ENTITY,
                        default=_default(CONF_LOADS_POWER_ENTITY),
                    ): sensor_selector,
                    vol.Optional(
                        CONF_PV_POWER_ENTITY,
                        default=_default(CONF_PV_POWER_ENTITY),
                    ): sensor_selector,
                    vol.Optional(
                        CONF_FEEDIN_ENERGY_ENTITY,
                        default=_default(CONF_FEEDIN_ENERGY_ENTITY),
                    ): sensor_selector,
                    vol.Optional(
                        CONF_INVERTER_POWER,
                        default=opts.get(CONF_INVERTER_POWER, DEFAULT_INVERTER_POWER),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1000,
                            max=30000,
                            step=100,
                            unit_of_measurement="W",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
