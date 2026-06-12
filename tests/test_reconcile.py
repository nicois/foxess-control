"""Unit tests for the brand-agnostic work-mode reconciliation helper.

The helper decides whether the inverter's reported work mode diverges
from what the integration commanded, tolerating a propagation grace
window.  Pure function — no HA, no brand adapter (C-040).
"""

from __future__ import annotations

import datetime

from smart_battery.reconcile import ReconcileVerdict, reconcile_commanded_mode

_T0 = datetime.datetime(2026, 6, 12, 1, 30, 0, tzinfo=datetime.UTC)
_GRACE = datetime.timedelta(minutes=6)


def _at(seconds: float) -> datetime.datetime:
    return _T0 + datetime.timedelta(seconds=seconds)


class TestReconcileCommandedMode:
    def test_nothing_commanded_is_ok(self) -> None:
        v = reconcile_commanded_mode(None, _T0, "SelfUse", _at(600), _GRACE)
        assert v is ReconcileVerdict.OK

    def test_match_is_ok(self) -> None:
        v = reconcile_commanded_mode(
            "ForceCharge", _T0, "ForceCharge", _at(600), _GRACE
        )
        assert v is ReconcileVerdict.OK

    def test_mismatch_within_grace_is_within_grace(self) -> None:
        # Commanded ForceCharge 100 s ago, still reports SelfUse — inside the
        # 6-min grace, so not yet a conflict (propagation lag tolerated).
        v = reconcile_commanded_mode("ForceCharge", _T0, "SelfUse", _at(100), _GRACE)
        assert v is ReconcileVerdict.WITHIN_GRACE

    def test_mismatch_past_grace_is_conflict(self) -> None:
        # Commanded ForceCharge, still reports SelfUse 7 min later — conflict
        # (the issue-#11 "override not applied" direction).
        v = reconcile_commanded_mode("ForceCharge", _T0, "SelfUse", _at(420), _GRACE)
        assert v is ReconcileVerdict.CONFLICT

    def test_override_not_removed_past_grace_is_conflict(self) -> None:
        # Commanded removal (expect SelfUse) but inverter still ForceCharge —
        # the issue-#11 "ran to 100%" direction.
        v = reconcile_commanded_mode("SelfUse", _T0, "ForceCharge", _at(420), _GRACE)
        assert v is ReconcileVerdict.CONFLICT

    def test_reported_none_treated_as_self_use(self) -> None:
        # No enabled group → get_current_mode returns None → SelfUse.
        # Commanded SelfUse, reports None: OK.
        v = reconcile_commanded_mode("SelfUse", _T0, None, _at(420), _GRACE)
        assert v is ReconcileVerdict.OK
        # Commanded ForceCharge, reports None past grace: conflict.
        v2 = reconcile_commanded_mode("ForceCharge", _T0, None, _at(420), _GRACE)
        assert v2 is ReconcileVerdict.CONFLICT

    def test_exact_grace_boundary_is_within_grace(self) -> None:
        # now - commanded_at == grace exactly → still within grace (strict >).
        v = reconcile_commanded_mode("ForceCharge", _T0, "SelfUse", _at(360), _GRACE)
        assert v is ReconcileVerdict.WITHIN_GRACE
