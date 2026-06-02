# Design: E2E `wait_for_condition` step helper + DOM capture on failure

**Date:** 2026-06-02
**Status:** Approved (brainstorming) — pending implementation plan
**Blocks:** the 1.0.19-beta.1 prerelease (per user: the E2E flake must be
properly fixed before the prerelease proceeds).

## Problem

The E2E suite detects render failures by **timeout alone** and captures
**no DOM state** when a wait fails. Concretely:

- `tests/e2e/conftest.py::_wait_for_lovelace_panel` (and its
  `_wait_for_stage`) polls a `wait_for_function` predicate with a 75s
  budget. On timeout it re-raises a bare `PwTimeoutError` with only the
  stage name — no HTML, no screenshot, no shadow-DOM dump.
- This made the panel-wait flake (`test_ws_refuse_falls_back_to_api_during_session`
  and many predecessors) effectively un-root-causable: it has been
  "hardened" ~5 times via docstring-documented *guesses* (`panel.hass`
  flap, `networkidle` storm, stage budgets) because nobody can see what
  the DOM actually contained at the moment of failure.

Two smells, one root cause:
1. **No failure observability** — a timeout tells you *that* a wait
   failed, never *what the page showed*.
2. **Timeout-as-detector** — a bare `wait_for_function(pred, timeout)`
   cannot distinguish "still legitimately booting (keep waiting)" from
   "entered a known-bad state (fail now)". It always burns the full
   budget before failing.

## Goal

A reusable poll primitive that (a) succeeds the instant a pass predicate
is true, (b) **fails fast** when an optional fail predicate trips (a
known-bad DOM state), and (c) **captures DOM state** (full HTML +
screenshot + a short summary in the exception) on any non-success exit.
Migrate the suite's *wait* helpers onto it. Captures must survive as
GitHub CI artifacts.

## Design

### The primitive — `wait_for_condition` (in `tests/e2e/conftest.py`)

```
wait_for_condition(
    page,
    pass_check: str,                 # JS predicate; truthy return = success
    *,
    timeout_ms: int,
    fail_check: str | None = None,   # JS predicate; truthy = abort now
    description: str = "",           # names the wait in logs/errors
    poll_ms: int = 250,              # gap between predicate evaluations
) -> Any                             # the pass_check's truthy return value
```

Behaviour:
- Polls `pass_check`; returns its value the moment it is truthy.
- If `fail_check` is provided and becomes truthy first, raise
  `E2EConditionFailed` immediately (no waiting out the clock).
- On overall-budget exhaustion, raise `E2EConditionTimeout`.
- **Preserves the existing context-destroyed retry idiom**: each
  predicate evaluation that hits `"Execution context was destroyed" /
  "navigating"` is retried after a ≤3000ms `domcontentloaded` settle,
  within the remaining budget. (This logic in `_wait_for_stage` is sound
  and must be carried over verbatim in spirit.)
- On **either** failure exit (fail_check or timeout), call the capture
  routine before raising, and embed a short DOM summary in the exception
  message.

`pass_check`/`fail_check` are JS predicate strings evaluated via
`page.wait_for_function` / `page.evaluate` (reusing `_safe_evaluate`'s
context-destroyed resilience internally). Python-callable predicates are
**not** in scope (YAGNI — every current wait uses a JS predicate).

### DOM capture routine — `_capture_failure(page, description)`

Writes to `tests/e2e/screenshots/<worker>/failures/<slug>-<n>.{html,png}`:
- `page.content()` → `.html` (full DOM incl. light-DOM; shadow roots are
  serialized best-effort via an `evaluate` that walks `shadowRoot`s where
  reachable).
- `page.screenshot(full_page=True)` → `.png` (best-effort; suppressed if
  the page is in a state that can't screenshot).
- A compact summary string (top-level custom elements present, whether
  `home-assistant`/`home-assistant-main`/`ha-panel-lovelace`/`hui-root`
  are attached, any `ha-panel-error`/error-toast text) — returned so the
  caller embeds it in the exception message (visible in `--tb=long` CI
  logs without downloading artifacts).

**Why `tests/e2e/screenshots/`:** the existing GH workflows
(`e2e.yml:122-129`, `flaky-tests.yml:72-79`) already upload that path
with `if: always()` and 14-day retention. Writing captures into a
`failures/` subdir there means **they become downloadable CI artifacts
with zero workflow change**. (Confirmed against both workflow files.)

Capture must itself never raise (best-effort, broad-suppressed) — a
failing capture must not mask the original wait failure.

### Migration — rebuild the *wait* helpers on the primitive

These delegate to `wait_for_condition`, **preserving their current
signatures** so the ~36 call sites are untouched:

| Helper | Calls | Change |
|---|---|---|
| `_wait_for_lovelace_panel` / `_wait_for_stage` (`conftest.py`) | 1 | Each stage → a `wait_for_condition` with the stage `pass_check` and a shared `fail_check` for HA error states (`ha-panel-error` attached, config-entry-error screen, error toast). This is the flaking wait — it gains fail-fast + capture. |
| `_find_card` (`test_ui.py`, 21 calls) | 21 | Thin wrapper → `wait_for_condition(pass_check=<card-present JS>, ...)`. Same return contract (bool/element-present). |
| `_wait_for_card_hass` (`test_ui.py`, 2 calls) | 2 | Wrapper → `wait_for_condition(pass_check=<card._hass truthy + rendered JS>, ...)`. |
| `_safe_wait_for_function` (`test_ui.py`, 13 calls) | 13 | Wrapper → `wait_for_condition`, returning the predicate's value (current callers use `.json_value()` on the handle — preserve that, or adapt the wrapper to keep the existing return shape). |

`fail_check` is **available but optional**; only the lovelace panel wait
gets a populated `fail_check` in this change (HA error-screen selectors).
Card waits pass `fail_check=None` for now — the mechanism is wired; we
populate per-wait fail predicates opportunistically as known-bad states
are identified. (Decision: wire the mechanism now, enumerate HA error
selectors for the lovelace wait, don't speculatively invent fail_checks
for card waits.)

### `_safe_evaluate` — keep separate, add capture

`_safe_evaluate` (`test_ui.py`, 24 calls) is a **one-shot
evaluate-with-retry**, not a poll — it stays a distinct primitive and
its 24 call sites are unchanged. The only change: on **final failure**
(after retries exhausted, before the last re-raise), call
`_capture_failure(page, "_safe_evaluate: <expr-snippet>")` so a one-shot
evaluate that dies on context destruction also leaves a DOM artifact.
`wait_for_condition` reuses `_safe_evaluate`'s retry idiom internally for
its predicate evaluations.

## Testing

Helper-level (no container needed where possible; otherwise against the
real HA container like existing `TestWaitForLovelacePanel*`):
- `pass_check` truthy → returns immediately, no capture written.
- `fail_check` trips before timeout → raises `E2EConditionFailed`
  *before* the budget elapses (assert elapsed << timeout), capture files
  written, summary in the message.
- timeout with neither → raises `E2EConditionTimeout`, capture written.
- context-destroyed during predicate eval → retried, still converges
  (mirror the existing `_wait_for_stage` retry regression tests).
- `_capture_failure` writes `.html` + `.png` under
  `tests/e2e/screenshots/<worker>/failures/` and never raises (feed it a
  closed/broken page → no exception).
- `_safe_evaluate` final-failure path writes a capture (mock `page` whose
  `evaluate` always raises context-destroyed).

Migration regression: the existing E2E suite must still pass (proves the
wrapped helpers preserved behaviour). Existing
`TestWaitForLovelacePanelCloudVariantSignalStability` /
`TestWaitForLovelacePanelEntityModeInitRace` must still pass (or be
ported onto the new helper with equivalent assertions).

## What this does NOT do

- Does not change any product code (test infrastructure only).
- Does not add Python-callable predicates (JS-only — YAGNI).
- Does not populate fail_checks for card waits (mechanism only).
- Does not change the GH workflow (existing upload path covers it).
- Does not, by itself, *guarantee* the panel flake is gone — it makes the
  next occurrence diagnosable and adds fail-fast. If the capture reveals a
  genuine product/HA-boot issue, that's a follow-up fix; if it confirms
  pure runner slowness, the budget is justified and the fail_check
  prevents the 75s burn on real error states.

## Effect on the flake / prerelease

The immediate `test_ws_refuse_falls_back_to_api_during_session` timeout
becomes: either it fails fast with a captured DOM showing an HA error
state (→ root cause visible, fix it), or it converges (→ was slow-boot,
now observable next time). Per the user's instruction, the prerelease is
held until this lands and the flake is understood — not merely re-run
green.
