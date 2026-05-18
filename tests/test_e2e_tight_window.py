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


def _patch_now(target: datetime.datetime) -> Any:
    """Patch ``datetime.datetime.now`` inside ``tests.e2e.test_e2e``.

    Returns the patcher; caller uses ``with`` or ``.start()/.stop()``.
    The helper calls ``datetime.datetime.now(tz=datetime.UTC)``.  We
    can't subclass ``datetime.datetime`` (mypy rejects the dynamic
    base) — instead, patch the *attribute* ``test_e2e.datetime`` (the
    module's bound name) with a stand-in that has a ``now`` classmethod
    and a ``UTC`` constant.  The helper only uses
    ``datetime.datetime.now(tz=datetime.UTC)``, so a small stand-in
    suffices.
    """
    from tests.e2e import test_e2e  # noqa: PLC0415

    _utc = datetime.UTC

    class _StandinDatetime:
        @staticmethod
        def now(tz: Any = None) -> datetime.datetime:
            if tz is None:
                return target.replace(tzinfo=None)
            return target.astimezone(tz)

    _stand_dt = _StandinDatetime

    class _StandinModule:
        datetime = _stand_dt
        UTC = _utc

    return patch.object(test_e2e, "datetime", _StandinModule)


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
        """``end - now`` must be >= ``minutes - 2`` minutes."""
        tight_window = _get_tight_window()

        h, m = (int(x) for x in hhmm.split(":"))
        # Use a fixed date so midnight maths are deterministic.  Pick a
        # weekday far from any DST transition (UTC has none, but
        # belt-and-braces).
        now = datetime.datetime(2026, 5, 17, h, m, 0, tzinfo=datetime.UTC)

        with _patch_now(now):
            start_str, end_str = tight_window(minutes)

        end_min = _parse_hhmm(end_str)
        now_min = h * 60 + m

        # Compute remaining minutes accounting for possible midnight
        # rollover (helper may return tomorrow's window).
        if end_min >= now_min:
            remaining_min = end_min - now_min
        else:
            remaining_min = (24 * 60 - now_min) + end_min

        # The helper documents "starting ~2 min before now" so we lose
        # up to 2 minutes of remaining duration vs the requested
        # ``minutes`` due to the backshift.  Anything less than that
        # means the window is unusable for an ``end``-bound test.
        min_acceptable_remaining = minutes - 2

        assert remaining_min >= min_acceptable_remaining, (
            f"_tight_window({minutes}) at now={hhmm} returned "
            f"({start_str!r}, {end_str!r}) with only {remaining_min} "
            f"minutes remaining after now — needs >= "
            f"{min_acceptable_remaining}.  A test that calls a service "
            f"and waits for state transitions will hit "
            f"_on_timer_expire and see the session cancelled to "
            f"'idle' before the wait_for_state completes."
        )

    def test_window_does_not_cross_midnight(self) -> None:
        """C-009: end_str's date must equal start_str's date.  The
        helper uses HH:MM:00 strings only (no date), so this is
        encoded as ``end_min >= start_min`` — i.e. the helper must
        not return values where ``end`` would naturally fall on the
        next day.
        """
        tight_window = _get_tight_window()

        # Test the late-night band where naïve arithmetic would cross.
        for hh in range(20, 24):
            for mm in (0, 15, 30, 45, 58, 59):
                now = datetime.datetime(2026, 5, 17, hh, mm, 0, tzinfo=datetime.UTC)
                with _patch_now(now):
                    start_str, end_str = tight_window(10)
                start_min = _parse_hhmm(start_str)
                end_min = _parse_hhmm(end_str)
                assert end_min > start_min, (
                    f"At now={hh:02d}:{mm:02d}, tight_window(10) returned "
                    f"start={start_str}, end={end_str} — end <= start "
                    f"would mean the helper produced a midnight-crossing "
                    f"window, violating C-009."
                )
