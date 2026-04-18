"""Smart session persistence — save and recover charge/discharge sessions.

Sessions are stored in HA's ``Store`` so they survive restarts.  Each
brand integration creates its own store with a domain-specific key.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .const import DEFAULT_API_MIN_SOC

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)
_SAVE_DELAY = 60


async def load_taper_profile(
    store: Store[dict[str, Any]] | None,
) -> Any:
    """Load the adaptive taper profile from the session Store.

    Returns a :class:`~smart_battery.taper.TaperProfile` instance.
    """
    from .taper import TaperProfile

    if store is None:
        return TaperProfile()
    stored: dict[str, Any] = await store.async_load() or {}
    raw = stored.get("taper_profile")
    return TaperProfile.from_dict(raw) if raw else TaperProfile()


async def save_session(
    store: Store[dict[str, Any]] | None,
    key: str,
    data: dict[str, Any],
) -> None:
    """Persist a smart session to storage.

    Uses ``async_delay_save`` to coalesce frequent writes (e.g. every
    listener tick) into a single disk write.  ``EVENT_HOMEASSISTANT_FINAL_WRITE``
    guarantees a flush at shutdown so no data is lost.
    """
    if store is None:
        return
    stored: dict[str, Any] = await store.async_load() or {}
    stored[key] = data
    store.async_delay_save(lambda: stored, _SAVE_DELAY)


async def clear_stored_session(
    store: Store[dict[str, Any]] | None,
    key: str,
) -> None:
    """Remove a smart session from storage."""
    if store is None:
        return
    stored: dict[str, Any] = await store.async_load() or {}
    if key in stored:
        del stored[key]
        await store.async_save(stored)


def _serialise_groups(groups: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Serialise schedule groups for storage (cloud mode only)."""
    if not groups:
        return []
    return [dict(g) for g in groups]


def session_data_from_charge_state(state: dict[str, Any]) -> dict[str, Any]:
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
        "start_soc": state.get("start_soc"),
        "groups": _serialise_groups(state.get("groups")),
    }


def session_data_from_discharge_state(state: dict[str, Any]) -> dict[str, Any]:
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
    data["discharging_started"] = state.get("discharging_started", True)
    started_at = state.get("discharging_started_at")
    data["discharging_started_at"] = started_at.isoformat() if started_at else None
    data["consumption_peak_kw"] = state.get("consumption_peak_kw", 0.0)
    data["start_soc"] = state.get("start_soc")
    data["groups"] = _serialise_groups(state.get("groups"))
    return data


def cancel_smart_session(
    domain_data: dict[str, Any],
    state_key: str,
    unsubs_key: str,
    store: Store[dict[str, Any]] | None,
    storage_key: str,
    hass: HomeAssistant,
    *,
    clear_storage: bool = True,
) -> Any:
    """Cancel an active smart session — unsubscribe listeners and clear state.

    Returns whatever the ``_on_session_cancel`` hook returns (typically a
    coroutine for deferred WS shutdown that the caller should await after
    the override removal completes).
    """
    unsubs: list[Callable[[], None]] = domain_data.get(unsubs_key, [])
    for unsub in unsubs:
        unsub()
    domain_data[unsubs_key] = []
    domain_data.pop(state_key, None)
    if clear_storage and store is not None:
        hass.async_create_task(clear_stored_session(store, storage_key))
    # Brand-specific post-cancel hook (e.g., stop WebSocket)
    hook = domain_data.get("_on_session_cancel")
    if hook is not None:
        return hook()
    return None
