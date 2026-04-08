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
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DOMAIN,
    MAX_OVERRIDE_HOURS,
    PLATFORMS,
)
from .foxess import FoxESSClient, Inverter, WorkMode

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event as HAEvent
    from homeassistant.core import EventStateChangedData, HomeAssistant, ServiceCall

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
        vol.Required("min_soc"): vol.All(int, vol.Range(min=11, max=100)),
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_SMART_CHARGE = vol.Schema(
    {
        vol.Required("start_time"): cv.time,
        vol.Required("end_time"): cv.time,
        vol.Required("target_soc"): vol.All(int, vol.Range(min=11, max=100)),
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
    },
    extra=vol.ALLOW_EXTRA,
)


def _first_entry_id(hass: HomeAssistant) -> str:
    """Return the entry_id of the first real config entry in domain data."""
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

    return start, end


def _get_battery_soc_entity(hass: HomeAssistant) -> str:
    """Get battery_soc_entity from the first config entry's options."""
    entry_id = _first_entry_id(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return ""
    entity: str = entry.options.get(CONF_BATTERY_SOC_ENTITY, "")
    return entity


def _cancel_smart_discharge(hass: HomeAssistant) -> None:
    """Cancel any active smart discharge listeners."""
    unsubs: list[Callable[[], None]] = hass.data[DOMAIN].get(
        "_smart_discharge_unsubs", []
    )
    for unsub in unsubs:
        unsub()
    hass.data[DOMAIN]["_smart_discharge_unsubs"] = []
    hass.data[DOMAIN].pop("_smart_discharge_state", None)


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
) -> int:
    """Calculate the charge power needed to reach target SoC in remaining time.

    Returns an integer power in watts, clamped to [100, max_power_w].
    """
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    if energy_needed_kwh <= 0:
        return 100
    if remaining_hours <= 0:
        return max_power_w
    power_w = energy_needed_kwh / remaining_hours * 1000
    return max(100, min(int(power_w), max_power_w))


# Fraction of max power used to calculate the deferred start time.
# 0.8 means we plan to charge at 80% of max, leaving a 20% buffer
# to absorb local consumption or reduced solar.
DEFERRED_START_POWER_FRACTION = 0.8


def _calculate_deferred_start(
    current_soc: float,
    target_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    end: datetime.datetime,
) -> datetime.datetime:
    """Calculate the latest time to start charging to reach target SoC by *end*.

    Plans charging at 80% of *max_power_w*, leaving a 20% buffer to
    account for local consumption that reduces effective charge rate.

    Returns *end* if no charging is needed (SoC already at target).
    Returns a time before *end* otherwise; may be in the past if the
    window is too short to reach the target even at 80% power.
    """
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    if energy_needed_kwh <= 0:
        return end
    charge_power_kw = max_power_w * DEFERRED_START_POWER_FRACTION / 1000.0
    if charge_power_kw <= 0:
        return end
    charge_hours = energy_needed_kwh / charge_power_kw
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
    """Cancel any active smart charge listeners."""
    unsubs: list[Callable[[], None]] = hass.data[DOMAIN].get("_smart_charge_unsubs", [])
    for unsub in unsubs:
        unsub()
    hass.data[DOMAIN]["_smart_charge_unsubs"] = []
    hass.data[DOMAIN].pop("_smart_charge_state", None)


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
    """Check if a group's end time has already passed today."""
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
) -> ScheduleGroup:
    """Build a single ScheduleGroup for a timed override.

    The FoxESS API requires ``fdSoc >= 11`` and ``minSocOnGrid <= fdSoc``.
    """
    fd_soc = max(fd_soc, 11)
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FoxESS Control from a config entry."""
    client = FoxESSClient(entry.data[CONF_API_KEY])
    inverter = Inverter(client, entry.data[CONF_DEVICE_SERIAL])

    # Pre-warm max_power_w (validates connection, caches rated power)
    await hass.async_add_executor_job(lambda: inverter.max_power_w)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("_smart_discharge_unsubs", [])
    hass.data[DOMAIN].setdefault("_smart_charge_unsubs", [])
    hass.data[DOMAIN][entry.entry_id] = {"inverter": inverter}

    # Register services once (first real entry)
    real_entries = {k for k in hass.data[DOMAIN] if not k.startswith("_")}
    if len(real_entries) == 1:
        _register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


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

        group = _build_override_group(
            start,
            end,
            WorkMode.FORCE_CHARGE,
            inverter,
            min_soc_on_grid,
            fd_soc=100,
            fd_pwr=power,
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

        group = _build_override_group(
            start,
            end,
            WorkMode.FORCE_DISCHARGE,
            inverter,
            min_soc_on_grid,
            fd_soc=11,
            fd_pwr=power,
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

        group = _build_override_group(
            start,
            end,
            WorkMode.FEEDIN,
            inverter,
            min_soc_on_grid,
            fd_soc=11,
            fd_pwr=power,
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

        start, end = _resolve_start_end_explicit(start_time, end_time)

        soc_entity = _get_battery_soc_entity(hass)
        if not soc_entity:
            raise ServiceValidationError(
                "Battery SoC entity not configured. Set it in the "
                "integration options before using smart discharge."
            )

        inverter = _get_inverter(hass)
        min_soc_on_grid = _get_min_soc_on_grid(hass)

        group = _build_override_group(
            start,
            end,
            WorkMode.FORCE_DISCHARGE,
            inverter,
            min_soc_on_grid,
            fd_soc=11,
            fd_pwr=power,
        )

        groups = await hass.async_add_executor_job(
            _merge_with_existing,
            inverter,
            group,
            WorkMode.FORCE_DISCHARGE,
            force,
        )

        _LOGGER.info(
            "Smart discharge %02d:%02d - %02d:%02d (power=%s, min_soc=%d%%)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
            min_soc,
        )
        await hass.async_add_executor_job(inverter.set_schedule, groups)

        # Cancel any previous smart discharge listeners
        _cancel_smart_discharge(hass)

        effective_power = power if power is not None else inverter.max_power_w

        # Store state for binary sensor and diagnostics
        hass.data[DOMAIN]["_smart_discharge_state"] = {
            "groups": groups,
            "end": end,
            "min_soc": min_soc,
            "last_power_w": effective_power,
            "soc_entity": soc_entity,
        }

        end_utc = dt_util.as_utc(end)

        async def _remove_discharge_override() -> None:
            await hass.async_add_executor_job(
                _remove_mode_from_schedule,
                inverter,
                WorkMode.FORCE_DISCHARGE,
                min_soc_on_grid,
            )

        async def _on_soc_change(
            event: HAEvent[EventStateChangedData],
        ) -> None:
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable"):
                return
            try:
                soc_value = float(new_state.state)
            except (ValueError, TypeError):
                return
            if soc_value <= min_soc:
                _LOGGER.info(
                    "Smart discharge: SoC %.1f%% reached threshold %d%%, "
                    "removing override",
                    soc_value,
                    min_soc,
                )
                _cancel_smart_discharge(hass)
                await _remove_discharge_override()

        async def _on_timer_expire(_now: datetime.datetime) -> None:
            _LOGGER.info("Smart discharge: window ended, removing override")
            _cancel_smart_discharge(hass)
            await _remove_discharge_override()

        unsub_state = async_track_state_change_event(hass, [soc_entity], _on_soc_change)
        unsub_timer = async_track_point_in_time(hass, _on_timer_expire, end_utc)

        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [unsub_state, unsub_timer]

    async def handle_smart_charge(call: ServiceCall) -> None:
        start_time_val: datetime.time = call.data["start_time"]
        end_time_val: datetime.time = call.data["end_time"]
        max_power: int | None = call.data.get("power")
        target_soc: int = call.data["target_soc"]
        force: bool = call.data.get("replace_conflicts", False)

        start, end = _resolve_start_end_explicit(start_time_val, end_time_val)

        soc_entity = _get_battery_soc_entity(hass)
        if not soc_entity:
            raise ServiceValidationError(
                "Battery SoC entity not configured. Set it in the "
                "integration options before using smart charge."
            )

        battery_capacity_kwh = _get_battery_capacity_kwh(hass)
        if battery_capacity_kwh <= 0:
            raise ServiceValidationError(
                "Battery capacity (kWh) not configured. Set it in the "
                "integration options before using smart charge."
            )

        inverter = _get_inverter(hass)
        min_soc_on_grid = _get_min_soc_on_grid(hass)
        effective_max_power = (
            max_power if max_power is not None else inverter.max_power_w
        )

        # Read current SoC for initial power calculation and deferred start
        soc_state = hass.states.get(soc_entity)
        current_soc: float | None = None
        if soc_state is not None and soc_state.state not in (
            "unknown",
            "unavailable",
        ):
            try:
                current_soc = float(soc_state.state)
            except (ValueError, TypeError):
                pass
            else:
                if current_soc >= target_soc:
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
        should_defer = False
        if current_soc is not None:
            deferred_start = _calculate_deferred_start(
                current_soc,
                target_soc,
                battery_capacity_kwh,
                effective_max_power,
                end,
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
                )

            group = _build_override_group(
                start,
                end,
                WorkMode.FORCE_CHARGE,
                inverter,
                min_soc_on_grid,
                fd_soc=100,
                fd_pwr=initial_power,
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

        min_power_change = _get_min_power_change(hass)

        # Store state for periodic adjustments
        hass.data[DOMAIN]["_smart_charge_state"] = {
            "groups": initial_groups,
            "end": end,
            "target_soc": target_soc,
            "battery_capacity_kwh": battery_capacity_kwh,
            "max_power_w": effective_max_power,
            "last_power_w": initial_power,
            "soc_entity": soc_entity,
            "min_soc_on_grid": min_soc_on_grid,
            "min_power_change": min_power_change,
            "charging_started": not should_defer,
            "force": force,
            "soc_unavailable_count": 0,
        }

        end_utc = dt_util.as_utc(end)

        async def _remove_charge_override() -> None:
            await hass.async_add_executor_job(
                _remove_mode_from_schedule,
                inverter,
                WorkMode.FORCE_CHARGE,
                min_soc_on_grid,
            )

        async def _on_charge_soc_change(
            event: HAEvent[EventStateChangedData],
        ) -> None:
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in (
                "unknown",
                "unavailable",
            ):
                return
            try:
                soc_value = float(new_state.state)
            except (ValueError, TypeError):
                return
            if soc_value >= target_soc:
                _LOGGER.info(
                    "Smart charge: SoC %.1f%% reached target %d%%, removing override",
                    soc_value,
                    target_soc,
                )
                charging_started = (
                    hass.data[DOMAIN]
                    .get("_smart_charge_state", {})
                    .get("charging_started", False)
                )
                _cancel_smart_charge(hass)
                if charging_started:
                    await _remove_charge_override()

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
            state = hass.data[DOMAIN].get("_smart_charge_state")
            if state is None:
                return

            soc_st = hass.states.get(state["soc_entity"])
            if soc_st is None or soc_st.state in ("unknown", "unavailable"):
                state["soc_unavailable_count"] = (
                    state.get("soc_unavailable_count", 0) + 1
                )
                if state["soc_unavailable_count"] >= MAX_SOC_UNAVAILABLE_COUNT:
                    _LOGGER.warning(
                        "Smart charge: SoC unavailable for %d checks, aborting",
                        state["soc_unavailable_count"],
                    )
                    charging_started = state["charging_started"]
                    _cancel_smart_charge(hass)
                    if charging_started:
                        await _remove_charge_override()
                    return
                _LOGGER.debug("Smart charge: SoC unavailable, skipping adjustment")
                return
            try:
                cur_soc = float(soc_st.state)
            except (ValueError, TypeError):
                return
            state["soc_unavailable_count"] = 0

            if cur_soc >= state["target_soc"]:
                _LOGGER.info(
                    "Smart charge: SoC %.1f%% reached target %d%%, reverting",
                    cur_soc,
                    state["target_soc"],
                )
                charging_started = state["charging_started"]
                _cancel_smart_charge(hass)
                if charging_started:
                    await _remove_charge_override()
                return

            now_dt = dt_util.now()
            remaining = (state["end"] - now_dt).total_seconds() / 3600.0
            if remaining <= 0:
                return  # end timer will handle cleanup

            if not state["charging_started"]:
                # Check if it's time to start deferred charging
                deferred = _calculate_deferred_start(
                    cur_soc,
                    state["target_soc"],
                    state["battery_capacity_kwh"],
                    state["max_power_w"],
                    state["end"],
                )
                if now_dt < deferred:
                    _LOGGER.debug(
                        "Smart charge: deferring until ~%02d:%02d (SoC=%.1f%%)",
                        deferred.hour,
                        deferred.minute,
                        cur_soc,
                    )
                    return

                # Time to start charging — build and set the schedule
                new_power = _calculate_charge_power(
                    cur_soc,
                    state["target_soc"],
                    state["battery_capacity_kwh"],
                    remaining,
                    state["max_power_w"],
                )
                group = _build_override_group(
                    now_dt,
                    state["end"],
                    WorkMode.FORCE_CHARGE,
                    inverter,
                    state["min_soc_on_grid"],
                    fd_soc=100,
                    fd_pwr=new_power,
                )
                try:
                    groups = await hass.async_add_executor_job(
                        _merge_with_existing,
                        inverter,
                        group,
                        WorkMode.FORCE_CHARGE,
                        state.get("force", False),
                    )
                except ServiceValidationError as exc:
                    _LOGGER.warning(
                        "Smart charge: conflict detected when starting "
                        "deferred charge, aborting: %s",
                        exc,
                    )
                    _cancel_smart_charge(hass)
                    return
                await hass.async_add_executor_job(inverter.set_schedule, groups)

                state["groups"] = groups
                state["last_power_w"] = new_power
                state["charging_started"] = True
                _LOGGER.info(
                    "Smart charge: deferred charge started (SoC=%.1f%%, power=%dW)",
                    cur_soc,
                    new_power,
                )
                return

            # Already charging — adjust power as needed
            new_power = _calculate_charge_power(
                cur_soc,
                state["target_soc"],
                state["battery_capacity_kwh"],
                remaining,
                state["max_power_w"],
            )

            if abs(new_power - state["last_power_w"]) < state["min_power_change"]:
                _LOGGER.debug(
                    "Smart charge: power change %dW -> %dW below threshold "
                    "%dW, skipping",
                    state["last_power_w"],
                    new_power,
                    state["min_power_change"],
                )
                return

            _LOGGER.info(
                "Smart charge: adjusting power %dW -> %dW "
                "(SoC=%.1f%%, remaining=%.2fh)",
                state["last_power_w"],
                new_power,
                cur_soc,
                remaining,
            )

            for g in state["groups"]:
                if g.get("workMode") == WorkMode.FORCE_CHARGE.value:
                    g["fdPwr"] = new_power
                    break

            state["last_power_w"] = new_power
            await hass.async_add_executor_job(inverter.set_schedule, state["groups"])

        unsub_charge_state = async_track_state_change_event(
            hass, [soc_entity], _on_charge_soc_change
        )
        unsub_charge_timer = async_track_point_in_time(
            hass, _on_charge_timer_expire, end_utc
        )
        unsub_charge_interval = async_track_time_interval(
            hass, _adjust_charge_power, SMART_CHARGE_ADJUST_INTERVAL
        )

        hass.data[DOMAIN]["_smart_charge_unsubs"] = [
            unsub_charge_state,
            unsub_charge_timer,
            unsub_charge_interval,
        ]

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
