"""Sensors for FoxESS Control inverter override status."""

from __future__ import annotations

import collections
import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_SMART_HEADROOM,
    DEFAULT_SMART_HEADROOM,
    DOMAIN,
)
from .coordinator import FoxESSDataCoordinator, get_coordinator_soc

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

SCAN_INTERVAL = datetime.timedelta(seconds=30)

# If this input_boolean exists and is "on", the integration captures log
# messages into a sensor entity queryable via the HA REST API.
DEBUG_LOG_ENTITY = "input_boolean.foxess_control_debug_log"
_DEBUG_LOG_BUFFER_SIZE = 200

_ICON_CHARGING = "mdi:battery-charging"
_ICON_DEFERRED = "mdi:battery-clock"
_ICON_DISCHARGING = "mdi:battery-arrow-down"
_ICON_FORECAST = "mdi:chart-timeline-variant"
_ICON_IDLE = "mdi:home-battery"
_ICON_POWER = "mdi:flash"
_ICON_CLOCK = "mdi:clock-outline"
_ICON_TIMER = "mdi:timer-sand"
_STATE_UNAVAILABLE = None

# Resolution for forecast data points (5 minutes).
_FORECAST_STEP = datetime.timedelta(minutes=5)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build DeviceInfo so all sensors are grouped under one device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="FoxESS",
        manufacturer="FoxESS",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FoxESS Control sensors."""
    entities: list[SensorEntity] = [
        InverterOverrideStatusSensor(hass, entry),
        SmartOperationsOverviewSensor(hass, entry),
        ChargePowerSensor(hass, entry),
        ChargeWindowSensor(hass, entry),
        ChargeRemainingSensor(hass, entry),
        DischargePowerSensor(hass, entry),
        DischargeWindowSensor(hass, entry),
        DischargeRemainingSensor(hass, entry),
        BatteryForecastSensor(hass, entry),
    ]

    coordinator: FoxESSDataCoordinator | None = (
        hass.data[DOMAIN].get(entry.entry_id, {}).get("coordinator")
    )
    if coordinator is not None:
        entities.extend(
            FoxESSPolledSensor(coordinator, entry, desc)
            for desc in POLLED_SENSOR_DESCRIPTIONS
        )
        entities.append(FoxESSWorkModeSensor(coordinator, entry))

    # Opt-in debug log capture
    result = setup_debug_log(hass, entry)
    if result is not None:
        sensor, handler = result
        entities.append(sensor)
        hass.data[DOMAIN].setdefault("_debug_log_handlers", []).append(handler)

    async_add_entities(entities)


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


def _get_soc_value(hass: Any) -> float | None:
    """Read the current SoC from the coordinator."""
    return get_coordinator_soc(hass)


def _get_battery_capacity_kwh(hass: HomeAssistant) -> float:
    """Read battery capacity from the first config entry's options."""
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return 0.0
    for key in domain_data:
        if not str(key).startswith("_"):
            entry = hass.config_entries.async_get_entry(str(key))
            if entry is not None:
                cap: float = entry.options.get(CONF_BATTERY_CAPACITY_KWH, 0.0)
                return cap
    return 0.0


def _get_smart_headroom_fraction(hass: HomeAssistant) -> float:
    """Read charge headroom from the first config entry's options as a fraction."""
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return DEFAULT_SMART_HEADROOM / 100.0
    for key in domain_data:
        if not str(key).startswith("_"):
            entry = hass.config_entries.async_get_entry(str(key))
            if entry is not None:
                pct: int = entry.options.get(
                    CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM
                )
                return pct / 100.0
    return DEFAULT_SMART_HEADROOM / 100.0


def _deferred_power_fraction(hass: HomeAssistant) -> float:
    """Fraction of max power assumed during deferred start estimation.

    Derived from the configured charge headroom: accounts for both the
    time buffer and the power reservation.
    """
    h = _get_smart_headroom_fraction(hass)
    return (1 - h) * (1 - h)


def _is_effectively_charging(hass: HomeAssistant, cs: dict[str, Any]) -> bool:
    """Return True if the charge session should be considered active.

    The ``charging_started`` flag in the state dict is only flipped by
    the 5-minute callback.  Between the calculated deferred start time
    and the next callback fire, the flag is still False even though
    charging should have started.  This helper bridges the gap by
    recalculating the deferred start time and checking whether it has
    already passed.
    """
    if cs.get("charging_started", True):
        return True
    # charging_started is False — check if deferred time has passed
    soc = _get_soc_value(hass)
    capacity_kwh = _get_battery_capacity_kwh(hass)
    target_soc: int = cs.get("target_soc", 100)
    max_power_w: int = cs.get("max_power_w", 0)
    end: datetime.datetime = cs["end"]
    start: datetime.datetime | None = cs.get("start")
    if soc is not None and capacity_kwh > 0 and max_power_w > 0 and soc < target_soc:
        energy_kwh = (target_soc - soc) / 100.0 * capacity_kwh
        charge_kw = max_power_w * _deferred_power_fraction(hass) / 1000.0
        charge_hours = energy_kwh / charge_kw
        deferred_start = end - datetime.timedelta(hours=charge_hours)
        if start is not None and deferred_start < start:
            deferred_start = start
        now = dt_util.now()
        if end.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        return (deferred_start - now).total_seconds() <= 0
    return False


def _get_actual_discharge_power_w(hass: HomeAssistant, requested_w: int) -> int:
    """Return observed discharge power, falling back to the requested value.

    The inverter may discharge at less than the requested rate due to
    hardware export limits.  When the polled ``batDischargePower`` is
    available and non-zero, return it (converted from kW to W) capped at
    the requested value.  Otherwise return the requested value.
    """
    polled_kw = _get_coordinator_value(hass, "batDischargePower")
    if polled_kw is not None and polled_kw > 0:
        return min(int(polled_kw * 1000), requested_w)
    return requested_w


def _get_coordinator_value(hass: HomeAssistant, key: str) -> float | None:
    """Read a numeric value from the first available coordinator."""
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return None
    for k in domain_data:
        if not str(k).startswith("_"):
            entry_data = domain_data.get(k)
            if isinstance(entry_data, dict):
                coordinator = entry_data.get("coordinator")
                if coordinator is not None and coordinator.data:
                    raw = coordinator.data.get(key)
                    if raw is not None:
                        try:
                            return float(raw)
                        except (ValueError, TypeError):
                            pass
    return None


def _estimate_discharge_remaining(
    hass: HomeAssistant,
    ds: dict[str, Any],
) -> str:
    """Estimate time until discharge ends or energy limit is reached.

    Before the discharge window opens, shows "starts in Xh Ym".
    During the window, shows whichever constraint is closer to being
    hit: time remaining or energy remaining (kWh).  If no energy limit
    is configured, always shows time remaining.
    """
    now = dt_util.now()
    start: datetime.datetime = ds.get("start", now)
    end: datetime.datetime = ds["end"]
    if end.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    if now < start:
        wait = start - now
        return f"starts in {_format_duration(wait)}"

    window_remaining = end - now
    if window_remaining.total_seconds() <= 0:
        return "ending"

    # Check if the energy limit is closer to being hit than the time window
    energy_limit = ds.get("feedin_energy_limit_kwh")
    feedin_start = ds.get("feedin_start_kwh")
    if energy_limit and feedin_start is not None:
        feedin_now = _get_coordinator_value(hass, "feedin")
        if feedin_now is not None:
            energy_used = feedin_now - feedin_start
            energy_remaining = max(0.0, energy_limit - energy_used)
            elapsed = (now - start).total_seconds()
            total_window = (end - start).total_seconds()
            if total_window > 0 and energy_limit > 0:
                time_fraction = elapsed / total_window
                energy_fraction = energy_used / energy_limit
                if energy_fraction > time_fraction:
                    return f"{energy_remaining:.1f} kWh left"

    return _format_duration(window_remaining)


def _format_duration(td: datetime.timedelta) -> str:
    """Format a timedelta as a compact human-readable string."""
    total_minutes = int(td.total_seconds() / 60)
    if total_minutes <= 0:
        return "0m"
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _estimate_charge_remaining(
    hass: HomeAssistant,
    cs: dict[str, Any],
) -> str:
    """Estimate time until charge completes, or starts if deferred.

    - **Deferred**: returns "starts in Xh Ym" — time until charging begins.
    - **Charging**: returns the shorter of window remaining or time to
      reach target_soc at current power.
    - **Fallback**: window remaining when SoC/capacity unavailable.
    """
    now = dt_util.now()
    end: datetime.datetime = cs["end"]
    if end.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    window_remaining = end - now
    if window_remaining.total_seconds() <= 0:
        return "ending"

    if _is_effectively_charging(hass, cs):
        # Actively charging — window remaining is the best estimate.
        return _format_duration(window_remaining)

    # Deferred — estimate when charging will begin
    soc = _get_soc_value(hass)
    capacity_kwh = _get_battery_capacity_kwh(hass)
    target_soc: int = cs.get("target_soc", 100)
    max_power_w: int = cs.get("max_power_w", 0)
    start: datetime.datetime | None = cs.get("start")
    if soc is not None and capacity_kwh > 0 and max_power_w > 0 and soc < target_soc:
        energy_kwh = (target_soc - soc) / 100.0 * capacity_kwh
        charge_kw = max_power_w * _deferred_power_fraction(hass) / 1000.0
        charge_hours = energy_kwh / charge_kw
        deferred_start = end - datetime.timedelta(hours=charge_hours)
        if start is not None and deferred_start < start:
            deferred_start = start
        wait = deferred_start - now
        if wait.total_seconds() <= 0:
            return _format_duration(window_remaining)
        return f"starts in {_format_duration(wait)}"
    return _format_remaining(end)


def _build_forecast(
    hass: HomeAssistant,
    cs: dict[str, Any] | None,
    ds: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build a SoC forecast series for active smart operations.

    Returns a list of ``{"time": epoch_ms, "soc": float}`` dicts
    suitable for ApexCharts ``data_generator``.
    """
    now = dt_util.now()
    capacity_kwh = _get_battery_capacity_kwh(hass)
    raw_points: list[dict[str, Any]] = []

    if cs is not None:
        soc = _get_soc_value(hass)
        if soc is None or capacity_kwh <= 0:
            return []

        end: datetime.datetime = cs["end"]
        session_start: datetime.datetime = cs.get("start", now)
        target_soc: int = cs.get("target_soc", 100)
        max_power_w: int = cs.get("max_power_w", 0)
        power_w: int = cs.get("last_power_w", 0)
        effectively_charging: bool = _is_effectively_charging(hass, cs)

        # Rate of SoC change per second while charging
        charge_rate = 0.0
        if power_w > 0 and capacity_kwh > 0:
            charge_rate = (power_w / 1000.0) / capacity_kwh * 100.0 / 3600.0

        # Deferred: compute when charging will start
        deferred_start = now
        if not effectively_charging and max_power_w > 0:
            energy_kwh = (target_soc - soc) / 100.0 * capacity_kwh
            dpf = _deferred_power_fraction(hass)
            charge_kw = max_power_w * dpf / 1000.0
            if charge_kw > 0:
                charge_hours = energy_kwh / charge_kw
                deferred_start = end - datetime.timedelta(hours=charge_hours)
                if deferred_start < now:
                    deferred_start = now
            # Use planned power rate for projection after deferred start
            if capacity_kwh > 0:
                planned_power = max_power_w * dpf
                charge_rate = (planned_power / 1000.0) / capacity_kwh * 100.0 / 3600.0

        # Anchor at session start (or now if earlier) so the time axis is
        # stable once charging begins and the "now" marker progresses smoothly.
        t = min(session_start, now)
        cur_soc = soc
        while t <= end:
            epoch_ms = int(t.timestamp() * 1000)
            if t <= now:
                # Historical/current: hold at current SoC
                raw_points.append({"time": epoch_ms, "soc": round(soc, 1)})
                t += _FORECAST_STEP
                continue
            if not effectively_charging and t < deferred_start:
                # Still waiting — SoC stays flat
                raw_points.append({"time": epoch_ms, "soc": round(cur_soc, 1)})
                t += _FORECAST_STEP
                continue
            raw_points.append({"time": epoch_ms, "soc": round(cur_soc, 1)})
            t += _FORECAST_STEP
            step_secs = _FORECAST_STEP.total_seconds()
            cur_soc = min(cur_soc + charge_rate * step_secs, target_soc)

        # Final point at end
        if raw_points and raw_points[-1]["time"] < int(end.timestamp() * 1000):
            raw_points.append(
                {
                    "time": int(end.timestamp() * 1000),
                    "soc": round(cur_soc, 1),
                }
            )
        return _deduplicate_forecast(raw_points)

    if ds is not None:
        soc = _get_soc_value(hass)
        if soc is None or capacity_kwh <= 0:
            return []

        end = ds["end"]
        discharge_start: datetime.datetime = ds.get("start", now)
        min_soc: int = ds.get("min_soc", 0)
        power_w = ds.get("last_power_w", 0)

        discharge_rate = 0.0
        if power_w > 0 and capacity_kwh > 0:
            discharge_rate = (power_w / 1000.0) / capacity_kwh * 100.0 / 3600.0

        # Energy limit tracking — the limit constrains *grid export*, not
        # total battery discharge, so we cap the projected SoC drop to the
        # energy-limit equivalent in SoC points.  This is simpler and more
        # accurate than step-wise export-rate tracking (which would need
        # net consumption data the forecast doesn't have).
        energy_limit = ds.get("feedin_energy_limit_kwh")
        energy_used = 0.0
        if energy_limit is not None:
            feedin_start = ds.get("feedin_start_kwh")
            feedin_now = _get_coordinator_value(hass, "feedin")
            if feedin_start is not None and feedin_now is not None:
                energy_used = feedin_now - feedin_start
        energy_remaining_kwh = (
            energy_limit - energy_used if energy_limit is not None else None
        )
        # Floor SoC: don't project below min_soc or below what the
        # feed-in energy budget allows.
        soc_floor = float(min_soc)
        if energy_remaining_kwh is not None and capacity_kwh > 0:
            max_soc_drop = energy_remaining_kwh / capacity_kwh * 100.0
            soc_floor = max(soc_floor, soc - max_soc_drop)

        # Anchor at session start (or now if earlier) so the time axis is
        # stable once discharging begins and the "now" marker progresses.
        t = min(discharge_start, now)
        cur_soc = soc
        while t <= end:
            epoch_ms = int(t.timestamp() * 1000)
            if t <= now or t < discharge_start:
                # Before now or before discharge starts — hold at current SoC
                raw_points.append({"time": epoch_ms, "soc": round(soc, 1)})
                t += _FORECAST_STEP
                continue
            raw_points.append({"time": epoch_ms, "soc": round(cur_soc, 1)})
            t += _FORECAST_STEP
            step_secs = _FORECAST_STEP.total_seconds()
            cur_soc = max(cur_soc - discharge_rate * step_secs, soc_floor)

        if raw_points and raw_points[-1]["time"] < int(end.timestamp() * 1000):
            raw_points.append(
                {
                    "time": int(end.timestamp() * 1000),
                    "soc": round(cur_soc, 1),
                }
            )
        return _deduplicate_forecast(raw_points)

    return []


def _deduplicate_forecast(
    points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove intermediate points where SoC hasn't changed.

    Always keeps the first point, last point, and any point where the
    SoC differs from the previous point. Also keeps the last point
    before a change to preserve transition timing.
    """
    if len(points) <= 2:
        return points

    result: list[dict[str, Any]] = [points[0]]
    for i in range(1, len(points) - 1):
        prev_soc = points[i - 1]["soc"]
        cur_soc = points[i]["soc"]
        next_soc = points[i + 1]["soc"]
        # Keep if value changed from previous, or next will change from current
        if cur_soc != prev_soc or next_soc != cur_soc:
            result.append(points[i])
    result.append(points[-1])
    return result


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

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_override_status"
        self._attr_name = "Status"
        self._attr_device_info = _device_info(entry)
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
            if not _is_effectively_charging(self.hass, cs):
                return f"Wait→{target}%"
            power_w = cs.get("last_power_w", 0) or cs.get("max_power_w", 0)
            power = _format_power(power_w)
            return f"Chg {power}→{target}%"

        ds = _get_discharge_state(self.hass)
        if ds is not None:
            now = dt_util.now()
            start = ds.get("start", now)
            if start.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            if now < start:
                return f"Dchg@{_format_time(start)}"
            power = _format_power(
                _get_actual_discharge_power_w(self.hass, ds.get("last_power_w", 0))
            )
            feedin_limit = ds.get("feedin_energy_limit_kwh")
            if feedin_limit is not None:
                return f"Dchg {power} {feedin_limit}kWh"
            end = ds.get("end")
            if end is not None:
                return f"Dchg {power}→{_format_time(end)}"
            min_soc = ds.get("min_soc", "?")
            return f"Dchg {power}→{min_soc}%"

        return "Idle"

    @property
    def icon(self) -> str:
        """Return an icon based on the current override state."""
        cs = _get_charge_state(self.hass)
        if cs is not None:
            if not _is_effectively_charging(self.hass, cs):
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
            phase = (
                "charging" if _is_effectively_charging(self.hass, cs) else "deferred"
            )
            return {
                "mode": "smart_charge",
                "phase": phase,
                "power_w": cs.get("last_power_w", 0),
                "max_power_w": cs.get("max_power_w"),
                "target_soc": cs.get("target_soc"),
                "end_time": cs["end"].isoformat(),
            }

        ds = _get_discharge_state(self.hass)
        if ds is not None:
            return {
                "mode": "smart_discharge",
                "power_w": ds.get("last_power_w", 0),
                "min_soc": ds.get("min_soc"),
                "end_time": ds["end"].isoformat(),
            }

        return None


# ---------------------------------------------------------------------------
# Dashboard overview sensor — descriptive state + rich attributes
# ---------------------------------------------------------------------------


class SmartOperationsOverviewSensor(SensorEntity):
    """Dashboard sensor providing a rich overview of smart operations."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_smart_operations"
        self._attr_name = "Smart Operations"
        self._attr_device_info = _device_info(entry)
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
            if cs.get("target_reached"):
                return f"Charged to {target}% (monitoring)"
            if not _is_effectively_charging(self.hass, cs):
                return f"Deferred charge to {target}%"
            return f"Charging to {target}%"

        if ds is not None:
            now = dt_util.now()
            start = ds.get("start", now)
            if start.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            if now < start:
                return f"Discharge scheduled at {_format_time(start)}"
            if ds.get("suspended"):
                return "Discharge suspended (high consumption)"
            feedin_limit = ds.get("feedin_energy_limit_kwh")
            if feedin_limit is not None:
                return f"Discharging {feedin_limit} kWh feed-in"
            end = ds.get("end")
            if end is not None:
                return f"Discharging until {_format_time(end)}"
            min_soc = ds.get("min_soc", "?")
            return f"Discharging to {min_soc}%"

        return "Idle"

    @property
    def icon(self) -> str:
        """Return an icon based on the current state."""
        cs = _get_charge_state(self.hass)
        if cs is not None:
            if not _is_effectively_charging(self.hass, cs):
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
            charging = _is_effectively_charging(self.hass, cs)
            soc = _get_soc_value(self.hass)
            attrs.update(
                {
                    "charge_phase": "charging" if charging else "deferred",
                    "charge_power_w": (
                        cs.get("last_power_w", 0)
                        or (cs.get("max_power_w", 0) if charging else 0)
                    ),
                    "charge_max_power_w": cs.get("max_power_w"),
                    "charge_target_soc": cs.get("target_soc"),
                    "charge_current_soc": soc,
                    "charge_window": (
                        f"{_format_time(cs['start'])} – {_format_time(cs['end'])}"
                    ),
                    "charge_remaining": _estimate_charge_remaining(self.hass, cs),
                    "charge_end_time": cs["end"].isoformat(),
                }
            )

        if ds is not None:
            soc = _get_soc_value(self.hass)
            now = dt_util.now()
            ds_start = ds.get("start", now)
            if ds_start.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            requested = ds.get("last_power_w", 0)
            ds_power = (
                0
                if now < ds_start
                else _get_actual_discharge_power_w(self.hass, requested)
            )
            attrs.update(
                {
                    "discharge_power_w": ds_power,
                    "discharge_min_soc": ds.get("min_soc"),
                    "discharge_current_soc": soc,
                    "discharge_window": (
                        f"{_format_time(ds['start'])} – {_format_time(ds['end'])}"
                    ),
                    "discharge_remaining": _estimate_discharge_remaining(self.hass, ds),
                    "discharge_end_time": ds["end"].isoformat(),
                }
            )

        return attrs


# ---------------------------------------------------------------------------
# Individual dashboard sensors — one per metric, no templates needed
# ---------------------------------------------------------------------------


class ChargePowerSensor(SensorEntity):
    """Current smart charge power."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = _ICON_POWER
    _attr_native_unit_of_measurement = "W"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charge_power"
        self._attr_name = "Charge Power"
        self._attr_device_info = _device_info(entry)
        self.hass = hass

    @property
    def native_value(self) -> int | None:
        cs = _get_charge_state(self.hass)
        if cs is None:
            return _STATE_UNAVAILABLE
        power: int = cs.get("last_power_w", 0)
        if power == 0 and _is_effectively_charging(self.hass, cs):
            power = cs.get("max_power_w", 0)
        return power

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        cs = _get_charge_state(self.hass)
        if cs is None:
            return None
        return {
            "target_soc": cs.get("target_soc"),
            "max_power_w": cs.get("max_power_w"),
            "phase": (
                "charging" if _is_effectively_charging(self.hass, cs) else "deferred"
            ),
        }


class ChargeWindowSensor(SensorEntity):
    """Smart charge time window."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = _ICON_CLOCK

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charge_window"
        self._attr_name = "Charge Window"
        self._attr_device_info = _device_info(entry)
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        cs = _get_charge_state(self.hass)
        if cs is None:
            return _STATE_UNAVAILABLE
        return f"{_format_time(cs['start'])} – {_format_time(cs['end'])}"


class ChargeRemainingSensor(SensorEntity):
    """Time remaining in the smart charge window."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = _ICON_TIMER

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_charge_remaining"
        self._attr_name = "Charge Remaining"
        self._attr_device_info = _device_info(entry)
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        cs = _get_charge_state(self.hass)
        if cs is None:
            return _STATE_UNAVAILABLE
        return _estimate_charge_remaining(self.hass, cs)


class DischargePowerSensor(SensorEntity):
    """Current smart discharge power."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = _ICON_POWER
    _attr_native_unit_of_measurement = "W"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_discharge_power"
        self._attr_name = "Discharge Power"
        self._attr_device_info = _device_info(entry)
        self.hass = hass

    @property
    def native_value(self) -> int | None:
        ds = _get_discharge_state(self.hass)
        if ds is None:
            return _STATE_UNAVAILABLE
        now = dt_util.now()
        start = ds.get("start", now)
        if start.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        if now < start:
            return 0
        return _get_actual_discharge_power_w(self.hass, ds.get("last_power_w", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        ds = _get_discharge_state(self.hass)
        if ds is None:
            return None
        return {"min_soc": ds.get("min_soc")}


class DischargeWindowSensor(SensorEntity):
    """Smart discharge time window."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = _ICON_CLOCK

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_discharge_window"
        self._attr_name = "Discharge Window"
        self._attr_device_info = _device_info(entry)
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        ds = _get_discharge_state(self.hass)
        if ds is None:
            return _STATE_UNAVAILABLE
        return f"{_format_time(ds['start'])} – {_format_time(ds['end'])}"


class DischargeRemainingSensor(SensorEntity):
    """Time remaining in the smart discharge window."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = _ICON_TIMER

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_discharge_remaining"
        self._attr_name = "Discharge Remaining"
        self._attr_device_info = _device_info(entry)
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        ds = _get_discharge_state(self.hass)
        if ds is None:
            return _STATE_UNAVAILABLE
        return _estimate_discharge_remaining(self.hass, ds)


class BatteryForecastSensor(SensorEntity):
    """Projected battery SoC over time for ApexCharts display.

    Provides a ``forecast`` attribute containing a list of
    ``{"time": epoch_ms, "soc": float}`` points that ApexCharts
    can consume via ``data_generator``::

        data_generator: |
          return entity.attributes.forecast.map(
            p => [p.time, p.soc]
          );
    """

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = _ICON_FORECAST
    _attr_native_unit_of_measurement = "%"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_battery_forecast"
        self._attr_name = "Battery Forecast"
        self._attr_device_info = _device_info(entry)
        self.hass = hass

    @property
    def native_value(self) -> float | None:
        cs = _get_charge_state(self.hass)
        ds = _get_discharge_state(self.hass)
        if cs is None and ds is None:
            return _STATE_UNAVAILABLE
        return _get_soc_value(self.hass)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cs = _get_charge_state(self.hass)
        ds = _get_discharge_state(self.hass)
        if cs is None and ds is None:
            return {"forecast": []}
        forecast = _build_forecast(self.hass, cs, ds)
        return {"forecast": forecast}


# ---------------------------------------------------------------------------
# Coordinator-backed sensors — polled from the FoxESS Cloud API
# ---------------------------------------------------------------------------


class _PolledSensorDescription:
    """Descriptor for a coordinator-backed sensor."""

    __slots__ = (
        "variable",
        "name",
        "unique_id_suffix",
        "device_class",
        "unit",
        "state_class",
        "icon",
    )

    def __init__(
        self,
        variable: str,
        name: str,
        unique_id_suffix: str,
        device_class: SensorDeviceClass | None,
        unit: str,
        state_class: SensorStateClass,
        icon: str,
    ) -> None:
        self.variable = variable
        self.name = name
        self.unique_id_suffix = unique_id_suffix
        self.device_class = device_class
        self.unit = unit
        self.state_class = state_class
        self.icon = icon


POLLED_SENSOR_DESCRIPTIONS: list[_PolledSensorDescription] = [
    _PolledSensorDescription(
        "SoC",
        "Battery SoC",
        "battery_soc",
        SensorDeviceClass.BATTERY,
        "%",
        SensorStateClass.MEASUREMENT,
        "mdi:battery",
    ),
    _PolledSensorDescription(
        "batChargePower",
        "Charge Rate",
        "bat_charge_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:battery-charging",
    ),
    _PolledSensorDescription(
        "batDischargePower",
        "Discharge Rate",
        "bat_discharge_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:battery-arrow-down",
    ),
    _PolledSensorDescription(
        "loadsPower",
        "House Load",
        "loads_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:home-lightning-bolt",
    ),
    _PolledSensorDescription(
        "pvPower",
        "Solar Power",
        "pv_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-power",
    ),
    _PolledSensorDescription(
        "batTemperature",
        "Battery Temperature",
        "bat_temperature",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer",
    ),
    _PolledSensorDescription(
        "gridConsumptionPower",
        "Grid Consumption",
        "grid_consumption",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:transmission-tower-import",
    ),
    _PolledSensorDescription(
        "feedinPower",
        "Grid Feed-in",
        "feedin_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:transmission-tower-export",
    ),
    _PolledSensorDescription(
        "generationPower",
        "Generation",
        "generation_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-power-variant",
    ),
    _PolledSensorDescription(
        "batVolt",
        "Battery Voltage",
        "bat_volt",
        SensorDeviceClass.VOLTAGE,
        "V",
        SensorStateClass.MEASUREMENT,
        "mdi:flash-triangle",
    ),
    _PolledSensorDescription(
        "batCurrent",
        "Battery Current",
        "bat_current",
        SensorDeviceClass.CURRENT,
        "A",
        SensorStateClass.MEASUREMENT,
        "mdi:current-dc",
    ),
    _PolledSensorDescription(
        "pv1Power",
        "PV1 Power",
        "pv1_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-panel",
    ),
    _PolledSensorDescription(
        "pv2Power",
        "PV2 Power",
        "pv2_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-panel",
    ),
    _PolledSensorDescription(
        "ambientTemperation",
        "Ambient Temperature",
        "ambient_temp",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer",
    ),
    _PolledSensorDescription(
        "invTemperation",
        "Inverter Temperature",
        "inverter_temp",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer-alert",
    ),
    # Cumulative energy counters (lifetime kWh)
    _PolledSensorDescription(
        "feedin",
        "Grid Feed-in Energy",
        "feedin_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:transmission-tower-export",
    ),
    _PolledSensorDescription(
        "gridConsumption",
        "Grid Consumption Energy",
        "grid_consumption_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:transmission-tower-import",
    ),
    _PolledSensorDescription(
        "generation",
        "Solar Generation Energy",
        "generation_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:solar-power-variant",
    ),
    _PolledSensorDescription(
        "chargeEnergyToTal",
        "Battery Charge Energy",
        "charge_energy_total",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-charging",
    ),
    _PolledSensorDescription(
        "dischargeEnergyToTal",
        "Battery Discharge Energy",
        "discharge_energy_total",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-arrow-down",
    ),
    _PolledSensorDescription(
        "loads",
        "House Load Energy",
        "loads_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:home-lightning-bolt",
    ),
    _PolledSensorDescription(
        "energyThroughput",
        "Battery Throughput",
        "energy_throughput",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-sync",
    ),
    # Grid connection
    _PolledSensorDescription(
        "meterPower",
        "Grid Meter Power",
        "meter_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:meter-electric",
    ),
    _PolledSensorDescription(
        "RVolt",
        "Grid Voltage",
        "grid_voltage",
        SensorDeviceClass.VOLTAGE,
        "V",
        SensorStateClass.MEASUREMENT,
        "mdi:flash-triangle",
    ),
    _PolledSensorDescription(
        "RCurrent",
        "Grid Current",
        "grid_current",
        SensorDeviceClass.CURRENT,
        "A",
        SensorStateClass.MEASUREMENT,
        "mdi:current-ac",
    ),
    _PolledSensorDescription(
        "RFreq",
        "Grid Frequency",
        "grid_frequency",
        None,
        "Hz",
        SensorStateClass.MEASUREMENT,
        "mdi:sine-wave",
    ),
    # EPS / backup
    _PolledSensorDescription(
        "epsPower",
        "EPS Power",
        "eps_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:power-plug-battery",
    ),
]


class FoxESSPolledSensor(CoordinatorEntity[FoxESSDataCoordinator], SensorEntity):
    """Sensor backed by the DataUpdateCoordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FoxESSDataCoordinator,
        entry: ConfigEntry,
        desc: _PolledSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._variable = desc.variable
        self._attr_unique_id = f"{entry.entry_id}_{desc.unique_id_suffix}"
        self._attr_name = desc.name
        self._attr_device_class = desc.device_class
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_state_class = desc.state_class
        self._attr_icon = desc.icon
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._variable)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None


class FoxESSWorkModeSensor(CoordinatorEntity[FoxESSDataCoordinator], SensorEntity):
    """Sensor showing the inverter's current work mode."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:state-machine"

    def __init__(
        self,
        coordinator: FoxESSDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_work_mode"
        self._attr_name = "Work Mode"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("_work_mode")


# ---------------------------------------------------------------------------
# Debug log capture — opt-in via input_boolean.foxess_control_debug_log
# ---------------------------------------------------------------------------


class _DebugLogHandler(logging.Handler):
    """Logging handler that captures records into a bounded deque."""

    def __init__(self, buffer: collections.deque[dict[str, str]]) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(
                {
                    "t": datetime.datetime.fromtimestamp(
                        record.created, tz=datetime.UTC
                    ).isoformat(timespec="seconds"),
                    "level": record.levelname,
                    "msg": self.format(record),
                }
            )
        except Exception:  # noqa: BLE001
            self.handleError(record)


class DebugLogSensor(SensorEntity):
    """Sensor exposing recent foxess_control log entries as attributes.

    Created only when ``input_boolean.foxess_control_debug_log`` exists
    and is on.  The ``state`` is the number of buffered entries; the full
    log ring is in the ``entries`` attribute, readable via the REST API::

        GET /api/states/sensor.foxess_control_debug_log
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:math-log"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        entry: ConfigEntry,
        buffer: collections.deque[dict[str, str]],
    ) -> None:
        self._buffer = buffer
        self._attr_unique_id = f"{entry.entry_id}_debug_log"
        self._attr_name = "Debug Log"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self._buffer)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entries": list(self._buffer)}


def setup_debug_log(
    hass: Any,
    entry: ConfigEntry,
) -> tuple[DebugLogSensor, _DebugLogHandler] | None:
    """Attach a log handler and return a sensor if debug logging is opted-in.

    Returns ``None`` when the opt-in entity does not exist or is off.
    The caller is responsible for adding the sensor entity and storing
    the handler reference for cleanup on unload.
    """
    state = hass.states.get(DEBUG_LOG_ENTITY)
    if state is None or state.state != "on":
        return None

    buf: collections.deque[dict[str, str]] = collections.deque(
        maxlen=_DEBUG_LOG_BUFFER_SIZE
    )
    handler = _DebugLogHandler(buf)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    # Capture all messages from the integration's top-level package.
    logger = logging.getLogger("custom_components.foxess_control")
    # Save the original level so unload can restore it.
    handler._original_level = logger.level  # type: ignore[attr-defined]
    logger.addHandler(handler)
    # Ensure messages reach the handler even if HA hasn't set the level.
    # getEffectiveLevel() walks up to the root logger; we need DEBUG locally.
    if logger.getEffectiveLevel() > logging.DEBUG:
        logger.setLevel(logging.DEBUG)

    sensor = DebugLogSensor(entry, buf)
    return sensor, handler
