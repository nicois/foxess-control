"""Sensors for FoxESS Control inverter override status."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

SCAN_INTERVAL = datetime.timedelta(seconds=30)

_ICON_CHARGING = "mdi:battery-charging"
_ICON_DEFERRED = "mdi:battery-clock"
_ICON_DISCHARGING = "mdi:battery-arrow-down"
_ICON_IDLE = "mdi:home-battery"
_ICON_POWER = "mdi:flash"
_ICON_CLOCK = "mdi:clock-outline"
_ICON_TIMER = "mdi:timer-sand"
_STATE_UNAVAILABLE = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FoxESS Control sensors."""
    async_add_entities(
        [
            InverterOverrideStatusSensor(hass, entry),
            SmartOperationsOverviewSensor(hass, entry),
            ChargePowerSensor(hass, entry),
            ChargeWindowSensor(hass, entry),
            ChargeRemainingSensor(hass, entry),
            DischargePowerSensor(hass, entry),
            DischargeWindowSensor(hass, entry),
            DischargeRemainingSensor(hass, entry),
        ]
    )


def _format_power(watts: int) -> str:
    """Format watts as a compact string."""
    if watts >= 1000:
        kw = watts / 1000
        # Drop the decimal if it's a whole number (e.g. "6kW" not "6.0kW")
        if kw == int(kw):
            return f"{int(kw)}kW"
        return f"{kw:.1f}kW"
    return f"{watts}W"


def _format_time(dt: datetime.datetime) -> str:
    """Format a datetime as HH:MM."""
    return f"{dt.hour:02d}:{dt.minute:02d}"


def _format_remaining(end: datetime.datetime) -> str:
    """Format the time remaining until *end* as a human-readable string."""
    now = dt_util.now()
    # Ensure both are naive or both are aware for subtraction.
    if end.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    remaining = end - now
    if remaining.total_seconds() <= 0:
        return "ending"
    total_minutes = int(remaining.total_seconds() / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _get_soc_value(hass: Any, soc_entity: str) -> float | None:
    """Read the current SoC from a HA entity, or None if unavailable."""
    soc_state = hass.states.get(soc_entity)
    if soc_state is None or soc_state.state in ("unknown", "unavailable"):
        return None
    try:
        return float(soc_state.state)
    except (ValueError, TypeError):
        return None


def _get_charge_state(hass: HomeAssistant) -> dict[str, Any] | None:
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return None
    result: dict[str, Any] | None = domain_data.get("_smart_charge_state")
    return result


def _get_discharge_state(hass: HomeAssistant) -> dict[str, Any] | None:
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return None
    result: dict[str, Any] | None = domain_data.get("_smart_discharge_state")
    return result


# ---------------------------------------------------------------------------
# Android Auto sensor — short state strings for small displays
# ---------------------------------------------------------------------------


class InverterOverrideStatusSensor(SensorEntity):
    """Compact status for Android Auto: icon + short text."""

    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_override_status"
        self._attr_name = "FoxESS Status"
        self.hass = hass

    @property
    def native_value(self) -> str:
        """Return a short status string.

        Kept under ~15 characters so Android Auto doesn't truncate.
        Examples: "Chg 6kW→80%", "Wait→80%", "Dchg 5kW", "Idle"
        """
        cs = _get_charge_state(self.hass)
        if cs is not None:
            target = cs.get("target_soc", "?")
            if not cs.get("charging_started", True):
                return f"Wait→{target}%"
            power = _format_power(cs.get("last_power_w", 0))
            return f"Chg {power}→{target}%"

        ds = _get_discharge_state(self.hass)
        if ds is not None:
            power = _format_power(ds.get("last_power_w", 0))
            min_soc = ds.get("min_soc", "?")
            return f"Dchg {power}→{min_soc}%"

        return "Idle"

    @property
    def icon(self) -> str:
        """Return an icon based on the current override state."""
        cs = _get_charge_state(self.hass)
        if cs is not None:
            if not cs.get("charging_started", True):
                return _ICON_DEFERRED
            return _ICON_CHARGING

        if _get_discharge_state(self.hass) is not None:
            return _ICON_DISCHARGING

        return _ICON_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return session details as attributes."""
        cs = _get_charge_state(self.hass)
        if cs is not None:
            phase = "charging" if cs.get("charging_started", True) else "deferred"
            return {
                "mode": "smart_charge",
                "phase": phase,
                "power_w": cs.get("last_power_w", 0),
                "max_power_w": cs.get("max_power_w"),
                "target_soc": cs.get("target_soc"),
                "end_time": cs["end"].isoformat(),
                "soc_entity": cs.get("soc_entity"),
            }

        ds = _get_discharge_state(self.hass)
        if ds is not None:
            return {
                "mode": "smart_discharge",
                "power_w": ds.get("last_power_w", 0),
                "min_soc": ds.get("min_soc"),
                "end_time": ds["end"].isoformat(),
                "soc_entity": ds.get("soc_entity"),
            }

        return None


# ---------------------------------------------------------------------------
# Dashboard overview sensor — descriptive state + rich attributes
# ---------------------------------------------------------------------------


class SmartOperationsOverviewSensor(SensorEntity):
    """Dashboard sensor providing a rich overview of smart operations."""

    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_smart_operations"
        self._attr_name = "FoxESS Smart Operations"
        self.hass = hass

    @property
    def native_value(self) -> str:
        """Return a concise status line."""
        cs = _get_charge_state(self.hass)
        ds = _get_discharge_state(self.hass)

        if cs is not None and ds is not None:
            return "Charge + Discharge active"

        if cs is not None:
            target = cs.get("target_soc", "?")
            if not cs.get("charging_started", True):
                return f"Deferred charge to {target}%"
            return f"Charging to {target}%"

        if ds is not None:
            min_soc = ds.get("min_soc", "?")
            return f"Discharging to {min_soc}%"

        return "Idle"

    @property
    def icon(self) -> str:
        """Return an icon based on the current state."""
        cs = _get_charge_state(self.hass)
        if cs is not None:
            if not cs.get("charging_started", True):
                return _ICON_DEFERRED
            return _ICON_CHARGING
        if _get_discharge_state(self.hass) is not None:
            return _ICON_DISCHARGING
        return _ICON_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return comprehensive attributes for dashboard templating."""
        cs = _get_charge_state(self.hass)
        ds = _get_discharge_state(self.hass)

        attrs: dict[str, Any] = {
            "charge_active": cs is not None,
            "discharge_active": ds is not None,
        }

        if cs is not None:
            charging = cs.get("charging_started", True)
            soc = _get_soc_value(self.hass, cs.get("soc_entity", ""))
            attrs.update(
                {
                    "charge_phase": "charging" if charging else "deferred",
                    "charge_power_w": cs.get("last_power_w", 0),
                    "charge_max_power_w": cs.get("max_power_w"),
                    "charge_target_soc": cs.get("target_soc"),
                    "charge_current_soc": soc,
                    "charge_window": (
                        f"{_format_time(cs['start'])} – {_format_time(cs['end'])}"
                    ),
                    "charge_remaining": _format_remaining(cs["end"]),
                    "charge_end_time": cs["end"].isoformat(),
                }
            )

        if ds is not None:
            soc = _get_soc_value(self.hass, ds.get("soc_entity", ""))
            attrs.update(
                {
                    "discharge_power_w": ds.get("last_power_w", 0),
                    "discharge_min_soc": ds.get("min_soc"),
                    "discharge_current_soc": soc,
                    "discharge_window": (
                        f"{_format_time(ds['start'])} – {_format_time(ds['end'])}"
                    ),
                    "discharge_remaining": _format_remaining(ds["end"]),
                    "discharge_end_time": ds["end"].isoformat(),
                }
            )

        return attrs


# ---------------------------------------------------------------------------
# Individual dashboard sensors — one per metric, no templates needed
# ---------------------------------------------------------------------------


class ChargePowerSensor(SensorEntity):
    """Current smart charge power."""

    _attr_should_poll = True
    _attr_icon = _ICON_POWER
    _attr_native_unit_of_measurement = "W"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charge_power"
        self._attr_name = "FoxESS Charge Power"
        self.hass = hass

    @property
    def native_value(self) -> int | None:
        cs = _get_charge_state(self.hass)
        if cs is None:
            return _STATE_UNAVAILABLE
        power: int = cs.get("last_power_w", 0)
        return power

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        cs = _get_charge_state(self.hass)
        if cs is None:
            return None
        return {
            "target_soc": cs.get("target_soc"),
            "max_power_w": cs.get("max_power_w"),
            "phase": ("charging" if cs.get("charging_started", True) else "deferred"),
        }


class ChargeWindowSensor(SensorEntity):
    """Smart charge time window."""

    _attr_should_poll = True
    _attr_icon = _ICON_CLOCK

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charge_window"
        self._attr_name = "FoxESS Charge Window"
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        cs = _get_charge_state(self.hass)
        if cs is None:
            return _STATE_UNAVAILABLE
        return f"{_format_time(cs['start'])} – {_format_time(cs['end'])}"


class ChargeRemainingSensor(SensorEntity):
    """Time remaining in the smart charge window."""

    _attr_should_poll = True
    _attr_icon = _ICON_TIMER

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charge_remaining"
        self._attr_name = "FoxESS Charge Remaining"
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        cs = _get_charge_state(self.hass)
        if cs is None:
            return _STATE_UNAVAILABLE
        return _format_remaining(cs["end"])


class DischargePowerSensor(SensorEntity):
    """Current smart discharge power."""

    _attr_should_poll = True
    _attr_icon = _ICON_POWER
    _attr_native_unit_of_measurement = "W"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_discharge_power"
        self._attr_name = "FoxESS Discharge Power"
        self.hass = hass

    @property
    def native_value(self) -> int | None:
        ds = _get_discharge_state(self.hass)
        if ds is None:
            return _STATE_UNAVAILABLE
        power: int = ds.get("last_power_w", 0)
        return power

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        ds = _get_discharge_state(self.hass)
        if ds is None:
            return None
        return {"min_soc": ds.get("min_soc")}


class DischargeWindowSensor(SensorEntity):
    """Smart discharge time window."""

    _attr_should_poll = True
    _attr_icon = _ICON_CLOCK

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_discharge_window"
        self._attr_name = "FoxESS Discharge Window"
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        ds = _get_discharge_state(self.hass)
        if ds is None:
            return _STATE_UNAVAILABLE
        return f"{_format_time(ds['start'])} – {_format_time(ds['end'])}"


class DischargeRemainingSensor(SensorEntity):
    """Time remaining in the smart discharge window."""

    _attr_should_poll = True
    _attr_icon = _ICON_TIMER

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_discharge_remaining"
        self._attr_name = "FoxESS Discharge Remaining"
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        ds = _get_discharge_state(self.hass)
        if ds is None:
            return _STATE_UNAVAILABLE
        return _format_remaining(ds["end"])
