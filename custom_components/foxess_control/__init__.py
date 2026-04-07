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
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_FORCE_DISCHARGE = "force_discharge"

SCHEMA_FORCE_CHARGE = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
    }
)

SCHEMA_FORCE_DISCHARGE = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("min_soc", default=10): vol.All(int, vol.Range(min=5, max=100)),
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


def _validate_duration(duration: datetime.timedelta) -> datetime.datetime:
    """Validate override duration and return the end time.

    Raises ServiceValidationError if duration exceeds MAX_OVERRIDE_HOURS
    or would extend past midnight.
    """
    max_delta = datetime.timedelta(hours=MAX_OVERRIDE_HOURS)
    if duration <= datetime.timedelta(0):
        raise ServiceValidationError("Duration must be positive")
    if duration > max_delta:
        raise ServiceValidationError(
            f"Duration must not exceed {MAX_OVERRIDE_HOURS} hours"
        )

    now = dt_util.now()
    end = now + duration

    if end.date() != now.date():
        raise ServiceValidationError("Override must not extend past midnight")

    return end


def _build_override_group(
    now: datetime.datetime,
    end: datetime.datetime,
    work_mode: WorkMode,
    inverter: Inverter,
    min_soc_on_grid: int,
    fd_soc: int,
) -> ScheduleGroup:
    """Build a single ScheduleGroup for a timed override."""
    return {
        "enable": 1,
        "startHour": now.hour,
        "startMinute": now.minute,
        "endHour": end.hour,
        "endMinute": end.minute,
        "workMode": work_mode.value,
        "minSocOnGrid": min_soc_on_grid,
        "fdSoc": fd_soc,
        "fdPwr": inverter.max_power_w,
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
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_CHARGE)
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_DISCHARGE)

    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register the three inverter control services."""

    async def handle_clear_overrides(call: ServiceCall) -> None:
        inverter = _get_inverter(hass)
        min_soc_on_grid = _get_min_soc_on_grid(hass)
        _LOGGER.info("Clearing overrides, setting SelfUse")
        await hass.async_add_executor_job(inverter.self_use, min_soc_on_grid)

    async def handle_force_charge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        end = _validate_duration(duration)
        now = dt_util.now()

        inverter = _get_inverter(hass)
        min_soc_on_grid = _get_min_soc_on_grid(hass)

        group = _build_override_group(
            now, end, WorkMode.FORCE_CHARGE, inverter, min_soc_on_grid, fd_soc=100
        )

        _LOGGER.info(
            "Force charge %02d:%02d - %02d:%02d",
            now.hour,
            now.minute,
            end.hour,
            end.minute,
        )
        await hass.async_add_executor_job(inverter.set_schedule, [group])

    async def handle_force_discharge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        min_soc: int = call.data["min_soc"]
        end = _validate_duration(duration)
        now = dt_util.now()

        inverter = _get_inverter(hass)
        min_soc_on_grid = _get_min_soc_on_grid(hass)

        group = _build_override_group(
            now,
            end,
            WorkMode.FORCE_DISCHARGE,
            inverter,
            min_soc_on_grid,
            fd_soc=min_soc,
        )

        _LOGGER.info(
            "Force discharge %02d:%02d - %02d:%02d (min_soc=%d%%)",
            now.hour,
            now.minute,
            end.hour,
            end.minute,
            min_soc,
        )
        await hass.async_add_executor_job(inverter.set_schedule, [group])

    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_OVERRIDES, handle_clear_overrides
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
