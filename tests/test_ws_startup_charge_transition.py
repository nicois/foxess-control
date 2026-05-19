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
