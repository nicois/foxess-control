"""Tests for FoxESSWebSession — BMS battery temperature fetching.

Verifies that the web portal data endpoints are called using HTTP POST
with JSON bodies (matching the FoxESS web portal API contract), not
HTTP GET with query parameters.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import aiohttp.web
import pytest

# ---------------------------------------------------------------------------
# Helpers — tiny aiohttp test server that only accepts POST
# ---------------------------------------------------------------------------


def _create_web_portal_app(
    plant_devices: list[dict[str, Any]],
    battery_detail: dict[str, Any],
) -> aiohttp.web.Application:
    """Build a minimal aiohttp app simulating the FoxESS web portal.

    The FoxESS web portal uses POST for data endpoints.  GET requests
    receive a 405 Method Not Allowed, just like the real server.
    """
    app = aiohttp.web.Application()

    async def handle_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response(
            {"errno": 0, "result": {"token": "test-token-123"}}
        )

    async def handle_device_list_post(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"errno": 0, "result": plant_devices})

    async def handle_device_detail_post(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"errno": 0, "result": battery_detail})

    # Only POST is accepted for data endpoints (matches real FoxESS behaviour)
    app.router.add_post("/basic/v0/user/login", handle_login)
    app.router.add_post("/dew/v0/plant/device/list", handle_device_list_post)
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
    """BMS temperature fetch must use POST to match the web portal API."""

    @pytest.mark.asyncio
    async def test_battery_temperature_returned_when_api_responds(self) -> None:
        """Given a working web portal that returns battery temperature,
        async_get_battery_temperature should return the numeric value.

        This is the happy-path: the sensor should not be 'unknown'.
        """
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        plant_devices = [
            {"sn": "BAT-SN-001", "id": "bat-device-id-42"},
        ]
        battery_detail = {
            "battery": {
                "temperature": {"value": 18.5, "unit": "°C"},
            },
        }

        app = _create_web_portal_app(plant_devices, battery_detail)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",  # MD5 of ""
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature("plant-123", "BAT-SN-001")
                assert temp == 18.5
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_battery_temperature_with_nested_child_device(self) -> None:
        """Battery SN found as a child device in the plant device list."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        plant_devices = [
            {
                "sn": "INV-SN-001",
                "id": "inv-id",
                "children": [
                    {"sn": "BAT-SN-002", "id": "bat-child-id-99"},
                ],
            },
        ]
        battery_detail = {
            "battery": {
                "temperature": {"value": 22.3, "unit": "°C"},
            },
        }

        app = _create_web_portal_app(plant_devices, battery_detail)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature("plant-123", "BAT-SN-002")
                assert temp == 22.3
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_battery_temperature_none_when_sn_not_found(self) -> None:
        """Returns None when the battery SN doesn't appear in the device list."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        plant_devices = [
            {"sn": "OTHER-SN", "id": "other-id"},
        ]
        battery_detail = {
            "battery": {
                "temperature": {"value": 20.0, "unit": "°C"},
            },
        }

        app = _create_web_portal_app(plant_devices, battery_detail)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature("plant-123", "MISSING-SN")
                assert temp is None
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_battery_temperature_none_when_value_missing(self) -> None:
        """Returns None when the API response lacks a temperature value."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        plant_devices = [
            {"sn": "BAT-SN-001", "id": "bat-device-id-42"},
        ]
        battery_detail: dict[str, Any] = {
            "battery": {},
        }

        app = _create_web_portal_app(plant_devices, battery_detail)
        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                temp = await ws.async_get_battery_temperature("plant-123", "BAT-SN-001")
                assert temp is None
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_battery_temperature_when_device_list_returns_404(self) -> None:
        """Battery temperature must still be returned even when the device
        list endpoint returns 404 (Not Found).

        Production symptom: /dew/v0/plant/device/list returns 404, causing
        _discover_battery_device_id to fail, leaving the BMS temperature
        sensor permanently 'unknown'.

        The fix should allow temperature fetching to work by using the
        inverter device SN directly with the device detail endpoint,
        bypassing the broken device list discovery.
        """
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 0, "result": {"token": "test-token"}}
            )

        async def handle_device_detail(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            body = await req.json()
            # Only return temperature if the request includes an sn
            if body.get("sn"):
                return aiohttp.web.json_response(
                    {
                        "errno": 0,
                        "result": {
                            "battery": {"temperature": {"value": 17.2, "unit": "°C"}},
                        },
                    }
                )
            return aiohttp.web.json_response(
                {"errno": 41205, "result": None, "msg": "device not found"}
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        # NO route for /dew/v0/plant/device/list — simulates production 404
        app.router.add_post("/dew/v0/device/detail", handle_device_detail)

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
                    "plant-123", "BAT-SN-001", device_sn="INV-SN-001"
                )
                assert temp == 17.2, (
                    f"Expected 17.2 but got {temp!r} — sensor would be 'unknown'"
                )
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_battery_temperature_device_list_404_no_device_sn(self) -> None:
        """When device list returns 404 and no device_sn is provided,
        temperature should gracefully return None (not raise).
        """
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 0, "result": {"token": "test-token"}}
            )

        # NO device list route
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
                # No device_sn, no device list — should return None gracefully
                temp = await ws.async_get_battery_temperature("plant-123", "BAT-SN-001")
                assert temp is None

        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_battery_temperature_direct_sn_bypasses_discovery(self) -> None:
        """When device_sn is provided, the device detail endpoint should be
        called with the SN directly, without needing device list discovery.
        """
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        call_count = {"device_detail": 0}

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 0, "result": {"token": "test-token"}}
            )

        async def handle_device_detail(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            call_count["device_detail"] += 1
            body = await req.json()
            assert body.get("sn") == "INV-SN-001"
            return aiohttp.web.json_response(
                {
                    "errno": 0,
                    "result": {
                        "battery": {"temperature": {"value": 15.0}},
                    },
                }
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        # Deliberately NO device list endpoint
        app.router.add_post("/dew/v0/device/detail", handle_device_detail)

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
                    "plant-123", "BAT-SN-001", device_sn="INV-SN-001"
                )
                assert temp == 15.0
                assert call_count["device_detail"] == 1
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_device_id_cached_after_first_discovery(self) -> None:
        """The battery device ID should be cached, not re-discovered each time."""
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        call_count = {"device_list": 0, "device_detail": 0}

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
                {"errno": 0, "result": [{"sn": "BAT-SN", "id": "bat-id"}]}
            )

        async def handle_device_detail(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            call_count["device_detail"] += 1
            return aiohttp.web.json_response(
                {
                    "errno": 0,
                    "result": {
                        "battery": {"temperature": {"value": 25.0}},
                    },
                }
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        app.router.add_post("/dew/v0/plant/device/list", handle_device_list)
        app.router.add_post("/dew/v0/device/detail", handle_device_detail)

        runner, base_url = await _start_test_server(app)

        try:
            async with aiohttp.ClientSession() as session:
                ws = FoxESSWebSession(
                    "testuser",
                    "d41d8cd98f00b204e9800998ecf8427e",
                    base_url=base_url,
                    session=session,
                )
                # First call — discovery + fetch
                t1 = await ws.async_get_battery_temperature("plant-1", "BAT-SN")
                assert t1 == 25.0
                assert call_count["device_list"] == 1

                # Second call — should skip discovery
                t2 = await ws.async_get_battery_temperature("plant-1", "BAT-SN")
                assert t2 == 25.0
                assert call_count["device_list"] == 1  # NOT called again
                assert call_count["device_detail"] == 2  # called twice
        finally:
            await runner.cleanup()
