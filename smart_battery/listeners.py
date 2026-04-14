"""Smart charge/discharge listener loops.

These functions register HA time-based listeners that periodically
adjust charge/discharge power using the pacing algorithms.  They are
parameterized by an :class:`~smart_battery.adapter.InverterAdapter`
so the same logic works for any inverter brand.
"""

from __future__ import annotations

import datetime
import logging
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
    DEFAULT_MIN_POWER_CHANGE,
    MAX_SOC_UNAVAILABLE_COUNT,
    SMART_CHARGE_ADJUST_SECONDS,
    SMART_DISCHARGE_CHECK_SECONDS,
)
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


def _get_coordinator_value(
    hass: HomeAssistant,
    domain: str,
    variable: str,
) -> float | None:
    """Read a numeric variable from the first coordinator in domain data."""
    domain_data = hass.data.get(domain)
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


def _get_current_soc(hass: HomeAssistant, domain: str) -> float | None:
    """Get current battery SoC from the coordinator."""
    return _get_coordinator_value(hass, domain, "SoC")


def _get_net_consumption(hass: HomeAssistant, domain: str) -> float:
    """Return net site consumption (loads minus solar) in kW."""
    domain_data = hass.data.get(domain)
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
                        return 0.0
    return 0.0


def _get_feedin_energy_kwh(hass: HomeAssistant, domain: str) -> float | None:
    """Return cumulative grid feed-in energy in kWh from the coordinator."""
    return _get_coordinator_value(hass, domain, "feedin")


def _get_smart_headroom(hass: HomeAssistant, domain: str) -> float:
    """Return the charge headroom as a fraction (e.g. 0.10 for 10%)."""
    from .const import CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM

    domain_data = hass.data.get(domain, {})
    for key in domain_data:
        if not str(key).startswith("_"):
            entry_data = domain_data.get(key)
            if isinstance(entry_data, dict):
                entry = entry_data.get("entry")
                if entry is not None:
                    pct: int = entry.options.get(
                        CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM
                    )
                    return pct / 100.0
    return DEFAULT_SMART_HEADROOM / 100.0


def _get_polling_interval_seconds(hass: HomeAssistant, domain: str) -> int:
    """Return the coordinator's polling interval in seconds."""
    from .const import DEFAULT_POLLING_INTERVAL

    domain_data = hass.data.get(domain, {})
    for key in domain_data:
        if not str(key).startswith("_"):
            entry_data = domain_data.get(key)
            if isinstance(entry_data, dict):
                coordinator = entry_data.get("coordinator")
                if coordinator is not None and coordinator.update_interval is not None:
                    return int(coordinator.update_interval.total_seconds())
    return DEFAULT_POLLING_INTERVAL


def _get_store(hass: HomeAssistant, domain: str) -> Store[dict[str, Any]] | None:
    """Return the session Store from domain data."""
    return hass.data.get(domain, {}).get("_store")  # type: ignore[no-any-return]


def _get_taper_profile(hass: HomeAssistant, domain: str) -> TaperProfile | None:
    """Return the adaptive taper profile from domain data.

    Returns None if the profile has not been loaded yet.  Brand
    integrations should load it on startup via
    :func:`~smart_battery.session.load_taper_profile`.
    """
    return hass.data.get(domain, {}).get("_taper_profile")  # type: ignore[no-any-return]


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


def cancel_smart_charge(
    hass: HomeAssistant,
    domain: str,
    *,
    clear_storage: bool = True,
) -> None:
    """Cancel any active smart charge listeners and clear stored session."""
    cancel_smart_session(
        hass.data[domain],
        "_smart_charge_state",
        "_smart_charge_unsubs",
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
) -> None:
    """Cancel any active smart discharge listeners and clear stored session."""
    cancel_smart_session(
        hass.data[domain],
        "_smart_discharge_state",
        "_smart_discharge_unsubs",
        _get_store(hass, domain),
        "smart_discharge",
        hass,
        clear_storage=clear_storage,
    )


def setup_smart_charge_listeners(
    hass: HomeAssistant,
    domain: str,
    adapter: InverterAdapter,
) -> None:
    """Register HA listeners for an active smart charge session.

    Reads all parameters from ``hass.data[domain]["_smart_charge_state"]``.
    """
    from .types import WorkMode

    state = hass.data[domain]["_smart_charge_state"]
    end: datetime.datetime = state["end"]
    end_utc = dt_util.as_utc(end)
    my_session_id: str = state["session_id"]

    async def _remove_charge_override() -> None:
        await adapter.remove_override(hass, WorkMode.FORCE_CHARGE)

    def _is_my_session() -> bool:
        cur = hass.data[domain].get("_smart_charge_state")
        return cur is not None and cur.get("session_id") == my_session_id

    async def _on_charge_timer_expire(_now: datetime.datetime) -> None:
        if not _is_my_session():
            return
        _LOGGER.info("Smart charge: window ended, removing override")
        charging_started = (
            hass.data[domain]
            .get("_smart_charge_state", {})
            .get("charging_started", False)
        )
        cancel_smart_charge(hass, domain)
        if charging_started:
            await _remove_charge_override()

    async def _adjust_charge_power(_now: datetime.datetime) -> None:
        cur_state = hass.data[domain].get("_smart_charge_state")
        if cur_state is None or cur_state.get("session_id") != my_session_id:
            return

        try:
            await _adjust_charge_power_inner(cur_state)
        except Exception:
            _LOGGER.exception("Smart charge: unexpected error, aborting session")
            try:
                charging_started = cur_state.get("charging_started", False)
                if _is_my_session():
                    cancel_smart_charge(hass, domain)
                    if charging_started:
                        await _remove_charge_override()
            except Exception:
                _LOGGER.exception("Smart charge: cleanup also failed")

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
                charging_started = cur_state.get("charging_started", False)
                if _is_my_session():
                    cancel_smart_charge(hass, domain)
                    if charging_started:
                        await _remove_charge_override()
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
                cancel_smart_charge(hass, domain)
                if charging_started:
                    await _remove_charge_override()
            return

        net_consumption = _get_net_consumption(hass, domain)
        headroom = _get_smart_headroom(hass, domain)
        taper = _get_taper_profile(hass, domain)

        # Record taper observation from previous tick's requested power
        if (
            taper is not None
            and cur_state.get("charging_started")
            and cur_state.get("last_power_w", 0) >= 500
        ):
            actual_kw = _get_coordinator_value(hass, domain, "batChargePower")
            if actual_kw is not None:
                taper.record_charge(
                    cur_soc, cur_state["last_power_w"], actual_kw * 1000
                )
                cur_state["taper_tick"] = cur_state.get("taper_tick", 0) + 1
                if cur_state["taper_tick"] % 3 == 0:
                    hass.async_create_task(_save_taper_profile(hass, domain, taper))

        if not cur_state["charging_started"]:
            # Check if it's time to start deferred charging
            deferred = calculate_deferred_start(
                cur_soc,
                cur_state["target_soc"],
                cur_state["battery_capacity_kwh"],
                cur_state["max_power_w"],
                cur_state["end"],
                net_consumption_kw=net_consumption,
                start=cur_state["start"],
                headroom=headroom,
                taper_profile=taper,
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

            # Time to start charging
            new_power = calculate_charge_power(
                cur_soc,
                cur_state["target_soc"],
                cur_state["battery_capacity_kwh"],
                remaining,
                cur_state["max_power_w"],
                net_consumption_kw=net_consumption,
                headroom=headroom,
                taper_profile=taper,
            )
            await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, new_power)

            # Re-check state after await
            if not _is_my_session():
                return
            cur_state = hass.data[domain]["_smart_charge_state"]

            cur_state["groups"] = []
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
            await save_session(
                _get_store(hass, domain),
                "smart_charge",
                session_data_from_charge_state(cur_state),
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
        new_power = calculate_charge_power(
            cur_soc,
            cur_state["target_soc"],
            cur_state["battery_capacity_kwh"],
            remaining,
            cur_state["max_power_w"],
            net_consumption_kw=net_consumption,
            headroom=headroom,
            charging_started_energy_kwh=cur_state.get("charging_started_energy_kwh"),
            elapsed_since_charge_started=elapsed_since_start,
            effective_charge_window=window_from_start,
            min_power_change_w=cur_state["min_power_change"],
            taper_profile=taper,
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
        await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, new_power)

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
    hass.data[domain]["_smart_charge_unsubs"] = unsubs


def setup_smart_discharge_listeners(
    hass: HomeAssistant,
    domain: str,
    adapter: InverterAdapter,
) -> None:
    """Register HA listeners for an active smart discharge session.

    Reads all parameters from ``hass.data[domain]["_smart_discharge_state"]``.
    """
    from .types import WorkMode

    state = hass.data[domain]["_smart_discharge_state"]
    end: datetime.datetime = state["end"]
    end_utc = dt_util.as_utc(end)
    my_session_id: str = state["session_id"]

    async def _remove_discharge_override() -> None:
        await adapter.remove_override(hass, WorkMode.FORCE_DISCHARGE)

    def _is_my_session() -> bool:
        cur = hass.data[domain].get("_smart_discharge_state")
        return cur is not None and cur.get("session_id") == my_session_id

    def _log_session_end(reason: str) -> None:
        cur = hass.data[domain].get("_smart_discharge_state")
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
        cur = hass.data[domain].get("_smart_discharge_state")
        was_active = cur is not None and cur.get("discharging_started", True)
        _log_session_end("window ended, removing override")
        cancel_smart_discharge(hass, domain)
        if was_active:
            await _remove_discharge_override()

    async def _check_discharge_soc(_now: datetime.datetime) -> None:
        cur_state = hass.data[domain].get("_smart_discharge_state")
        if cur_state is None or cur_state.get("session_id") != my_session_id:
            return

        try:
            await _check_discharge_soc_inner(cur_state)
        except Exception:
            _LOGGER.exception("Smart discharge: unexpected error, aborting session")
            try:
                discharging_started = cur_state.get("discharging_started", False)
                if _is_my_session():
                    cancel_smart_discharge(hass, domain)
                    if discharging_started:
                        await _remove_discharge_override()
            except Exception:
                _LOGGER.exception("Smart discharge: cleanup also failed")

    async def _check_discharge_soc_inner(
        cur_state: dict[str, Any],
    ) -> None:
        # --- Update peak consumption tracker ---
        current_consumption = max(0.0, _get_net_consumption(hass, domain))
        old_peak = cur_state.get("consumption_peak_kw", 0.0)
        cur_state["consumption_peak_kw"] = max(
            current_consumption, old_peak * PEAK_DECAY_PER_TICK
        )

        # --- Deferred self-use phase ---
        if not cur_state.get("discharging_started", True):
            soc_value = _get_current_soc(hass, domain)
            if soc_value is not None and soc_value > cur_state["min_soc"]:
                now_dt = dt_util.now()
                taper = _get_taper_profile(hass, domain)
                peak = cur_state.get("consumption_peak_kw", 0.0)
                deferred = calculate_discharge_deferred_start(
                    soc_value,
                    cur_state["min_soc"],
                    cur_state["battery_capacity_kwh"],
                    cur_state["max_power_w"],
                    cur_state["end"],
                    net_consumption_kw=_get_net_consumption(hass, domain),
                    headroom=_get_smart_headroom(hass, domain),
                    taper_profile=taper,
                    feedin_energy_limit_kwh=cur_state.get("feedin_energy_limit_kwh"),
                    consumption_peak_kw=peak,
                )
                if now_dt < deferred:
                    _LOGGER.debug(
                        "Smart discharge: deferring until ~%02d:%02d "
                        "(SoC=%.1f%%, peak=%.2fkW)",
                        deferred.hour,
                        deferred.minute,
                        soc_value,
                        peak,
                    )
                    return

                # Time to start forced discharge — use paced power
                remaining_h = (cur_state["end"] - now_dt).total_seconds() / 3600.0
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
                    return
                cur_state = hass.data[domain]["_smart_discharge_state"]
                cur_state["last_power_w"] = new_power
                cur_state["discharging_started"] = True
                cur_state["discharging_started_at"] = now_dt
                _LOGGER.info(
                    "Smart discharge: deferred discharge started "
                    "(SoC=%.1f%%, power=%dW)",
                    soc_value,
                    new_power,
                )
                await save_session(
                    _get_store(hass, domain),
                    "smart_discharge",
                    session_data_from_discharge_state(cur_state),
                )
                return
            # SoC <= min_soc during deferred phase — fall through to
            # the SoC threshold check below which will end the session.

        # --- Check feed-in energy limit using cumulative counter ---
        feedin_remaining_for_pacing: float | None = None
        feedin_limit = cur_state.get("feedin_energy_limit_kwh")
        if feedin_limit is not None:
            feedin_now = _get_feedin_energy_kwh(hass, domain)
            if feedin_now is not None:
                feedin_start = cur_state.get("feedin_start_kwh")
                if feedin_start is None:
                    _LOGGER.debug(
                        "Smart discharge: feed-in baseline captured at %.2f kWh",
                        feedin_now,
                    )
                    cur_state["feedin_start_kwh"] = feedin_now
                    feedin_remaining_for_pacing = feedin_limit
                    hass.async_create_task(
                        save_session(
                            _get_store(hass, domain),
                            "smart_discharge",
                            session_data_from_discharge_state(cur_state),
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
                            cancel_smart_discharge(hass, domain)
                            await _remove_discharge_override()
                        return

                    remaining_kwh = feedin_limit - exported
                    feedin_remaining_for_pacing = remaining_kwh
                    poll_seconds = _get_polling_interval_seconds(hass, domain)
                    poll_hours = poll_seconds / 3600

                    feedin_prev: float | None = cur_state.get("feedin_prev_kwh")
                    has_observed = feedin_prev is not None and feedin_now != feedin_prev
                    if has_observed:
                        assert feedin_prev is not None  # narrowed above
                        observed_rate_kw = (feedin_now - feedin_prev) / poll_hours
                    cur_state["feedin_prev_kwh"] = feedin_now

                    if (
                        has_observed
                        and observed_rate_kw > 0
                        and remaining_kwh <= observed_rate_kw * poll_hours
                        and not cur_state.get("feedin_stop_scheduled")
                    ):
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

                        async def _early_stop(_now: datetime.datetime) -> None:
                            if not _is_my_session():
                                return
                            _log_session_end(
                                "early stop triggered (feed-in target ~reached)"
                            )
                            cancel_smart_discharge(hass, domain)
                            await _remove_discharge_override()

                        unsub = async_track_point_in_time(hass, _early_stop, stop_at)
                        hass.data[domain].setdefault(
                            "_smart_discharge_unsubs", []
                        ).append(unsub)
                        cur_state["feedin_stop_scheduled"] = True

        # --- Power pacing ---
        soc_value = _get_current_soc(hass, domain)
        if soc_value is None:
            cur_state["soc_unavailable_count"] = (
                cur_state.get("soc_unavailable_count", 0) + 1
            )
            if cur_state["soc_unavailable_count"] >= MAX_SOC_UNAVAILABLE_COUNT:
                _LOGGER.warning(
                    "Smart discharge: SoC unavailable for %d checks, aborting",
                    cur_state["soc_unavailable_count"],
                )
                discharging_started = cur_state.get("discharging_started", False)
                if _is_my_session():
                    cancel_smart_discharge(hass, domain)
                    if discharging_started:
                        await _remove_discharge_override()
                return
            _LOGGER.debug("Smart discharge: SoC unavailable, skipping adjustment")
            return
        cur_state["soc_unavailable_count"] = 0

        # Record taper observation for discharge
        taper = _get_taper_profile(hass, domain)
        if (
            taper is not None
            and cur_state.get("last_power_w", 0) >= 500
            and not cur_state.get("suspended", False)
        ):
            actual_kw = _get_coordinator_value(hass, domain, "batDischargePower")
            if actual_kw is not None:
                taper.record_discharge(
                    soc_value, cur_state["last_power_w"], actual_kw * 1000
                )
                cur_state["taper_tick"] = cur_state.get("taper_tick", 0) + 1
                if cur_state["taper_tick"] % 5 == 0:
                    hass.async_create_task(_save_taper_profile(hass, domain, taper))

        if cur_state.get("pacing_enabled") and soc_value > cur_state["min_soc"]:
            now_dt = dt_util.now()
            remaining_h = (cur_state["end"] - now_dt).total_seconds() / 3600.0
            net_consumption = _get_net_consumption(hass, domain)
            headroom = _get_smart_headroom(hass, domain)
            peak = cur_state.get("consumption_peak_kw", 0.0)

            # --- Suspend / resume ---
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
                    return
                cur_state = hass.data[domain]["_smart_discharge_state"]
                await save_session(
                    _get_store(hass, domain),
                    "smart_discharge",
                    session_data_from_discharge_state(cur_state),
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
                # Fall through to pacing to re-apply the override

            if cur_state.get("suspended"):
                return
            if remaining_h > 0:
                new_power = calculate_discharge_power(
                    soc_value,
                    cur_state["min_soc"],
                    cur_state["battery_capacity_kwh"],
                    remaining_h,
                    cur_state["max_power_w"],
                    net_consumption_kw=net_consumption,
                    headroom=headroom,
                    feedin_remaining_kwh=feedin_remaining_for_pacing,
                    consumption_peak_kw=peak,
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
                    await adapter.apply_mode(
                        hass,
                        WorkMode.FORCE_DISCHARGE,
                        new_power,
                        fd_soc=cur_state.get("min_soc", 11),
                    )
                    if not _is_my_session():
                        return
                    cur_state = hass.data[domain]["_smart_discharge_state"]
                    await save_session(
                        _get_store(hass, domain),
                        "smart_discharge",
                        session_data_from_discharge_state(cur_state),
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
                cancel_smart_discharge(hass, domain)
                await _remove_discharge_override()
        else:
            cur_state["soc_below_min_count"] = 0

    unsubs: list[Callable[[], None]] = [
        async_track_time_interval(
            hass,
            _check_discharge_soc,
            datetime.timedelta(seconds=SMART_DISCHARGE_CHECK_SECONDS),
        ),
        async_track_point_in_time(hass, _on_timer_expire, end_utc),
    ]
    hass.data[domain]["_smart_discharge_unsubs"] = unsubs
