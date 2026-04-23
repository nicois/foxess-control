"""Tests for D-043: charge re-deferral when ahead of schedule.

When smart charge is actively charging but solar has pushed SoC ahead
of the pacing trajectory, the listener should switch back to self-use
(clear charging_started) and re-evaluate deferral each tick.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.foxess_control import _register_services
from custom_components.foxess_control.const import (
    CONF_API_MIN_SOC,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    CONF_SMART_HEADROOM,
    DEFAULT_API_MIN_SOC,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_SMART_HEADROOM,
    DOMAIN,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
    build_config,
)
from custom_components.foxess_control.foxess.inverter import Inverter

from .conftest import _get_handler

_LISTENERS = "custom_components.foxess_control.smart_battery.listeners"


def _make_hass(
    inverter: Inverter | None = None,
    battery_capacity_kwh: float = 10.0,
    coordinator_data: dict[str, Any] | None = None,
) -> MagicMock:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, **kwargs: asyncio.ensure_future(coro)
    )

    if inverter is None:
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

    mock_store = MagicMock()
    mock_store.async_load = AsyncMock(return_value={})
    mock_store.async_save = AsyncMock()

    mock_coordinator = MagicMock()
    mock_coordinator.data = coordinator_data
    mock_coordinator.update_interval = datetime.timedelta(seconds=300)
    mock_coordinator.async_request_refresh = AsyncMock()

    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData(
        coordinator=mock_coordinator, inverter=inverter
    )
    dd.smart_discharge_unsubs = []
    dd.smart_charge_unsubs = []
    dd.store = mock_store
    hass.data = {DOMAIN: dd}

    mock_entry = MagicMock()
    mock_entry.options = {
        CONF_MIN_SOC_ON_GRID: DEFAULT_MIN_SOC_ON_GRID,
        CONF_BATTERY_CAPACITY_KWH: battery_capacity_kwh,
        CONF_MIN_POWER_CHANGE: DEFAULT_MIN_POWER_CHANGE,
        CONF_API_MIN_SOC: DEFAULT_API_MIN_SOC,
        CONF_SMART_HEADROOM: DEFAULT_SMART_HEADROOM,
    }
    hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)
    dd.config = build_config(
        dict(mock_entry.options), inverter_max_power_w=inverter.max_power_w
    )

    return hass


def _make_call(data: dict[str, Any] | None = None) -> MagicMock:
    call_mock = MagicMock()
    call_mock.data = data or {}
    return call_mock


async def _start_charge_session(
    hass: MagicMock,
    inv: MagicMock,
    start_time: datetime.time,
    end_time: datetime.time,
    target_soc: int,
    now: datetime.datetime,
) -> Any:
    """Start a charge session and return the interval callback."""
    inv.get_schedule.return_value = {"enable": 0, "groups": []}

    captured_cb = None

    def capture(h: Any, cb: Any, i: Any) -> MagicMock:
        nonlocal captured_cb
        captured_cb = cb
        return MagicMock()

    _register_services(hass)
    handler = _get_handler(hass, "smart_charge")

    with (
        patch(f"{_LISTENERS}.dt_util.now", return_value=now),
        patch(
            f"{_LISTENERS}.async_track_point_in_time",
            return_value=MagicMock(),
        ),
        patch(
            f"{_LISTENERS}.async_track_time_interval",
            side_effect=capture,
        ),
    ):
        await handler(
            _make_call(
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "target_soc": target_soc,
                }
            )
        )

    assert captured_cb is not None
    return captured_cb


class TestChargeRedeferral:
    """D-043: re-deferral when SoC is ahead of schedule during charge."""

    @pytest.mark.asyncio
    async def test_ahead_of_schedule_switches_to_self_use(self) -> None:
        """When SoC has advanced far ahead (solar supplement), the listener
        should remove the ForceCharge override and revert to self-use."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        # Start at SoC 50%, charge window 2:00-6:00, target 80%
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 50.0, "loadsPower": 0.5, "pvPower": 0.0},
        )

        session_start = datetime.datetime(2026, 4, 23, 2, 0, 0)
        cb = await _start_charge_session(
            hass,
            inv,
            start_time=datetime.time(2, 0),
            end_time=datetime.time(6, 0),
            target_soc=80,
            now=session_start,
        )

        # Simulate: charging has been active, SoC jumped to 75% by 3:00
        # (solar supplement pushed it ahead). Energy needed: only 0.5 kWh
        # with 3h remaining — deferred start would be ~5:50.
        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        state["charging_started"] = True
        state["charging_started_at"] = session_start
        state["start_soc"] = 50.0
        state["charging_started_energy_kwh"] = 5.0
        state["last_power_w"] = 2000

        # Update coordinator to show SoC at 75%
        dd = hass.data[DOMAIN]
        for entry_data in dd.entries.values():
            entry_data.coordinator.data = {
                "SoC": 75.0,
                "loadsPower": 0.5,
                "pvPower": 2.0,
            }

        inv.set_schedule.reset_mock()
        now_tick = datetime.datetime(2026, 4, 23, 3, 0, 0)
        with patch(f"{_LISTENERS}.dt_util.now", return_value=now_tick):
            await cb(now_tick)

        # The listener should have removed ForceCharge override (self-use)
        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        assert state["charging_started"] is False, (
            "charging_started should be cleared when ahead of schedule"
        )

    @pytest.mark.asyncio
    async def test_at_or_behind_schedule_keeps_charging(self) -> None:
        """When SoC is on track, the listener continues adjusting power
        normally — no re-deferral."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 50.0, "loadsPower": 0.5, "pvPower": 0.0},
        )

        session_start = datetime.datetime(2026, 4, 23, 2, 0, 0)
        cb = await _start_charge_session(
            hass,
            inv,
            start_time=datetime.time(2, 0),
            end_time=datetime.time(6, 0),
            target_soc=80,
            now=session_start,
        )

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        state["charging_started"] = True
        state["charging_started_at"] = session_start
        state["start_soc"] = 50.0
        state["charging_started_energy_kwh"] = 5.0
        state["last_power_w"] = 2000

        # SoC is 55% at 5:45 — needs 2.5 kWh, deferred start ~05:42.
        # now (5:45) > deferred (5:42), so no re-deferral.
        dd = hass.data[DOMAIN]
        for entry_data in dd.entries.values():
            entry_data.coordinator.data = {
                "SoC": 55.0,
                "loadsPower": 0.5,
                "pvPower": 0.0,
            }

        now_tick = datetime.datetime(2026, 4, 23, 5, 45, 0)
        with patch(f"{_LISTENERS}.dt_util.now", return_value=now_tick):
            await cb(now_tick)

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        assert state["charging_started"] is True, (
            "charging_started should remain True when on schedule"
        )

    @pytest.mark.asyncio
    async def test_redeferral_clears_charging_started(self) -> None:
        """Re-deferral must clear charging_started so the next tick
        re-evaluates from the deferral path."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 50.0, "loadsPower": 0.3, "pvPower": 0.0},
        )

        session_start = datetime.datetime(2026, 4, 23, 2, 0, 0)
        cb = await _start_charge_session(
            hass,
            inv,
            start_time=datetime.time(2, 0),
            end_time=datetime.time(6, 0),
            target_soc=80,
            now=session_start,
        )

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        state["charging_started"] = True
        state["charging_started_at"] = session_start
        state["start_soc"] = 50.0
        state["charging_started_energy_kwh"] = 5.0
        state["last_power_w"] = 3000

        # SoC jumped to 78% — almost at target with 3h left
        dd = hass.data[DOMAIN]
        for entry_data in dd.entries.values():
            entry_data.coordinator.data = {
                "SoC": 78.0,
                "loadsPower": 0.3,
                "pvPower": 3.0,
            }

        now_tick = datetime.datetime(2026, 4, 23, 3, 0, 0)
        with patch(f"{_LISTENERS}.dt_util.now", return_value=now_tick):
            await cb(now_tick)

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        assert state["charging_started"] is False
        assert (
            "charging_started_at" not in state
            or state.get("charging_started_at") is None
        )

    @pytest.mark.asyncio
    async def test_redeferral_saves_session(self) -> None:
        """Session must be persisted after switching to self-use."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 50.0, "loadsPower": 0.3, "pvPower": 0.0},
        )

        session_start = datetime.datetime(2026, 4, 23, 2, 0, 0)
        cb = await _start_charge_session(
            hass,
            inv,
            start_time=datetime.time(2, 0),
            end_time=datetime.time(6, 0),
            target_soc=80,
            now=session_start,
        )

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        state["charging_started"] = True
        state["charging_started_at"] = session_start
        state["start_soc"] = 50.0
        state["charging_started_energy_kwh"] = 5.0
        state["last_power_w"] = 3000

        dd = hass.data[DOMAIN]
        for entry_data in dd.entries.values():
            entry_data.coordinator.data = {
                "SoC": 78.0,
                "loadsPower": 0.3,
                "pvPower": 3.0,
            }

        store = dd.store
        store.async_delay_save = MagicMock()

        now_tick = datetime.datetime(2026, 4, 23, 3, 0, 0)
        with patch(f"{_LISTENERS}.dt_util.now", return_value=now_tick):
            await cb(now_tick)

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        assert state["charging_started"] is False
        assert store.async_delay_save.called, "Session must be saved after re-deferral"

    @pytest.mark.asyncio
    async def test_resumes_charging_after_redeferral(self) -> None:
        """After re-deferral, when deferral deadline arrives on a later tick,
        charging should resume (charging_started becomes True again)."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 50.0, "loadsPower": 0.5, "pvPower": 0.0},
        )

        session_start = datetime.datetime(2026, 4, 23, 2, 0, 0)
        cb = await _start_charge_session(
            hass,
            inv,
            start_time=datetime.time(2, 0),
            end_time=datetime.time(6, 0),
            target_soc=80,
            now=session_start,
        )

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        state["charging_started"] = True
        state["charging_started_at"] = session_start
        state["start_soc"] = 50.0
        state["charging_started_energy_kwh"] = 5.0
        state["last_power_w"] = 2000

        # Tick 1: SoC at 75% at 3:00 — way ahead, should re-defer
        dd = hass.data[DOMAIN]
        for entry_data in dd.entries.values():
            entry_data.coordinator.data = {
                "SoC": 75.0,
                "loadsPower": 0.5,
                "pvPower": 2.0,
            }

        now_tick1 = datetime.datetime(2026, 4, 23, 3, 0, 0)
        with patch(f"{_LISTENERS}.dt_util.now", return_value=now_tick1):
            await cb(now_tick1)

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        assert state["charging_started"] is False, "Should have re-deferred"

        # Tick 2: Still at 75% at 5:57 — past deferred start (~05:56), should resume
        for entry_data in dd.entries.values():
            entry_data.coordinator.data = {
                "SoC": 75.0,
                "loadsPower": 0.5,
                "pvPower": 0.0,
            }

        now_tick2 = datetime.datetime(2026, 4, 23, 5, 57, 0)
        with patch(f"{_LISTENERS}.dt_util.now", return_value=now_tick2):
            await cb(now_tick2)

        state = hass.data[DOMAIN].smart_charge_state
        assert state is not None
        assert state["charging_started"] is True, (
            "Should resume charging when deferred start deadline arrives"
        )
