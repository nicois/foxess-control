"""Shared options flow steps for smart battery integrations.

Brand integrations subclass ``SmartBatteryOptionsFlow`` to get the
shared battery-configuration step (``async_step_battery``) and the
entity-mapping step (``async_step_entities``).

Typical usage in a brand's ``config_flow.py``::

    class MyBrandOptionsFlow(SmartBatteryOptionsFlow):
        async def async_step_init(self, user_input):
            if user_input is not None:
                self._init_data = user_input
                return await self.async_step_entities()
            return self.async_show_form(...)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_API_MIN_SOC,
    CONF_BAT_CHARGE_POWER_ENTITY,
    CONF_BAT_DISCHARGE_POWER_ENTITY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BMS_POLLING_INTERVAL,
    CONF_CHARGE_POWER_ENTITY,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_FEEDIN_ENERGY_ENTITY,
    CONF_FEEDIN_POWER_ENTITY,
    CONF_GRID_CONSUMPTION_POWER_ENTITY,
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
    DEFAULT_BMS_POLLING_INTERVAL,
    DEFAULT_INVERTER_POWER,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_SMART_HEADROOM,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# Standard mapping from config option key → (coordinator variable, expected unit).
# This is the shared contract between entity_mapping_schema and EntityCoordinator.
# The expected unit tells the EntityCoordinator what the cloud API would return
# so it can convert source entities (which may use W, Wh, etc.) to match.
_ENTITY_VAR_MAP: list[tuple[str, str, str]] = [
    (CONF_SOC_ENTITY, "SoC", "%"),
    (CONF_LOADS_POWER_ENTITY, "loadsPower", "kW"),
    (CONF_PV_POWER_ENTITY, "pvPower", "kW"),
    (CONF_FEEDIN_ENERGY_ENTITY, "feedin", "kWh"),
    (CONF_WORK_MODE_ENTITY, "_work_mode", ""),
    (CONF_BAT_CHARGE_POWER_ENTITY, "batChargePower", "kW"),
    (CONF_BAT_DISCHARGE_POWER_ENTITY, "batDischargePower", "kW"),
    (CONF_GRID_CONSUMPTION_POWER_ENTITY, "gridConsumptionPower", "kW"),
    (CONF_FEEDIN_POWER_ENTITY, "feedinPower", "kW"),
]

# All entity keys that the entity mapping step manages
ENTITY_KEYS = (
    CONF_WORK_MODE_ENTITY,
    CONF_CHARGE_POWER_ENTITY,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_MIN_SOC_ENTITY,
    CONF_SOC_ENTITY,
    CONF_LOADS_POWER_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_FEEDIN_ENERGY_ENTITY,
    CONF_BAT_CHARGE_POWER_ENTITY,
    CONF_BAT_DISCHARGE_POWER_ENTITY,
    CONF_GRID_CONSUMPTION_POWER_ENTITY,
    CONF_FEEDIN_POWER_ENTITY,
    CONF_INVERTER_POWER,
)


def battery_options_schema(
    config_entry: ConfigEntry,
) -> vol.Schema:
    """Build the vol.Schema for the shared battery options step."""
    opts = config_entry.options
    return vol.Schema(
        {
            vol.Optional(
                CONF_MIN_SOC_ON_GRID,
                default=opts.get(CONF_MIN_SOC_ON_GRID, DEFAULT_MIN_SOC_ON_GRID),
            ): vol.All(int, vol.Range(min=0, max=100)),
            vol.Optional(
                CONF_BATTERY_CAPACITY_KWH,
                default=opts.get(CONF_BATTERY_CAPACITY_KWH, 0.0),
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
                CONF_MIN_POWER_CHANGE,
                default=opts.get(CONF_MIN_POWER_CHANGE, DEFAULT_MIN_POWER_CHANGE),
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
                CONF_API_MIN_SOC,
                default=opts.get(CONF_API_MIN_SOC, DEFAULT_API_MIN_SOC),
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
                CONF_POLLING_INTERVAL,
                default=opts.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL),
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
                CONF_BMS_POLLING_INTERVAL,
                default=opts.get(
                    CONF_BMS_POLLING_INTERVAL, DEFAULT_BMS_POLLING_INTERVAL
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=60,
                    max=3600,
                    step=30,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_SMART_HEADROOM,
                default=opts.get(CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM),
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
    )


def entity_mapping_schema(
    config_entry: ConfigEntry,
    detected: dict[str, str],
    *,
    default_inverter_power: int = DEFAULT_INVERTER_POWER,
) -> vol.Schema:
    """Build the vol.Schema for the entity mapping step.

    *detected* maps ``CONF_*_ENTITY`` keys to auto-detected entity IDs.
    """
    opts = config_entry.options

    def _default(conf_key: str) -> str:
        return opts.get(conf_key) or detected.get(conf_key, "")

    select_selector = EntitySelector(EntitySelectorConfig(domain="select"))
    number_selector = EntitySelector(EntitySelectorConfig(domain="number"))
    sensor_selector = EntitySelector(EntitySelectorConfig(domain="sensor"))

    return vol.Schema(
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
                CONF_BAT_CHARGE_POWER_ENTITY,
                default=_default(CONF_BAT_CHARGE_POWER_ENTITY),
            ): sensor_selector,
            vol.Optional(
                CONF_BAT_DISCHARGE_POWER_ENTITY,
                default=_default(CONF_BAT_DISCHARGE_POWER_ENTITY),
            ): sensor_selector,
            vol.Optional(
                CONF_GRID_CONSUMPTION_POWER_ENTITY,
                default=_default(CONF_GRID_CONSUMPTION_POWER_ENTITY),
            ): sensor_selector,
            vol.Optional(
                CONF_FEEDIN_POWER_ENTITY,
                default=_default(CONF_FEEDIN_POWER_ENTITY),
            ): sensor_selector,
            vol.Optional(
                CONF_INVERTER_POWER,
                default=opts.get(CONF_INVERTER_POWER, default_inverter_power),
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
    )


def build_entity_map(opts: Any) -> dict[str, tuple[str, str]]:
    """Build a ``{polled_variable: (entity_id, expected_unit)}`` map.

    Returns an empty dict when entity mode is not configured (no work mode entity).
    """
    if not opts.get(CONF_WORK_MODE_ENTITY):
        return {}

    mapping: dict[str, tuple[str, str]] = {}
    for conf_key, var_name, expected_unit in _ENTITY_VAR_MAP:
        entity_id = opts.get(conf_key, "")
        if entity_id:
            mapping[var_name] = (entity_id, expected_unit)
    return mapping


def detect_entities(
    hass: HomeAssistant,
    platform: str,
    name_map: dict[str, str],
) -> dict[str, str]:
    """Auto-detect entities from the entity registry for a given platform.

    *platform* is the integration domain to search (e.g. ``"goodwe"``,
    ``"foxess_modbus"``).  *name_map* maps ``entity.original_name`` values
    to ``CONF_*`` keys.
    """
    detected: dict[str, str] = {}
    registry = er.async_get(hass)

    for entry in hass.config_entries.async_entries(platform):
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            conf_key = name_map.get(entity.original_name or "")
            if conf_key and conf_key not in detected:
                detected[conf_key] = entity.entity_id

    return detected
