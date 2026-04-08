"""Sensor for FoxESS Control inverter override status."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

SCAN_INTERVAL = datetime.timedelta(seconds=30)

_ICON_CHARGING = "mdi:battery-charging"
_ICON_DEFERRED = "mdi:battery-clock"
_ICON_DISCHARGING = "mdi:battery-arrow-down"
_ICON_FEEDIN = "mdi:transmission-tower-export"
_ICON_IDLE = "mdi:home-battery"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FoxESS Control sensors."""
    async_add_entities([InverterOverrideStatusSensor(hass, entry)])


def _format_power(watts: int) -> str:
    """Format watts as a human-readable string."""
    if watts >= 1000:
        return f"{watts / 1000:.1f}kW"
    return f"{watts}W"


class InverterOverrideStatusSensor(SensorEntity):
    """Sensor showing the current inverter override mode and power."""

    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_override_status"
        self._attr_name = "FoxESS Override Status"
        self.hass = hass

    @property
    def native_value(self) -> str:
        """Return a human-readable status string."""
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return "Idle"

        charge_state = domain_data.get("_smart_charge_state")
        if charge_state is not None:
            if not charge_state.get("charging_started", True):
                return "Deferred"
            power = charge_state.get("last_power_w", 0)
            return f"Charging {_format_power(power)}"

        discharge_state = domain_data.get("_smart_discharge_state")
        if discharge_state is not None:
            power = discharge_state.get("last_power_w", 0)
            return f"Discharging {_format_power(power)}"

        return "Idle"

    @property
    def icon(self) -> str:
        """Return an icon based on the current override state."""
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return _ICON_IDLE

        charge_state = domain_data.get("_smart_charge_state")
        if charge_state is not None:
            if not charge_state.get("charging_started", True):
                return _ICON_DEFERRED
            return _ICON_CHARGING

        discharge_state = domain_data.get("_smart_discharge_state")
        if discharge_state is not None:
            return _ICON_DISCHARGING

        return _ICON_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return session details as attributes."""
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return None

        charge_state = domain_data.get("_smart_charge_state")
        if charge_state is not None:
            phase = (
                "charging" if charge_state.get("charging_started", True) else "deferred"
            )
            return {
                "mode": "smart_charge",
                "phase": phase,
                "power_w": charge_state.get("last_power_w", 0),
                "max_power_w": charge_state.get("max_power_w"),
                "target_soc": charge_state.get("target_soc"),
                "end_time": charge_state["end"].isoformat(),
                "soc_entity": charge_state.get("soc_entity"),
            }

        discharge_state = domain_data.get("_smart_discharge_state")
        if discharge_state is not None:
            return {
                "mode": "smart_discharge",
                "power_w": discharge_state.get("last_power_w", 0),
                "min_soc": discharge_state.get("min_soc"),
                "end_time": discharge_state["end"].isoformat(),
                "soc_entity": discharge_state.get("soc_entity"),
            }

        return None
