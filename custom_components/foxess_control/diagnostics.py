"""Diagnostics support for FoxESS Control."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .domain_data import FoxESSControlData

from .const import DOMAIN

REDACT_KEYS = {
    "api_key",
    "web_password",
    "web_username",
    "device_serial",
    "token",
    "batSn",
    "battery_compound_id",
}


def _integration_version() -> str | None:
    """Read the integration version from manifest.json."""
    import json
    from pathlib import Path

    try:
        manifest = Path(__file__).parent / "manifest.json"
        version = json.loads(manifest.read_text()).get("version")
        return str(version) if version is not None else None
    except Exception:  # diagnostics must never raise
        return None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    domain_data: FoxESSControlData | None = hass.data.get(DOMAIN)
    if domain_data is None:
        return {}

    entry_data = domain_data.entries.get(entry.entry_id)

    coordinator = entry_data.coordinator if entry_data else None
    coordinator_data = None
    if coordinator is not None and coordinator.data is not None:
        coordinator_data = dict(coordinator.data)

    inverter = entry_data.inverter if entry_data else None

    charge_state = domain_data.smart_charge_state
    discharge_state = domain_data.smart_discharge_state
    error_state = domain_data.smart_error_state

    ws = domain_data.realtime_ws
    ws_info = None
    if ws is not None:
        ws_info = {
            "connected": ws.is_connected,
            "mode": domain_data.ws_mode,
        }

    taper = domain_data.taper_profile

    web_session = getattr(domain_data, "web_session", None)
    cloud_base_url = getattr(web_session, "BASE_URL", None)
    ws_connected = bool(ws is not None and getattr(ws, "is_connected", False))
    compound = getattr(domain_data, "battery_compound_id", None)
    compound_status = "discovered" if compound else "missing"
    environment = {
        "integration_version": _integration_version(),
        "cloud_base_url": cloud_base_url,
        "ws_mode": getattr(domain_data, "ws_mode", None),
        "ws_connected": ws_connected,
        "battery_compound_id_status": compound_status,
        "plant_id_present": bool(getattr(domain_data, "plant_id", None)),
        "inverter_model": getattr(inverter, "model", None) if inverter else None,
        "max_power_w": inverter.max_power_w if inverter else None,
        "data_source": (
            coordinator_data.get("data_source") if coordinator_data else None
        ),
    }
    recent_errors = list(getattr(domain_data, "recent_errors", []))

    from ._helpers import _cfg

    try:
        _entity_mode = _cfg(hass).entity_mode
    except Exception:  # diagnostics must never raise
        _entity_mode = False

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
            "environment": environment,
            "recent_errors": recent_errors,
            "schedule": _schedule_section(domain_data, _entity_mode),
        },
        REDACT_KEYS,
    )


def _schedule_section(
    domain_data: FoxESSControlData, entity_mode: bool
) -> dict[str, Any] | str:
    """Report the live schedule snapshot + reconcile outcome from the cache.

    Sourced from the startup reconcile's cached snapshot (no live API call
    on the diagnostics path).  Entity mode has no cloud schedule.
    """
    if entity_mode:
        return "n/a (entity mode)"
    return {
        "as_of": getattr(domain_data, "last_schedule_snapshot_at", None),
        "groups": getattr(domain_data, "last_schedule_snapshot", None),
        "reconcile": getattr(domain_data, "last_schedule_reconcile", None),
    }


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
