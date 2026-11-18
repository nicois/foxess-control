"""Local gateway backend: the data coordinator (REST seed + SSE stream).

``GET /state`` seeds and periodically refreshes the full dataset; the
``/events`` stream overlays live power/SoC every ~5 s. Run against the
simulator's gateway (C-028); waits are on coordinator listener callbacks,
never fixed sleeps.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
import pytest_asyncio
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.foxess_control.coordinator import FoxESSLocalCoordinator
from custom_components.foxess_control.foxess.local_client import FoxESSLocalClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from tests.conftest import SimulatorHandle


@pytest.fixture
def sim(foxess_sim: SimulatorHandle) -> SimulatorHandle:
    foxess_sim.set(fuzzing=False, local_sse_interval_s=0.05)
    return foxess_sim


@pytest_asyncio.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


def _coordinator(client: FoxESSLocalClient, **kwargs: Any) -> FoxESSLocalCoordinator:
    hass = MagicMock()
    hass.data = {}
    with patch("homeassistant.helpers.frame.report_usage"):
        return FoxESSLocalCoordinator(hass, client, 60, **kwargs)


async def _wait_for(
    coord: FoxESSLocalCoordinator,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float = 5.0,
) -> None:
    """Wait until a listener notification shows *predicate* true."""
    done = asyncio.Event()

    def _check() -> None:
        if coord.data is not None and predicate(coord.data):
            done.set()

    remove = coord.async_add_listener(_check)
    try:
        _check()
        await asyncio.wait_for(done.wait(), timeout)
    finally:
        remove()


async def _seed(coord: FoxESSLocalCoordinator) -> None:
    coord.data = await coord._async_update_data()


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _wait_connected(coord: FoxESSLocalCoordinator) -> None:
    connected = asyncio.Event()
    remove = coord.async_add_listener(
        lambda: connected.set() if coord.stream_connected else None
    )
    try:
        await asyncio.wait_for(connected.wait(), 5)
    finally:
        remove()


class TestRefresh:
    @pytest.mark.asyncio
    async def test_refresh_maps_gateway_state(
        self, sim: SimulatorHandle, session: aiohttp.ClientSession
    ) -> None:
        sim.set(soc=55.0, solar_kw=2.0)
        coord = _coordinator(FoxESSLocalClient(session, sim.url))
        data = await coord._async_update_data()
        assert data["SoC"] == 55
        assert data["pvPower"] == pytest.approx(2.0)
        assert data["batTemperature"] == pytest.approx(25.0)
        assert data["_data_source"] == "local"

    @pytest.mark.asyncio
    async def test_gateway_error_fails_the_update_with_its_code(
        self, sim: SimulatorHandle, session: aiohttp.ClientSession
    ) -> None:
        """A refused or unreachable gateway makes entities unavailable
        rather than showing the last values as current (C-020)."""
        sim.set(local_gateway_token="fl_right")
        coord = _coordinator(FoxESSLocalClient(session, sim.url, "fl_wrong"))
        with pytest.raises(UpdateFailed, match="unauthorized"):
            await coord._async_update_data()


class TestStream:
    @pytest.mark.asyncio
    async def test_snapshot_overlays_live_values_and_keeps_the_rest(
        self, sim: SimulatorHandle, session: aiohttp.ClientSession
    ) -> None:
        sim.set(soc=55.0)
        coord = _coordinator(FoxESSLocalClient(session, sim.url))
        await _seed(coord)
        sim.set(soc=57.0)
        task = asyncio.create_task(coord.async_run_stream())
        try:
            await _wait_for(coord, lambda d: d.get("SoC") == 57)
        finally:
            await _stop(task)
        # Values only /state carries survive the overlay.
        assert coord.data["batTemperature"] == pytest.approx(25.0)
        assert "_work_mode" in coord.data
        # A stopped stream is not reported as connected (diagnostics).
        assert coord.stream_connected is False

    @pytest.mark.asyncio
    async def test_stream_marks_connected_while_running(
        self, sim: SimulatorHandle, session: aiohttp.ClientSession
    ) -> None:
        coord = _coordinator(FoxESSLocalClient(session, sim.url))
        await _seed(coord)
        task = asyncio.create_task(coord.async_run_stream())
        try:
            await _wait_connected(coord)
            assert coord.last_stream_error is None
        finally:
            await _stop(task)

    @pytest.mark.asyncio
    async def test_offline_connection_event_is_surfaced(
        self, sim: SimulatorHandle, session: aiohttp.ClientSession
    ) -> None:
        coord = _coordinator(FoxESSLocalClient(session, sim.url))
        await _seed(coord)
        task = asyncio.create_task(coord.async_run_stream())
        try:
            # Flip only once the stream is up, so it sees the transition.
            await _wait_connected(coord)
            sim.set(local_gateway_online=False)
            await _wait_for(coord, lambda d: d.get("_gateway_online") is False)
        finally:
            await _stop(task)

    @pytest.mark.asyncio
    async def test_silent_stream_is_treated_as_offline(
        self, sim: SimulatorHandle, session: aiohttp.ClientSession
    ) -> None:
        """The 5 s snapshot is the heartbeat: silence beyond the idle
        timeout means the gateway or inverter is gone, even though the TCP
        connection is still open."""
        sim.set(local_sse_interval_s=30)
        coord = _coordinator(
            FoxESSLocalClient(session, sim.url),
            stream_idle_timeout_s=0.2,
            reconnect_delay_s=0.05,
        )
        await _seed(coord)
        task = asyncio.create_task(coord.async_run_stream())
        try:
            await _wait_for(coord, lambda d: d.get("_gateway_online") is False)
        finally:
            await _stop(task)

    @pytest.mark.asyncio
    async def test_refused_stream_records_the_error_and_keeps_polling(
        self, sim: SimulatorHandle, session: aiohttp.ClientSession
    ) -> None:
        coord = _coordinator(
            FoxESSLocalClient(session, sim.url, "fl_wrong"),
            reconnect_delay_s=0.05,
        )
        sim.set(local_gateway_token="fl_right")
        failed = asyncio.Event()
        coord.async_add_listener(
            lambda: failed.set() if coord.last_stream_error else None
        )
        task = asyncio.create_task(coord.async_run_stream())
        try:
            await asyncio.wait_for(failed.wait(), 5)
        finally:
            await _stop(task)
        assert coord.last_stream_error is not None
        assert "unauthorized" in coord.last_stream_error
        assert coord.stream_connected is False
