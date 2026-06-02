"""Regression test for WS-startup latency on deferred→active charge
transition (live observation 2026-05-19).

Symptom (from production):

    01:00:01 — sensor.foxess_status flipped from "deferred" to "charging"
               inside _adjust_charge_power.
    01:04:21 — first "FoxESS WebSocket connected" log line.

A 4 m 20 s gap during which ``data_freshness=api`` and the dashboard
shows stale 5-minute REST data instead of the 5-second WS push — a
direct violation of **C-020** (UI must reflect actual system state
without log inspection).

Root cause: the brand-side wrapper ``_ws_aware_charge_cb`` in
``custom_components/foxess_control/__init__.py`` is the ONLY path
that wakes ``_maybe_start_realtime_ws`` after session-start.  The
wrapper fires on a periodic timer at ``SMART_CHARGE_ADJUST_SECONDS``
(300 s) intervals — so if the deferred→active transition happens
just after the wrapper's last tick, WS startup waits up to 5 minutes
for the next tick.  The brand-agnostic listener flips
``charging_started=True`` inside
``smart_battery/listeners.py:_adjust_charge_power_inner`` (around
line 811) but has no event-driven hook to notify the brand layer
that a session-start transition has occurred — only the periodic
tick is polled.

Fix shape (option b from the bug report):

The brand-agnostic ``InverterAdapter`` Protocol gains an optional
``on_session_started`` Callable hook. The listener calls
``adapter.on_session_started(...)`` at the moment of transition.
The brand layer's ``_build_foxess_adapter`` injects a function that
schedules ``_maybe_start_realtime_ws(hass)``.  This is event-driven,
respects **C-039** (no brand imports from ``smart_battery/`` —
dependency inversion via Protocol/Callable), and keeps the test
brand-agnostic per **C-040** (FakeAdapter records the hook).

This test exercises the brand-agnostic listener directly via
``setup_smart_charge_listeners`` against a ``FakeAdapter``, drives a
deferred→active transition by invoking the periodic adjust callback
under conditions where ``calculate_deferred_start`` resolves to a
time at or before ``now``, then asserts that the adapter's
``on_session_started`` hook was invoked AS PART OF THE TRANSITION
TICK — NOT contingent on a separate brand-side wrapper firing.

The test must FAIL on current ``develop`` because the listener has
no such hook today.
"""

from __future__ import annotations

import asyncio
import datetime
from contextlib import ExitStack
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
) -> MagicMock:
    """Minimal hass shaped like the brand-agnostic listener expects.

    Mirrors the helper in ``test_smart_charge_unreachable_clear.py``.
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
    entry.options = {}

    dd = SmartBatteryDomainData()
    dd.entries["entry1"] = EntryData(
        coordinator=coordinator,
        inverter=None,
        entry=entry,
    )

    store = MagicMock()

    async def _load() -> dict[str, Any]:
        return {}

    store.async_load = _load
    store.async_delay_save = MagicMock()
    dd.store = store

    hass.data = {_DOMAIN: dd}
    return hass


def _make_deferred_state(
    *,
    now: datetime.datetime,
    end: datetime.datetime,
    target_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    current_soc: float,
) -> dict[str, Any]:
    """Build a charge-session state in the DEFERRED phase.

    ``charging_started=False`` and ``start <= now`` so the deferred
    pre-start branch is the one that runs on the next tick.
    """
    state = cast(
        "dict[str, Any]",
        create_charge_session(
            start=now - datetime.timedelta(hours=1),
            end=end,
            target_soc=target_soc,
            battery_capacity_kwh=battery_capacity_kwh,
            max_power_w=max_power_w,
            initial_power=0,
            min_soc_on_grid=10,
            min_power_change=200,
            api_min_soc=10,
            force=False,
            current_soc=current_soc,
            should_defer=True,
            now=now,
            groups=[],
        ),
    )
    state["last_power_w"] = 0
    return state


async def _capture_listener_callback(
    hass: MagicMock,
    adapter: FakeAdapter,
) -> Any:
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


class _PointInTimeRegistry:
    """Records every ``async_track_point_in_time`` registration.

    Each entry is ``(when, callback)``.  The interval callback (the
    300 s adjust tick) is captured separately so the test can assert
    that the *prompt* transition trigger is a point-in-time wake at
    the committed deferred-start deadline — not merely the slow
    periodic interval.
    """

    def __init__(self) -> None:
        self.point_in_time: list[tuple[datetime.datetime, Any]] = []
        self.interval: list[Any] = []

    @staticmethod
    def _naive(when: datetime.datetime) -> datetime.datetime:
        """Normalise to naive for comparison against naive test clocks.

        The end-of-window timer is registered as a tz-aware UTC time
        (``dt_util.as_utc``); the test drives a naive clock.  Strip
        tzinfo so ``<=`` comparisons don't raise.
        """
        return when.replace(tzinfo=None) if when.tzinfo is not None else when

    def track_point_in_time(self, _h: Any, cb: Any, when: Any) -> MagicMock:
        self.point_in_time.append((when, cb))
        return MagicMock()

    def track_interval(self, _h: Any, cb: Any, _i: Any) -> MagicMock:
        self.interval.append(cb)
        return MagicMock()


async def _setup_with_registry(
    hass: MagicMock,
    adapter: FakeAdapter,
    stack: ExitStack,
) -> tuple[Any, _PointInTimeRegistry]:
    """Run ``setup_smart_charge_listeners`` capturing both timer kinds.

    Returns the periodic interval callback and the registry of all
    point-in-time / interval registrations the listener made.  The
    timer patches are entered on ``stack`` and stay active for the
    caller's lifetime — the listener also schedules point-in-time
    wakes INSIDE its tick callbacks (the fix under test), so the patch
    must remain active while the test drives those ticks, not only
    during setup.
    """
    reg = _PointInTimeRegistry()
    stack.enter_context(
        patch(
            f"{_LISTENERS}.async_track_point_in_time",
            side_effect=reg.track_point_in_time,
        )
    )
    stack.enter_context(
        patch(
            f"{_LISTENERS}.async_track_time_interval",
            side_effect=reg.track_interval,
        )
    )
    setup_smart_charge_listeners(hass, _DOMAIN, adapter)
    assert reg.interval, "setup_smart_charge_listeners did not register periodic CB"
    return reg.interval[0], reg


class _RecordingAdapter(FakeAdapter):
    """FakeAdapter extended with a session-start observer.

    Intentionally implemented as a plain attribute on the adapter
    instance rather than a new Protocol method, so this test does
    NOT presuppose the exact shape of the fix.  The contract being
    asserted is: "the brand-agnostic listener notifies the adapter
    of a deferred→active transition the moment it occurs, via some
    well-defined hook the listener can call."  Whether the fix names
    that hook ``on_session_started`` or something else, the test
    asserts the observable effect: the recorded list grows when the
    transition fires.

    The fix's adapter Protocol surface should be a Callable that the
    listener invokes; this recorder mirrors that shape.
    """

    def __init__(self, max_power_w: int = 5000) -> None:
        super().__init__(max_power_w=max_power_w)
        self.session_started_calls: list[dict[str, Any]] = []

    def on_session_started(self, **payload: Any) -> None:  # noqa: D401
        """Hook fired by the listener when a session transitions to active.

        Default no-op on the base ``InverterAdapter`` Protocol;
        recorded here for assertion in tests.
        """
        self.session_started_calls.append(dict(payload))


class TestWsStartupOnDeferredToActiveTransition:
    """C-020: WS startup must be event-driven on the transition itself,
    not bound to the periodic ``SMART_CHARGE_ADJUST_SECONDS`` tick.

    Asserts the existence of a brand-agnostic hook that the listener
    invokes the moment ``charging_started`` flips to True.  The brand
    layer wires that hook to ``_maybe_start_realtime_ws``; without
    such a hook, the only path to WS startup is the brand wrapper's
    periodic tick — which can lag the actual transition by up to
    ``SMART_CHARGE_ADJUST_SECONDS`` seconds.
    """

    @pytest.mark.asyncio
    async def test_listener_notifies_adapter_on_deferred_to_active_transition(
        self,
    ) -> None:
        """The transition tick must call ``adapter.on_session_started``.

        Test setup: a deferred charge session whose deferred-start
        deadline is at or before ``now`` — the next tick of the
        periodic adjust CB will execute the transition path
        (``charging_started`` flips to True).

        Assertion: the adapter's ``on_session_started`` hook is
        recorded exactly once during that tick — before the test
        function returns, NOT after the next 5-minute interval.

        On current ``develop`` the listener has no such hook, so
        ``session_started_calls`` is empty and the test fails.
        """
        # Window: started 1 h ago, ends 30 min from now. Tight window
        # forces calculate_deferred_start to resolve in the past, so
        # the next tick promotes the session through the
        # deferred→active branch.
        now = datetime.datetime(2026, 5, 19, 1, 0, 0)
        end = now + datetime.timedelta(minutes=30)

        hass = _build_hass(
            coordinator_data={"SoC": 50.0, "loadsPower": 0.3, "pvPower": 0.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_deferred_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=50.0,
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = _RecordingAdapter(max_power_w=5000)

        cb = await _capture_listener_callback(hass, adapter)

        # Sanity pre-condition: charging_started is False going in.
        assert hass.data[_DOMAIN].smart_charge_state["charging_started"] is False

        with patch(f"{_LISTENERS}.dt_util.now", return_value=now):
            await cb(now)

        cur_state = hass.data[_DOMAIN].smart_charge_state
        assert cur_state is not None

        # Test pre-condition: the tick took the deferred→active
        # transition path.  Without this guard, a future change to
        # the deferred-start algorithm could silently skip the
        # transition and the regression test would pass for the
        # wrong reason.
        assert cur_state["charging_started"] is True, (
            "Test pre-condition: the tick must take the "
            "deferred→active transition path. If charging_started is "
            "still False, calculate_deferred_start did not resolve "
            "to <= now under the test inputs and this test is not "
            "exercising the path it intends to."
        )

        # The actual contract: the adapter was notified at the
        # moment of transition. C-020: WS-startup must not wait for
        # the next 5-minute interval timer.
        assert adapter.session_started_calls, (
            "C-020 violation: the listener must notify the adapter "
            "(via an on_session_started hook on InverterAdapter) at "
            "the deferred→active transition, so the brand layer can "
            "start the WebSocket immediately. Today the only path is "
            "the brand wrapper's periodic tick (every "
            "SMART_CHARGE_ADJUST_SECONDS = 300 s) which can lag the "
            "actual transition by up to 5 minutes."
        )

        # Exactly one notification per transition (idempotency
        # neighbourhood case — see follow-up test).
        assert len(adapter.session_started_calls) == 1, (
            "Exactly one on_session_started call per transition; "
            f"observed {len(adapter.session_started_calls)}"
        )

    @pytest.mark.asyncio
    async def test_no_notification_on_pre_start_defer_branch(self) -> None:
        """Inverse / neighbourhood: a tick that stays deferred must NOT fire the hook.

        If the deferred-start deadline is still in the future, the
        tick exits via the pre-start defer branch and
        ``charging_started`` remains False.  No transition occurred,
        so ``on_session_started`` MUST NOT fire.  Catches a
        regression where the fix unconditionally calls the hook on
        every tick — which would mask other bugs and churn WS
        startup attempts during the deferred phase (when WS is
        explicitly suppressed by ``_should_start_realtime_ws``).
        """
        # Long window with low headroom requirement → deferred-start
        # resolves comfortably in the future, tick stays deferred.
        now = datetime.datetime(2026, 5, 19, 1, 0, 0)
        end = now + datetime.timedelta(hours=8)

        hass = _build_hass(
            coordinator_data={"SoC": 78.0, "loadsPower": 0.1, "pvPower": 0.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_deferred_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=78.0,
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = _RecordingAdapter(max_power_w=5000)

        cb = await _capture_listener_callback(hass, adapter)

        with patch(f"{_LISTENERS}.dt_util.now", return_value=now):
            await cb(now)

        cur_state = hass.data[_DOMAIN].smart_charge_state
        assert cur_state is not None
        # Test pre-condition: tick exited via pre-start defer.
        assert cur_state["charging_started"] is False, (
            "Test pre-condition: pre-start defer branch must fire. "
            "calculate_deferred_start unexpectedly resolved at or "
            "before now under these inputs."
        )

        assert not adapter.session_started_calls, (
            "Pre-start defer branch must NOT fire the session-started "
            "hook — no transition occurred. Got "
            f"{adapter.session_started_calls!r}"
        )

    @pytest.mark.asyncio
    async def test_idempotent_across_subsequent_ticks(self) -> None:
        """Neighbourhood: subsequent ticks of an already-active session
        must NOT re-fire the hook.

        Once ``charging_started`` is True, every subsequent tick goes
        through the active-charge adjustment path, NOT the transition
        path.  The session-start hook is for the moment of transition
        only; firing it on every active tick would cause WS startup
        attempts every 5 minutes for the lifetime of the session.
        """
        now = datetime.datetime(2026, 5, 19, 1, 0, 0)
        end = now + datetime.timedelta(minutes=30)

        hass = _build_hass(
            coordinator_data={"SoC": 50.0, "loadsPower": 0.3, "pvPower": 0.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_deferred_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=50.0,
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = _RecordingAdapter(max_power_w=5000)

        cb = await _capture_listener_callback(hass, adapter)

        # First tick: deferred→active transition.
        with patch(f"{_LISTENERS}.dt_util.now", return_value=now):
            await cb(now)

        first_calls = list(adapter.session_started_calls)

        # Second tick: 5 minutes later, charging_started is already
        # True. The active-charge adjustment branch runs; no second
        # transition.
        later = now + datetime.timedelta(minutes=5)
        hass.data[_DOMAIN].coordinator_data = {
            "SoC": 52.0,
            "loadsPower": 0.3,
            "pvPower": 0.0,
        }
        # Keep the coordinator data for _get_current_soc:
        hass.data[_DOMAIN].entries["entry1"].coordinator.data = {
            "SoC": 52.0,
            "loadsPower": 0.3,
            "pvPower": 0.0,
        }

        with patch(f"{_LISTENERS}.dt_util.now", return_value=later):
            await cb(later)

        assert adapter.session_started_calls == first_calls, (
            "Subsequent active-charge ticks must NOT re-fire the "
            "session-started hook. Hook count grew from "
            f"{len(first_calls)} to {len(adapter.session_started_calls)}."
        )


class TestFoxESSAdapterWiresWebSocketStartup:
    """Brand-side wiring: ``FoxESSCloudAdapter.on_session_started`` must
    fire the injected callback synchronously.

    The brand-agnostic listener test above proves the listener invokes
    the Protocol method on the adapter at the transition.  This test
    proves the FoxESS adapter's implementation forwards that
    invocation to its injected callback — i.e. the chain
    listener→adapter→``_maybe_start_realtime_ws`` is wired end to
    end.

    Without this test, the brand-side hook (``on_session_started_cb``
    parameter on ``FoxESSCloudAdapter``) could be silently dropped
    during a refactor and the listener-side test would still pass —
    leaving the original C-020 production bug unfixed.
    """

    def test_cloud_adapter_forwards_to_injected_callback(self) -> None:
        """Constructing with a callback and calling on_session_started fires it.

        Mirrors the wiring in ``_build_foxess_adapter``: the
        callback is the channel the brand layer uses to hop from the
        synchronous Protocol method into
        ``hass.async_create_task(_maybe_start_realtime_ws(hass))``.
        """
        from custom_components.foxess_control.foxess_adapter import (
            FoxESSCloudAdapter,
        )

        recorded: list[str] = []

        def cb(session_type: str) -> None:
            recorded.append(session_type)

        adapter = FoxESSCloudAdapter(
            hass=MagicMock(),
            inverter=MagicMock(max_power_w=5000),
            min_soc_on_grid=10,
            api_min_soc=10,
            start=datetime.datetime(2026, 5, 19, 0, 0, 0),
            end=datetime.datetime(2026, 5, 19, 6, 0, 0),
            on_session_started_cb=cb,
        )

        adapter.on_session_started(session_type="charge")
        adapter.on_session_started(session_type="discharge")

        assert recorded == ["charge", "discharge"], (
            "FoxESSCloudAdapter.on_session_started must forward "
            "session_type to the injected callback in order. "
            f"Got {recorded!r}."
        )

    def test_cloud_adapter_no_callback_is_noop(self) -> None:
        """Without a callback, on_session_started must not raise.

        Defensive: legacy or test-only construction paths that omit
        ``on_session_started_cb`` must be tolerated — the Protocol
        method is still callable, just inert.
        """
        from custom_components.foxess_control.foxess_adapter import (
            FoxESSCloudAdapter,
        )

        adapter = FoxESSCloudAdapter(
            hass=MagicMock(),
            inverter=MagicMock(max_power_w=5000),
            min_soc_on_grid=10,
            api_min_soc=10,
            start=datetime.datetime(2026, 5, 19, 0, 0, 0),
            end=datetime.datetime(2026, 5, 19, 6, 0, 0),
        )

        # Must not raise.
        adapter.on_session_started(session_type="charge")

    def test_build_foxess_adapter_injects_ws_startup(self) -> None:
        """``_build_foxess_adapter`` must inject a callback that
        triggers ``_maybe_start_realtime_ws``.

        Simulates the full chain by constructing the adapter via
        ``_build_foxess_adapter`` against a mocked hass + integration
        config, then invoking ``on_session_started`` on the returned
        adapter and asserting that ``hass.async_create_task`` was
        called with a coroutine produced by ``_maybe_start_realtime_ws``.
        """
        from custom_components.foxess_control import _build_foxess_adapter

        coordinator = MagicMock()
        coordinator.data = {"SoC": 50.0}

        from smart_battery.domain_data import EntryData, SmartBatteryDomainData

        dd = SmartBatteryDomainData()
        dd.entries["entry1"] = EntryData(
            coordinator=coordinator,
            inverter=None,
            entry=MagicMock(),
        )

        hass = MagicMock()
        hass.data = {"foxess_control": dd}

        # Track scheduled coroutines so we can assert on type.
        scheduled: list[Any] = []

        def _create_task(coro: Any, **_kwargs: Any) -> MagicMock:
            scheduled.append(coro)
            # Close the coroutine so we don't get an "unawaited" warning.
            coro.close()
            return MagicMock()

        hass.async_create_task = MagicMock(side_effect=_create_task)

        cfg = MagicMock()
        cfg.entity_mode = False
        cfg.min_soc_on_grid = 10
        cfg.api_min_soc = 10
        cfg.export_limit_entity = None

        state = {
            "start": datetime.datetime(2026, 5, 19, 0, 0, 0),
            "end": datetime.datetime(2026, 5, 19, 6, 0, 0),
            "min_soc_on_grid": 10,
            "api_min_soc": 10,
            "force": False,
            "battery_capacity_kwh": 10.0,
            "groups": [],
        }

        with (
            patch("custom_components.foxess_control._cfg", return_value=cfg),
            patch(
                "custom_components.foxess_control._first_entry_id",
                return_value="entry1",
            ),
            patch(
                "custom_components.foxess_control._get_inverter",
                return_value=MagicMock(max_power_w=5000),
            ),
            patch(
                "custom_components.foxess_control._dd",
                return_value=dd,
            ),
        ):
            adapter = _build_foxess_adapter(hass, MagicMock(max_power_w=5000), state)

        # Sanity: cloud adapter (entity_mode=False).
        assert hasattr(adapter, "on_session_started_cb") or hasattr(
            adapter, "_on_session_started_cb"
        ), "Cloud adapter must store the injected callback"

        # Fire the hook the listener would fire.
        adapter.on_session_started(session_type="charge")

        # The injected callback must have scheduled exactly one task —
        # the coroutine must come from _maybe_start_realtime_ws.
        assert len(scheduled) == 1, (
            "_build_foxess_adapter must inject a callback that "
            "schedules exactly one task via hass.async_create_task. "
            f"Got {len(scheduled)} task(s)."
        )
        coro = scheduled[0]
        # qualname check is the cheapest way to identify the coroutine
        # without awaiting it (which would require a running loop).
        assert "_maybe_start_realtime_ws" in getattr(coro, "__qualname__", ""), (
            "Injected callback must schedule _maybe_start_realtime_ws. "
            f"Got coroutine: {coro!r}"
        )


class TestTransitionFiresPromptlyAtDeferredDeadline:
    """C-020 / D-008: the deferred→active transition (and therefore the
    ``on_session_started`` WS-startup hook) must fire PROMPTLY at the
    committed deferred-start deadline — not be gated to the next
    ``SMART_CHARGE_ADJUST_SECONDS`` (300 s) periodic interval tick.

    Live regression 2026-06-02 (v1.0.17): the status sensor flipped
    ``scheduled → charging`` at 01:00:10 (it recomputes ``now >=
    deferred_start_committed`` on every ~5 s coordinator refresh —
    ``is_effectively_charging``), but the LISTENER only re-evaluates
    the transition on its 300 s interval.  The prior interval tick was
    at 00:58:46, so ``charging_started`` did not actually flip — and
    ``on_session_started`` did not fire — until the next interval tick
    at ~01:03:46, bringing the WebSocket up at 01:04:04.  ~3 m 54 s of
    ``data_freshness=api`` while the dashboard claimed "charging": a
    C-020 defect, the SAME symptom and magnitude as the beta.2 bug.

    The beta.2 fix made WS startup event-driven *on the listener's
    transition* (the ``on_session_started`` hook).  But the listener's
    transition is ITSELF NOT event-driven: it only happens when a
    300 s interval tick observes ``now >= deferred_start_committed``.
    There is no point-in-time wake scheduled AT the deferred-start
    deadline.  So the hook fires promptly relative to the listener's
    transition, but the transition lags the real deadline by up to one
    interval — reproducing the ~4-minute gap.

    These tests exercise the path the existing tests miss: the
    *scheduling* of the transition, not just its hook side effect once
    invoked.  The existing tests call ``cb(now)`` exactly at the
    transition instant, so they never observe that the listener fails
    to arrange to BE called at the deadline.
    """

    @pytest.mark.asyncio
    async def test_prompt_wake_scheduled_at_committed_deferred_deadline(
        self,
    ) -> None:
        """A deferred tick that commits a FUTURE deferred-start must
        schedule a point-in-time wake at (or before) that deadline.

        Setup: a long window whose ``calculate_deferred_start``
        resolves to a time strictly in the future but well WITHIN one
        300 s adjust interval (here ~90 s out is not guaranteed, so we
        assert against the committed value the listener actually
        computed).  After the first tick:

        * ``charging_started`` is still False (we are deferred), and
        * ``deferred_start_committed`` is a future datetime.

        Observable contract: the listener must have registered a
        point-in-time trigger that fires no later than the committed
        deferred-start deadline, so the transition (and WS startup)
        happens within seconds of the deadline — NOT up to 300 s later
        on the next interval tick.

        On current ``develop`` the listener registers only the 300 s
        ``async_track_time_interval``; no point-in-time wake is
        scheduled at the deferred deadline, so this assertion fails —
        reproducing the live ~4-minute WS-startup lag.
        """
        now = datetime.datetime(2026, 6, 2, 0, 58, 46)
        end = now + datetime.timedelta(hours=8)

        hass = _build_hass(
            coordinator_data={"SoC": 78.0, "loadsPower": 0.1, "pvPower": 0.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_deferred_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=78.0,
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = _RecordingAdapter(max_power_w=5000)

        with ExitStack() as stack:
            cb, reg = await _setup_with_registry(hass, adapter, stack)

            # Drive one deferred tick: commits deferred_start_committed.
            stack.enter_context(patch(f"{_LISTENERS}.dt_util.now", return_value=now))
            await cb(now)

        cur_state = hass.data[_DOMAIN].smart_charge_state
        assert cur_state is not None
        # Test pre-condition: we are in the deferred phase with a
        # future committed deadline.
        assert cur_state["charging_started"] is False, (
            "Test pre-condition: tick must stay deferred. "
            "calculate_deferred_start resolved <= now unexpectedly."
        )
        committed = cur_state.get("deferred_start_committed")
        assert committed is not None and committed > now, (
            "Test pre-condition: a future deferred-start must be "
            f"committed. Got {committed!r} (now={now!r})."
        )

        # The next periodic interval tick would land at now + 300 s.
        next_interval = now + datetime.timedelta(seconds=300)

        # Observable contract: a point-in-time wake must be scheduled
        # at or before the committed deadline so the transition is
        # prompt.  A wake that exists but lands AFTER the deadline (or
        # only the 300 s interval) is the bug.
        prompt_wakes = [
            when for (when, _c) in reg.point_in_time if reg._naive(when) <= committed
        ]
        assert prompt_wakes, (
            "C-020 regression: the listener committed a deferred-start "
            f"at {committed}, but scheduled NO point-in-time wake at or "
            "before that deadline. The only trigger is the 300 s "
            f"interval (next at {next_interval}), so charging_started "
            "and the on_session_started WS-startup hook lag the real "
            "deferred-start deadline by up to SMART_CHARGE_ADJUST_"
            "SECONDS (live 2026-06-02: ~3 m 54 s of data_freshness=api "
            "while the status sensor already read 'charging'). "
            f"point-in-time registrations: "
            f"{[w for (w, _c) in reg.point_in_time]!r}"
        )

    @pytest.mark.asyncio
    async def test_firing_prompt_wake_performs_transition_and_hook(
        self,
    ) -> None:
        """Firing the scheduled deferred-deadline wake must perform the
        transition and fire ``on_session_started``.

        End-to-end of the prompt path: after committing a future
        deferred-start, the listener schedules a wake; invoking that
        wake's callback at the committed deadline must flip
        ``charging_started`` True and notify the adapter — i.e. the
        WebSocket starts at the deadline, not one interval later.
        """
        now = datetime.datetime(2026, 6, 2, 0, 58, 46)
        end = now + datetime.timedelta(hours=8)

        hass = _build_hass(
            coordinator_data={"SoC": 78.0, "loadsPower": 0.1, "pvPower": 0.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_deferred_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=78.0,
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = _RecordingAdapter(max_power_w=5000)

        with ExitStack() as stack:
            cb, reg = await _setup_with_registry(hass, adapter, stack)

            now_patch = stack.enter_context(
                patch(f"{_LISTENERS}.dt_util.now", return_value=now)
            )
            await cb(now)

            cur_state = hass.data[_DOMAIN].smart_charge_state
            assert cur_state is not None
            committed = cur_state.get("deferred_start_committed")
            assert committed is not None and committed > now

            # Find the prompt wake scheduled at/just before the deadline.
            prompt = [
                (when, c)
                for (when, c) in reg.point_in_time
                if reg._naive(when) <= committed
            ]
            assert prompt, (
                "C-020 regression: no prompt point-in-time wake scheduled "
                f"at/before the committed deadline {committed}; only the "
                "300 s interval would eventually transition the session."
            )
            wake_when, wake_cb = prompt[-1]

            # No transition / hook yet — we are still deferred.
            assert cur_state["charging_started"] is False
            assert not adapter.session_started_calls

            # Fire the wake at the committed deadline.
            now_patch.return_value = committed
            await wake_cb(committed)

        cur_state = hass.data[_DOMAIN].smart_charge_state
        assert cur_state is not None
        assert cur_state["charging_started"] is True, (
            "Firing the deferred-deadline wake must transition the "
            "session to active (charging_started=True)."
        )
        assert adapter.session_started_calls, (
            "Firing the deferred-deadline wake must fire "
            "on_session_started so the brand layer starts the WebSocket "
            "promptly at the deadline (C-020), not one 300 s interval "
            "later."
        )

    @pytest.mark.asyncio
    async def test_no_prompt_wake_when_already_active(self) -> None:
        """Neighbourhood: an already-active tick must not schedule a
        spurious deferred-deadline wake.

        Once ``charging_started`` is True, ``deferred_start_committed``
        is cleared (None) and the session is in the active-adjust path.
        A tick here must not register a new point-in-time wake for a
        (non-existent) future deferred-start.
        """
        now = datetime.datetime(2026, 6, 2, 1, 0, 0)
        end = now + datetime.timedelta(minutes=30)

        hass = _build_hass(
            coordinator_data={"SoC": 50.0, "loadsPower": 0.3, "pvPower": 0.0},
            battery_capacity_kwh=10.0,
        )
        state = _make_deferred_state(
            now=now,
            end=end,
            target_soc=80,
            battery_capacity_kwh=10.0,
            max_power_w=5000,
            current_soc=50.0,
        )
        hass.data[_DOMAIN].smart_charge_state = state

        adapter = _RecordingAdapter(max_power_w=5000)

        with ExitStack() as stack:
            cb, reg = await _setup_with_registry(hass, adapter, stack)

            now_patch = stack.enter_context(
                patch(f"{_LISTENERS}.dt_util.now", return_value=now)
            )
            # Tick 1: deferred→active transition (tight window).
            await cb(now)

            cur_state = hass.data[_DOMAIN].smart_charge_state
            assert cur_state is not None
            assert cur_state["charging_started"] is True, (
                "Test pre-condition: tight window must transition to active."
            )

            wakes_after_transition = len(reg.point_in_time)

            # Tick 2 while active: must not add a deferred-deadline wake.
            later = now + datetime.timedelta(minutes=5)
            hass.data[_DOMAIN].entries["entry1"].coordinator.data = {
                "SoC": 52.0,
                "loadsPower": 0.3,
                "pvPower": 0.0,
            }
            now_patch.return_value = later
            await cb(later)

        assert len(reg.point_in_time) == wakes_after_transition, (
            "Active-phase ticks must not schedule new deferred-deadline "
            "wakes. point-in-time grew from "
            f"{wakes_after_transition} to {len(reg.point_in_time)}."
        )
