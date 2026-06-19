# Design: startup schedule-reconcile + schedule-in-diagnostics

**Status:** APPROVED (brainstorm complete) — ready for implementation planning.
**Date:** 2026-06-18
**Motivated by:** the issue-#11 leftover-schedule investigation — a FoxESS
schedule group recurs daily (no date field), so a managed work-mode group
left enabled in the inverter re-fires every day until something removes it.

---

## Background — the gap this closes

A FoxESS scheduler group is `startHour/startMinute/endHour/endMinute` with
**no date**, so any enabled group **recurs daily** at its window. The
integration's end-of-window teardown removes its ForceCharge/ForceDischarge/
Feedin group on a clean run, but three failure paths can leave one orphaned:

1. **HA restart between session-start and window-end** — the end-of-window
   teardown is an in-memory `async_track_point_in_time` timer
   (`smart_battery/listeners.py:1095`); it does not survive a restart.
2. **Silently-dropped removal write** — the inverter ACKs the
   `set_schedule`/`self_use` write (errno 0) but firmware does not apply it
   (the issue-#11 firmware-compat failure mode).
3. **`_check_schedule_safe` raising mid-teardown** — an unmanaged-mode guard
   abort would skip the removal.

The existing startup recovery (`_recover_sessions` in `__init__.py:697`)
returns early when there is **no persisted session** (`if not stored:
return`) — so it never inspects the inverter's *actual* schedule in the
orphan case. A recurring managed group with no stored session is exactly
what slips through: nothing reconciles it, and (separately) the diagnostics
download does not include the live schedule, so "is there a leftover group?"
can only be inferred from logs.

This design adds two cloud-backend improvements:
- **(A) Startup schedule-reconcile** — always read the live schedule on
  setup and remove an orphaned managed group (none of the recovered sessions
  cover it). Detect → surface → auto-heal.
- **(B) Schedule in diagnostics** — capture the live schedule snapshot + the
  reconcile outcome so the leftover question is directly answerable.

## Scope

**In scope (cloud backend only):**
- Read the live schedule once on every `async_setup_entry`, after the
  existing `_recover_sessions(...)` has run.
- Remove an enabled **managed** group (ForceCharge / ForceDischarge / Feedin)
  that no recovered/resumed session covers — surfacing it loudly.
- Cache the fetched schedule snapshot + reconcile outcome in domain data.
- Report that cached snapshot + outcome in the diagnostics download.

**Out of scope / non-goals (YAGNI):**
- **No periodic mid-run reconcile.** Startup-only. The orphan only matters
  across the restart / teardown-failure boundary; the live end-of-window
  teardown already handles the running case.
- **No entity-mode reconcile.** Entity mode (foxess_modbus) has no cloud
  schedule. Diagnostics reports `"n/a (entity mode)"`.
- No change to the happy-path teardown.
- No synchronous schedule fetch on the diagnostics download path (it reuses
  the cached startup snapshot).

## (A) Startup schedule-reconcile

### Placement
In `async_setup_entry`, **after** the existing `await _recover_sessions(hass,
inverter)` call (`__init__.py:1510`). Running after recovery means the
reconcile knows which groups are legitimately covered by a session that was
just recovered/resumed, so it won't remove a group belonging to an
in-progress session the integration re-adopted.

Cloud mode only — guard on `not _cfg(hass).entity_mode`.

### Logic
1. Fetch the live schedule once: `await
   hass.async_add_executor_job(inverter.get_schedule)`. On fetch failure,
   log + record (best-effort) and skip reconcile — must never block setup.
2. Cache the fetched groups + a UTC timestamp in domain data (for feature B),
   regardless of whether anything is removed.
3. Determine the set of work modes legitimately covered by a session
   recovered/resumed by `_recover_sessions`. Because the reconcile runs
   **after** `_recover_sessions`, it reads the now-populated domain-data
   session state: if `smart_charge_state` is present and its window covers
   the group, ForceCharge is covered; if `smart_discharge_state` is present,
   ForceDischarge / Feedin (as the resumed session's mode) is covered. (Read
   the post-recovery `smart_charge_state` / `smart_discharge_state` rather
   than threading a return value out of `_recover_sessions` — keeps the
   recovery function's signature unchanged and the reconcile self-contained.)
   A group is "covered" when a recovered session of that mode exists whose
   start/end window matches the group's window.
4. **Orphan criterion:** an *enabled* group whose `workMode` is in the
   managed set (ForceCharge, ForceDischarge, Feedin) and which is **not**
   covered by a recovered/active session → orphan. Self-use groups and
   **unmanaged** groups (e.g. Backup) are never orphans.
5. **C-018 guard:** if the schedule contains any **unmanaged** group, do
   **not** write (mirrors the existing refuse-to-modify-with-unmanaged-mode
   rule). Record the orphan as "detected but not removed (unmanaged mode
   present)" and surface; leave the schedule untouched.
6. Otherwise, remove the orphaned managed group(s) via the existing
   `_remove_mode_from_schedule(inverter, mode, min_soc_on_grid)` path (which
   filters out the mode's group and writes the remainder, or reverts to
   self-use). Re-use, do not duplicate, that helper.

### Surfacing (detect → surface → auto-heal)
On removing an orphan (or detecting-but-blocked):
- Loud `_LOGGER.warning` naming the mode and window (e.g. "Startup reconcile:
  removed orphaned ForceCharge group (11:00–13:59) left from a prior session
  — no active session covers it").
- `record_operational_error(..., category="orphaned_schedule_removed",
  attempted="startup schedule reconcile", hint="a recurring managed schedule
  group was left in the inverter with no active session — likely a teardown
  that did not complete (HA restart mid-session, or a write the inverter did
  not apply); it has been removed", context={mode, window, removed: bool,
  blocked_by_unmanaged: bool})` so it lands in the diagnostics ring buffer
  (C-026) and the debug-log sensors.
- The reconcile MUST NOT raise into setup — wrap so a reconcile failure can
  never break integration load.

## (B) Schedule in diagnostics

The startup reconcile caches, in `FoxESSControlData`:
- `last_schedule_snapshot`: the groups as fetched (list of
  `{enable, startHour, startMinute, endHour, endMinute, workMode,
  minSocOnGrid, fdSoc, fdPwr}`), or `None`.
- `last_schedule_snapshot_at`: ISO-8601 UTC timestamp of the fetch, or `None`.
- `last_schedule_reconcile`: a small record of the outcome — e.g.
  `{action: "removed"|"none"|"blocked_unmanaged"|"fetch_failed",
  orphans: [{mode, window}], detail: str}`.

`async_get_config_entry_diagnostics` (in `diagnostics.py`) adds a
`schedule` section reporting the cached snapshot + timestamp + reconcile
outcome, **clearly labelled "as of last startup reconcile"**. No blocking
API call on the download path. Entity mode → `"n/a (entity mode)"`. The
schedule carries no secrets, so no new redaction is required; the existing
diagnostics redaction (tokens/serials/passwords/battery-compound-id) is
unchanged and still applies to the surrounding document.

## Architecture & constraints

- Both features are **brand-layer** (`custom_components/foxess_control/`) —
  the schedule and diagnostics are FoxESS-specific. Nothing in
  `smart_battery/`; no C-039 concern.
- Reuses existing building blocks: `inverter.get_schedule()`,
  `_remove_mode_from_schedule`, `_check_schedule_safe` / `_MANAGED_WORK_MODES`,
  `record_operational_error`, the `_recover_sessions` ordering, the
  diagnostics platform.
- New domain-data fields on `FoxESSControlData`:
  `last_schedule_snapshot`, `last_schedule_snapshot_at`,
  `last_schedule_reconcile`.
- Constraints:
  - **C-018** (refuse to modify schedule when unmanaged modes present):
    preserved — reconcile does not write when an unmanaged group exists.
  - **C-025** (session boundary cleanliness): this *enforces* it across the
    restart/teardown-failure boundary that the in-memory timer cannot.
  - **C-026** (persistent errors surfaced via sensor state / diagnostics):
    the removal is recorded.
  - **C-020** (UI-determinable state): the orphan removal is observable.
  - **C-008/C-009** (fdSoc/midnight invariants): unchanged — `set_schedule`
    still enforces them.
- No change to the happy-path end-of-window teardown.

## Testing (simulator over mocks — C-028)

Reconcile (drive `async_setup_entry`-equivalent path against the simulator,
or the reconcile entry point with a real `Inverter` against the simulator):
- **Orphan removed:** simulator seeded with an enabled ForceCharge group and
  NO persisted session → after reconcile the group is gone, an
  `orphaned_schedule_removed` operational error is recorded, schedule ends
  with no managed group.
- **Covered group kept:** an enabled ForceCharge group WITH a persisted
  session that `_recover_sessions` resumes → NOT removed.
- **C-018 block:** an unmanaged Backup group present alongside an orphan →
  orphan NOT removed, surfaced as `blocked_unmanaged`, schedule untouched.
- **No orphan:** schedule already self-use only → no write, no error.
- **Discharge/Feedin orphan:** an enabled ForceDischarge (and Feedin) group
  with no session → removed (parity with charge).
- **Fetch failure:** `get_schedule` raises → setup still completes, outcome
  recorded as `fetch_failed`, no crash.

Diagnostics:
- After a reconcile, the diagnostics dict contains the `schedule` section
  with the snapshot groups, timestamp, and reconcile outcome.
- Entity mode → `schedule` reports `"n/a (entity mode)"`.

All via the in-repo FoxESS simulator (`simulator/`), not mocks. Per
[[feedback-validate-premise-before-fix]]: the simulator reproduces the
*mechanism* (an orphaned recurring group) deterministically; it is not a
claim about which failure path produced the reporter's orphan.

## What this design explicitly does NOT do

- It does not run a periodic mid-session reconcile (startup-only).
- It does not touch entity mode's mode-setting path.
- It does not change the end-of-window teardown or the schedule-write
  errno/circuit-breaker handling.
- It does not assert which of the three failure paths caused issue #11 — it
  defends against all three and makes the next occurrence self-diagnosing.

## Known limitation — manual managed groups are removed

Coverage is matched on **work mode only** (mirroring the in-session
`_has_matching_schedule_group` invariant), not on the exact schedule window.
This is required for correctness: `_build_override_group` sets a group's
start from the write-time `now`, and for ForceDischarge C-027 sets the end
to a safe horizon (SoC/rate/safety) that is *earlier* than the session's
full-window end — so a just-resumed session's live group legitimately has a
window that differs from the session window. An exact 4-field window match
would flag that group as an orphan and remove the **just-resumed live
session's** group — worse than the bug this feature fixes. Work-mode-only
coverage errs toward keeping a group whenever a session of that family is
active (a discharge session covers both ForceDischarge and Feedin).

The cost of work-mode-only coverage is that the reconcile **cannot
distinguish a user-created managed group from a true orphan**: a Force Charge
/ Force Discharge / Feed-in group created manually in the FoxESS app (with no
active smart session) looks identical to a leftover orphan and **will be
removed on startup**. To retain a manual schedule, use an unmanaged mode
(e.g. Backup), which the C-018 guard preserves, or the integration's own
controls. Provenance tracking (tagging integration-created groups so a
manual group can be recognised and kept) is deferred as out of scope.
