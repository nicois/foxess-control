"""FoxESS Control — Home Assistant integration for inverter mode management."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time as _time
import uuid
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
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
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    CONF_WORK_MODE_ENTITY,
    CONF_WS_ALL_SESSIONS,
    DEFAULT_API_MIN_SOC,
    DEFAULT_ENTITY_POLLING_INTERVAL,
    DEFAULT_INVERTER_POWER,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_SMART_HEADROOM,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import (
    FoxESSDataCoordinator,
    FoxESSEntityCoordinator,
    get_coordinator_soc,
)
from .foxess import FoxESSClient, FoxESSRealtimeWS, FoxESSWebSession, Inverter, WorkMode

# Re-export schedule utilities from foxess_adapter for backward
# compatibility with tests that import them from __init__.py.
from .foxess_adapter import (  # noqa: F401
    FoxESSCloudAdapter,
    FoxESSEntityAdapter,
    _build_override_group,
    _check_schedule_safe,
    _groups_overlap,
    _is_expired,
    _is_placeholder,
    _merge_with_existing,
    _remove_mode_from_schedule,
    _sanitize_group,
    _to_minutes,
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
from .smart_battery.algorithms import (  # noqa: F401
    should_suspend_discharge as _should_suspend_discharge,
)
from .smart_battery.config_flow_base import build_entity_map as _build_entity_map
from .smart_battery.listeners import (
    setup_smart_charge_listeners as _sb_setup_smart_charge_listeners,
)
from .smart_battery.listeners import (
    setup_smart_discharge_listeners as _sb_setup_smart_discharge_listeners,
)
from .smart_battery.services import (
    resolve_start_end as _resolve_start_end,
)
from .smart_battery.services import (
    resolve_start_end_explicit as _resolve_start_end_explicit,
)
from .smart_battery.session import (
    clear_stored_session as _sb_clear_stored_session,
)
from .smart_battery.session import (
    save_session as _sb_save_session,
)
from .smart_battery.session import (
    session_data_from_charge_state as _session_data_from_charge_state,
)
from .smart_battery.session import (
    session_data_from_discharge_state as _session_data_from_discharge_state,
)
from .smart_battery.taper import TaperProfile as _TaperProfile

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


def _record_error(hass: HomeAssistant, message: str) -> None:
    """Record a session error for UI surfacing (C-026)."""
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return
    prev = domain_data.get("_smart_error_state", {})
    domain_data["_smart_error_state"] = {
        "last_error": message,
        "last_error_at": dt_util.now().isoformat(),
        "error_count": prev.get("error_count", 0) + 1,
    }


# Persist the taper profile to HA Store every N taper observations.
# Charge ticks every 5 min, discharge every 1 min — real-time save
# frequency is 25 min (charge) and 5 min (discharge).
_TAPER_SAVE_EVERY_N = 5

STORAGE_KEY = "foxess_control_sessions"
STORAGE_VERSION = 1

VALID_MODES = [m.value for m in WorkMode]

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
        vol.Required("min_soc"): vol.All(int, vol.Range(min=0, max=100)),
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


def _entity_service_domain(entity_id: str, default: str) -> str:
    """Derive service domain from entity ID (input_select → input_select)."""
    prefix = entity_id.split(".", 1)[0]
    return prefix if prefix.startswith("input_") else default


async def _apply_mode_via_entities(
    hass: HomeAssistant,
    mode: WorkMode,
    power_w: int | None = None,
    fd_soc: int = 11,
) -> None:
    """Set inverter mode by writing to external entities (foxess_modbus interop).

    Sets the work mode via a ``select`` entity and optionally adjusts the
    charge/discharge power limit and min SoC via ``number`` entities.
    Detects ``input_select`` / ``input_number`` entities and uses the
    correct service domain.
    """
    opts = _get_first_entry(hass).options

    _LOGGER.debug(
        "Entity backend: setting mode=%s power=%s fd_soc=%d",
        mode,
        f"{power_w}W" if power_w is not None else "unchanged",
        fd_soc,
    )

    wm_entity = opts[CONF_WORK_MODE_ENTITY]
    mode_option = _ENTITY_MODE_MAP.get(mode)
    if mode_option:
        await hass.services.async_call(
            _entity_service_domain(wm_entity, "select"),
            "select_option",
            {"entity_id": wm_entity, "option": mode_option},
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
                _entity_service_domain(power_entity, "number"),
                "set_value",
                {"entity_id": power_entity, "value": power_w},
            )

    min_soc_entity = opts.get(CONF_MIN_SOC_ENTITY)
    if min_soc_entity and mode == WorkMode.FORCE_DISCHARGE:
        await hass.services.async_call(
            _entity_service_domain(min_soc_entity, "number"),
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


def _get_coordinator_value(hass: HomeAssistant, variable: str) -> float | None:
    """Read a numeric variable from the coordinator."""
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return None
    for key in domain_data:
        if not str(key).startswith("_"):
            entry_data = domain_data.get(key)
            if isinstance(entry_data, dict):
                coordinator = entry_data.get("coordinator")
                if coordinator is not None and coordinator.data:
                    raw = coordinator.data.get(variable)
                    if raw is not None:
                        try:
                            return float(raw)
                        except (ValueError, TypeError):
                            return None
    return None


def _get_taper_profile(hass: HomeAssistant) -> _TaperProfile | None:
    """Return the adaptive taper profile from domain data."""
    return hass.data.get(DOMAIN, {}).get("_taper_profile")  # type: ignore[no-any-return]


async def _save_taper_profile(hass: HomeAssistant, profile: _TaperProfile) -> None:
    """Persist the taper profile to the session Store."""
    store: Store[dict[str, Any]] | None = hass.data.get(DOMAIN, {}).get("_store")
    if store is None:
        return
    stored: dict[str, Any] = await store.async_load() or {}
    stored["taper_profile"] = profile.to_dict()
    await store.async_save(stored)


def _cancel_smart_discharge(hass: HomeAssistant, *, clear_storage: bool = True) -> None:
    """Cancel any active smart discharge listeners and clear stored session."""
    unsubs: list[Callable[[], None]] = hass.data[DOMAIN].get(
        "_smart_discharge_unsubs", []
    )
    for unsub in unsubs:
        unsub()
    hass.data[DOMAIN]["_smart_discharge_unsubs"] = []
    hass.data[DOMAIN].pop("_smart_discharge_state", None)
    hass.data[DOMAIN].pop("_ws_discharge_callback", None)
    # Disconnect WebSocket — no active discharge session needs it
    hass.async_create_task(_stop_realtime_ws(hass))
    if clear_storage and hass.data.get(DOMAIN, {}).get("_store") is not None:
        hass.async_create_task(_clear_stored_session(hass, "smart_discharge"))


async def _save_session(hass: HomeAssistant, key: str, data: dict[str, Any]) -> None:
    """Persist a smart session to storage."""
    store: Store[dict[str, Any]] = hass.data[DOMAIN].get("_store")
    await _sb_save_session(store, key, data)


async def _clear_stored_session(hass: HomeAssistant, key: str) -> None:
    """Remove a smart session from storage."""
    store: Store[dict[str, Any]] | None = hass.data.get(DOMAIN, {}).get("_store")
    await _sb_clear_stored_session(store, key)


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
    # Stop WebSocket if no longer needed (discharge may still need it)
    if not _should_start_realtime_ws(hass):
        hass.async_create_task(_stop_realtime_ws(hass))


def _build_foxess_adapter(
    hass: HomeAssistant,
    inverter: Inverter | None,
    state: dict[str, Any],
) -> FoxESSCloudAdapter | FoxESSEntityAdapter:
    """Build the appropriate adapter for the current session."""
    if _is_entity_mode(hass):
        return FoxESSEntityAdapter(
            entry_options=dict(_get_first_entry(hass).options),
            max_power_w=_get_max_power_w(hass),
        )
    inv = inverter or _get_inverter(hass)
    entry_id = _first_entry_id(hass)
    coordinator = hass.data[DOMAIN][entry_id]["coordinator"]
    adapter = FoxESSCloudAdapter(
        hass=hass,
        inverter=inv,
        min_soc_on_grid=state.get("min_soc_on_grid", _get_min_soc_on_grid(hass)),
        api_min_soc=state.get("api_min_soc", _get_api_min_soc(hass)),
        start=state["start"],
        end=state["end"],
        force=state.get("force", False),
        capacity_kwh=state.get("battery_capacity_kwh", 0),
        soc_getter=lambda: coordinator.data.get("SoC") if coordinator.data else None,
    )
    # Seed the adapter with groups built by the service handler
    if state.get("groups"):
        adapter.set_groups(state["groups"])
    return adapter


def _setup_smart_charge_listeners(
    hass: HomeAssistant,
    inverter: Inverter | None,
) -> None:
    """Register HA listeners for an active smart charge session.

    Delegates to the brand-agnostic smart_battery listeners via a
    FoxESS-specific InverterAdapter.
    """
    state = hass.data[DOMAIN]["_smart_charge_state"]
    adapter = _build_foxess_adapter(hass, inverter, state)
    _sb_setup_smart_charge_listeners(hass, DOMAIN, adapter)  # type: ignore[arg-type]
    # Trigger WS check — needed when ws_all_sessions is enabled
    hass.async_create_task(_maybe_start_realtime_ws(hass))


def _setup_smart_discharge_listeners(
    hass: HomeAssistant,
    inverter: Inverter | None,
) -> None:
    """Register HA listeners for an active smart discharge session.

    Delegates to the brand-agnostic smart_battery listeners via a
    FoxESS-specific InverterAdapter.
    """
    state = hass.data[DOMAIN]["_smart_discharge_state"]
    adapter = _build_foxess_adapter(hass, inverter, state)
    cb = _sb_setup_smart_discharge_listeners(hass, DOMAIN, adapter)  # type: ignore[arg-type]

    # Wrap the callback to manage WebSocket lifecycle after each check.
    # The brand-agnostic listener doesn't know about WS — this is
    # FoxESS-specific policy that was previously inline in the listener.
    async def _ws_aware_discharge_cb(now: datetime.datetime) -> None:
        await cb(now)
        await _maybe_start_realtime_ws(hass)

    hass.data[DOMAIN]["_ws_discharge_callback"] = _ws_aware_discharge_cb


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
                taper_profile=_get_taper_profile(hass),
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
            "start_soc": charge_data.get("start_soc"),
        }
        _setup_smart_charge_listeners(hass, inverter)
        await _maybe_start_realtime_ws(hass)
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
            recovered_peak = discharge_data.get("consumption_peak_kw", 0.0)
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
                    consumption_peak_kw=recovered_peak,
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
            "soc_unavailable_count": 0,
            "feedin_energy_limit_kwh": discharge_data.get("feedin_energy_limit_kwh"),
            "feedin_start_kwh": discharge_data.get("feedin_start_kwh"),
            "pacing_enabled": pacing_enabled,
            "battery_capacity_kwh": battery_capacity_kwh,
            "min_power_change": discharge_data.get(
                "min_power_change", DEFAULT_MIN_POWER_CHANGE
            ),
            "discharging_started": discharge_data.get("discharging_started", True),
            "discharging_started_at": (
                datetime.datetime.fromisoformat(
                    discharge_data["discharging_started_at"]
                )
                if discharge_data.get("discharging_started_at")
                else None
            ),
            "consumption_peak_kw": discharge_data.get("consumption_peak_kw", 0.0),
            "start_soc": discharge_data.get("start_soc"),
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


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to a new version."""
    if entry.version < 2:
        # v1 -> v2: no web credentials, WebSocket disabled (safe default)
        _LOGGER.info("Migrating config entry from version %s to 2", entry.version)
        hass.config_entries.async_update_entry(entry, version=2)
    return True


# ---------------------------------------------------------------------------
# WebSocket real-time data lifecycle
# ---------------------------------------------------------------------------

_WS_DEBOUNCE_SECONDS = 10.0


def _should_start_realtime_ws(hass: HomeAssistant) -> bool:
    """Return True if the WebSocket should be active right now.

    Default criteria: cloud mode, web credentials configured, active forced
    discharge with min_soc < 100 (i.e. discharge is actually paced).

    When the ``ws_all_sessions`` option is enabled, the WebSocket is also
    activated during smart charge sessions and force operations (charge /
    discharge / feed-in).
    """
    if _is_entity_mode(hass):
        _LOGGER.debug("WS check: entity mode, skipping")
        return False
    try:
        entry = _get_first_entry(hass)
    except (ServiceValidationError, KeyError):
        _LOGGER.debug("WS check: no config entry")
        return False
    if not entry.data.get(CONF_WEB_USERNAME):
        _LOGGER.debug("WS check: no web credentials configured")
        return False

    domain_data = hass.data.get(DOMAIN, {})
    ws_all = entry.options.get(CONF_WS_ALL_SESSIONS, False)

    # Active forced discharge — only when power is paced below max,
    # which is when house load could exceed discharge power and cause
    # grid import.  At full power there is plenty of headroom.
    ds = domain_data.get("_smart_discharge_state")
    if ds is not None and ds.get("discharging_started", False):
        min_soc = ds.get("min_soc", 0)
        last_pw = ds.get("last_power_w", 0)
        max_pw = ds.get("max_power_w", 0)
        paced = not min_soc >= 100 and last_pw < max_pw
        _LOGGER.debug(
            "WS check: discharge active, min_soc=%s, "
            "last_power=%dW, max_power=%dW, paced=%s",
            min_soc,
            last_pw,
            max_pw,
            paced,
        )
        if paced:
            return True

    # Any *started* smart session when ws_all_sessions is enabled
    if not ws_all:
        _LOGGER.debug(
            "WS check: ws_all_sessions=%s, no paced discharge "
            "(options=%s)",
            ws_all,
            {k: v for k, v in entry.options.items() if "password" not in k.lower()},
        )
        return False
    cs = domain_data.get("_smart_charge_state")
    if (ds is not None and ds.get("discharging_started", False)) or (
        cs is not None and cs.get("charging_started", False)
    ):
        return True

    # Force operations (charge / discharge / feed-in) with a future end time
    force_end: datetime.datetime | None = domain_data.get("_force_op_end")
    return force_end is not None and dt_util.now() < force_end


async def _maybe_start_realtime_ws(hass: HomeAssistant) -> None:
    """Start the WebSocket if conditions are met and it's not running."""
    if not _should_start_realtime_ws(hass):
        return
    domain_data = hass.data[DOMAIN]
    ws_ref = domain_data.get("_realtime_ws")
    if ws_ref is not None and ws_ref.is_active:
        _LOGGER.debug("WS: already running or reconnecting, skipping")
        return
    _LOGGER.debug("WS: conditions met, attempting connection")

    entry = _get_first_entry(hass)
    username = entry.data[CONF_WEB_USERNAME]
    password_md5 = entry.data[CONF_WEB_PASSWORD]  # stored as MD5 hash

    # Get or create web session (reused across discharge sessions)
    web_session: FoxESSWebSession | None = domain_data.get("_web_session")
    if web_session is None:
        _sim = os.environ.get("FOXESS_SIMULATOR_URL")
        web_session = FoxESSWebSession(username, password_md5, base_url=_sim)
        domain_data["_web_session"] = web_session

    # Discover plantId (cached after first call)
    plant_id: str | None = domain_data.get("_plant_id")
    if plant_id is None:
        inverter = _get_inverter(hass)
        try:
            plant_id = await hass.async_add_executor_job(inverter.get_plant_id)
        except Exception:
            _LOGGER.warning(
                "Could not discover plantId for WebSocket, "
                "continuing with REST polling",
                exc_info=True,
            )
            return
        domain_data["_plant_id"] = plant_id

    # Get coordinator for data injection
    entry_id = _first_entry_id(hass)
    coordinator = domain_data[entry_id]["coordinator"]

    async def on_data(ws_data: dict[str, Any]) -> None:
        coordinator.inject_realtime_data(ws_data)
        _trigger_discharge_listener(hass)

    def on_disconnect() -> None:
        _LOGGER.warning("FoxESS WebSocket disconnected, falling back to REST polling")
        # Don't pop _realtime_ws here — the WS object may be
        # reconnecting.  _maybe_start_realtime_ws checks is_active
        # and _stop_realtime_ws handles the final cleanup.
        if coordinator.data is not None:
            coordinator.data["_data_source"] = "api"
            coordinator.async_set_updated_data(dict(coordinator.data))

    _sim = os.environ.get("FOXESS_SIMULATOR_URL")
    _ws_url = _sim.replace("http://", "ws://") + "/dew/v0/wsmaitian" if _sim else None
    ws = FoxESSRealtimeWS(plant_id, web_session, on_data, on_disconnect, ws_url=_ws_url)
    try:
        await ws.async_connect()
        domain_data["_realtime_ws"] = ws
        _LOGGER.info("FoxESS WebSocket real-time data stream active")
    except Exception:
        _LOGGER.warning(
            "Failed to start WebSocket, continuing with REST polling",
            exc_info=True,
        )


_WS_LINGER_TIMEOUT = 30.0  # seconds to wait for one final WS update


async def _stop_realtime_ws(hass: HomeAssistant) -> None:
    """Keep WS alive briefly to capture post-session data, then disconnect.

    After a smart session ends the inverter reverts to self-use, but the
    REST API may still return the old snapshot for up to 5 minutes.  By
    lingering on the WebSocket for one more ~5-second data push we inject
    the fresh post-session values into the coordinator so the overview card
    immediately reflects reality.
    """
    domain_data = hass.data.get(DOMAIN, {})
    ws: FoxESSRealtimeWS | None = domain_data.pop("_realtime_ws", None)
    if ws is None:
        return

    if ws.is_connected:
        received = asyncio.Event()
        original_on_data = ws._on_data  # noqa: SLF001

        async def _linger_on_data(mapped: dict[str, Any]) -> None:
            """Forward one final message then signal completion."""
            await original_on_data(mapped)
            received.set()

        ws._on_data = _linger_on_data  # noqa: SLF001
        _LOGGER.debug(
            "FoxESS WebSocket: lingering up to %.0fs for post-session data",
            _WS_LINGER_TIMEOUT,
        )
        try:
            await asyncio.wait_for(received.wait(), _WS_LINGER_TIMEOUT)
            _LOGGER.info(
                "FoxESS WebSocket: received post-session update, disconnecting"
            )
        except TimeoutError:
            _LOGGER.info(
                "FoxESS WebSocket: linger timeout, disconnecting with stale data"
            )

    await ws.async_disconnect()
    _LOGGER.info("FoxESS WebSocket real-time data stream stopped")

    # Update data source badge immediately so the user sees "API"
    # instead of stale "WS" for up to 5 minutes.
    for _eid, entry_data in domain_data.items():
        if isinstance(entry_data, dict) and "coordinator" in entry_data:
            coord = entry_data["coordinator"]
            if coord.data is not None and coord.data.get("_data_source") == "ws":
                coord.data["_data_source"] = "api"
                coord.async_set_updated_data(dict(coord.data))


async def _start_force_op_ws(
    hass: HomeAssistant,
    end: datetime.datetime,
) -> None:
    """Activate WebSocket for the duration of a force operation.

    Sets ``_force_op_end`` so ``_should_start_realtime_ws`` can see the
    active operation, then schedules cleanup when the window expires.
    """
    domain_data = hass.data.get(DOMAIN, {})

    # Cancel any previous force-op cleanup timer
    cancel_prev: asyncio.TimerHandle | None = domain_data.pop("_force_op_timer", None)
    if cancel_prev is not None:
        cancel_prev.cancel()

    domain_data["_force_op_end"] = end
    await _maybe_start_realtime_ws(hass)

    # Schedule WS stop when the window ends
    delay = max(0.0, (end - dt_util.now()).total_seconds())

    def _on_force_op_expired() -> None:
        domain_data.pop("_force_op_end", None)
        domain_data.pop("_force_op_timer", None)
        if not _should_start_realtime_ws(hass):
            hass.async_create_task(_stop_realtime_ws(hass))

    handle = hass.loop.call_later(delay, _on_force_op_expired)
    domain_data["_force_op_timer"] = handle


def _trigger_discharge_listener(hass: HomeAssistant) -> None:
    """Invoke the discharge listener immediately (debounced)."""
    domain_data = hass.data.get(DOMAIN, {})
    now = _time.monotonic()
    if now - domain_data.get("_ws_last_trigger", 0) < _WS_DEBOUNCE_SECONDS:
        return
    domain_data["_ws_last_trigger"] = now
    cb = domain_data.get("_ws_discharge_callback")
    if cb is not None:
        hass.async_create_task(cb(dt_util.now()))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FoxESS Control from a config entry."""
    try:
        from homeassistant.loader import async_get_integration  # noqa: PLC0415

        integration = await async_get_integration(hass, DOMAIN)
        version = integration.version or "unknown"
    except Exception:
        version = "unknown"
    _LOGGER.info(
        "FoxESS Control %s starting (serial=%s, entity_mode=%s, "
        "ws_all=%s, min_power_change=%s, polling=%s)",
        version,
        entry.data.get(CONF_DEVICE_SERIAL, "?"),
        bool(entry.options.get(CONF_WORK_MODE_ENTITY)),
        entry.options.get(CONF_WS_ALL_SESSIONS, False),
        entry.options.get(CONF_MIN_POWER_CHANGE, DEFAULT_MIN_POWER_CHANGE),
        entry.options.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL),
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("_smart_discharge_unsubs", [])
    hass.data[DOMAIN].setdefault("_smart_charge_unsubs", [])
    hass.data[DOMAIN].setdefault(
        "_store", Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
    )

    # Post-cancel hook: stop WebSocket and clear stale work mode.
    # Called by the brand-agnostic cancel_smart_session after clearing state.
    def _on_session_cancel() -> None:
        if not _should_start_realtime_ws(hass):
            hass.async_create_task(_stop_realtime_ws(hass))
        # Clear work mode immediately so the overview card drops the
        # label without waiting for the next REST poll.
        entry_id = _first_entry_id(hass)
        coordinator = hass.data[DOMAIN][entry_id]["coordinator"]
        if coordinator.data is not None:
            coordinator.data["_work_mode"] = None
            coordinator.async_set_updated_data(dict(coordinator.data))

    hass.data[DOMAIN]["_on_session_cancel"] = _on_session_cancel

    # Load adaptive taper profile from persistent storage
    if "_taper_profile" not in hass.data[DOMAIN]:
        store: Store[dict[str, Any]] = hass.data[DOMAIN]["_store"]
        stored = await store.async_load() or {}
        raw_taper = stored.get("taper_profile")
        profile = _TaperProfile.from_dict(raw_taper) if raw_taper else _TaperProfile()
        if raw_taper and not profile.is_plausible():
            _LOGGER.warning(
                "Taper profile has implausible ratios (likely corrupted "
                "by a sensor unit mismatch); resetting to empty"
            )
            profile = _TaperProfile()
            stored.pop("taper_profile", None)
            await store.async_save(stored)
        hass.data[DOMAIN]["_taper_profile"] = profile

    entity_map = _build_entity_map(entry.options)
    entity_mode = bool(entity_map)
    api_key = entry.data.get(CONF_API_KEY, "")

    sim_url = os.environ.get("FOXESS_SIMULATOR_URL")

    inverter: Inverter | None = None
    if api_key:
        client = FoxESSClient(api_key, base_url=sim_url)
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
        # Close web session used for WebSocket auth
        web_session: FoxESSWebSession | None = hass.data[DOMAIN].pop(
            "_web_session", None
        )
        if web_session is not None:
            await web_session.async_close()
        # Detach debug log handlers and restore logger level
        fox_logger = logging.getLogger("custom_components.foxess_control")
        for handler in hass.data[DOMAIN].get("_debug_log_handlers", []):
            fox_logger.removeHandler(handler)
            original = getattr(handler, "original_level", logging.NOTSET)
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

        # Clear force-op WS tracking
        domain_data = hass.data.get(DOMAIN, {})
        timer: asyncio.TimerHandle | None = domain_data.pop("_force_op_timer", None)
        if timer is not None:
            timer.cancel()
        domain_data.pop("_force_op_end", None)
        if not _should_start_realtime_ws(hass):
            hass.async_create_task(_stop_realtime_ws(hass))

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

        await _start_force_op_ws(hass, end)

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

        await _start_force_op_ws(hass, end)

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

        await _start_force_op_ws(hass, end)

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

        # Decide whether to start discharging now or defer (stay in self-use)
        current_soc = _get_current_soc(hass)
        now = dt_util.now()
        headroom = _get_smart_headroom(hass)
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
                taper_profile=hass.data.get(DOMAIN, {}).get("_taper_profile"),
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

        # Clear any previous error — new session is starting
        hass.data[DOMAIN].pop("_smart_error_state", None)

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
            "soc_unavailable_count": 0,
            "feedin_energy_limit_kwh": feedin_energy_limit,
            "feedin_start_kwh": _get_feedin_energy_kwh(hass),
            "battery_capacity_kwh": battery_capacity_kwh,
            "min_power_change": _get_min_power_change(hass),
            "pacing_enabled": pacing_enabled,
            "discharging_started": not should_defer,
            "discharging_started_at": None if should_defer else now,
            "consumption_peak_kw": max(0.0, net_consumption),
            "start_soc": current_soc,
        }

        _setup_smart_discharge_listeners(hass, inverter)

        await _save_session(
            hass,
            "smart_discharge",
            _session_data_from_discharge_state(
                hass.data[DOMAIN]["_smart_discharge_state"]
            ),
        )

        # Start WebSocket if discharge is immediately active (not deferred)
        if not should_defer:
            await _maybe_start_realtime_ws(hass)

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
            if current_soc is not None:
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

        # Clear any previous error — new session is starting
        hass.data[DOMAIN].pop("_smart_error_state", None)

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
            "start_soc": current_soc,
        }

        _setup_smart_charge_listeners(hass, inverter)

        await _save_session(
            hass,
            "smart_charge",
            _session_data_from_charge_state(hass.data[DOMAIN]["_smart_charge_state"]),
        )
        await _maybe_start_realtime_ws(hass)

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
