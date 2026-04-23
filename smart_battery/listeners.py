"""Smart charge/discharge listener loops.

These functions register HA time-based listeners that periodically
adjust charge/discharge power using the pacing algorithms.  They are
parameterized by an :class:`~smart_battery.adapter.InverterAdapter`
so the same logic works for any inverter brand.
"""

from __future__ import annotations

import datetime
import logging
import sys
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .algorithms import (
    PEAK_DECAY_PER_TICK,
    calculate_charge_power,
    calculate_deferred_start,
    calculate_discharge_deferred_start,
    calculate_discharge_power,
    should_suspend_discharge,
)
from .const import (
    CIRCUIT_BREAKER_TICKS_BEFORE_ABORT,
    DEFAULT_MIN_POWER_CHANGE,
    MAX_CONSECUTIVE_ADAPTER_ERRORS,
    MAX_SOC_UNAVAILABLE_COUNT,
    SMART_CHARGE_ADJUST_SECONDS,
    SMART_DISCHARGE_CHECK_SECONDS,
)
from .domain_data import get_domain_data, get_first_coordinator
from .session import (
    cancel_smart_session,
    save_session,
    session_data_from_charge_state,
    session_data_from_discharge_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store

    from .adapter import InverterAdapter
    from .taper import TaperProfile

_LOGGER = logging.getLogger(__name__)


def _exc_summary() -> str:
    """One-line summary of the current exception for warning-level logs."""
    exc = sys.exc_info()[1]
    return f"{type(exc).__name__}: {exc}" if exc else "unknown"


def _notify_replay(
    hass: HomeAssistant,
    domain: str,
    session_type: str,
    state: dict[str, Any],
) -> None:
    """Notify the brand integration that a session is eligible for replay."""
    dd = get_domain_data(hass, domain)
    callback = getattr(dd, "on_circuit_breaker_abort", None)
    if callback is not None:
        callback(session_type, state)


def _get_coordinator_value(
    hass: HomeAssistant,
    domain: str,
    variable: str,
) -> float | None:
    """Read a numeric variable from the first coordinator in domain data."""
    coordinator = get_first_coordinator(hass, domain)
    if coordinator is not None and coordinator.data:
        raw = coordinator.data.get(variable)
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                return None
    return None


def _get_current_soc(hass: HomeAssistant, domain: str) -> float | None:
    """Get current battery SoC from the coordinator."""
    return _get_coordinator_value(hass, domain, "SoC")


def _get_net_consumption(hass: HomeAssistant, domain: str) -> float:
    """Return net site consumption (loads minus solar) in kW."""
    coordinator = get_first_coordinator(hass, domain)
    if coordinator is not None and coordinator.data:
        try:
            loads = float(coordinator.data.get("loadsPower", 0))
            pv = float(coordinator.data.get("pvPower", 0))
            return loads - pv
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def _get_feedin_energy_kwh(hass: HomeAssistant, domain: str) -> float | None:
    """Return cumulative grid feed-in energy in kWh from the coordinator."""
    return _get_coordinator_value(hass, domain, "feedin")


def _get_smart_headroom(hass: HomeAssistant, domain: str) -> float:
    """Return the charge headroom as a fraction (e.g. 0.10 for 10%)."""
    from .const import CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM

    dd = get_domain_data(hass, domain)
    for entry_data in dd.entries.values():
        entry = getattr(entry_data, "entry", None)
        if entry is not None:
            pct: int = entry.options.get(CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM)
            return pct / 100.0
    return DEFAULT_SMART_HEADROOM / 100.0


def _get_grid_export_limit(hass: HomeAssistant, domain: str) -> int:
    """Return the grid export limit in watts (0 = no limit)."""
    from .const import CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT

    dd = get_domain_data(hass, domain)
    for entry_data in dd.entries.values():
        entry = getattr(entry_data, "entry", None)
        if entry is not None:
            return int(
                entry.options.get(CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT)
            )
    return DEFAULT_GRID_EXPORT_LIMIT


def _get_polling_interval_seconds(hass: HomeAssistant, domain: str) -> int:
    """Return the coordinator's polling interval in seconds."""
    from .const import DEFAULT_POLLING_INTERVAL

    coordinator = get_first_coordinator(hass, domain)
    if coordinator is not None and coordinator.update_interval is not None:
        return int(coordinator.update_interval.total_seconds())
    return DEFAULT_POLLING_INTERVAL


def _get_bms_temperature(hass: HomeAssistant, domain: str) -> float | None:
    """Return the BMS battery temperature in °C, or None if unavailable."""
    return _get_coordinator_value(hass, domain, "bmsBatteryTemperature")


def _get_store(hass: HomeAssistant, domain: str) -> Store[dict[str, Any]] | None:
    """Return the session Store from domain data."""
    return get_domain_data(hass, domain).store


def _record_error(hass: HomeAssistant, domain: str, message: str) -> None:
    """Record a session error for UI surfacing (C-026)."""
    if domain not in hass.data:
        return
    dd = get_domain_data(hass, domain)
    prev = dd.smart_error_state or {}
    dd.smart_error_state = {
        "last_error": message,
        "last_error_at": dt_util.now().isoformat(),
        "error_count": prev.get("error_count", 0) + 1,
    }
    _create_session_issue(hass, domain, message)


def _create_session_issue(hass: HomeAssistant, domain: str, message: str) -> None:
    """Surface a session abort as an HA Repair issue."""
    from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue

    session_type = "charge" if "harge" in message else "discharge"
    async_create_issue(
        hass,
        domain,
        "session_aborted",
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key="session_aborted",
        translation_placeholders={
            "session_type": session_type,
            "reason": message,
        },
    )


def _clear_session_issue(hass: HomeAssistant, domain: str) -> None:
    """Clear the session abort issue when a new session starts."""
    from homeassistant.helpers.issue_registry import async_delete_issue

    async_delete_issue(hass, domain, "session_aborted")


async def _with_circuit_breaker(
    cur_state: dict[str, Any],
    session_label: str,
    inner_fn: Callable[[dict[str, Any]], Any],
    on_abort: Callable[[], Any],
    hass: HomeAssistant,
    domain: str,
) -> None:
    """Run inner_fn with two-tier circuit breaker protection (C-024).

    Tier 1: after MAX_CONSECUTIVE_ADAPTER_ERRORS consecutive failures,
    open the breaker and hold position.  Tier 2: after
    CIRCUIT_BREAKER_TICKS_BEFORE_ABORT additional ticks with breaker
    open, abort the session and notify replay.
    """
    if cur_state.get("circuit_open"):
        ticks = cur_state.get("circuit_open_ticks", 0) + 1
        cur_state["circuit_open_ticks"] = ticks
        if ticks >= CIRCUIT_BREAKER_TICKS_BEFORE_ABORT:
            try:
                _soc = _get_current_soc(hass, domain)
            except Exception:
                _soc = None
            label = session_label.capitalize()
            _LOGGER.error(
                "Smart %s: circuit breaker open for %d ticks, "
                "aborting session (SoC=%s)",
                session_label,
                ticks,
                _soc,
            )
            _record_error(
                hass,
                domain,
                f"{label} session aborted: adapter unreachable",
            )
            _notify_replay(hass, domain, session_label, dict(cur_state))
            try:
                await on_abort()
            except Exception:
                _LOGGER.exception("Smart %s: cleanup also failed", session_label)
            return
        _LOGGER.warning(
            "Smart %s: circuit breaker open, holding position (tick %d/%d)",
            session_label,
            ticks,
            CIRCUIT_BREAKER_TICKS_BEFORE_ABORT,
        )
        return

    try:
        await inner_fn(cur_state)
        cur_state["consecutive_error_count"] = 0
        if cur_state.get("circuit_open"):
            _LOGGER.info(
                "Smart %s: adapter recovered, circuit breaker reset", session_label
            )
            cur_state["circuit_open"] = False
            cur_state["circuit_open_ticks"] = 0
            cur_state.pop("circuit_open_since", None)
    except Exception:
        count = cur_state.get("consecutive_error_count", 0) + 1
        cur_state["consecutive_error_count"] = count
        if count < MAX_CONSECUTIVE_ADAPTER_ERRORS:
            _LOGGER.warning(
                "Smart %s: transient error (%d/%d), will retry: %s",
                session_label,
                count,
                MAX_CONSECUTIVE_ADAPTER_ERRORS,
                _exc_summary(),
            )
            return
        cur_state["circuit_open"] = True
        cur_state["circuit_open_ticks"] = 0
        cur_state["circuit_open_since"] = dt_util.now().isoformat()
        _LOGGER.warning(
            "Smart %s: %d consecutive errors, circuit breaker open "
            "(holding position for up to %d ticks)",
            session_label,
            count,
            CIRCUIT_BREAKER_TICKS_BEFORE_ABORT,
        )
        label = session_label.capitalize()
        _record_error(
            hass,
            domain,
            f"{label}: adapter errors, holding position (circuit breaker)",
        )


def _get_taper_profile(hass: HomeAssistant, domain: str) -> TaperProfile | None:
    """Return the adaptive taper profile from domain data.

    Returns None if the profile has not been loaded yet.  Brand
    integrations should load it on startup via
    :func:`~smart_battery.session.load_taper_profile`.
    """
    return get_domain_data(hass, domain).taper_profile


async def _save_taper_profile(
    hass: HomeAssistant, domain: str, profile: TaperProfile
) -> None:
    """Persist the taper profile to the session Store."""
    store = _get_store(hass, domain)
    if store is None:
        return
    stored: dict[str, Any] = await store.async_load() or {}
    stored["taper_profile"] = profile.to_dict()
    await store.async_save(stored)


TEMP_STABILITY_SECONDS = 600  # 10 minutes
TEMP_DEFICIT_THRESHOLD = 0.95  # actual must be < 95% of requested


def _record_taper_observation(
    hass: HomeAssistant,
    domain: str,
    taper: TaperProfile | None,
    cur_state: dict[str, Any],
    soc: float,
    coordinator_var: str,
    record_fn_name: str,
    save_every: int,
    interval_seconds: int,
) -> None:
    """Record a taper observation if conditions are met.

    Shared between charge and discharge listeners.

    *interval_seconds* is the listener tick interval (300 for charge,
    60 for discharge).  Temperature-based taper recording requires the
    actual power to stay below ``TEMP_DEFICIT_THRESHOLD`` of the
    requested power for ``TEMP_STABILITY_SECONDS`` consecutive seconds
    before a temperature observation is recorded.
    """
    if taper is None or cur_state.get("last_power_w", 0) < 500:
        cur_state["taper_deficit_streak"] = 0
        return
    actual_kw = _get_coordinator_value(hass, domain, coordinator_var)
    if actual_kw is None:
        cur_state["taper_deficit_streak"] = 0
        return

    actual_w = actual_kw * 1000
    # Use max_power_w (inverter maximum) as the denominator, not
    # last_power_w (paced request).  The taper ratio represents "what
    # fraction of maximum power does the BMS actually accept at this
    # SoC", not "what fraction of the paced request was delivered".
    # When pacing reduces last_power_w below the BMS limit, using it
    # as the denominator yields actual/paced > 1.0, clamped to 1.0,
    # hiding the taper entirely.
    max_power_w = cur_state.get("max_power_w", cur_state["last_power_w"])

    # Always record SoC-based taper (existing behavior, no stability gate)
    getattr(taper, record_fn_name)(soc, max_power_w, actual_w)

    # Temperature recording: require sustained deficit for TEMP_STABILITY_SECONDS
    if actual_w < max_power_w * TEMP_DEFICIT_THRESHOLD:
        cur_state["taper_deficit_streak"] = cur_state.get("taper_deficit_streak", 0) + 1
    else:
        cur_state["taper_deficit_streak"] = 0

    streak_seconds = cur_state["taper_deficit_streak"] * interval_seconds
    if streak_seconds >= TEMP_STABILITY_SECONDS:
        bms_temp = _get_bms_temperature(hass, domain)
        if bms_temp is not None:
            # record_fn_name is "record_charge" or "record_discharge"
            # temp method is "record_charge_temp" or "record_discharge_temp"
            temp_fn_name = record_fn_name + "_temp"
            getattr(taper, temp_fn_name)(bms_temp, soc, max_power_w, actual_w)

    cur_state["taper_tick"] = cur_state.get("taper_tick", 0) + 1
    if cur_state["taper_tick"] % save_every == 0:
        hass.async_create_task(
            _save_taper_profile(hass, domain, taper), name=f"{domain}_save_taper"
        )


def cancel_smart_charge(
    hass: HomeAssistant,
    domain: str,
    *,
    clear_storage: bool = True,
) -> Any:
    """Cancel any active smart charge listeners and clear stored session.

    Returns the deferred WS shutdown coroutine (if any) from the cancel hook.
    Callers should await it AFTER override removal completes.
    """
    return cancel_smart_session(
        get_domain_data(hass, domain),
        "smart_charge_state",
        "smart_charge_unsubs",
        _get_store(hass, domain),
        "smart_charge",
        hass,
        clear_storage=clear_storage,
    )


def cancel_smart_discharge(
    hass: HomeAssistant,
    domain: str,
    *,
    clear_storage: bool = True,
) -> Any:
    """Cancel any active smart discharge listeners and clear stored session.

    Returns the deferred WS shutdown coroutine (if any) from the cancel hook.
    Callers should await it AFTER override removal completes.
    """
    return cancel_smart_session(
        get_domain_data(hass, domain),
        "smart_discharge_state",
        "smart_discharge_unsubs",
        _get_store(hass, domain),
        "smart_discharge",
        hass,
        clear_storage=clear_storage,
    )


def setup_smart_charge_listeners(
    hass: HomeAssistant,
    domain: str,
    adapter: InverterAdapter,
) -> Any:
    """Register HA listeners for an active smart charge session.

    Returns the periodic callback so callers can wrap it (e.g. with
    a WebSocket reconnect check).

    Reads all parameters from the ``smart_charge_state`` attribute of domain data.
    """
    from .types import WorkMode

    _clear_session_issue(hass, domain)
    dd = get_domain_data(hass, domain)
    state = dd.smart_charge_state
    assert state is not None, "smart_charge_state must be set before calling listeners"
    end: datetime.datetime = state["end"]
    end_utc = dt_util.as_utc(end)
    my_session_id: str = state["session_id"]

    async def _remove_charge_override() -> None:
        try:
            await adapter.remove_override(hass, WorkMode.FORCE_CHARGE)
        except Exception:
            _LOGGER.warning(
                "Smart charge: override removal failed, scheduling retry: %s",
                _exc_summary(),
            )
            get_domain_data(hass, domain).pending_override_cleanup = {
                "mode": WorkMode.FORCE_CHARGE.value,
            }

    def _is_my_session() -> bool:
        cur = get_domain_data(hass, domain).smart_charge_state
        return cur is not None and cur.get("session_id") == my_session_id

    async def _on_charge_timer_expire(_now: datetime.datetime) -> None:
        if not _is_my_session():
            return
        _LOGGER.info("Smart charge: window ended, removing override")
        cur = get_domain_data(hass, domain).smart_charge_state
        charging_started = (cur or {}).get("charging_started", False)
        ws_stop = cancel_smart_charge(hass, domain)
        if charging_started:
            await _remove_charge_override()
        if ws_stop is not None:
            await ws_stop

    async def _abort_charge() -> None:
        charging_started = (get_domain_data(hass, domain).smart_charge_state or {}).get(
            "charging_started", False
        )
        if _is_my_session():
            ws_stop = cancel_smart_charge(hass, domain)
            if charging_started:
                await _remove_charge_override()
            if ws_stop is not None:
                await ws_stop

    async def _adjust_charge_power(_now: datetime.datetime) -> None:
        cur_state = get_domain_data(hass, domain).smart_charge_state
        if cur_state is None or cur_state.get("session_id") != my_session_id:
            return

        await _with_circuit_breaker(
            cur_state,
            "charge",
            _adjust_charge_power_inner,
            _abort_charge,
            hass,
            domain,
        )

    async def _adjust_charge_power_inner(
        cur_state: dict[str, Any],
    ) -> None:
        cur_soc = _get_current_soc(hass, domain)
        if cur_soc is None:
            cur_state["soc_unavailable_count"] = (
                cur_state.get("soc_unavailable_count", 0) + 1
            )
            if cur_state["soc_unavailable_count"] >= MAX_SOC_UNAVAILABLE_COUNT:
                _LOGGER.warning(
                    "Smart charge: SoC unavailable for %d checks, aborting",
                    cur_state["soc_unavailable_count"],
                )
                _record_error(
                    hass, domain, "Charge aborted: SoC unavailable for 15 min"
                )
                charging_started = cur_state.get("charging_started", False)
                if _is_my_session():
                    ws_stop = cancel_smart_charge(hass, domain)
                    if charging_started:
                        await _remove_charge_override()
                    if ws_stop is not None:
                        await ws_stop
                return
            _LOGGER.debug("Smart charge: SoC unavailable, skipping adjustment")
            return
        cur_state["soc_unavailable_count"] = 0

        if cur_soc >= cur_state["target_soc"]:
            if not cur_state.get("target_reached"):
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
            cur_state["target_reached"] = False
            _LOGGER.info(
                "Smart charge: SoC %.1f%% dropped below target %d%%, resuming",
                cur_soc,
                cur_state["target_soc"],
            )

        now_dt = dt_util.now()
        remaining = (cur_state["end"] - now_dt).total_seconds() / 3600.0
        if remaining <= 0:
            _LOGGER.info("Smart charge: window expired during adjustment, reverting")
            charging_started = cur_state.get("charging_started", False)
            if _is_my_session():
                ws_stop = cancel_smart_charge(hass, domain)
                if charging_started:
                    await _remove_charge_override()
                if ws_stop is not None:
                    await ws_stop
            return

        net_consumption = _get_net_consumption(hass, domain)
        headroom = _get_smart_headroom(hass, domain)
        taper = _get_taper_profile(hass, domain)
        effective_max = cur_state["max_power_w"]
        cur_state["effective_max_power_w"] = effective_max

        bms_temp = _get_bms_temperature(hass, domain)

        if cur_state.get("charging_started"):
            _record_taper_observation(
                hass,
                domain,
                taper,
                cur_state,
                cur_soc,
                "batChargePower",
                "record_charge",
                save_every=3,
                interval_seconds=SMART_CHARGE_ADJUST_SECONDS,
            )

        if not cur_state["charging_started"]:
            # Check if it's time to start deferred charging
            deferred = calculate_deferred_start(
                cur_soc,
                cur_state["target_soc"],
                cur_state["battery_capacity_kwh"],
                effective_max,
                cur_state["end"],
                net_consumption_kw=net_consumption,
                start=cur_state["start"],
                headroom=headroom,
                taper_profile=taper,
                bms_temp_c=bms_temp,
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
                    effective_max,
                    headroom * 100,
                )
                return

            # Time to start charging
            new_power = calculate_charge_power(
                cur_soc,
                cur_state["target_soc"],
                cur_state["battery_capacity_kwh"],
                remaining,
                effective_max,
                net_consumption_kw=net_consumption,
                headroom=headroom,
                taper_profile=taper,
                bms_temp_c=bms_temp,
            )
            await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, new_power, fd_soc=100)

            # Re-check state after await
            if not _is_my_session():
                return
            _cs = get_domain_data(hass, domain).smart_charge_state
            assert _cs is not None
            cur_state = _cs

            cur_state["groups"] = []
            cur_state["last_power_w"] = new_power
            cur_state["charging_started"] = True
            cur_state["charging_started_at"] = now_dt
            cur_state["start_soc"] = cur_soc
            cur_state["charging_started_energy_kwh"] = (
                cur_soc / 100.0 * cur_state["battery_capacity_kwh"]
            )
            _LOGGER.info(
                "Smart charge: deferred charge started (SoC=%.1f%%, power=%dW)",
                cur_soc,
                new_power,
            )
            await save_session(
                _get_store(hass, domain),
                "smart_charge",
                session_data_from_charge_state(cur_state),
            )
            return

        # D-043: re-defer when ahead of schedule (solar supplement)
        deferred = calculate_deferred_start(
            cur_soc,
            cur_state["target_soc"],
            cur_state["battery_capacity_kwh"],
            effective_max,
            cur_state["end"],
            net_consumption_kw=net_consumption,
            start=cur_state["start"],
            headroom=headroom,
            taper_profile=taper,
            bms_temp_c=bms_temp,
        )
        if now_dt < deferred:
            _LOGGER.info(
                "Smart charge: ahead of schedule (SoC=%.1f%%), "
                "re-deferring until ~%02d:%02d",
                cur_soc,
                deferred.hour,
                deferred.minute,
            )
            await _remove_charge_override()
            if not _is_my_session():
                return
            _cs = get_domain_data(hass, domain).smart_charge_state
            assert _cs is not None
            _cs["charging_started"] = False
            _cs["charging_started_at"] = None
            await save_session(
                _get_store(hass, domain),
                "smart_charge",
                session_data_from_charge_state(_cs),
            )
            return

        # Already charging — adjust power as needed
        if cur_state.get("full_power"):
            new_power = effective_max
        else:
            started_at = cur_state.get("charging_started_at")
            if started_at is not None:
                elapsed_since_start = (now_dt - started_at).total_seconds() / 3600.0
                window_from_start = (
                    cur_state["end"] - started_at
                ).total_seconds() / 3600.0
            else:
                elapsed_since_start = 0.0
                window_from_start = 0.0
            new_power = calculate_charge_power(
                cur_soc,
                cur_state["target_soc"],
                cur_state["battery_capacity_kwh"],
                remaining,
                effective_max,
                net_consumption_kw=net_consumption,
                headroom=headroom,
                charging_started_energy_kwh=cur_state.get(
                    "charging_started_energy_kwh"
                ),
                elapsed_since_charge_started=elapsed_since_start,
                effective_charge_window=window_from_start,
                min_power_change_w=cur_state["min_power_change"],
                taper_profile=taper,
                bms_temp_c=bms_temp,
            )

        if (
            abs(new_power - cur_state["last_power_w"]) < cur_state["min_power_change"]
            and new_power != effective_max
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
        await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, new_power, fd_soc=100)

        # Re-check state after await
        if not _is_my_session():
            return
        await save_session(
            _get_store(hass, domain),
            "smart_charge",
            session_data_from_charge_state(cur_state),
        )

    unsubs: list[Callable[[], None]] = [
        async_track_point_in_time(hass, _on_charge_timer_expire, end_utc),
        async_track_time_interval(
            hass,
            _adjust_charge_power,
            datetime.timedelta(seconds=SMART_CHARGE_ADJUST_SECONDS),
        ),
    ]
    dd.smart_charge_unsubs = unsubs
    return _adjust_charge_power


def setup_smart_discharge_listeners(
    hass: HomeAssistant,
    domain: str,
    adapter: InverterAdapter,
) -> Any:
    """Register HA listeners for an active smart discharge session.

    Reads all parameters from the ``smart_discharge_state`` attribute of domain data.
    """
    from .types import WorkMode

    _clear_session_issue(hass, domain)
    dd = get_domain_data(hass, domain)
    state = dd.smart_discharge_state
    assert state is not None, (
        "smart_discharge_state must be set before calling listeners"
    )
    end: datetime.datetime = state["end"]
    end_utc = dt_util.as_utc(end)
    my_session_id: str = state["session_id"]

    async def _remove_discharge_override() -> None:
        try:
            await adapter.remove_override(hass, WorkMode.FORCE_DISCHARGE)
        except Exception:
            _LOGGER.warning(
                "Smart discharge: override removal failed, scheduling retry: %s",
                _exc_summary(),
            )
            get_domain_data(hass, domain).pending_override_cleanup = {
                "mode": WorkMode.FORCE_DISCHARGE.value,
            }

    def _is_my_session() -> bool:
        cur = get_domain_data(hass, domain).smart_discharge_state
        return cur is not None and cur.get("session_id") == my_session_id

    def _log_session_end(reason: str) -> None:
        cur = get_domain_data(hass, domain).smart_discharge_state
        feedin_str = ""
        if cur is not None:
            feedin_start = cur.get("feedin_start_kwh")
            if feedin_start is not None:
                feedin_now = _get_feedin_energy_kwh(hass, domain)
                if feedin_now is not None:
                    total = feedin_now - feedin_start
                    feedin_str = f", fed in {total:.2f} kWh"
        _LOGGER.info("Smart discharge: %s%s", reason, feedin_str)

    async def _on_timer_expire(_now: datetime.datetime) -> None:
        if not _is_my_session():
            return
        cur = get_domain_data(hass, domain).smart_discharge_state
        was_active = cur is not None and cur.get("discharging_started", True)
        _log_session_end("window ended, removing override")
        ws_stop = cancel_smart_discharge(hass, domain)
        if was_active:
            await _remove_discharge_override()
        if ws_stop is not None:
            await ws_stop

    async def _abort_discharge() -> None:
        discharging_started = (
            get_domain_data(hass, domain).smart_discharge_state or {}
        ).get("discharging_started", False)
        if _is_my_session():
            ws_stop = cancel_smart_discharge(hass, domain)
            if discharging_started:
                await _remove_discharge_override()
            if ws_stop is not None:
                await ws_stop

    async def _check_discharge_soc(_now: datetime.datetime) -> None:
        cur_state = get_domain_data(hass, domain).smart_discharge_state
        if cur_state is None or cur_state.get("session_id") != my_session_id:
            return

        await _with_circuit_breaker(
            cur_state,
            "discharge",
            _check_discharge_soc_inner,
            _abort_discharge,
            hass,
            domain,
        )

    async def _start_deferred_discharge(
        cur_state: dict[str, Any],
    ) -> bool:
        """Try to start deferred discharge. Returns True if caller should return."""
        soc_value = _get_current_soc(hass, domain)
        if soc_value is None or soc_value <= cur_state["min_soc"]:
            return False

        now_dt = dt_util.now()
        taper = _get_taper_profile(hass, domain)
        bms_temp = _get_bms_temperature(hass, domain)
        peak = cur_state.get("consumption_peak_kw", 0.0)
        deferred = calculate_discharge_deferred_start(
            soc_value,
            cur_state["min_soc"],
            cur_state["battery_capacity_kwh"],
            cur_state["max_power_w"],
            cur_state["end"],
            net_consumption_kw=_get_net_consumption(hass, domain),
            start=cur_state.get("start"),
            headroom=_get_smart_headroom(hass, domain),
            taper_profile=taper,
            feedin_energy_limit_kwh=cur_state.get("feedin_energy_limit_kwh"),
            consumption_peak_kw=peak,
            bms_temp_c=bms_temp,
            grid_export_limit_w=_get_grid_export_limit(hass, domain),
        )
        if now_dt < deferred:
            _LOGGER.debug(
                "Smart discharge: deferring until ~%02d:%02d (SoC=%.1f%%, peak=%.2fkW)",
                deferred.hour,
                deferred.minute,
                soc_value,
                peak,
            )
            return True

        remaining_h = (cur_state["end"] - now_dt).total_seconds() / 3600.0
        if _get_grid_export_limit(hass, domain) > 0:
            new_power = cur_state["max_power_w"]
        else:
            new_power = calculate_discharge_power(
                soc_value,
                cur_state["min_soc"],
                cur_state["battery_capacity_kwh"],
                remaining_h,
                cur_state["max_power_w"],
                net_consumption_kw=_get_net_consumption(hass, domain),
                headroom=_get_smart_headroom(hass, domain),
                consumption_peak_kw=peak,
            )
        await adapter.apply_mode(
            hass,
            WorkMode.FORCE_DISCHARGE,
            new_power,
            fd_soc=cur_state.get("min_soc", 11),
        )
        if not _is_my_session():
            return True
        _ds = get_domain_data(hass, domain).smart_discharge_state
        assert _ds is not None
        cur_state = _ds
        cur_state["last_power_w"] = new_power
        cur_state["target_power_w"] = new_power
        cur_state["discharging_started"] = True
        cur_state["discharging_started_at"] = now_dt
        cur_state["start_soc"] = soc_value
        _LOGGER.info(
            "Smart discharge: deferred discharge started (SoC=%.1f%%, power=%dW)",
            soc_value,
            new_power,
        )
        await save_session(
            _get_store(hass, domain),
            "smart_discharge",
            session_data_from_discharge_state(cur_state),
        )
        return True

    async def _check_feedin_limit(
        cur_state: dict[str, Any],
    ) -> tuple[bool, float | None]:
        """Check feed-in energy limit. Returns (should_return, feedin_remaining)."""
        feedin_limit = cur_state.get("feedin_energy_limit_kwh")
        if feedin_limit is None:
            return False, None

        feedin_now = _get_feedin_energy_kwh(hass, domain)
        if feedin_now is None:
            return False, None

        feedin_start = cur_state.get("feedin_start_kwh")
        if feedin_start is None:
            _LOGGER.debug(
                "Smart discharge: feed-in baseline captured at %.2f kWh",
                feedin_now,
            )
            cur_state["feedin_start_kwh"] = feedin_now
            hass.async_create_task(
                save_session(
                    _get_store(hass, domain),
                    "smart_discharge",
                    session_data_from_discharge_state(cur_state),
                ),
                name=f"{domain}_save_discharge_session",
            )
            return False, feedin_limit

        exported = feedin_now - feedin_start
        if exported >= feedin_limit:
            if _is_my_session():
                _log_session_end(
                    f"feed-in energy {exported:.2f} kWh "
                    f"reached limit {feedin_limit:.2f} kWh, "
                    "removing override"
                )
                ws_stop = cancel_smart_discharge(hass, domain)
                await _remove_discharge_override()
                if ws_stop is not None:
                    await ws_stop
            return True, None

        remaining_kwh = feedin_limit - exported
        _maybe_schedule_feedin_stop(cur_state, feedin_now, remaining_kwh, exported)
        return False, remaining_kwh

    def _maybe_schedule_feedin_stop(
        cur_state: dict[str, Any],
        feedin_now: float,
        remaining_kwh: float,
        exported: float,
    ) -> None:
        """Schedule an early stop if feed-in will reach limit within one poll."""
        poll_seconds = _get_polling_interval_seconds(hass, domain)
        poll_hours = poll_seconds / 3600

        feedin_prev: float | None = cur_state.get("feedin_prev_kwh")
        has_observed = feedin_prev is not None and feedin_now != feedin_prev
        observed_rate_kw = 0.0
        if has_observed:
            assert feedin_prev is not None
            observed_rate_kw = (feedin_now - feedin_prev) / poll_hours
        cur_state["feedin_prev_kwh"] = feedin_now

        if not (
            has_observed
            and observed_rate_kw > 0
            and remaining_kwh <= observed_rate_kw * poll_hours
            and not cur_state.get("feedin_stop_scheduled")
        ):
            return

        seconds_to_target = remaining_kwh / observed_rate_kw * 3600
        stop_at = dt_util.utcnow() + datetime.timedelta(seconds=seconds_to_target)
        _LOGGER.info(
            "Smart discharge: scheduling stop in %.0fs "
            "(remaining=%.2f kWh, exported=%.2f kWh, observed=%.1fkW)",
            seconds_to_target,
            remaining_kwh,
            exported,
            observed_rate_kw,
        )

        async def _early_stop(_now: datetime.datetime) -> None:
            if not _is_my_session():
                return
            _log_session_end("early stop triggered (feed-in target ~reached)")
            ws_stop = cancel_smart_discharge(hass, domain)
            await _remove_discharge_override()
            if ws_stop is not None:
                await ws_stop

        unsub = async_track_point_in_time(hass, _early_stop, stop_at)
        get_domain_data(hass, domain).smart_discharge_unsubs.append(unsub)
        cur_state["feedin_stop_scheduled"] = True

    async def _handle_soc_unavailable(cur_state: dict[str, Any]) -> bool:
        """Abort after too many SoC-unavailable checks. Returns True to stop."""
        cur_state["soc_unavailable_count"] = (
            cur_state.get("soc_unavailable_count", 0) + 1
        )
        if cur_state["soc_unavailable_count"] < MAX_SOC_UNAVAILABLE_COUNT:
            _LOGGER.debug("Smart discharge: SoC unavailable, skipping adjustment")
            return True
        _LOGGER.warning(
            "Smart discharge: SoC unavailable for %d checks, aborting",
            cur_state["soc_unavailable_count"],
        )
        _record_error(hass, domain, "Discharge aborted: SoC unavailable")
        discharging_started = cur_state.get("discharging_started", False)
        if _is_my_session():
            ws_stop = cancel_smart_discharge(hass, domain)
            if discharging_started:
                await _remove_discharge_override()
            if ws_stop is not None:
                await ws_stop
        return True

    async def _handle_suspend_resume(
        cur_state: dict[str, Any],
        soc_value: float,
        remaining_h: float,
        net_consumption: float,
        headroom: float,
        peak: float,
    ) -> tuple[bool, bool]:
        """Handle suspend/resume logic. Returns (was_suspended, should_return)."""
        should_suspend_now = remaining_h > 0 and should_suspend_discharge(
            soc_value,
            cur_state["min_soc"],
            cur_state["battery_capacity_kwh"],
            remaining_h,
            net_consumption,
            headroom=headroom,
            consumption_peak_kw=peak,
        )
        was_suspended = cur_state.get("suspended", False)

        if should_suspend_now and not was_suspended:
            _LOGGER.info(
                "Smart discharge: suspending — house consumption "
                "(%.2f kW, peak %.2f kW) would breach min SoC %d%% "
                "(SoC=%.1f%%, remaining=%.2fh)",
                net_consumption,
                peak,
                cur_state["min_soc"],
                soc_value,
                remaining_h,
            )
            cur_state["suspended"] = True
            await _remove_discharge_override()
            if not _is_my_session():
                return was_suspended, True
            _ds = get_domain_data(hass, domain).smart_discharge_state
            assert _ds is not None
            await save_session(
                _get_store(hass, domain),
                "smart_discharge",
                session_data_from_discharge_state(_ds),
            )
        elif was_suspended and not should_suspend_now:
            _LOGGER.info(
                "Smart discharge: resuming — conditions improved "
                "(consumption=%.2f kW, SoC=%.1f%%, remaining=%.2fh)",
                net_consumption,
                soc_value,
                remaining_h,
            )
            cur_state["suspended"] = False

        if cur_state.get("suspended"):
            return was_suspended, True
        return was_suspended, False

    async def _apply_discharge_power(
        cur_state: dict[str, Any],
        soc_value: float,
        remaining_h: float,
        net_consumption: float,
        headroom: float,
        peak: float,
        feedin_remaining: float | None,
        was_suspended: bool,
    ) -> None:
        """Calculate and apply discharge power."""
        if _get_grid_export_limit(hass, domain) > 0:
            new_power = cur_state["max_power_w"]
        else:
            new_power = calculate_discharge_power(
                soc_value,
                cur_state["min_soc"],
                cur_state["battery_capacity_kwh"],
                remaining_h,
                cur_state["max_power_w"],
                net_consumption_kw=net_consumption,
                headroom=headroom,
                feedin_remaining_kwh=feedin_remaining,
                consumption_peak_kw=peak,
            )
        min_change = cur_state.get("min_power_change", DEFAULT_MIN_POWER_CHANGE)
        cur_state["target_power_w"] = new_power

        feedin_self_use = (
            feedin_remaining is not None
            and new_power < min_change
            and cur_state["last_power_w"] != 0
        )
        if feedin_self_use:
            _LOGGER.info(
                "Smart discharge: target %dW below threshold "
                "%dW, switching to self-use",
                new_power,
                min_change,
            )
            cur_state["last_power_w"] = 0
            await adapter.apply_mode(
                hass, WorkMode.SELF_USE, 0, fd_soc=cur_state.get("min_soc", 11)
            )
            if not _is_my_session():
                return
            _ds = get_domain_data(hass, domain).smart_discharge_state
            assert _ds is not None
            await save_session(
                _get_store(hass, domain),
                "smart_discharge",
                session_data_from_discharge_state(_ds),
            )
            return

        power_delta = abs(new_power - cur_state.get("last_power_w", 0))
        should_update = (
            power_delta >= min_change or new_power == cur_state["max_power_w"]
        )
        if was_suspended and not cur_state.get("suspended"):
            should_update = True

        if should_update and new_power != cur_state.get("last_power_w", 0):
            _LOGGER.info(
                "Smart discharge: adjusting power %dW -> %dW "
                "(SoC=%.1f%%, remaining=%.2fh)",
                cur_state["last_power_w"],
                new_power,
                soc_value,
                remaining_h,
            )
            cur_state["last_power_w"] = new_power
        elif not should_update and new_power != cur_state.get("last_power_w", 0):
            _LOGGER.debug(
                "Smart discharge: power change %dW -> %dW "
                "below threshold %dW, skipping",
                cur_state["last_power_w"],
                new_power,
                min_change,
            )
        else:
            _LOGGER.debug(
                "Smart discharge: holding at %dW (SoC=%.1f%%, remaining=%.2fh)",
                new_power,
                soc_value,
                remaining_h,
            )

        await adapter.apply_mode(
            hass,
            WorkMode.FORCE_DISCHARGE,
            cur_state["last_power_w"],
            fd_soc=cur_state.get("min_soc", 11),
        )
        if not _is_my_session():
            return
        _ds = get_domain_data(hass, domain).smart_discharge_state
        assert _ds is not None
        if should_update:
            await save_session(
                _get_store(hass, domain),
                "smart_discharge",
                session_data_from_discharge_state(_ds),
            )

    async def _check_soc_threshold(
        cur_state: dict[str, Any],
        soc_value: float,
    ) -> None:
        """End session when SoC confirmed at/below min_soc."""
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
                ws_stop = cancel_smart_discharge(hass, domain)
                await _remove_discharge_override()
                if ws_stop is not None:
                    await ws_stop
        else:
            cur_state["soc_below_min_count"] = 0

    async def _check_discharge_soc_inner(
        cur_state: dict[str, Any],
    ) -> None:
        # Update peak consumption tracker
        current_consumption = max(0.0, _get_net_consumption(hass, domain))
        old_peak = cur_state.get("consumption_peak_kw", 0.0)
        cur_state["consumption_peak_kw"] = max(
            current_consumption, old_peak * PEAK_DECAY_PER_TICK
        )

        # Deferred self-use phase
        if not cur_state.get(
            "discharging_started", True
        ) and await _start_deferred_discharge(cur_state):
            return

        # Feed-in energy limit
        should_return, feedin_remaining = await _check_feedin_limit(cur_state)
        if should_return:
            return

        # SoC availability
        soc_value = _get_current_soc(hass, domain)
        if soc_value is None:
            await _handle_soc_unavailable(cur_state)
            return
        cur_state["soc_unavailable_count"] = 0

        # Taper observation
        taper = _get_taper_profile(hass, domain)
        if not cur_state.get("suspended", False):
            _record_taper_observation(
                hass,
                domain,
                taper,
                cur_state,
                soc_value,
                "batDischargePower",
                "record_discharge",
                save_every=5,
                interval_seconds=SMART_DISCHARGE_CHECK_SECONDS,
            )

        # Power pacing
        if cur_state.get("pacing_enabled") and soc_value > cur_state["min_soc"]:
            now_dt = dt_util.now()
            remaining_h = (cur_state["end"] - now_dt).total_seconds() / 3600.0
            net_consumption = _get_net_consumption(hass, domain)
            headroom = _get_smart_headroom(hass, domain)
            peak = cur_state.get("consumption_peak_kw", 0.0)

            was_suspended, should_stop = await _handle_suspend_resume(
                cur_state,
                soc_value,
                remaining_h,
                net_consumption,
                headroom,
                peak,
            )
            if should_stop:
                return
            if remaining_h > 0:
                await _apply_discharge_power(
                    cur_state,
                    soc_value,
                    remaining_h,
                    net_consumption,
                    headroom,
                    peak,
                    feedin_remaining,
                    was_suspended,
                )

        # SoC threshold check
        await _check_soc_threshold(cur_state, soc_value)

    unsubs: list[Callable[[], None]] = [
        async_track_time_interval(
            hass,
            _check_discharge_soc,
            datetime.timedelta(seconds=SMART_DISCHARGE_CHECK_SECONDS),
        ),
        async_track_point_in_time(hass, _on_timer_expire, end_utc),
    ]
    dd.smart_discharge_unsubs = unsubs
    return _check_discharge_soc
