"""Brand-agnostic: a full-power charge session never defers (issue #32).

``force_charge`` runs as a smart charge session with ``full_power`` set.
The listener's pre-start deferral and the D-043 "ahead of schedule"
re-defer ignored that flag, so a forced charge was handed back to
self-use whenever the pacing maths found slack — permanently when the
battery capacity was unset, which makes the energy needed look like 0.

Per C-040 these run ``smart_battery/`` through
``setup_smart_charge_listeners`` against a :class:`FakeAdapter`; the
FoxESS-level tests are in ``test_charge_redeferral.py``.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from smart_battery.testing import FakeAdapter
from smart_battery.types import WorkMode

from .test_smart_charge_unreachable_clear import (
    _DOMAIN,
    _LISTENERS,
    _build_hass,
    _capture_listener_callback,
    _make_charge_state,
)

_NOW = datetime.datetime(2026, 10, 13, 11, 30, 0)
_END = datetime.datetime(2026, 10, 13, 14, 0, 0)


async def _tick(
    *, capacity_kwh: float, soc: float, charging_started: bool
) -> tuple[dict[str, object], FakeAdapter]:
    hass = _build_hass(
        coordinator_data={"SoC": soc, "loadsPower": 0.5, "pvPower": 0.0},
        battery_capacity_kwh=capacity_kwh,
    )
    state = _make_charge_state(
        now=_NOW,
        end=_END,
        target_soc=100,
        battery_capacity_kwh=capacity_kwh,
        max_power_w=10000,
        current_soc=soc,
        last_power_w=10000,
        charging_started=charging_started,
    )
    state["full_power"] = True
    hass.data[_DOMAIN].smart_charge_state = state
    adapter = FakeAdapter(max_power_w=10000)
    cb = await _capture_listener_callback(hass, adapter, end=_END)
    with patch(f"{_LISTENERS}.dt_util.now", return_value=_NOW):
        await cb(_NOW)
    result = hass.data[_DOMAIN].smart_charge_state
    assert result is not None
    return result, adapter


class TestFullPowerNeverDefers:
    @pytest.mark.asyncio
    async def test_no_redefer_when_ahead_of_schedule(self) -> None:
        # 62% of 42 kWh with 2.5 h left: pacing alone would wait ~25 min.
        state, adapter = await _tick(capacity_kwh=42.0, soc=62.0, charging_started=True)
        assert state["charging_started"] is True
        assert adapter.remove_override_calls == []

    @pytest.mark.asyncio
    async def test_no_redefer_with_capacity_unset(self) -> None:
        state, adapter = await _tick(capacity_kwh=0.0, soc=40.0, charging_started=True)
        assert state["charging_started"] is True
        assert adapter.remove_override_calls == []

    @pytest.mark.asyncio
    async def test_starts_immediately_at_full_power(self) -> None:
        state, adapter = await _tick(
            capacity_kwh=42.0, soc=62.0, charging_started=False
        )
        assert state["charging_started"] is True
        (call,) = adapter.apply_mode_calls
        assert call.mode == WorkMode.FORCE_CHARGE
        assert call.power_w == 10000


async def _ticks(
    socs: list[float], *, full_power: bool, minutes_left: float = 120
) -> tuple[dict[str, object], FakeAdapter, MagicMock]:
    """Run one listener tick per SoC reading, a minute apart."""
    end = _NOW + datetime.timedelta(minutes=minutes_left)
    hass = _build_hass(
        coordinator_data={"SoC": socs[0], "loadsPower": 0.5, "pvPower": 0.0},
        battery_capacity_kwh=42.0,
    )
    state = _make_charge_state(
        now=_NOW,
        end=end,
        target_soc=100,
        battery_capacity_kwh=42.0,
        max_power_w=10000,
        current_soc=socs[0],
        last_power_w=10000,
        charging_started=True,
    )
    state["full_power"] = full_power
    hass.data[_DOMAIN].smart_charge_state = state
    adapter = FakeAdapter(max_power_w=10000)
    cb = await _capture_listener_callback(hass, adapter, end=end)
    coordinator = hass.data[_DOMAIN].entries["entry1"].coordinator
    with patch(f"{_LISTENERS}._create_unreachable_issue") as create_issue:
        for i, soc in enumerate(socs):
            coordinator.data["SoC"] = soc
            now = _NOW + datetime.timedelta(minutes=i)
            with patch(f"{_LISTENERS}.dt_util.now", return_value=now):
                await cb(now)
    result = hass.data[_DOMAIN].smart_charge_state
    assert result is not None
    return result, adapter, create_issue


class TestFullPowerTargetIsFinal:
    """Issue #32: a forced charge reached 100 %, went to self-use, and
    restarted ForceCharge for the rest of the window when the battery
    dropped to 99 %.  Reaching the target finishes a full-power charge;
    only a paced session re-arms on a dip (contract §7.6)."""

    @pytest.mark.asyncio
    async def test_no_restart_after_a_dip_below_target(self) -> None:
        state, adapter, _ = await _ticks([100.0, 99.0, 98.0], full_power=True)
        assert state["target_reached"] is True
        assert adapter.apply_mode_calls == []
        assert len(adapter.remove_override_calls) == 1

    @pytest.mark.asyncio
    async def test_a_paced_session_still_resumes(self) -> None:
        state, adapter, _ = await _ticks([100.0, 60.0], full_power=False)
        assert state["target_reached"] is False


class TestFullPowerNeverReportsUnreachable:
    """Issue #32: "Smart charge can't reach target SoC" was raised for a
    forced charge at 99 % with 0.16 h left.  A forced charge already runs
    at maximum power for the whole window, so the warning has nothing to
    tell the user; a paced session still raises it (C-022)."""

    @pytest.mark.asyncio
    async def test_no_unreachable_issue_for_a_forced_charge(self) -> None:
        _, _, create_issue = await _ticks([20.0], full_power=True, minutes_left=10)
        create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_paced_session_still_reports_it(self) -> None:
        _, _, create_issue = await _ticks([20.0], full_power=False, minutes_left=10)
        create_issue.assert_called_once()
