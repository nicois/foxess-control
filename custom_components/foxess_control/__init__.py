"""FoxESS Control — Home Assistant integration for inverter mode management."""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.persistent_notification import async_create as pn_create
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY,
    CONF_API_MIN_SOC,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGE_POWER_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_INVERTER_POWER,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ENTITY,
    CONF_MIN_SOC_ON_GRID,
    CONF_POLLING_INTERVAL,
    CONF_SMART_HEADROOM,
    CONF_WORK_MODE_ENTITY,
    DEFAULT_API_MIN_SOC,
    DEFAULT_ENTITY_POLLING_INTERVAL,
    DEFAULT_INVERTER_POWER,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_SMART_HEADROOM,
    DOMAIN,
    MAX_OVERRIDE_HOURS,
    PLATFORMS,
)
from .coordinator import (
    FoxESSDataCoordinator,
    FoxESSEntityCoordinator,
    get_coordinator_soc,
)
from .foxess import FoxESSClient, Inverter, WorkMode

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

    from .foxess.inverter import ScheduleGroup

_LOGGER = logging.getLogger(__name__)

SERVICE_CLEAR_OVERRIDES = "clear_overrides"
SERVICE_FEEDIN = "feedin"
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_SMART_CHARGE = "smart_charge"
SERVICE_SMART_DISCHARGE = "smart_discharge"

SMART_CHARGE_ADJUST_INTERVAL = datetime.timedelta(minutes=5)
SMART_DISCHARGE_CHECK_INTERVAL = datetime.timedelta(seconds=60)

# Cancel a smart session if the SoC entity is unavailable for this many
# consecutive periodic checks (3 × 5 min = 15 minutes).
MAX_SOC_UNAVAILABLE_COUNT = 3

STORAGE_KEY = "foxess_control_sessions"
STORAGE_VERSION = 1

VALID_MODES = [m.value for m in WorkMode]

# Modes that this integration creates or treats as a safe baseline.
# Any other mode in existing schedule groups indicates a non-standard
# configuration that we should not overwrite.
_MANAGED_WORK_MODES = frozenset(
    {
        WorkMode.SELF_USE.value,
        WorkMode.FORCE_CHARGE.value,
        WorkMode.FORCE_DISCHARGE.value,
        WorkMode.FEEDIN.value,
    }
)

SCHEMA_CLEAR_OVERRIDES = vol.Schema(
    {
        vol.Optional("mode"): vol.In(VALID_MODES),
    }
)

SCHEMA_FORCE_CHARGE = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Optional("start_time"): cv.time,
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_FORCE_DISCHARGE = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Optional("start_time"): cv.time,
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_FEEDIN = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Optional("start_time"): cv.time,
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_SMART_DISCHARGE = vol.Schema(
    {
        vol.Required("start_time"): cv.time,
        vol.Required("end_time"): cv.time,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Required("min_soc"): vol.All(int, vol.Range(min=5, max=100)),
        vol.Optional("feedin_energy_limit_kwh"): vol.All(
            vol.Coerce(float), vol.Range(min=0.1)
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_SMART_CHARGE = vol.Schema(
    {
        vol.Required("start_time"): cv.time,
        vol.Required("end_time"): cv.time,
        vol.Required("target_soc"): vol.All(int, vol.Range(min=5, max=100)),
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
    },
    extra=vol.ALLOW_EXTRA,
)


def _first_entry_id(hass: HomeAssistant) -> str:
    """Return the entry_id of the first real config entry in domain data.

    NOTE: Services currently operate on a single inverter only.
    If multiple config entries exist, only the first is used.
    """
    for key in hass.data[DOMAIN]:
        if not str(key).startswith("_"):
            return str(key)
    raise ServiceValidationError("No FoxESS Control integration configured")


def _get_inverter(hass: HomeAssistant) -> Inverter:
    """Get the first configured Inverter instance."""
    entry_id = _first_entry_id(hass)
    inverter: Inverter = hass.data[DOMAIN][entry_id]["inverter"]
    return inverter


def _get_min_soc_on_grid(hass: HomeAssistant) -> int:
    """Get min_soc_on_grid from the first config entry's options."""
    entry_id = _first_entry_id(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return DEFAULT_MIN_SOC_ON_GRID
    soc: int = entry.options.get(CONF_MIN_SOC_ON_GRID, DEFAULT_MIN_SOC_ON_GRID)
    return soc


def _get_first_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the first real config entry."""
    entry_id = _first_entry_id(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise ServiceValidationError("No FoxESS Control integration configured")
    return entry


def _get_smart_headroom(hass: HomeAssistant) -> float:
    """Return the charge headroom as a fraction (e.g. 0.10 for 10%)."""
    entry = _get_first_entry(hass)
    pct: int = entry.options.get(CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM)
    return pct / 100.0


def _get_polling_interval_seconds(hass: HomeAssistant) -> int:
    """Return the coordinator's polling interval in seconds."""
    entry_id = _first_entry_id(hass)
    coordinator = hass.data[DOMAIN][entry_id].get("coordinator")
    if coordinator is not None and coordinator.update_interval is not None:
        return int(coordinator.update_interval.total_seconds())
    return DEFAULT_POLLING_INTERVAL


def _is_entity_mode(hass: HomeAssistant) -> bool:
    """Check if entity-based control is configured (foxess_modbus interop)."""
    try:
        entry = _get_first_entry(hass)
    except (ServiceValidationError, KeyError):
        return False
    return bool(entry.options.get(CONF_WORK_MODE_ENTITY))


def _get_max_power_w(hass: HomeAssistant) -> int:
    """Return the inverter's maximum power in watts.

    In entity mode this comes from the ``CONF_INVERTER_POWER`` option.
    In cloud mode it comes from the cached ``Inverter.max_power_w`` property.
    """
    entry = _get_first_entry(hass)
    configured = entry.options.get(CONF_INVERTER_POWER)
    if configured:
        return int(configured)
    # Fall back to cloud API inverter object
    try:
        return _get_inverter(hass).max_power_w
    except Exception:
        return DEFAULT_INVERTER_POWER


# Map foxess_control WorkMode values to foxess_modbus select entity options.
_ENTITY_MODE_MAP: dict[str, str] = {
    WorkMode.SELF_USE: "Self Use",
    WorkMode.FORCE_CHARGE: "Force Charge",
    WorkMode.FORCE_DISCHARGE: "Force Discharge",
    WorkMode.BACKUP: "Back-up",
    WorkMode.FEEDIN: "Feed-in First",
}


async def _apply_mode_via_entities(
    hass: HomeAssistant,
    mode: WorkMode,
    power_w: int | None = None,
    fd_soc: int = 11,
) -> None:
    """Set inverter mode by writing to external entities (foxess_modbus interop).

    Sets the work mode via a ``select`` entity and optionally adjusts the
    charge/discharge power limit and min SoC via ``number`` entities.
    """
    opts = _get_first_entry(hass).options

    _LOGGER.debug(
        "Entity backend: setting mode=%s power=%s fd_soc=%d",
        mode,
        f"{power_w}W" if power_w is not None else "unchanged",
        fd_soc,
    )

    mode_option = _ENTITY_MODE_MAP.get(mode)
    if mode_option:
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": opts[CONF_WORK_MODE_ENTITY], "option": mode_option},
        )

    if power_w is not None and mode in (
        WorkMode.FORCE_CHARGE,
        WorkMode.FORCE_DISCHARGE,
    ):
        power_entity = (
            opts.get(CONF_CHARGE_POWER_ENTITY)
            if mode == WorkMode.FORCE_CHARGE
            else opts.get(CONF_DISCHARGE_POWER_ENTITY)
        )
        if power_entity:
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": power_entity, "value": power_w},
            )

    min_soc_entity = opts.get(CONF_MIN_SOC_ENTITY)
    if min_soc_entity and mode == WorkMode.FORCE_DISCHARGE:
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": min_soc_entity, "value": fd_soc},
        )


def _get_api_min_soc(hass: HomeAssistant) -> int:
    """Get api_min_soc from the first config entry's options."""
    entry_id = _first_entry_id(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return DEFAULT_API_MIN_SOC
    val: int = int(entry.options.get(CONF_API_MIN_SOC, DEFAULT_API_MIN_SOC))
    return val


def _resolve_start_end(
    duration: datetime.timedelta,
    start_time: datetime.time | None = None,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Validate duration and resolve start/end datetimes.

    If *start_time* is ``None`` the override begins now, otherwise it
    begins at *start_time* today.

    Raises ServiceValidationError if duration exceeds MAX_OVERRIDE_HOURS
    or the window would extend past midnight.
    """
    max_delta = datetime.timedelta(hours=MAX_OVERRIDE_HOURS)
    if duration <= datetime.timedelta(0):
        raise ServiceValidationError("Duration must be positive")
    if duration > max_delta:
        raise ServiceValidationError(
            f"Duration must not exceed {MAX_OVERRIDE_HOURS} hours"
        )

    now = dt_util.now()

    if start_time is not None:
        start = now.replace(
            hour=start_time.hour,
            minute=start_time.minute,
            second=0,
            microsecond=0,
        )
    else:
        start = now

    end = start + duration

    if end.date() != start.date():
        raise ServiceValidationError("Override must not extend past midnight")

    return start, end


def _resolve_start_end_explicit(
    start_time: datetime.time,
    end_time: datetime.time,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Validate and resolve explicit start/end times to datetimes (today).

    Raises ServiceValidationError if end <= start, window exceeds
    MAX_OVERRIDE_HOURS, or the window would cross midnight.
    """
    now = dt_util.now()
    start = now.replace(
        hour=start_time.hour,
        minute=start_time.minute,
        second=0,
        microsecond=0,
    )
    end = now.replace(
        hour=end_time.hour,
        minute=end_time.minute,
        second=0,
        microsecond=0,
    )

    if end <= start:
        raise ServiceValidationError("End time must be after start time")

    max_delta = datetime.timedelta(hours=MAX_OVERRIDE_HOURS)
    if (end - start) > max_delta:
        raise ServiceValidationError(
            f"Window must not exceed {MAX_OVERRIDE_HOURS} hours"
        )

    if start < now:
        _LOGGER.warning(
            "Start time %02d:%02d is in the past (now %02d:%02d); "
            "the inverter will begin immediately",
            start.hour,
            start.minute,
            now.hour,
            now.minute,
        )

    return start, end


def _get_current_soc(hass: HomeAssistant) -> float | None:
    """Get current battery SoC from the coordinator.

    Returns None if SoC is unavailable.
    """
    return get_coordinator_soc(hass)


def _get_net_consumption(hass: HomeAssistant) -> float:
    """Return net site consumption (loads minus solar) in kW.

    Reads ``loadsPower`` and ``pvPower`` from the coordinator.
    Returns ``0.0`` when coordinator data is unavailable so callers
    fall back to the previous no-offset behaviour.
    """
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return 0.0
    for key in domain_data:
        if not str(key).startswith("_"):
            entry_data = domain_data.get(key)
            if isinstance(entry_data, dict):
                coordinator = entry_data.get("coordinator")
                if coordinator is not None and coordinator.data:
                    try:
                        loads = float(coordinator.data.get("loadsPower", 0))
                        pv = float(coordinator.data.get("pvPower", 0))
                        return loads - pv
                    except (ValueError, TypeError):
                        _LOGGER.warning(
                            "Failed to parse loadsPower=%r / pvPower=%r "
                            "from coordinator, using 0",
                            coordinator.data.get("loadsPower"),
                            coordinator.data.get("pvPower"),
                        )
                        return 0.0
    return 0.0


def _get_feedin_energy_kwh(hass: HomeAssistant) -> float | None:
    """Return cumulative grid feed-in energy in kWh from the coordinator.

    Reads the ``feedin`` variable (lifetime counter) rather than
    the instantaneous ``feedinPower``.  Returns ``None`` when
    coordinator data is unavailable.
    """
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return None
    for key in domain_data:
        if not str(key).startswith("_"):
            entry_data = domain_data.get(key)
            if isinstance(entry_data, dict):
                coordinator = entry_data.get("coordinator")
                if coordinator is not None and coordinator.data:
                    raw = coordinator.data.get("feedin")
                    if raw is None:
                        return None
                    try:
                        return float(raw)
                    except (ValueError, TypeError):
                        return None
    return None


def _soc_energy_kwh(soc: float, capacity_kwh: float) -> float:
    """Convert a SoC percentage to energy in kWh."""
    return soc / 100.0 * capacity_kwh


def _cancel_smart_discharge(hass: HomeAssistant, *, clear_storage: bool = True) -> None:
    """Cancel any active smart discharge listeners and clear stored session."""
    unsubs: list[Callable[[], None]] = hass.data[DOMAIN].get(
        "_smart_discharge_unsubs", []
    )
    for unsub in unsubs:
        unsub()
    hass.data[DOMAIN]["_smart_discharge_unsubs"] = []
    hass.data[DOMAIN].pop("_smart_discharge_state", None)
    if clear_storage and hass.data.get(DOMAIN, {}).get("_store") is not None:
        hass.async_create_task(_clear_stored_session(hass, "smart_discharge"))


async def _save_session(hass: HomeAssistant, key: str, data: dict[str, Any]) -> None:
    """Persist a smart session to storage."""
    store: Store[dict[str, Any]] = hass.data[DOMAIN].get("_store")
    if store is None:
        return
    stored: dict[str, Any] = await store.async_load() or {}
    stored[key] = data
    await store.async_save(stored)


async def _clear_stored_session(hass: HomeAssistant, key: str) -> None:
    """Remove a smart session from storage."""
    store: Store[dict[str, Any]] | None = hass.data.get(DOMAIN, {}).get("_store")
    if store is None:
        return
    stored: dict[str, Any] = await store.async_load() or {}
    if key in stored:
        del stored[key]
        await store.async_save(stored)


def _session_data_from_charge_state(state: dict[str, Any]) -> dict[str, Any]:
    """Build a serialisable dict from a smart charge state dict."""
    return {
        "date": state["start"].strftime("%Y-%m-%d"),
        "start_hour": state["start"].hour,
        "start_minute": state["start"].minute,
        "end_hour": state["end"].hour,
        "end_minute": state["end"].minute,
        "target_soc": state["target_soc"],
        "max_power_w": state["max_power_w"],
        "battery_capacity_kwh": state["battery_capacity_kwh"],
        "min_soc_on_grid": state["min_soc_on_grid"],
        "min_power_change": state["min_power_change"],
        "api_min_soc": state.get("api_min_soc", DEFAULT_API_MIN_SOC),
        "force": state.get("force", False),
        "charging_started": state["charging_started"],
        "charging_started_at": (
            state["charging_started_at"].isoformat()
            if state.get("charging_started_at")
            else None
        ),
        "charging_started_energy_kwh": state.get("charging_started_energy_kwh"),
    }


def _session_data_from_discharge_state(state: dict[str, Any]) -> dict[str, Any]:
    """Build a serialisable dict from a smart discharge state dict."""
    data: dict[str, Any] = {
        "date": state["start"].strftime("%Y-%m-%d"),
        "start_hour": state["start"].hour,
        "start_minute": state["start"].minute,
        "end_hour": state["end"].hour,
        "end_minute": state["end"].minute,
        "min_soc": state["min_soc"],
        "max_power_w": state.get("max_power_w", state["last_power_w"]),
        "last_power_w": state["last_power_w"],
    }
    if state.get("feedin_energy_limit_kwh") is not None:
        data["feedin_energy_limit_kwh"] = state["feedin_energy_limit_kwh"]
        if state.get("feedin_start_kwh") is not None:
            data["feedin_start_kwh"] = state["feedin_start_kwh"]
    if state.get("pacing_enabled"):
        data["pacing_enabled"] = True
        data["battery_capacity_kwh"] = state["battery_capacity_kwh"]
        data["min_power_change"] = state["min_power_change"]
    return data


def _get_battery_capacity_kwh(hass: HomeAssistant) -> float:
    """Get battery_capacity_kwh from the first config entry's options."""
    entry_id = _first_entry_id(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return 0.0
    capacity: float = entry.options.get(CONF_BATTERY_CAPACITY_KWH, 0.0)
    return capacity


def _get_min_power_change(hass: HomeAssistant) -> int:
    """Get min_power_change from the first config entry's options."""
    entry_id = _first_entry_id(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return DEFAULT_MIN_POWER_CHANGE
    val: int = int(entry.options.get(CONF_MIN_POWER_CHANGE, DEFAULT_MIN_POWER_CHANGE))
    return val


def _calculate_charge_power(
    current_soc: float,
    target_soc: int,
    battery_capacity_kwh: float,
    remaining_hours: float,
    max_power_w: int,
    net_consumption_kw: float = 0.0,
    headroom: float = 0.10,
    charging_started_energy_kwh: float | None = None,
    elapsed_since_charge_started: float = 0.0,
    effective_charge_window: float = 0.0,
    min_power_change_w: int = 0,
) -> int:
    """Calculate the charge power needed to reach target SoC in remaining time.

    *headroom* is a fraction (e.g. 0.10 for 10%) controlling how much
    spare capacity to reserve.  It is applied both as a time buffer (plan
    to finish in ``1 - headroom`` of the remaining time) and as a power
    multiplier (``1 + headroom``).  This matches the headroom used by
    ``_calculate_deferred_start`` so the two stay in agreement.

    When *charging_started_energy_kwh* and *elapsed_since_charge_started*
    are provided, the function checks whether the current energy is behind
    the ideal headroom-adjusted trajectory.  The ideal trajectory is a
    linear ramp from the starting energy to the target energy, completing
    in ``effective_charge_window * (1 - headroom)`` hours.  If the actual
    energy is below the ideal by more than a tolerance, *max_power_w* is
    returned to catch up.  Once the trajectory is regained, normal pacing
    resumes.  The tolerance equals ``min_power_change_w / 1000 ×
    remaining_hours`` — the energy a minimum-meaningful power bump would
    recover over the remaining window.  This prevents premature bursting
    from minor measurement fluctuations while still catching up promptly
    as the window closes.

    When *net_consumption_kw* is positive the house is drawing power that
    competes with the battery for inverter capacity, so we add it to the
    required charge rate.  When negative (solar exceeds consumption) the
    battery gets a free boost — we don't subtract because the inverter
    will naturally absorb the surplus.

    Returns an integer power in watts, clamped to [100, max_power_w].
    """
    target_energy_kwh = _soc_energy_kwh(target_soc, battery_capacity_kwh)
    energy_needed_kwh = target_energy_kwh - _soc_energy_kwh(
        current_soc, battery_capacity_kwh
    )
    if energy_needed_kwh <= 0:
        return 100
    if remaining_hours <= 0:
        return max_power_w

    # Check if we're behind the ideal headroom-adjusted trajectory.
    if (
        charging_started_energy_kwh is not None
        and elapsed_since_charge_started > 0
        and effective_charge_window > 0
        and headroom > 0
    ):
        effective_window = effective_charge_window * (1 - headroom)
        if effective_window > 0:
            energy_to_add = target_energy_kwh - charging_started_energy_kwh
            if energy_to_add > 0:
                progress = min(elapsed_since_charge_started / effective_window, 1.0)
                ideal_energy_now = (
                    charging_started_energy_kwh + progress * energy_to_add
                )
                actual_energy = _soc_energy_kwh(current_soc, battery_capacity_kwh)
                tolerance_kwh = min_power_change_w / 1000.0 * remaining_hours
                deficit = ideal_energy_now - actual_energy
                if deficit > tolerance_kwh:
                    _LOGGER.debug(
                        "Smart charge: behind schedule "
                        "(%.2f kWh < ideal %.2f kWh, "
                        "deficit %.3f > tolerance %.3f), "
                        "charging at max power",
                        actual_energy,
                        ideal_energy_now,
                        deficit,
                        tolerance_kwh,
                    )
                    return max_power_w

    # Plan to finish in (1 - headroom) of the remaining time so there is
    # a buffer if consumption spikes or the inverter can't sustain full power.
    effective_hours = remaining_hours * (1 - headroom)
    if effective_hours <= 0:
        effective_hours = remaining_hours
    battery_power_kw = energy_needed_kwh / effective_hours
    total_power_kw = battery_power_kw + max(0.0, net_consumption_kw)
    # Over-provision the charge rate so unexpected load doesn't prevent
    # reaching the target.  The absolute headroom scales linearly with the
    # required power, so it naturally shrinks as SoC approaches the target.
    total_power_kw *= 1 + headroom
    power_w = total_power_kw * 1000
    return max(100, min(int(power_w), max_power_w))


def _should_suspend_discharge(
    current_soc: float,
    min_soc: int,
    battery_capacity_kwh: float,
    remaining_hours: float,
    net_consumption_kw: float,
    headroom: float = 0.10,
) -> bool:
    """Return True if forced discharge should be suspended.

    Suspension protects the min SoC target.  If household consumption
    alone would drain the battery to (or past) min SoC within the
    remaining window, adding *any* forced discharge power risks
    breaching the floor.  In that case the inverter should revert to
    SelfUse so that only unavoidable house load draws from the battery.

    A headroom factor is applied so suspension triggers slightly early,
    giving the system time to react.
    """
    if remaining_hours <= 0 or battery_capacity_kwh <= 0:
        return False
    energy_kwh = (current_soc - min_soc) / 100.0 * battery_capacity_kwh
    if energy_kwh <= 0:
        return True  # already at or below min SoC
    consumption = max(0.0, net_consumption_kw)
    if consumption <= 0:
        return False  # no house load — no risk from forced discharge
    # Time until house load alone drains to min SoC
    hours_to_min = energy_kwh / consumption
    # Suspend if house load would reach min SoC within the window
    # (with headroom so we suspend slightly early)
    return hours_to_min <= remaining_hours * (1 + headroom)


def _calculate_discharge_power(
    current_soc: float,
    min_soc: int,
    battery_capacity_kwh: float,
    remaining_hours: float,
    max_power_w: int,
    net_consumption_kw: float = 0.0,
    headroom: float = 0.10,
    feedin_remaining_kwh: float | None = None,
) -> int:
    """Calculate the discharge power needed to reach min SoC by window end.

    Mirrors ``_calculate_charge_power`` but for discharge.  *headroom* is
    applied as both a time buffer and a power multiplier.

    Unlike charge, household consumption *assists* discharge (the load
    drains the battery alongside the inverter), so *net_consumption_kw*
    is **subtracted** from the required discharge rate.  If the house load
    alone is enough to drain the battery at the needed rate, returns 100
    (minimum).

    When *feedin_remaining_kwh* is provided, the target energy is capped
    so the grid export budget is spread evenly across the remaining window.
    The maximum achievable battery drain within both time and export
    constraints is ``feedin_remaining + house_load × remaining_hours``;
    pacing to this cap ensures the export budget lasts the full window,
    maximising the energy absorbed by household consumption.

    Returns an integer power in watts, clamped to [100, max_power_w].
    """
    energy_kwh = (current_soc - min_soc) / 100.0 * battery_capacity_kwh
    if energy_kwh <= 0:
        return 100
    if remaining_hours <= 0:
        return max_power_w
    # When a feed-in energy limit constrains the session, cap the target
    # energy so the export budget is spread across the full window.  At
    # discharge power P with house load L, export rate = P − L and total
    # battery drain = P × T.  The maximum drain that keeps total export
    # within budget is feedin_remaining + L × T.  Using this cap means the
    # pacing naturally sets P = L + feedin/T, which is the power level that
    # maximises battery drain for a given export budget.
    if (
        feedin_remaining_kwh is not None
        and feedin_remaining_kwh >= 0
        and remaining_hours > 0
    ):
        house_absorption_kwh = max(0.0, net_consumption_kw) * remaining_hours
        max_drain_kwh = feedin_remaining_kwh + house_absorption_kwh
        if max_drain_kwh < energy_kwh:
            _LOGGER.debug(
                "Smart discharge: capping target energy %.2f kWh -> %.2f kWh "
                "(feedin_remaining=%.2f kWh, house_absorption=%.2f kWh)",
                energy_kwh,
                max_drain_kwh,
                feedin_remaining_kwh,
                house_absorption_kwh,
            )
            energy_kwh = max_drain_kwh
            if energy_kwh <= 0:
                return 100
    effective_hours = remaining_hours * (1 - headroom)
    if effective_hours <= 0:
        effective_hours = remaining_hours
    battery_power_kw = energy_kwh / effective_hours
    # House load assists discharge — subtract it from needed inverter power.
    battery_power_kw -= max(0.0, net_consumption_kw)
    if battery_power_kw <= 0:
        return 100
    battery_power_kw *= 1 + headroom
    power_w = battery_power_kw * 1000
    return max(100, min(int(power_w), max_power_w))


def _calculate_deferred_start(
    current_soc: float,
    target_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    end: datetime.datetime,
    net_consumption_kw: float = 0.0,
    start: datetime.datetime | None = None,
    headroom: float = 0.10,
) -> datetime.datetime:
    """Calculate the latest time to start charging to reach target SoC by *end*.

    *headroom* is a fraction (e.g. 0.10 for 10%) controlling how much
    spare capacity to reserve for transient loads.

    Uses *net_consumption_kw* (house load minus solar) to estimate how
    much inverter capacity is available for charging.  A minimum headroom
    of *headroom* × *max_power_w* is always reserved.

    When *start* is provided the result is clamped so it never precedes
    the window opening time.

    Returns *end* if no charging is needed (SoC already at target).
    Returns a time before *end* otherwise; may be in the past if the
    window is too short to reach the target.
    """
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    if energy_needed_kwh <= 0:
        return end
    max_power_kw = max_power_w / 1000.0
    consumption_headroom_kw = max(0.0, net_consumption_kw)
    min_headroom_kw = max_power_kw * headroom
    headroom_kw = max(consumption_headroom_kw, min_headroom_kw)
    effective_charge_kw = max_power_kw - headroom_kw
    if effective_charge_kw <= 0:
        effective_charge_kw = max_power_kw * headroom
    charge_hours = energy_needed_kwh / effective_charge_kw
    # Add a time buffer so that transient load spikes don't prevent
    # reaching the target.  This matches the headroom used by
    # _calculate_charge_power when sizing the charge rate.
    buffered_hours = charge_hours / (1 - headroom)
    deferred = end - datetime.timedelta(hours=buffered_hours)
    if start is not None and deferred < start:
        deferred = start
    return deferred


def _remove_mode_from_schedule(
    inverter: Inverter,
    mode: WorkMode,
    min_soc_on_grid: int,
) -> None:
    """Remove all groups of *mode* from the schedule, keeping other modes.

    If no groups remain after filtering, falls back to ``self_use``.
    This is a blocking call — use via ``async_add_executor_job``.

    This function is only used in cloud-API mode.  In entity mode,
    callers use ``_async_remove_override`` instead.
    """
    schedule = inverter.get_schedule()
    raw_groups = schedule.get("groups", [])
    _LOGGER.debug(
        "Removing %s: current schedule has %d groups: %s",
        mode.value,
        len(raw_groups),
        raw_groups,
    )
    _check_schedule_safe(raw_groups)
    kept: list[ScheduleGroup] = []
    for raw_group in raw_groups:
        if _is_placeholder(raw_group):
            continue
        if raw_group.get("workMode") == mode.value:
            continue
        group = _sanitize_group(raw_group)
        group["enable"] = 1
        kept.append(group)
    if kept:
        _LOGGER.debug("After filtering: %d groups remain: %s", len(kept), kept)
        inverter.set_schedule(kept)
    else:
        _LOGGER.debug(
            "No groups remain after removing %s, reverting to SelfUse", mode.value
        )
        inverter.self_use(min_soc_on_grid)


async def _async_remove_override(
    hass: HomeAssistant,
    mode: WorkMode,
) -> None:
    """Remove a work-mode override, dispatching to cloud or entity backend."""
    if _is_entity_mode(hass):
        await _apply_mode_via_entities(hass, WorkMode.SELF_USE)
    else:
        inverter = _get_inverter(hass)
        min_soc_on_grid = _get_min_soc_on_grid(hass)
        await hass.async_add_executor_job(
            _remove_mode_from_schedule,
            inverter,
            mode,
            min_soc_on_grid,
        )


def _cancel_smart_charge(hass: HomeAssistant, *, clear_storage: bool = True) -> None:
    """Cancel any active smart charge listeners and clear stored session."""
    unsubs: list[Callable[[], None]] = hass.data[DOMAIN].get("_smart_charge_unsubs", [])
    for unsub in unsubs:
        unsub()
    hass.data[DOMAIN]["_smart_charge_unsubs"] = []
    hass.data[DOMAIN].pop("_smart_charge_state", None)
    if clear_storage and hass.data.get(DOMAIN, {}).get("_store") is not None:
        hass.async_create_task(_clear_stored_session(hass, "smart_charge"))


def _to_minutes(hour: int, minute: int) -> int:
    """Convert hour:minute to minutes since midnight."""
    return hour * 60 + minute


def _groups_overlap(a: ScheduleGroup, b: ScheduleGroup) -> bool:
    """Check whether two schedule groups have overlapping time windows."""
    a_start = _to_minutes(a["startHour"], a["startMinute"])
    a_end = _to_minutes(a["endHour"], a["endMinute"])
    b_start = _to_minutes(b["startHour"], b["startMinute"])
    b_end = _to_minutes(b["endHour"], b["endMinute"])
    return a_start < b_end and b_start < a_end


_SCHEDULE_GROUP_KEYS = {
    "enable",
    "startHour",
    "startMinute",
    "endHour",
    "endMinute",
    "workMode",
    "minSocOnGrid",
    "fdSoc",
    "fdPwr",
}

# Work modes that represent API placeholders, not real schedule entries.
_PLACEHOLDER_MODES = {"Invalid", ""}


def _is_placeholder(group: dict[str, Any]) -> bool:
    """Check if a group is an API placeholder (not a real schedule entry).

    The FoxESS API always returns 8 groups.  Unused slots come back as
    either ``workMode: "Invalid"`` / ``""`` **or** as zero-duration
    ``SelfUse`` groups (00:00–00:00).  Both forms must be filtered out
    when re-writing the schedule; leaving the zero-duration SelfUse
    groups in causes API error 42023 ("Time overlap").
    """
    if group.get("workMode", "") in _PLACEHOLDER_MODES:
        return True
    # Zero-duration window — start and end are identical.  Only check when
    # at least one time key is present (test dicts may omit them).
    if any(k in group for k in ("startHour", "startMinute", "endHour", "endMinute")):
        start = group.get("startHour", 0) * 60 + group.get("startMinute", 0)
        end = group.get("endHour", 0) * 60 + group.get("endMinute", 0)
        if start == end:
            return True
    return False


def _sanitize_group(raw: dict[str, Any]) -> ScheduleGroup:
    """Strip unknown fields and fix invalid values in an API-returned group.

    The FoxESS API sometimes returns groups with fdSoc below its own minimum
    (11).  It accepts these on read but rejects them on write (errno 40257).
    Clamp fdSoc and ensure minSocOnGrid <= fdSoc so the schedule can be
    written back without errors.
    """
    group: ScheduleGroup = {k: raw[k] for k in _SCHEDULE_GROUP_KEYS if k in raw}  # type: ignore[assignment]
    if "fdSoc" in group:
        group["fdSoc"] = max(group["fdSoc"], DEFAULT_API_MIN_SOC)
    if "minSocOnGrid" in group and "fdSoc" in group:
        group["minSocOnGrid"] = min(group["minSocOnGrid"], group["fdSoc"])
    return group


def _check_schedule_safe(
    groups: list[dict[str, Any]],
    hass: HomeAssistant | None = None,
) -> None:
    """Raise if the schedule contains modes this integration does not manage.

    The integration assumes SelfUse is the baseline mode.  If the schedule
    contains groups with unmanaged modes (e.g. Backup), modifying the
    schedule could overwrite the user's intended configuration.

    When *hass* is provided a persistent notification is created so the
    user sees the problem in the HA UI even if the exception is caught
    silently by a smart-session callback.
    """
    for group in groups:
        if _is_placeholder(group):
            continue
        mode = group.get("workMode", "")
        if mode and mode not in _MANAGED_WORK_MODES:
            time_range = (
                f"{group.get('startHour', 0):02d}:{group.get('startMinute', 0):02d}"
                f"–{group.get('endHour', 0):02d}:{group.get('endMinute', 0):02d}"
            )
            message = (
                f"The inverter schedule contains a **{mode}** group "
                f"({time_range}) which is not managed by this integration. "
                f"FoxESS Control expects Self Use as the default work mode "
                f"and will not modify the schedule while an unmanaged mode "
                f"is present.\n\n"
                f"Please remove the '{mode}' schedule group via the "
                f"FoxESS app, then retry the operation."
            )
            if hass is not None:
                pn_create(
                    hass,
                    message=message,
                    title="FoxESS Control: unmanaged work mode detected",
                    notification_id="foxess_control_unmanaged_mode",
                )
            raise ServiceValidationError(message)


def _is_expired(group: ScheduleGroup) -> bool:
    """Check if a group's end time has already passed today.

    NOTE: This assumes same-day groups only (no midnight crossing).
    The _resolve_start_end / _resolve_start_end_explicit validators
    enforce this constraint.  If midnight-crossing is ever allowed,
    this function must be updated.
    """
    now = dt_util.now()
    group_end = _to_minutes(group["endHour"], group["endMinute"])
    current = _to_minutes(now.hour, now.minute)
    return group_end <= current


def _merge_with_existing(
    inverter: Inverter,
    new_group: ScheduleGroup,
    work_mode: WorkMode,
    force: bool = False,
) -> list[ScheduleGroup]:
    """Fetch the current schedule, remove same-mode groups, and merge.

    Placeholder, same-mode, and SelfUse groups are removed.  Other
    groups are kept and re-enabled — even if the API auto-disabled
    them after their time window — because they may represent
    recurring daily schedules.

    If *force* is True, overlapping groups of a different mode are
    silently removed instead of raising an error.

    Raises ServiceValidationError if any retained group of a
    *different* mode overlaps with the new time window (unless
    *force* is True).
    """
    schedule = inverter.get_schedule()
    existing: list[dict[str, Any]] = schedule.get("groups", [])
    _LOGGER.debug("Current schedule has %d groups: %s", len(existing), existing)
    _check_schedule_safe(existing)

    kept: list[ScheduleGroup] = []
    for raw_group in existing:
        if _is_placeholder(raw_group):
            continue
        group = _sanitize_group(raw_group)
        if group.get("workMode") == work_mode.value:
            _LOGGER.debug("Removing existing %s group", work_mode.value)
            continue
        if group.get("workMode") == WorkMode.SELF_USE.value:
            _LOGGER.debug("Dropping SelfUse baseline group")
            continue
        group["enable"] = 1
        if _groups_overlap(group, new_group):
            if force:
                _LOGGER.debug(
                    "Force-removing conflicting %s group", group.get("workMode")
                )
                continue
            raise ServiceValidationError(
                f"New {work_mode.value} window conflicts with an existing "
                f"{group.get('workMode')} override "
                f"({group['startHour']:02d}:{group['startMinute']:02d}"
                f"-{group['endHour']:02d}:{group['endMinute']:02d})"
            )
        kept.append(group)

    kept.append(new_group)
    _LOGGER.debug("Setting schedule with %d groups: %s", len(kept), kept)
    return kept


def _build_override_group(
    now: datetime.datetime,
    end: datetime.datetime,
    work_mode: WorkMode,
    inverter: Inverter,
    min_soc_on_grid: int,
    fd_soc: int,
    fd_pwr: int | None = None,
    api_min_soc: int = DEFAULT_API_MIN_SOC,
) -> ScheduleGroup:
    """Build a single ScheduleGroup for a timed override.

    The FoxESS API requires ``fdSoc >= api_min_soc`` and
    ``minSocOnGrid <= fdSoc``.
    """
    fd_soc = max(fd_soc, api_min_soc)
    min_soc_on_grid = min(min_soc_on_grid, fd_soc)
    return {
        "enable": 1,
        "startHour": now.hour,
        "startMinute": now.minute,
        "endHour": end.hour,
        "endMinute": end.minute,
        "workMode": work_mode.value,
        "minSocOnGrid": min_soc_on_grid,
        "fdSoc": fd_soc,
        "fdPwr": fd_pwr if fd_pwr is not None else inverter.max_power_w,
    }


def _setup_smart_charge_listeners(
    hass: HomeAssistant,
    inverter: Inverter | None,
) -> None:
    """Register HA listeners for an active smart charge session.

    Reads all parameters from ``hass.data[DOMAIN]["_smart_charge_state"]``.
    """
    state = hass.data[DOMAIN]["_smart_charge_state"]
    end: datetime.datetime = state["end"]
    end_utc = dt_util.as_utc(end)
    my_session_id: str = state["session_id"]

    async def _remove_charge_override() -> None:
        await _async_remove_override(hass, WorkMode.FORCE_CHARGE)

    def _is_my_session() -> bool:
        """Return True if our session is still the active one."""
        cur = hass.data[DOMAIN].get("_smart_charge_state")
        return cur is not None and cur.get("session_id") == my_session_id

    async def _on_charge_timer_expire(_now: datetime.datetime) -> None:
        if not _is_my_session():
            return
        _LOGGER.info("Smart charge: window ended, removing override")
        charging_started = (
            hass.data[DOMAIN]
            .get("_smart_charge_state", {})
            .get("charging_started", False)
        )
        _cancel_smart_charge(hass)
        if charging_started:
            await _remove_charge_override()

    async def _adjust_charge_power(
        _now: datetime.datetime,
    ) -> None:
        cur_state = hass.data[DOMAIN].get("_smart_charge_state")
        if cur_state is None or cur_state.get("session_id") != my_session_id:
            return

        cur_soc = _get_current_soc(hass)
        if cur_soc is None:
            cur_state["soc_unavailable_count"] = (
                cur_state.get("soc_unavailable_count", 0) + 1
            )
            if cur_state["soc_unavailable_count"] >= MAX_SOC_UNAVAILABLE_COUNT:
                _LOGGER.warning(
                    "Smart charge: SoC unavailable for %d checks, aborting",
                    cur_state["soc_unavailable_count"],
                )
                charging_started = cur_state.get("charging_started", False)
                if _is_my_session():
                    _cancel_smart_charge(hass)
                    if charging_started:
                        await _remove_charge_override()
                return
            _LOGGER.debug("Smart charge: SoC unavailable, skipping adjustment")
            return
        cur_state["soc_unavailable_count"] = 0

        if cur_soc >= cur_state["target_soc"]:
            if not cur_state.get("target_reached"):
                # Remove the ForceCharge override immediately to stop
                # unnecessary charging, but keep the session alive so we
                # can resume if SoC drops back below target (e.g. due to
                # household consumption or clouds reducing solar).
                if cur_state.get("charging_started", False):
                    await _remove_charge_override()
                    cur_state["groups"] = []
                cur_state["target_reached"] = True
                _LOGGER.info(
                    "Smart charge: SoC %.1f%% >= target %d%%, "
                    "charge stopped, monitoring until window ends",
                    cur_soc,
                    cur_state["target_soc"],
                )
            return
        if cur_state.get("target_reached"):
            # SoC dropped back below target — resume charging.
            cur_state["target_reached"] = False
            _LOGGER.info(
                "Smart charge: SoC %.1f%% dropped below target %d%%, resuming",
                cur_soc,
                cur_state["target_soc"],
            )
        cur_state.pop("soc_above_target_count", None)

        now_dt = dt_util.now()
        remaining = (cur_state["end"] - now_dt).total_seconds() / 3600.0
        if remaining <= 0:
            _LOGGER.info("Smart charge: window expired during adjustment, reverting")
            charging_started = cur_state.get("charging_started", False)
            if _is_my_session():
                _cancel_smart_charge(hass)
                if charging_started:
                    await _remove_charge_override()
            return

        net_consumption = _get_net_consumption(hass)

        if not cur_state["charging_started"]:
            # Check if it's time to start deferred charging
            headroom = _get_smart_headroom(hass)
            deferred = _calculate_deferred_start(
                cur_soc,
                cur_state["target_soc"],
                cur_state["battery_capacity_kwh"],
                cur_state["max_power_w"],
                cur_state["end"],
                net_consumption_kw=net_consumption,
                start=cur_state["start"],
                headroom=headroom,
            )
            if now_dt < deferred:
                _LOGGER.debug(
                    "Smart charge: deferring until ~%02d:%02d "
                    "(SoC=%.1f%%, net_consumption=%.2fkW, "
                    "capacity=%.1fkWh, max_power=%dW, headroom=%.0f%%)",
                    deferred.hour,
                    deferred.minute,
                    cur_soc,
                    net_consumption,
                    cur_state["battery_capacity_kwh"],
                    cur_state["max_power_w"],
                    headroom * 100,
                )
                return

            # Time to start charging — build and set the schedule
            new_power = _calculate_charge_power(
                cur_soc,
                cur_state["target_soc"],
                cur_state["battery_capacity_kwh"],
                remaining,
                cur_state["max_power_w"],
                net_consumption_kw=net_consumption,
                headroom=headroom,
            )
            if _is_entity_mode(hass):
                await _apply_mode_via_entities(
                    hass,
                    WorkMode.FORCE_CHARGE,
                    new_power,
                )
                groups: list[ScheduleGroup] = []
            else:
                assert inverter is not None  # cloud mode
                group = _build_override_group(
                    now_dt,
                    cur_state["end"],
                    WorkMode.FORCE_CHARGE,
                    inverter,
                    cur_state["min_soc_on_grid"],
                    fd_soc=100,
                    fd_pwr=new_power,
                    api_min_soc=cur_state.get("api_min_soc", DEFAULT_API_MIN_SOC),
                )
                try:
                    groups = await hass.async_add_executor_job(
                        _merge_with_existing,
                        inverter,
                        group,
                        WorkMode.FORCE_CHARGE,
                        cur_state.get("force", False),
                    )
                except ServiceValidationError:
                    _LOGGER.warning(
                        "Smart charge: failed to start deferred charge "
                        "(cloud mode, new_group=%s), aborting",
                        group,
                        exc_info=True,
                    )
                    pn_create(
                        hass,
                        message=(
                            "Smart charge could not start because the "
                            "inverter schedule contains an unmanaged work "
                            "mode. Check the FoxESS app and remove any "
                            "non-Self Use schedule groups."
                        ),
                        title="FoxESS Control: schedule conflict",
                        notification_id="foxess_control_unmanaged_mode",
                    )
                    _cancel_smart_charge(hass)
                    return
                except Exception:
                    _LOGGER.warning(
                        "Smart charge: failed to start deferred charge "
                        "(cloud mode, new_group=%s), aborting",
                        group,
                        exc_info=True,
                    )
                    _cancel_smart_charge(hass)
                    return
                await hass.async_add_executor_job(inverter.set_schedule, groups)

            # Re-check state after await — may have been replaced concurrently
            if not _is_my_session():
                return
            cur_state = hass.data[DOMAIN]["_smart_charge_state"]

            cur_state["groups"] = groups
            cur_state["last_power_w"] = new_power
            cur_state["charging_started"] = True
            cur_state["charging_started_at"] = now_dt
            cur_state["charging_started_energy_kwh"] = (
                cur_soc / 100.0 * cur_state["battery_capacity_kwh"]
            )
            _LOGGER.info(
                "Smart charge: deferred charge started (SoC=%.1f%%, power=%dW)",
                cur_soc,
                new_power,
            )
            await _save_session(
                hass,
                "smart_charge",
                _session_data_from_charge_state(cur_state),
            )
            return

        # Already charging — adjust power as needed
        started_at = cur_state.get("charging_started_at")
        if started_at is not None:
            elapsed_since_start = (now_dt - started_at).total_seconds() / 3600.0
            window_from_start = (cur_state["end"] - started_at).total_seconds() / 3600.0
        else:
            elapsed_since_start = 0.0
            window_from_start = 0.0
        new_power = _calculate_charge_power(
            cur_soc,
            cur_state["target_soc"],
            cur_state["battery_capacity_kwh"],
            remaining,
            cur_state["max_power_w"],
            net_consumption_kw=net_consumption,
            headroom=_get_smart_headroom(hass),
            charging_started_energy_kwh=cur_state.get("charging_started_energy_kwh"),
            elapsed_since_charge_started=elapsed_since_start,
            effective_charge_window=window_from_start,
            min_power_change_w=cur_state["min_power_change"],
        )

        if (
            abs(new_power - cur_state["last_power_w"]) < cur_state["min_power_change"]
            and new_power != cur_state["max_power_w"]
        ):
            _LOGGER.debug(
                "Smart charge: power change %dW -> %dW below threshold %dW, skipping",
                cur_state["last_power_w"],
                new_power,
                cur_state["min_power_change"],
            )
            return

        if new_power != cur_state["last_power_w"]:
            _LOGGER.info(
                "Smart charge: adjusting power %dW -> %dW"
                " (SoC=%.1f%%, remaining=%.2fh)",
                cur_state["last_power_w"],
                new_power,
                cur_soc,
                remaining,
            )
        else:
            _LOGGER.debug(
                "Smart charge: holding at %dW (SoC=%.1f%%, remaining=%.2fh)",
                new_power,
                cur_soc,
                remaining,
            )

        cur_state["last_power_w"] = new_power
        if _is_entity_mode(hass):
            await _apply_mode_via_entities(
                hass,
                WorkMode.FORCE_CHARGE,
                new_power,
            )
        else:
            assert inverter is not None  # cloud mode
            groups = cur_state.get("groups") or []
            if groups:
                for g in groups:
                    if g.get("workMode") == WorkMode.FORCE_CHARGE.value:
                        g["fdPwr"] = new_power
                        break
            else:
                # Post-recovery: rebuild groups from the live schedule
                now_dt_adj = dt_util.now()
                group = _build_override_group(
                    now_dt_adj,
                    cur_state["end"],
                    WorkMode.FORCE_CHARGE,
                    inverter,
                    cur_state["min_soc_on_grid"],
                    fd_soc=100,
                    fd_pwr=new_power,
                    api_min_soc=cur_state.get("api_min_soc", DEFAULT_API_MIN_SOC),
                )
                try:
                    groups = await hass.async_add_executor_job(
                        _merge_with_existing,
                        inverter,
                        group,
                        WorkMode.FORCE_CHARGE,
                        cur_state.get("force", False),
                    )
                except ServiceValidationError:
                    _LOGGER.warning(
                        "Smart charge: conflict rebuilding schedule after "
                        "recovery (cloud mode, new_group=%s)",
                        group,
                        exc_info=True,
                    )
                    pn_create(
                        hass,
                        message=(
                            "Smart charge power adjustment failed because "
                            "the inverter schedule contains an unmanaged "
                            "work mode. Check the FoxESS app and remove "
                            "any non-Self Use schedule groups."
                        ),
                        title="FoxESS Control: schedule conflict",
                        notification_id="foxess_control_unmanaged_mode",
                    )
                    groups = []
                except Exception:
                    _LOGGER.warning(
                        "Smart charge: conflict rebuilding schedule after "
                        "recovery (cloud mode, new_group=%s)",
                        group,
                        exc_info=True,
                    )
                    groups = []

            if groups:
                cur_state["groups"] = groups
                await hass.async_add_executor_job(inverter.set_schedule, groups)
            # Re-check state after await — may have been replaced concurrently
            if not _is_my_session():
                return
        await _save_session(
            hass,
            "smart_charge",
            _session_data_from_charge_state(cur_state),
        )

    unsubs: list[Callable[[], None]] = [
        async_track_point_in_time(hass, _on_charge_timer_expire, end_utc),
        async_track_time_interval(
            hass, _adjust_charge_power, SMART_CHARGE_ADJUST_INTERVAL
        ),
    ]

    hass.data[DOMAIN]["_smart_charge_unsubs"] = unsubs


def _setup_smart_discharge_listeners(
    hass: HomeAssistant,
    inverter: Inverter | None,
) -> None:
    """Register HA listeners for an active smart discharge session.

    Reads all parameters from ``hass.data[DOMAIN]["_smart_discharge_state"]``.
    """
    state = hass.data[DOMAIN]["_smart_discharge_state"]
    end: datetime.datetime = state["end"]
    end_utc = dt_util.as_utc(end)
    my_session_id: str = state["session_id"]

    async def _remove_discharge_override() -> None:
        await _async_remove_override(hass, WorkMode.FORCE_DISCHARGE)

    def _is_my_session() -> bool:
        """Return True if our session is still the active one."""
        cur = hass.data[DOMAIN].get("_smart_discharge_state")
        return cur is not None and cur.get("session_id") == my_session_id

    def _log_session_end(reason: str) -> None:
        """Log a summary when the discharge session ends."""
        cur = hass.data[DOMAIN].get("_smart_discharge_state")
        feedin_str = ""
        if cur is not None:
            feedin_start = cur.get("feedin_start_kwh")
            if feedin_start is not None:
                feedin_now = _get_feedin_energy_kwh(hass)
                if feedin_now is not None:
                    total = feedin_now - feedin_start
                    feedin_str = f", fed in {total:.2f} kWh"
        _LOGGER.info("Smart discharge: %s%s", reason, feedin_str)

    async def _on_timer_expire(_now: datetime.datetime) -> None:
        if not _is_my_session():
            return
        _log_session_end("window ended, removing override")
        _cancel_smart_discharge(hass)
        await _remove_discharge_override()

    async def _check_discharge_soc(_now: datetime.datetime) -> None:
        """Periodic SoC and feed-in energy check from coordinator data."""
        cur_state = hass.data[DOMAIN].get("_smart_discharge_state")
        if cur_state is None or cur_state.get("session_id") != my_session_id:
            return

        # --- Check feed-in energy limit using cumulative counter ---
        feedin_remaining_for_pacing: float | None = None
        feedin_limit = cur_state.get("feedin_energy_limit_kwh")
        if feedin_limit is not None:
            feedin_now = _get_feedin_energy_kwh(hass)
            if feedin_now is not None:
                feedin_start = cur_state.get("feedin_start_kwh")
                if feedin_start is None:
                    # First reading — record baseline
                    _LOGGER.debug(
                        "Smart discharge: feed-in baseline captured at %.2f kWh",
                        feedin_now,
                    )
                    cur_state["feedin_start_kwh"] = feedin_now
                    feedin_remaining_for_pacing = feedin_limit
                    hass.async_create_task(
                        _save_session(
                            hass,
                            "smart_discharge",
                            _session_data_from_discharge_state(cur_state),
                        )
                    )
                else:
                    exported = feedin_now - feedin_start
                    if exported >= feedin_limit:
                        if _is_my_session():
                            _log_session_end(
                                f"feed-in energy {exported:.2f} kWh "
                                f"reached limit {feedin_limit:.2f} kWh, "
                                "removing override"
                            )
                            _cancel_smart_discharge(hass)
                            await _remove_discharge_override()
                        return

                    # --- Early stop to avoid overshoot ---
                    remaining_kwh = feedin_limit - exported
                    feedin_remaining_for_pacing = remaining_kwh
                    poll_seconds = _get_polling_interval_seconds(hass)
                    poll_hours = poll_seconds / 3600

                    # Use observed export rate from the feedin counter
                    # rather than the configured discharge power, since
                    # the actual rate is limited by the inverter's grid
                    # export limit and household consumption.
                    feedin_prev = cur_state.get("feedin_prev_kwh")
                    has_observed = feedin_prev is not None and feedin_now != feedin_prev
                    if has_observed:
                        observed_rate_kw = (feedin_now - feedin_prev) / poll_hours
                    cur_state["feedin_prev_kwh"] = feedin_now

                    if (
                        has_observed
                        and observed_rate_kw > 0
                        and remaining_kwh <= observed_rate_kw * poll_hours
                        and not cur_state.get("feedin_stop_scheduled")
                    ):
                        # Target will be reached before the next poll.
                        # Schedule a one-shot stop at the projected time.
                        seconds_to_target = remaining_kwh / observed_rate_kw * 3600
                        stop_at = dt_util.utcnow() + datetime.timedelta(
                            seconds=seconds_to_target
                        )
                        _LOGGER.info(
                            "Smart discharge: scheduling stop in %.0fs "
                            "(remaining=%.2f kWh, exported=%.2f kWh, "
                            "observed=%.1fkW)",
                            seconds_to_target,
                            remaining_kwh,
                            exported,
                            observed_rate_kw,
                        )

                        async def _early_stop(
                            _now: datetime.datetime,
                        ) -> None:
                            if not _is_my_session():
                                return
                            _log_session_end(
                                "early stop triggered (feed-in target ~reached)"
                            )
                            _cancel_smart_discharge(hass)
                            await _remove_discharge_override()

                        unsub = async_track_point_in_time(hass, _early_stop, stop_at)
                        hass.data[DOMAIN].setdefault(
                            "_smart_discharge_unsubs", []
                        ).append(unsub)
                        cur_state["feedin_stop_scheduled"] = True

        # --- Power pacing ---
        soc_value = _get_current_soc(hass)
        if soc_value is None:
            return
        if cur_state.get("pacing_enabled") and soc_value > cur_state["min_soc"]:
            now_dt = dt_util.now()
            remaining_h = (cur_state["end"] - now_dt).total_seconds() / 3600.0
            net_consumption = _get_net_consumption(hass)
            headroom = _get_smart_headroom(hass)

            # --- Suspend / resume ---
            # If house consumption alone would drain to min SoC within
            # the window, suspend forced discharge to protect the floor.
            should_suspend = remaining_h > 0 and _should_suspend_discharge(
                soc_value,
                cur_state["min_soc"],
                cur_state["battery_capacity_kwh"],
                remaining_h,
                net_consumption,
                headroom=headroom,
            )
            was_suspended = cur_state.get("suspended", False)

            if should_suspend and not was_suspended:
                _LOGGER.info(
                    "Smart discharge: suspending — house consumption "
                    "(%.2f kW) would breach min SoC %d%% "
                    "(SoC=%.1f%%, remaining=%.2fh)",
                    net_consumption,
                    cur_state["min_soc"],
                    soc_value,
                    remaining_h,
                )
                cur_state["suspended"] = True
                await _remove_discharge_override()
                if not _is_my_session():
                    return
                cur_state = hass.data[DOMAIN]["_smart_discharge_state"]
                await _save_session(
                    hass,
                    "smart_discharge",
                    _session_data_from_discharge_state(cur_state),
                )
            elif was_suspended and not should_suspend:
                _LOGGER.info(
                    "Smart discharge: resuming — conditions improved "
                    "(consumption=%.2f kW, SoC=%.1f%%, remaining=%.2fh)",
                    net_consumption,
                    soc_value,
                    remaining_h,
                )
                cur_state["suspended"] = False
                # Fall through to pacing to re-apply the override

            if cur_state.get("suspended"):
                # Still suspended — skip pacing, let SoC check handle
                # the case where SoC has already dropped to min.
                pass
            elif remaining_h > 0:
                new_power = _calculate_discharge_power(
                    soc_value,
                    cur_state["min_soc"],
                    cur_state["battery_capacity_kwh"],
                    remaining_h,
                    cur_state["max_power_w"],
                    net_consumption_kw=net_consumption,
                    headroom=headroom,
                    feedin_remaining_kwh=feedin_remaining_for_pacing,
                )
                min_change = cur_state.get("min_power_change", DEFAULT_MIN_POWER_CHANGE)
                power_delta = abs(new_power - cur_state["last_power_w"])
                should_update = (
                    power_delta >= min_change or new_power == cur_state["max_power_w"]
                ) and new_power != cur_state["last_power_w"]
                # Always re-apply when resuming from suspension
                if was_suspended and not cur_state.get("suspended"):
                    should_update = True
                if should_update:
                    _LOGGER.info(
                        "Smart discharge: adjusting power %dW -> %dW "
                        "(SoC=%.1f%%, remaining=%.2fh)",
                        cur_state["last_power_w"],
                        new_power,
                        soc_value,
                        remaining_h,
                    )
                    cur_state["last_power_w"] = new_power
                    if _is_entity_mode(hass):
                        await _apply_mode_via_entities(
                            hass,
                            WorkMode.FORCE_DISCHARGE,
                            new_power,
                            fd_soc=_get_api_min_soc(hass),
                        )
                    elif inverter is not None:
                        for g in cur_state.get("groups") or []:
                            if g.get("workMode") == WorkMode.FORCE_DISCHARGE.value:
                                g["fdPwr"] = new_power
                                break
                        await hass.async_add_executor_job(
                            inverter.set_schedule,
                            cur_state["groups"],
                        )
                    # Re-check state after await — may have been replaced
                    if not _is_my_session():
                        return
                    cur_state = hass.data[DOMAIN]["_smart_discharge_state"]
                    await _save_session(
                        hass,
                        "smart_discharge",
                        _session_data_from_discharge_state(cur_state),
                    )
                else:
                    _LOGGER.debug(
                        "Smart discharge: power change %dW -> %dW "
                        "below threshold %dW, skipping",
                        cur_state["last_power_w"],
                        new_power,
                        min_change,
                    )

        # --- SoC threshold check ---
        if soc_value <= cur_state["min_soc"]:
            cur_state["soc_below_min_count"] = (
                cur_state.get("soc_below_min_count", 0) + 1
            )
            if cur_state["soc_below_min_count"] < 2:
                _LOGGER.debug(
                    "Smart discharge: SoC %.1f%% <= threshold %d%% "
                    "(count=%d, waiting for confirmation)",
                    soc_value,
                    cur_state["min_soc"],
                    cur_state["soc_below_min_count"],
                )
                return
            if _is_my_session():
                _log_session_end(
                    f"SoC {soc_value:.1f}% confirmed at/below "
                    f"threshold {cur_state['min_soc']}%, removing override"
                )
                _cancel_smart_discharge(hass)
                await _remove_discharge_override()
        else:
            cur_state["soc_below_min_count"] = 0

    unsubs: list[Callable[[], None]] = [
        async_track_time_interval(
            hass,
            _check_discharge_soc,
            SMART_DISCHARGE_CHECK_INTERVAL,
        ),
        async_track_point_in_time(hass, _on_timer_expire, end_utc),
    ]

    hass.data[DOMAIN]["_smart_discharge_unsubs"] = unsubs


def _has_matching_schedule_group(
    inverter: Inverter,
    work_mode: WorkMode,
    end_hour: int,
    end_minute: int,
) -> bool:
    """Check if the inverter has a matching schedule group still active."""
    schedule = inverter.get_schedule()
    for group in schedule.get("groups", []):
        if _is_placeholder(group):
            continue
        if (
            group.get("workMode") == work_mode.value
            and group.get("endHour") == end_hour
            and group.get("endMinute") == end_minute
        ):
            return True
    return False


async def _recover_charge_session(
    hass: HomeAssistant,
    inverter: Inverter | None,
    charge_data: dict[str, Any],
    stored: dict[str, Any],
    now: datetime.datetime,
    today_str: str,
    changed: bool,
) -> bool:
    """Recover or discard a persisted smart charge session.

    Returns the (possibly updated) *changed* flag.
    Raises KeyError/TypeError/ValueError on corrupted data.
    """
    if charge_data.get("date") != today_str:
        _LOGGER.info(
            "Smart charge: stale session from %s, cleaning up",
            charge_data.get("date"),
        )
        del stored["smart_charge"]
        return True

    end = now.replace(
        hour=charge_data["end_hour"],
        minute=charge_data["end_minute"],
        second=0,
        microsecond=0,
    )
    if now >= end:
        _LOGGER.info("Smart charge: session window has passed, cleaning up")
        if _is_entity_mode(hass):
            await _async_remove_override(hass, WorkMode.FORCE_CHARGE)
        else:
            assert inverter is not None  # cloud mode
            min_soc_on_grid = _get_min_soc_on_grid(hass)
            try:
                await hass.async_add_executor_job(
                    _remove_mode_from_schedule,
                    inverter,
                    WorkMode.FORCE_CHARGE,
                    min_soc_on_grid,
                )
            except Exception:
                _LOGGER.exception("Smart charge: failed to clean up expired schedule")
        del stored["smart_charge"]
        return True

    # Window still active — check if session should be resumed
    start = now.replace(
        hour=charge_data["start_hour"],
        minute=charge_data["start_minute"],
        second=0,
        microsecond=0,
    )
    if _is_entity_mode(hass):
        # Entity mode: no schedule groups to check, always resume
        has_group = True
    else:
        assert inverter is not None  # cloud mode
        has_group = await hass.async_add_executor_job(
            _has_matching_schedule_group,
            inverter,
            WorkMode.FORCE_CHARGE,
            charge_data["end_hour"],
            charge_data["end_minute"],
        )
    if has_group or not charge_data.get("charging_started", False):
        _LOGGER.info(
            "Smart charge: resuming session %02d:%02d-%02d:%02d (target=%d%%)",
            charge_data["start_hour"],
            charge_data["start_minute"],
            charge_data["end_hour"],
            charge_data["end_minute"],
            charge_data.get("target_soc", 100),
        )
        remaining = (end - now).total_seconds() / 3600.0
        current_soc = _get_current_soc(hass)

        max_power = charge_data.get("max_power_w", 10000)
        target_soc = charge_data.get("target_soc", 100)
        capacity = charge_data.get("battery_capacity_kwh", 0.0)
        started_at_str = charge_data.get("charging_started_at")
        started_at = dt_util.parse_datetime(started_at_str) if started_at_str else None
        started_energy = charge_data.get("charging_started_energy_kwh")
        if not charge_data.get("charging_started", False):
            last_power = 0
        elif current_soc is not None:
            if started_at is not None:
                elapsed_since_start = (now - started_at).total_seconds() / 3600.0
                window_from_start = (end - started_at).total_seconds() / 3600.0
            else:
                elapsed_since_start = 0.0
                window_from_start = 0.0
            last_power = _calculate_charge_power(
                current_soc,
                target_soc,
                capacity,
                remaining,
                max_power,
                net_consumption_kw=_get_net_consumption(hass),
                headroom=_get_smart_headroom(hass),
                charging_started_energy_kwh=started_energy,
                elapsed_since_charge_started=elapsed_since_start,
                effective_charge_window=window_from_start,
                min_power_change_w=charge_data.get(
                    "min_power_change", DEFAULT_MIN_POWER_CHANGE
                ),
            )
        else:
            last_power = max_power

        hass.data[DOMAIN]["_smart_charge_state"] = {
            "session_id": str(uuid.uuid4()),
            "groups": [],
            "start": start,
            "end": end,
            "target_soc": target_soc,
            "battery_capacity_kwh": capacity,
            "max_power_w": max_power,
            "last_power_w": last_power,
            "min_soc_on_grid": charge_data.get(
                "min_soc_on_grid", DEFAULT_MIN_SOC_ON_GRID
            ),
            "min_power_change": charge_data.get(
                "min_power_change", DEFAULT_MIN_POWER_CHANGE
            ),
            "api_min_soc": charge_data.get("api_min_soc", DEFAULT_API_MIN_SOC),
            "charging_started": charge_data.get("charging_started", False),
            "charging_started_at": started_at,
            "charging_started_energy_kwh": started_energy,
            "force": charge_data.get("force", False),
            "soc_unavailable_count": 0,
            "soc_above_target_count": 0,
        }
        _setup_smart_charge_listeners(hass, inverter)
    else:
        _LOGGER.info(
            "Smart charge: no matching schedule group on inverter, discarding session"
        )
        del stored["smart_charge"]
        return True

    return changed


async def _recover_discharge_session(
    hass: HomeAssistant,
    inverter: Inverter | None,
    discharge_data: dict[str, Any],
    stored: dict[str, Any],
    now: datetime.datetime,
    today_str: str,
    changed: bool,
) -> bool:
    """Recover or discard a persisted smart discharge session.

    Returns the (possibly updated) *changed* flag.
    Raises KeyError/TypeError/ValueError on corrupted data.
    """
    if discharge_data.get("date") != today_str:
        _LOGGER.info(
            "Smart discharge: stale session from %s, cleaning up",
            discharge_data.get("date"),
        )
        del stored["smart_discharge"]
        return True

    end = now.replace(
        hour=discharge_data["end_hour"],
        minute=discharge_data["end_minute"],
        second=0,
        microsecond=0,
    )
    if now >= end:
        _LOGGER.info("Smart discharge: session window has passed, cleaning up")
        if _is_entity_mode(hass):
            await _async_remove_override(hass, WorkMode.FORCE_DISCHARGE)
        else:
            assert inverter is not None  # cloud mode
            min_soc_on_grid = _get_min_soc_on_grid(hass)
            try:
                await hass.async_add_executor_job(
                    _remove_mode_from_schedule,
                    inverter,
                    WorkMode.FORCE_DISCHARGE,
                    min_soc_on_grid,
                )
            except Exception:
                _LOGGER.exception(
                    "Smart discharge: failed to clean up expired schedule"
                )
        del stored["smart_discharge"]
        return True

    if _is_entity_mode(hass):
        has_group = True
    else:
        assert inverter is not None  # cloud mode
        has_group = await hass.async_add_executor_job(
            _has_matching_schedule_group,
            inverter,
            WorkMode.FORCE_DISCHARGE,
            discharge_data["end_hour"],
            discharge_data["end_minute"],
        )
    if has_group:
        start = now.replace(
            hour=discharge_data["start_hour"],
            minute=discharge_data["start_minute"],
            second=0,
            microsecond=0,
        )
        _LOGGER.info(
            "Smart discharge: resuming session %02d:%02d-%02d:%02d (min_soc=%d%%)",
            discharge_data["start_hour"],
            discharge_data["start_minute"],
            discharge_data["end_hour"],
            discharge_data["end_minute"],
            discharge_data.get("min_soc", 10),
        )
        recovered_power = discharge_data.get("last_power_w", 0)
        pacing_enabled = discharge_data.get("pacing_enabled", False)
        battery_capacity_kwh = discharge_data.get("battery_capacity_kwh", 0.0)
        min_soc = discharge_data.get("min_soc", 10)
        max_power_w = discharge_data.get("max_power_w", recovered_power)

        if pacing_enabled and battery_capacity_kwh > 0:
            current_soc = _get_current_soc(hass)
            remaining_h = (end - now).total_seconds() / 3600.0
            # Compute remaining feedin budget for pacing
            feedin_remaining: float | None = None
            feedin_limit = discharge_data.get("feedin_energy_limit_kwh")
            if feedin_limit is not None:
                feedin_start = discharge_data.get("feedin_start_kwh")
                feedin_now = _get_feedin_energy_kwh(hass)
                if feedin_start is not None and feedin_now is not None:
                    feedin_remaining = feedin_limit - (feedin_now - feedin_start)
                else:
                    feedin_remaining = feedin_limit
            if current_soc is not None and remaining_h > 0:
                recovered_power = _calculate_discharge_power(
                    current_soc,
                    min_soc,
                    battery_capacity_kwh,
                    remaining_h,
                    max_power_w,
                    net_consumption_kw=_get_net_consumption(hass),
                    headroom=_get_smart_headroom(hass),
                    feedin_remaining_kwh=feedin_remaining,
                )

        hass.data[DOMAIN]["_smart_discharge_state"] = {
            "session_id": str(uuid.uuid4()),
            "groups": [],
            "start": start,
            "end": end,
            "min_soc": min_soc,
            "max_power_w": max_power_w,
            "last_power_w": recovered_power,
            "soc_below_min_count": 0,
            "feedin_energy_limit_kwh": discharge_data.get("feedin_energy_limit_kwh"),
            "feedin_start_kwh": discharge_data.get("feedin_start_kwh"),
            "pacing_enabled": pacing_enabled,
            "battery_capacity_kwh": battery_capacity_kwh,
            "min_power_change": discharge_data.get(
                "min_power_change", DEFAULT_MIN_POWER_CHANGE
            ),
        }
        _setup_smart_discharge_listeners(hass, inverter)
    else:
        _LOGGER.info(
            "Smart discharge: no matching schedule group on inverter, "
            "discarding session"
        )
        del stored["smart_discharge"]
        return True

    return changed


async def _recover_sessions(
    hass: HomeAssistant,
    inverter: Inverter | None,
) -> None:
    """Recover or clean up smart sessions persisted before a restart."""
    store: Store[dict[str, Any]] | None = hass.data[DOMAIN].get("_store")
    if store is None:
        return

    stored: dict[str, Any] | None = await store.async_load()
    if not stored:
        return

    now = dt_util.now()
    today_str = now.strftime("%Y-%m-%d")
    changed = False

    # --- Smart charge recovery ---
    charge_data = stored.get("smart_charge")
    if charge_data is not None:
        try:
            changed = await _recover_charge_session(
                hass, inverter, charge_data, stored, now, today_str, changed
            )
        except (KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Smart charge: corrupted session data, discarding "
                "(backend=%s, data=%s): %s",
                "entity" if _is_entity_mode(hass) else "cloud",
                charge_data,
                exc,
            )
            stored.pop("smart_charge", None)
            changed = True

    # --- Smart discharge recovery ---
    discharge_data = stored.get("smart_discharge")
    if discharge_data is not None:
        try:
            changed = await _recover_discharge_session(
                hass, inverter, discharge_data, stored, now, today_str, changed
            )
        except (KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "Smart discharge: corrupted session data, discarding "
                "(backend=%s, data=%s): %s",
                "entity" if _is_entity_mode(hass) else "cloud",
                discharge_data,
                exc,
            )
            stored.pop("smart_discharge", None)
            changed = True

    if changed:
        await store.async_save(stored)


def _build_entity_map(opts: Any) -> dict[str, str]:
    """Build a {polled_variable: entity_id} map from config options.

    Returns an empty dict when entity mode is not configured.
    """
    from .const import (
        CONF_FEEDIN_ENERGY_ENTITY,
        CONF_LOADS_POWER_ENTITY,
        CONF_PV_POWER_ENTITY,
        CONF_SOC_ENTITY,
    )

    if not opts.get(CONF_WORK_MODE_ENTITY):
        return {}

    mapping: dict[str, str] = {}
    # Map config option → polled variable name
    _ENTITY_VAR_MAP: list[tuple[str, str]] = [
        (CONF_SOC_ENTITY, "SoC"),
        (CONF_LOADS_POWER_ENTITY, "loadsPower"),
        (CONF_PV_POWER_ENTITY, "pvPower"),
        (CONF_FEEDIN_ENERGY_ENTITY, "feedin"),
        (CONF_WORK_MODE_ENTITY, "_work_mode"),
    ]
    for conf_key, var_name in _ENTITY_VAR_MAP:
        entity_id = opts.get(conf_key, "")
        if entity_id:
            mapping[var_name] = entity_id
    return mapping


# -- WebSocket API for Lovelace cards ----------------------------------------

# Map of role → unique_id suffix used by the integration's sensor entities.
_ENTITY_ROLES: dict[str, str] = {
    "solar_power": "pv_power",
    "house_load": "loads_power",
    "grid_consumption": "grid_consumption",
    "grid_feed_in": "feedin_power",
    "charge_rate": "bat_charge_power",
    "discharge_rate": "bat_discharge_power",
    "battery_soc": "battery_soc",
    "work_mode": "work_mode",
    "pv1_power": "pv1_power",
    "pv2_power": "pv2_power",
    "grid_voltage": "grid_voltage",
    "grid_frequency": "grid_frequency",
    "battery_temperature": "bat_temperature",
    "smart_operations": "smart_operations",
    "battery_forecast": "battery_forecast",
}


def _register_websocket_api(hass: HomeAssistant) -> None:
    """Register WebSocket commands for Lovelace card entity discovery."""
    from homeassistant.components.websocket_api import (  # type: ignore[attr-defined]
        async_register_command,
        async_response,
        websocket_command,
    )
    from homeassistant.helpers import entity_registry as er

    @websocket_command({vol.Required("type"): f"{DOMAIN}/entity_map"})
    @async_response
    async def ws_entity_map(
        hass: HomeAssistant,
        connection: Any,
        msg: dict[str, Any],
    ) -> None:
        """Return a role->entity_id mapping for foxess_control entities."""
        registry = er.async_get(hass)
        # Find the config entry id
        entry_id: str | None = None
        for key in hass.data.get(DOMAIN, {}):
            if not str(key).startswith("_"):
                entry_id = key
                break

        result: dict[str, str] = {}
        if entry_id is not None:
            entries = er.async_entries_for_config_entry(registry, entry_id)
            # Build suffix -> entity_id lookup
            suffix_map: dict[str, str] = {}
            for ent in entries:
                for suffix in _ENTITY_ROLES.values():
                    if ent.unique_id.endswith(f"_{suffix}"):
                        suffix_map[suffix] = ent.entity_id
                        break
            # Map role -> entity_id
            for role, suffix in _ENTITY_ROLES.items():
                if suffix in suffix_map:
                    result[role] = suffix_map[suffix]

        connection.send_result(msg["id"], result)

    async_register_command(hass, ws_entity_map)


# -- Lovelace card frontend --------------------------------------------------

_CARD_URLS = [
    f"/{DOMAIN}/foxess-control-card.js",
    f"/{DOMAIN}/foxess-overview-card.js",
]


async def _register_card_frontend(hass: HomeAssistant) -> None:
    """Serve the custom Lovelace card JS files and register them as resources."""
    import json
    from pathlib import Path

    from homeassistant.components.http import StaticPathConfig

    card_dir = Path(__file__).parent
    static_paths = []
    for card_url in _CARD_URLS:
        filename = card_url.rsplit("/", 1)[-1]
        card_path = card_dir / "www" / filename
        static_paths.append(
            StaticPathConfig(card_url, str(card_path), cache_headers=True)
        )
    await hass.http.async_register_static_paths(static_paths)

    # Read version from manifest for cache-busting query parameter.
    try:
        raw = await hass.async_add_executor_job((card_dir / "manifest.json").read_text)
        manifest = json.loads(raw)
        version = manifest.get("version", "0")
    except Exception:
        version = "0"

    # Auto-register as Lovelace resources (storage mode only).
    try:
        import importlib

        _ll_mod = importlib.import_module("homeassistant.components.lovelace")
        LOVELACE_DATA = _ll_mod.LOVELACE_DATA

        ll_data = hass.data.get(LOVELACE_DATA)
        if ll_data is not None and hasattr(ll_data.resources, "async_create_item"):
            for card_url in _CARD_URLS:
                versioned_url = f"{card_url}?v={version}"
                # Remove any existing registration (may have old version query)
                existing = [
                    r
                    for r in ll_data.resources.async_items()
                    if card_url in r.get("url", "")
                ]
                for r in existing:
                    if r.get("url") != versioned_url:
                        await ll_data.resources.async_delete_item(r["id"])
                # Register with versioned URL if not already present
                current = [
                    r
                    for r in ll_data.resources.async_items()
                    if r.get("url") == versioned_url
                ]
                if not current:
                    await ll_data.resources.async_create_item(
                        {"res_type": "module", "url": versioned_url}
                    )
                    _LOGGER.info("Registered Lovelace resource: %s", versioned_url)
    except Exception:
        _LOGGER.debug(
            "Could not auto-register Lovelace resources; "
            "add them manually as module resources",
            exc_info=True,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FoxESS Control from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("_smart_discharge_unsubs", [])
    hass.data[DOMAIN].setdefault("_smart_charge_unsubs", [])
    hass.data[DOMAIN].setdefault(
        "_store", Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
    )

    entity_map = _build_entity_map(entry.options)
    entity_mode = bool(entity_map)
    api_key = entry.data.get(CONF_API_KEY, "")

    inverter: Inverter | None = None
    if api_key:
        client = FoxESSClient(api_key)
        inverter = Inverter(client, entry.data[CONF_DEVICE_SERIAL])
        # Pre-warm max_power_w (validates connection, caches rated power)
        await hass.async_add_executor_job(lambda: inverter.max_power_w)
    elif not entity_mode:
        raise ServiceValidationError(
            "No API key configured and entity mode is not active. "
            "Provide a FoxESS Cloud API key or configure foxess_modbus entities."
        )

    # Choose coordinator and polling interval
    default_poll = (
        DEFAULT_ENTITY_POLLING_INTERVAL if entity_mode else DEFAULT_POLLING_INTERVAL
    )
    polling_interval = int(entry.options.get(CONF_POLLING_INTERVAL, default_poll))

    if entity_map:
        _LOGGER.info(
            "Entity mode active: reading from %d mapped entities",
            len(entity_map),
        )
        coordinator: FoxESSDataCoordinator | FoxESSEntityCoordinator = (
            FoxESSEntityCoordinator(hass, entity_map, polling_interval)
        )
    else:
        assert inverter is not None
        coordinator = FoxESSDataCoordinator(hass, inverter, polling_interval)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "inverter": inverter,
        "coordinator": coordinator,
    }

    # Register services, frontend card, and WS API once (first real entry)
    real_entries = {k for k in hass.data[DOMAIN] if not k.startswith("_")}
    if len(real_entries) == 1:
        _register_services(hass)
        _register_websocket_api(hass)
        await _register_card_frontend(hass)
    elif len(real_entries) > 1:
        _LOGGER.warning(
            "Multiple FoxESS Control entries detected. Services and smart "
            "sessions operate on the first configured inverter only."
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Recover smart sessions persisted before a restart
    await _recover_sessions(hass, inverter)

    # Reload entry when options change (picks up new polling interval)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data[DOMAIN].pop(entry.entry_id)

    # Only internal "_*" keys remain → last real entry was removed
    remaining = {k for k in hass.data[DOMAIN] if not k.startswith("_")}
    if not remaining:
        # Preserve persisted sessions so recovery works after options reload
        _cancel_smart_discharge(hass, clear_storage=False)
        _cancel_smart_charge(hass, clear_storage=False)
        # Detach debug log handlers and restore logger level
        fox_logger = logging.getLogger("custom_components.foxess_control")
        for handler in hass.data[DOMAIN].get("_debug_log_handlers", []):
            fox_logger.removeHandler(handler)
            original = getattr(handler, "_original_level", logging.NOTSET)
            fox_logger.setLevel(original)
        hass.data.pop(DOMAIN)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_OVERRIDES)
        hass.services.async_remove(DOMAIN, SERVICE_FEEDIN)
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_CHARGE)
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_DISCHARGE)
        hass.services.async_remove(DOMAIN, SERVICE_SMART_CHARGE)
        hass.services.async_remove(DOMAIN, SERVICE_SMART_DISCHARGE)

    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register inverter control services."""

    async def handle_clear_overrides(call: ServiceCall) -> None:
        mode_filter: str | None = call.data.get("mode")

        # Cancel smart listeners BEFORE any awaits to prevent old callbacks
        # from racing with the override removal.
        if mode_filter is None or mode_filter == WorkMode.FORCE_CHARGE.value:
            _cancel_smart_charge(hass)
        if mode_filter is None or mode_filter == WorkMode.FORCE_DISCHARGE.value:
            _cancel_smart_discharge(hass)

        if _is_entity_mode(hass):
            _LOGGER.info("Clearing overrides via entity backend, setting SelfUse")
            await _apply_mode_via_entities(hass, WorkMode.SELF_USE)
        elif mode_filter is None:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _get_min_soc_on_grid(hass)
            schedule = await hass.async_add_executor_job(inverter.get_schedule)
            _check_schedule_safe(schedule.get("groups", []), hass)
            _LOGGER.info("Clearing all overrides, setting SelfUse")
            await hass.async_add_executor_job(inverter.self_use, min_soc_on_grid)
        else:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _get_min_soc_on_grid(hass)
            _LOGGER.info("Clearing %s overrides", mode_filter)
            schedule = await hass.async_add_executor_job(inverter.get_schedule)
            _check_schedule_safe(schedule.get("groups", []), hass)
            kept: list[ScheduleGroup] = []
            for g in schedule.get("groups", []):
                if _is_placeholder(g):
                    continue
                if g.get("workMode") == mode_filter:
                    continue
                group = _sanitize_group(g)
                group["enable"] = 1
                kept.append(group)
            if kept:
                await hass.async_add_executor_job(inverter.set_schedule, kept)
            else:
                await hass.async_add_executor_job(inverter.self_use, min_soc_on_grid)

    async def handle_force_charge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        force: bool = call.data.get("replace_conflicts", False)
        start, end = _resolve_start_end(duration, start_time)

        _LOGGER.info(
            "Force charge %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )

        # Cancel smart charge BEFORE any awaits to prevent old callbacks
        # from racing with the schedule change.
        _cancel_smart_charge(hass)

        if _is_entity_mode(hass):
            await _apply_mode_via_entities(hass, WorkMode.FORCE_CHARGE, power)
        else:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _get_min_soc_on_grid(hass)
            api_min_soc = _get_api_min_soc(hass)

            group = _build_override_group(
                start,
                end,
                WorkMode.FORCE_CHARGE,
                inverter,
                min_soc_on_grid,
                fd_soc=100,
                fd_pwr=power,
                api_min_soc=api_min_soc,
            )
            groups = await hass.async_add_executor_job(
                _merge_with_existing,
                inverter,
                group,
                WorkMode.FORCE_CHARGE,
                force,
            )
            await hass.async_add_executor_job(inverter.set_schedule, groups)

    async def handle_force_discharge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        force: bool = call.data.get("replace_conflicts", False)
        start, end = _resolve_start_end(duration, start_time)

        _LOGGER.info(
            "Force discharge %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )

        # Cancel smart discharge BEFORE any awaits to prevent old callbacks
        # from racing with the schedule change.
        _cancel_smart_discharge(hass)

        if _is_entity_mode(hass):
            api_min_soc = _get_api_min_soc(hass)
            await _apply_mode_via_entities(
                hass,
                WorkMode.FORCE_DISCHARGE,
                power,
                fd_soc=api_min_soc,
            )
        else:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _get_min_soc_on_grid(hass)
            api_min_soc = _get_api_min_soc(hass)

            group = _build_override_group(
                start,
                end,
                WorkMode.FORCE_DISCHARGE,
                inverter,
                min_soc_on_grid,
                fd_soc=api_min_soc,
                fd_pwr=power,
                api_min_soc=api_min_soc,
            )
            groups = await hass.async_add_executor_job(
                _merge_with_existing,
                inverter,
                group,
                WorkMode.FORCE_DISCHARGE,
                force,
            )
            await hass.async_add_executor_job(inverter.set_schedule, groups)

    async def handle_feedin(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        force: bool = call.data.get("replace_conflicts", False)
        start, end = _resolve_start_end(duration, start_time)

        _LOGGER.info(
            "Feed-in %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )

        if _is_entity_mode(hass):
            await _apply_mode_via_entities(hass, WorkMode.FEEDIN, power)
        else:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _get_min_soc_on_grid(hass)
            api_min_soc = _get_api_min_soc(hass)

            group = _build_override_group(
                start,
                end,
                WorkMode.FEEDIN,
                inverter,
                min_soc_on_grid,
                fd_soc=api_min_soc,
                fd_pwr=power,
                api_min_soc=api_min_soc,
            )
            groups = await hass.async_add_executor_job(
                _merge_with_existing,
                inverter,
                group,
                WorkMode.FEEDIN,
                force,
            )
            await hass.async_add_executor_job(inverter.set_schedule, groups)

    async def handle_smart_discharge(call: ServiceCall) -> None:
        start_time: datetime.time = call.data["start_time"]
        end_time: datetime.time = call.data["end_time"]
        power: int | None = call.data.get("power")
        min_soc: int = call.data["min_soc"]
        force: bool = call.data.get("replace_conflicts", False)
        feedin_energy_limit: float | None = call.data.get("feedin_energy_limit_kwh")
        inverter: Inverter | None = None

        start, end = _resolve_start_end_explicit(start_time, end_time)

        if _get_current_soc(hass) is None:
            raise ServiceValidationError(
                "Battery SoC is not available. Wait for the API poll to complete."
            )

        api_min_soc = _get_api_min_soc(hass)

        feedin_str = (
            f", feedin_limit={feedin_energy_limit}kWh"
            if feedin_energy_limit is not None
            else ""
        )
        _LOGGER.info(
            "Smart discharge %02d:%02d - %02d:%02d (power=%s, min_soc=%d%%%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
            min_soc,
            feedin_str,
        )

        # Cancel any previous smart discharge listeners
        _cancel_smart_discharge(hass)

        # Cancel any active smart charge — the two sessions would conflict
        if hass.data[DOMAIN].get("_smart_charge_state") is not None:
            _LOGGER.info("Smart discharge: cancelling active smart charge session")
            _cancel_smart_charge(hass)

        max_power_w = power if power is not None else _get_max_power_w(hass)
        battery_capacity_kwh = _get_battery_capacity_kwh(hass)
        pacing_enabled = battery_capacity_kwh > 0

        # Calculate paced initial power when capacity is configured
        current_soc = _get_current_soc(hass)
        if pacing_enabled and current_soc is not None:
            now = dt_util.now()
            remaining = (end - now).total_seconds() / 3600.0
            initial_power = _calculate_discharge_power(
                current_soc,
                min_soc,
                battery_capacity_kwh,
                remaining,
                max_power_w,
                net_consumption_kw=_get_net_consumption(hass),
                headroom=_get_smart_headroom(hass),
                feedin_remaining_kwh=feedin_energy_limit,
            )
        else:
            initial_power = max_power_w

        groups: list[ScheduleGroup] = []
        if _is_entity_mode(hass):
            await _apply_mode_via_entities(
                hass,
                WorkMode.FORCE_DISCHARGE,
                initial_power,
                fd_soc=api_min_soc,
            )
        else:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _get_min_soc_on_grid(hass)
            group = _build_override_group(
                start,
                end,
                WorkMode.FORCE_DISCHARGE,
                inverter,
                min_soc_on_grid,
                fd_soc=api_min_soc,
                fd_pwr=initial_power,
                api_min_soc=api_min_soc,
            )
            groups = await hass.async_add_executor_job(
                _merge_with_existing,
                inverter,
                group,
                WorkMode.FORCE_DISCHARGE,
                force,
            )
            await hass.async_add_executor_job(inverter.set_schedule, groups)

        conditions = [
            f"window ends at {end.strftime('%H:%M')}",
            f"SoC drops to {min_soc}%",
        ]
        if feedin_energy_limit is not None:
            conditions.append(f"feed-in reaches {feedin_energy_limit} kWh")
        _LOGGER.debug(
            "Smart discharge: will stop when: %s",
            " OR ".join(conditions),
        )

        # Store state for binary sensor and diagnostics
        hass.data[DOMAIN]["_smart_discharge_state"] = {
            "session_id": str(uuid.uuid4()),
            "groups": groups,
            "start": start,
            "end": end,
            "min_soc": min_soc,
            "max_power_w": max_power_w,
            "last_power_w": initial_power,
            "soc_below_min_count": 0,
            "feedin_energy_limit_kwh": feedin_energy_limit,
            "feedin_start_kwh": _get_feedin_energy_kwh(hass),
            "battery_capacity_kwh": battery_capacity_kwh,
            "min_power_change": _get_min_power_change(hass),
            "pacing_enabled": pacing_enabled,
        }

        _setup_smart_discharge_listeners(hass, inverter)

        await _save_session(
            hass,
            "smart_discharge",
            _session_data_from_discharge_state(
                hass.data[DOMAIN]["_smart_discharge_state"]
            ),
        )

    async def handle_smart_charge(call: ServiceCall) -> None:
        start_time_val: datetime.time = call.data["start_time"]
        end_time_val: datetime.time = call.data["end_time"]
        max_power: int | None = call.data.get("power")
        target_soc: int = call.data["target_soc"]
        force: bool = call.data.get("replace_conflicts", False)
        inverter: Inverter | None = None

        start, end = _resolve_start_end_explicit(start_time_val, end_time_val)

        if _get_current_soc(hass) is None:
            raise ServiceValidationError(
                "Battery SoC is not available. Wait for the API poll to complete."
            )

        battery_capacity_kwh = _get_battery_capacity_kwh(hass)
        if battery_capacity_kwh <= 0:
            raise ServiceValidationError(
                "Battery capacity (kWh) not configured. Set it in the "
                "integration options before using smart charge."
            )

        min_soc_on_grid = _get_min_soc_on_grid(hass)
        api_min_soc = _get_api_min_soc(hass)
        effective_max_power = (
            max_power if max_power is not None else _get_max_power_w(hass)
        )

        # Read current SoC for initial power calculation and deferred start
        current_soc = _get_current_soc(hass)
        if current_soc is not None and current_soc >= target_soc:
            raise ServiceValidationError(
                f"Current SoC ({current_soc}%) already at or above "
                f"target ({target_soc}%)"
            )

        entity_mode = _is_entity_mode(hass)

        # Validate conflicts upfront (cloud mode only)
        if not entity_mode:
            inverter = _get_inverter(hass)
            validation_group = _build_override_group(
                start,
                end,
                WorkMode.FORCE_CHARGE,
                inverter,
                min_soc_on_grid,
                fd_soc=100,
                fd_pwr=effective_max_power,
                api_min_soc=api_min_soc,
            )
            await hass.async_add_executor_job(
                _merge_with_existing,
                inverter,
                validation_group,
                WorkMode.FORCE_CHARGE,
                force,
            )

        # Cancel previous sessions before any schedule mutation to prevent
        # old callbacks from racing with the new session's setup.
        _cancel_smart_charge(hass)
        if hass.data[DOMAIN].get("_smart_discharge_state") is not None:
            _LOGGER.info("Smart charge: cancelling active smart discharge session")
            _cancel_smart_discharge(hass)

        # Decide whether to start charging now or defer
        now = dt_util.now()
        net_consumption = _get_net_consumption(hass)
        headroom = _get_smart_headroom(hass)
        should_defer = False
        if current_soc is not None:
            deferred_start = _calculate_deferred_start(
                current_soc,
                target_soc,
                battery_capacity_kwh,
                effective_max_power,
                end,
                net_consumption_kw=net_consumption,
                start=start,
                headroom=headroom,
            )
            should_defer = now < deferred_start

        if should_defer:
            _LOGGER.info(
                "Smart charge %02d:%02d - %02d:%02d deferred until ~%02d:%02d "
                "(target_soc=%d%%, SoC=%.1f%%, capacity=%.1fkWh, "
                "max_power=%dW, headroom=%.0f%%)",
                start.hour,
                start.minute,
                end.hour,
                end.minute,
                deferred_start.hour,
                deferred_start.minute,
                target_soc,
                current_soc,
                battery_capacity_kwh,
                effective_max_power,
                headroom * 100,
            )
            initial_groups: list[ScheduleGroup] | None = None
            initial_power = 0
        else:
            remaining = (end - now).total_seconds() / 3600.0
            initial_power = effective_max_power
            if current_soc is not None:
                initial_power = _calculate_charge_power(
                    current_soc,
                    target_soc,
                    battery_capacity_kwh,
                    remaining,
                    effective_max_power,
                    net_consumption_kw=net_consumption,
                    headroom=headroom,
                )

            _LOGGER.info(
                "Smart charge %02d:%02d - %02d:%02d (power=%dW, target_soc=%d%%, "
                "SoC=%.1f%%, capacity=%.1fkWh)",
                start.hour,
                start.minute,
                end.hour,
                end.minute,
                initial_power,
                target_soc,
                current_soc if current_soc is not None else -1,
                battery_capacity_kwh,
            )

            if entity_mode:
                await _apply_mode_via_entities(
                    hass,
                    WorkMode.FORCE_CHARGE,
                    initial_power,
                )
                initial_groups = []
            else:
                assert inverter is not None  # cloud mode
                group = _build_override_group(
                    start,
                    end,
                    WorkMode.FORCE_CHARGE,
                    inverter,
                    min_soc_on_grid,
                    fd_soc=100,
                    fd_pwr=initial_power,
                    api_min_soc=api_min_soc,
                )
                initial_groups = await hass.async_add_executor_job(
                    _merge_with_existing,
                    inverter,
                    group,
                    WorkMode.FORCE_CHARGE,
                    force,
                )
                await hass.async_add_executor_job(
                    inverter.set_schedule,
                    initial_groups,
                )

        min_power_change = _get_min_power_change(hass)

        # Store state for periodic adjustments
        hass.data[DOMAIN]["_smart_charge_state"] = {
            "session_id": str(uuid.uuid4()),
            "groups": initial_groups,
            "start": start,
            "end": end,
            "target_soc": target_soc,
            "battery_capacity_kwh": battery_capacity_kwh,
            "max_power_w": effective_max_power,
            "last_power_w": initial_power,
            "min_soc_on_grid": min_soc_on_grid,
            "min_power_change": min_power_change,
            "api_min_soc": api_min_soc,
            "charging_started": not should_defer,
            "charging_started_at": None if should_defer else now,
            "charging_started_energy_kwh": (
                None
                if should_defer
                else (
                    current_soc / 100.0 * battery_capacity_kwh
                    if current_soc is not None
                    else None
                )
            ),
            "force": force,
            "soc_unavailable_count": 0,
            "soc_above_target_count": 0,
        }

        _setup_smart_charge_listeners(hass, inverter)

        await _save_session(
            hass,
            "smart_charge",
            _session_data_from_charge_state(hass.data[DOMAIN]["_smart_charge_state"]),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_OVERRIDES,
        handle_clear_overrides,
        schema=SCHEMA_CLEAR_OVERRIDES,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_FEEDIN, handle_feedin, schema=SCHEMA_FEEDIN
    )
    hass.services.async_register(
        DOMAIN, SERVICE_FORCE_CHARGE, handle_force_charge, schema=SCHEMA_FORCE_CHARGE
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_DISCHARGE,
        handle_force_discharge,
        schema=SCHEMA_FORCE_DISCHARGE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SMART_CHARGE,
        handle_smart_charge,
        schema=SCHEMA_SMART_CHARGE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SMART_DISCHARGE,
        handle_smart_discharge,
        schema=SCHEMA_SMART_DISCHARGE,
    )
