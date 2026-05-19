"""Brand-agnostic regression tests for the charge_target_unreachable
Repair-issue clear path (C-022, C-026).

The bug: ``smart_battery/listeners.py:_adjust_charge_power`` raises an
HA Repair issue when ``is_charge_target_reachable`` returns False, but
the clear path only runs in the active-charge post-adjust branch
(lines ~922-933).  Two early-``return`` paths skip the clear:

1. **Pre-start defer branch** (~line 737): when ``charging_started`` is
   False and ``now < deferred``, the function returns without checking
   whether the previously-issued ``unreachable_issued`` flag should be
   cleared.
2. **D-043 re-defer branch** (~line 843): when SoC has run ahead of
   schedule, the listener removes the override, clears
   ``charging_started``, and returns — again without clearing the
   issue.

Live failure observed 2026-05-18: active charge raised the issue, then
solar improved and pushed SoC ahead of schedule; the D-043 branch
fired (charging_started=False, deferred_start_committed set);
subsequent ticks exit via the pre-start defer branch.  The post-adjust
clear is unreachable.  Sensor reports
``charge_target_reachable: true`` and ``charge_phase: deferred`` but
the Repair issue persists — sensor and Repair contradict each other.

These tests are brand-agnostic per **C-040** — they exercise
``smart_battery/`` code through ``setup_smart_charge_listeners``
against a :class:`smart_battery.testing.FakeAdapter`, with no FoxESS
client, simulator, or brand-specific module loaded.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from smart_battery.domain_data import EntryData, SmartBatteryDomainData
from smart_battery.listeners import setup_smart_charge_listeners
from smart_battery.testing import FakeAdapter
from smart_battery.types import create_charge_session

_LISTENERS = "smart_battery.listeners"
_DOMAIN = "smart_battery_test"


def _build_hass(
    coordinator_data: dict[str, Any],
    *,
    battery_capacity_kwh: float = 10.0,
    inverter_max_power_w: int = 5000,
) -> MagicMock:
    """Build a minimal hass-shaped MagicMock with typed domain data.

    Uses the brand-agnostic :class:`SmartBatteryDomainData`, no
    FoxESS-specific dataclasses — proves the listener code under test
    is brand-portable.
    """
    hass = MagicMock()
    hass.async_add_executor_job = MagicMock(side_effect=lambda fn, *a: fn(*a))
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, **kwargs: asyncio.ensure_future(coro)
    )

    coordinator = MagicMock()
    coordinator.data = coordinator_data
    coordinator.update_interval = datetime.timedelta(seconds=300)

    entry = MagicMock()
    # Empty options → all listener helpers fall back to defaults
    # (CONF_SMART_HEADROOM, CONF_GRID_EXPORT_LIMIT, ...).
    entry.options = {}

    dd = SmartBatteryDomainData()
    dd.entries["entry1"] = EntryData(
        coordinator=coordinator,
        inverter=None,
        entry=entry,
    )

    store = MagicMock()
    store.async_load = MagicMock()

    # async_load is awaited; return an awaitable
    async def _load() -> dict[str, Any]:
        return {}

    store.async_load = _load
    store.async_delay_save = MagicMock()
    dd.store = store

    hass.data = {_DOMAIN: dd}
    return hass


def _make_charge_state(
    *,
    now: datetime.datetime,
    end: datetime.datetime,
    target_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    current_soc: float,
    last_power_w: int = 3000,
    charging_started: bool = True,
) -> dict[str, Any]:
    """Build a charge session state dict via the canonical factory."""
    state = cast(
        "dict[str, Any]",
        create_charge_session(
            start=now - datetime.timedelta(hours=1),
            end=end,
            target_soc=target_soc,
            battery_capacity_kwh=battery_capacity_kwh,
            max_power_w=max_power_w,
            initial_power=last_power_w,
            min_soc_on_grid=10,
            min_power_change=200,
            api_min_soc=10,
            force=False,
            current_soc=current_soc,
            should_defer=not charging_started,
            now=now,
            groups=[],
        ),
    )
    if charging_started:
        state["charging_started"] = True
        state["charging_started_at"] = now - datetime.timedelta(hours=1)
        state["charging_started_energy_kwh"] = (
            current_soc / 100.0 * battery_capacity_kwh
        )
    state["last_power_w"] = last_power_w
    return state


async def _capture_listener_callback(
    hass: MagicMock,
    adapter: FakeAdapter,
    *,
    end: datetime.datetime,
) -> Any:
    """Register listeners and return the periodic adjust callback.

    NB: ``setup_smart_charge_listeners`` itself calls
    ``_clear_unreachable_issue`` at session start (defensive cleanup)
    before any tick fires.  Callers wanting to assert *tick-time*
    behaviour must run setup OUTSIDE the assertion-mock context, or
    reset the mocks after setup returns.
    """
    captured: list[Any] = []

    def capture_interval(h: Any, cb: Any, i: Any) -> MagicMock:
        captured.append(cb)
        return MagicMock()

    with (
        patch(f"{_LISTENERS}.async_track_point_in_time", return_value=MagicMock()),
        patch(f"{_LISTENERS}.async_track_time_interval", side_effect=capture_interval),
    ):
        setup_smart_charge_listeners(hass, _DOMAIN, adapter)

    assert captured, "setup_smart_charge_listeners did not register the periodic CB"
    return captured[0]


class TestUnreachableIssueClearOnEarlyReturn:
    """C-022 / C-026: the unreachable Repair issue must clear on every
    branch where the algorithm reports the target is reachable, not
    only the active-charge post-adjust branch.
    """

    @pytest.mark.asyncio
    async def test_redefer_branch_clears_unreachable_when_reachable(self) -> None:
        """D-043 re-defer with `unreachable_issued=True` → clear.

        Reproduces the live 2026-05-18 failure:
        1. Active charge raised the issue (unreachable_issued=True).
        2. Conditions improve: net consumption drops, SoC at 78% with
           3h remaining → target trivially reachable AND ahead of
           schedule (D-043 fires).
        3. The re-defer early-return path must clear the issue before
           returning, otherwise the next tick exits via pre-start
           defer and the clear is permanently unreachable.
        """
        # Window 02:00-06:00, 30 min into charge.  SoC 78% with 80%
        # target — easily reachable in 3.5h remaining, AND ahead of the
        # pacing schedule (deferred start computes near window end).
        now = datetime.datetime(2026, 5, 18, 2, 30, 0)
        end = datetime.datetime(2026, 5, 18, 6, 0, 0)
        hass = _build_hass(
            coordinator_data={"SoC": 78.0, "loadsPower": 0.3, "pvPower": 3.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_charge_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=78.0,
            charging_started=True,
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = FakeAdapter(max_power_w=5000)

        # Register listeners FIRST (setup calls _clear_unreachable_issue
        # defensively at session start — that is correct behaviour but
        # not what we are asserting here).
        cb = await _capture_listener_callback(hass, adapter, end=end)
        # Now mark the issue as previously raised — simulating a prior
        # tick that hit the post-adjust unreachable branch.
        hass.data[_DOMAIN].smart_charge_state["unreachable_issued"] = True

        with (
            patch(f"{_LISTENERS}._create_unreachable_issue") as create_mock,
            patch(f"{_LISTENERS}._clear_unreachable_issue") as clear_mock,
            patch(f"{_LISTENERS}.dt_util.now", return_value=now),
        ):
            await cb(now)

        cur_state = hass.data[_DOMAIN].smart_charge_state
        assert cur_state is not None

        # D-043 must have fired (validates the test reaches the right
        # branch — without this guard the test silently falls through
        # to active-charge post-adjust and would still pass after a
        # fix that only patched the pre-start branch).
        assert cur_state["charging_started"] is False, (
            "Test pre-condition: D-043 re-defer must have fired "
            "(otherwise we are not exercising the early-return path "
            "this test targets)"
        )

        # The bug: clear was NEVER called on the re-defer path.
        assert clear_mock.called, (
            "C-026 violation: _clear_unreachable_issue must be called "
            "on the D-043 re-defer early-return path when the target "
            "is reachable again"
        )
        # And the in-state flag must be reset so subsequent ticks
        # (which exit via pre-start defer) do not re-raise.
        assert cur_state.get("unreachable_issued") is False, (
            "unreachable_issued flag must be cleared alongside the Repair-registry call"
        )
        # No new issue raised in this scenario.
        assert not create_mock.called, (
            "create_unreachable_issue must not fire when target is reachable"
        )

    @pytest.mark.asyncio
    async def test_pre_start_defer_branch_clears_unreachable_when_reachable(
        self,
    ) -> None:
        """Pre-start defer branch with `unreachable_issued=True` → clear.

        Mirror of the D-043 case for the pre-start defer early return.
        After D-043 fires once, ``charging_started`` stays False until
        the deferred-start deadline arrives; every intervening tick
        exits via this branch.  Without the fix, the issue persists
        for the rest of the session.
        """
        # Window 02:00-06:00.  Tick at 02:30 — well before the
        # deferred start (which is computed for ~end-window with this
        # easily-reachable scenario).
        now = datetime.datetime(2026, 5, 18, 2, 30, 0)
        end = datetime.datetime(2026, 5, 18, 6, 0, 0)
        hass = _build_hass(
            coordinator_data={"SoC": 78.0, "loadsPower": 0.3, "pvPower": 3.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_charge_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=78.0,
            charging_started=False,  # pre-start / re-deferred state
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = FakeAdapter(max_power_w=5000)

        cb = await _capture_listener_callback(hass, adapter, end=end)
        # Carry the unreachable_issued flag forward (as would happen
        # after the D-043 branch on a previous tick) — even with the
        # D-043 fix, this branch must independently clear because a
        # session can also enter the pre-start defer branch on its
        # very first tick if the user starts a session whose deferred
        # start is in the future.
        hass.data[_DOMAIN].smart_charge_state["unreachable_issued"] = True

        with (
            patch(f"{_LISTENERS}._create_unreachable_issue") as create_mock,
            patch(f"{_LISTENERS}._clear_unreachable_issue") as clear_mock,
            patch(f"{_LISTENERS}.dt_util.now", return_value=now),
        ):
            await cb(now)

        cur_state = hass.data[_DOMAIN].smart_charge_state
        assert cur_state is not None

        # Test pre-condition: pre-start defer branch fired (we did NOT
        # transition to active charging this tick).
        assert cur_state["charging_started"] is False, (
            "Test pre-condition: pre-start defer branch must fire "
            "(otherwise we are not exercising the early-return path "
            "this test targets)"
        )

        assert clear_mock.called, (
            "C-026 violation: _clear_unreachable_issue must be called "
            "on the pre-start defer early-return path when the target "
            "has become reachable"
        )
        assert cur_state.get("unreachable_issued") is False, (
            "unreachable_issued flag must be cleared alongside the Repair-registry call"
        )
        assert not create_mock.called

    @pytest.mark.asyncio
    async def test_redefer_branch_does_not_clear_when_no_issue_raised(
        self,
    ) -> None:
        """Inverse: do not call clear() if no issue was ever raised.

        Catches a regression where the fix unconditionally clears on
        every tick, which would generate spurious registry churn and
        mask other bugs (clear called even when no issue exists).
        """
        now = datetime.datetime(2026, 5, 18, 2, 30, 0)
        end = datetime.datetime(2026, 5, 18, 6, 0, 0)
        hass = _build_hass(
            coordinator_data={"SoC": 78.0, "loadsPower": 0.3, "pvPower": 3.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_charge_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=78.0,
            charging_started=True,
        )
        # No flag set — fresh state, target reachable from the start.
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = FakeAdapter(max_power_w=5000)

        cb = await _capture_listener_callback(hass, adapter, end=end)

        with (
            patch(f"{_LISTENERS}._create_unreachable_issue") as create_mock,
            patch(f"{_LISTENERS}._clear_unreachable_issue") as clear_mock,
            patch(f"{_LISTENERS}.dt_util.now", return_value=now),
        ):
            await cb(now)

        # D-043 fired but no issue was outstanding → no clear call.
        assert not clear_mock.called, (
            "Must not call _clear_unreachable_issue when no issue was "
            "previously raised — wastes registry round-trips and "
            "masks other bugs"
        )
        assert not create_mock.called


class TestUnreachableIssueRendersWithPlaceholders:
    """C-020 / C-026: the Repair issue must surface the actual
    user-relevant numbers (current SoC, target SoC, remaining hours,
    max power) so the user can determine system state from the UI
    alone — not "foxess_control: charge_target_unreachable" as a
    bare key.

    HA renders Repair issues by looking up ``translation_key`` against
    the integration's ``strings.json`` ``issues`` block.  Without an
    entry there, HA falls back to the raw key.  And without
    ``translation_placeholders`` populated by the listener, even a
    well-written translation cannot interpolate the actual gap
    (current SoC, target SoC, remaining hours, max power).

    These tests assert the contract on the listener's call — the
    strings.json contents are tested separately by the existing
    translation-coverage test suite.
    """

    @pytest.mark.asyncio
    async def test_create_issue_passes_translation_placeholders(self) -> None:
        """The listener must pass current_soc, target_soc, remaining_h,
        max_power_w to ``async_create_issue`` so the translated
        message can interpolate them."""
        # Active charge with target out of reach in the remaining
        # window — drives the post-adjust unreachable branch.
        # Window 02:00-02:30 (30 min total), tick at 02:25 (5 min
        # remaining); 30% SoC, 90% target, max 5000W, 10kWh capacity →
        # reaching target needs ~6kWh, but max 5kW × 5min = ~0.42kWh.
        now = datetime.datetime(2026, 5, 18, 2, 25, 0)
        end = datetime.datetime(2026, 5, 18, 2, 30, 0)
        hass = _build_hass(
            coordinator_data={"SoC": 30.0, "loadsPower": 0.3, "pvPower": 0.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_charge_state(
            now=now,
            end=end,
            target_soc=90,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=30.0,
            charging_started=True,
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = FakeAdapter(max_power_w=5000)
        cb = await _capture_listener_callback(hass, adapter, end=end)

        # Patch async_create_issue at its source so we observe the
        # exact kwargs the listener constructs — one level deeper
        # than the _create_unreachable_issue helper.
        with (
            patch(
                "homeassistant.helpers.issue_registry.async_create_issue"
            ) as create_issue_mock,
            patch(f"{_LISTENERS}.dt_util.now", return_value=now),
        ):
            await cb(now)

        # The post-adjust branch must have fired the create call.
        assert create_issue_mock.called, (
            "Test pre-condition: scenario must reach the unreachable "
            "branch (target 90% from SoC 30% in 5 min at 5kW max)"
        )

        kwargs = create_issue_mock.call_args.kwargs
        placeholders = kwargs.get("translation_placeholders")
        assert placeholders is not None, (
            "C-020 violation: async_create_issue must be called with "
            "translation_placeholders so the rendered Repair message "
            "can name the actual SoC/target/remaining/power numbers "
            "instead of falling back to the bare translation key"
        )

        # Each placeholder must be present and carry a sensible value
        # (formatted as a string per HA's translation contract).
        for key in ("current_soc", "target_soc", "remaining_hours", "max_power_w"):
            assert key in placeholders, (
                f"translation_placeholders missing required key {key!r}: "
                f"got {sorted(placeholders.keys())}"
            )

        # Spot-check the values. The listener may format these as
        # strings; we accept either str or numeric and coerce for the
        # comparison.
        assert int(float(placeholders["target_soc"])) == 90
        assert abs(float(placeholders["current_soc"]) - 30.0) < 1.0
        # Remaining hours = 5 min ≈ 0.083h — accept anything in (0, 0.2).
        rh = float(placeholders["remaining_hours"])
        assert 0.0 < rh < 0.2, f"remaining_hours={rh} outside expected range"
        # Max power was 5000W (the effective max in this scenario).
        assert int(float(placeholders["max_power_w"])) == 5000
