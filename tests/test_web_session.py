"""Tests for FoxESSWebSession — BMS battery temperature fetching.

Verifies that the web session correctly fetches BMS battery temperature
via ``GET /dew/v0/device/detail?id={batteryId}@{batSn}&category=battery``.

The compound ID (``batteryId@batSn``) comes from the WebSocket ``bat``
node.  The ``/dew/v0/`` namespace accepts the web session token from
``/basic/v0/user/login``.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import aiohttp.web
import pytest

# ---------------------------------------------------------------------------
# Helpers — tiny aiohttp test server simulating the FoxESS web portal
# ---------------------------------------------------------------------------

COMPOUND_ID = "2d35cd73-c9a7-40f2-82e6-2f97e712126f@60E5M4805BLF116"


def _create_web_portal_app(
    device_detail_response: dict[str, Any] | None = None,
) -> aiohttp.web.Application:
    """Build a minimal aiohttp app simulating the FoxESS web portal.

    Registers:
    - POST /basic/v0/user/login (auth — returns a session token)
    - GET  /dew/v0/device/detail (battery detail with temperature)
    """
    app = aiohttp.web.Application()

    async def handle_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response(
            {"errno": 0, "result": {"token": "test-token-123"}}
        )

    async def handle_device_detail_get(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        if device_detail_response is not None:
            return aiohttp.web.json_response(device_detail_response)
        return aiohttp.web.json_response(
            {"errno": 41205, "result": None, "msg": "device not found"}
        )

    app.router.add_post("/basic/v0/user/login", handle_login)
    app.router.add_get("/dew/v0/device/detail", handle_device_detail_get)

    return app


async def _start_test_server(
    app: aiohttp.web.Application,
) -> tuple[aiohttp.web.AppRunner, str]:
    """Start the test server and return (runner, base_url)."""
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "localhost", 0)
    await site.start()
    sock = site._server.sockets[0]  # type: ignore[union-attr]
    port = sock.getsockname()[1]
    return runner, f"http://localhost:{port}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBMSBatteryTemperature:
    """BMS temperature fetch via GET /dew/v0/device/detail."""

    @pytest.mark.asyncio
    async def test_temperature_returned_from_device_detail(self) -> None:
        """Battery temperature from /dew/v0/device/detail is returned."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        detail_resp = {
            "errno": 0,
            "result": {
                "battery": {
                    "temperature": {"value": "18.5", "unit": "\u00b0C"},
                    "soc": "75",
                },
            },
        }

        app = _create_web_portal_app(device_detail_response=detail_resp)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp == 18.5
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_as_plain_number(self) -> None:
        """Temperature given directly as a number (not a dict)."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        detail_resp = {
            "errno": 0,
            "result": {
                "battery": {
                    "temperature": 22.3,
                },
            },
        }

        app = _create_web_portal_app(device_detail_response=detail_resp)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp == 22.3
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_as_string_value(self) -> None:
        """Temperature value is a string (as returned by real FoxESS API)."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        detail_resp = {
            "errno": 0,
            "result": {
                "battery": {
                    "temperature": {"value": "19", "unit": "\u00b0C"},
                },
            },
        }

        app = _create_web_portal_app(device_detail_response=detail_resp)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp == 19.0
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_none_when_no_battery_data(self) -> None:
        """Returns None when the response has no battery section."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        detail_resp = {
            "errno": 0,
            "result": {},
        }

        app = _create_web_portal_app(device_detail_response=detail_resp)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp is None
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_none_when_temp_field_missing(self) -> None:
        """Returns None when the battery dict has no temperature key."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        detail_resp = {
            "errno": 0,
            "result": {
                "battery": {
                    "soc": "75",
                },
            },
        }

        app = _create_web_portal_app(device_detail_response=detail_resp)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp is None
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_none_when_endpoint_errors(self) -> None:
        """Returns None gracefully when /dew/v0/device/detail returns an error."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        error_resp = {
            "errno": 40004,
            "result": None,
            "msg": "Loading data. Ensure stable internet",
        }

        app = _create_web_portal_app(device_detail_response=error_resp)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp is None
        finally:
            await runner.cleanup()


class TestBatteryDetailEndpoint:
    """Verify the endpoint is called correctly: GET with query params."""

    @pytest.mark.asyncio
    async def test_uses_get_with_compound_id_and_category(self) -> None:
        """The request must be GET with id=<compound> and category=battery."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        captured: dict[str, Any] = {}

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 0, "result": {"token": "test-token"}}
            )

        async def handle_detail_get(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            captured["method"] = req.method
            captured["id"] = req.query.get("id")
            captured["category"] = req.query.get("category")
            captured["token"] = req.headers.get("token", "")
            return aiohttp.web.json_response(
                {
                    "errno": 0,
                    "result": {
                        "battery": {
                            "temperature": {"value": "20", "unit": "°C"},
                        },
                    },
                }
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        app.router.add_get("/dew/v0/device/detail", handle_detail_get)

        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp == 20.0
                assert captured["method"] == "GET"
                assert captured["id"] == COMPOUND_ID
                assert captured["category"] == "battery"
                assert captured["token"] == "test-token"
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_token_plumbing_end_to_end(self) -> None:
        """Token from login is passed to the GET request headers."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        token_issued = "fresh-session-token-abc"
        received_tokens: list[str] = []

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 0, "result": {"token": token_issued}}
            )

        async def handle_detail_get(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            received_tokens.append(req.headers.get("token", ""))
            token_hdr = req.headers.get("token", "")
            if token_hdr != token_issued:
                return aiohttp.web.json_response(
                    {"errno": 41808, "result": None, "msg": "Token has expired"}
                )
            return aiohttp.web.json_response(
                {
                    "errno": 0,
                    "result": {
                        "battery": {
                            "temperature": {"value": "22", "unit": "°C"},
                        },
                    },
                }
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        app.router.add_get("/dew/v0/device/detail", handle_detail_get)

        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp == 22.0
                assert len(received_tokens) > 0
                assert received_tokens[-1] == token_issued
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_login_failure_returns_none(self) -> None:
        """When login itself fails, temperature returns None gracefully."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 41030, "result": None, "msg": "Username not found"}
            )

        app.router.add_post("/basic/v0/user/login", handle_login)

        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(
                    battery_compound_id=COMPOUND_ID,
                )
                assert temp is None
        finally:
            await runner.cleanup()


class TestCompoundIdFromWebSocket:
    """Verify that map_ws_to_coordinator extracts the battery compound ID."""

    def test_compound_id_extracted_from_ws_message(self) -> None:
        """batteryId + first multipleBatterySoc.batSn → _battery_compound_id."""
        from custom_components.foxess_control.foxess.realtime_ws import (
            map_ws_to_coordinator,
        )

        ws_msg = {
            "result": {
                "node": {
                    "bat": {
                        "power": {"value": "373", "unit": "W"},
                        "soc": 68,
                        "charge": 2,
                        "batteryId": "2d35cd73-c9a7-40f2-82e6-2f97e712126f",
                        "multipleBatterySoc": [
                            {
                                "soc": 68,
                                "charge": 2,
                                "productType": "EQ4800",
                                "batSn": "60E5M4805BLF116",
                            },
                        ],
                    },
                    "solar": {"power": {"value": "0", "unit": "W"}},
                    "grid": {"power": {"value": "21", "unit": "W"}, "gridStatus": 3},
                    "load": {"power": {"value": "352", "unit": "W"}},
                },
            },
        }
        mapped = map_ws_to_coordinator(ws_msg)
        assert mapped["_battery_compound_id"] == COMPOUND_ID

    def test_no_compound_id_without_battery_id(self) -> None:
        """No _battery_compound_id when batteryId is missing from WS."""
        from custom_components.foxess_control.foxess.realtime_ws import (
            map_ws_to_coordinator,
        )

        ws_msg = {
            "result": {
                "node": {
                    "bat": {
                        "power": {"value": "100", "unit": "W"},
                        "soc": 50,
                        "charge": 1,
                    },
                    "solar": {"power": {"value": "0", "unit": "W"}},
                    "grid": {"power": {"value": "0", "unit": "W"}, "gridStatus": 3},
                    "load": {"power": {"value": "100", "unit": "W"}},
                },
            },
        }
        mapped = map_ws_to_coordinator(ws_msg)
        assert "_battery_compound_id" not in mapped

    def test_no_compound_id_without_bat_sn(self) -> None:
        """No _battery_compound_id when multipleBatterySoc is empty."""
        from custom_components.foxess_control.foxess.realtime_ws import (
            map_ws_to_coordinator,
        )

        ws_msg = {
            "result": {
                "node": {
                    "bat": {
                        "power": {"value": "100", "unit": "W"},
                        "soc": 50,
                        "charge": 1,
                        "batteryId": "some-id",
                        "multipleBatterySoc": [],
                    },
                    "solar": {"power": {"value": "0", "unit": "W"}},
                    "grid": {"power": {"value": "0", "unit": "W"}, "gridStatus": 3},
                    "load": {"power": {"value": "100", "unit": "W"}},
                },
            },
        }
        mapped = map_ws_to_coordinator(ws_msg)
        assert "_battery_compound_id" not in mapped
