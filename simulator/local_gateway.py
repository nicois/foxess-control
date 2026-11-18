"""Simulated foxess-local gateway (``/api/v1``).

Serves the subset of the foxess-local REST + SSE contract the
integration's local backend consumes, rendered from the same
:class:`InverterModel` as the simulated cloud API — so a test can drive
physics once and observe it through either backend (C-028).

Shapes follow foxess-local's ``openapi.yaml`` 1.0.0: signed integer watts
(grid + import / - export, battery + charging / - discharging), a
``State`` snapshot, RFC 7807 problem documents with a stable ``code``,
and a ``text/event-stream`` of ``snapshot`` / ``connection`` events.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from .model import InverterModel, ScheduleGroup

PREFIX = "/api/v1"

# Cloud work-mode names (model) -> gateway ScheduleWorkMode names.
_MODE_TO_GATEWAY: dict[str, str] = {
    "SelfUse": "self-use",
    "Feedin": "feed-in",
    "Backup": "backup",
    "ForceCharge": "force-charge",
    "ForceDischarge": "force-discharge",
    "PeakShaving": "peak-shaving",
}


def _model(request: web.Request) -> InverterModel:
    result: InverterModel = request.app["model"]
    return result


def _problem(status: int, code: str, title: str, detail: str = "") -> web.Response:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code,
    }
    if detail:
        body["detail"] = detail
    return web.json_response(
        body, status=status, content_type="application/problem+json"
    )


def _authorised(request: web.Request) -> bool:
    token = _model(request).local_gateway_token
    if token is None:
        return True
    header = request.headers.get("Authorization", "")
    return header == f"Bearer {token}" or request.query.get("token") == token


def _unauthorised() -> web.Response:
    return _problem(401, "unauthorized", "Unauthorized", "missing or invalid token")


def _hhmm(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _gateway_group(index: int, group: ScheduleGroup | None) -> dict[str, Any]:
    if group is None or not group.enable:
        return {
            "index": index,
            "enabled": False,
            "start": "00:00",
            "end": "00:00",
            "work_mode": "self-use",
            "max_soc_pct": 100,
            "min_soc_on_grid_pct": 10,
            "fd_soc_pct": 10,
            "fd_pwr_w": 0,
        }
    return {
        "index": index,
        "enabled": True,
        "start": _hhmm(group.startHour, group.startMinute),
        "end": _hhmm(group.endHour, group.endMinute),
        "work_mode": _MODE_TO_GATEWAY.get(group.workMode, "self-use"),
        "max_soc_pct": 100,
        "min_soc_on_grid_pct": group.minSocOnGrid,
        "fd_soc_pct": group.fdSoc,
        "fd_pwr_w": group.fdPwr,
    }


def _power(model: InverterModel) -> dict[str, int]:
    pv = model._fuzz(model.solar_kw)
    load = model._fuzz(model.load_kw)
    battery = model._fuzz(model.bat_charge_kw - model.bat_discharge_kw)
    grid = model._fuzz(model.grid_import_kw - model.grid_export_kw)
    return {
        "pv_w": round(pv * 1000),
        "grid_w": round(grid * 1000),
        "load_w": round(load * 1000),
        "ac_port_w": round((pv - battery) * 1000),
        "battery_w": round(battery * 1000),
        "eps_w": 0,
    }


def _battery_state(model: InverterModel) -> str:
    if model.bat_charge_kw > 0.01:
        return "charging"
    if model.bat_discharge_kw > 0.01:
        return "discharging"
    return "idle"


_ENERGY_FIELDS: tuple[tuple[str, str], ...] = (
    ("grid_export_kwh", "feedin_total_kwh"),
    ("grid_import_kwh", "grid_consumption_total_kwh"),
    ("pv_kwh", "pv_energy_total_kwh"),
    ("battery_charge_kwh", "charge_total_kwh"),
    ("battery_discharge_kwh", "discharge_total_kwh"),
    ("load_kwh", "loads_total_kwh"),
    ("output_kwh", "generation_total_kwh"),
)


def _energy(model: InverterModel) -> dict[str, Any]:
    """The ``Energy`` schema: 0.1 kWh register counters, null before read."""
    read = model.local_gateway_energy
    energy: dict[str, Any] = {
        field: round(getattr(model, attr), 1) if read else None
        for field, attr in _ENERGY_FIELDS
    }
    energy["as_of"] = model.sim_time.isoformat() if read else None
    energy["sources"] = (
        {field: "register" for field, _ in _ENERGY_FIELDS} if read else {}
    )
    return energy


def render_state(model: InverterModel) -> dict[str, Any]:
    """Build a foxess-local ``State`` body from the model."""
    capacity_wh = round(model.battery_capacity_kwh * 1000)
    soc = round(model.soc)
    active = model.get_active_mode()
    group = model.get_active_group()
    groups = [g for g in model.schedule_groups if g.enable][:8]
    state: dict[str, Any] = {
        "ts": model.sim_time.isoformat(),
        "as_of": model.sim_time.isoformat(),
        "online": model.local_gateway_online,
        "power": _power(model),
        "battery": {
            "soc_pct": soc,
            "state": _battery_state(model),
            "stored_energy_wh": round(capacity_wh * soc / 100),
            "capacity_wh": capacity_wh,
            "pack_voltage_v": None,
            "bms_charge_limit_a": None,
        },
        "work_mode": _MODE_TO_GATEWAY.get(model.work_mode_direct, "self-use"),
        "active_mode": {
            "mode": _MODE_TO_GATEWAY.get(active, "self-use"),
            "source": "schedule" if group is not None else "base",
        },
        "eps_mode": "ups",
        "meter_compensation_w": 0,
        "limits": {"import_w": 30000, "export_w": model.max_grid_export_limit_w},
        "schedule": {
            "enabled": model.scheduler_enabled,
            "groups": [
                _gateway_group(i + 1, groups[i] if i < len(groups) else None)
                for i in range(8)
            ],
        },
        "forward_mode": "telemetry_only",
        "identity": {
            "sn": model.device_sn,
            "model": model.device_type or "",
            "firmware": {"master": "1.73", "slave": "1.03", "manager": "1.71"},
            "verified_profile": "kh10-fw1.73" if model.device_type == "KH10" else None,
            "compatibility": {"live_values": "exact", "schedule": "exact"},
            "writes": {"work_mode": "verified", "schedule": "verified"},
        },
        "inverter_state": "on-grid",
        "grid_connected": True,
        "temperatures": {
            "battery_c": model.battery_temperature,
            "inverter_c": None,
            "ambient_c": None,
            "cell_min_c": None,
            "cell_max_c": None,
        },
    }
    state["energy"] = _energy(model)
    state["write_budget"] = {
        setting: {"limit": 10, "remaining": 10, "window_s": 3600, "next_free_at": None}
        for setting in ("work_mode", "schedule", "power_limits")
    }
    return state


def _snapshot(model: InverterModel) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "ts": model.sim_time.isoformat(),
        "power": _power(model),
        "soc_pct": round(model.soc),
        "battery_state": _battery_state(model),
    }
    if model.local_gateway_energy:
        snapshot["energy"] = _energy(model)
    return snapshot


async def handle_state(request: web.Request) -> web.Response:
    if not _authorised(request):
        return _unauthorised()
    return web.json_response(render_state(_model(request)))


async def handle_config(request: web.Request) -> web.Response:
    if not _authorised(request):
        return _unauthorised()
    return web.json_response(
        {
            "raw_hours": 720,
            "bucket_days": 365,
            "version": "v0.1.1-sim",
            "public_access": _model(request).local_gateway_token is None,
            "time_zone": "UTC (UTC)",
            "time_zone_name": "UTC",
            "forward_mode": "telemetry_only",
            "utc_offset_s": 0,
            "inverter_clock_offset_s": 0,
        }
    )


async def handle_events(request: web.Request) -> web.StreamResponse:
    if not _authorised(request):
        return _unauthorised()
    model = _model(request)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    next_id = int(request.headers.get("Last-Event-ID", "0") or 0) + 1
    was_online: bool | None = None

    async def send(name: str, payload: dict[str, Any]) -> None:
        nonlocal next_id
        frame = f"id: {next_id}\nevent: {name}\ndata: {json.dumps(payload)}\n\n"
        next_id += 1
        await resp.write(frame.encode())

    try:
        while True:
            online = model.local_gateway_online
            if online != was_online and was_online is not None:
                await send("connection", {"state": "online" if online else "offline"})
            was_online = online
            if online:
                await send("snapshot", _snapshot(model))
            await asyncio.sleep(model.local_sse_interval_s)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


def register_routes(app: web.Application) -> None:
    app.router.add_get(f"{PREFIX}/state", handle_state)
    app.router.add_get(f"{PREFIX}/config", handle_config)
    app.router.add_get(f"{PREFIX}/events", handle_events)
