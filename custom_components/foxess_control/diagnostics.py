"""Diagnostics support for FoxESS Control."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

from .const import DOMAIN

REDACT_KEYS = {"api_key", "web_password", "web_username", "device_serial"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    domain_data = hass.data.get(DOMAIN, {})
    entry_data = domain_data.get(entry.entry_id, {})

    coordinator = entry_data.get("coordinator")
    coordinator_data = None
    if coordinator is not None and coordinator.data is not None:
        coordinator_data = dict(coordinator.data)

    inverter = entry_data.get("inverter")

    charge_state = domain_data.get("_smart_charge_state")
    discharge_state = domain_data.get("_smart_discharge_state")
    error_state = domain_data.get("_smart_error_state")

    ws = domain_data.get("_realtime_ws")
    ws_info = None
    if ws is not None:
        ws_info = {
            "connected": ws.connected,
            "mode": domain_data.get("_ws_mode"),
        }

    taper = domain_data.get("_taper_profile")

    return async_redact_data(
        {
            "entry": {
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "coordinator": coordinator_data,
            "inverter": {
                "max_power_w": inverter.max_power_w if inverter else None,
            },
            "smart_charge_state": _safe_session(charge_state),
            "smart_discharge_state": _safe_session(discharge_state),
            "error_state": error_state,
            "websocket": ws_info,
            "taper_profile": taper.to_dict() if taper else None,
        },
        REDACT_KEYS,
    )


def _safe_session(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Serialise a session state for diagnostics, converting datetimes."""
    if state is None:
        return None
    result: dict[str, Any] = {}
    for key, value in state.items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result
