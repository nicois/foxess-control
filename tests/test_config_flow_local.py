"""Config flow: the local gateway branch, validated against the simulator.

The flow reads ``GET /state`` from the gateway the user names; these tests
point it at the simulator's gateway over real HTTP (C-028).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.foxess_control.config_flow import FoxessControlConfigFlow
from custom_components.foxess_control.const import (
    BACKEND_LOCAL,
    CONF_BACKEND,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_DEVICE_SERIAL,
    CONF_LOCAL_TOKEN,
    CONF_LOCAL_URL,
)

if TYPE_CHECKING:
    from tests.conftest import SimulatorHandle


def _flow() -> FoxessControlConfigFlow:
    flow = FoxessControlConfigFlow()
    flow.hass = MagicMock()
    flow.async_set_unique_id = AsyncMock()  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = MagicMock()  # type: ignore[method-assign]
    flow.async_create_entry = MagicMock(  # type: ignore[method-assign]
        return_value={"type": "create_entry"}
    )
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]
        return_value={"type": "form"}
    )
    return flow


async def _submit(
    flow: FoxessControlConfigFlow, user_input: dict[str, Any]
) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        with patch(
            "custom_components.foxess_control.config_flow.async_get_clientsession",
            return_value=session,
        ):
            result: dict[str, Any] = await flow.async_step_local(user_input)  # type: ignore[assignment]
    return result


def _form_errors(flow: FoxessControlConfigFlow) -> dict[str, str]:
    errors: dict[str, str] = flow.async_show_form.call_args.kwargs["errors"]  # type: ignore[attr-defined]
    return errors


class TestLocalStep:
    @pytest.mark.asyncio
    async def test_form_is_shown_first(self) -> None:
        flow = _flow()
        await flow.async_step_local(None)
        kwargs = flow.async_show_form.call_args.kwargs  # type: ignore[attr-defined]
        assert kwargs["step_id"] == "local"
        assert kwargs["errors"] == {}

    @pytest.mark.asyncio
    async def test_valid_gateway_creates_the_entry(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(local_gateway_token="fl_tok", battery_capacity_kwh=41.94)
        flow = _flow()
        result = await _submit(
            flow, {CONF_LOCAL_URL: foxess_sim.url, CONF_LOCAL_TOKEN: "fl_tok"}
        )
        assert result["type"] == "create_entry"
        flow.async_set_unique_id.assert_awaited_once_with("SIM0001")  # type: ignore[attr-defined]
        kwargs = flow.async_create_entry.call_args.kwargs  # type: ignore[attr-defined]
        assert kwargs["data"] == {
            CONF_BACKEND: BACKEND_LOCAL,
            CONF_LOCAL_URL: foxess_sim.url,
            CONF_LOCAL_TOKEN: "fl_tok",
            CONF_DEVICE_SERIAL: "SIM0001",
        }
        # The gateway knows the usable capacity: discharge pacing needs it.
        assert kwargs["options"] == {CONF_BATTERY_CAPACITY_KWH: pytest.approx(41.94)}
        assert "SIM0001" in kwargs["title"]

    @pytest.mark.asyncio
    async def test_pasted_bearer_prefix_and_trailing_slash_are_tidied(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """The gateway's HA docs keep the whole header value, "Bearer "
        included, in secrets.yaml — a user may paste either form."""
        foxess_sim.set(local_gateway_token="fl_tok")
        flow = _flow()
        result = await _submit(
            flow,
            {
                CONF_LOCAL_URL: f" {foxess_sim.url}/ ",
                CONF_LOCAL_TOKEN: "Bearer fl_tok",
            },
        )
        assert result["type"] == "create_entry"
        data = flow.async_create_entry.call_args.kwargs["data"]  # type: ignore[attr-defined]
        assert data[CONF_LOCAL_URL] == foxess_sim.url
        assert data[CONF_LOCAL_TOKEN] == "fl_tok"

    @pytest.mark.asyncio
    async def test_public_gateway_needs_no_token(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        flow = _flow()
        result = await _submit(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert result["type"] == "create_entry"
        data = flow.async_create_entry.call_args.kwargs["data"]  # type: ignore[attr-defined]
        assert data[CONF_LOCAL_TOKEN] == ""

    @pytest.mark.asyncio
    async def test_unknown_capacity_leaves_options_empty(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(battery_capacity_kwh=0)
        flow = _flow()
        await _submit(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert flow.async_create_entry.call_args.kwargs["options"] == {}  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_wrong_token(self, foxess_sim: SimulatorHandle) -> None:
        foxess_sim.set(local_gateway_token="fl_right")
        flow = _flow()
        result = await _submit(
            flow, {CONF_LOCAL_URL: foxess_sim.url, CONF_LOCAL_TOKEN: "fl_wrong"}
        )
        assert result["type"] == "form"
        assert _form_errors(flow) == {"base": "local_invalid_auth"}

    @pytest.mark.asyncio
    async def test_unreachable_gateway(self) -> None:
        flow = _flow()
        result = await _submit(flow, {CONF_LOCAL_URL: "http://127.0.0.1:9"})
        assert result["type"] == "form"
        assert _form_errors(flow) == {"base": "local_cannot_connect"}

    @pytest.mark.asyncio
    async def test_inverter_not_yet_identified(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """No serial yet (gateway just started, inverter not connected):
        refuse rather than create an entry with no stable identity."""
        foxess_sim.set(device_sn="")
        flow = _flow()
        result = await _submit(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert result["type"] == "form"
        assert _form_errors(flow) == {"base": "inverter_not_identified"}
