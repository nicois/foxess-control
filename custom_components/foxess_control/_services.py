"""FoxESS Control — service registration and handlers.

Extracted from __init__.py for maintainability.  The public entry point
is :func:`_register_services`, called once from ``async_setup``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from ._helpers import (
    SCHEMA_CLEAR_OVERRIDES,
    SCHEMA_FEEDIN,
    SCHEMA_FORCE_CHARGE,
    SCHEMA_FORCE_DISCHARGE,
    SCHEMA_SMART_CHARGE,
    SCHEMA_SMART_DISCHARGE,
    SERVICE_CLEAR_OVERRIDES,
    SERVICE_FEEDIN,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_SMART_CHARGE,
    SERVICE_SMART_DISCHARGE,
    _apply_mode_via_entities,
    _cancel_smart_charge,
    _cancel_smart_discharge,
    _cfg,
    _dd,
    _first_entry_id,
    _get_current_soc,
    _get_inverter,
    _get_net_consumption,
    _get_taper_profile,
    _save_session,
)
from .const import DOMAIN
from .foxess import Inverter, WorkMode
from .foxess_adapter import (
    _build_override_group,
    _check_schedule_safe,
    _is_placeholder,
    _merge_with_existing,
    _sanitize_group,
)
from .smart_battery.algorithms import (
    DISCHARGE_SAFETY_FACTOR as _DISCHARGE_SAFETY_FACTOR,
)
from .smart_battery.algorithms import (
    calculate_charge_power as _calculate_charge_power,
)
from .smart_battery.algorithms import (
    calculate_deferred_start as _calculate_deferred_start,
)
from .smart_battery.algorithms import (
    calculate_discharge_deferred_start as _calculate_discharge_deferred_start,
)
from .smart_battery.algorithms import (
    calculate_discharge_power as _calculate_discharge_power,
)
from .smart_battery.algorithms import (
    compute_safe_schedule_end as _compute_safe_schedule_end,
)
from .smart_battery.services import (
    resolve_start_end as _resolve_start_end,
)
from .smart_battery.services import (
    resolve_start_end_explicit as _resolve_start_end_explicit,
)
from .smart_battery.session import (
    session_data_from_charge_state as _session_data_from_charge_state,
)
from .smart_battery.session import (
    session_data_from_discharge_state as _session_data_from_discharge_state,
)
from .smart_battery.types import (
    create_charge_session as _create_charge_session,
)
from .smart_battery.types import (
    create_discharge_session as _create_discharge_session,
)

if TYPE_CHECKING:
    import datetime
    from collections.abc import Callable, Coroutine

    from homeassistant.core import HomeAssistant, ServiceCall

    from .foxess.inverter import ScheduleGroup

_LOGGER = logging.getLogger(__name__)


def _api_error_handler(
    func: Callable[[ServiceCall], Coroutine[Any, Any, None]],
) -> Callable[[ServiceCall], Coroutine[Any, Any, None]]:
    """Wrap service handlers to translate API errors into HomeAssistantError."""
    import functools

    import requests

    from .foxess.client import FoxESSApiError

    @functools.wraps(func)
    async def wrapper(call: ServiceCall) -> None:
        try:
            await func(call)
        except (ServiceValidationError, HomeAssistantError):
            raise
        except FoxESSApiError as err:
            raise HomeAssistantError(
                f"FoxESS API error: {err}",
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err
        except requests.RequestException as err:
            raise HomeAssistantError(
                "Could not reach FoxESS Cloud API",
                translation_domain=DOMAIN,
                translation_key="api_unreachable",
            ) from err
        except Exception:
            _LOGGER.exception("Unhandled error in service %s", func.__name__)
            raise

    return wrapper


def _register_services(hass: HomeAssistant) -> None:
    """Register inverter control services."""
    # Late import only for WS/listener functions that live in __init__.py
    # and depend on heavy HA lifecycle state (adapters, WS objects).
    import custom_components.foxess_control as _pkg

    _setup_smart_charge_listeners = _pkg._setup_smart_charge_listeners
    _setup_smart_discharge_listeners = _pkg._setup_smart_discharge_listeners
    _should_start_realtime_ws = _pkg._should_start_realtime_ws
    _maybe_start_realtime_ws = _pkg._maybe_start_realtime_ws
    _stop_realtime_ws = _pkg._stop_realtime_ws

    async def handle_clear_overrides(call: ServiceCall) -> None:
        mode_filter: str | None = call.data.get("mode")

        ws_stops: list[Any] = []
        if mode_filter is None or mode_filter == WorkMode.FORCE_CHARGE.value:
            ws_stop = _cancel_smart_charge(hass)
            if ws_stop is not None:
                ws_stops.append(ws_stop)
        if mode_filter is None or mode_filter == WorkMode.FORCE_DISCHARGE.value:
            ws_stop = _cancel_smart_discharge(hass)
            if ws_stop is not None:
                ws_stops.append(ws_stop)

        if not _should_start_realtime_ws(hass) and not ws_stops:
            ws_stops.append(_stop_realtime_ws(hass))

        if _cfg(hass).entity_mode:
            _LOGGER.info("Clearing overrides via entity backend, setting SelfUse")
            await _apply_mode_via_entities(hass, WorkMode.SELF_USE)
        elif mode_filter is None:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _cfg(hass).min_soc_on_grid
            schedule = await hass.async_add_executor_job(inverter.get_schedule)
            _check_schedule_safe(schedule.get("groups", []), hass)
            _LOGGER.info("Clearing all overrides, setting SelfUse")
            await hass.async_add_executor_job(inverter.self_use, min_soc_on_grid)
        else:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _cfg(hass).min_soc_on_grid
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

        # Dispatch WS linger as background tasks so the service call returns
        # promptly.  The linger waits up to 30s for a final data push (D-009);
        # awaiting it inline would block the HA HTTP response and risk a
        # ReadTimeout for the caller under load.
        for coro in ws_stops:
            hass.async_create_task(coro, name="foxess_stop_ws_clear_overrides")

    async def handle_force_charge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        start_time: datetime.time | None = call.data.get("start_time")
        force: bool = call.data.get("replace_conflicts", False)
        start, end = _resolve_start_end(duration, start_time)

        _LOGGER.info(
            "Force charge %02d:%02d - %02d:%02d (full power)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
        )

        await _do_smart_charge(
            start=start,
            end=end,
            target_soc=100,
            max_power=None,
            replace_conflicts=force,
            full_power=True,
        )

    async def handle_force_discharge(call: ServiceCall) -> None:
        duration: datetime.timedelta = call.data["duration"]
        start_time: datetime.time | None = call.data.get("start_time")
        force: bool = call.data.get("replace_conflicts", False)
        start, end = _resolve_start_end(duration, start_time)

        _LOGGER.info(
            "Force discharge %02d:%02d - %02d:%02d (full power)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
        )

        await _do_smart_discharge(
            start=start,
            end=end,
            min_soc=_cfg(hass).api_min_soc,
            power=None,
            replace_conflicts=force,
            feedin_energy_limit=None,
            full_power=True,
        )

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

        if _cfg(hass).entity_mode:
            await _apply_mode_via_entities(hass, WorkMode.FEEDIN, power)
        else:
            inverter = _get_inverter(hass)
            min_soc_on_grid = _cfg(hass).min_soc_on_grid
            api_min_soc = _cfg(hass).api_min_soc

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

    async def _do_smart_discharge(
        *,
        start: datetime.datetime,
        end: datetime.datetime,
        min_soc: int,
        power: int | None,
        replace_conflicts: bool,
        feedin_energy_limit: float | None,
        full_power: bool = False,
    ) -> None:
        """Core smart discharge logic shared by smart_discharge and force_discharge."""
        if not full_power and _get_current_soc(hass) is None:
            raise ServiceValidationError(
                "Battery SoC is not available",
                translation_domain=DOMAIN,
                translation_key="soc_unavailable",
            )

        api_min_soc = _cfg(hass).api_min_soc
        inverter: Inverter | None = None

        ws_stop = _cancel_smart_discharge(hass)
        if ws_stop is not None:
            hass.async_create_task(ws_stop, name="foxess_stop_ws_smart_discharge")

        if _dd(hass).smart_charge_state is not None:
            _LOGGER.info("Smart discharge: cancelling active smart charge session")
            ws_stop = _cancel_smart_charge(hass)
            if ws_stop is not None:
                hass.async_create_task(ws_stop, name="foxess_stop_ws_smart_discharge")

        max_power_w = power if power is not None else _cfg(hass).max_power_w
        battery_capacity_kwh = _cfg(hass).battery_capacity_kwh
        pacing_enabled = not full_power and battery_capacity_kwh > 0

        current_soc = _get_current_soc(hass)
        now = dt_util.now()
        headroom = _cfg(hass).smart_headroom
        net_consumption = _get_net_consumption(hass)
        should_defer = False
        if pacing_enabled and current_soc is not None:
            deferred_start = _calculate_discharge_deferred_start(
                current_soc,
                min_soc,
                battery_capacity_kwh,
                max_power_w,
                end,
                net_consumption_kw=net_consumption,
                start=start,
                headroom=headroom,
                taper_profile=_get_taper_profile(hass),
                feedin_energy_limit_kwh=feedin_energy_limit,
                grid_export_limit_w=_cfg(hass).grid_export_limit_w,
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
            if (
                pacing_enabled
                and current_soc is not None
                and _cfg(hass).grid_export_limit_w == 0
            ):
                remaining = (end - now).total_seconds() / 3600.0
                initial_power = _calculate_discharge_power(
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

        groups: list[ScheduleGroup] = []
        if not should_defer:
            if _cfg(hass).entity_mode:
                await _apply_mode_via_entities(
                    hass,
                    WorkMode.FORCE_DISCHARGE,
                    initial_power,
                    fd_soc=api_min_soc,
                )
            else:
                inverter = _get_inverter(hass)
                min_soc_on_grid = _cfg(hass).min_soc_on_grid
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
                    replace_conflicts,
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

        _dd(hass).smart_error_state = None

        schedule_horizon: str | None = None
        if (
            not full_power
            and not should_defer
            and initial_power > 0
            and battery_capacity_kwh > 0
            and current_soc is not None
        ):
            safe_end = _compute_safe_schedule_end(
                current_soc,
                min_soc,
                battery_capacity_kwh,
                initial_power,
                end,
                safety_factor=_DISCHARGE_SAFETY_FACTOR,
                now=now,
            )
            if safe_end != end:
                schedule_horizon = safe_end.isoformat()

        dd = _dd(hass)
        dd.smart_discharge_state = dict(
            _create_discharge_session(
                start=start,
                end=end,
                min_soc=min_soc,
                max_power_w=max_power_w,
                initial_power=initial_power,
                battery_capacity_kwh=battery_capacity_kwh,
                min_power_change=_cfg(hass).min_power_change,
                pacing_enabled=pacing_enabled,
                current_soc=current_soc,
                net_consumption=net_consumption,
                should_defer=should_defer,
                now=now,
                feedin_energy_limit=feedin_energy_limit,
                schedule_horizon=schedule_horizon,
                groups=groups,
                full_power=full_power,
            )
        )

        _setup_smart_discharge_listeners(hass, inverter)

        assert dd.smart_discharge_state is not None
        await _save_session(
            hass,
            "smart_discharge",
            _session_data_from_discharge_state(dd.smart_discharge_state),
        )

        if should_defer:
            coordinator = _dd(hass).entries[_first_entry_id(hass)].coordinator
            await coordinator.async_request_refresh()
        else:
            await _maybe_start_realtime_ws(hass)

    async def handle_smart_discharge(call: ServiceCall) -> None:
        start_time_val: datetime.time = call.data["start_time"]
        end_time_val: datetime.time = call.data["end_time"]
        power: int | None = call.data.get("power")
        min_soc: int = call.data["min_soc"]
        force: bool = call.data.get("replace_conflicts", False)
        feedin_energy_limit: float | None = call.data.get("feedin_energy_limit_kwh")

        start, end = _resolve_start_end_explicit(start_time_val, end_time_val)

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

        await _do_smart_discharge(
            start=start,
            end=end,
            min_soc=min_soc,
            power=power,
            replace_conflicts=force,
            feedin_energy_limit=feedin_energy_limit,
        )

    async def _do_smart_charge(
        *,
        start: datetime.datetime,
        end: datetime.datetime,
        target_soc: int,
        max_power: int | None,
        replace_conflicts: bool,
        full_power: bool = False,
    ) -> None:
        """Core smart charge logic shared by smart_charge and force_charge."""
        if not full_power:
            if _get_current_soc(hass) is None:
                raise ServiceValidationError(
                    "Battery SoC is not available",
                    translation_domain=DOMAIN,
                    translation_key="soc_unavailable",
                )

            battery_capacity_kwh = _cfg(hass).battery_capacity_kwh
            if battery_capacity_kwh <= 0:
                raise ServiceValidationError(
                    "Battery capacity (kWh) not configured",
                    translation_domain=DOMAIN,
                    translation_key="battery_capacity_not_configured",
                )

            current_soc = _get_current_soc(hass)
            if current_soc is not None and current_soc >= target_soc:
                raise ServiceValidationError(
                    f"Current SoC ({current_soc}%) at or above target ({target_soc}%)",
                    translation_domain=DOMAIN,
                    translation_key="soc_above_target",
                    translation_placeholders={
                        "current_soc": str(current_soc),
                        "target_soc": str(target_soc),
                    },
                )
        else:
            battery_capacity_kwh = _cfg(hass).battery_capacity_kwh
            current_soc = _get_current_soc(hass)

        min_soc_on_grid = _cfg(hass).min_soc_on_grid
        api_min_soc = _cfg(hass).api_min_soc
        effective_max_power = (
            max_power if max_power is not None else _cfg(hass).max_power_w
        )

        entity_mode = _cfg(hass).entity_mode
        inverter: Inverter | None = None

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
                replace_conflicts,
            )

        ws_stop = _cancel_smart_charge(hass)
        if ws_stop is not None:
            hass.async_create_task(ws_stop, name="foxess_stop_ws_smart_charge")
        if _dd(hass).smart_discharge_state is not None:
            _LOGGER.info("Smart charge: cancelling active smart discharge session")
            ws_stop = _cancel_smart_discharge(hass)
            if ws_stop is not None:
                hass.async_create_task(ws_stop, name="foxess_stop_ws_smart_charge")

        now = dt_util.now()
        net_consumption = _get_net_consumption(hass)
        headroom = _cfg(hass).smart_headroom
        should_defer = False
        if not full_power and current_soc is not None:
            deferred_start = _calculate_deferred_start(
                current_soc,
                target_soc,
                battery_capacity_kwh,
                effective_max_power,
                end,
                net_consumption_kw=net_consumption,
                start=start,
                headroom=headroom,
                taper_profile=_get_taper_profile(hass),
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
            if not full_power and current_soc is not None:
                initial_power = _calculate_charge_power(
                    current_soc,
                    target_soc,
                    battery_capacity_kwh,
                    remaining,
                    effective_max_power,
                    net_consumption_kw=net_consumption,
                    headroom=headroom,
                    taper_profile=_get_taper_profile(hass),
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
                assert inverter is not None
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
                    replace_conflicts,
                )
                await hass.async_add_executor_job(
                    inverter.set_schedule,
                    initial_groups,
                )

        min_power_change = _cfg(hass).min_power_change

        _dd(hass).smart_error_state = None

        dd = _dd(hass)
        dd.smart_charge_state = dict(
            _create_charge_session(
                start=start,
                end=end,
                target_soc=target_soc,
                battery_capacity_kwh=battery_capacity_kwh,
                max_power_w=effective_max_power,
                initial_power=initial_power,
                min_soc_on_grid=min_soc_on_grid,
                min_power_change=min_power_change,
                api_min_soc=api_min_soc,
                force=replace_conflicts,
                current_soc=current_soc,
                should_defer=should_defer,
                now=now,
                groups=initial_groups,
                full_power=full_power,
            )
        )

        _setup_smart_charge_listeners(hass, inverter)

        assert dd.smart_charge_state is not None
        await _save_session(
            hass,
            "smart_charge",
            _session_data_from_charge_state(dd.smart_charge_state),
        )
        if should_defer:
            coordinator = _dd(hass).entries[_first_entry_id(hass)].coordinator
            await coordinator.async_request_refresh()
        await _maybe_start_realtime_ws(hass)

    async def handle_smart_charge(call: ServiceCall) -> None:
        start_time_val: datetime.time = call.data["start_time"]
        end_time_val: datetime.time = call.data["end_time"]
        max_power: int | None = call.data.get("power")
        target_soc: int = call.data["target_soc"]
        force: bool = call.data.get("replace_conflicts", False)

        start, end = _resolve_start_end_explicit(start_time_val, end_time_val)

        _LOGGER.info(
            "Smart charge %02d:%02d - %02d:%02d (power=%s, target_soc=%d%%)",
            start.hour,
            start.minute,
            end.hour,
            end.minute,
            f"{max_power}W" if max_power else "max",
            target_soc,
        )

        await _do_smart_charge(
            start=start,
            end=end,
            target_soc=target_soc,
            max_power=max_power,
            replace_conflicts=force,
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_OVERRIDES,
        _api_error_handler(handle_clear_overrides),
        schema=SCHEMA_CLEAR_OVERRIDES,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FEEDIN,
        _api_error_handler(handle_feedin),
        schema=SCHEMA_FEEDIN,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_CHARGE,
        _api_error_handler(handle_force_charge),
        schema=SCHEMA_FORCE_CHARGE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_DISCHARGE,
        _api_error_handler(handle_force_discharge),
        schema=SCHEMA_FORCE_DISCHARGE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SMART_CHARGE,
        _api_error_handler(handle_smart_charge),
        schema=SCHEMA_SMART_CHARGE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SMART_DISCHARGE,
        _api_error_handler(handle_smart_discharge),
        schema=SCHEMA_SMART_DISCHARGE,
    )
