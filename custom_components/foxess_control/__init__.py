"""FoxESS Control — Home Assistant integration for inverter mode management."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY,
    CONF_DEVICE_SERIAL,
    CONF_MIN_SOC_ON_GRID,
    DEFAULT_MIN_SOC_ON_GRID,
    DOMAIN,
    MAX_OVERRIDE_HOURS,
)
from .foxess import FoxESSClient, Inverter, WorkMode

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

    from .foxess.inverter import ScheduleGroup

_LOGGER = logging.getLogger(__name__)

SERVICE_CLEAR_OVERRIDES = "clear_overrides"
SERVICE_FEEDIN = "feedin"
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_FORCE_DISCHARGE = "force_discharge"

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
    }
)

SCHEMA_FORCE_DISCHARGE = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Optional("start_time"): cv.time,
    }
)

SCHEMA_FEEDIN = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Optional("start_time"): cv.time,
    }
)


def _get_inverter(hass: HomeAssistant) -> Inverter:
    """Get the first configured Inverter instance."""
    entries: dict[str, Any] = hass.data[DOMAIN]
    if not entries:
        raise ServiceValidationError("No FoxESS Control integration configured")
    inverter: Inverter = next(iter(entries.values()))["inverter"]
    return inverter


def _get_min_soc_on_grid(hass: HomeAssistant) -> int:
    """Get min_soc_on_grid from the first config entry's options."""
    entry_id = next(iter(hass.data[DOMAIN]))
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
) -> list[ScheduleGroup]:
    """Fetch the current schedule, remove same-mode groups, and merge.

    Disabled and same-mode groups are removed. Past groups are kept
    because they may represent recurring daily schedules (e.g. a
    standing force-charge window for free-electricity hours).

    Raises ServiceValidationError if any retained group of a
    *different* mode overlaps with the new time window.
    """
    schedule = inverter.get_schedule()
    existing: list[dict[str, Any]] = schedule.get("groups", [])
    _LOGGER.debug("Current schedule has %d groups", len(existing))

    kept: list[ScheduleGroup] = []
    for raw_group in existing:
        if not raw_group.get("enable"):
            continue
        group = _sanitize_group(raw_group)
        if group.get("workMode") == work_mode.value:
            _LOGGER.debug("Removing existing %s group", work_mode.value)
            continue
        if group.get("workMode") == WorkMode.SELF_USE.value:
            _LOGGER.debug("Dropping SelfUse baseline group")
            continue
        if _groups_overlap(group, new_group):
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
    hass.data[DOMAIN][entry.entry_id] = {"inverter": inverter}

    # Register services once (first entry)
    if len(hass.data[DOMAIN]) == 1:
        _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data[DOMAIN].pop(entry.entry_id)

    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_OVERRIDES)
        hass.services.async_remove(DOMAIN, SERVICE_FEEDIN)
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_CHARGE)
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_DISCHARGE)

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
            kept: list[ScheduleGroup] = [
                _sanitize_group(g)
                for g in schedule.get("groups", [])
                if g.get("enable") and g.get("workMode") != mode_filter
            ]
            if kept:
                await hass.async_add_executor_job(inverter.set_schedule, kept)
            else:
                await hass.async_add_executor_job(inverter.self_use, min_soc_on_grid)

    async def handle_force_charge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
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

    async def handle_force_discharge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
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

    async def handle_feedin(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
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
