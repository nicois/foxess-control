---
project: FoxESS Control
level: 2
last_verified: 2026-04-18
traces_up: [01-vision.md]
traces_down: [03-architecture.md, 04-design/]
---
# Constraints

Three tiers: **principles** derive from the vision and justify multiple
design decisions; **invariants** are specific testable rules; **proposed**
constraints are identified gaps not yet implemented.

Constraint IDs (C-NNN) are stable and never reused.

## Principles

Broad constraints that derive directly from the vision. Each justifies
multiple invariants and design decisions below it.

### C-020: Operational transparency
**Statement**: The user must be able to determine the system's current
state, what it is doing, and why, from the UI alone — without
inspecting logs, developer tools, or source code.
**Rationale**: Smart battery operations involve time-dependent pacing,
deferred starts, session recovery, and multiple data sources. When
something unexpected happens (e.g. discharge not starting, values
appearing stale), the user needs enough visible state to diagnose
the issue themselves. Opacity creates support burden and erodes trust.
**Violation consequence**: Users cannot distinguish normal operation
(e.g. deferred start waiting) from a fault, leading to unnecessary
manual intervention or missed issues.
**Traces**: D-021, D-027;
C-022 (unreachable target surfaced), C-026 (error surfacing)

### C-021: Brand-agnostic code belongs in the common package
**Statement**: Code, algorithms, types, and related assets that do not
directly relate to a specific inverter brand must be placed in the
`smart_battery/` common package, not in brand-specific integration
directories.
**Rationale**: The strategic direction is multi-brand support. Code
that lives in `custom_components/foxess_control/` but has no FoxESS
dependency becomes an obstacle — it must be duplicated or extracted
when adding a second brand. Placing it in `smart_battery/` from the
start avoids this.
**Violation consequence**: Brand-agnostic logic trapped in a
brand-specific directory, requiring extraction work before each new
brand integration and risking divergence between copies.
**Traces**: C-015

### C-024: Safe state on failure
**Statement**: On persistent failure — repeated uncaught exceptions
in a session callback, API becoming unresponsive, or integration
unload — the system must ensure that forced charge/discharge
overrides do not persist long enough to cause serious inconvenience
to the user. Transient errors (single API timeout, brief DNS outage)
must be tolerated and retried on the next timer tick; only
`MAX_CONSECUTIVE_ADAPTER_ERRORS` (3) consecutive failures trigger
session abort.
**Rationale**: Self-use is the only mode where the inverter manages
itself safely without external control. A forced mode left active
after the controlling session has failed will run unchecked —
charging indefinitely or discharging past min SoC with no pacing.
However, aborting on a single transient error (e.g. a few seconds of
DNS instability) kills multi-hour sessions unnecessarily — the error
would self-resolve on the next tick.
**Violation consequence**: Inverter stuck in forced charge
(overcharging, wasted grid import) or forced discharge
(over-discharge, grid import) with no active session monitoring it.
**Traces**: C-012 (specific case);
`tests/test_services.py::TestCallbackExceptionSafety`,
`tests/test_services.py::TestTransientApiErrorResilience`,
`tests/test_services.py::TestStaleWorkModeAfterCleanupFailure`

### C-025: Session boundary cleanliness
**Statement**: When a smart session ends (normally, by cancellation,
or by failure), all inverter overrides created by that session must
be fully removed and the inverter returned to self-use before a new
session can start. Transient state from the previous session (peak
consumption tracking, taper tick counters, feed-in baselines) must
not leak into the next session.
**Rationale**: Back-to-back sessions are a supported use case (C-013).
If the previous session's overrides or state linger, the new session
inherits incorrect assumptions — e.g. an inflated peak consumption
from a kettle boil in the previous window artificially floors
discharge power in the next window.
**Violation consequence**: New session operates on stale state from
the previous one, causing incorrect pacing or unexpected forced mode
retention.
**Traces**: C-003 (stale callback prevention);
`tests/test_services.py::TestSessionBoundaryCleanness`,
`tests/test_services.py::TestStaleWorkModeAfterCleanupFailure`

## Invariants — Safety

Specific testable rules that protect the battery, the grid connection,
and the user's configuration.

### C-001: No grid import during forced discharge
**Statement**: During forced discharge, inverter output power must be
floored at `max(paced_power, peak_consumption * 1.5)` to prevent the
house drawing from the grid.
**Rationale**: When paced discharge power drops below household load,
the shortfall is imported from the grid. This defeats the purpose of
discharge (self-consumption or feed-in) and incurs cost.
**Violation consequence**: Unexpected grid import during discharge
windows, inflating electricity costs.
**Traces**: D-001, D-004;
`tests/test_smart_battery_algorithms.py::TestCalculateDischargePower::test_consumption_exceeds_needed_floors_at_consumption`,
`tests/test_smart_battery_algorithms.py::TestShouldSuspendDischarge`

### C-002: Never discharge below minimum SoC
**Statement**: Discharge must suspend when battery SoC reaches or drops
below the configured `min_soc`. Two mechanisms enforce this:
(a) `should_suspend_discharge()` (pure function) returns True when
remaining energy above min_soc is zero or insufficient to sustain the
safety floor; (b) the listener layer requires 2 consecutive checks
with SoC at/below min_soc before ending the session, preventing
premature termination from transient SoC dips.
**Rationale**: Deep discharge damages battery longevity and may leave
insufficient reserve for backup power during outages.
**Violation consequence**: Battery over-discharge, reduced cycle life,
no backup reserve.
**Traces**: D-001;
`tests/test_smart_battery_algorithms.py::TestShouldSuspendDischarge::test_soc_at_min_suspends`,
`tests/test_smart_battery_algorithms.py::TestShouldSuspendDischarge::test_soc_below_min_suspends`,
`tests/test_services.py::TestSocStabilityCounters`

### C-003: Session identity prevents stale callback races
**Statement**: Every smart session receives a unique `session_id`.
Periodic callbacks verify `cur_state["session_id"] == my_session_id`
before taking any action. Stale callbacks from cancelled sessions are
silently dropped.
**Rationale**: When a user starts a new session while an old one is
still winding down, the old session's timers may fire and corrupt the
new session's state or send conflicting inverter commands.
**Violation consequence**: Race condition — old session overwrites new
session's power setting or cancels it.
**Traces**: D-017, D-018;
`tests/test_init.py` (session lifecycle tests)

### C-012: SoC unavailability cancels session after 15 minutes
**Statement**: If the SoC entity is unavailable for 3 consecutive checks
(3 x 5 min = 15 min), the smart session is cancelled.
**Rationale**: Operating blind without SoC data risks over-charging or
over-discharging.
**Violation consequence**: Battery damage from uncontrolled charge/discharge.
**Traces**: D-019; `smart_battery/const.py:52`

### C-013: Maximum override duration is 4 hours
**Statement**: Service calls for force charge/discharge/feedin are
capped at 4 hours (`MAX_OVERRIDE_HOURS`).
**Rationale**: Prevents accidental indefinite forced operations that
could drain or stress the battery. In the rare cases where greater durations
are desired, it is possible to schedule a second smart operation shortly after
the first one has finished.
**Violation consequence**: Battery stuck in forced mode indefinitely.
**Traces**: `smart_battery/const.py:24`;
`tests/test_init.py::TestResolveStartEnd::test_exceeds_max_hours`

### C-016: Cancel listeners before awaits
**Statement**: When cancelling a smart session, all listener
unsubscriptions must happen synchronously before any `await` calls.
**Rationale**: If an `await` yields, a stale timer callback can fire
between the cancellation decision and the actual unsubscription,
corrupting state.
**Violation consequence**: Race condition — stale callback fires during
cancellation, re-enabling an override that should be removed.
**Traces**: D-018

### C-017: End-of-discharge guard
**Statement**: When remaining energy above min_soc cannot sustain the
discharge safety floor for `_END_GUARD_MINUTES` (10 minutes), forced
discharge must suspend and switch to self-use.
**Rationale**: Near the end of a discharge window, paced power drops to
the 100W minimum — well below house load. Continuing forced discharge
causes grid import for the remaining minutes.
**Violation consequence**: Tail-end grid import when paced power is
below house load.
**Traces**: D-003;
`tests/test_smart_battery_algorithms.py::TestShouldSuspendDischarge::test_high_consumption_suspends`

### C-018: Unmanaged work mode protection
**Statement**: Service calls must refuse to modify the inverter schedule
when non-managed work modes (e.g. Backup) are present in existing
schedule groups.
**Rationale**: Silently overwriting a Backup schedule could leave the
home unprotected during a grid outage. The integration assumes SelfUse
as the baseline mode.
**Violation consequence**: User's Backup protection silently removed.
**Traces**: D-016;
`tests/test_init.py::TestCheckScheduleSafe`,
`tests/test_init.py::TestMergeWithExisting::test_rejects_schedule_with_backup_mode`

### C-019: Discharge SoC unavailability aborts session
**Statement**: If the SoC entity is unavailable for
`MAX_SOC_UNAVAILABLE_COUNT` (3) consecutive checks, the smart
discharge session is cancelled and the inverter reverted to self-use,
matching the charge path (C-012).
**Rationale**: Operating blind without SoC data during forced discharge
risks over-discharge past min SoC. Reverting to self-use is the
safest option (C-001 no-import priority).
**Violation consequence**: Inverter stuck in forced discharge with no
SoC feedback, risking over-discharge.
**Traces**: D-019;
`tests/test_services.py::TestDischargeSocUnavailability::test_discharge_soc_unavailable_aborts`,
`tests/test_services.py::TestDischargeSocUnavailability::test_discharge_soc_available_resets_count`

## Invariants — Data Integrity

### C-004: WebSocket values are in watts; coordinator expects kW
**Statement**: All WebSocket power values are strings representing
**watts**. They must be converted to kW before injection into the
coordinator. The per-field `unit` property is authoritative — when
`unit: "kW"`, the value is used as-is.
**Rationale**: The REST API returns kW. The WebSocket returns watts as
strings, but sometimes sends individual fields in kW. Verified
empirically 2026-04-14 (mixed units in same message).
**Violation consequence**: Power values displayed 1000x too large or
too small on dashboard cards; taper profile corruption.
**Traces**: D-010;
`tests/test_realtime_ws.py::TestMapWsToCoordinator`

### C-005: WebSocket stale messages must be filtered
**Statement**: WebSocket messages with `timeDiff > 30` seconds must be
discarded. The first message after connecting is typically 30-200+
seconds stale (cached cloud data).
**Rationale**: Stale data overwrites valid REST values, causing sensors
to briefly show incorrect or missing values on the overview card.
**Violation consequence**: Dashboard shows "—" or wrong values for
grid/battery/solar power immediately after WS connects.
**Traces**: D-008;
`tests/test_realtime_ws.py::TestStaleness::test_stale_messages_skipped`

### C-006: Grid direction derived from power balance
**Statement**: Grid import/export direction is computed as
`net = load + bat_charge - bat_discharge - solar`. Positive = importing,
negative = exporting. The `gridStatus` field is used only as fallback
when solar or load data is missing.
**Rationale**: The `gridStatus` field from the WebSocket is unreliable
(meaning varies by firmware version).
**Violation consequence**: Import shown as export or vice versa on
dashboard; feed-in energy integration goes wrong.
**Traces**: D-010;
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_importing_from_balance`,
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_exporting_from_balance`

### C-007: REST poll resets WebSocket integration state
**Statement**: When a REST API poll completes, the WebSocket feed-in
integration baseline is reset and the authoritative REST `feedin` value
is restored.
**Rationale**: REST data is the authoritative source. WebSocket
trapezoidal integration of feed-in power is an approximation between
polls. Accumulated error must not persist.
**Violation consequence**: Feed-in energy drifts from actual meter
reading over time.
**Traces**: D-009;
`tests/test_coordinator.py::TestInjectRealtimeData::test_rest_poll_resets_integration_state`

### C-014: Taper profile plausibility check
**Statement**: On load, the taper profile is checked for plausibility
(median trusted ratio > 0.10). Implausible profiles are auto-reset.
**Rationale**: A corrupted taper profile (e.g., from a unit mismatch
bug) causes the behind-schedule detector to always fire at max power,
breaking pacing.
**Violation consequence**: Charge/discharge runs at max power every tick
instead of pacing to target.
**Traces**: D-006;
`tests/test_taper.py::TestIsPlausible`

### C-015: Vendored smart_battery must match canonical
**Statement**: The vendored copy at
`custom_components/foxess_control/smart_battery/` must be byte-identical
to the canonical root-level `smart_battery/` directory.
**Rationale**: Drift between copies causes subtle behavioural
differences that are hard to diagnose.
**Violation consequence**: FoxESS integration uses stale algorithm code.
**Traces**:
`tests/test_smart_battery_sync.py::test_vendored_copy_matches_canonical`

## Invariants — FoxESS API

### C-008: fdSoc >= 11 and minSocOnGrid <= fdSoc
**Statement**: The FoxESS Cloud API rejects schedule writes where
`fdSoc < 11` or `minSocOnGrid > fdSoc` (errno 40257). All schedule
groups must be sanitised before writing.
**Rationale**: Undocumented API hard limits discovered empirically.
**Violation consequence**: Schedule write fails with errno 40257.
**Traces**: D-011;
`tests/test_init.py::TestSanitizeGroup::test_clamps_fd_soc_to_api_minimum`

### C-009: Schedule windows must not cross midnight
**Statement**: All schedule group time windows must start and end on
the same calendar day. This is a FoxESS API limitation.
**Rationale**: The FoxESS schedule API uses `startHour/startMinute` and
`endHour/endMinute` without a date component.
**Violation consequence**: Undefined schedule behaviour on the inverter.
**Traces**: D-011;
`tests/test_init.py::TestResolveStartEnd::test_crosses_midnight_rejected`

### C-010: Placeholder schedule groups must be filtered
**Statement**: The FoxESS API always returns 8 schedule groups. Unused
slots must be filtered before writing back.
**Rationale**: Leaving zero-duration SelfUse groups causes API error
42023 ("Time overlap").
**Violation consequence**: Schedule write fails; stale schedule persists.
**Traces**: D-011;
`tests/test_init.py::TestIsPlaceholder`

### C-011: Extra fields must be stripped from schedule groups
**Statement**: Schedule groups returned by `scheduler/get` include extra
fields that `scheduler/enable` rejects. Groups must be sanitised to
the known-good field set.
**Rationale**: API read/write field sets are inconsistent.
**Violation consequence**: Schedule write fails with errno 40257.
**Traces**: D-011;
`tests/test_init.py::TestSanitizeGroup::test_strips_unknown_keys`

## Invariants — Observability

These enforce the C-020 (operational transparency) and C-026 (error
surfacing) principles at a specific, testable level.

### C-022: Unreachable charge target must be surfaced
**Statement**: When the system detects that the target SoC is
unreachable within the remaining window (accounting for consumption
headroom, taper, and current SoC), it must surface this to the user
via the UI.
**Rationale**: The user scheduled a charge expecting to reach the
target. Silent failure to reach it wastes cheap-rate hours and may
leave the battery underprepared for the next discharge window.
**Violation consequence**: User discovers the charge fell short only
after the window has closed, too late to take corrective action.
**Traces**: C-020;
`tests/test_smart_battery_algorithms.py::TestIsChargeTargetReachable`

### C-026: Proactive error surfacing
**Statement**: When the system encounters a persistent error state —
API returning errors, inverter not responding to mode changes,
schedule writes failing, or session unable to make progress — it must
surface the error to the user via the UI (sensor state or attribute),
not only to the log.
**Rationale**: C-020 ensures the user can see what the system is
*doing*. But when the system is *failing*, log-only errors are
invisible to the dashboard user.
**Violation consequence**: User believes the system is operating
normally when it is actually failing.
**Traces**: C-020;
`tests/test_services.py::TestErrorSurfacing`

## Invariants — Testing

### C-028: Simulator over mocks, with instance isolation
**Statement**: Tests that exercise FoxESS API or WebSocket behaviour
must use the FoxESS simulator (`simulator/`) rather than response
mocking libraries. Mocks are acceptable only for HA framework
internals that the simulator cannot replace. Each test must have an
independent simulator instance with no shared mutable state — the
`InverterModel` and WebSocket client list must be per-app, not
module-level.
**Rationale**: Mocks encode assumptions about API behaviour that
drift from reality. The simulator implements the actual API contract
(REST, WS, schedule validation, fault injection) and is authoritative
for the integration's external interface. Tests that mock the API
pass when the mock is wrong. Instance isolation prevents cross-test
contamination under parallel execution (pytest-xdist): a module-level
singleton was the root cause of ~0.5% flaky test rate where teardown
`reset()` from one worker clobbered another test's state.
**Violation consequence**: Tests pass against a mock that doesn't
match real API behaviour, masking integration bugs. Shared simulator
state causes intermittent test failures under parallel execution.
**Traces**: `tests/test_client.py`, `tests/test_inverter.py`
(migrated from `responses` library to simulator);
`simulator/server.py::create_app` (per-app state isolation)

### C-029: E2E tests for HA-dependent behaviour
**Statement**: Behaviour that depends on HA's runtime environment —
config flow, entity lifecycle, service registration, Lovelace card
rendering, auth, coordinator polling — must have E2E tests against
a real HA instance (containerised). Unit tests with mocked HA
internals are insufficient for these code paths.
**Rationale**: HA's internal APIs (config entries, entity platforms,
service calls, frontend WebSocket) have undocumented behaviours and
version-specific quirks that mocks cannot reproduce. The E2E suite
(`e2e/`) uses a Podman HA container with pre-seeded config to test
the actual integration lifecycle.
**Violation consequence**: Code works in unit tests but fails in a
real HA installation due to auth, entity discovery, shadow DOM, or
service registration differences.
**Traces**: `e2e/test_e2e.py`, `e2e/test_ui.py`

### C-030: E2E tests run in parallel before tagging
**Statement**: E2E tests must run with `pytest -n auto` (xdist
parallel workers) and must pass before any version tag is pushed.
The pre-push hook enforces this gate.
**Rationale**: Serial E2E takes 25+ min; parallel takes ~4 min with
10 workers. The parallel infrastructure (named containers, atexit
cleanup, shared SELinux labels) is designed for concurrent execution
and serial runs waste development time. Running before tagging
ensures no release ships with broken E2E.
**Violation consequence**: Slow feedback loop or regressions shipping
in tagged releases.
**Traces**: `.githooks/pre-push`, `conftest.py::pytest_xdist_auto_num_workers`

### C-031: No flaky tests — fix root causes, don't mask symptoms
**Statement**: Every test failure must be investigated to its root
cause. Intermittent failures signal real race conditions or timing
assumptions that also affect production. Tests must not be skipped,
xfailed, or have parameters tuned to avoid triggering the issue.
**Rationale**: A flaky test is a bug report from the test
infrastructure. The race condition it exposes exists in production
too — the test just makes it visible. Masking it (skip, xfail,
longer timeout, different parameters) hides the bug without fixing
it. The correct response is: identify the event ordering assumption
that's violated, and fix the code or the test infrastructure so the
assumption holds deterministically.
**Violation consequence**: Race conditions persist in production,
manifesting as intermittent user-facing bugs that are hard to
reproduce and diagnose.
**Traces**: C-028 (simulator over mocks), C-029 (E2E for
HA-dependent behaviour); `e2e/ha_client.py::HAEventStream`
(drain-before-wait pattern eliminates event ordering races)

### C-032: Reproduce failure before fixing
**Statement**: When a bug is discovered — from user reports, live
monitoring, or test failures — a test must be written that reliably
reproduces the failure with the current (broken) code BEFORE any fix
is attempted. The fix is only applied after the test is confirmed to
fail, and the commit includes both the test and the fix.
**Rationale**: A fix without a reproducing test provides no confidence
that it addresses the actual issue. The test proves the bug exists,
proves the fix works, and prevents regression. Without this discipline,
fixes may address a different symptom, or the test may pass for the
wrong reason (as happened with the SoC interpolation mock that had
`capacity=0`, silently preventing integration from running).
The test must fail for the **same root cause** as production —
a test that fails because of test environment differences (e.g.
E2E polling interval differs from production) is a test bug,
not a valid reproduction.
**Violation consequence**: False confidence in fixes; regressions
reappear because the "fix" didn't address the root cause.
**Traces**: C-031 (no flaky tests);
`/regression-test` skill (enforces the discipline);
`tests/test_coordinator.py::TestSocInterpolationDuringDischarge`
(confirmed to fail with bug, pass with fix)

### C-033: Minimise known simulator–production deviations
**Statement**: Known behavioural differences between the FoxESS
simulator and the real FoxESS cloud/inverter system must be
documented and minimised. When a deviation is identified, it must
be either fixed in the simulator or explicitly accepted with a
rationale for why the difference is safe.
**Rationale**: The simulator is the integration's authoritative test
double (C-028). Any deviation between the simulator and the real
system creates a blind spot: tests pass against behaviour the real
system doesn't exhibit, or fail to test behaviour it does. These
blind spots compound — a test written to work around a simulator
quirk may mask a real bug, and the workaround becomes load-bearing.
The `test_schedule_horizon_during_discharge` failure (2026-04-17)
was caused by the E2E polling interval (60s) differing from the
simulator's immediate state visibility, causing the coordinator
to use stale SoC data and take a different code path than intended.
**Violation consequence**: Tests pass in CI but the integration
misbehaves in production, or tests fail for reasons unrelated to
the code under test — both erode confidence in the test suite.
**Traces**: C-028 (simulator over mocks), C-031 (no flaky tests),
C-032 (reproduce before fix)

### C-027: Progressive schedule extension (discharge safety)
**Statement**: The inverter schedule end time for forced discharge must
be set to a safe horizon — the time at which the battery would reach
min_soc at the current discharge rate, divided by the safety factor —
not the full user-requested window end. The horizon is recomputed and
extended on each power adjustment.
**Rationale**: If HA loses connectivity (crash, network outage, power
loss), the inverter continues executing the schedule unsupervised.
With the full window end, this means draining to fdSoc for the entire
window. With a safe horizon, the schedule expires within minutes and
the inverter reverts to self-use automatically.
**Violation consequence**: Battery drains to fdSoc during HA downtime
instead of reverting to self-use.
**Traces**: C-024 (safe state on failure);
D-023; `smart_battery/algorithms.py::compute_safe_schedule_end`

## Proposed

Constraints identified from vision gap analysis but not yet implemented.

### C-023: Solar-aware charge reduction
**Statement**: During smart charge, when solar generation exceeds
household consumption, the system must reduce grid charge power to
allow solar surplus to contribute to the battery.
**Rationale**: Charging at full grid power while solar is producing
wastes free energy — solar output goes to the grid at the feed-in
rate (often zero or low) instead of into the battery for free.
**Violation consequence**: User pays for grid energy that solar could
have provided for free, directly reducing ROI.
**Status**: Under investigation. Discharge observation (2026-04-15)
confirmed the inverter manages power flow internally
(`grid_export = discharge + solar - load`). Charge test pending to
verify if the inverter also uses solar first during ForceCharge.
**Traces**: -- (pending investigation)
