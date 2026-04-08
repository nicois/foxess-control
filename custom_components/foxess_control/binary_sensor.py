"""Binary sensors for FoxESS Control smart charge/discharge status."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

SCAN_INTERVAL = datetime.timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FoxESS Control binary sensors."""
    async_add_entities(
        [
            SmartChargeActiveSensor(hass, entry),
            SmartDischargeActiveSensor(hass, entry),
        ]
    )


class SmartChargeActiveSensor(BinarySensorEntity):
    """Binary sensor that is on while a smart charge session is active."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_smart_charge_active"
        self._attr_name = "FoxESS Smart Charge Active"
        self.hass = hass

    @property
    def is_on(self) -> bool:
        """Return True if a smart charge session is active."""
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return False
        return domain_data.get("_smart_charge_state") is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return smart charge session details when active."""
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return None
        state = domain_data.get("_smart_charge_state")
        if state is None:
            return None
        return {
            "target_soc": state["target_soc"],
            "current_power_w": state["last_power_w"],
            "max_power_w": state["max_power_w"],
            "end_time": state["end"].isoformat(),
            "soc_entity": state["soc_entity"],
        }


class SmartDischargeActiveSensor(BinarySensorEntity):
    """Binary sensor that is on while a smart discharge session is active."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_smart_discharge_active"
        self._attr_name = "FoxESS Smart Discharge Active"
        self.hass = hass

    @property
    def is_on(self) -> bool:
        """Return True if a smart discharge session is active."""
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return False
        return domain_data.get("_smart_discharge_state") is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return smart discharge session details when active."""
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return None
        state = domain_data.get("_smart_discharge_state")
        if state is None:
            return None
        return {
            "min_soc": state["min_soc"],
            "last_power_w": state["last_power_w"],
            "end_time": state["end"].isoformat(),
            "soc_entity": state["soc_entity"],
        }
