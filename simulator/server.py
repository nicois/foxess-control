"""FoxESS simulator HTTP/WS server.

Single aiohttp application serving REST API, WebSocket, web auth,
and backchannel endpoints.  Each ``create_app()`` call produces an
independent application with its own ``InverterModel`` so multiple
simulators can run in the same process without cross-contamination.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import WSMsgType, web

from .model import InverterModel

_LOGGER = logging.getLogger(__name__)


def _model(request: web.Request) -> InverterModel:
    result: InverterModel = request.app["model"]
    return result


def _ws_clients(request: web.Request) -> list[web.WebSocketResponse]:
    result: list[web.WebSocketResponse] = request.app["ws_clients"]
    return result


async def _broadcast_ws(app: web.Application, msg: dict[str, Any]) -> None:
    """Send a message to all connected WebSocket clients."""
    ws_clients: list[web.WebSocketResponse] = app["ws_clients"]
    payload = json.dumps(msg)
    dead: list[web.WebSocketResponse] = []
    for ws in ws_clients:
        if ws.closed:
            dead.append(ws)
            continue
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# REST API — /op/v0/
# ---------------------------------------------------------------------------


def _api_response(result: Any, errno: int = 0, msg: str = "success") -> web.Response:
    return web.json_response({"errno": errno, "msg": msg, "result": result})


def _check_fault(request: web.Request) -> web.Response | None:
    model = _model(request)
    if model.active_fault is None:
        return None
    # Decrement remaining count; auto-clear when exhausted
    if model.fault_remaining > 0:
        model.fault_remaining -= 1
        if model.fault_remaining == 0:
            fault = model.active_fault
            model.active_fault = None
            # Return fault response for this last faulted request
            if fault == "api_down":
                return web.Response(status=503, text="Service Unavailable")
            if fault == "rate_limit":
                return _api_response(None, errno=40400, msg="Rate limit")
            if fault == "api_400":
                return web.Response(status=400, text="Bad Request")
            if fault == "api_500":
                return web.Response(status=500, text="Internal Server Error")
            return None
    if model.active_fault == "api_down":
        return web.Response(status=503, text="Service Unavailable")
    if model.active_fault == "rate_limit":
        return _api_response(None, errno=40400, msg="Rate limit")
    if model.active_fault == "api_400":
        return web.Response(status=400, text="Bad Request")
    if model.active_fault == "api_500":
        return web.Response(status=500, text="Internal Server Error")
    return None


async def handle_device_list(request: web.Request) -> web.Response:
    if fault := _check_fault(request):
        return fault
    return _api_response({"data": [{"deviceSN": _model(request).device_sn}]})


async def handle_device_detail(request: web.Request) -> web.Response:
    if fault := _check_fault(request):
        return fault
    return _api_response({"capacity": _model(request).max_power_w / 1050})


async def handle_real_query(request: web.Request) -> web.Response:
    if fault := _check_fault(request):
        return fault
    body = await request.json()
    variables = body.get("variables", [])
    result = _model(request).get_real_time_response(variables)
    return _api_response(result)


async def handle_scheduler_get(request: web.Request) -> web.Response:
    if fault := _check_fault(request):
        return fault
    return _api_response(_model(request).get_schedule_response())


async def handle_scheduler_enable(request: web.Request) -> web.Response:
    if fault := _check_fault(request):
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

    model = _model(request)
    model.set_schedule(groups)
    _LOGGER.info("Schedule set: %d groups", len(model.schedule_groups))
    return _api_response(None)


async def handle_plant_list(request: web.Request) -> web.Response:
    if fault := _check_fault(request):
        return fault
    return _api_response({"data": [{"stationID": _model(request).plant_id}]})


async def handle_battery_soc_get(request: web.Request) -> web.Response:
    if fault := _check_fault(request):
        return fault
    model = _model(request)
    return _api_response(
        {
            "minSoc": model.min_soc,
            "minSocOnGrid": model.min_soc_on_grid,
        }
    )


async def handle_battery_soc_set(request: web.Request) -> web.Response:
    if fault := _check_fault(request):
        return fault
    body = await request.json()
    model = _model(request)
    model.min_soc = body.get("minSoc", model.min_soc)
    model.min_soc_on_grid = body.get("minSocOnGrid", model.min_soc_on_grid)
    return _api_response(None)


# ---------------------------------------------------------------------------
# Web Portal Auth — /basic/v0/
# ---------------------------------------------------------------------------


async def handle_login(request: web.Request) -> web.Response:
    model = _model(request)
    if model.active_fault == "wrong_password":
        return _api_response(None, errno=41038, msg="Incorrect password")
    body = await request.json()
    _LOGGER.info("Login: user=%s", body.get("user"))
    return _api_response({"token": f"sim-token-{model.device_sn}"})


# ---------------------------------------------------------------------------
# WebSocket — /dew/v0/wsmaitian
# ---------------------------------------------------------------------------


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    model = _model(request)
    ws_clients = _ws_clients(request)
    if model.active_fault == "ws_refuse":
        raise web.HTTPForbidden(text="WebSocket refused")
    # No heartbeat — matches FoxESS cloud behaviour where the server
    # does not send pings.  The client's heartbeat=20 sends pings but
    # if the server doesn't respond, aiohttp may close the connection.
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.append(ws)
    _LOGGER.info(
        "WebSocket connected (clients=%d, active=newest only)",
        len(ws_clients),
    )

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if msg.data == "getdata":
                    await ws.send_json(model.get_ws_message())
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                break
    finally:
        if ws in ws_clients:
            ws_clients.remove(ws)
        _LOGGER.info("WebSocket disconnected (clients=%d)", len(ws_clients))

    return ws


async def _ws_push_loop(app: web.Application) -> None:
    """Background task: push WS messages every 5 seconds.

    Only the NEWEST client receives real data — older connections
    receive stale keepalive messages (high timeDiff) that prevent
    the client's receive() from timing out but carry no useful data.
    This replicates FoxESS cloud behaviour where a new web/app login
    takes over the stream but the old connection stays alive.
    """
    model: InverterModel = app["model"]
    ws_clients: list[web.WebSocketResponse] = app["ws_clients"]
    try:
        while True:
            await asyncio.sleep(5)
            if not ws_clients or model.active_fault == "ws_disconnect":
                continue
            msg = json.dumps(model.get_ws_message())
            stale_msg = json.dumps({"errno": 0, "result": {"timeDiff": 999}})
            for i, ws in enumerate(list(ws_clients)):
                if ws.closed:
                    continue
                try:
                    if i == len(ws_clients) - 1:
                        await ws.send_str(msg)
                    else:
                        await ws.send_str(stale_msg)
                except Exception:
                    pass
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Backchannel — /sim/
# ---------------------------------------------------------------------------


async def handle_sim_state(request: web.Request) -> web.Response:
    return web.json_response(_model(request).to_dict())


async def handle_sim_set(request: web.Request) -> web.Response:
    body = await request.json()
    model = _model(request)
    for key, value in body.items():
        if hasattr(model, key):
            setattr(model, key, value)
    # Re-tick to update derived values
    model.tick(0)
    return web.json_response({"ok": True})


async def handle_sim_tick(request: web.Request) -> web.Response:
    body = await request.json()
    seconds = body.get("seconds", 5)
    model = _model(request)
    model.tick(seconds)
    return web.json_response(model.to_dict())


async def handle_sim_fast_forward(request: web.Request) -> web.Response:
    body = await request.json()
    total_seconds = body.get("seconds", 300)
    step = body.get("step", 5)
    ws_delay = body.get("ws_delay", 0.01)

    model = _model(request)
    ws_clients = _ws_clients(request)
    steps = int(total_seconds / step)
    for _ in range(steps):
        model.tick(step)
        if ws_clients:
            await _broadcast_ws(request.app, model.get_ws_message())
            if ws_delay > 0:
                await asyncio.sleep(ws_delay)

    return web.json_response(model.to_dict())


async def handle_sim_fault(request: web.Request) -> web.Response:
    body = await request.json()
    fault_type = body.get("type")
    count = body.get("count", 0)  # 0 = permanent
    model = _model(request)
    ws_clients = _ws_clients(request)
    model.active_fault = fault_type
    model.fault_remaining = count
    _LOGGER.info("Fault injected: %s (count=%d)", fault_type, count)

    if fault_type in ("ws_disconnect", "ws_refuse"):
        for ws in list(ws_clients):
            await ws.close()
        ws_clients.clear()

    return web.json_response({"ok": True, "fault": fault_type})


async def handle_sim_clear_fault(request: web.Request) -> web.Response:
    _model(request).active_fault = None
    return web.json_response({"ok": True})


async def handle_sim_fuzzing(request: web.Request) -> web.Response:
    body = await request.json()
    model = _model(request)
    model.fuzzing = body.get("enabled", True)
    return web.json_response({"ok": True, "fuzzing": model.fuzzing})


async def handle_sim_ws_unit(request: web.Request) -> web.Response:
    body = await request.json()
    model = _model(request)
    model.ws_unit = body.get("unit", "W")
    return web.json_response({"ok": True, "unit": model.ws_unit})


async def handle_sim_ws_stale(request: web.Request) -> web.Response:
    body = await request.json()
    model = _model(request)
    model.ws_time_diff = body.get("timeDiff", 5)
    return web.json_response({"ok": True, "timeDiff": model.ws_time_diff})


async def handle_sim_reset(request: web.Request) -> web.Response:
    _model(request).reset()
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    app = web.Application()
    app["model"] = InverterModel()
    app["ws_clients"] = []

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
    app.router.add_post("/sim/fuzzing", handle_sim_fuzzing)
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
