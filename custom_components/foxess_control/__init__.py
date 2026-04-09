"""FoxESS Control — Home Assistant integration for inverter mode management."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
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
    CONF_DEVICE_SERIAL,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    CONF_POLLING_INTERVAL,
    DEFAULT_API_MIN_SOC,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
    MAX_OVERRIDE_HOURS,
    PLATFORMS,
)
from .coordinator import FoxESSDataCoordinator, get_coordinator_soc
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

# Cancel a smart session if the SoC entity is unavailable for this many
# consecutive periodic checks (3 × 5 min = 15 minutes).
MAX_SOC_UNAVAILABLE_COUNT = 3

STORAGE_KEY = "foxess_control_sessions"
STORAGE_VERSION = 1

VALID_MODES = [m.value for m in WorkMode]

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


def _cancel_smart_discharge(hass: HomeAssistant) -> None:
    """Cancel any active smart discharge listeners and clear stored session."""
    unsubs: list[Callable[[], None]] = hass.data[DOMAIN].get(
        "_smart_discharge_unsubs", []
    )
    for unsub in unsubs:
        unsub()
    hass.data[DOMAIN]["_smart_discharge_unsubs"] = []
    hass.data[DOMAIN].pop("_smart_discharge_state", None)
    if hass.data.get(DOMAIN, {}).get("_store") is not None:
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
        "last_power_w": state["last_power_w"],
    }
    if state.get("feedin_energy_limit_kwh") is not None:
        data["feedin_energy_limit_kwh"] = state["feedin_energy_limit_kwh"]
        if state.get("feedin_start_kwh") is not None:
            data["feedin_start_kwh"] = state["feedin_start_kwh"]
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
) -> int:
    """Calculate the charge power needed to reach target SoC in remaining time.

    Aims to finish with a 10% time buffer so the inverter doesn't have to
    run at full capacity.  This matches the headroom used by
    ``_calculate_deferred_start`` so the two stay in agreement.

    When *net_consumption_kw* is positive the house is drawing power that
    competes with the battery for inverter capacity, so we add it to the
    required charge rate.  When negative (solar exceeds consumption) the
    battery gets a free boost — we don't subtract because the inverter
    will naturally absorb the surplus.

    Returns an integer power in watts, clamped to [100, max_power_w].
    """
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    if energy_needed_kwh <= 0:
        return 100
    if remaining_hours <= 0:
        return max_power_w
    # Plan to finish in 90% of the remaining time so there is a buffer
    # if consumption spikes or the inverter can't sustain full power.
    effective_hours = remaining_hours * (1 - DEFERRED_START_MIN_HEADROOM)
    if effective_hours <= 0:
        effective_hours = remaining_hours
    battery_power_kw = energy_needed_kwh / effective_hours
    total_power_kw = battery_power_kw + max(0.0, net_consumption_kw)
    power_w = total_power_kw * 1000
    return max(100, min(int(power_w), max_power_w))


# Minimum fraction of max power reserved as headroom when calculating
# the deferred start time.  Even when measured net consumption is zero
# we still plan with at least 10% spare capacity for transient loads.
DEFERRED_START_MIN_HEADROOM = 0.10


def _calculate_deferred_start(
    current_soc: float,
    target_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    end: datetime.datetime,
    net_consumption_kw: float = 0.0,
) -> datetime.datetime:
    """Calculate the latest time to start charging to reach target SoC by *end*.

    Uses *net_consumption_kw* (house load minus solar) to estimate how
    much inverter capacity is available for charging.  A minimum headroom
    of 10% of *max_power_w* is always reserved for transient loads.

    Returns *end* if no charging is needed (SoC already at target).
    Returns a time before *end* otherwise; may be in the past if the
    window is too short to reach the target.
    """
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    if energy_needed_kwh <= 0:
        return end
    max_power_kw = max_power_w / 1000.0
    consumption_headroom_kw = max(0.0, net_consumption_kw)
    min_headroom_kw = max_power_kw * DEFERRED_START_MIN_HEADROOM
    headroom_kw = max(consumption_headroom_kw, min_headroom_kw)
    effective_charge_kw = max_power_kw - headroom_kw
    if effective_charge_kw <= 0:
        effective_charge_kw = max_power_kw * DEFERRED_START_MIN_HEADROOM
    charge_hours = energy_needed_kwh / effective_charge_kw
    return end - datetime.timedelta(hours=charge_hours)


def _remove_mode_from_schedule(
    inverter: Inverter,
    mode: WorkMode,
    min_soc_on_grid: int,
) -> None:
    """Remove all groups of *mode* from the schedule, keeping other modes.

    If no groups remain after filtering, falls back to ``self_use``.
    This is a blocking call — use via ``async_add_executor_job``.
    """
    schedule = inverter.get_schedule()
    kept: list[ScheduleGroup] = []
    for raw_group in schedule.get("groups", []):
        if _is_placeholder(raw_group):
            continue
        if raw_group.get("workMode") == mode.value:
            continue
        group = _sanitize_group(raw_group)
        group["enable"] = 1
        kept.append(group)
    if kept:
        _LOGGER.debug("Removing %s groups, %d groups remain", mode.value, len(kept))
        inverter.set_schedule(kept)
    else:
        _LOGGER.debug(
            "No groups remain after removing %s, reverting to SelfUse", mode.value
        )
        inverter.self_use(min_soc_on_grid)


def _cancel_smart_charge(hass: HomeAssistant) -> None:
    """Cancel any active smart charge listeners and clear stored session."""
    unsubs: list[Callable[[], None]] = hass.data[DOMAIN].get("_smart_charge_unsubs", [])
    for unsub in unsubs:
        unsub()
    hass.data[DOMAIN]["_smart_charge_unsubs"] = []
    hass.data[DOMAIN].pop("_smart_charge_state", None)
    if hass.data.get(DOMAIN, {}).get("_store") is not None:
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
    """Check if a group is an API placeholder (not a real schedule entry)."""
    return group.get("workMode", "") in _PLACEHOLDER_MODES


def _sanitize_group(raw: dict[str, Any]) -> ScheduleGroup:
    """Strip unknown fields from an API-returned group."""
    return {k: raw[k] for k in _SCHEDULE_GROUP_KEYS if k in raw}  # type: ignore[return-value]


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
    _LOGGER.debug("Current schedule has %d groups", len(existing))

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
    _LOGGER.debug("Setting schedule with %d groups", len(kept))
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
    inverter: Inverter,
) -> None:
    """Register HA listeners for an active smart charge session.

    Reads all parameters from ``hass.data[DOMAIN]["_smart_charge_state"]``.
    """
    state = hass.data[DOMAIN]["_smart_charge_state"]
    min_soc_on_grid: int = state["min_soc_on_grid"]
    end: datetime.datetime = state["end"]
    end_utc = dt_util.as_utc(end)

    async def _remove_charge_override() -> None:
        await hass.async_add_executor_job(
            _remove_mode_from_schedule,
            inverter,
            WorkMode.FORCE_CHARGE,
            min_soc_on_grid,
        )

    async def _on_charge_timer_expire(_now: datetime.datetime) -> None:
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
        if cur_state is None:
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
                if hass.data[DOMAIN].get("_smart_charge_state") is not None:
                    _cancel_smart_charge(hass)
                    if charging_started:
                        await _remove_charge_override()
                return
            _LOGGER.debug("Smart charge: SoC unavailable, skipping adjustment")
            return
        cur_state["soc_unavailable_count"] = 0

        if cur_soc >= cur_state["target_soc"]:
            cur_state["soc_above_target_count"] = (
                cur_state.get("soc_above_target_count", 0) + 1
            )
            if cur_state["soc_above_target_count"] < 2:
                _LOGGER.debug(
                    "Smart charge: SoC %.1f%% >= target %d%% "
                    "(count=%d, waiting for confirmation)",
                    cur_soc,
                    cur_state["target_soc"],
                    cur_state["soc_above_target_count"],
                )
                return
            _LOGGER.info(
                "Smart charge: SoC %.1f%% confirmed at/above target %d%%, reverting",
                cur_soc,
                cur_state["target_soc"],
            )
            charging_started = cur_state.get("charging_started", False)
            if hass.data[DOMAIN].get("_smart_charge_state") is not None:
                _cancel_smart_charge(hass)
                if charging_started:
                    await _remove_charge_override()
            return
        cur_state["soc_above_target_count"] = 0

        now_dt = dt_util.now()
        remaining = (cur_state["end"] - now_dt).total_seconds() / 3600.0
        if remaining <= 0:
            _LOGGER.info("Smart charge: window expired during adjustment, reverting")
            charging_started = cur_state.get("charging_started", False)
            if hass.data[DOMAIN].get("_smart_charge_state") is not None:
                _cancel_smart_charge(hass)
                if charging_started:
                    await _remove_charge_override()
            return

        net_consumption = _get_net_consumption(hass)

        if not cur_state["charging_started"]:
            # Check if it's time to start deferred charging
            deferred = _calculate_deferred_start(
                cur_soc,
                cur_state["target_soc"],
                cur_state["battery_capacity_kwh"],
                cur_state["max_power_w"],
                cur_state["end"],
                net_consumption_kw=net_consumption,
            )
            if now_dt < deferred:
                _LOGGER.debug(
                    "Smart charge: deferring until ~%02d:%02d "
                    "(SoC=%.1f%%, net_consumption=%.2fkW)",
                    deferred.hour,
                    deferred.minute,
                    cur_soc,
                    net_consumption,
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
            )
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
            except Exception as exc:
                _LOGGER.warning(
                    "Smart charge: failed to start deferred charge, aborting: %s",
                    exc,
                )
                _cancel_smart_charge(hass)
                return
            await hass.async_add_executor_job(inverter.set_schedule, groups)

            # Re-check state after await — may have been cancelled concurrently
            cur_state = hass.data[DOMAIN].get("_smart_charge_state")
            if cur_state is None:
                return

            cur_state["groups"] = groups
            cur_state["last_power_w"] = new_power
            cur_state["charging_started"] = True
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
        new_power = _calculate_charge_power(
            cur_soc,
            cur_state["target_soc"],
            cur_state["battery_capacity_kwh"],
            remaining,
            cur_state["max_power_w"],
            net_consumption_kw=net_consumption,
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

        _LOGGER.info(
            "Smart charge: adjusting power %dW -> %dW (SoC=%.1f%%, remaining=%.2fh)",
            cur_state["last_power_w"],
            new_power,
            cur_soc,
            remaining,
        )

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
            except (ServiceValidationError, Exception):
                _LOGGER.warning(
                    "Smart charge: conflict rebuilding schedule after recovery",
                    exc_info=True,
                )
                groups = []

        cur_state["last_power_w"] = new_power
        if groups:
            cur_state["groups"] = groups
            await hass.async_add_executor_job(inverter.set_schedule, groups)
            # Re-check state after await — may have been cancelled concurrently
            if hass.data[DOMAIN].get("_smart_charge_state") is None:
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
    inverter: Inverter,
) -> None:
    """Register HA listeners for an active smart discharge session.

    Reads all parameters from ``hass.data[DOMAIN]["_smart_discharge_state"]``.
    """
    state = hass.data[DOMAIN]["_smart_discharge_state"]
    min_soc_on_grid: int = _get_min_soc_on_grid(hass)
    end: datetime.datetime = state["end"]
    end_utc = dt_util.as_utc(end)

    async def _remove_discharge_override() -> None:
        await hass.async_add_executor_job(
            _remove_mode_from_schedule,
            inverter,
            WorkMode.FORCE_DISCHARGE,
            min_soc_on_grid,
        )

    async def _on_timer_expire(_now: datetime.datetime) -> None:
        _LOGGER.info("Smart discharge: window ended, removing override")
        _cancel_smart_discharge(hass)
        await _remove_discharge_override()

    async def _check_discharge_soc(_now: datetime.datetime) -> None:
        """Periodic SoC and feed-in energy check from coordinator data."""
        cur_state = hass.data[DOMAIN].get("_smart_discharge_state")
        if cur_state is None:
            return

        # --- Check feed-in energy limit using cumulative counter ---
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
                        _LOGGER.info(
                            "Smart discharge: feed-in energy %.2f kWh reached "
                            "limit %.2f kWh, removing override",
                            exported,
                            feedin_limit,
                        )
                        if hass.data[DOMAIN].get("_smart_discharge_state") is not None:
                            _cancel_smart_discharge(hass)
                            await _remove_discharge_override()
                        return

        # --- SoC threshold check ---
        soc_value = _get_current_soc(hass)
        if soc_value is None:
            return
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
            _LOGGER.info(
                "Smart discharge: SoC %.1f%% confirmed at/below "
                "threshold %d%%, removing override",
                soc_value,
                cur_state["min_soc"],
            )
            if hass.data[DOMAIN].get("_smart_discharge_state") is not None:
                _cancel_smart_discharge(hass)
                await _remove_discharge_override()
        else:
            cur_state["soc_below_min_count"] = 0

    unsubs: list[Callable[[], None]] = [
        async_track_time_interval(
            hass,
            _check_discharge_soc,
            datetime.timedelta(seconds=60),
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
    inverter: Inverter,
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

    # Window still active — check if the schedule group exists
    has_group = await hass.async_add_executor_job(
        _has_matching_schedule_group,
        inverter,
        WorkMode.FORCE_CHARGE,
        charge_data["end_hour"],
        charge_data["end_minute"],
    )
    start = now.replace(
        hour=charge_data["start_hour"],
        minute=charge_data["start_minute"],
        second=0,
        microsecond=0,
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
        last_power = max_power
        if current_soc is not None and charge_data.get("charging_started", False):
            last_power = _calculate_charge_power(
                current_soc,
                target_soc,
                capacity,
                remaining,
                max_power,
                net_consumption_kw=_get_net_consumption(hass),
            )

        hass.data[DOMAIN]["_smart_charge_state"] = {
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
    inverter: Inverter,
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
        min_soc_on_grid = _get_min_soc_on_grid(hass)
        try:
            await hass.async_add_executor_job(
                _remove_mode_from_schedule,
                inverter,
                WorkMode.FORCE_DISCHARGE,
                min_soc_on_grid,
            )
        except Exception:
            _LOGGER.exception("Smart discharge: failed to clean up expired schedule")
        del stored["smart_discharge"]
        return True

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
        hass.data[DOMAIN]["_smart_discharge_state"] = {
            "groups": [],
            "start": start,
            "end": end,
            "min_soc": discharge_data.get("min_soc", 10),
            "last_power_w": discharge_data.get("last_power_w", 0),
            "soc_below_min_count": 0,
            "feedin_energy_limit_kwh": discharge_data.get("feedin_energy_limit_kwh"),
            "feedin_start_kwh": discharge_data.get("feedin_start_kwh"),
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
    inverter: Inverter,
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
            _LOGGER.warning("Smart charge: corrupted session data, discarding: %s", exc)
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
                "Smart discharge: corrupted session data, discarding: %s", exc
            )
            stored.pop("smart_discharge", None)
            changed = True

    if changed:
        await store.async_save(stored)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FoxESS Control from a config entry."""
    client = FoxESSClient(entry.data[CONF_API_KEY])
    inverter = Inverter(client, entry.data[CONF_DEVICE_SERIAL])

    # Pre-warm max_power_w (validates connection, caches rated power)
    await hass.async_add_executor_job(lambda: inverter.max_power_w)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("_smart_discharge_unsubs", [])
    hass.data[DOMAIN].setdefault("_smart_charge_unsubs", [])
    hass.data[DOMAIN].setdefault(
        "_store", Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
    )
    # Create DataUpdateCoordinator for API polling
    polling_interval = int(
        entry.options.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL)
    )
    coordinator = FoxESSDataCoordinator(hass, inverter, polling_interval)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "inverter": inverter,
        "coordinator": coordinator,
    }

    # Register services once (first real entry)
    real_entries = {k for k in hass.data[DOMAIN] if not k.startswith("_")}
    if len(real_entries) == 1:
        _register_services(hass)
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

    # Only "_smart_discharge_unsubs" key remains → last real entry was removed
    remaining = {k for k in hass.data[DOMAIN] if not k.startswith("_")}
    if not remaining:
        _cancel_smart_discharge(hass)
        _cancel_smart_charge(hass)
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
        inverter = _get_inverter(hass)
        min_soc_on_grid = _get_min_soc_on_grid(hass)
        mode_filter: str | None = call.data.get("mode")

        if mode_filter is None:
            _LOGGER.info("Clearing all overrides, setting SelfUse")
            await hass.async_add_executor_job(inverter.self_use, min_soc_on_grid)
        else:
            _LOGGER.info("Clearing %s overrides", mode_filter)
            schedule = await hass.async_add_executor_job(inverter.get_schedule)
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

        # Cancel smart listeners that correspond to cleared modes
        if mode_filter is None or mode_filter == WorkMode.FORCE_CHARGE.value:
            _cancel_smart_charge(hass)
        if mode_filter is None or mode_filter == WorkMode.FORCE_DISCHARGE.value:
            _cancel_smart_discharge(hass)

    async def handle_force_charge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        force: bool = call.data.get("replace_conflicts", False)
        start, end = _resolve_start_end(duration, start_time)

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

        _LOGGER.info(
            "Force charge %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )
        await hass.async_add_executor_job(inverter.set_schedule, groups)
        _cancel_smart_charge(hass)

    async def handle_force_discharge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        force: bool = call.data.get("replace_conflicts", False)
        start, end = _resolve_start_end(duration, start_time)

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

        _LOGGER.info(
            "Force discharge %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )
        await hass.async_add_executor_job(inverter.set_schedule, groups)
        _cancel_smart_discharge(hass)

    async def handle_feedin(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        force: bool = call.data.get("replace_conflicts", False)
        start, end = _resolve_start_end(duration, start_time)

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

        _LOGGER.info(
            "Feed-in %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )
        await hass.async_add_executor_job(inverter.set_schedule, groups)

    async def handle_smart_discharge(call: ServiceCall) -> None:
        start_time: datetime.time = call.data["start_time"]
        end_time: datetime.time = call.data["end_time"]
        power: int | None = call.data.get("power")
        min_soc: int = call.data["min_soc"]
        force: bool = call.data.get("replace_conflicts", False)
        feedin_energy_limit: float | None = call.data.get("feedin_energy_limit_kwh")

        start, end = _resolve_start_end_explicit(start_time, end_time)

        if _get_current_soc(hass) is None:
            raise ServiceValidationError(
                "Battery SoC is not available. Wait for the API poll to complete."
            )

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

        # Cancel any previous smart discharge listeners
        _cancel_smart_discharge(hass)

        # Cancel any active smart charge — the two sessions would conflict
        if hass.data[DOMAIN].get("_smart_charge_state") is not None:
            _LOGGER.info("Smart discharge: cancelling active smart charge session")
            _cancel_smart_charge(hass)

        effective_power = power if power is not None else inverter.max_power_w

        # Store state for binary sensor and diagnostics
        hass.data[DOMAIN]["_smart_discharge_state"] = {
            "groups": groups,
            "start": start,
            "end": end,
            "min_soc": min_soc,
            "last_power_w": effective_power,
            "soc_below_min_count": 0,
            "feedin_energy_limit_kwh": feedin_energy_limit,
            "feedin_start_kwh": _get_feedin_energy_kwh(hass),
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

        inverter = _get_inverter(hass)
        min_soc_on_grid = _get_min_soc_on_grid(hass)
        api_min_soc = _get_api_min_soc(hass)
        effective_max_power = (
            max_power if max_power is not None else inverter.max_power_w
        )

        # Read current SoC for initial power calculation and deferred start
        current_soc = _get_current_soc(hass)
        if current_soc is not None and current_soc >= target_soc:
            raise ServiceValidationError(
                f"Current SoC ({current_soc}%) already at or above "
                f"target ({target_soc}%)"
            )

        # Validate conflicts upfront using the full window
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

        # Decide whether to start charging now or defer
        now = dt_util.now()
        net_consumption = _get_net_consumption(hass)
        should_defer = False
        if current_soc is not None:
            deferred_start = _calculate_deferred_start(
                current_soc,
                target_soc,
                battery_capacity_kwh,
                effective_max_power,
                end,
                net_consumption_kw=net_consumption,
            )
            should_defer = now < deferred_start

        if should_defer:
            _LOGGER.info(
                "Smart charge %02d:%02d - %02d:%02d deferred until ~%02d:%02d "
                "(target_soc=%d%%, SoC=%.1f%%)",
                start.hour,
                start.minute,
                end.hour,
                end.minute,
                deferred_start.hour,
                deferred_start.minute,
                target_soc,
                current_soc,
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
                )

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
            _LOGGER.info(
                "Smart charge %02d:%02d - %02d:%02d (power=%dW, target_soc=%d%%)",
                start.hour,
                start.minute,
                end.hour,
                end.minute,
                initial_power,
                target_soc,
            )
            await hass.async_add_executor_job(inverter.set_schedule, initial_groups)

        # Cancel any previous smart charge listeners
        _cancel_smart_charge(hass)

        # Cancel any active smart discharge — the two sessions would conflict
        if hass.data[DOMAIN].get("_smart_discharge_state") is not None:
            _LOGGER.info("Smart charge: cancelling active smart discharge session")
            _cancel_smart_discharge(hass)

        min_power_change = _get_min_power_change(hass)

        # Store state for periodic adjustments
        hass.data[DOMAIN]["_smart_charge_state"] = {
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
