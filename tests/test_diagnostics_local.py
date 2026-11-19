"""Diagnostics for the local gateway backend.

A diagnostics download is what a user attaches to an issue, so it must
(a) never carry the gateway token or the inverter serial, and (b) say
enough about the gateway — stream health, liveness, identity, write
budget, time zone — to triage a report without log access (C-020).
Real HTTP against the simulator's gateway (C-028).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.foxess_control.const import (
    BACKEND_LOCAL,
    CONF_BACKEND,
    CONF_DEVICE_SERIAL,
    CONF_LOCAL_TOKEN,
    CONF_LOCAL_URL,
    DOMAIN,
)
from custom_components.foxess_control.coordinator import FoxESSLocalCoordinator
from custom_components.foxess_control.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
)
from custom_components.foxess_control.foxess.local_client import FoxESSLocalClient

if TYPE_CHECKING:
    from tests.conftest import SimulatorHandle


async def _diagnostics(
    sim: SimulatorHandle,
    *,
    token: str = "fl_secret_token",
    gateway_token: str | None = None,
    ha_tz: str = "UTC",
) -> dict[str, Any]:
    """Diagnostics for a local entry using *token*; the simulated gateway
    accepts *gateway_token* (default: the same token)."""
    sim.set(
        fuzzing=False,
        local_gateway_token=gateway_token or token,
        device_sn="KH10SERIAL0001",
    )
    async with aiohttp.ClientSession() as session:
        with patch("homeassistant.helpers.frame.report_usage"):
            coord = FoxESSLocalCoordinator(
                MagicMock(), FoxESSLocalClient(session, sim.url, token), 60
            )
        data: dict[str, Any] | None
        try:
            data = await coord._async_update_data()
        except UpdateFailed:
            data = None
        coord.data = data  # type: ignore[assignment]  # None until a first refresh succeeds
        coord.last_stream_error = "unreachable: example"
        dd = FoxESSControlData()
        dd.entries["e1"] = FoxESSEntryData(coordinator=coord, inverter=None)
        hass = MagicMock()
        hass.data = {DOMAIN: dd}
        hass.config.time_zone = ha_tz
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {
            CONF_BACKEND: BACKEND_LOCAL,
            CONF_LOCAL_URL: sim.url,
            CONF_LOCAL_TOKEN: token,
            CONF_DEVICE_SERIAL: "KH10SERIAL0001",
        }
        entry.options = {}
        result: dict[str, Any] = await async_get_config_entry_diagnostics(hass, entry)
    return result


class TestRedaction:
    @pytest.mark.asyncio
    async def test_gateway_token_and_serial_never_appear(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        result = await _diagnostics(foxess_sim)
        dumped = repr(result)
        assert "fl_secret_token" not in dumped
        assert "KH10SERIAL0001" not in dumped
        assert result["entry"]["data"][CONF_LOCAL_TOKEN] == "**REDACTED**"


class TestLocalGatewaySection:
    @pytest.mark.asyncio
    async def test_stream_and_liveness(self, foxess_sim: SimulatorHandle) -> None:
        section = (await _diagnostics(foxess_sim))["local_gateway"]
        assert section["url"] == f"{foxess_sim.url}/api/v1"
        assert section["stream_connected"] is False
        assert section["last_stream_error"] == "unreachable: example"
        assert section["inverter_online"] is True

    @pytest.mark.asyncio
    async def test_live_gateway_facts(self, foxess_sim: SimulatorHandle) -> None:
        section = (await _diagnostics(foxess_sim))["local_gateway"]
        assert section["gateway_version"] == "v0.1.1-sim"
        assert section["identity"]["model"] == "KH10"
        assert section["identity"]["verified_profile"] == "kh10-fw1.73"
        assert section["forward_mode"] == "telemetry_only"
        assert section["write_budget"]["schedule"]["remaining"] == 10
        assert section["energy_sources"]["grid_export_kwh"] == "register"

    @pytest.mark.asyncio
    async def test_time_zone_mismatch_is_called_out(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Schedule windows run in the gateway host's zone; HA computes
        them in its own. A mismatch shifts every window."""
        section = (await _diagnostics(foxess_sim, ha_tz="Australia/Melbourne"))[
            "local_gateway"
        ]
        assert section["time_zone"] == {
            "gateway": "UTC",
            "home_assistant": "Australia/Melbourne",
            "match": False,
        }

    @pytest.mark.asyncio
    async def test_unreachable_gateway_does_not_break_the_download(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Diagnostics are most needed exactly when the gateway is down."""
        result = await _diagnostics(foxess_sim)
        assert "error" not in result["local_gateway"]

        result = await _diagnostics(
            foxess_sim, token="fl_wrong", gateway_token="fl_right"
        )
        assert "unauthorized" in result["local_gateway"]["error"]
        assert result["local_gateway"]["stream_connected"] is False


class TestEnvironmentDataSource:
    @pytest.mark.asyncio
    async def test_reports_the_coordinators_data_source(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Regression: read the non-existent ``data_source`` key, so every
        download reported null; the coordinator writes ``_data_source``."""
        result = await _diagnostics(foxess_sim)
        assert result["environment"]["data_source"] == "local"
