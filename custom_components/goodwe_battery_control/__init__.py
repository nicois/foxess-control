"""GoodWe Battery Control — Home Assistant integration for inverter mode management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .const import (
    CONF_CHARGE_POWER_ENTITY,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_INVERTER_POWER,
    CONF_MIN_SOC_ENTITY,
    CONF_POLLING_INTERVAL,
    CONF_WORK_MODE_ENTITY,
    DEFAULT_ENTITY_POLLING_INTERVAL,
    DEFAULT_GOODWE_INVERTER_POWER,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import GoodWeEntityCoordinator
from .smart_battery.adapter import EntityAdapter
from .smart_battery.config_flow_base import build_entity_map
from .smart_battery.const import (
    SERVICE_CLEAR_OVERRIDES,
    SERVICE_FEEDIN,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_SMART_CHARGE,
    SERVICE_SMART_DISCHARGE,
    STORAGE_VERSION,
)
from .smart_battery.services import register_services
from .smart_battery.types import WorkMode

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "goodwe_battery_control_sessions"

# GoodWe mode names as exposed by the home-assistant-goodwe-inverter integration.
_GOODWE_MODE_MAP: dict[WorkMode, str] = {
    WorkMode.SELF_USE: "eco",
    WorkMode.FORCE_CHARGE: "eco_charge",
    WorkMode.FORCE_DISCHARGE: "eco_discharge",
    WorkMode.BACKUP: "backup",
    WorkMode.FEEDIN: "peak_shaving",
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GoodWe Battery Control from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("_smart_discharge_unsubs", [])
    hass.data[DOMAIN].setdefault("_smart_charge_unsubs", [])
    if "_store" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["_store"] = Store[dict[str, Any]](
            hass, STORAGE_VERSION, STORAGE_KEY
        )

    entity_map = build_entity_map(entry.options)
    if not entity_map:
        _LOGGER.error(
            "No entity mappings configured. Configure GoodWe entities in options."
        )
        return False

    polling_interval = int(
        entry.options.get(CONF_POLLING_INTERVAL, DEFAULT_ENTITY_POLLING_INTERVAL)
    )
    coordinator = GoodWeEntityCoordinator(hass, entity_map, polling_interval)
    await coordinator.async_config_entry_first_refresh()

    max_power = int(
        entry.options.get(CONF_INVERTER_POWER, DEFAULT_GOODWE_INVERTER_POWER)
    )
    adapter = EntityAdapter(
        mode_map=_GOODWE_MODE_MAP,
        work_mode_entity=entry.options[CONF_WORK_MODE_ENTITY],
        charge_power_entity=entry.options.get(CONF_CHARGE_POWER_ENTITY),
        discharge_power_entity=entry.options.get(CONF_DISCHARGE_POWER_ENTITY),
        min_soc_entity=entry.options.get(CONF_MIN_SOC_ENTITY),
        max_power_w=max_power,
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "adapter": adapter,
        "entry": entry,
    }

    # Register services once (first real entry)
    real_entries = {k for k in hass.data[DOMAIN] if not k.startswith("_")}
    if len(real_entries) == 1:
        register_services(hass, DOMAIN, adapter)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    hass.data[DOMAIN].pop(entry.entry_id)

    remaining = {k for k in hass.data[DOMAIN] if not k.startswith("_")}
    if not remaining:
        from .smart_battery.listeners import cancel_smart_charge, cancel_smart_discharge

        cancel_smart_charge(hass, DOMAIN, clear_storage=False)
        cancel_smart_discharge(hass, DOMAIN, clear_storage=False)
        hass.data.pop(DOMAIN)
        for svc in (
            SERVICE_CLEAR_OVERRIDES,
            SERVICE_FEEDIN,
            SERVICE_FORCE_CHARGE,
            SERVICE_FORCE_DISCHARGE,
            SERVICE_SMART_CHARGE,
            SERVICE_SMART_DISCHARGE,
        ):
            hass.services.async_remove(DOMAIN, svc)

    return True
