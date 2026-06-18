# Startup Schedule-Reconcile + Schedule-in-Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On every cloud-backend startup, read the live inverter schedule and remove any orphaned managed work-mode group (ForceCharge/ForceDischarge/Feedin) that no recovered session covers — surfaced loudly — and add the live-schedule snapshot + reconcile outcome to the diagnostics download.

**Architecture:** A new `_reconcile_schedule(hass, inverter)` runs in `async_setup_entry` immediately after the existing `_recover_sessions(...)`, reads `inverter.get_schedule()`, decides which enabled managed groups are uncovered by the now-populated `smart_charge_state`/`smart_discharge_state`, and removes them via the existing `_remove_mode_from_schedule` (respecting the C-018 unmanaged-mode guard). It caches the fetched snapshot + outcome on `FoxESSControlData`; the diagnostics platform reports that cache. All brand-layer, cloud-only, startup-only.

**Tech Stack:** Python 3.13/3.14, Home Assistant custom integration, pytest, the in-repo FoxESS simulator (`simulator/`).

**Key constraints (spec + CLAUDE.md):**
- **C-018**: refuse to modify schedule when unmanaged modes (e.g. Backup) are present — reconcile must NOT write in that case, only surface.
- **C-026 / C-020**: removal surfaced via `record_operational_error` (diagnostics ring buffer) + loud log.
- **C-025**: this enforces session-boundary cleanliness across the restart/teardown-failure boundary.
- **C-021**: brand-specific (schedule/diagnostics) — lives in `custom_components/foxess_control/`, NOT `smart_battery/`. No C-039 concern.
- **C-028**: simulator over mocks for tests.
- **C-008/C-009**: unchanged — `set_schedule` (via `_remove_mode_from_schedule`) still enforces them.

**Verified facts about current code (do not re-derive; confirm if unsure):**
- `async_setup_entry` calls `await _recover_sessions(hass, inverter)` at `custom_components/foxess_control/__init__.py:1510`, after `async_forward_entry_setups`. The reconcile call goes immediately after this line.
- After recovery, the live session state lives in `_dd(hass).smart_charge_state` / `.smart_discharge_state` — plain dicts with `start` and `end` **datetime** values and a `target_soc` etc. (set at `__init__.py:544-548` for charge recovery, `656-660` for discharge; `cs.get("start")`/`cs.get("end")` are used by the WS gate at `__init__.py:1037,1055`). `None` when no session.
- `Inverter.get_schedule()` (`foxess/inverter.py:127`) returns `{"enable": int, "groups": [ScheduleGroup, ...]}`; normalises a null API result to `{"enable": 0, "groups": []}`. Blocking — call via `hass.async_add_executor_job`.
- A `ScheduleGroup` dict has: `enable` (int 0/1), `startHour, startMinute, endHour, endMinute` (ints), `workMode` (str), `minSocOnGrid, fdSoc, fdPwr`.
- `_MANAGED_WORK_MODES` (`foxess_adapter.py:58`) = `{SelfUse, ForceCharge, ForceDischarge, Feedin}` (`.value` strings). "Managed override" modes for orphan purposes = these minus `SelfUse`.
- `_is_placeholder(group)` (`foxess_adapter.py:288`) — true for empty/unused API slots; skip these.
- `_remove_mode_from_schedule(inverter, mode: WorkMode, min_soc_on_grid: int)` (`foxess_adapter.py:473`) — removes all groups of `mode`, writes the remainder or reverts to self-use. This is the removal primitive; reuse it.
- `_check_schedule_safe(groups, hass=None)` (`foxess_adapter.py:335`) — raises if any non-placeholder group has a `workMode` NOT in `_MANAGED_WORK_MODES`. Use it (or its predicate logic) to detect the C-018 unmanaged-mode condition.
- `record_operational_error(logger, buffer, *, category, attempted, exc, hint=None, context=None, severity="warning")` is in `smart_battery/logging.py`; the brand ring buffer is `_recent_errors(hass)` in `foxess_adapter.py` (→ `_dd(hass).recent_errors`). It requires an `exc: BaseException`.
- `WorkMode` is importable from `custom_components/foxess_control/foxess` (`from .foxess import WorkMode`).
- `_cfg(hass).entity_mode` (bool) and `_cfg(hass).min_soc_on_grid` (int) are available via `from ._helpers import _cfg`.
- `diagnostics.py` builds a dict and runs `async_redact_data(..., REDACT_KEYS)`; it reads `domain_data = hass.data.get(DOMAIN)` and per-entry `entry_data`.
- Simulator: `model.set_schedule(groups)` seeds groups; `model.get_schedule_response()` serves them (8-slot padded). A test builds an inverter with `Inverter(FoxESSClient("test-api-key", base_url=sim.url), "SIM0001")` and seeds via the `foxess_sim` fixture / its `/sim` backchannel. There is a `silent_drop_schedule` flag (added earlier) for ACK-but-don't-apply.

---

## File Structure

- **Modify** `custom_components/foxess_control/domain_data.py` — add 3 fields to `FoxESSControlData`: `last_schedule_snapshot: list[dict[str, Any]] | None`, `last_schedule_snapshot_at: str | None`, `last_schedule_reconcile: dict[str, Any] | None`.
- **Create** `custom_components/foxess_control/_schedule_reconcile.py` — the reconcile logic: a pure `find_orphan_modes(groups, covered_modes)` decision helper + an async `reconcile_schedule(hass, inverter)` orchestrator (fetch → cache snapshot → decide → C-018 guard → remove → surface). Keeping it in its own module keeps `__init__.py` from growing and gives the pure decision a clean unit-test seam.
- **Modify** `custom_components/foxess_control/__init__.py` — call `await reconcile_schedule(hass, inverter)` after `_recover_sessions` (cloud mode only).
- **Modify** `custom_components/foxess_control/diagnostics.py` — add the `schedule` section from the cached snapshot + outcome.
- **Create** `tests/test_schedule_reconcile.py` — pure-helper unit tests + simulator-backed integration tests.
- **Modify** `tests/test_diagnostics.py` (or create if absent) — assert the `schedule` section.

---

## Task 1: Pure orphan-decision helper

**Files:**
- Create: `custom_components/foxess_control/_schedule_reconcile.py`
- Test: `tests/test_schedule_reconcile.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_schedule_reconcile.py`:

```python
"""Tests for the startup schedule-reconcile (issue-#11 leftover-group defence)."""

from __future__ import annotations

from custom_components.foxess_control._schedule_reconcile import find_orphan_modes


def _g(mode: str, sh: int, eh: int, enable: int = 1) -> dict:
    return {
        "enable": enable,
        "startHour": sh,
        "startMinute": 0,
        "endHour": eh,
        "endMinute": 0,
        "workMode": mode,
        "minSocOnGrid": 11,
        "fdSoc": 100,
        "fdPwr": 10000,
    }


class TestFindOrphanModes:
    def test_enabled_managed_group_with_no_cover_is_orphan(self) -> None:
        groups = [_g("ForceCharge", 11, 14)]
        assert find_orphan_modes(groups, covered_modes=set()) == ["ForceCharge"]

    def test_covered_group_is_not_orphan(self) -> None:
        groups = [_g("ForceCharge", 11, 14)]
        assert find_orphan_modes(groups, covered_modes={"ForceCharge"}) == []

    def test_self_use_is_never_orphan(self) -> None:
        groups = [_g("SelfUse", 0, 23)]
        assert find_orphan_modes(groups, covered_modes=set()) == []

    def test_disabled_group_is_not_orphan(self) -> None:
        groups = [_g("ForceCharge", 11, 14, enable=0)]
        assert find_orphan_modes(groups, covered_modes=set()) == []

    def test_discharge_and_feedin_orphans_detected(self) -> None:
        groups = [_g("ForceDischarge", 17, 20), _g("Feedin", 9, 11)]
        assert sorted(find_orphan_modes(groups, covered_modes=set())) == [
            "Feedin",
            "ForceDischarge",
        ]

    def test_unmanaged_group_is_not_reported_as_orphan(self) -> None:
        # Backup is unmanaged — it is not an orphan we would remove; the
        # C-018 block is handled separately by the orchestrator.
        groups = [_g("Backup", 0, 23)]
        assert find_orphan_modes(groups, covered_modes=set()) == []

    def test_mixed_covered_and_orphan(self) -> None:
        groups = [_g("ForceCharge", 11, 14), _g("ForceDischarge", 17, 20)]
        assert find_orphan_modes(groups, covered_modes={"ForceCharge"}) == [
            "ForceDischarge"
        ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_schedule_reconcile.py::TestFindOrphanModes -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'custom_components.foxess_control._schedule_reconcile'`.

- [ ] **Step 3: Write minimal implementation**

Create `custom_components/foxess_control/_schedule_reconcile.py`:

```python
"""Startup schedule-reconcile: remove orphaned managed schedule groups.

Issue #11 leftover-group defence.  A FoxESS schedule group recurs daily
(no date), so a managed work-mode group (ForceCharge/ForceDischarge/
Feedin) left enabled in the inverter — by a teardown that did not
complete (HA restart mid-session, a write the inverter did not apply, or
a safety-check abort) — re-fires every day until removed.  On startup,
after session recovery, remove any such group that no recovered session
covers.  Cloud backend only; startup only.  See
docs/superpowers/specs/2026-06-18-startup-schedule-reconcile-design.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .foxess.inverter import Inverter

_LOGGER = logging.getLogger(__name__)

# Managed override modes (the managed set minus SelfUse).  An enabled
# group in one of these modes that no session covers is an orphan.
_MANAGED_OVERRIDE_MODES = ("ForceCharge", "ForceDischarge", "Feedin")


def find_orphan_modes(
    groups: list[dict[str, Any]],
    covered_modes: set[str],
) -> list[str]:
    """Return managed-override work modes present as orphaned groups.

    An orphan is an *enabled* group whose ``workMode`` is a managed
    override mode (ForceCharge/ForceDischarge/Feedin) and is NOT in
    *covered_modes* (the modes a recovered/active session legitimately
    owns).  SelfUse, disabled groups, and unmanaged modes (e.g. Backup)
    are never orphans.  Order-preserving, de-duplicated.
    """
    orphans: list[str] = []
    for group in groups:
        if group.get("enable") != 1:
            continue
        mode = group.get("workMode", "")
        if mode not in _MANAGED_OVERRIDE_MODES:
            continue
        if mode in covered_modes:
            continue
        if mode not in orphans:
            orphans.append(mode)
    return orphans
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_schedule_reconcile.py::TestFindOrphanModes -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/_schedule_reconcile.py tests/test_schedule_reconcile.py
git commit --no-gpg-sign -m "feat: pure orphan-mode decision helper for schedule reconcile (issue #11)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

Note: GPG signing times out in this environment — use `--no-gpg-sign` on every commit in this plan.

---

## Task 2: Domain-data snapshot fields

**Files:**
- Modify: `custom_components/foxess_control/domain_data.py` (the `FoxESSControlData` dataclass, ~line 125-152)

- [ ] **Step 1: Add the fields**

In `custom_components/foxess_control/domain_data.py`, inside `FoxESSControlData`, after the `commanded_work_mode_at` fields (added earlier), add:

```python
    # Live inverter schedule captured by the startup reconcile, for the
    # diagnostics download (issue #11).  Snapshot is the groups as fetched;
    # _at is an ISO-8601 UTC timestamp; _reconcile is the outcome record
    # ({action, orphans, detail}).  None until the first startup reconcile.
    last_schedule_snapshot: list[dict[str, Any]] | None = None
    last_schedule_snapshot_at: str | None = None
    last_schedule_reconcile: dict[str, Any] | None = None
```

(`Any` is already imported in this file via `from typing import Any` — confirm; if not, add it.)

- [ ] **Step 2: Verify import**

Run: `python3 -c "from custom_components.foxess_control.domain_data import FoxESSControlData; d=FoxESSControlData(); print(d.last_schedule_snapshot, d.last_schedule_snapshot_at, d.last_schedule_reconcile)"`
Expected: `None None None`

- [ ] **Step 3: Commit**

```bash
git add custom_components/foxess_control/domain_data.py
git commit --no-gpg-sign -m "feat: domain-data fields for schedule snapshot + reconcile outcome (issue #11)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: The async reconcile orchestrator

**Files:**
- Modify: `custom_components/foxess_control/_schedule_reconcile.py` (add `reconcile_schedule`)
- Test: `tests/test_schedule_reconcile.py` (add simulator-backed integration tests)

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_schedule_reconcile.py`:

```python
import datetime

import pytest

from custom_components.foxess_control._schedule_reconcile import reconcile_schedule
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter


def _force_charge_group() -> dict:
    return {
        "enable": 1,
        "startHour": 11,
        "startMinute": 0,
        "endHour": 13,
        "endMinute": 59,
        "workMode": "ForceCharge",
        "minSocOnGrid": 11,
        "fdSoc": 100,
        "fdPwr": 10000,
    }


def _backup_group() -> dict:
    return {
        "enable": 1,
        "startHour": 0,
        "startMinute": 0,
        "endHour": 23,
        "endMinute": 59,
        "workMode": "Backup",
        "minSocOnGrid": 20,
        "fdSoc": 20,
        "fdPwr": 0,
    }


def _enabled_managed(inv: Inverter) -> list[str]:
    """Return the work modes of enabled managed-override groups in the inverter."""
    sched = inv.get_schedule()
    out = []
    for g in sched.get("groups", []):
        if g.get("enable") == 1 and g.get("workMode") in (
            "ForceCharge",
            "ForceDischarge",
            "Feedin",
        ):
            out.append(g["workMode"])
    return out


@pytest.fixture(autouse=True)
def _fast_client():
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


class TestReconcileOrchestrator:
    @pytest.mark.asyncio
    async def test_orphan_force_charge_removed(
        self, foxess_sim, reconcile_hass
    ) -> None:
        # Inverter has a leftover ForceCharge group, integration has no session.
        foxess_sim.model.set_schedule([_force_charge_group()])
        inv = Inverter(FoxESSClient("k", base_url=foxess_sim.url), "SIM0001")
        hass = reconcile_hass  # cloud mode, no smart_charge_state/discharge_state

        await reconcile_schedule(hass, inv)

        assert _enabled_managed(inv) == [], "orphaned ForceCharge should be removed"
        from custom_components.foxess_control._helpers import _dd

        dd = _dd(hass)
        assert any(
            e.get("category") == "orphaned_schedule_removed"
            for e in dd.recent_errors
        ), "removal should be recorded as an operational error"
        assert dd.last_schedule_reconcile["action"] == "removed"

    @pytest.mark.asyncio
    async def test_covered_group_kept(self, foxess_sim, reconcile_hass) -> None:
        foxess_sim.model.set_schedule([_force_charge_group()])
        inv = Inverter(FoxESSClient("k", base_url=foxess_sim.url), "SIM0001")
        hass = reconcile_hass
        # A recovered charge session covers ForceCharge.
        from custom_components.foxess_control._helpers import _dd

        now = datetime.datetime.now(datetime.timezone.utc)
        _dd(hass).smart_charge_state = {
            "start": now,
            "end": now + datetime.timedelta(hours=3),
            "target_soc": 100,
        }

        await reconcile_schedule(hass, inv)

        assert _enabled_managed(inv) == ["ForceCharge"], "covered group must remain"
        assert _dd(hass).last_schedule_reconcile["action"] == "none"

    @pytest.mark.asyncio
    async def test_unmanaged_mode_blocks_removal(
        self, foxess_sim, reconcile_hass
    ) -> None:
        foxess_sim.model.set_schedule([_force_charge_group(), _backup_group()])
        inv = Inverter(FoxESSClient("k", base_url=foxess_sim.url), "SIM0001")
        hass = reconcile_hass

        await reconcile_schedule(hass, inv)

        # C-018: an unmanaged Backup group is present → do NOT write.
        assert "ForceCharge" in _enabled_managed(inv), "must not modify w/ unmanaged"
        from custom_components.foxess_control._helpers import _dd

        assert _dd(hass).last_schedule_reconcile["action"] == "blocked_unmanaged"

    @pytest.mark.asyncio
    async def test_no_orphan_no_write(self, foxess_sim, reconcile_hass) -> None:
        # Schedule already self-use only.
        foxess_sim.model.set_schedule(
            [
                {
                    "enable": 1,
                    "startHour": 0,
                    "startMinute": 0,
                    "endHour": 23,
                    "endMinute": 59,
                    "workMode": "SelfUse",
                    "minSocOnGrid": 11,
                    "fdSoc": 11,
                    "fdPwr": 10000,
                }
            ]
        )
        inv = Inverter(FoxESSClient("k", base_url=foxess_sim.url), "SIM0001")
        hass = reconcile_hass

        await reconcile_schedule(hass, inv)

        from custom_components.foxess_control._helpers import _dd

        assert _dd(hass).last_schedule_reconcile["action"] == "none"
        # snapshot still captured
        assert _dd(hass).last_schedule_snapshot is not None
```

Add the `reconcile_hass` fixture at the top of the test file's integration section. `_cfg(hass)` returns `_dd(hass).config` (an `IntegrationConfig`), and the orchestrator reads `_cfg(hass).entity_mode` and `_cfg(hass).min_soc_on_grid`. `IntegrationConfig` has 10 required positional fields, so construct it fully (cloud mode = `entity_mode=False`):

```python
import pytest
from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    IntegrationConfig,
)


@pytest.fixture
def reconcile_hass():
    """Minimal HA-like holder: only needs hass.data[DOMAIN] for _dd/_cfg.

    The orchestrator uses hass.async_add_executor_job + hass.data; a real
    HomeAssistant is heavier than needed here, but async_add_executor_job
    must work.  Use a real HomeAssistant if the simpler holder cannot run
    executor jobs — mirror tests/test_schedule_reconciliation.py's
    `reconcile_hass` (real HomeAssistant('/tmp'), verify_event_loop_thread
    stubbed) and additionally set dd.config below.
    """
    from unittest.mock import MagicMock
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import issue_registry as ir

    ha = HomeAssistant("/tmp")
    ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
    ha.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    dd = FoxESSControlData()
    dd.config = IntegrationConfig(
        min_soc_on_grid=11,
        api_min_soc=11,
        battery_capacity_kwh=10.0,
        min_power_change=100,
        max_power_w=10000,
        grid_export_limit_w=5000,
        smart_headroom=0.10,
        bms_polling_interval=300.0,
        ws_mode="auto",
        entity_mode=False,
    )
    ha.data[DOMAIN] = dd
    return ha
```

Confirm the `IntegrationConfig` field list against `custom_components/foxess_control/domain_data.py` before relying on it (required positional fields: `min_soc_on_grid, api_min_soc, battery_capacity_kwh, min_power_change, max_power_w, grid_export_limit_w, smart_headroom, bms_polling_interval, ws_mode, entity_mode`; optional: `export_limit_entity`, `additional_pv_power_variable`). For an entity-mode diagnostics test, set `entity_mode=True`. Verify by running the tests; if `HomeAssistant("/tmp")` + `async_add_executor_job` does not run cleanly off-loop, mirror exactly how `tests/test_schedule_reconciliation.py` constructs and drives its async `reconcile_hass` (its tests are `@pytest.mark.asyncio`).

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_schedule_reconcile.py::TestReconcileOrchestrator -v`
Expected: FAIL with `ImportError: cannot import name 'reconcile_schedule'`.

- [ ] **Step 3: Implement `reconcile_schedule`**

Append to `custom_components/foxess_control/_schedule_reconcile.py`:

```python
async def reconcile_schedule(hass: HomeAssistant, inverter: Inverter | None) -> None:
    """Remove orphaned managed schedule groups left in the inverter.

    Runs once on startup, AFTER session recovery, so the live session
    state (``smart_charge_state`` / ``smart_discharge_state``) reflects
    any resumed session.  Cloud backend only.  Never raises — must not
    break integration setup.
    """
    from ._helpers import _cfg, _dd
    from .foxess import WorkMode
    from .foxess_adapter import (
        _MANAGED_WORK_MODES,
        _is_placeholder,
        _recent_errors,
        _remove_mode_from_schedule,
    )
    from .smart_battery.logging import record_operational_error

    try:
        if inverter is None or _cfg(hass).entity_mode:
            return
        dd = _dd(hass)

        # 1. Fetch the live schedule (blocking → executor).
        try:
            schedule = await hass.async_add_executor_job(inverter.get_schedule)
        except Exception as err:  # noqa: BLE001 — must not break setup
            dd.last_schedule_reconcile = {
                "action": "fetch_failed",
                "orphans": [],
                "detail": str(err),
            }
            _LOGGER.warning("Startup schedule reconcile: get_schedule failed: %s", err)
            return

        from homeassistant.util import dt as dt_util

        groups = [g for g in schedule.get("groups", []) if not _is_placeholder(g)]
        # 2. Cache the snapshot regardless of outcome (feature B).
        dd.last_schedule_snapshot = [dict(g) for g in groups]
        dd.last_schedule_snapshot_at = dt_util.utcnow().isoformat()

        # 3. Which managed-override modes does a recovered session cover?
        covered: set[str] = set()
        cs = dd.smart_charge_state
        if cs is not None and _covers(cs, groups, WorkMode.FORCE_CHARGE.value):
            covered.add(WorkMode.FORCE_CHARGE.value)
        ds = dd.smart_discharge_state
        if ds is not None:
            for m in (WorkMode.FORCE_DISCHARGE.value, WorkMode.FEEDIN.value):
                if _covers(ds, groups, m):
                    covered.add(m)

        orphans = find_orphan_modes(groups, covered)
        if not orphans:
            dd.last_schedule_reconcile = {
                "action": "none",
                "orphans": [],
                "detail": "no orphaned managed group",
            }
            return

        # 4. C-018 guard: any UNmanaged group present → do not write.
        has_unmanaged = any(
            g.get("workMode") and g.get("workMode") not in _MANAGED_WORK_MODES
            for g in groups
        )
        if has_unmanaged:
            dd.last_schedule_reconcile = {
                "action": "blocked_unmanaged",
                "orphans": orphans,
                "detail": "unmanaged work-mode group present; schedule not modified",
            }
            _LOGGER.warning(
                "Startup schedule reconcile: found orphaned %s but an unmanaged "
                "mode is present — not modifying the schedule (C-018)",
                orphans,
            )
            record_operational_error(
                _LOGGER,
                _recent_errors(hass),
                category="orphaned_schedule_blocked",
                attempted="startup schedule reconcile",
                exc=_OrphanedSchedule(
                    f"orphaned {orphans} not removed: unmanaged mode present"
                ),
                hint=(
                    "a recurring managed schedule group was left in the inverter "
                    "but an unmanaged mode (e.g. Backup) is also present, so it was "
                    "not removed automatically — remove it via the FoxESS app"
                ),
                context={"orphans": orphans},
            )
            return

        # 5. Remove each orphan via the existing primitive.
        min_soc = _cfg(hass).min_soc_on_grid
        removed: list[str] = []
        for mode_str in orphans:
            try:
                await hass.async_add_executor_job(
                    _remove_mode_from_schedule, inverter, WorkMode(mode_str), min_soc
                )
                removed.append(mode_str)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Startup schedule reconcile: failed to remove %s: %s",
                    mode_str,
                    err,
                )

        dd.last_schedule_reconcile = {
            "action": "removed" if removed else "remove_failed",
            "orphans": orphans,
            "detail": f"removed {removed}",
        }
        if removed:
            _LOGGER.warning(
                "Startup schedule reconcile: removed orphaned %s group(s) left "
                "from a prior session — no active session covers them",
                removed,
            )
            record_operational_error(
                _LOGGER,
                _recent_errors(hass),
                category="orphaned_schedule_removed",
                attempted="startup schedule reconcile",
                exc=_OrphanedSchedule(f"removed orphaned managed group(s): {removed}"),
                hint=(
                    "a recurring managed schedule group was left in the inverter "
                    "with no active session — likely a teardown that did not "
                    "complete (HA restart mid-session, or a write the inverter did "
                    "not apply); it has been removed"
                ),
                context={"removed": removed},
            )
    except Exception:  # noqa: BLE001 — reconcile must never break setup
        _LOGGER.debug("Startup schedule reconcile failed (non-critical)", exc_info=True)


def _covers(session: dict[str, Any], groups: list[dict[str, Any]], mode: str) -> bool:
    """True if *session* legitimately owns a group of *mode* in *groups*.

    A session covers a group when the session's start/end window matches an
    enabled group of that mode (hour+minute).  The session dict carries
    ``start``/``end`` datetimes (set during recovery).
    """
    start = session.get("start")
    end = session.get("end")
    if start is None or end is None:
        # A session exists but without a window — conservatively treat its
        # mode as covered so we never remove a group for an active session.
        return True
    for g in groups:
        if g.get("enable") != 1 or g.get("workMode") != mode:
            continue
        if (
            g.get("startHour") == start.hour
            and g.get("startMinute") == start.minute
            and g.get("endHour") == end.hour
            and g.get("endMinute") == end.minute
        ):
            return True
    return False


class _OrphanedSchedule(Exception):
    """Constructed (not raised into control flow) to give record_operational_error
    a BaseException with a useful message."""
```

(Note: `record_operational_error` requires a `BaseException`, so `_OrphanedSchedule` is constructed for that purpose — same pattern as `ScheduleNotApplied` elsewhere. Confirm `WorkMode.FEEDIN` / `FORCE_DISCHARGE` / `FORCE_CHARGE` `.value` spellings against `foxess/__init__.py`.)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_schedule_reconcile.py -v`
Expected: PASS (7 helper + 4 orchestrator tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/_schedule_reconcile.py tests/test_schedule_reconcile.py
git commit --no-gpg-sign -m "feat: startup schedule-reconcile orchestrator (remove orphaned managed groups, issue #11)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire reconcile into setup

**Files:**
- Modify: `custom_components/foxess_control/__init__.py:1510` (after `_recover_sessions`)
- Test: covered by Task 3's orchestrator tests (the wiring is a one-line call; a full async_setup_entry E2E is Task 6).

- [ ] **Step 1: Add the call**

In `custom_components/foxess_control/__init__.py`, immediately after the `await _recover_sessions(hass, inverter)` line (~1510), add:

```python
    # Remove any orphaned managed schedule group left in the inverter when a
    # prior session's teardown did not complete (issue #11).  Runs after
    # recovery so resumed sessions are reflected.  Cloud-only; never raises.
    from ._schedule_reconcile import reconcile_schedule

    await reconcile_schedule(hass, inverter)
```

(Function-local import keeps any module-load cycle out of `__init__.py`. If `__init__.py` already imports freely from sibling modules at top level and there's no cycle, a top-level import is fine — check and prefer top-level if clean.)

- [ ] **Step 2: Verify import**

Run: `python3 -c "import custom_components.foxess_control as m; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Run the reconcile + recovery tests**

Run: `python3 -m pytest tests/test_schedule_reconcile.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add custom_components/foxess_control/__init__.py
git commit --no-gpg-sign -m "feat: run startup schedule-reconcile after session recovery (issue #11)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Schedule section in diagnostics

**Files:**
- Modify: `custom_components/foxess_control/diagnostics.py`
- Test: `tests/test_diagnostics.py` (create if absent)

- [ ] **Step 1: Write the failing test**

Create/extend `tests/test_diagnostics.py`:

```python
"""Diagnostics: live-schedule snapshot + reconcile outcome (issue #11)."""

from __future__ import annotations

import pytest

from custom_components.foxess_control.diagnostics import _schedule_section
from custom_components.foxess_control.domain_data import FoxESSControlData


class TestScheduleSection:
    def test_reports_cached_snapshot_and_outcome(self) -> None:
        dd = FoxESSControlData()
        dd.last_schedule_snapshot = [
            {"enable": 1, "workMode": "SelfUse", "startHour": 0, "endHour": 23}
        ]
        dd.last_schedule_snapshot_at = "2026-06-18T01:00:00+00:00"
        dd.last_schedule_reconcile = {
            "action": "removed",
            "orphans": ["ForceCharge"],
            "detail": "removed ['ForceCharge']",
        }
        out = _schedule_section(dd, entity_mode=False)
        assert out["as_of"] == "2026-06-18T01:00:00+00:00"
        assert out["groups"][0]["workMode"] == "SelfUse"
        assert out["reconcile"]["action"] == "removed"

    def test_entity_mode_reports_na(self) -> None:
        dd = FoxESSControlData()
        out = _schedule_section(dd, entity_mode=True)
        assert out == "n/a (entity mode)"

    def test_no_snapshot_yet(self) -> None:
        dd = FoxESSControlData()
        out = _schedule_section(dd, entity_mode=False)
        assert out["as_of"] is None
        assert out["groups"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_diagnostics.py::TestScheduleSection -v`
Expected: FAIL with `ImportError: cannot import name '_schedule_section'`.

- [ ] **Step 3: Implement**

In `custom_components/foxess_control/diagnostics.py`, add the helper:

```python
def _schedule_section(
    domain_data: FoxESSControlData, entity_mode: bool
) -> dict[str, Any] | str:
    """Report the live schedule snapshot + reconcile outcome from the cache.

    Sourced from the startup reconcile's cached snapshot (no live API call
    on the diagnostics path).  Entity mode has no cloud schedule.
    """
    if entity_mode:
        return "n/a (entity mode)"
    return {
        "as_of": getattr(domain_data, "last_schedule_snapshot_at", None),
        "groups": getattr(domain_data, "last_schedule_snapshot", None),
        "reconcile": getattr(domain_data, "last_schedule_reconcile", None),
    }
```

Then wire it into `async_get_config_entry_diagnostics`'s returned dict. After the `recent_errors = ...` line, compute entity-mode and add the section to the dict passed to `async_redact_data`:

```python
    from ._helpers import _cfg

    try:
        _entity_mode = _cfg(hass).entity_mode
    except Exception:  # diagnostics must never raise
        _entity_mode = False
```

and add to the returned dict (alongside `"recent_errors": recent_errors,`):

```python
            "schedule": _schedule_section(domain_data, _entity_mode),
```

(The schedule carries no secrets; `async_redact_data` will still recurse it harmlessly. `Any` and `FoxESSControlData` are already imported/typed in this file.)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_diagnostics.py::TestScheduleSection -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/diagnostics.py tests/test_diagnostics.py
git commit --no-gpg-sign -m "feat: add live-schedule snapshot + reconcile outcome to diagnostics (issue #11)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Changelog + full verification

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add a changelog entry**

At the top of `CHANGELOG.md`, under a new `## Unreleased` heading (above the current top section), add under `### Fixed`:

```markdown
## Unreleased

### Fixed
- **Orphaned schedule groups are now cleared on startup** (issue #11, C-025). A FoxESS schedule group recurs daily, so a managed group (Force Charge / Force Discharge / Feed-in) left in the inverter by a smart session whose teardown did not complete — e.g. Home Assistant restarted mid-session, or the inverter accepted but did not apply the removal write — would re-fire at that window every day with the integration unaware. On startup, after session recovery, the integration now reads the live inverter schedule and removes any managed group that no active session covers, logging it and recording it in diagnostics. If an unmanaged mode (e.g. Backup) is present it does not modify the schedule (C-018) and surfaces the leftover instead. The diagnostics download now also includes the live inverter schedule (as of the last startup) so a leftover group is directly visible.
```

- [ ] **Step 2: Full unit suite**

Run: `python3 -m pytest tests/ -m "not slow" --tb=short`
Expected: all pass (prior count + new `tests/test_schedule_reconcile.py` and `tests/test_diagnostics.py` cases).

- [ ] **Step 3: Pre-commit (vendored sync, ruff, mypy, semgrep)**

Run: `pre-commit run --all-files`
Expected: all hooks Pass. (`_schedule_reconcile.py` is brand-layer, so semgrep's `smart_battery/` purity rule does not apply to it; mypy must pass — watch the `Any`/`Inverter` TYPE_CHECKING imports.)

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit --no-gpg-sign -m "docs: changelog for startup schedule-reconcile (issue #11)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **GPG signing times out in this environment** — pass `--no-gpg-sign` on every `git commit`.
- **Cloud-only:** `reconcile_schedule` returns immediately if `_cfg(hass).entity_mode` is True. Do not touch the entity-mode path.
- **Runs after recovery:** the call site is AFTER `_recover_sessions`, so `smart_charge_state`/`smart_discharge_state` are populated for resumed sessions. Do not move it before recovery.
- **Never raises:** the orchestrator wraps everything in a best-effort `try/except` — a reconcile failure must never break `async_setup_entry`.
- **Reuse, don't duplicate:** removal goes through `_remove_mode_from_schedule`; the C-018 unmanaged check mirrors `_check_schedule_safe`'s `_MANAGED_WORK_MODES` test (you may call a shared predicate or inline the same check — do not invent a second managed-modes list; import `_MANAGED_WORK_MODES`).
- **`record_operational_error` needs a real exception** — that's why `_OrphanedSchedule` is constructed (never raised into control flow).
- **Confirm WorkMode value spellings** (`ForceCharge`/`ForceDischarge`/`Feedin`/`SelfUse`) against `custom_components/foxess_control/foxess/__init__.py` before relying on the literal strings in `_MANAGED_OVERRIDE_MODES`; if they differ, derive `_MANAGED_OVERRIDE_MODES` from the `WorkMode` enum instead of hardcoding.
- **Out of scope (do not implement):** periodic mid-run reconcile; entity-mode reconcile; any change to the end-of-window teardown or the schedule-write errno handling.
