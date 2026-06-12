"""Integration test: poll-time reconciliation surfaces a schedule that the
inverter ACKs but does not apply (issue #11).

Exercises the brand wiring (reconcile_work_mode) against a real HA issue
registry and real domain data — not mocks.  The simulator silent-drop
seam (Part A) is exercised end-to-end by the E2E test in a later task;
here we drive reconcile_work_mode directly with controlled commanded
intent + reported mode + grace.
"""

from __future__ import annotations

import datetime
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.foxess_control._helpers import _dd
from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.coordinator import FoxESSDataCoordinator
from custom_components.foxess_control.domain_data import FoxESSControlData
from custom_components.foxess_control.foxess import WorkMode
from custom_components.foxess_control.foxess.inverter import Inverter
from custom_components.foxess_control.foxess_adapter import (
    _SCHEDULE_NOT_APPLIED_ISSUE,
    _record_commanded_mode,
    reconcile_work_mode,
)
from custom_components.foxess_control.smart_battery.reconcile import CommandKind

if TYPE_CHECKING:
    from homeassistant.helpers.issue_registry import IssueEntry


@pytest_asyncio.fixture  # type: ignore[untyped-decorator]
async def reconcile_hass() -> HomeAssistant:
    # HomeAssistant() captures the running event loop in __init__, so it
    # must be built inside an async context (mirrors the harness used by
    # tests/test_sensor_listener_safety.py).
    ha = HomeAssistant("/tmp")
    ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
    ha.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    ha.data[DOMAIN] = FoxESSControlData()
    return ha


def _set_commanded(
    hass: HomeAssistant,
    mode_value: str,
    at: datetime.datetime,
    kind: str = "apply",
) -> None:
    dd = _dd(hass)
    dd.commanded_work_mode = mode_value
    dd.commanded_work_mode_at = at.isoformat()
    dd.commanded_kind = kind


def _issues(hass: HomeAssistant) -> list[IssueEntry]:
    registry = ir.async_get(hass)
    return [
        i
        for i in registry.issues.values()
        if i.domain == DOMAIN and i.issue_id == _SCHEDULE_NOT_APPLIED_ISSUE
    ]


class TestReconcileSurfacing:
    @pytest.mark.asyncio
    async def test_conflict_past_grace_raises_repair(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, "ForceCharge", long_ago)

        reconcile_work_mode(hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300))

        assert _issues(hass), "Expected a schedule_not_applied Repair after grace"
        dd = _dd(hass)
        assert any(
            e.get("category") == "schedule_not_applied" for e in dd.recent_errors
        ), "Expected a schedule_not_applied operational error in the diagnostics buffer"

    @pytest.mark.asyncio
    async def test_within_grace_does_not_raise(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        just_now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=30)
        _set_commanded(hass, "ForceCharge", just_now)

        reconcile_work_mode(hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300))

        assert not _issues(hass), "Should not surface within the grace window"

    @pytest.mark.asyncio
    async def test_match_clears_existing_issue(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, "ForceCharge", long_ago)
        reconcile_work_mode(hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300))
        assert _issues(hass), "pre-condition: conflict raised the issue"

        # Inverter now reports the commanded mode → issue clears.
        reconcile_work_mode(
            hass, DOMAIN, "ForceCharge", datetime.timedelta(seconds=300)
        )
        assert not _issues(hass), "Issue should clear once modes reconcile"


class TestReconcileRemoveKind:
    """Removal watches the REMOVED mode (false-positive Repair fix).

    When an override is removed, the recorded intent is kind="remove" with
    the removed mode as the watched mode.  A conflict is only the inverter
    STILL reporting that mode — not it reporting an unrelated managed group
    (e.g. a standalone Feed-in schedule), which previously raised a spurious
    schedule_not_applied Repair that never self-healed.
    """

    @pytest.mark.asyncio
    async def test_removed_force_charge_reports_feedin_no_repair(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        # Regression for the false positive: removed ForceCharge, but a
        # standalone user Feed-in group makes get_current_mode report
        # "Feedin".  Past grace, this must NOT raise a Repair.
        hass = reconcile_hass
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, WorkMode.FORCE_CHARGE.value, long_ago, kind="remove")

        reconcile_work_mode(hass, DOMAIN, "Feedin", datetime.timedelta(seconds=300))

        assert not _issues(hass), (
            "Removing ForceCharge while a standalone Feed-in group is active "
            "must not raise a spurious schedule_not_applied Repair"
        )
        dd = _dd(hass)
        assert not any(
            e.get("category") == "schedule_not_applied" for e in dd.recent_errors
        ), "Should not record a schedule_not_applied operational error"

    @pytest.mark.asyncio
    async def test_removed_force_charge_still_force_charge_raises_repair(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        # Genuine issue-#11 case preserved: removed ForceCharge but the
        # inverter STILL reports ForceCharge past grace → real conflict.
        hass = reconcile_hass
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, WorkMode.FORCE_CHARGE.value, long_ago, kind="remove")

        reconcile_work_mode(
            hass, DOMAIN, "ForceCharge", datetime.timedelta(seconds=300)
        )

        assert _issues(hass), (
            "A removed ForceCharge that the inverter still reports is a "
            "genuine conflict and must raise a Repair"
        )
        dd = _dd(hass)
        assert any(
            e.get("category") == "schedule_not_applied" for e in dd.recent_errors
        ), "Expected a schedule_not_applied operational error"

    @pytest.mark.asyncio
    async def test_removed_force_charge_reports_self_use_no_repair(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, WorkMode.FORCE_CHARGE.value, long_ago, kind="remove")

        reconcile_work_mode(hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300))

        assert not _issues(hass), "Removed mode no longer reported → no conflict"

    @pytest.mark.asyncio
    async def test_record_commanded_mode_remove_writes_watched_mode(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        # The production recorder must store the REMOVED mode as the watched
        # mode with kind="remove" — not "SelfUse".
        hass = reconcile_hass
        _record_commanded_mode(hass, WorkMode.FORCE_CHARGE, kind=CommandKind.REMOVE)

        dd = _dd(hass)
        assert dd.commanded_work_mode == WorkMode.FORCE_CHARGE.value
        assert dd.commanded_kind == "remove"
        assert dd.commanded_work_mode_at is not None


class TestCommandedClockStability:
    @pytest.mark.asyncio
    async def test_reissuing_same_mode_does_not_reset_grace_clock(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        dd = _dd(hass)
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, WorkMode.FORCE_CHARGE.value, long_ago)

        # Re-issue the SAME mode now (mimics the listener's periodic apply_mode).
        _record_commanded_mode(hass, WorkMode.FORCE_CHARGE)

        assert dd.commanded_work_mode_at is not None
        recorded = datetime.datetime.fromisoformat(dd.commanded_work_mode_at)
        age = datetime.datetime.now(datetime.UTC) - recorded
        assert age > datetime.timedelta(minutes=5), (
            "Re-issuing the same commanded mode reset the grace clock — a "
            "persistent conflict would never cross the grace window"
        )

    @pytest.mark.asyncio
    async def test_changing_mode_does_reset_clock(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        dd = _dd(hass)
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, WorkMode.SELF_USE.value, long_ago)

        _record_commanded_mode(hass, WorkMode.FORCE_CHARGE)

        assert dd.commanded_work_mode == WorkMode.FORCE_CHARGE.value
        assert dd.commanded_work_mode_at is not None
        recorded = datetime.datetime.fromisoformat(dd.commanded_work_mode_at)
        age = datetime.datetime.now(datetime.UTC) - recorded
        assert age < datetime.timedelta(minutes=1), (
            "A genuine mode change must reset the grace clock"
        )

    @pytest.mark.asyncio
    async def test_changing_kind_does_reset_clock(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        # Applying then removing the SAME mode is a new expectation: the
        # kind change (apply → remove) must reset the grace clock.
        hass = reconcile_hass
        dd = _dd(hass)
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, WorkMode.FORCE_CHARGE.value, long_ago, kind="apply")

        _record_commanded_mode(hass, WorkMode.FORCE_CHARGE, kind=CommandKind.REMOVE)

        assert dd.commanded_kind == "remove"
        assert dd.commanded_work_mode_at is not None
        recorded = datetime.datetime.fromisoformat(dd.commanded_work_mode_at)
        age = datetime.datetime.now(datetime.UTC) - recorded
        assert age < datetime.timedelta(minutes=1), (
            "Apply→remove of the same mode is a new expectation and must "
            "reset the grace clock"
        )


class TestReconcileRunsOnEventLoop:
    """Issue #11: the reconcile call MUST run on the event loop, not in the
    executor thread that runs ``_fetch_all``.

    Root cause of the silent no-op: ``reconcile_work_mode`` was called from
    inside ``FoxESSDataCoordinator._fetch_all``, which the coordinator
    dispatches via ``await hass.async_add_executor_job(self._fetch_all)`` —
    i.e. in a worker thread.  ``reconcile_work_mode`` may create/delete an HA
    Repair issue (IssueRegistry.async_get_or_create / async_delete), and those
    call ``hass.verify_event_loop_thread(...)``, which RAISES off the loop.
    The exception is swallowed by ``reconcile_work_mode``'s broad ``except``
    (correctly — it must never break the poll), so the Repair is NEVER
    created in the live integration.

    This test exercises the real threading: a real ``HomeAssistant`` (real
    executor + real IssueRegistry), with ``verify_event_loop_thread`` LEFT
    INTACT (NOT stubbed — the stub is what hid the bug in the other tests
    here).  ``_fetch_all`` is replaced with a stub that records which thread
    it runs on and returns a mismatching ``_work_mode``.  We then drive the
    coordinator's real ``_async_update_data`` and assert a Repair issue IS
    created.

    Before the fix (reconcile inside ``_fetch_all`` / executor thread): the
    issue-registry call raises and is swallowed → NO issue → FAIL.
    After the fix (reconcile on the loop in ``_async_update_data``): the
    issue is created → PASS.
    """

    @pytest_asyncio.fixture  # type: ignore[untyped-decorator]
    async def real_loop_hass(self) -> HomeAssistant:
        # NOTE: deliberately does NOT stub verify_event_loop_thread — that
        # stub is exactly what masks this bug.  A real IssueRegistry +
        # real executor are required to reproduce the executor-vs-loop split.
        ha = HomeAssistant("/tmp")
        ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
        ha.data[DOMAIN] = FoxESSControlData()
        return ha

    @pytest.mark.asyncio
    async def test_conflict_surfaces_repair_through_coordinator_flow(
        self, real_loop_hass: HomeAssistant
    ) -> None:
        hass = real_loop_hass
        loop_thread_id = threading.get_ident()

        # Seed a commanded intent that is past the grace window so the poll
        # produces a genuine CONFLICT verdict.
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        dd = _dd(hass)
        dd.commanded_work_mode = WorkMode.FORCE_CHARGE.value
        dd.commanded_work_mode_at = long_ago.isoformat()
        dd.commanded_kind = "apply"

        with patch("homeassistant.helpers.frame.report_usage"):
            coord = FoxESSDataCoordinator(
                hass, MagicMock(spec=Inverter), update_interval_seconds=300
            )

        # Replace _fetch_all with a stub that (a) records the thread it runs
        # on — proving it executes off the event loop in a real worker thread
        # — and (b) returns a work mode that conflicts with the commanded
        # ForceCharge.  Keep it a plain (sync) function so the coordinator
        # dispatches it via the REAL async_add_executor_job.
        fetch_thread_ids: list[int] = []

        def _fake_fetch_all() -> dict[str, Any]:
            fetch_thread_ids.append(threading.get_ident())
            return {"SoC": 50.0, "_work_mode": WorkMode.SELF_USE.value}

        coord._fetch_all = _fake_fetch_all  # type: ignore[method-assign]

        try:
            await coord._async_update_data()
        finally:
            await hass.async_stop()

        # Sanity: prove the threading reality this test depends on — _fetch_all
        # genuinely ran in a worker thread, NOT on the event loop.  If this
        # ever fails, the test is no longer exercising the bug.
        assert fetch_thread_ids, "_fetch_all should have been invoked"
        assert fetch_thread_ids[0] != loop_thread_id, (
            "_fetch_all must run in an executor (worker) thread, not on the "
            "event loop — otherwise this test does not exercise the bug"
        )

        # The reconcile must have created the Repair.  It can only do so if it
        # ran on the event loop (verify_event_loop_thread is intact); if it ran
        # in the executor thread (the bug) the issue-registry call raised and
        # was swallowed, leaving no issue.
        registry = ir.async_get(hass)
        issues = [
            i
            for i in registry.issues.values()
            if i.domain == DOMAIN and i.issue_id == _SCHEDULE_NOT_APPLIED_ISSUE
        ]
        assert issues, (
            "Expected a schedule_not_applied Repair to be created via the "
            "coordinator's _async_update_data flow.  If absent, reconcile_work_mode "
            "ran in the executor thread (inside _fetch_all) where the HA issue "
            "registry refuses to mutate off the event loop (issue #11)."
        )

    @pytest.mark.asyncio
    async def test_reconcile_in_executor_thread_is_a_silent_noop(
        self, real_loop_hass: HomeAssistant
    ) -> None:
        """Directly demonstrate the failure mode: calling reconcile_work_mode
        from a worker thread (as the old _fetch_all did) creates NO issue,
        whereas calling it on the loop DOES.

        This pins the root cause independently of the coordinator wiring:
        the difference is purely which thread the call runs on.
        """
        hass = real_loop_hass
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        dd = _dd(hass)
        dd.commanded_work_mode = WorkMode.FORCE_CHARGE.value
        dd.commanded_work_mode_at = long_ago.isoformat()
        dd.commanded_kind = "apply"

        def _issues() -> list[IssueEntry]:
            registry = ir.async_get(hass)
            return [
                i
                for i in registry.issues.values()
                if i.domain == DOMAIN and i.issue_id == _SCHEDULE_NOT_APPLIED_ISSUE
            ]

        try:
            # Off-loop (the bug): reconcile swallows the loop-affinity error,
            # so NO issue is created.
            await hass.async_add_executor_job(
                reconcile_work_mode,
                hass,
                DOMAIN,
                WorkMode.SELF_USE.value,
                datetime.timedelta(seconds=300),
            )
            assert not _issues(), (
                "Reconciling from a worker thread must NOT create the Repair — "
                "the issue registry refuses to mutate off the event loop and the "
                "error is swallowed (this is the issue-#11 silent no-op)"
            )

            # On the loop (the fix): the issue IS created.
            reconcile_work_mode(
                hass, DOMAIN, WorkMode.SELF_USE.value, datetime.timedelta(seconds=300)
            )
            assert _issues(), "Reconciling on the event loop must create the Repair"
        finally:
            await hass.async_stop()
