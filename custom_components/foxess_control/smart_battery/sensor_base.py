"""Shared sensor base classes for smart battery integrations.

All sensor classes are parameterized by ``domain`` and ``device_info``
so brand integrations create thin subclasses that bind these values.
"""

from __future__ import annotations

import contextlib
import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_GRID_EXPORT_LIMIT,
    CONF_SMART_HEADROOM,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_SMART_HEADROOM,
)
from .coordinator import get_coordinator_soc
from .domain_data import get_domain_data

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo

    from .taper import TaperProfile

# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

ICON_CHARGING = "mdi:battery-charging"
ICON_DEFERRED = "mdi:battery-clock"
ICON_DISCHARGING = "mdi:battery-arrow-down"
ICON_FORECAST = "mdi:chart-timeline-variant"
ICON_IDLE = "mdi:home-battery"
ICON_POWER = "mdi:flash"
ICON_CLOCK = "mdi:clock-outline"
ICON_TIMER = "mdi:timer-sand"

_STATE_UNAVAILABLE = None
_FORECAST_STEP = datetime.timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Domain-aware data access helpers
# ---------------------------------------------------------------------------


def get_charge_state(hass: HomeAssistant, domain: str) -> dict[str, Any] | None:
    """Read the smart charge state from hass.data."""
    return get_domain_data(hass, domain).smart_charge_state


def get_discharge_state(hass: HomeAssistant, domain: str) -> dict[str, Any] | None:
    """Read the smart discharge state from hass.data."""
    return get_domain_data(hass, domain).smart_discharge_state


def get_soc_value(hass: HomeAssistant, domain: str) -> float | None:
    """Read the current SoC from the coordinator."""
    return get_coordinator_soc(hass, domain)


def get_interpolated_soc(hass: HomeAssistant, domain: str) -> float | None:
    """Read the interpolated SoC if available, falling back to raw SoC.

    The coordinator integrates battery power between integer SoC ticks
    to provide a sub-percent estimate for smoother progress display.
    """
    from .domain_data import get_first_coordinator

    coordinator = get_first_coordinator(hass, domain)
    if coordinator is not None and coordinator.data is not None:
        interp = coordinator.data.get("_soc_interpolated")
        if interp is not None:
            return float(interp)
    return get_coordinator_soc(hass, domain)


def get_battery_capacity_kwh(hass: HomeAssistant, domain: str) -> float:
    """Read battery capacity from the first config entry's options."""
    from .domain_data import get_first_entry_id

    eid = get_first_entry_id(hass, domain)
    if eid is not None:
        entry = hass.config_entries.async_get_entry(eid)
        if entry is not None:
            cap: float = entry.options.get(CONF_BATTERY_CAPACITY_KWH, 0.0)
            return cap
    return 0.0


def get_smart_headroom_fraction(hass: HomeAssistant, domain: str) -> float:
    """Read charge headroom from the first config entry's options as a fraction."""
    from .domain_data import get_first_entry_id

    eid = get_first_entry_id(hass, domain)
    if eid is not None:
        entry = hass.config_entries.async_get_entry(eid)
        if entry is not None:
            pct: int = entry.options.get(CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM)
            return pct / 100.0
    return DEFAULT_SMART_HEADROOM / 100.0


def _get_grid_export_limit(hass: HomeAssistant, domain: str) -> int:
    """Read grid export limit from the first config entry's options (watts, 0=none)."""
    from .domain_data import get_first_entry_id

    eid = get_first_entry_id(hass, domain)
    if eid is not None:
        entry = hass.config_entries.async_get_entry(eid)
        if entry is not None:
            return int(
                entry.options.get(CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT)
            )
    return DEFAULT_GRID_EXPORT_LIMIT


def get_coordinator_value(hass: HomeAssistant, domain: str, key: str) -> float | None:
    """Read a numeric value from the first available coordinator."""
    from .domain_data import get_first_coordinator

    coordinator = get_first_coordinator(hass, domain)
    if coordinator is not None and coordinator.data:
        raw = coordinator.data.get(key)
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
    return None


def _get_net_consumption(hass: HomeAssistant, domain: str) -> float:
    """Return net site consumption (loads minus solar) in kW.

    Mirrors the listener's ``_get_net_consumption`` so the sensor and
    listener use the same data when computing deferred start times.
    """
    loads = get_coordinator_value(hass, domain, "loadsPower")
    pv = get_coordinator_value(hass, domain, "pvPower")
    return (loads or 0.0) - (pv or 0.0)


def _get_bms_temp(hass: HomeAssistant, domain: str) -> float | None:
    """Return the BMS battery temperature in degrees C, or None."""
    return get_coordinator_value(hass, domain, "bmsBatteryTemperature")


def get_actual_discharge_power_w(
    hass: HomeAssistant, domain: str, requested_w: int
) -> int:
    """Return observed discharge power, falling back to the requested value.

    When ``polled_kw`` is ``None`` (no data from the coordinator), the
    requested/target value is returned as a best-effort estimate.  When
    ``polled_kw`` is 0 (battery not discharging — e.g. solar > load),
    the function returns 0 so the sensor reflects reality rather than
    showing the *target* power.
    """
    polled_kw = get_coordinator_value(hass, domain, "batDischargePower")
    if polled_kw is None:
        return requested_w
    if polled_kw <= 0:
        return 0
    return min(int(polled_kw * 1000), requested_w)


# ---------------------------------------------------------------------------
# Pure formatting helpers
# ---------------------------------------------------------------------------


def format_power(watts: int) -> str:
    """Format watts as a compact string."""
    if watts >= 1000:
        kw = watts / 1000
        if kw == int(kw):
            return f"{int(kw)}kW"
        return f"{kw:.1f}kW"
    return f"{watts}W"


def format_time(dt: datetime.datetime) -> str:
    """Format a datetime as HH:MM."""
    return f"{dt.hour:02d}:{dt.minute:02d}"


def format_remaining(end: datetime.datetime) -> str:
    """Format the time remaining until *end* as a human-readable string."""
    now = dt_util.now()
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


def format_duration(td: datetime.timedelta) -> str:
    """Format a timedelta as a compact human-readable string."""
    total_minutes = int(td.total_seconds() / 60)
    if total_minutes <= 0:
        return "0m"
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Computation helpers
# ---------------------------------------------------------------------------


def deferred_power_fraction(hass: HomeAssistant, domain: str) -> float:
    """Fraction of max power assumed during deferred start estimation."""
    h = get_smart_headroom_fraction(hass, domain)
    return (1 - h) * (1 - h)


def is_effectively_charging(
    hass: HomeAssistant, domain: str, cs: dict[str, Any]
) -> bool:
    """Return True if the charge session should be considered active.

    Bridges the gap between the calculated deferred start time and the
    next 5-minute callback.
    """
    if cs.get("charging_started", True):
        return True
    soc = get_soc_value(hass, domain)
    capacity_kwh = get_battery_capacity_kwh(hass, domain)
    target_soc: int = cs.get("target_soc", 100)
    max_power_w: int = cs.get("max_power_w", 0)
    end: datetime.datetime = cs["end"]
    start: datetime.datetime | None = cs.get("start")
    if soc is not None and capacity_kwh > 0 and max_power_w > 0 and soc < target_soc:
        energy_kwh = (target_soc - soc) / 100.0 * capacity_kwh
        charge_kw = max_power_w * deferred_power_fraction(hass, domain) / 1000.0
        charge_hours = energy_kwh / charge_kw
        deferred_start = end - datetime.timedelta(hours=charge_hours)
        if start is not None and deferred_start < start:
            deferred_start = start
        now = dt_util.now()
        if end.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        return (deferred_start - now).total_seconds() <= 0
    return False


def estimate_discharge_remaining(
    hass: HomeAssistant, domain: str, ds: dict[str, Any]
) -> str:
    """Estimate time until discharge ends or energy limit is reached."""
    now = dt_util.now()
    start: datetime.datetime = ds.get("start", now)
    end: datetime.datetime = ds["end"]
    if end.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    if now < start:
        wait = start - now
        return f"starts in {format_duration(wait)}"

    # Deferred phase — estimate when forced discharge will begin
    if not ds.get("discharging_started", True):
        from .algorithms import calculate_discharge_deferred_start

        soc = get_soc_value(hass, domain)
        if soc is not None:
            capacity = get_battery_capacity_kwh(hass, domain)
            headroom = get_smart_headroom_fraction(hass, domain)
            net_consumption = _get_net_consumption(hass, domain)
            peak = ds.get("consumption_peak_kw", 0.0)
            taper = get_domain_data(hass, domain).taper_profile
            bms_temp = _get_bms_temp(hass, domain)
            deferred = calculate_discharge_deferred_start(
                soc,
                ds.get("min_soc", 10),
                capacity,
                ds.get("max_power_w", 0),
                end,
                net_consumption_kw=net_consumption,
                start=ds.get("start"),
                headroom=headroom,
                taper_profile=taper,
                feedin_energy_limit_kwh=ds.get("feedin_energy_limit_kwh"),
                consumption_peak_kw=peak,
                bms_temp_c=bms_temp,
                grid_export_limit_w=_get_grid_export_limit(hass, domain),
            )
            if now < deferred:
                wait = deferred - now
                return f"defers {format_duration(wait)}"
        return format_duration(end - now)

    window_remaining = end - now
    if window_remaining.total_seconds() <= 0:
        return "ending"

    energy_limit = ds.get("feedin_energy_limit_kwh")
    feedin_start = ds.get("feedin_start_kwh")
    if energy_limit and feedin_start is not None:
        feedin_now = get_coordinator_value(hass, domain, "feedin")
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

    return format_duration(window_remaining)


def estimate_charge_remaining(
    hass: HomeAssistant, domain: str, cs: dict[str, Any]
) -> str:
    """Estimate time until charge completes, or starts if deferred."""
    now = dt_util.now()
    end: datetime.datetime = cs["end"]
    if end.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    window_remaining = end - now
    if window_remaining.total_seconds() <= 0:
        return "ending"

    if is_effectively_charging(hass, domain, cs):
        return format_duration(window_remaining)

    # Deferred — estimate when charging will begin
    soc = get_soc_value(hass, domain)
    capacity_kwh = get_battery_capacity_kwh(hass, domain)
    target_soc: int = cs.get("target_soc", 100)
    max_power_w: int = cs.get("effective_max_power_w", cs.get("max_power_w", 0))
    start: datetime.datetime | None = cs.get("start")
    if soc is not None and capacity_kwh > 0 and max_power_w > 0 and soc < target_soc:
        energy_kwh = (target_soc - soc) / 100.0 * capacity_kwh
        charge_kw = max_power_w * deferred_power_fraction(hass, domain) / 1000.0
        charge_hours = energy_kwh / charge_kw
        deferred_start = end - datetime.timedelta(hours=charge_hours)
        if start is not None and deferred_start < start:
            deferred_start = start
        wait = deferred_start - now
        if wait.total_seconds() <= 0:
            return format_duration(window_remaining)
        return f"starts in {format_duration(wait)}"
    return format_remaining(end)


# ---------------------------------------------------------------------------
# Forecast projection
# ---------------------------------------------------------------------------


def power_to_soc_rate(power_w: float, capacity_kwh: float) -> float:
    """Convert watts to SoC-percentage change per second."""
    if capacity_kwh <= 0:
        return 0.0
    return (power_w / 1000.0) / capacity_kwh * 100.0 / 3600.0


def project_soc_series(
    start: datetime.datetime,
    end: datetime.datetime,
    now: datetime.datetime,
    soc: float,
    rate_per_sec: float,
    target: float,
    *,
    flat_until: datetime.datetime | None = None,
    direction: int = 1,
    taper_profile: TaperProfile | None = None,
    max_power_w: int = 0,
    capacity_kwh: float = 0.0,
) -> list[dict[str, Any]]:
    """Project a SoC series from *start* to *end*.

    When *taper_profile* is provided with *max_power_w* and *capacity_kwh*,
    the effective power (and thus SoC rate) varies per SoC bucket based on
    observed BMS charge/discharge acceptance ratios.
    """
    points: list[dict[str, Any]] = []
    t = start
    cur_soc = soc
    step_secs = _FORECAST_STEP.total_seconds()
    use_taper = taper_profile is not None and max_power_w > 0 and capacity_kwh > 0
    while t <= end:
        epoch_ms = int(t.timestamp() * 1000)
        if t <= now or (flat_until is not None and t < flat_until):
            points.append({"time": epoch_ms, "soc": round(soc, 1)})
            t += _FORECAST_STEP
            continue
        points.append({"time": epoch_ms, "soc": round(cur_soc, 1)})
        t += _FORECAST_STEP
        if use_taper:
            assert taper_profile is not None
            if direction > 0:
                ratio = taper_profile.charge_ratio(cur_soc)
            else:
                ratio = taper_profile.discharge_ratio(cur_soc)
            effective_rate = power_to_soc_rate(max_power_w * ratio, capacity_kwh)
        else:
            effective_rate = rate_per_sec
        if direction > 0:
            cur_soc = min(cur_soc + effective_rate * step_secs, target)
        else:
            cur_soc = max(cur_soc - effective_rate * step_secs, target)
    end_ms = int(end.timestamp() * 1000)
    if points and points[-1]["time"] < end_ms:
        points.append({"time": end_ms, "soc": round(cur_soc, 1)})
    return deduplicate_forecast(points)


def deduplicate_forecast(
    points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove intermediate points where SoC hasn't changed."""
    if len(points) <= 2:
        return points

    result: list[dict[str, Any]] = [points[0]]
    for i in range(1, len(points) - 1):
        prev_soc = points[i - 1]["soc"]
        cur_soc = points[i]["soc"]
        next_soc = points[i + 1]["soc"]
        if cur_soc != prev_soc or next_soc != cur_soc:
            result.append(points[i])
    result.append(points[-1])
    return result


def build_forecast(
    hass: HomeAssistant,
    domain: str,
    cs: dict[str, Any] | None,
    ds: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build a SoC forecast series for active smart operations."""
    now = dt_util.now()
    capacity_kwh = get_battery_capacity_kwh(hass, domain)
    taper = get_domain_data(hass, domain).taper_profile

    if cs is not None:
        soc = get_interpolated_soc(hass, domain)
        if soc is None or capacity_kwh <= 0:
            return []

        end: datetime.datetime = cs["end"]
        session_start: datetime.datetime = cs.get("start", now)
        target_soc: int = cs.get("target_soc", 100)
        max_power_w: int = cs.get("max_power_w", 0)
        power_w: int = cs.get("last_power_w", 0)
        effectively_charging: bool = is_effectively_charging(hass, domain, cs)

        charge_rate = power_to_soc_rate(power_w, capacity_kwh)
        forecast_power_w = power_w

        deferred_start: datetime.datetime | None = None
        if not effectively_charging and max_power_w > 0:
            energy_kwh = (target_soc - soc) / 100.0 * capacity_kwh
            dpf = deferred_power_fraction(hass, domain)
            charge_kw = max_power_w * dpf / 1000.0
            if charge_kw > 0:
                charge_hours = energy_kwh / charge_kw
                ds_calc = end - datetime.timedelta(hours=charge_hours)
                deferred_start = ds_calc if ds_calc > now else now
            charge_rate = power_to_soc_rate(max_power_w * dpf, capacity_kwh)
            forecast_power_w = int(max_power_w * dpf)

        return project_soc_series(
            session_start,
            end,
            now,
            soc,
            charge_rate,
            target_soc,
            flat_until=deferred_start if not effectively_charging else None,
            direction=1,
            taper_profile=taper,
            max_power_w=forecast_power_w,
            capacity_kwh=capacity_kwh,
        )

    if ds is not None:
        soc = get_interpolated_soc(hass, domain)
        if soc is None or capacity_kwh <= 0:
            return []

        end = ds["end"]
        discharge_start: datetime.datetime = ds.get("start", now)
        min_soc: int = ds.get("min_soc", 0)
        power_w = ds.get("last_power_w", 0)

        discharge_rate = power_to_soc_rate(power_w, capacity_kwh)

        energy_limit = ds.get("feedin_energy_limit_kwh")
        energy_used = 0.0
        if energy_limit is not None:
            feedin_start = ds.get("feedin_start_kwh")
            feedin_now = get_coordinator_value(hass, domain, "feedin")
            if feedin_start is not None and feedin_now is not None:
                energy_used = feedin_now - feedin_start
        energy_remaining_kwh = (
            energy_limit - energy_used if energy_limit is not None else None
        )
        soc_floor = float(min_soc)
        if energy_remaining_kwh is not None and capacity_kwh > 0:
            max_soc_drop = energy_remaining_kwh / capacity_kwh * 100.0
            soc_floor = max(soc_floor, soc - max_soc_drop)

        return project_soc_series(
            discharge_start,
            end,
            now,
            soc,
            discharge_rate,
            soc_floor,
            flat_until=discharge_start,
            direction=-1,
            taper_profile=taper,
            max_power_w=power_w,
            capacity_kwh=capacity_kwh,
        )

    return []


# ---------------------------------------------------------------------------
# Sensor base classes
# ---------------------------------------------------------------------------


class OverrideStatusSensor(SensorEntity):
    """Compact status for Android Auto: icon + short text."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _unrecorded_attributes = frozenset(
        {
            "mode",
            "phase",
            "power_w",
            "max_power_w",
            "target_soc",
            "end_time",
            "min_soc",
            "consumption_peak_kw",
            "safety_floor_w",
        }
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_override_status"
        self._attr_translation_key = "override_status"
        self._attr_device_info = device_info
        self.hass = hass

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates for instant state changes."""
        dd = get_domain_data(self.hass, self._domain)
        ed = dd.entries.get(self._entry.entry_id)
        if ed is not None and ed.coordinator is not None:
            self.async_on_remove(
                ed.coordinator.async_add_listener(self.async_write_ha_state)
            )

    @property
    def native_value(self) -> str:
        cs = get_charge_state(self.hass, self._domain)
        if cs is not None:
            target = cs.get("target_soc", "?")
            if not is_effectively_charging(self.hass, self._domain, cs):
                return f"Wait→{target}%"
            power_w = cs.get("last_power_w", 0) or cs.get("max_power_w", 0)
            power = format_power(power_w)
            return f"Chg {power}→{target}%"

        ds = get_discharge_state(self.hass, self._domain)
        if ds is not None:
            now = dt_util.now()
            start = ds.get("start", now)
            if start.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            if now < start:
                return f"Dchg@{format_time(start)}"
            if not ds.get("discharging_started", True):
                min_soc = ds.get("min_soc", "?")
                return f"Wait→{min_soc}%"
            power = format_power(
                get_actual_discharge_power_w(
                    self.hass, self._domain, ds.get("last_power_w", 0)
                )
            )
            feedin_limit = ds.get("feedin_energy_limit_kwh")
            if feedin_limit is not None:
                return f"Dchg {power} {feedin_limit}kWh"
            end = ds.get("end")
            if end is not None:
                return f"Dchg {power}→{format_time(end)}"
            min_soc = ds.get("min_soc", "?")
            return f"Dchg {power}→{min_soc}%"

        return "Idle"

    @property
    def icon(self) -> str:
        cs = get_charge_state(self.hass, self._domain)
        if cs is not None:
            if not is_effectively_charging(self.hass, self._domain, cs):
                return ICON_DEFERRED
            return ICON_CHARGING

        ds = get_discharge_state(self.hass, self._domain)
        if ds is not None:
            if not ds.get("discharging_started", True):
                return ICON_DEFERRED
            return ICON_DISCHARGING

        return ICON_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        cs = get_charge_state(self.hass, self._domain)
        if cs is not None:
            phase = (
                "charging"
                if is_effectively_charging(self.hass, self._domain, cs)
                else "deferred"
            )
            attrs: dict[str, Any] = {
                "mode": "smart_charge",
                "phase": phase,
                "power_w": cs.get("last_power_w", 0),
                "max_power_w": cs.get("max_power_w"),
                "target_soc": cs.get("target_soc"),
                "end_time": cs["end"].isoformat(),
            }
            if cs.get("circuit_open"):
                attrs["circuit_breaker_active"] = True
                attrs["circuit_breaker_since"] = cs.get("circuit_open_since")
            return attrs

        ds = get_discharge_state(self.hass, self._domain)
        if ds is not None:
            phase = (
                "deferred" if not ds.get("discharging_started", True) else "discharging"
            )
            peak = ds.get("consumption_peak_kw", 0.0)
            from .algorithms import safety_floor_w

            ds_attrs: dict[str, Any] = {
                "mode": "smart_discharge",
                "phase": phase,
                "power_w": ds.get("last_power_w", 0),
                "min_soc": ds.get("min_soc"),
                "end_time": ds["end"].isoformat(),
                "consumption_peak_kw": round(peak, 2),
                "safety_floor_w": safety_floor_w(peak),
            }
            if ds.get("circuit_open"):
                ds_attrs["circuit_breaker_active"] = True
                ds_attrs["circuit_breaker_since"] = ds.get("circuit_open_since")
            return ds_attrs

        return None


class SmartOperationsOverviewSensor(RestoreSensor):
    """Dashboard sensor providing a rich overview of smart operations."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_translation_key = "smart_operations"
    _attr_device_class = SensorDeviceClass.ENUM
    _unrecorded_attributes = frozenset(
        {
            "charge_power_w",
            "charge_effective_max_power_w",
            "charge_current_soc",
            "charge_confirmed_soc",
            "charge_remaining",
            "charge_target_reachable",
            "discharge_power_w",
            "discharge_target_power_w",
            "discharge_current_soc",
            "discharge_confirmed_soc",
            "discharge_remaining",
            "discharge_feedin_used_kwh",
            "discharge_feedin_projected_kwh",
            "has_error",
            "last_error",
            "last_error_at",
            "error_count",
        }
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_smart_operations"
        self._attr_device_info = device_info
        self._attr_options = [
            "idle",
            "error",
            "charging",
            "deferred",
            "target_reached",
            "discharging",
            "discharge_deferred",
            "discharge_scheduled",
            "discharge_suspended",
            "charge_discharge_active",
        ]
        self.hass = hass
        self._restored_state: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore last state and subscribe to coordinator updates."""
        await super().async_added_to_hass()
        last_data = await self.async_get_last_sensor_data()
        if last_data and last_data.native_value:
            self._restored_state = str(last_data.native_value)
        entry_data = getattr(self._entry, "runtime_data", None)
        coordinator = getattr(entry_data, "coordinator", None) if entry_data else None
        if coordinator is not None:

            def _on_coordinator_update() -> None:
                self._restored_state = None
                self.async_write_ha_state()

            self.async_on_remove(coordinator.async_add_listener(_on_coordinator_update))

    @property
    def native_value(self) -> str:
        cs = get_charge_state(self.hass, self._domain)
        ds = get_discharge_state(self.hass, self._domain)

        if cs is not None and ds is not None:
            return "charge_discharge_active"

        if cs is not None:
            if cs.get("target_reached"):
                return "target_reached"
            if not is_effectively_charging(self.hass, self._domain, cs):
                return "deferred"
            return "charging"

        if ds is not None:
            now = dt_util.now()
            start = ds.get("start", now)
            if start.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            if now < start:
                return "discharge_scheduled"
            if not ds.get("discharging_started", True):
                return "discharge_deferred"
            if ds.get("suspended"):
                return "discharge_suspended"
            return "discharging"

        err = get_domain_data(self.hass, self._domain).smart_error_state
        if err and err.get("last_error"):
            return "error"

        if self._restored_state and self._restored_state in (self._attr_options or ()):
            return self._restored_state

        return "idle"

    @property
    def icon(self) -> str:
        cs = get_charge_state(self.hass, self._domain)
        if cs is not None:
            if not is_effectively_charging(self.hass, self._domain, cs):
                return ICON_DEFERRED
            return ICON_CHARGING
        ds = get_discharge_state(self.hass, self._domain)
        if ds is not None:
            if not ds.get("discharging_started", True):
                return ICON_DEFERRED
            return ICON_DISCHARGING
        return ICON_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cs = get_charge_state(self.hass, self._domain)
        ds = get_discharge_state(self.hass, self._domain)

        err = get_domain_data(self.hass, self._domain).smart_error_state or {}
        attrs: dict[str, Any] = {
            "charge_active": cs is not None,
            "discharge_active": ds is not None,
            "has_error": bool(err.get("last_error")),
            "last_error": err.get("last_error"),
            "last_error_at": err.get("last_error_at"),
            "error_count": err.get("error_count", 0),
        }

        if cs is not None:
            charging = is_effectively_charging(self.hass, self._domain, cs)
            soc = get_interpolated_soc(self.hass, self._domain)
            attrs.update(
                {
                    "charge_phase": "charging" if charging else "deferred",
                    "charge_power_w": (
                        cs.get("last_power_w", 0)
                        or (cs.get("max_power_w", 0) if charging else 0)
                    ),
                    "charge_max_power_w": cs.get("max_power_w"),
                    "charge_effective_max_power_w": cs.get(
                        "effective_max_power_w", cs.get("max_power_w")
                    ),
                    "charge_target_soc": cs.get("target_soc"),
                    "charge_current_soc": soc,
                    "charge_confirmed_soc": get_soc_value(self.hass, self._domain),
                    "charge_window": (
                        f"{format_time(cs['start'])} – {format_time(cs['end'])}"
                    ),
                    "charge_remaining": estimate_charge_remaining(
                        self.hass, self._domain, cs
                    ),
                    "charge_start_time": cs["start"].isoformat(),
                    "charge_end_time": cs["end"].isoformat(),
                    "charge_start_soc": cs.get("start_soc", soc),
                    "charge_target_reachable": self._is_charge_reachable(cs, soc),
                }
            )
            if cs.get("circuit_open"):
                attrs["circuit_breaker_active"] = True
                attrs["circuit_breaker_since"] = cs.get("circuit_open_since")

        if ds is not None:
            soc = get_interpolated_soc(self.hass, self._domain)
            now = dt_util.now()
            ds_start = ds.get("start", now)
            if ds_start.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            requested = ds.get("last_power_w", 0)
            ds_power = (
                0
                if now < ds_start
                else get_actual_discharge_power_w(self.hass, self._domain, requested)
            )
            feedin_limit = ds.get("feedin_energy_limit_kwh")
            feedin_used: float | None = None
            feedin_projected: float | None = None
            if feedin_limit is not None:
                feedin_start = ds.get("feedin_start_kwh")
                feedin_now = get_coordinator_value(self.hass, self._domain, "feedin")
                if feedin_start is not None and feedin_now is not None:
                    feedin_used = round(feedin_now - feedin_start, 2)
                    elapsed = (now - ds_start).total_seconds()
                    total_secs = (ds["end"] - ds_start).total_seconds()
                    if elapsed > 0 and total_secs > 0:
                        feedin_projected = round(
                            min(feedin_used / elapsed * total_secs, feedin_limit), 2
                        )
            discharge_phase = "discharging"
            if now < ds_start:
                discharge_phase = "scheduled"
            elif not ds.get("discharging_started", True):
                discharge_phase = "deferred"
            elif ds.get("suspended"):
                discharge_phase = "suspended"
            attrs.update(
                {
                    "discharge_phase": discharge_phase,
                    "discharge_power_w": ds_power,
                    "discharge_target_power_w": ds.get("target_power_w", ds_power),
                    "discharge_max_power_w": ds.get("max_power_w"),
                    "discharge_min_soc": ds.get("min_soc"),
                    "discharge_current_soc": soc,
                    "discharge_confirmed_soc": get_soc_value(self.hass, self._domain),
                    "discharge_window": (
                        f"{format_time(ds['start'])} – {format_time(ds['end'])}"
                    ),
                    "discharge_remaining": estimate_discharge_remaining(
                        self.hass, self._domain, ds
                    ),
                    "discharge_start_time": ds["start"].isoformat(),
                    "discharge_end_time": ds["end"].isoformat(),
                    "discharge_schedule_horizon": ds.get("schedule_horizon"),
                    "discharge_start_soc": ds.get("start_soc", soc),
                    "discharge_feedin_limit_kwh": feedin_limit,
                    "discharge_feedin_used_kwh": feedin_used,
                    "discharge_feedin_projected_kwh": feedin_projected,
                }
            )
            if ds.get("circuit_open"):
                attrs["circuit_breaker_active"] = True
                attrs["circuit_breaker_since"] = ds.get("circuit_open_since")

        return attrs

    def _is_charge_reachable(
        self, cs: dict[str, Any], soc: float | None
    ) -> bool | None:
        """Check if the charge target can be reached in remaining time."""
        from .algorithms import is_charge_target_reachable

        if soc is None:
            return None
        now = dt_util.now()
        end: datetime.datetime = cs["end"]
        if end.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        remaining_h = (end - now).total_seconds() / 3600.0
        if remaining_h <= 0:
            return None
        capacity = get_battery_capacity_kwh(self.hass, self._domain)
        dd = get_domain_data(self.hass, self._domain)
        taper = dd.taper_profile
        from .domain_data import get_first_coordinator

        bms_temp: float | None = None
        coordinator = get_first_coordinator(self.hass, self._domain)
        if coordinator is not None and coordinator.data:
            raw = coordinator.data.get("bmsBatteryTemperature")
            if raw is not None:
                with contextlib.suppress(ValueError, TypeError):
                    bms_temp = float(raw)
        return is_charge_target_reachable(
            soc,
            cs.get("target_soc", 100),
            capacity,
            remaining_h,
            cs.get("max_power_w", 0),
            taper_profile=taper,
            bms_temp_c=bms_temp,
        )


class ChargePowerSensor(SensorEntity):
    """Current smart charge power."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = ICON_POWER
    _attr_native_unit_of_measurement = "W"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_charge_power"
        self._attr_translation_key = "charge_power"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def native_value(self) -> int | None:
        cs = get_charge_state(self.hass, self._domain)
        if cs is None:
            return _STATE_UNAVAILABLE
        power: int = cs.get("last_power_w", 0)
        if power == 0 and is_effectively_charging(self.hass, self._domain, cs):
            power = cs.get("max_power_w", 0)
        return power

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        cs = get_charge_state(self.hass, self._domain)
        if cs is None:
            return None
        return {
            "target_soc": cs.get("target_soc"),
            "max_power_w": cs.get("max_power_w"),
            "phase": (
                "charging"
                if is_effectively_charging(self.hass, self._domain, cs)
                else "deferred"
            ),
        }


class ChargeWindowSensor(SensorEntity):
    """Smart charge time window."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = ICON_CLOCK

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_charge_window"
        self._attr_translation_key = "charge_window"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        cs = get_charge_state(self.hass, self._domain)
        if cs is None:
            return _STATE_UNAVAILABLE
        return f"{format_time(cs['start'])} – {format_time(cs['end'])}"


class ChargeRemainingSensor(SensorEntity):
    """Time remaining in the smart charge window."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = ICON_TIMER

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_charge_remaining"
        self._attr_translation_key = "charge_remaining"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        cs = get_charge_state(self.hass, self._domain)
        if cs is None:
            return _STATE_UNAVAILABLE
        return estimate_charge_remaining(self.hass, self._domain, cs)


class DischargePowerSensor(SensorEntity):
    """Current smart discharge power."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = ICON_POWER
    _attr_native_unit_of_measurement = "W"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_discharge_power"
        self._attr_translation_key = "discharge_power"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def native_value(self) -> int | None:
        ds = get_discharge_state(self.hass, self._domain)
        if ds is None:
            return _STATE_UNAVAILABLE
        now = dt_util.now()
        start = ds.get("start", now)
        if start.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        if now < start:
            return 0
        if not ds.get("discharging_started", True):
            return 0
        return get_actual_discharge_power_w(
            self.hass, self._domain, ds.get("last_power_w", 0)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        ds = get_discharge_state(self.hass, self._domain)
        if ds is None:
            return None
        peak = ds.get("consumption_peak_kw", 0.0)
        from .algorithms import safety_floor_w

        return {
            "min_soc": ds.get("min_soc"),
            "consumption_peak_kw": round(peak, 2),
            "safety_floor_w": safety_floor_w(peak),
        }


class DischargeWindowSensor(SensorEntity):
    """Smart discharge time window."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = ICON_CLOCK

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_discharge_window"
        self._attr_translation_key = "discharge_window"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        ds = get_discharge_state(self.hass, self._domain)
        if ds is None:
            return _STATE_UNAVAILABLE
        return f"{format_time(ds['start'])} – {format_time(ds['end'])}"


class DischargeRemainingSensor(SensorEntity):
    """Time remaining in the smart discharge window."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = ICON_TIMER

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_discharge_remaining"
        self._attr_translation_key = "discharge_remaining"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def native_value(self) -> str | None:
        ds = get_discharge_state(self.hass, self._domain)
        if ds is None:
            return _STATE_UNAVAILABLE
        return estimate_discharge_remaining(self.hass, self._domain, ds)


class BatteryForecastSensor(SensorEntity):
    """Projected battery SoC over time for ApexCharts display."""

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = ICON_FORECAST
    _attr_native_unit_of_measurement = "%"
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_battery_forecast"
        self._attr_translation_key = "battery_forecast"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def native_value(self) -> float | None:
        cs = get_charge_state(self.hass, self._domain)
        ds = get_discharge_state(self.hass, self._domain)
        if cs is None and ds is None:
            return _STATE_UNAVAILABLE
        return get_soc_value(self.hass, self._domain)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cs = get_charge_state(self.hass, self._domain)
        ds = get_discharge_state(self.hass, self._domain)
        if cs is None and ds is None:
            return {"forecast": []}
        forecast = build_forecast(self.hass, self._domain, cs, ds)
        return {"forecast": forecast}


# ---------------------------------------------------------------------------
# Binary sensor base classes
# ---------------------------------------------------------------------------


class SmartChargeActiveSensor(BinarySensorEntity):
    """Binary sensor that is on while a smart charge session is active."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_smart_charge_active"
        self._attr_translation_key = "smart_charge_active"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def is_on(self) -> bool:
        return get_charge_state(self.hass, self._domain) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = get_charge_state(self.hass, self._domain)
        if state is None:
            return None
        phase = "charging" if state.get("charging_started", True) else "deferred"
        end = state.get("end")
        return {
            "target_soc": state.get("target_soc"),
            "phase": phase,
            "current_power_w": state.get("last_power_w", 0),
            "max_power_w": state.get("max_power_w", 0),
            "end_time": end.isoformat() if end is not None else None,
        }


class SmartDischargeActiveSensor(BinarySensorEntity):
    """Binary sensor that is on while a smart discharge session is active."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        device_info: DeviceInfo,
    ) -> None:
        self._entry = entry
        self._domain = domain
        self._attr_unique_id = f"{entry.entry_id}_smart_discharge_active"
        self._attr_translation_key = "smart_discharge_active"
        self._attr_device_info = device_info
        self.hass = hass

    @property
    def is_on(self) -> bool:
        return get_discharge_state(self.hass, self._domain) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = get_discharge_state(self.hass, self._domain)
        if state is None:
            return None
        end = state.get("end")
        return {
            "min_soc": state.get("min_soc"),
            "last_power_w": state.get("last_power_w", 0),
            "end_time": end.isoformat() if end is not None else None,
        }
