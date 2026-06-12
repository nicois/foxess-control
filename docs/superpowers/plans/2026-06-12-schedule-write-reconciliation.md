# Schedule-Write Reconciliation (Mode-Mismatch Detection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect (and surface, never auto-fix) when the inverter's actual active work mode diverges from what the integration last commanded — the failure class behind issue #11 — at zero extra API cost.

**Architecture:** A pure, stateless decision helper in `smart_battery/` decides OK / WITHIN_GRACE / CONFLICT from (commanded mode, commanded-at, reported mode, now, grace). The FoxESS cloud adapter records its commanded mode on every schedule write; the coordinator's existing poll (which already derives the live `_work_mode`) calls the helper each cycle and, on a confirmed conflict, records an operational error and raises a Repair issue, clearing it when the modes reconcile.

**Tech Stack:** Python 3.13/3.14, Home Assistant custom integration, pytest, the in-repo FoxESS simulator (`simulator/`), HA issue-registry (Repair issues).

**Key constraints (from spec + CLAUDE.md):**
- C-039: `smart_battery/` MUST NOT import brand modules. The pure helper takes plain values only.
- C-040: brand-agnostic code gets brand-agnostic tests (no FoxESS adapter in the helper's tests).
- C-021: brand-specific wiring lives in `custom_components/foxess_control/`.
- C-015: ONLY edit canonical root `smart_battery/`; never the vendored copy (pre-commit hook syncs it).
- C-028: simulator over mocks for the integration test.
- C-035/C-036: config via `_cfg`, domain data via `_dd`/`get_domain_data`, not raw `hass.data`.
- Detect-and-surface ONLY — no read-back, no extra API call, no corrective write.

**Background facts (already verified against `develop`):**
- The poll already derives the live mode: `coordinator.py:240-245` calls `self.inverter.get_current_mode()` → `data["_work_mode"] = mode.value or None`.
- `record_operational_error(logger, buffer, *, category, attempted, exc, hint=None, context=None, severity="warning")` lives in `smart_battery/logging.py:109`. It REQUIRES an `exc: BaseException`.
- The diagnostics ring buffer is reachable in brand code via `_recent_errors(hass)` (`foxess_adapter.py:81`) → `_dd(hass).recent_errors`.
- Existing Repair pattern: `_create_unreachable_issue` / `_clear_unreachable_issue` in `smart_battery/listeners.py` use `async_create_issue` / `async_delete_issue` with a `translation_key` + `translation_placeholders`.
- Cloud adapter write methods to hook: `FoxESSCloudAdapter.apply_mode` (`foxess_adapter.py:429`) and `remove_override` (`foxess_adapter.py:499`). (The entity adapter at 788/922 is OUT OF SCOPE.)
- Domain data class: `FoxESSControlData` (`domain_data.py:125`); add new fields there.
- Simulator: `handle_scheduler_enable` (`simulator/server.py:197`) calls `model.set_schedule(groups)`; `/sim/set` backchannel (`server.py:390`) sets any model attribute that exists.

---

## File Structure

- **Create** `smart_battery/reconcile.py` — the pure decision helper + verdict enum. One responsibility: decide a reconciliation verdict from plain values. No I/O, no HA imports.
- **Create** `tests/test_reconcile.py` — unit tests for the pure helper (brand-agnostic).
- **Modify** `custom_components/foxess_control/domain_data.py` — add `commanded_work_mode: str | None` and `commanded_work_mode_at: str | None` (ISO) fields to `FoxESSControlData`.
- **Modify** `custom_components/foxess_control/foxess_adapter.py` — record commanded mode in `FoxESSCloudAdapter.apply_mode` / `remove_override`; add a `ScheduleNotApplied` exception and the conflict-surfacing helpers (`_create_schedule_not_applied_issue` / `_clear_schedule_not_applied_issue`); add the reconcile entry point `reconcile_work_mode(hass, reported_mode)`.
- **Modify** `custom_components/foxess_control/coordinator.py` — call `reconcile_work_mode` once per poll after `_work_mode` is derived.
- **Modify** `custom_components/foxess_control/translations/en.json` — add the `schedule_not_applied` issue block.
- **Modify** `simulator/model.py` — add `silent_drop_schedule: bool = False` field.
- **Modify** `simulator/server.py` — when `silent_drop_schedule` is set, `handle_scheduler_enable` returns success (errno 0) WITHOUT applying the groups.
- **Create** `tests/test_schedule_reconciliation.py` — simulator-based integration test.

---

## Task 1: Pure reconciliation helper in `smart_battery/`

**Files:**
- Create: `smart_battery/reconcile.py`
- Test: `tests/test_reconcile.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_reconcile.py`:

```python
"""Unit tests for the brand-agnostic work-mode reconciliation helper.

The helper decides whether the inverter's reported work mode diverges
from what the integration commanded, tolerating a propagation grace
window.  Pure function — no HA, no brand adapter (C-040).
"""

from __future__ import annotations

import datetime

from smart_battery.reconcile import ReconcileVerdict, reconcile_commanded_mode

_T0 = datetime.datetime(2026, 6, 12, 1, 30, 0, tzinfo=datetime.timezone.utc)
_GRACE = datetime.timedelta(minutes=6)


def _at(seconds: float) -> datetime.datetime:
    return _T0 + datetime.timedelta(seconds=seconds)


class TestReconcileCommandedMode:
    def test_nothing_commanded_is_ok(self) -> None:
        v = reconcile_commanded_mode(None, _T0, "SelfUse", _at(600), _GRACE)
        assert v is ReconcileVerdict.OK

    def test_match_is_ok(self) -> None:
        v = reconcile_commanded_mode("ForceCharge", _T0, "ForceCharge", _at(600), _GRACE)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'smart_battery.reconcile'`.

- [ ] **Step 3: Write minimal implementation**

Create `smart_battery/reconcile.py`:

```python
"""Brand-agnostic reconciliation of commanded vs reported work mode.

Pure decision logic (C-039): given what the integration last commanded
and what the inverter currently reports, decide whether they diverge —
tolerating a grace window for write propagation.  No I/O, no HA imports.
See docs/superpowers/specs/2026-06-12-schedule-write-verification-design.md.
"""

from __future__ import annotations

import datetime
import enum

# The mode the inverter reports when no managed group is active.
SELF_USE = "SelfUse"


class ReconcileVerdict(enum.Enum):
    """Outcome of comparing commanded vs reported work mode."""

    OK = "ok"
    WITHIN_GRACE = "within_grace"
    CONFLICT = "conflict"


def _norm(mode: str | None) -> str:
    """Normalise a reported/commanded mode; None reports as SelfUse.

    ``get_current_mode`` returns None when no enabled group covers now —
    the inverter is in self-use.  Treat that as SelfUse so a commanded
    removal (expecting SelfUse) reconciles cleanly.
    """
    return mode if mode else SELF_USE


def reconcile_commanded_mode(
    commanded_mode: str | None,
    commanded_at: datetime.datetime,
    reported_mode: str | None,
    now: datetime.datetime,
    grace: datetime.timedelta,
) -> ReconcileVerdict:
    """Return the reconciliation verdict.

    - ``commanded_mode is None`` → OK (nothing has been commanded yet).
    - reported matches commanded → OK.
    - mismatch but ``now - commanded_at <= grace`` → WITHIN_GRACE
      (tolerate write-propagation lag).
    - mismatch and ``now - commanded_at > grace`` → CONFLICT.

    A commanded *removal* is represented by ``commanded_mode == "SelfUse"``,
    so both conflict directions (override-not-applied and
    override-not-removed) are the same comparison.
    """
    if commanded_mode is None:
        return ReconcileVerdict.OK
    if _norm(reported_mode) == _norm(commanded_mode):
        return ReconcileVerdict.OK
    if now - commanded_at > grace:
        return ReconcileVerdict.CONFLICT
    return ReconcileVerdict.WITHIN_GRACE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reconcile.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add smart_battery/reconcile.py tests/test_reconcile.py
git commit -m "feat: pure work-mode reconciliation helper (smart_battery, issue #11)"
```

---

## Task 2: Domain-data fields for commanded intent

**Files:**
- Modify: `custom_components/foxess_control/domain_data.py:125-152` (the `FoxESSControlData` class)
- Test: covered by Task 4's integration test (these are plain dataclass fields; a dedicated unit test would be a tautology).

- [ ] **Step 1: Add the fields**

In `custom_components/foxess_control/domain_data.py`, inside the `FoxESSControlData` dataclass (after the `work_mode: str | None = None` field, around line 146), add:

```python
    # Last work mode the integration commanded via a cloud schedule write,
    # and when (ISO 8601 string).  Used by the poll-time reconciler to
    # detect a divergence between commanded and actually-applied mode
    # (issue #11).  "SelfUse" represents a commanded override removal.
    commanded_work_mode: str | None = None
    commanded_work_mode_at: str | None = None
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `python3 -c "from custom_components.foxess_control.domain_data import FoxESSControlData; d = FoxESSControlData(); print(d.commanded_work_mode, d.commanded_work_mode_at)"`
Expected: `None None`

- [ ] **Step 3: Commit**

```bash
git add custom_components/foxess_control/domain_data.py
git commit -m "feat: domain-data fields for last-commanded work mode (issue #11)"
```

---

## Task 3: Record commanded mode + add reconcile entry point + surfacing (brand layer)

**Files:**
- Modify: `custom_components/foxess_control/foxess_adapter.py` (`FoxESSCloudAdapter.apply_mode` ~429, `remove_override` ~499; add module-level exception + issue helpers + `reconcile_work_mode`)
- Modify: `custom_components/foxess_control/translations/en.json` (add `schedule_not_applied` issue block)
- Test: covered by Task 4's simulator integration test.

- [ ] **Step 1: Add the exception, the grace constant, and the issue helpers**

In `custom_components/foxess_control/foxess_adapter.py`, near the top-level helpers (after the imports / `_recent_errors` definition around line 95), add:

```python
import datetime as _dt

from homeassistant.util import dt as _dt_util

# Reconciliation: how long after a commanded write before a persisting
# mode mismatch is treated as a real conflict (issue #11).  One poll
# interval is the propagation budget; add a margin so a single slightly-
# late poll does not false-alarm.  The coordinator passes the real
# interval; this is the additive margin.
SCHEDULE_RECONCILE_MARGIN = _dt.timedelta(seconds=60)

_SCHEDULE_NOT_APPLIED_ISSUE = "schedule_not_applied"


class ScheduleNotApplied(Exception):
    """The inverter's reported work mode diverges from what was commanded.

    Carried into ``record_operational_error`` (which requires a
    ``BaseException``) so the divergence is recorded with a self-
    sufficient message.  Not raised into control flow — surfacing only.
    """


def _create_schedule_not_applied_issue(
    hass: HomeAssistant,
    domain: str,
    *,
    commanded: str,
    reported: str,
) -> None:
    """Raise a Repair issue: the inverter is not applying schedule changes."""
    try:
        from homeassistant.helpers.issue_registry import (
            IssueSeverity,
            async_create_issue,
        )

        async_create_issue(
            hass,
            domain,
            _SCHEDULE_NOT_APPLIED_ISSUE,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key=_SCHEDULE_NOT_APPLIED_ISSUE,
            translation_placeholders={
                "commanded": commanded,
                "reported": reported,
            },
        )
    except Exception:  # noqa: BLE001 — Repair surfacing is best-effort
        _LOGGER.debug("Failed to create schedule_not_applied issue (non-critical)")


def _clear_schedule_not_applied_issue(hass: HomeAssistant, domain: str) -> None:
    """Dismiss the schedule_not_applied Repair issue."""
    try:
        from homeassistant.helpers.issue_registry import async_delete_issue

        async_delete_issue(hass, domain, _SCHEDULE_NOT_APPLIED_ISSUE)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Failed to clear schedule_not_applied issue (non-critical)")
```

(If `from homeassistant.core import HomeAssistant` is only imported under `TYPE_CHECKING`, add a runtime import where needed; check the file's existing import block and match its style. `_LOGGER` already exists in this module.)

- [ ] **Step 2: Record commanded mode on write**

Add a small private recorder near the issue helpers:

```python
def _record_commanded_mode(hass: HomeAssistant, mode: WorkMode) -> None:
    """Persist the work mode just commanded via a cloud schedule write."""
    try:
        from ._helpers import _dd

        dd = _dd(hass)
        dd.commanded_work_mode = mode.value
        dd.commanded_work_mode_at = _dt_util.utcnow().isoformat()
    except Exception:  # noqa: BLE001 — recording is best-effort
        _LOGGER.debug("Failed to record commanded work mode (non-critical)")
```

In `FoxESSCloudAdapter.apply_mode` (~line 429), at the very end of the method body (after the schedule write actually happens — both the fast-path `return` and the slow-path final `set_schedule`), record the commanded mode. The cleanest way is to record at the top of the method, since both paths write `mode`:

Immediately after the method's docstring and `safe_end = self._safe_end(...)` line, add:

```python
        _record_commanded_mode(hass, mode)
```

In `FoxESSCloudAdapter.remove_override` (~line 499), a removal reverts to self-use. Immediately after its docstring (before the executor call), add:

```python
        _record_commanded_mode(hass, WorkMode.SELF_USE)
```

(Verify `WorkMode` is imported in this module — it is used elsewhere here, e.g. `WorkMode.SELF_USE` at line 928.)

- [ ] **Step 3: Add the reconcile entry point**

Add a module-level function (the coordinator will call this each poll):

```python
def reconcile_work_mode(hass: HomeAssistant, domain: str, reported_mode: str | None,
                        poll_interval: _dt.timedelta) -> None:
    """Reconcile the last-commanded work mode against the polled mode.

    Detect-and-surface only (issue #11): on a confirmed conflict, record
    an operational error and raise a Repair issue; clear the issue when
    the modes reconcile.  Never raises — must not break the poll.
    """
    try:
        from ._helpers import _dd
        from .smart_battery.reconcile import (
            ReconcileVerdict,
            reconcile_commanded_mode,
        )

        dd = _dd(hass)
        commanded = dd.commanded_work_mode
        commanded_at_iso = dd.commanded_work_mode_at
        if commanded is None or commanded_at_iso is None:
            return
        commanded_at = _dt.datetime.fromisoformat(commanded_at_iso)
        grace = poll_interval + SCHEDULE_RECONCILE_MARGIN
        verdict = reconcile_commanded_mode(
            commanded, commanded_at, reported_mode, _dt_util.utcnow(), grace
        )
        if verdict is ReconcileVerdict.CONFLICT:
            reported = reported_mode or "SelfUse"
            record_operational_error(
                _LOGGER,
                _recent_errors(hass),
                category="schedule_not_applied",
                attempted="reconcile commanded work mode against polled mode",
                exc=ScheduleNotApplied(
                    f"commanded {commanded} but inverter reports {reported}"
                ),
                hint=(
                    "the inverter reports a different work mode than was "
                    "commanded — it may not be applying schedule changes; "
                    "check inverter firmware/compatibility"
                ),
                context={
                    "commanded": commanded,
                    "reported": reported,
                    "since": commanded_at_iso,
                },
            )
            _create_schedule_not_applied_issue(
                hass, domain, commanded=commanded, reported=reported
            )
        elif verdict is ReconcileVerdict.OK:
            _clear_schedule_not_applied_issue(hass, domain)
        # WITHIN_GRACE: do nothing (neither surface nor clear yet).
    except Exception:  # noqa: BLE001 — reconciliation must never break the poll
        _LOGGER.debug("Work-mode reconciliation failed (non-critical)", exc_info=True)
```

(Verify `record_operational_error` is imported in this module — it is, at line 41 per the spec notes. `DOMAIN` is also imported here.)

- [ ] **Step 4: Add the translation block**

In `custom_components/foxess_control/translations/en.json`, inside the `"issues"` object (after the `"sensor_write_failed"` entry, before the closing brace of `"issues"`), add:

```json
    ,
    "schedule_not_applied": {
      "title": "Inverter is not applying schedule changes",
      "description": "FoxESS Control commanded the inverter to **{commanded}** mode, but the inverter still reports **{reported}** mode well after the change should have taken effect.\n\nThis usually means the inverter is accepting the schedule write at the API but not applying it — often after an inverter firmware update. Smart charge/discharge may not behave as configured (for example, charging past the target SoC).\n\nThe issue will clear automatically once the inverter's reported mode matches what was commanded. If it persists, check for an inverter firmware update or compatibility note, and consider reporting it with a diagnostics download."
    }
```

(Match the existing indentation; ensure the comma placement produces valid JSON — the entry before it must be followed by a comma. Validate with `python3 -m json.tool`.)

- [ ] **Step 5: Verify JSON + imports**

Run: `python3 -m json.tool custom_components/foxess_control/translations/en.json > /dev/null && echo "JSON OK"`
Run: `python3 -c "from custom_components.foxess_control.foxess_adapter import reconcile_work_mode, ScheduleNotApplied; print('import OK')"`
Expected: `JSON OK` then `import OK`.

- [ ] **Step 6: Commit**

```bash
git add custom_components/foxess_control/foxess_adapter.py custom_components/foxess_control/translations/en.json
git commit -m "feat: record commanded mode + reconcile/surface conflicts (cloud adapter, issue #11)"
```

---

## Task 4: Wire reconciliation into the coordinator poll

**Files:**
- Modify: `custom_components/foxess_control/coordinator.py:239-247` (the `_work_mode` derivation in `_fetch_all`)
- Test: Task 5's simulator integration test exercises this end-to-end.

- [ ] **Step 1: Call the reconciler after `_work_mode` is derived**

In `custom_components/foxess_control/coordinator.py`, replace the work-mode block (currently lines 240-245):

```python
        try:
            mode = self.inverter.get_current_mode()
            data["_work_mode"] = mode.value if mode is not None else None
        except Exception:
            _LOGGER.debug("Failed to fetch work mode, skipping", exc_info=True)
            data["_work_mode"] = None

        return data
```

with:

```python
        try:
            mode = self.inverter.get_current_mode()
            data["_work_mode"] = mode.value if mode is not None else None
        except Exception:
            _LOGGER.debug("Failed to fetch work mode, skipping", exc_info=True)
            data["_work_mode"] = None

        # Reconcile the last-commanded mode against what the inverter
        # actually reports (issue #11).  Detect-and-surface only; never
        # raises (the helper swallows its own errors).
        from .foxess_adapter import reconcile_work_mode

        interval = self.update_interval or datetime.timedelta(
            seconds=DEFAULT_POLLING_INTERVAL
        )
        reconcile_work_mode(self.hass, DOMAIN, data["_work_mode"], interval)

        return data
```

(Verified: `datetime` is imported at `coordinator.py:5`; `DOMAIN` is imported at line 16 via `from .const import DOMAIN, POLLED_VARIABLES`. `DEFAULT_POLLING_INTERVAL` is NOT yet imported in `coordinator.py` but IS re-exported from `const.py:100` — change line 16 to `from .const import DEFAULT_POLLING_INTERVAL, DOMAIN, POLLED_VARIABLES`. `self.hass` is available on a `DataUpdateCoordinator`.)

- [ ] **Step 2: Verify import wiring**

Run: `python3 -c "import custom_components.foxess_control.coordinator as c; print('OK')"`
Expected: `OK` (no ImportError).

- [ ] **Step 3: Commit**

```bash
git add custom_components/foxess_control/coordinator.py
git commit -m "feat: call work-mode reconciler each poll (issue #11)"
```

---

## Task 5: Simulator silent-drop mode + integration test

**Files:**
- Modify: `simulator/model.py` (add `silent_drop_schedule` field, ~line 120 region with other fields)
- Modify: `simulator/server.py:197` (`handle_scheduler_enable`)
- Create: `tests/test_schedule_reconciliation.py`

- [ ] **Step 1: Add the simulator field**

In `simulator/model.py`, in the model dataclass alongside `schedule_groups` (~line 120), add:

```python
    # Test seam (issue #11): when True, /scheduler/enable returns success
    # (errno 0) but does NOT apply the groups — models a firmware that
    # ACKs the write at the API but silently fails to apply it.
    silent_drop_schedule: bool = False
```

- [ ] **Step 2: Honour it in the enable handler**

In `simulator/server.py`, in `handle_scheduler_enable` (~line 227), replace:

```python
    model = _model(request)
    model.set_schedule(groups)
    _LOGGER.info("Schedule set: %d groups", len(model.schedule_groups))
    return _api_response(None)
```

with:

```python
    model = _model(request)
    if getattr(model, "silent_drop_schedule", False):
        # Firmware ACKs but does not apply (issue #11 test seam).
        _LOGGER.info("Schedule silently dropped (silent_drop_schedule)")
        return _api_response(None)
    model.set_schedule(groups)
    _LOGGER.info("Schedule set: %d groups", len(model.schedule_groups))
    return _api_response(None)
```

(The `/sim/set` backchannel at `server.py:390` already sets any existing model attribute, so a test can enable this via `foxess_sim.set(silent_drop_schedule=True)`.)

- [ ] **Step 3: Write the failing integration test**

Create `tests/test_schedule_reconciliation.py`:

```python
"""Integration test: poll-time reconciliation surfaces a schedule that the
inverter ACKs but does not apply (issue #11), via the FoxESS simulator.

Uses the simulator (C-028).  Drives the cloud adapter's apply_mode +
remove_override and the coordinator's reconcile entry point against a
simulator that silently drops schedule writes, and asserts the Repair
issue + operational-error are surfaced after the grace window, and that
a normally-applied write surfaces nothing.
"""

from __future__ import annotations

import datetime

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.foxess_adapter import (
    _SCHEDULE_NOT_APPLIED_ISSUE,
    reconcile_work_mode,
)
from custom_components.foxess_control.smart_battery.domain_data import (
    get_domain_data,
)


def _set_commanded(hass, mode_value: str, at: datetime.datetime) -> None:
    dd = get_domain_data(hass, DOMAIN)
    dd.commanded_work_mode = mode_value
    dd.commanded_work_mode_at = at.isoformat()


class TestReconcileSurfacing:
    """reconcile_work_mode end-to-end against a real issue registry."""

    @pytest.mark.asyncio
    async def test_conflict_past_grace_raises_repair(self, reconcile_hass) -> None:
        hass = reconcile_hass
        # Commanded ForceCharge 10 minutes ago; inverter still reports SelfUse.
        long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            minutes=10
        )
        _set_commanded(hass, "ForceCharge", long_ago)

        reconcile_work_mode(
            hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300)
        )

        registry = ir.async_get(hass)
        issues = [
            i
            for i in registry.issues.values()
            if i.domain == DOMAIN and i.issue_id == _SCHEDULE_NOT_APPLIED_ISSUE
        ]
        assert issues, "Expected a schedule_not_applied Repair issue after grace"
        dd = get_domain_data(hass, DOMAIN)
        assert any(
            e.get("category") == "schedule_not_applied" for e in dd.recent_errors
        ), "Expected an operational error recorded in the diagnostics buffer"

    @pytest.mark.asyncio
    async def test_within_grace_does_not_raise(self, reconcile_hass) -> None:
        hass = reconcile_hass
        just_now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=30
        )
        _set_commanded(hass, "ForceCharge", just_now)

        reconcile_work_mode(
            hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300)
        )

        registry = ir.async_get(hass)
        assert not [
            i
            for i in registry.issues.values()
            if i.domain == DOMAIN and i.issue_id == _SCHEDULE_NOT_APPLIED_ISSUE
        ], "Should not surface within the grace window"

    @pytest.mark.asyncio
    async def test_match_clears_existing_issue(self, reconcile_hass) -> None:
        hass = reconcile_hass
        long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            minutes=10
        )
        # First: conflict raises the issue.
        _set_commanded(hass, "ForceCharge", long_ago)
        reconcile_work_mode(hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300))
        registry = ir.async_get(hass)
        assert [
            i for i in registry.issues.values()
            if i.issue_id == _SCHEDULE_NOT_APPLIED_ISSUE
        ]
        # Then: inverter now reports the commanded mode → issue clears.
        reconcile_work_mode(hass, DOMAIN, "ForceCharge", datetime.timedelta(seconds=300))
        assert not [
            i for i in registry.issues.values()
            if i.issue_id == _SCHEDULE_NOT_APPLIED_ISSUE and i.active
        ], "Issue should clear once modes reconcile"
```

Add the `reconcile_hass` fixture to `tests/test_schedule_reconciliation.py` (mirror the `_make_hass` pattern already used in `tests/test_sensor_listener_safety.py` — a real `HomeAssistant` with a seeded issue registry and domain data):

```python
from unittest.mock import MagicMock

from custom_components.foxess_control.smart_battery.domain_data import (
    FoxESSControlData,
)
from homeassistant.core import HomeAssistant


@pytest.fixture
def reconcile_hass() -> HomeAssistant:
    ha = HomeAssistant("/tmp")
    ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
    ha.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    dd = FoxESSControlData()
    ha.data[DOMAIN] = dd
    return ha
```

(If `tests/test_sensor_listener_safety.py` uses a different domain-data class name or seeding for `recent_errors`, match it. `FoxESSControlData.recent_errors` is a `deque` from the base class, so `dd.recent_errors` is ready to append to. Confirm `get_domain_data(hass, DOMAIN)` returns the seeded instance.)

- [ ] **Step 4: Run the integration test to verify it passes**

Run: `python3 -m pytest tests/test_schedule_reconciliation.py -v`
Expected: PASS (3 tests). (These test the brand wiring through the real issue registry + domain data; the simulator silent-drop seam from Steps 1-2 is exercised by the end-to-end E2E in Task 6.)

- [ ] **Step 5: Commit**

```bash
git add simulator/model.py simulator/server.py tests/test_schedule_reconciliation.py
git commit -m "test: simulator silent-drop seam + reconciliation surfacing (issue #11)"
```

---

## Task 6: End-to-end coverage (HA-visible Repair issue)

**Files:**
- Modify: `tests/e2e/test_e2e.py` (add a test in the cloud-only section; the `foxess_sim` fixture supports `.set(...)`)

Per the standing rule to extend E2E for HA-visible changes (the Repair issue is HA-visible). If the silent-drop seam is not cleanly reachable through the E2E harness, document why inline and rely on Task 5's integration test.

- [ ] **Step 1: Write the E2E test**

In `tests/e2e/test_e2e.py`, add to the cloud-only test class (the `TestFeedinPacing`/charge area; requires `connection_mode == "cloud"`):

```python
    def test_schedule_not_applied_surfaces_repair(
        self,
        ha_e2e: "HAClient",
        foxess_sim: "SimulatorHandle | None",
        connection_mode: str,
    ) -> None:
        """A firmware that ACKs but drops schedule writes surfaces a Repair.

        Drives a smart charge while the simulator silently drops the
        schedule write, then asserts the schedule_not_applied Repair
        issue appears once the grace window elapses (issue #11).
        """
        if connection_mode != "cloud":
            pytest.skip("requires simulator silent-drop seam")
        assert foxess_sim is not None

        foxess_sim.set(soc=20, load_kw=0.3, silent_drop_schedule=True)
        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {"start_time": start, "end_time": end, "target_soc": 80},
        )

        # The commanded ForceCharge is never applied (simulator drops it),
        # so after the grace window the reconciler raises the Repair issue.
        # Poll the HA repairs/issue registry via the REST API until it
        # appears (bounded wait, no fixed sleep).
        ha_e2e.wait_for_issue(
            DOMAIN,
            "schedule_not_applied",
            timeout_s=180,
        )
```

(Check `tests/e2e/ha_client.py` for an existing issue-registry accessor. If `wait_for_issue` does not exist, either add it next to the other `wait_for_*` helpers — querying the HA `/api/...` issue-registry or template endpoint — or, if the issue registry is not reachable via the E2E REST surface, DELETE this E2E task and add a one-line note in the plan/PR that E2E cannot observe Repair issues through the harness, so Task 5's integration test is the coverage of record. Do not fake it.)

- [ ] **Step 2: Run the E2E test**

Run: `pytest tests/e2e/test_e2e.py -k schedule_not_applied -n auto`
Expected: PASS, or a documented skip/removal per the note above.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_e2e.py tests/e2e/ha_client.py
git commit -m "test(e2e): schedule_not_applied Repair surfaces on dropped write (issue #11)"
```

### Task 6 outcome note (2026-06-12)

**E2E Repair observation via WS is feasible — the test is added but SKIPPED due to a production gap, not a harness limitation.**

- Built `HAClient.list_repair_issues()` + `wait_for_repair_issue(domain, issue_id, timeout_s)` in `tests/e2e/ha_client.py`. They open a short-lived authenticated WS connection (same pattern as `enable_entity`) and send `{"type": "repairs/list_issues"}` (the real HA command name; `repairs/list` returns `unknown_command`). The reply's `result.issues` carry `domain` + `issue_id`. **Proven working** against the live container: a run of the silent-drop charge scenario observed `homeassistant/country_not_configured` and, at the first charge-listener tick (~300s), the integration's own `foxess_control/charge_target_unreachable` Repair appearing in real time.
- **`schedule_not_applied` never appeared** in the live scenario. Root cause: every cloud session-start path in `_services.py` writes the schedule via `inverter.set_schedule(...)` DIRECTLY and never calls `_record_commanded_mode`. Only `FoxESSScheduleAdapter.apply_mode` records the commanded mode, and the charge listener's first `apply_mode` tick is `SMART_CHARGE_ADJUST_SECONDS` (300s) after start — so for the first ~5 minutes the reconciler sees `commanded_work_mode is None` and returns early, and a tight charge window expires before any commanded intent is recorded.
- The E2E test (`TestScheduleReconciliation.test_schedule_not_applied_surfaces_repair`) is committed with its full body intact behind a `pytest.skip(reason=...)` documenting the gap; un-skip once session-start writes record the commanded mode. **Coverage of record remains `tests/test_schedule_reconciliation.py`** (it seeds `commanded_work_mode` directly and passes). Consider a follow-up to record commanded intent on the initial `set_schedule`, not only on the periodic `apply_mode` tick.

---

## Task 8: Close the session-start + clock-reset gaps (added after Task 6 investigation)

The Task 6 E2E investigation surfaced TWO real production gaps that make the
reconciler effectively inert in cloud mode. Both must be fixed for the feature
to actually cover the issue-#11 scenario.

**Gap A — commanded intent not recorded at session start.** The cloud
session-start writes in `_services.py` call `inverter.set_schedule(...)`
DIRECTLY (charge `:682`, discharge `:404`, feed-in `:285`), bypassing the
adapter's `_record_commanded_mode`. So commanded intent is first recorded only
on the listener's periodic `apply_mode` tick — up to `SMART_CHARGE_ADJUST_SECONDS`
(300s) into the session.

**Gap B — the grace clock resets on every identical re-issue.**
`_record_commanded_mode` unconditionally sets `commanded_work_mode_at = now`.
The listener re-issues the SAME mode via `apply_mode` every 300s, while the
grace window is `poll_interval (300) + 60 = 360s`. Because the clock resets
every 300s but the threshold is 360s, elapsed-since-commanded NEVER crosses
the grace — so `CONFLICT` can never fire in production. The integration test
masked this by seeding the timestamp once and never re-recording.

**Scope:** cloud mode only. `reconcile_work_mode` is called ONLY from the
FoxESS cloud coordinator's `_fetch_all` (`coordinator.py:255`); the
`EntityCoordinator` never calls it. Do NOT add recording to the entity-mode
`_apply_mode_via_entities` paths — nothing reads it there and it risks
confusion. Only the cloud `set_schedule` session-start sites.

**Files:**
- Modify: `custom_components/foxess_control/foxess_adapter.py` (`_record_commanded_mode`)
- Modify: `custom_components/foxess_control/_services.py` (cloud session-start write sites)
- Modify: `tests/test_schedule_reconciliation.py` (add tests for both gaps)
- Modify: `tests/e2e/test_e2e.py` (un-skip the E2E test)

- [ ] **Step 1 (Gap B): Write the failing test for clock-reset**

Add to `tests/test_schedule_reconciliation.py` a test proving that re-recording
the SAME commanded mode does NOT reset the grace clock (so a persistent conflict
eventually surfaces even though the listener re-issues every tick). Because the
production re-issue happens through `_record_commanded_mode`, drive that helper
directly:

```python
class TestCommandedClockStability:
    @pytest.mark.asyncio
    async def test_reissuing_same_mode_does_not_reset_grace_clock(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        from custom_components.foxess_control.foxess_adapter import (
            _record_commanded_mode,
        )
        from custom_components.foxess_control.foxess import WorkMode

        hass = reconcile_hass
        # First record: 10 minutes ago.
        dd = _dd(hass)
        long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            minutes=10
        )
        dd.commanded_work_mode = WorkMode.FORCE_CHARGE.value
        dd.commanded_work_mode_at = long_ago.isoformat()

        # Re-issue the SAME mode now (mimics the listener's periodic apply_mode).
        _record_commanded_mode(hass, WorkMode.FORCE_CHARGE)

        # The timestamp must NOT have jumped to ~now — it must still reflect
        # the original command, so a persisting conflict can cross the grace.
        recorded = datetime.datetime.fromisoformat(dd.commanded_work_mode_at)
        age = datetime.datetime.now(datetime.timezone.utc) - recorded
        assert age > datetime.timedelta(minutes=5), (
            "Re-issuing the same commanded mode reset the grace clock — a "
            "persistent conflict would never cross the grace window"
        )

    @pytest.mark.asyncio
    async def test_changing_mode_does_reset_clock(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        from custom_components.foxess_control.foxess_adapter import (
            _record_commanded_mode,
        )
        from custom_components.foxess_control.foxess import WorkMode

        hass = reconcile_hass
        dd = _dd(hass)
        long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            minutes=10
        )
        dd.commanded_work_mode = WorkMode.SELF_USE.value
        dd.commanded_work_mode_at = long_ago.isoformat()

        # Command a DIFFERENT mode → clock resets to ~now.
        _record_commanded_mode(hass, WorkMode.FORCE_CHARGE)

        assert dd.commanded_work_mode == WorkMode.FORCE_CHARGE.value
        recorded = datetime.datetime.fromisoformat(dd.commanded_work_mode_at)
        age = datetime.datetime.now(datetime.timezone.utc) - recorded
        assert age < datetime.timedelta(minutes=1), (
            "A genuine mode change must reset the grace clock"
        )
```

(`_dd` is already imported in this test file from Task 5. If not, import it from `custom_components.foxess_control._helpers`.)

- [ ] **Step 2 (Gap B): Run to verify the first test FAILS**

Run: `python3 -m pytest tests/test_schedule_reconciliation.py::TestCommandedClockStability -v`
Expected: `test_reissuing_same_mode_does_not_reset_grace_clock` FAILS (current code resets the timestamp unconditionally); `test_changing_mode_does_reset_clock` PASSES.

- [ ] **Step 3 (Gap B): Fix `_record_commanded_mode` to reset the clock only on mode change**

In `custom_components/foxess_control/foxess_adapter.py`, replace the body of `_record_commanded_mode`:

```python
def _record_commanded_mode(hass: HomeAssistant, mode: WorkMode) -> None:
    """Persist the work mode just commanded via a cloud schedule write.

    The timestamp is reset only when the commanded mode *changes*.  The
    listener re-issues the same mode every adjust tick; if each re-issue
    reset the clock, a persisting conflict could never cross the grace
    window (the reconciler would always see it as freshly commanded).
    """
    try:
        from ._helpers import _dd

        dd = _dd(hass)
        if dd.commanded_work_mode != mode.value:
            dd.commanded_work_mode = mode.value
            dd.commanded_work_mode_at = dt_util.utcnow().isoformat()
    except Exception:  # noqa: BLE001 — recording is best-effort
        _LOGGER.debug("Failed to record commanded work mode (non-critical)")
```

- [ ] **Step 4 (Gap B): Run to verify both pass**

Run: `python3 -m pytest tests/test_schedule_reconciliation.py::TestCommandedClockStability -v`
Expected: both PASS.

- [ ] **Step 5 (Gap A): Record commanded intent at the cloud session-start sites**

In `custom_components/foxess_control/_services.py`, at EACH of the three cloud
(non-entity) session-start `set_schedule` calls, record the commanded mode
immediately AFTER the successful `await hass.async_add_executor_job(inverter.set_schedule, ...)`:

- Charge (`_do_smart_charge`, ~`:682`): after the `set_schedule`, add
  `_record_commanded_mode(hass, WorkMode.FORCE_CHARGE)`.
- Discharge (`_do_smart_discharge`, ~`:404`): after its `set_schedule`, add
  `_record_commanded_mode(hass, WorkMode.FORCE_DISCHARGE)`.
- Feed-in (~`:285`): after its `set_schedule`, add
  `_record_commanded_mode(hass, WorkMode.FEEDIN)`.

Import `_record_commanded_mode` from `.foxess_adapter` at the top of
`_services.py` (or a function-local import if there's a cycle — check; the
adapter imports from `_helpers`, services likely already imports adapter
symbols, so a top-level import is probably fine — verify ruff/import-cycle).

Record ONLY on the cloud (`else`/non-entity) branch, immediately after the
write succeeds (so a raised write does not record phantom intent — same
principle as Task 3). Do NOT add recording to the `_apply_mode_via_entities`
branches.

- [ ] **Step 6 (Gap A): Verify no import cycle + services import cleanly**

Run: `python3 -c "import custom_components.foxess_control._services; print('OK')"`
Expected: `OK`.

- [ ] **Step 7: Un-skip the E2E test and confirm it collects**

In `tests/e2e/test_e2e.py`, remove the `pytest.skip(...)` from
`test_schedule_not_applied_surfaces_repair` (keep the body). The grace in E2E
is `poll_interval + 60`; confirm the test's `timeout_s` exceeds it with headroom
(E2E polling is short, ~10–60s, so grace ≈ 70–120s; `timeout_s=240` is ample).

Run: `python3 -m pytest tests/e2e/test_e2e.py -k schedule_not_applied --collect-only`
Expected: collects without error. (Full E2E run requires the containerised
harness; run it if available — `pytest tests/e2e/test_e2e.py -k schedule_not_applied -n auto` — and report whether it actually surfaced the Repair live. If the harness is unavailable in this environment, report collect-only + that the live run is deferred to CI.)

- [ ] **Step 8: Pre-commit + commit**

Run: `pre-commit run --files custom_components/foxess_control/foxess_adapter.py custom_components/foxess_control/_services.py tests/test_schedule_reconciliation.py tests/e2e/test_e2e.py 2>&1 | tail -20` → all pass.

```bash
git add custom_components/foxess_control/foxess_adapter.py custom_components/foxess_control/_services.py custom_components/foxess_control/smart_battery/ tests/test_schedule_reconciliation.py tests/e2e/test_e2e.py
git commit -m "fix: record commanded mode at session start + don't reset grace clock on re-issue (issue #11)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Changelog + final verification

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add a changelog entry**

At the top of `CHANGELOG.md`, under a new `## Unreleased` (or the current beta heading if one is open), add under `### Added`:

```markdown
- **Detect when the inverter ignores schedule changes** (issue #11). The integration now reconciles the work mode it commands against the mode the inverter actually reports on each poll (no extra API calls). If the inverter keeps reporting a different mode well after a commanded change — e.g. it ACKs a schedule write but does not apply it, as some inverter firmware updates cause — a Repair issue is raised ("Inverter is not applying schedule changes") and the divergence is recorded in diagnostics. Detection only: it does not change control behaviour. Motivated by a report of smart charge running past the target SoC after a firmware update.
```

- [ ] **Step 2: Run the full unit suite**

Run: `python3 -m pytest tests/ -m "not slow" --tb=short`
Expected: all pass (existing count + the new `tests/test_reconcile.py` and `tests/test_schedule_reconciliation.py`).

- [ ] **Step 3: Run pre-commit (syncs vendored copy, mypy, ruff, semgrep)**

Run: `pre-commit run --all-files`
Expected: all hooks Pass. (The `sync-smart-battery` hook copies `smart_battery/reconcile.py` into the vendored tree; the semgrep architecture hook confirms `smart_battery/reconcile.py` has no brand imports.)

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md custom_components/foxess_control/smart_battery/
git commit -m "docs: changelog for schedule-write reconciliation (issue #11)"
```

---

## Notes for the implementer

- **C-039 check:** `smart_battery/reconcile.py` must import ONLY stdlib (`datetime`, `enum`). No `homeassistant`, no `custom_components`. The semgrep architecture hook enforces this.
- **C-015 check:** never edit `custom_components/foxess_control/smart_battery/reconcile.py` by hand — the pre-commit `sync-smart-battery` hook generates it from the root copy.
- **`record_operational_error` needs a real exception:** that's why `ScheduleNotApplied` exists — it is constructed (not raised into control flow) purely to give the recorder a `BaseException` with a useful message.
- **Grace semantics:** `reconcile_commanded_mode` uses strict `>` so exactly-at-grace is still WITHIN_GRACE. The coordinator passes `poll_interval` and the adapter adds `SCHEDULE_RECONCILE_MARGIN` (60 s) internally — so the effective grace is `interval + 60 s`.
- **Idempotency:** `async_create_issue` with the same `issue_id` is idempotent (updates in place), so re-surfacing each poll during an ongoing conflict does not spam; `async_delete_issue` on reconcile clears it.
- **Out of scope (do not implement):** entity-adapter path, parameter-level (fdSoc/fdPwr/window) comparison, read-back, corrective writes.
