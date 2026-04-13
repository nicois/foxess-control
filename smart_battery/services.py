"""Service registration scaffolding for smart battery integrations.

This module provides the shared service schemas and a registration
function that brand integrations call from ``async_setup_entry``.
The service handlers use the :class:`~smart_battery.adapter.InverterAdapter`
protocol so the same handlers work for any brand.

Cloud-mode brands that need schedule-group merging should override
the relevant handlers at the brand level (e.g. foxess_control wraps
these handlers to add cloud-API schedule management).
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .algorithms import (
    calculate_charge_power,
    calculate_deferred_start,
    calculate_discharge_deferred_start,
)
from .const import (
    CONF_API_MIN_SOC,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    CONF_SMART_HEADROOM,
    DEFAULT_API_MIN_SOC,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_SMART_HEADROOM,
    MAX_OVERRIDE_HOURS,
    SERVICE_CLEAR_OVERRIDES,
    SERVICE_FEEDIN,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_SMART_CHARGE,
    SERVICE_SMART_DISCHARGE,
)
from .listeners import (
    _get_current_soc,
    _get_feedin_energy_kwh,
    _get_net_consumption,
    cancel_smart_charge,
    cancel_smart_discharge,
    setup_smart_charge_listeners,
    setup_smart_discharge_listeners,
)
from .session import (
    save_session,
    session_data_from_charge_state,
    session_data_from_discharge_state,
)
from .types import WorkMode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers.storage import Store

    from .adapter import InverterAdapter

_LOGGER = logging.getLogger(__name__)

VALID_MODES = [m.value for m in WorkMode]

SCHEMA_CLEAR_OVERRIDES = vol.Schema({vol.Optional("mode"): vol.In(VALID_MODES)})

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


def resolve_start_end(
    duration: datetime.timedelta,
    start_time: datetime.time | None = None,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Validate duration and resolve start/end datetimes.

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


def resolve_start_end_explicit(
    start_time: datetime.time,
    end_time: datetime.time,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Validate and resolve explicit start/end times to datetimes (today)."""
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


def _get_entry_option(
    hass: HomeAssistant,
    domain: str,
    key: str,
    default: Any,
) -> Any:
    """Read an option from the first config entry."""
    domain_data = hass.data.get(domain, {})
    for k in domain_data:
        if not str(k).startswith("_"):
            entry_data = domain_data.get(k)
            if isinstance(entry_data, dict):
                entry = entry_data.get("entry")
                if entry is not None:
                    return entry.options.get(key, default)
    return default


def register_services(
    hass: HomeAssistant,
    domain: str,
    adapter: InverterAdapter,
) -> None:
    """Register the 6 standard smart battery services for a brand integration.

    This registers entity-mode service handlers.  Cloud-mode brands
    should either wrap these handlers or replace them entirely.
    """

    def _get_store() -> Store[dict[str, Any]] | None:
        return hass.data.get(domain, {}).get("_store")  # type: ignore[no-any-return]

    async def handle_clear_overrides(call: ServiceCall) -> None:
        mode_filter: str | None = call.data.get("mode")
        if mode_filter is None or mode_filter == WorkMode.FORCE_CHARGE.value:
            cancel_smart_charge(hass, domain)
        if mode_filter is None or mode_filter == WorkMode.FORCE_DISCHARGE.value:
            cancel_smart_discharge(hass, domain)
        _LOGGER.info("Clearing overrides, setting SelfUse")
        await adapter.apply_mode(hass, WorkMode.SELF_USE)

    async def handle_force_charge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        start, end = resolve_start_end(duration, start_time)

        _LOGGER.info(
            "Force charge %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )
        cancel_smart_charge(hass, domain)
        await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, power)

    async def handle_force_discharge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        start, end = resolve_start_end(duration, start_time)

        _LOGGER.info(
            "Force discharge %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )
        cancel_smart_discharge(hass, domain)
        api_min_soc = int(
            _get_entry_option(hass, domain, CONF_API_MIN_SOC, DEFAULT_API_MIN_SOC)
        )
        await adapter.apply_mode(
            hass, WorkMode.FORCE_DISCHARGE, power, fd_soc=api_min_soc
        )

    async def handle_feedin(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        power: int | None = call.data.get("power")
        start_time: datetime.time | None = call.data.get("start_time")
        start, end = resolve_start_end(duration, start_time)

        _LOGGER.info(
            "Feed-in %02d:%02d - %02d:%02d (power=%s)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{power}W" if power else "max",
        )
        await adapter.apply_mode(hass, WorkMode.FEEDIN, power)

    async def handle_smart_discharge(call: ServiceCall) -> None:
        start_time_val: datetime.time = call.data["start_time"]
        end_time_val: datetime.time = call.data["end_time"]
        power: int | None = call.data.get("power")
        min_soc: int = call.data["min_soc"]
        feedin_energy_limit: float | None = call.data.get("feedin_energy_limit_kwh")

        start, end = resolve_start_end_explicit(start_time_val, end_time_val)

        if _get_current_soc(hass, domain) is None:
            raise ServiceValidationError(
                "Battery SoC is not available. Wait for the poll to complete."
            )

        api_min_soc = int(
            _get_entry_option(hass, domain, CONF_API_MIN_SOC, DEFAULT_API_MIN_SOC)
        )
        max_power_w = power if power is not None else adapter.get_max_power_w()
        battery_capacity_kwh: float = _get_entry_option(
            hass, domain, CONF_BATTERY_CAPACITY_KWH, 0.0
        )
        pacing_enabled = battery_capacity_kwh > 0

        # Decide whether to start discharging now or defer (stay in self-use)
        current_soc = _get_current_soc(hass, domain)
        now = dt_util.now()
        headroom_pct: int = _get_entry_option(
            hass, domain, CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM
        )
        headroom = headroom_pct / 100.0
        net_consumption = _get_net_consumption(hass, domain)
        should_defer = False
        if pacing_enabled and current_soc is not None:
            deferred_start = calculate_discharge_deferred_start(
                current_soc,
                min_soc,
                battery_capacity_kwh,
                max_power_w,
                end,
                net_consumption_kw=net_consumption,
                start=start,
                headroom=headroom,
                taper_profile=hass.data.get(domain, {}).get("_taper_profile"),
                feedin_energy_limit_kwh=feedin_energy_limit,
            )
            should_defer = now < deferred_start

        if should_defer:
            _LOGGER.info(
                "Smart discharge %02d:%02d - %02d:%02d deferred "
                "(min_soc=%d%%, SoC=%.1f%%)",
                start.hour,
                start.minute,
                end.hour,
                end.minute,
                min_soc,
                current_soc,
            )
            initial_power = 0
        else:
            if pacing_enabled and current_soc is not None:
                from .algorithms import calculate_discharge_power

                remaining = (end - now).total_seconds() / 3600.0
                initial_power = calculate_discharge_power(
                    current_soc,
                    min_soc,
                    battery_capacity_kwh,
                    remaining,
                    max_power_w,
                    net_consumption_kw=net_consumption,
                    headroom=headroom,
                    feedin_remaining_kwh=feedin_energy_limit,
                )
            else:
                initial_power = max_power_w

        cancel_smart_discharge(hass, domain)
        if hass.data[domain].get("_smart_charge_state") is not None:
            _LOGGER.info("Smart discharge: cancelling active smart charge session")
            cancel_smart_charge(hass, domain)

        if not should_defer:
            await adapter.apply_mode(
                hass, WorkMode.FORCE_DISCHARGE, initial_power, fd_soc=api_min_soc
            )

        min_power_change = int(
            _get_entry_option(
                hass, domain, CONF_MIN_POWER_CHANGE, DEFAULT_MIN_POWER_CHANGE
            )
        )

        hass.data[domain]["_smart_discharge_state"] = {
            "session_id": str(uuid.uuid4()),
            "groups": [],
            "start": start,
            "end": end,
            "min_soc": min_soc,
            "max_power_w": max_power_w,
            "last_power_w": initial_power,
            "soc_below_min_count": 0,
            "feedin_energy_limit_kwh": feedin_energy_limit,
            "feedin_start_kwh": _get_feedin_energy_kwh(hass, domain),
            "battery_capacity_kwh": battery_capacity_kwh,
            "min_power_change": min_power_change,
            "pacing_enabled": pacing_enabled,
            "start_soc": current_soc,
            "discharging_started": not should_defer,
            "discharging_started_at": None if should_defer else now,
        }

        setup_smart_discharge_listeners(hass, domain, adapter)

        await save_session(
            _get_store(),
            "smart_discharge",
            session_data_from_discharge_state(
                hass.data[domain]["_smart_discharge_state"]
            ),
        )

    async def handle_smart_charge(call: ServiceCall) -> None:
        start_time_val: datetime.time = call.data["start_time"]
        end_time_val: datetime.time = call.data["end_time"]
        max_power: int | None = call.data.get("power")
        target_soc: int = call.data["target_soc"]

        start, end = resolve_start_end_explicit(start_time_val, end_time_val)

        if _get_current_soc(hass, domain) is None:
            raise ServiceValidationError(
                "Battery SoC is not available. Wait for the poll to complete."
            )

        battery_capacity_kwh: float = _get_entry_option(
            hass, domain, CONF_BATTERY_CAPACITY_KWH, 0.0
        )
        if battery_capacity_kwh <= 0:
            raise ServiceValidationError(
                "Battery capacity (kWh) not configured. Set it in the "
                "integration options before using smart charge."
            )

        min_soc_on_grid = int(
            _get_entry_option(
                hass, domain, CONF_MIN_SOC_ON_GRID, DEFAULT_MIN_SOC_ON_GRID
            )
        )
        api_min_soc = int(
            _get_entry_option(hass, domain, CONF_API_MIN_SOC, DEFAULT_API_MIN_SOC)
        )
        effective_max_power = (
            max_power if max_power is not None else adapter.get_max_power_w()
        )

        current_soc = _get_current_soc(hass, domain)
        if current_soc is not None and current_soc >= target_soc:
            raise ServiceValidationError(
                f"Current SoC ({current_soc}%) already at or above "
                f"target ({target_soc}%)"
            )

        cancel_smart_charge(hass, domain)
        if hass.data[domain].get("_smart_discharge_state") is not None:
            _LOGGER.info("Smart charge: cancelling active smart discharge session")
            cancel_smart_discharge(hass, domain)

        # Decide whether to start charging now or defer
        now = dt_util.now()
        net_consumption = _get_net_consumption(hass, domain)
        headroom_pct: int = _get_entry_option(
            hass, domain, CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM
        )
        headroom = headroom_pct / 100.0
        should_defer = False
        if current_soc is not None:
            deferred_start = calculate_deferred_start(
                current_soc,
                target_soc,
                battery_capacity_kwh,
                effective_max_power,
                end,
                net_consumption_kw=net_consumption,
                start=start,
                headroom=headroom,
                taper_profile=hass.data.get(domain, {}).get("_taper_profile"),
            )
            should_defer = now < deferred_start

        if should_defer:
            _LOGGER.info(
                "Smart charge %02d:%02d - %02d:%02d deferred "
                "(target_soc=%d%%, SoC=%.1f%%)",
                start.hour,
                start.minute,
                end.hour,
                end.minute,
                target_soc,
                current_soc,
            )
            initial_power = 0
        else:
            remaining = (end - now).total_seconds() / 3600.0
            initial_power = effective_max_power
            if current_soc is not None:
                initial_power = calculate_charge_power(
                    current_soc,
                    target_soc,
                    battery_capacity_kwh,
                    remaining,
                    effective_max_power,
                    net_consumption_kw=net_consumption,
                    headroom=headroom,
                )
            await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, initial_power)

        min_power_change = int(
            _get_entry_option(
                hass, domain, CONF_MIN_POWER_CHANGE, DEFAULT_MIN_POWER_CHANGE
            )
        )

        hass.data[domain]["_smart_charge_state"] = {
            "session_id": str(uuid.uuid4()),
            "groups": [],
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
            "force": False,
            "soc_unavailable_count": 0,
            "start_soc": current_soc,
        }

        setup_smart_charge_listeners(hass, domain, adapter)

        await save_session(
            _get_store(),
            "smart_charge",
            session_data_from_charge_state(hass.data[domain]["_smart_charge_state"]),
        )

    hass.services.async_register(
        domain,
        SERVICE_CLEAR_OVERRIDES,
        handle_clear_overrides,
        schema=SCHEMA_CLEAR_OVERRIDES,
    )
    hass.services.async_register(
        domain,
        SERVICE_FEEDIN,
        handle_feedin,
        schema=SCHEMA_FEEDIN,
    )
    hass.services.async_register(
        domain,
        SERVICE_FORCE_CHARGE,
        handle_force_charge,
        schema=SCHEMA_FORCE_CHARGE,
    )
    hass.services.async_register(
        domain,
        SERVICE_FORCE_DISCHARGE,
        handle_force_discharge,
        schema=SCHEMA_FORCE_DISCHARGE,
    )
    hass.services.async_register(
        domain,
        SERVICE_SMART_CHARGE,
        handle_smart_charge,
        schema=SCHEMA_SMART_CHARGE,
    )
    hass.services.async_register(
        domain,
        SERVICE_SMART_DISCHARGE,
        handle_smart_discharge,
        schema=SCHEMA_SMART_DISCHARGE,
    )
