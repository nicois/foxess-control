"""Local gateway backend: the HTTP/SSE client against the simulator (C-028).

The simulator serves a foxess-local-shaped ``/api/v1`` from the same
``InverterModel`` physics as the cloud API, so these tests exercise real
HTTP, real problem documents and a real event stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
import pytest

from custom_components.foxess_control.foxess.local_client import (
    FoxESSLocalClient,
    LocalGatewayError,
    map_state,
)

if TYPE_CHECKING:
    from tests.conftest import SimulatorHandle


@pytest.fixture
def sim(foxess_sim: SimulatorHandle) -> SimulatorHandle:
    foxess_sim.set(fuzzing=False)
    return foxess_sim


@pytest.mark.asyncio
async def test_state_reflects_the_model(sim: SimulatorHandle) -> None:
    sim.set(soc=72.0, solar_kw=3.0, load_kw=1.0)
    async with aiohttp.ClientSession() as session:
        state = await FoxESSLocalClient(session, sim.url).get_state()
    assert state["online"] is True
    assert state["battery"]["soc_pct"] == 72
    assert state["power"]["pv_w"] == 3000
    assert state["power"]["load_w"] == 1000
    assert state["identity"]["model"] == "KH10"
    assert len(state["schedule"]["groups"]) == 8
    data = map_state(state)
    assert data["SoC"] == 72
    assert data["pvPower"] == pytest.approx(3.0)
    assert data["_data_source"] == "local"


@pytest.mark.asyncio
async def test_base_url_may_already_carry_the_api_prefix(
    sim: SimulatorHandle,
) -> None:
    async with aiohttp.ClientSession() as session:
        client = FoxESSLocalClient(session, f"{sim.url}/api/v1/")
        state = await client.get_state()
    assert client.base_url == f"{sim.url}/api/v1"
    assert state["online"] is True


@pytest.mark.asyncio
async def test_token_is_required_when_the_gateway_demands_one(
    sim: SimulatorHandle,
) -> None:
    sim.set(local_gateway_token="fl_secret")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(LocalGatewayError) as excinfo:
            await FoxESSLocalClient(session, sim.url).get_state()
        state = await FoxESSLocalClient(session, sim.url, "fl_secret").get_state()
    assert excinfo.value.status == 401
    assert excinfo.value.code == "unauthorized"
    assert state["online"] is True


@pytest.mark.asyncio
async def test_offline_inverter_is_reported(sim: SimulatorHandle) -> None:
    sim.set(local_gateway_online=False)
    async with aiohttp.ClientSession() as session:
        state = await FoxESSLocalClient(session, sim.url).get_state()
    assert state["online"] is False
    assert map_state(state)["_gateway_online"] is False


@pytest.mark.asyncio
async def test_energy_counters_and_write_budget_are_served(
    sim: SimulatorHandle,
) -> None:
    """Both are required State fields since the gateway implemented
    docs/api/foxess-local-gateway-requirements.md."""
    sim.set(feedin_total_kwh=12.5)
    async with aiohttp.ClientSession() as session:
        state = await FoxESSLocalClient(session, sim.url).get_state()
    assert state["energy"]["grid_export_kwh"] == pytest.approx(12.5)
    assert state["energy"]["sources"]["grid_export_kwh"] == "register"
    assert map_state(state)["feedin"] == pytest.approx(12.5)
    budget = state["write_budget"]["schedule"]
    assert budget == {
        "limit": 10,
        "remaining": 10,
        "window_s": 3600,
        "next_free_at": None,
    }


@pytest.mark.asyncio
async def test_counters_are_null_before_their_first_read(
    sim: SimulatorHandle,
) -> None:
    sim.set(local_gateway_energy=False)
    async with aiohttp.ClientSession() as session:
        state = await FoxESSLocalClient(session, sim.url).get_state()
    assert state["energy"]["grid_export_kwh"] is None
    assert "feedin" not in map_state(state)


@pytest.mark.asyncio
async def test_snapshot_events_carry_the_counters(sim: SimulatorHandle) -> None:
    sim.set(feedin_total_kwh=7.0)
    async with aiohttp.ClientSession() as session:
        events = FoxESSLocalClient(session, sim.url).iter_events()
        _, name, payload = await anext(events)
        await events.aclose()
    assert name == "snapshot"
    assert payload["energy"]["grid_export_kwh"] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_event_stream_opens_with_a_snapshot(sim: SimulatorHandle) -> None:
    sim.set(soc=64.0)
    async with aiohttp.ClientSession() as session:
        client = FoxESSLocalClient(session, sim.url)
        events = client.iter_events()
        event_id, name, payload = await anext(events)
        await events.aclose()
    assert name == "snapshot"
    assert isinstance(event_id, int)
    assert payload["soc_pct"] == 64


@pytest.mark.asyncio
async def test_event_stream_refused_without_token(sim: SimulatorHandle) -> None:
    sim.set(local_gateway_token="fl_secret")
    async with aiohttp.ClientSession() as session:
        events = FoxESSLocalClient(session, sim.url).iter_events()
        with pytest.raises(LocalGatewayError) as excinfo:
            await anext(events)
    assert excinfo.value.code == "unauthorized"


@pytest.mark.asyncio
async def test_unreachable_gateway() -> None:
    async with aiohttp.ClientSession() as session:
        # Port 9 (discard) on loopback: nothing listens in the test env.
        client = FoxESSLocalClient(session, "http://127.0.0.1:9", timeout_s=2)
        with pytest.raises(LocalGatewayError) as excinfo:
            await client.get_state()
    assert excinfo.value.code == "unreachable"
    assert excinfo.value.status is None
