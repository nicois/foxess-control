# Design: schedule-write reconciliation (mode-mismatch detection)

**Status:** APPROVED (brainstorm complete) — ready for implementation planning.
**Date:** 2026-06-12
**Motivated by:** GH issue #11 (charge ran to 100%, ignoring the 95%
target; spurious "unreachable" log) on an inverter whose firmware was
updated the same night (H1-5.0-E-G2, Manager 1.76 / Master 1.57).
**Proposed decision id (if accepted into the knowledge tree):** D-062.

> This supersedes the earlier "write read-back" framing of this file.
> Read-back was rejected: a synchronous GET after every write would
> roughly double the rate-limited schedule-call quota. The approach
> below is passive — it reconciles against data the regular poll
> already fetches, at zero extra API cost.

---

## ⚠️ What is proven vs. what is hypothesis

This is deliberate, because issue #11 was previously misdiagnosed by
running ahead of the evidence (a "slow taper profile" theory that the
user falsified; the speculative fix was reverted).

### Proven (read directly from current `develop` source)

1. **API-rejected writes already surface.** `client.post` →
   `_check_response` (`foxess/client.py:72`) raises `FoxESSAPIError` on a
   non-zero errno. The cloud `apply_mode`/`remove_override` path does not
   catch it, so it bubbles to the C-024 circuit breaker
   (`smart_battery/listeners.py`), which holds position after consecutive
   failures and records a (generic) error. So an *errno != 0* rejection is
   NOT the gap.

2. **The genuinely-unsurfaced case is the API-accepted-but-not-applied
   write.** A write the API returns `errno == 0` for, but the inverter
   firmware does not actually apply, produces no exception, no
   circuit-breaker tick, and no Repair — nothing. This is invisible today.

3. **The regular poll already reads the live work mode at zero extra
   cost.** Every poll, `FoxESSDataCoordinator._fetch_all`
   (`coordinator.py:241`) calls `inverter.get_current_mode()`, which calls
   `get_schedule()` and derives the active mode into
   `coordinator.data["_work_mode"]`. The inverter's *actual* current mode
   is therefore already in hand each poll — the "subsequent API result" we
   can reconcile commanded intent against.

4. **The charge stop is SoC-driven with no independent net.** The only
   thing that removes the ForceCharge override is the G1 gate
   `cur_soc >= target_soc` (`listeners.py:705`), where `cur_soc` is
   `coordinator.data["SoC"]`; it then latches `target_reached=True`
   unconditionally. A wrong/stale SoC means the stop never fires; a
   silently-dropped removal write leaves the `fdSoc=100` ForceCharge group
   running to 100% while the integration believes it stopped.

### NOT proven (hypotheses for issue #11, consistent with the evidence)

- **H1 — firmware silently rejected the removal write.** Fits the
  100% overshoot + the firmware-change trigger.
- **H2 — SoC telemetry was wrong/stale after the firmware update**, so
  the `cur_soc >= target_soc` stop gate never fired *and* the same bad
  SoC fed `is_charge_target_reachable`, producing the spurious
  "unreachable" log. Also fits the physically-impossible "11%→100% in
  45 min" figure from the text report (≈12 kW on a 5 kW inverter ⇒ SoC
  reading suspect).

H1 vs H2 are distinguishable only with the reporter's data (debug-log
sensors + diagnostics + SoC/work-mode history across a failing window).
That request is tracked on issue #11.

### Ruled out

- **WebSocket errors interfering with the schedule logic.** The listener
  reads SoC/consumption from `coordinator.data`, not the WS; a WS
  disconnect does not clear `coordinator.data` (only re-tags
  `_data_source="api"`, `__init__.py`); WS errors are not adapter errors
  and cannot trip the circuit breaker. WS instability is a
  data-freshness/observability symptom only.

## Why this design stands regardless of which hypothesis wins

H1 and H2 are both firmware-triggered and both currently undetectable.
Whichever caused issue #11, the integration cannot today tell whether the
inverter's *actual* mode matches what it commanded. This change closes
that detection gap. It is **purely observational** — it surfaces the
divergence; it does **not** attempt to fix issue #11 or self-heal.

---

## Scope

**In scope:** detect, after a grace window, when the inverter's actual
active work mode (from the existing poll) diverges from the integration's
last-commanded mode; surface it via `record_operational_error` + a Repair
issue.

**Out of scope (decided):**
- No synchronous read-back (would double the rate-limited schedule quota).
- No corrective write / self-heal (purely observational — detect + surface
  only).
- No parameter-level comparison (fdSoc/fdPwr/window) — work-mode mismatch
  only, to stay robust against the API's group normalisation/placeholders.
- No change to the pacing/feasibility algorithms.
- No change to the existing errno → circuit-breaker path.

## Architecture & placement (C-021 / C-039 / C-040)

The comparison is brand-agnostic; the inputs are brand-layer. Split
accordingly:

### Pure decision helper — `smart_battery/`
A stateless function, unit-testable with `FakeAdapter` (C-040):

```
reconcile_commanded_mode(
    commanded_mode: str | None,     # last mode the integration wrote (None = nothing commanded yet)
    commanded_at: datetime,         # when that write was issued
    reported_mode: str | None,      # the mode the poll reports right now (_work_mode)
    now: datetime,
    grace: timedelta,               # one polling interval + margin
) -> ReconcileVerdict               # OK | WITHIN_GRACE | CONFLICT
```

- `commanded_mode is None` → `OK` (nothing to check).
- `reported_mode == commanded_mode` → `OK`.
- mismatch but `now - commanded_at <= grace` → `WITHIN_GRACE` (tolerate
  propagation lag; the inverter needs a poll cycle to reflect the change,
  and the commanded group's window must contain `now`).
- mismatch and `now - commanded_at > grace` → `CONFLICT`.

The helper carries no I/O and no HA/brand imports — it only decides.
"Removed override" is represented as `commanded_mode = "SelfUse"` (the
expected post-removal mode), so both conflict directions — override not
applied, and override not removed (the issue-#11 case) — are the same
comparison.

### Brand wiring — `custom_components/foxess_control/` (coordinator)
- **Record intent:** at the cloud-adapter write methods themselves —
  `apply_mode` records the override mode it wrote, `remove_override`
  records expected `SelfUse` — store `(commanded_mode, commanded_at)` in
  domain data via the typed accessors (`_dd`, not raw `hass.data`). This
  is adapter-level, not charge-specific, so it captures intent from every
  schedule write (charge *and* discharge sessions, and any future caller),
  which is why the reconciler is mode-agnostic.
- **Reconcile each poll:** in `_fetch_all` (after `_work_mode` is
  derived), call the pure helper with the recorded intent, the polled
  `_work_mode`, `now`, and the configured grace.
- **Surface on `CONFLICT`** (see below). On `OK` after a prior conflict,
  clear the Repair issue (idempotent).

`grace` = the coordinator's polling interval (`_get_polling_interval_seconds`
already exists) + a small margin. Entity mode is out of scope for this
change (it does not use schedule writes the same way); the wiring is on
the cloud path only.

## Surfacing & error handling

On a confirmed `CONFLICT` (idempotent — once per ongoing divergence):
- `record_operational_error(logger, recent_errors, category="schedule_not_applied",
  attempted="reconcile commanded work mode against polled mode",
  hint="the inverter reports a different work mode than was commanded — it
  may not be applying schedule changes; check inverter firmware/
  compatibility", context={"commanded": ..., "reported": ...,
  "since": commanded_at})` — lands in the always-on diagnostics ring
  buffer (D-059) and the debug-log sensors.
- A dedicated **Repair issue** (translation-keyed, e.g.
  `schedule_not_applied`) naming the firmware-compat cause (C-020 / C-026),
  with `translation_placeholders` for commanded vs reported mode. Cleared
  when the modes reconcile.

The reconciler MUST NOT raise into the poll — it is wrapped so a
reconciliation bug cannot break data collection (mirrors the existing
`get_current_mode` try/except in `_fetch_all`).

## Testing

- **Pure helper (`smart_battery/`):** unit tests — `OK` (match,
  nothing-commanded), `WITHIN_GRACE` (mismatch inside grace), `CONFLICT`
  (mismatch past grace), reconcile-clears (conflict then match), both
  conflict directions (override-not-applied and override-not-removed).
  Boundary: exactly at the grace edge.
- **Brand wiring (simulator, C-028):** extend the FoxESS simulator with a
  "firmware accepts the POST (errno 0) but does not apply it" mode — the
  next `get_schedule` returns the *prior* schedule. Drive a charge
  session through it and assert: (a) a confirmed conflict records a
  `record_operational_error` with `category="schedule_not_applied"` and
  raises the Repair issue after the grace window; (b) a normally-applied
  write surfaces nothing; (c) the Repair clears once modes reconcile;
  (d) a mismatch inside the grace window does not yet surface.
- Per [[feedback-validate-premise-before-fix]]: the simulator reproduces
  the *mechanism* (write not taking effect), NOT a claim that this is what
  happened on the user's system.
- E2E: per the standing rule to extend E2E for HA-visible changes, add an
  E2E assertion that the Repair issue appears/clears appropriately if the
  simulator's silent-drop mode is reachable from the E2E harness; if not
  cleanly reachable, document why and rely on the simulator integration
  test.

## Knowledge-tree / constraints touched

- **C-020** (UI-determinable state): a divergence becomes visible via a
  Repair issue instead of silent wrong behaviour.
- **C-026** (persistent errors surfaced via sensor state): routed through
  `record_operational_error` + Repair.
- **C-039 / C-040** (brand purity / agnostic tests): decision logic in
  `smart_battery/`, tested with `FakeAdapter`; brand I/O in the coordinator.
- **C-028** (simulator over mocks): new simulator "silent-drop" mode.
- **P-003 / P-002**: a silently-unapplied stop write violates the energy
  target; the same class of bug on a *discharge* stop could threaten min
  SoC — so although this change wires the cloud charge/discharge schedule
  path generally, the reconciler is mode-agnostic and covers both.
- No change to **C-008 / C-009 / C-018** invariants or the errno →
  circuit-breaker path.

## What this design explicitly does NOT do

- It does not assert H1 or H2 is the cause of issue #11.
- It does not read back after writes, or add any extra API call.
- It does not attempt corrective writes / self-healing.
- It does not change SoC-driven stop semantics or the pacing/feasibility
  algorithms (the earlier reverted mistake).
