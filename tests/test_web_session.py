"""Tests for FoxESSWebSession — BMS battery temperature fetching.

Verifies that the web portal data endpoints are called using HTTP POST
with JSON bodies (matching the FoxESS web portal API contract), not
HTTP GET with query parameters.

The FoxESS web portal JS (bus-device-inverterDetail) calls:
- POST /generic/v0/device/list  -> result.devices[].id (internal device ID)
- POST /generic/v0/device/battery/info {id: deviceId} -> result.batterys[]
The integration uses these to discover battery temperature data.

Previous fix attempts failed because they called /dew/v0/device/detail
with the device SN, but this endpoint does not return battery temperature.
The web portal uses /generic/v0/ endpoints with an internal device ID.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import aiohttp.web
import pytest

# ---------------------------------------------------------------------------
# Helpers — tiny aiohttp test server simulating the FoxESS web portal
# ---------------------------------------------------------------------------


def _create_web_portal_app(
    device_list_response: dict[str, Any] | None = None,
    battery_info_response: dict[str, Any] | None = None,
    device_detail_response: dict[str, Any] | None = None,
) -> aiohttp.web.Application:
    """Build a minimal aiohttp app simulating the FoxESS web portal.

    Registers the endpoints that the real web portal JS calls:
    - /basic/v0/user/login (auth)
    - /generic/v0/device/list (device discovery — returns internal IDs)
    - /generic/v0/device/battery/info (battery data including temperature)
    - /dew/v0/device/detail (legacy endpoint — kept as fallback)
    """
    app = aiohttp.web.Application()

    async def handle_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response(
            {"errno": 0, "result": {"token": "test-token-123"}}
        )

    async def handle_device_list(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        if device_list_response is not None:
            return aiohttp.web.json_response(device_list_response)
        return aiohttp.web.json_response(
            {"errno": 0, "result": {"total": 0, "devices": []}}
        )

    async def handle_battery_info(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        if battery_info_response is not None:
            return aiohttp.web.json_response(battery_info_response)
        return aiohttp.web.json_response({"errno": 0, "result": {"batterys": []}})

    async def handle_device_detail_post(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        if device_detail_response is not None:
            return aiohttp.web.json_response(device_detail_response)
        return aiohttp.web.json_response(
            {"errno": 41205, "result": None, "msg": "device not found"}
        )

    app.router.add_post("/basic/v0/user/login", handle_login)
    app.router.add_post("/generic/v0/device/list", handle_device_list)
    app.router.add_post("/generic/v0/device/battery/info", handle_battery_info)
    app.router.add_post("/dew/v0/device/detail", handle_device_detail_post)

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
    """BMS temperature fetch uses the web portal's /generic/v0/ endpoints."""

    @pytest.mark.asyncio
    async def test_temperature_via_generic_battery_info_endpoint(self) -> None:
        """Battery temperature should be fetched from
        /generic/v0/device/battery/info using the internal device ID
        discovered from /generic/v0/device/list.

        This matches the actual FoxESS web portal JavaScript behaviour
        observed in bus-device-inverterDetail.*.js.
        """
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        device_list_resp = {
            "errno": 0,
            "result": {
                "total": 1,
                "devices": [
                    {
                        "deviceSN": "INV-SN-001",
                        "id": "internal-uuid-42",
                        "stationID": "plant-123",
                        "status": 1,
                    },
                ],
            },
        }
        battery_info_resp = {
            "errno": 0,
            "result": {
                "batterys": [
                    {
                        "soc": 75,
                        "power": -3.2,
                        "volt": 414.5,
                        "current": -7.7,
                        "temperature": 18.5,
                        "status": 1,
                    },
                ],
            },
        }

        app = _create_web_portal_app(
            device_list_response=device_list_resp,
            battery_info_response=battery_info_resp,
        )
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
                assert temp == 18.5
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_min_of_multiple_batteries(self) -> None:
        """When multiple batteries are present, return the minimum
        temperature (worst case for charge rate limiting).
        """
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        device_list_resp = {
            "errno": 0,
            "result": {
                "total": 1,
                "devices": [
                    {"deviceSN": "INV-SN-001", "id": "uuid-1", "status": 1},
                ],
            },
        }
        battery_info_resp = {
            "errno": 0,
            "result": {
                "batterys": [
                    {"temperature": 22.0, "soc": 80, "status": 1},
                    {"temperature": 14.5, "soc": 78, "status": 1},
                    {"temperature": 19.3, "soc": 79, "status": 1},
                ],
            },
        }

        app = _create_web_portal_app(
            device_list_response=device_list_resp,
            battery_info_response=battery_info_resp,
        )
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
                assert temp == 14.5
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_none_when_no_batteries(self) -> None:
        """Returns None when the battery info response has no batteries."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        device_list_resp = {
            "errno": 0,
            "result": {
                "total": 1,
                "devices": [
                    {"deviceSN": "INV-SN-001", "id": "uuid-1", "status": 1},
                ],
            },
        }
        battery_info_resp = {
            "errno": 0,
            "result": {"batterys": []},
        }

        app = _create_web_portal_app(
            device_list_response=device_list_resp,
            battery_info_response=battery_info_resp,
        )
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
                assert temp is None
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_none_when_device_not_found(self) -> None:
        """Returns None when the device SN is not found in the device list."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        device_list_resp = {
            "errno": 0,
            "result": {
                "total": 1,
                "devices": [
                    {"deviceSN": "OTHER-SN", "id": "uuid-1", "status": 1},
                ],
            },
        }

        app = _create_web_portal_app(
            device_list_response=device_list_resp,
        )
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
                assert temp is None
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_temperature_none_when_batteries_have_no_temp(self) -> None:
        """Returns None when battery objects don't have temperature field."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        device_list_resp = {
            "errno": 0,
            "result": {
                "total": 1,
                "devices": [
                    {"deviceSN": "INV-SN-001", "id": "uuid-1", "status": 1},
                ],
            },
        }
        battery_info_resp = {
            "errno": 0,
            "result": {
                "batterys": [
                    {"soc": 80, "status": 1, "power": -3.0},
                ],
            },
        }

        app = _create_web_portal_app(
            device_list_response=device_list_resp,
            battery_info_response=battery_info_resp,
        )
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
                assert temp is None
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_internal_device_id_cached(self) -> None:
        """The internal device ID should be cached after first discovery."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        call_count = {"device_list": 0, "battery_info": 0}

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 0, "result": {"token": "test-token"}}
            )

        async def handle_device_list(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            call_count["device_list"] += 1
            return aiohttp.web.json_response(
                {
                    "errno": 0,
                    "result": {
                        "total": 1,
                        "devices": [
                            {"deviceSN": "INV-SN", "id": "uuid-x", "status": 1},
                        ],
                    },
                }
            )

        async def handle_battery_info(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            call_count["battery_info"] += 1
            return aiohttp.web.json_response(
                {
                    "errno": 0,
                    "result": {
                        "batterys": [{"temperature": 20.0, "soc": 75, "status": 1}],
                    },
                }
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        app.router.add_post("/generic/v0/device/list", handle_device_list)
        app.router.add_post("/generic/v0/device/battery/info", handle_battery_info)

        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                t1 = await ws.async_get_battery_temperature(device_sn="INV-SN")
                assert t1 == 20.0
                assert call_count["device_list"] == 1

                t2 = await ws.async_get_battery_temperature(device_sn="INV-SN")
                assert t2 == 20.0
                assert call_count["device_list"] == 1  # NOT called again
                assert call_count["battery_info"] == 2  # called twice
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_graceful_when_device_list_errors(self) -> None:
        """Returns None gracefully when device list endpoint fails."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 0, "result": {"token": "test-token"}}
            )

        async def handle_device_list(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 40004, "result": None, "msg": "Loading data"}
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        app.router.add_post("/generic/v0/device/list", handle_device_list)

        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN")
                assert temp is None
        finally:
            await runner.cleanup()
