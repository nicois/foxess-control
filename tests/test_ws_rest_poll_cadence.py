"""Regression test: REST poll cadence preserved during WS injections.

When WS mode is active, WebSocket messages arrive every ~5s and call
``inject_realtime_data()`` which delegates to
``DataUpdateCoordinator.async_set_updated_data()``.  That HA helper
cancels the pending refresh timer and reschedules it from "now", so
if WS messages arrive more frequently than ``update_interval``, the
REST poll is starved indefinitely.  The ~25 REST-only fields
(cumulative energy counters, PV string power, voltages/currents,
inverter/ambient temperatures, grid frequency, meter power, EPS)
therefore go stale while WS mode is active.

These tests exercise the real coordinator against a real HA event
loop (no mocking of the DataUpdateCoordinator internals) and
validate that the REST poll continues to fire on its configured
cadence even while WS injections are happening far more often.

The test fails against the current code because
``inject_realtime_data`` calls ``async_set_updated_data``, which
resets the refresh timer on every invocation.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.foxess_control.coordinator import FoxESSDataCoordinator


class _CountingInverter:
    """Minimal Inverter stub that counts REST calls.

    Returns a stable payload shape sufficient for
    ``_async_update_data`` (which reads SoC and battery power for
    interpolation, plus calls ``get_current_mode``).
    """

    def __init__(self) -> None:
        self.get_real_time_calls = 0

    def get_real_time(self, _vars: list[str]) -> dict[str, Any]:
        self.get_real_time_calls += 1
        return {
            "SoC": 50.0,
            "batChargePower": 0.0,
            "batDischargePower": 0.0,
            "feedin": 0.0,
        }

    def get_current_mode(self) -> None:  # noqa: D401 - trivial stub
        return None


def _make_hass() -> HomeAssistant:
    """Create a real HomeAssistant bound to the running loop.

    Doesn't start core services — just the loop, data dict and bus
    are enough for DataUpdateCoordinator's ``loop.call_at``
    scheduling.  CoreState defaults to ``not_running`` so
    ``hass.is_stopping`` is False.
    """
    return HomeAssistant("/tmp")


def _make_coord(
    hass: HomeAssistant, inv: _CountingInverter, interval_s: int
) -> FoxESSDataCoordinator:
    # Suppress the "must pass config_entry" frame warning that HA emits
    # when config_entry is not supplied (matches the style used in
    # tests/test_coordinator.py::_make_coordinator).
    with patch("homeassistant.helpers.frame.report_usage"):
        coord = FoxESSDataCoordinator(hass, inv, interval_s)  # type: ignore[arg-type]
    return coord


async def _bootstrap(coord: FoxESSDataCoordinator) -> None:
    """Do the initial REST poll and arm the periodic timer.

    ``_schedule_refresh`` only arms the timer when there is at least
    one listener, matching production behaviour where sensor entities
    subscribe.  We add a no-op listener to emulate that.
    """
    coord.async_add_listener(lambda: None)
    await coord.async_refresh()


@pytest.mark.asyncio
async def test_rest_poll_fires_on_schedule_without_ws() -> None:
    """Sanity control: without WS injections the REST poll ticks on schedule.

    This confirms the test harness (real HA loop + short interval +
    listener) drives the DataUpdateCoordinator timer as expected.
    If this fails, the starvation test below is uninterpretable.
    """
    hass = _make_hass()
    inv = _CountingInverter()
    coord = _make_coord(hass, inv, interval_s=1)
    await _bootstrap(coord)
    initial_calls = inv.get_real_time_calls
    assert initial_calls == 1, "first_refresh should have polled exactly once"

    # Give the periodic timer time to fire ~3 more times.
    await asyncio.sleep(3.2)

    further_calls = inv.get_real_time_calls - initial_calls
    assert further_calls >= 2, (
        f"Expected >= 2 further REST polls in 3.2s with interval=1s, "
        f"got {further_calls}"
    )


@pytest.mark.asyncio
async def test_rest_poll_not_starved_by_frequent_ws_injections() -> None:
    """WS injections faster than REST interval must not starve the REST poll.

    Setup: interval=1s, WS injection every 0.3s for ~3s (~10 WS
    messages).  Expectation: at least 2 further REST polls beyond the
    initial bootstrap.

    Current behaviour: ``inject_realtime_data`` calls
    ``async_set_updated_data`` which cancels & reschedules the timer
    on each call.  Because WS arrives every 0.3s (< 1s interval), the
    REST timer is perpetually reset and never fires — we see exactly
    1 call (the initial bootstrap).
    """
    hass = _make_hass()
    inv = _CountingInverter()
    coord = _make_coord(hass, inv, interval_s=1)
    await _bootstrap(coord)
    initial_calls = inv.get_real_time_calls
    assert initial_calls == 1

    # Run WS injections concurrently with the coordinator's internal
    # timer.  Each injection uses a different SoC so the change-detect
    # gate in ``inject_realtime_data`` does not short-circuit it
    # (which would bypass async_set_updated_data and defeat the point).
    async def ws_driver() -> None:
        # ~10 injections over ~3s at 0.3s spacing, faster than the 1s
        # REST interval.  Emulates the production WS cadence of ~5s
        # against a 300s REST interval, scaled down for test speed.
        for i in range(10):
            coord.inject_realtime_data({"SoC": 50.0 + (i + 1) * 0.01})
            await asyncio.sleep(0.3)

    await ws_driver()

    further_calls = inv.get_real_time_calls - initial_calls
    assert further_calls >= 2, (
        f"REST poll starved by WS injections: got {further_calls} further "
        f"REST polls in ~3s at interval=1s (expected >= 2). "
        f"async_set_updated_data resets the refresh timer on every WS "
        f"injection, so WS arriving faster than update_interval starves "
        f"the REST poll indefinitely."
    )


@pytest.mark.asyncio
async def test_rest_poll_not_starved_by_slow_ws_injections() -> None:
    """WS injections slower than REST interval must not affect the REST poll.

    Positive-intent control: when WS cadence (1.5s) exceeds the REST
    interval (1s), the REST poll should always fire in the gap
    between WS pushes.  This confirms the fix (when written) does
    not regress the slow-WS case.
    """
    hass = _make_hass()
    inv = _CountingInverter()
    coord = _make_coord(hass, inv, interval_s=1)
    await _bootstrap(coord)
    initial_calls = inv.get_real_time_calls

    async def ws_driver() -> None:
        # WS every 1.5s for 3s → 2 injections, interleaved with the
        # 1s REST poll.
        for i in range(2):
            coord.inject_realtime_data({"SoC": 50.0 + (i + 1) * 0.01})
            await asyncio.sleep(1.5)

    await ws_driver()

    further_calls = inv.get_real_time_calls - initial_calls
    assert further_calls >= 1, (
        f"Slow WS should not starve REST poll: got {further_calls} "
        f"further REST polls (expected >= 1)"
    )
