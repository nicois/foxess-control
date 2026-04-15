"""FoxESS simulator HTTP/WS server.

Single aiohttp application serving REST API, WebSocket, web auth,
and backchannel endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import WSMsgType, web

from .model import InverterModel

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_model = InverterModel()
_ws_clients: list[web.WebSocketResponse] = []


async def _broadcast_ws(msg: dict[str, Any]) -> None:
    """Send a message to all connected WebSocket clients."""
    payload = json.dumps(msg)
    dead: list[web.WebSocketResponse] = []
    for ws in _ws_clients:
        if ws.closed:
            dead.append(ws)
            continue
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# REST API — /op/v0/
# ---------------------------------------------------------------------------


def _api_response(result: Any, errno: int = 0, msg: str = "success") -> web.Response:
    return web.json_response({"errno": errno, "msg": msg, "result": result})


def _check_fault() -> web.Response | None:
    if _model.active_fault == "api_down":
        return web.Response(status=503, text="Service Unavailable")
    if _model.active_fault == "rate_limit":
        return _api_response(None, errno=40400, msg="Rate limit")
    return None


async def handle_device_list(request: web.Request) -> web.Response:
    if fault := _check_fault():
        return fault
    return _api_response({"data": [{"deviceSN": _model.device_sn}]})


async def handle_device_detail(request: web.Request) -> web.Response:
    if fault := _check_fault():
        return fault
    return _api_response({"capacity": _model.max_power_w / 1050})


async def handle_real_query(request: web.Request) -> web.Response:
    if fault := _check_fault():
        return fault
    body = await request.json()
    variables = body.get("variables", [])
    result = _model.get_real_time_response(variables)
    return _api_response(result)


async def handle_scheduler_get(request: web.Request) -> web.Response:
    if fault := _check_fault():
        return fault
    return _api_response(_model.get_schedule_response())


async def handle_scheduler_enable(request: web.Request) -> web.Response:
    if fault := _check_fault():
        return fault
    body = await request.json()
    groups = body.get("groups", [])

    # Validate constraints
    for g in groups:
        fd_soc = g.get("fdSoc", 100)
        if fd_soc < 11:
            return _api_response(None, errno=40257, msg="fdSoc < 11")
        min_soc_on_grid = g.get("minSocOnGrid", 0)
        if min_soc_on_grid > fd_soc:
            return _api_response(None, errno=40257, msg="minSocOnGrid > fdSoc")

    # Check for overlaps
    for i, a in enumerate(groups):
        for b in groups[i + 1 :]:
            a_start = a.get("startHour", 0) * 60 + a.get("startMinute", 0)
            a_end = a.get("endHour", 0) * 60 + a.get("endMinute", 0)
            b_start = b.get("startHour", 0) * 60 + b.get("startMinute", 0)
            b_end = b.get("endHour", 0) * 60 + b.get("endMinute", 0)
            if a_start < b_end and b_start < a_end:
                return _api_response(None, errno=42023, msg="Time overlap")

    _model.set_schedule(groups)
    _LOGGER.info("Schedule set: %d groups", len(_model.schedule_groups))
    return _api_response(None)


async def handle_plant_list(request: web.Request) -> web.Response:
    if fault := _check_fault():
        return fault
    return _api_response({"data": [{"stationID": _model.plant_id}]})


async def handle_battery_soc_get(request: web.Request) -> web.Response:
    if fault := _check_fault():
        return fault
    return _api_response(
        {
            "minSoc": _model.min_soc,
            "minSocOnGrid": _model.min_soc_on_grid,
        }
    )


async def handle_battery_soc_set(request: web.Request) -> web.Response:
    if fault := _check_fault():
        return fault
    body = await request.json()
    _model.min_soc = body.get("minSoc", _model.min_soc)
    _model.min_soc_on_grid = body.get("minSocOnGrid", _model.min_soc_on_grid)
    return _api_response(None)


# ---------------------------------------------------------------------------
# Web Portal Auth — /basic/v0/
# ---------------------------------------------------------------------------


async def handle_login(request: web.Request) -> web.Response:
    if _model.active_fault == "wrong_password":
        return _api_response(None, errno=41038, msg="Incorrect password")
    body = await request.json()
    _LOGGER.info("Login: user=%s", body.get("user"))
    return _api_response({"token": f"sim-token-{_model.device_sn}"})


# ---------------------------------------------------------------------------
# WebSocket — /dew/v0/wsmaitian
# ---------------------------------------------------------------------------


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)
    _LOGGER.info("WebSocket connected (clients=%d)", len(_ws_clients) + 1)
    _ws_clients.append(ws)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if msg.data == "getdata":
                    # Send initial message
                    await ws.send_json(_model.get_ws_message())
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                break
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        _LOGGER.info("WebSocket disconnected (clients=%d)", len(_ws_clients))

    return ws


async def _ws_push_loop(app: web.Application) -> None:
    """Background task: push WS messages every 5 seconds."""
    try:
        while True:
            await asyncio.sleep(5)
            if _ws_clients and _model.active_fault != "ws_disconnect":
                await _broadcast_ws(_model.get_ws_message())
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Backchannel — /sim/
# ---------------------------------------------------------------------------


async def handle_sim_state(request: web.Request) -> web.Response:
    return web.json_response(_model.to_dict())


async def handle_sim_set(request: web.Request) -> web.Response:
    body = await request.json()
    for key, value in body.items():
        if hasattr(_model, key):
            setattr(_model, key, value)
    # Re-tick to update derived values
    _model.tick(0)
    return web.json_response({"ok": True})


async def handle_sim_tick(request: web.Request) -> web.Response:
    body = await request.json()
    seconds = body.get("seconds", 5)
    _model.tick(seconds)
    return web.json_response(_model.to_dict())


async def handle_sim_fast_forward(request: web.Request) -> web.Response:
    body = await request.json()
    total_seconds = body.get("seconds", 300)
    step = body.get("step", 5)
    ws_delay = body.get("ws_delay", 0.01)

    steps = int(total_seconds / step)
    for _ in range(steps):
        _model.tick(step)
        if _ws_clients:
            await _broadcast_ws(_model.get_ws_message())
            if ws_delay > 0:
                await asyncio.sleep(ws_delay)

    return web.json_response(_model.to_dict())


async def handle_sim_fault(request: web.Request) -> web.Response:
    body = await request.json()
    fault_type = body.get("type")
    _model.active_fault = fault_type
    _LOGGER.info("Fault injected: %s", fault_type)

    if fault_type == "ws_disconnect":
        for ws in list(_ws_clients):
            await ws.close()
        _ws_clients.clear()

    return web.json_response({"ok": True, "fault": fault_type})


async def handle_sim_clear_fault(request: web.Request) -> web.Response:
    _model.active_fault = None
    return web.json_response({"ok": True})


async def handle_sim_ws_unit(request: web.Request) -> web.Response:
    body = await request.json()
    _model.ws_unit = body.get("unit", "W")
    return web.json_response({"ok": True, "unit": _model.ws_unit})


async def handle_sim_ws_stale(request: web.Request) -> web.Response:
    body = await request.json()
    _model.ws_time_diff = body.get("timeDiff", 5)
    return web.json_response({"ok": True, "timeDiff": _model.ws_time_diff})


async def handle_sim_reset(request: web.Request) -> web.Response:
    _model.reset()
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    app = web.Application()

    # REST API
    app.router.add_post("/op/v0/device/list", handle_device_list)
    app.router.add_get("/op/v0/device/detail", handle_device_detail)
    app.router.add_post("/op/v0/device/real/query", handle_real_query)
    app.router.add_post("/op/v0/device/scheduler/get", handle_scheduler_get)
    app.router.add_post("/op/v0/device/scheduler/enable", handle_scheduler_enable)
    app.router.add_get("/op/v0/device/battery/soc/get", handle_battery_soc_get)
    app.router.add_post("/op/v0/device/battery/soc/set", handle_battery_soc_set)
    app.router.add_post("/op/v0/plant/list", handle_plant_list)

    # Web portal auth
    app.router.add_post("/basic/v0/user/login", handle_login)

    # WebSocket
    app.router.add_get("/dew/v0/wsmaitian", handle_ws)

    # Backchannel
    app.router.add_get("/sim/state", handle_sim_state)
    app.router.add_post("/sim/set", handle_sim_set)
    app.router.add_post("/sim/tick", handle_sim_tick)
    app.router.add_post("/sim/fast_forward", handle_sim_fast_forward)
    app.router.add_post("/sim/fault", handle_sim_fault)
    app.router.add_post("/sim/clear_fault", handle_sim_clear_fault)
    app.router.add_post("/sim/ws_unit", handle_sim_ws_unit)
    app.router.add_post("/sim/ws_stale", handle_sim_ws_stale)
    app.router.add_post("/sim/reset", handle_sim_reset)

    # Background WS push task
    async def start_background(app: web.Application) -> None:
        app["ws_push_task"] = asyncio.create_task(_ws_push_loop(app))

    async def stop_background(app: web.Application) -> None:
        app["ws_push_task"].cancel()
        await app["ws_push_task"]

    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)

    return app
