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

    # Read once, before the environment dict: ``device_type`` is populated
    # as a side effect of the (cached) device-detail fetch behind
    # ``max_power_w``, so it must be resolved first to be reported.
    max_power_w = inverter.max_power_w if inverter else None

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
        # ``Inverter`` exposes the model as ``device_type``; the old
        # ``model`` lookup did not exist, so every report showed null and
        # sent bug triage after a model-specific theory that does not
        # exist (issues #12/#14/#17).
        "inverter_model": inverter.device_type if inverter else None,
        "max_power_w": max_power_w,
        "scheduler_limits": _scheduler_limits(inverter),
        "data_source": (
            coordinator_data.get("data_source") if coordinator_data else None
        ),
    }
    recent_errors = list(getattr(domain_data, "recent_errors", []))

    from ._helpers import _cfg

    try:
        _config = _cfg(hass)
    except Exception:  # diagnostics must never raise
        _config = None
    _entity_mode = _config.entity_mode if _config else False
    # ``None`` rather than False when the config could not be read: setup may
    # not have got that far, and reporting False would be a confident claim
    # about a setting nobody looked at (C-020).
    _handback_enabled = _config.scheduler_handback if _config else None

    return async_redact_data(
        {
            "entry": {
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "coordinator": coordinator_data,
            "inverter": {
                "max_power_w": max_power_w,
            },
            "smart_charge_state": _safe_session(charge_state),
            "smart_discharge_state": _safe_session(discharge_state),
            "error_state": error_state,
            "websocket": ws_info,
            "taper_profile": taper.to_dict() if taper else None,
            "environment": environment,
            "recent_errors": recent_errors,
            "schedule": _schedule_section(domain_data, _entity_mode, inverter),
            "handback": _handback_section(domain_data, _handback_enabled, inverter),
        },
        REDACT_KEYS,
    )


def _scheduler_limits(inverter: Any) -> dict[str, Any] | None:
    """Report the schedule-group ranges the inverter declares it accepts.

    Read from the cached ``/op/v3/device/scheduler/get`` probe taken at
    setup — never triggers a live API call on the diagnostics path.
    Present so a future "errno 40257" report says immediately whether the
    declared ceilings were read and what they were (C-020 / C-026, C-042).

    The snapshot carries the device's **whole** declared ``properties`` map,
    not just the fields this integration consumes: issue #17 rejects writes
    with the ``fdPwr`` ceiling correctly clamped and ``ForceCharge``
    correctly declared, so the cause has to lie in a parameter that was not
    being reported.
    """
    if inverter is None:
        return None
    try:
        snapshot: dict[str, Any] | None = inverter.declared_limits_snapshot
        return snapshot
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return None


def _schedule_section(
    domain_data: FoxESSControlData, entity_mode: bool, inverter: Any = None
) -> dict[str, Any] | str:
    """Report the live schedule snapshot + reconcile outcome from the cache.

    Sourced from the startup reconcile's cached snapshot (no live API call
    on the diagnostics path).  Entity mode has no cloud schedule.

    ``last_write_failure`` is the payload the inverter most recently
    *rejected*, paired with ``last_write_ok_at`` so a historical failure is
    distinguishable from a current one.  Together with
    ``environment.scheduler_limits`` this is the pair of facts needed to
    identify which parameter a device objects to, which errno 40257 itself
    never says (issue #17, C-020 / C-026).
    """
    if entity_mode:
        return "n/a (entity mode)"
    return {
        "as_of": getattr(domain_data, "last_schedule_snapshot_at", None),
        "groups": getattr(domain_data, "last_schedule_snapshot", None),
        "reconcile": getattr(domain_data, "last_schedule_reconcile", None),
        "last_write_failure": getattr(inverter, "last_write_failure", None),
        "last_write_ok_at": getattr(inverter, "last_write_ok_at", None),
    }


def _handback_section(
    domain_data: FoxESSControlData, enabled: bool | None, inverter: Any = None
) -> dict[str, Any]:
    """Report everything the scheduler handback remembered and last did.

    Pure export: nothing here is computed, and nothing here does I/O.  The
    handback is opt-in and off by default, so on almost every install every
    one of its outcomes is a *decline* — which makes this section the only
    place a support request can answer "why is my inverter still
    scheduler-controlled?" for an install where, by design, nothing happened
    (issues #16, #4; C-020, C-026, D-059).  It is therefore reported
    unconditionally: ``enabled: false`` is itself the answer, and a section
    that appeared only once the feature had fired would be missing from every
    report that needed it.

    ``enabled`` is ``null`` when the config could not be read at all — setup
    may have failed part-way — because reporting ``false`` would be a claim
    about a setting nobody looked at.

    ``captured_min_soc_on_grid`` is the user's own floor as read *before* this
    integration ever wrote to that register, and ``0`` is a real value: issue
    #4 is precisely a 0 % floor, which the Mode Scheduler itself refuses to
    accept.  So it is reported verbatim and ``min_soc_capture`` names the two
    cases separately — a falsy test here would render "captured 0" and "never
    captured" identically and quietly erase the feature's own use case.

    ``last_handback`` is ``domain_data.last_handback`` verbatim, declines and
    their reasons included (see ``_handback_teardown._record_outcome``).  A
    plain dict by contract, so this exporter needs no knowledge of the
    handback modules.

    ``scheduler_set_unavailable`` reports whether anything has seen a 404 on
    the master-switch write endpoint (issue #17): ``true`` means handback can
    never work on this hardware, which is a different support answer from
    "handback is broken".  ``null`` when there is no inverter to have learned
    anything — setup may have failed before one existed — because ``false``
    there would be a claim about hardware nobody reached.

    ``scheduler_flag`` is the *last known* master-switch state from
    ``Inverter.scheduler_flag_snapshot``, which never triggers a request —
    ``null`` when nothing has ever read it, which is deliberately distinct
    from "read, and it is off".  Diagnostics is downloaded because something
    is already wrong; fetching it on demand here could hang or rate-limit the
    one download that would have explained the problem.
    """
    captured = getattr(domain_data, "captured_min_soc_on_grid", None)
    snapshot = getattr(inverter, "scheduler_flag_snapshot", None)
    return {
        "enabled": enabled,
        "captured_min_soc_on_grid": captured,
        "min_soc_capture": "never captured" if captured is None else "captured",
        "last_handback": getattr(domain_data, "last_handback", None),
        "scheduler_flag": snapshot if isinstance(snapshot, dict) else None,
        "scheduler_set_unavailable": getattr(
            inverter, "scheduler_set_unavailable", None
        ),
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
