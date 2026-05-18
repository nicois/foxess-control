"""Regression test for the E2E ``_tight_window`` helper near midnight.

Symptom (CI run 26006442121, 2026-05-17 23:58 UTC): 16/20 E2E shards
failed with ``sensor.foxess_smart_operations did not reach
'discharging'/'charging' within 120s/180s/420s (last: 'idle')`` (or
``'discharge_deferred'`` for the deferred-start shard).

Root cause: the failing CI run was triggered at 23:56 UTC; container
build + integration boot took ~2 minutes, so the actual service calls
happened at 23:58:58 - 23:59:16 UTC.  Inside that window
``_tight_window(10)`` returns the strings ``("23:49:00", "23:59:00")``
because the midnight clamp pins ``end_min`` at ``23 * 60 + 59 = 1439``
whenever ``now_min + minutes > 1439``.

The session is therefore registered with ``end ≈ now + 0..2 seconds``.
``setup_smart_{charge,discharge}_listeners`` schedules
``_on_{charge_,}timer_expire`` at ``end_utc`` via
``async_track_point_in_time``; that timer fires within 1-2 seconds and
calls ``cancel_smart_{charge,discharge}`` — clearing
``dd.smart_{charge,discharge}_state`` and re-rendering the
``foxess_smart_operations`` sensor as ``"idle"``.

This is a pre-existing latent flake in the test helper.  Previous green
runs at 07:30 UTC happened to be far from midnight and didn't trigger
it.  The user push at 23:56 UTC put the failing run squarely inside the
broken window — the HA-stable image moving (4 weeks earlier) was a
red herring; the root cause is in the test helper.

C-031 says no flaky tests — fix the root cause, don't tune timeouts.
The fix MUST guarantee that ``_tight_window(minutes)`` returns a
window with at least most of ``minutes`` of remaining duration
*after* ``now`` — never a window whose ``end`` is within seconds of
``now``.  C-009 (no midnight crossings) must still be respected.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import patch

import pytest


def _get_tight_window() -> Any:
    """Import the helper.  Lives in ``tests/e2e/test_e2e.py``."""
    from tests.e2e import test_e2e  # noqa: PLC0415

    return test_e2e._tight_window


def _parse_hhmm(s: str) -> int:
    """Parse 'HH:MM:00' as minute-of-day."""
    h, m, _ = s.split(":")
    return int(h) * 60 + int(m)


class _FakeClock:
    """In-memory clock that advances on ``sleep``.

    The fix in ``_tight_window`` calls ``time.sleep(N)`` to wait until
    past midnight, then re-reads ``datetime.datetime.now``.  To verify
    the post-sleep window correctness without actually sleeping, the
    test patches BOTH the helper's ``datetime`` reference and its
    ``time.sleep`` reference: ``sleep(N)`` advances this clock by N
    seconds; the next ``datetime.now`` call returns the advanced time.
    """

    def __init__(self, start: datetime.datetime) -> None:
        self._now = start

    def sleep(self, seconds: float) -> None:
        self._now = self._now + datetime.timedelta(seconds=seconds)

    def now(self, tz: Any = None) -> datetime.datetime:
        if tz is None:
            return self._now.replace(tzinfo=None)
        return self._now.astimezone(tz)


def _install_fake_clock(clock: _FakeClock) -> tuple[Any, Any]:
    """Patch ``datetime`` and ``time`` on ``tests.e2e.test_e2e``.

    Returns the two patchers; caller activates with ``.__enter__()``
    or ``.start()`` and balances on teardown.  We can't subclass
    ``datetime.datetime`` (mypy rejects dynamic-base subclassing) —
    instead, patch the bound module attribute with a thin stand-in
    that exposes only ``datetime.now`` (the only ``datetime``
    surface the helper uses) and ``UTC``.  Same approach for
    ``time``: patch ``test_e2e.time`` with a stand-in exposing
    ``sleep``.
    """
    from tests.e2e import test_e2e  # noqa: PLC0415

    _utc = datetime.UTC

    class _StandinDatetime:
        @staticmethod
        def now(tz: Any = None) -> datetime.datetime:
            return clock.now(tz)

    _stand_dt = _StandinDatetime

    class _StandinDatetimeModule:
        datetime = _stand_dt
        UTC = _utc

    class _StandinTimeModule:
        @staticmethod
        def sleep(seconds: float) -> None:
            clock.sleep(seconds)

    return (
        patch.object(test_e2e, "datetime", _StandinDatetimeModule),
        patch.object(test_e2e, "time", _StandinTimeModule),
    )


class TestTightWindowFitsRequestedDurationAfterNow:
    """A test that calls ``_tight_window(M)`` and immediately calls a
    service must have enough remaining time for the test's longest
    ``wait_for_state`` to succeed before the schedule's ``end`` fires
    ``_on_timer_expire``.

    The current implementation clamps ``end_min`` at ``23:59`` without
    verifying that ``now`` is far enough away — at ``now=23:58:58`` the
    returned window ends in 2 seconds, breaking every test that waits
    for a state transition longer than that.

    Cases parametrise the time-of-day across the failing band
    (``23:50``-``23:59``) plus a known-good time and the wraparound
    minute itself.  The invariant: ``end_min - now_min`` (with
    midnight-wrap awareness) must be at least ``M - 2`` minutes — the
    helper documents a 2-minute backshift on ``start``, so legitimate
    remaining-duration loss is bounded by 2 minutes.
    """

    @pytest.mark.parametrize(
        ("hhmm", "minutes"),
        [
            ("07:30", 10),  # known-good (matches previous green CI)
            ("23:45", 10),  # within window: end=23:55, plenty remaining
            ("23:50", 10),  # boundary: end clamps to 23:59 (raw would be 00:00)
            ("23:55", 10),  # FAILING band: end=23:59, only ~4 min remaining
            ("23:58", 10),  # FAILING band: end=23:59, ~1 min remaining
            ("23:59", 10),  # worst case: end=23:59, 0 min remaining
            ("00:00", 10),  # wraparound: full window from 00:00
            ("23:55", 30),  # 30-min request near midnight
            ("23:30", 30),  # boundary for 30-min request
        ],
    )
    def test_window_has_sufficient_remaining_duration_after_now(
        self,
        hhmm: str,
        minutes: int,
    ) -> None:
        """``end - now`` (after any midnight wait) must be >=
        ``minutes - 2`` minutes."""
        tight_window = _get_tight_window()

        h, m = (int(x) for x in hhmm.split(":"))
        # Use a fixed date so midnight maths are deterministic.
        now0 = datetime.datetime(2026, 5, 17, h, m, 0, tzinfo=datetime.UTC)
        clock = _FakeClock(now0)
        dt_patcher, time_patcher = _install_fake_clock(clock)

        with dt_patcher, time_patcher:
            start_str, end_str = tight_window(minutes)
            # Read the post-call ``now`` from the fake clock — the fix
            # may have advanced it past midnight via mocked ``sleep``.
            now_after = clock.now(datetime.UTC)

        end_min = _parse_hhmm(end_str)
        now_after_min = now_after.hour * 60 + now_after.minute

        # The window must end on the same day as ``now_after`` (C-009)
        # — if the helper slept past midnight we are now on day N+1
        # and the window is on day N+1 too, so a simple subtraction
        # in minute-of-day works.
        remaining_min = end_min - now_after_min

        # The helper documents "starting ~2 min before now" so we lose
        # up to 2 minutes of remaining duration vs the requested
        # ``minutes`` due to the backshift.  Anything less than that
        # means the window is unusable for an ``end``-bound test.
        min_acceptable_remaining = minutes - 2

        assert remaining_min >= min_acceptable_remaining, (
            f"_tight_window({minutes}) at start_now={hhmm} returned "
            f"({start_str!r}, {end_str!r}) with only {remaining_min} "
            f"minutes remaining after the (post-sleep) now of "
            f"{now_after.hour:02d}:{now_after.minute:02d} — needs >= "
            f"{min_acceptable_remaining}.  A test that calls a service "
            f"and waits for state transitions will hit "
            f"_on_timer_expire and see the session cancelled to "
            f"'idle' before the wait_for_state completes."
        )

    def test_window_does_not_cross_midnight(self) -> None:
        """C-009: end_str's date must equal start_str's date.  The
        helper uses HH:MM:00 strings only (no date), so this is
        encoded as ``end_min > start_min`` — i.e. the helper must
        not return values where ``end`` would naturally fall on the
        next day.
        """
        tight_window = _get_tight_window()

        # Test the late-night band where naïve arithmetic would cross.
        for hh in range(20, 24):
            for mm in (0, 15, 30, 45, 58, 59):
                now0 = datetime.datetime(2026, 5, 17, hh, mm, 0, tzinfo=datetime.UTC)
                clock = _FakeClock(now0)
                dt_patcher, time_patcher = _install_fake_clock(clock)
                with dt_patcher, time_patcher:
                    start_str, end_str = tight_window(10)
                start_min = _parse_hhmm(start_str)
                end_min = _parse_hhmm(end_str)
                assert end_min > start_min, (
                    f"At now={hh:02d}:{mm:02d}, tight_window(10) returned "
                    f"start={start_str}, end={end_str} — end <= start "
                    f"would mean the helper produced a midnight-crossing "
                    f"window, violating C-009."
                )

    def test_window_at_2358_sleeps_past_midnight(self) -> None:
        """The fix's load-bearing behaviour: at ``now=23:58``,
        ``_tight_window(10)`` must sleep ~2 minutes (until past 00:00)
        and then return a window with ~10 minutes after the new now.

        The 23:58:58 production failure case: with the fake clock
        advanced by the helper's mocked sleep, the post-call now must
        be after midnight.  The schedule ``end`` must be at least 8
        minutes after the new now.
        """
        tight_window = _get_tight_window()

        now0 = datetime.datetime(2026, 5, 17, 23, 58, 58, tzinfo=datetime.UTC)
        clock = _FakeClock(now0)
        dt_patcher, time_patcher = _install_fake_clock(clock)

        with dt_patcher, time_patcher:
            start_str, end_str = tight_window(10)
            now_after = clock.now(datetime.UTC)

        # Sleep advanced clock past midnight.
        assert now_after.day == 18, (
            f"Expected sleep to advance past midnight, but now is "
            f"{now_after.isoformat()}"
        )
        # Window is now on day 18, fitting after now_after.
        end_min = _parse_hhmm(end_str)
        now_after_min = now_after.hour * 60 + now_after.minute
        remaining_min = end_min - now_after_min
        assert remaining_min >= 8, (
            f"After midnight sleep, _tight_window(10) returned "
            f"({start_str!r}, {end_str!r}) with {remaining_min} min "
            f"remaining after now={now_after.hour:02d}:{now_after.minute:02d} "
            f"— needs >= 8."
        )
