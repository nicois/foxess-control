"""Tests for FoxESSWebSession — BMS battery temperature fetching.

Verifies that the web session correctly fetches BMS battery temperature
via ``POST /dew/v0/device/detail`` with the device serial number.

The ``/dew/v0/`` namespace accepts the web session token from
``/basic/v0/user/login``.  Previous attempts using ``/generic/v0/``
endpoints failed because that namespace rejects the web session token
with errno=41808 ("Token has expired. Please log in again").
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
    device_detail_response: dict[str, Any] | None = None,
) -> aiohttp.web.Application:
    """Build a minimal aiohttp app simulating the FoxESS web portal.

    Registers:
    - /basic/v0/user/login (auth — returns a session token)
    - /dew/v0/device/detail (battery detail with temperature)
    """
    app = aiohttp.web.Application()

    async def handle_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response(
            {"errno": 0, "result": {"token": "test-token-123"}}
        )

    async def handle_device_detail_post(
        request: aiohttp.web.Request,
    ) -> aiohttp.web.Response:
        if device_detail_response is not None:
            return aiohttp.web.json_response(device_detail_response)
        return aiohttp.web.json_response(
            {"errno": 41205, "result": None, "msg": "device not found"}
        )

    app.router.add_post("/basic/v0/user/login", handle_login)
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
    """BMS temperature fetch via POST /dew/v0/device/detail."""

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
                    "temperature": {"value": 18.5, "unit": "\u00b0C"},
                    "soc": {"value": 75, "unit": "%"},
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
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
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
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
                assert temp == 22.3
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
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
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
                    "soc": {"value": 75},
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
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
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
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
                assert temp is None
        finally:
            await runner.cleanup()


class TestGenericV0TokenRejection:
    """Reproduce production bug: /generic/v0/ rejects the web session token.

    Production evidence (2026-04-19):
      1. FoxESS web login successful
      2. Internal device ID discovery failed: Web API POST
         /generic/v0/device/list failed: errno=41808,
         msg=Token has expired. Please log in again
      3. BMS temperature: no value returned

    The /generic/v0/ API namespace on the FoxESS cloud server rejects the
    token obtained from /basic/v0/user/login with errno=41808 (Token has
    expired), even though the token was issued seconds earlier.  The /dew/v0/
    endpoints DO accept the same token.

    The test simulates this by having the /generic/v0/ handler reject the
    token (matching production behaviour) while the /dew/v0/ handler accepts
    it and returns battery data.  The session should still return a valid
    temperature by using /dew/v0/ instead of /generic/v0/.
    """

    @pytest.mark.asyncio
    async def test_temperature_returned_despite_generic_v0_rejection(self) -> None:
        """After login, the token must reach an endpoint that accepts it.

        The /generic/v0/ endpoints reject the web session token with
        errno=41808 (observed in production).  The code should use an
        alternative endpoint path (/dew/v0/) that accepts the token.

        If this test FAILS with temp == None, it means the code is still
        trying /generic/v0/ which rejects the token.
        """
        from custom_components.foxess_control.foxess.web_session import (
            FoxESSWebSession,
        )

        token_issued = "valid-session-token-xyz"

        app = aiohttp.web.Application()

        async def handle_login(req: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {"errno": 0, "result": {"token": token_issued}}
            )

        # /generic/v0/ rejects ANY token with errno=41808
        # (matches real FoxESS cloud behaviour observed 2026-04-19)
        async def handle_generic_device_list(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {
                    "errno": 41808,
                    "result": None,
                    "msg": "Token has expired. Please log in again",
                }
            )

        async def handle_generic_battery_info(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {
                    "errno": 41808,
                    "result": None,
                    "msg": "Token has expired. Please log in again",
                }
            )

        # /dew/v0/ accepts the token and returns battery data
        async def handle_dew_device_detail(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            token_hdr = req.headers.get("token", "")
            if not token_hdr or token_hdr != token_issued:
                return aiohttp.web.json_response(
                    {"errno": 41808, "result": None, "msg": "Token has expired"}
                )
            return aiohttp.web.json_response(
                {
                    "errno": 0,
                    "result": {
                        "battery": {
                            "temperature": {"value": 18.5, "unit": "°C"},
                            "soc": {"value": 75, "unit": "%"},
                        },
                    },
                }
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        app.router.add_post("/generic/v0/device/list", handle_generic_device_list)
        app.router.add_post(
            "/generic/v0/device/battery/info", handle_generic_battery_info
        )
        app.router.add_post("/dew/v0/device/detail", handle_dew_device_detail)

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
                assert temp is not None, (
                    "BMS temperature is None — the code is still trying /generic/v0/ "
                    "which rejects the token with errno=41808"
                )
                assert temp == 18.5
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_token_passed_through_to_data_endpoint(self) -> None:
        """The token from login must be sent in the request headers
        to the data endpoint.  This verifies the token plumbing works
        end-to-end: login -> store token -> send token with data request.
        """
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

        # Capture the token from every /dew/v0/ request
        async def handle_dew_device_detail(
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
                            "temperature": {"value": 22.0, "unit": "°C"},
                        },
                    },
                }
            )

        # /generic/v0/ rejects token (production behaviour)
        async def handle_generic_reject(
            req: aiohttp.web.Request,
        ) -> aiohttp.web.Response:
            return aiohttp.web.json_response(
                {
                    "errno": 41808,
                    "result": None,
                    "msg": "Token has expired. Please log in again",
                }
            )

        app.router.add_post("/basic/v0/user/login", handle_login)
        app.router.add_post("/generic/v0/device/list", handle_generic_reject)
        app.router.add_post("/generic/v0/device/battery/info", handle_generic_reject)
        app.router.add_post("/dew/v0/device/detail", handle_dew_device_detail)

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
                assert temp == 22.0, (
                    f"Expected 22.0 but got {temp}. Token plumbing may be broken."
                )
                # Verify the token was actually sent
                assert len(received_tokens) > 0, "No requests reached /dew/v0/"
                assert received_tokens[-1] == token_issued, (
                    f"Wrong token sent: expected {token_issued!r}, "
                    f"got {received_tokens[-1]!r}"
                )
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_login_failure_returns_none(self) -> None:
        """When login itself fails, temperature returns None gracefully.

        This is a baseline sanity check — not the bug under investigation.
        The async_get_battery_temperature method should handle auth
        failures internally and return None (not raise).
        """
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
                temp = await ws.async_get_battery_temperature(device_sn="INV-SN-001")
                assert temp is None
        finally:
            await runner.cleanup()
