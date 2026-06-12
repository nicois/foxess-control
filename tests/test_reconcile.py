"""Unit tests for the brand-agnostic work-mode reconciliation helper.

The helper decides whether the inverter's reported work mode diverges
from what the integration commanded, tolerating a propagation grace
window.  Pure function — no HA, no brand adapter (C-040).

The intent is a ``(kind, watched_mode)`` pair:

- APPLY: an override was applied — conflict when the inverter does *not*
  report the watched mode.
- REMOVE: an override was removed — conflict when the inverter *still*
  reports the watched (removed) mode; any other mode is OK.
"""

from __future__ import annotations

import datetime

from smart_battery.reconcile import (
    CommandKind,
    ReconcileVerdict,
    reconcile_commanded_mode,
)

_T0 = datetime.datetime(2026, 6, 12, 1, 30, 0, tzinfo=datetime.UTC)
_GRACE = datetime.timedelta(minutes=6)


def _at(seconds: float) -> datetime.datetime:
    return _T0 + datetime.timedelta(seconds=seconds)


class TestReconcileApply:
    def test_nothing_commanded_is_ok(self) -> None:
        v = reconcile_commanded_mode(
            CommandKind.APPLY, None, _T0, "SelfUse", _at(600), _GRACE
        )
        assert v is ReconcileVerdict.OK

    def test_match_is_ok(self) -> None:
        v = reconcile_commanded_mode(
            CommandKind.APPLY, "ForceCharge", _T0, "ForceCharge", _at(600), _GRACE
        )
        assert v is ReconcileVerdict.OK

    def test_mismatch_within_grace_is_within_grace(self) -> None:
        # Applied ForceCharge 100 s ago, still reports SelfUse — inside the
        # 6-min grace, so not yet a conflict (propagation lag tolerated).
        v = reconcile_commanded_mode(
            CommandKind.APPLY, "ForceCharge", _T0, "SelfUse", _at(100), _GRACE
        )
        assert v is ReconcileVerdict.WITHIN_GRACE

    def test_mismatch_past_grace_is_conflict(self) -> None:
        # Applied ForceCharge, still reports SelfUse 7 min later — conflict
        # (the issue-#11 "override not applied" direction).
        v = reconcile_commanded_mode(
            CommandKind.APPLY, "ForceCharge", _T0, "SelfUse", _at(420), _GRACE
        )
        assert v is ReconcileVerdict.CONFLICT

    def test_reported_none_treated_as_self_use(self) -> None:
        # No enabled group → get_current_mode returns None → SelfUse.
        # Applied ForceCharge, reports None past grace: conflict.
        v = reconcile_commanded_mode(
            CommandKind.APPLY, "ForceCharge", _T0, None, _at(420), _GRACE
        )
        assert v is ReconcileVerdict.CONFLICT

    def test_exact_grace_boundary_is_within_grace(self) -> None:
        # now - commanded_at == grace exactly → still within grace (strict >).
        v = reconcile_commanded_mode(
            CommandKind.APPLY, "ForceCharge", _T0, "SelfUse", _at(360), _GRACE
        )
        assert v is ReconcileVerdict.WITHIN_GRACE


class TestReconcileRemove:
    def test_removed_mode_no_longer_reported_is_ok(self) -> None:
        # Removed ForceCharge, inverter now reports SelfUse past grace → OK.
        v = reconcile_commanded_mode(
            CommandKind.REMOVE, "ForceCharge", _T0, "SelfUse", _at(420), _GRACE
        )
        assert v is ReconcileVerdict.OK

    def test_removed_mode_reports_none_is_ok(self) -> None:
        # None → SelfUse; removed ForceCharge, reports None → OK.
        v = reconcile_commanded_mode(
            CommandKind.REMOVE, "ForceCharge", _T0, None, _at(420), _GRACE
        )
        assert v is ReconcileVerdict.OK

    def test_removed_mode_unrelated_managed_group_is_ok(self) -> None:
        # The false-positive bug case: removed ForceCharge, but a standalone
        # user Feed-in group makes the inverter report "Feedin".  That is an
        # unrelated managed mode, NOT the removed mode → OK (no conflict).
        v = reconcile_commanded_mode(
            CommandKind.REMOVE, "ForceCharge", _T0, "Feedin", _at(420), _GRACE
        )
        assert v is ReconcileVerdict.OK

    def test_removed_mode_still_reported_past_grace_is_conflict(self) -> None:
        # Genuine issue-#11 direction: removed ForceCharge but the inverter
        # STILL reports ForceCharge past grace → conflict (removal not applied).
        v = reconcile_commanded_mode(
            CommandKind.REMOVE, "ForceCharge", _T0, "ForceCharge", _at(420), _GRACE
        )
        assert v is ReconcileVerdict.CONFLICT

    def test_removed_mode_still_reported_within_grace(self) -> None:
        # Removed ForceCharge, still ForceCharge but within grace → tolerate.
        v = reconcile_commanded_mode(
            CommandKind.REMOVE, "ForceCharge", _T0, "ForceCharge", _at(100), _GRACE
        )
        assert v is ReconcileVerdict.WITHIN_GRACE
