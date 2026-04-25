---
project: FoxESS Control
level: 2
last_verified: 2026-04-24
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
**Priority enforced**: P-005 (operational transparency)
**Rationale**: Smart battery operations involve time-dependent pacing,
deferred starts, session recovery, and multiple data sources. When
something unexpected happens (e.g. discharge not starting, values
appearing stale), the user needs enough visible state to diagnose
the issue themselves. Opacity creates support burden and erodes trust.
**Violation consequence**: Users cannot distinguish normal operation
(e.g. deferred start waiting) from a fault, leading to unnecessary
manual intervention or missed issues.
**Traces**: D-021, D-027, D-030, D-033;
C-022 (unreachable target surfaced), C-026 (error surfacing)

### C-021: Brand-agnostic code belongs in the common package
**Statement**: Code, algorithms, types, and related assets that do not
directly relate to a specific inverter brand must be placed in the
`smart_battery/` common package, not in brand-specific integration
directories. **Conversely**, brand-specific code (cloud API clients,
WASM blobs, per-brand entity naming, brand-specific WebSocket
protocols) must not live in `smart_battery/`, and `smart_battery/`
must not import from brand-specific modules (see C-039 for the
enforced dependency-inversion form).
**Priority enforced**: P-006 (brand portability)
**Rationale**: The strategic direction is multi-brand support. Code
that lives in `custom_components/foxess_control/` but has no FoxESS
dependency becomes an obstacle — it must be duplicated or extracted
when adding a second brand. Conversely, brand leakage into
`smart_battery/` couples the common package to FoxESS assumptions
(e.g. FoxESS's `fdSoc` naming) that will not hold for Huawei /
SolaX / Sungrow. Both directions matter.
**Violation consequence**: Either (a) brand-agnostic logic trapped
in a brand-specific directory, requiring extraction work before
each new brand integration and risking divergence between copies;
or (b) brand leakage into `smart_battery/` that must be refactored
out before the next brand can be added.
**Traces**: C-015 (vendored sync — ensures the common-package copy
used at runtime matches the canonical one),
C-039 (dependency-inversion form — enforced by semgrep).

### C-024: Safe state on failure
**Statement**: On persistent failure — repeated uncaught exceptions
in a session callback, API becoming unresponsive, or integration
unload — the system must ensure that forced charge/discharge
overrides do not persist long enough to cause serious inconvenience
to the user.
**Priority enforced**: P-001 (no grid import) and P-002 (min SoC) —
an abandoned forced mode can violate both Transient errors (single API timeout, brief DNS outage)
must be tolerated and retried on the next timer tick. After
`MAX_CONSECUTIVE_ADAPTER_ERRORS` (3) consecutive failures, a circuit
breaker opens: the session holds its current position (no adapter
calls) for up to `CIRCUIT_BREAKER_TICKS_BEFORE_ABORT` (5) additional
ticks. If the adapter recovers during this window the circuit breaker
resets and normal operation resumes. If it does not recover, the
session aborts to self-use.
**Rationale**: Self-use is the only mode where the inverter manages
itself safely without external control. A forced mode left active
after the controlling session has failed will run unchecked —
charging indefinitely or discharging past min SoC with no pacing.
However, aborting on a single transient error (e.g. a few seconds of
DNS instability) kills multi-hour sessions unnecessarily — the error
would self-resolve on the next tick. The two-tier circuit breaker
adds further tolerance: a brief API outage (< 25 min for charge,
< 5 min for discharge) is survived without losing the session.
**Violation consequence**: Inverter stuck in forced charge
(overcharging, wasted grid import) or forced discharge
(over-discharge, grid import) with no active session monitoring it.
**Traces**: C-012 (specific case); D-025, D-026, D-031, D-032, D-034;
`tests/test_services.py::TestCallbackExceptionSafety`,
`tests/test_services.py::TestTransientApiErrorResilience`,
`tests/test_services.py::TestStaleWorkModeAfterCleanupFailure`

### C-025: Session boundary cleanliness
**Statement**: When a smart session ends (normally, by cancellation,
or by failure), all inverter overrides created by that session must
be fully removed and the inverter returned to self-use before a new
session can start.
**Priority enforced**: P-001 (no grid import) and P-002 (min SoC) —
stale state from the previous session can cause a new session to
violate either invariant Transient state from the previous session (peak
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
**Priority enforced**: P-001 (no grid import)
**Rationale**: When paced discharge power drops below household load,
the shortfall is imported from the grid. This defeats the purpose of
discharge (self-consumption or feed-in) and incurs cost. Import risk
scales with both the power gap (max_power - paced_power) and the
duration of forced discharge — long sessions at low paced power are
a primary risk vector. This is why feedin-limited sessions should
defer until the feedin deadline, then discharge at higher power for a
shorter burst, rather than starting immediately at low paced power
(see D-002, D-005).
**Violation consequence**: Unexpected grid import during discharge
windows, inflating electricity costs.
**Traces**: D-001, D-002, D-004, D-005;
`tests/test_smart_battery_algorithms.py::TestCalculateDischargePower::test_consumption_exceeds_needed_floors_at_consumption`,
`tests/test_smart_battery_algorithms.py::TestShouldSuspendDischarge`

### C-002: Never discharge below minimum SoC
**Priority enforced**: P-002 (respect minimum SoC)
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
**Priority enforced**: P-001 (no grid import) and P-002 (min SoC) —
stale callbacks corrupt state and can cause either invariant to
silently fail
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
**Priority enforced**: P-002 (min SoC) — operating blind risks
over-discharge past the floor
**Statement**: If the SoC entity is unavailable for 3 consecutive checks
(3 x 5 min = 15 min), the smart session is cancelled.
**Rationale**: Operating blind without SoC data risks over-charging or
over-discharging.
**Violation consequence**: Battery damage from uncontrolled charge/discharge.
**Traces**: D-019; `smart_battery/const.py:52`

### C-013: Maximum override duration is 4 hours
**Priority enforced**: P-001 (no grid import) and P-002 (min SoC) —
bounded override duration caps the blast radius of any runaway session
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
**Priority enforced**: P-001 (no grid import) and P-002 (min SoC) —
a stale callback re-enabling an override is the same class of
defect as C-003
**Statement**: When cancelling a smart session, all listener
unsubscriptions must happen synchronously before any `await` calls.
**Rationale**: If an `await` yields, a stale timer callback can fire
between the cancellation decision and the actual unsubscription,
corrupting state.
**Violation consequence**: Race condition — stale callback fires during
cancellation, re-enabling an override that should be removed.
**Traces**: D-018

### C-017: End-of-discharge guard
**Priority enforced**: P-001 (no grid import) — the guard specifically
protects against tail-end import when paced power would fall below
house load
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
**Priority enforced**: P-002 (min SoC) — silently removing a Backup
schedule leaves the user without the reserve they configured
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
**Priority enforced**: P-002 (min SoC) — operating blind during
forced discharge is the exact scenario that risks breaching the
min-SoC invariant
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
**Priority enforced**: P-005 (operational transparency) — incorrect
units surface as wrong dashboard values, which also corrupts the
taper profile and silently degrades pacing accuracy
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
**Priority enforced**: P-005 (operational transparency) — stale
values displayed in the UI undermine the user's ability to
understand current system state
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
**Priority enforced**: P-005 (operational transparency) — wrong grid
direction is the most visible possible data-integrity failure
**Statement**: Grid import/export direction is computed as
`net = load + bat_charge - bat_discharge - solar`. Positive = importing,
negative = exporting. The `gridStatus` field is used as fallback when:
(a) solar or load data is missing, or (b) the balance-predicted magnitude
diverges >3× from the actual grid reading (indicating unmeasured external
generation or load not visible to FoxESS).
**Rationale**: The `gridStatus` field from the WebSocket is unreliable
(meaning varies by firmware version), but the power balance is also
unreliable when FoxESS does not see all generation sources (e.g. a
separate grid-tied solar inverter on the same meter).
**Violation consequence**: Import shown as export or vice versa on
dashboard; feed-in energy integration goes wrong.
**Traces**: D-010;
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_importing_from_balance`,
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_exporting_from_balance`,
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_balance_unreliable_unmeasured_generation`,
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_balance_unreliable_importing`

### C-007: REST poll resets WebSocket integration state
**Priority enforced**: P-003 (energy target) — the feed-in target is
tracked against integrated energy; accumulated drift causes the
session to stop early or late
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
**Priority enforced**: P-003 (energy target) — a corrupt taper
profile makes pacing run at max power every tick, defeating the
target-achievement purpose of smart pacing
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
**Priority enforced**: P-006 (brand portability) and P-007 (process
integrity) — drift between canonical and vendored copies undermines
the "extract once, share everywhere" architecture
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
**Priority enforced**: P-003 (energy target) — failed schedule
writes mean the session never runs, so no target can be met
**Statement**: The FoxESS Cloud API rejects schedule writes where
`fdSoc < 11` or `minSocOnGrid > fdSoc` (errno 40257). All schedule
groups must be sanitised before writing.
**Rationale**: Undocumented API hard limits discovered empirically.
**Violation consequence**: Schedule write fails with errno 40257.
**Traces**: D-011;
`tests/test_init.py::TestSanitizeGroup::test_clamps_fd_soc_to_api_minimum`

### C-009: Schedule windows must not cross midnight
**Priority enforced**: P-003 (energy target) — same reasoning as
C-008; a schedule that can't be written can't deliver the target
**Statement**: All schedule group time windows must start and end on
the same calendar day. This is a FoxESS API limitation.
**Rationale**: The FoxESS schedule API uses `startHour/startMinute` and
`endHour/endMinute` without a date component.
**Violation consequence**: Undefined schedule behaviour on the inverter.
**Traces**: enforced directly by
`smart_battery/services.py::resolve_start_end` (raises
`ServiceValidationError` when the window would cross midnight) — no
dedicated D-NNN (simple validator);
`tests/test_init.py::TestResolveStartEnd::test_crosses_midnight_rejected`

### C-010: Placeholder schedule groups must be filtered
**Priority enforced**: P-003 (energy target) — API errno 42023 on
placeholder leakage blocks schedule writes
**Statement**: The FoxESS API always returns 8 schedule groups. Unused
slots must be filtered before writing back.
**Rationale**: Leaving zero-duration SelfUse groups causes API error
42023 ("Time overlap").
**Violation consequence**: Schedule write fails; stale schedule persists.
**Traces**: D-011;
`tests/test_init.py::TestIsPlaceholder`

### C-011: Extra fields must be stripped from schedule groups
**Priority enforced**: P-003 (energy target) — write rejection is
the same failure mode as C-008..C-010
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
**Priority enforced**: P-003 (energy target) and P-005 (operational
transparency) — this is the specific "you asked for X, we can't
give you X" signal both priorities depend on
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
**Priority enforced**: P-005 (operational transparency) — log-only
errors are invisible to the dashboard user and directly undermine
the transparency principle
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
**Traces**: D-029 (session-level error state surfaced via the
Smart Battery Status sensor), D-048 (sensor-listener write
failures surface as HA Repair issues);
`tests/test_services.py::TestErrorSurfacing`,
`tests/test_sensor_listener_safety.py::TestSensorListenerFailureSurfacesRepair` (6)

### C-038: Sensor-listener parameter parity
**Priority enforced**: P-005 (operational transparency) — when the
UI and the listener use different formulas, the user cannot trust
either display
**Statement**: Sensor display formulas that present operational timing
or state information must call the same algorithm functions as
listeners, with the same parameter lists. When a listener calls
`calculate_deferred_start()` or `calculate_discharge_deferred_start()`,
any sensor computing deferred start timing must pass identical
parameters. Simplified formulas are only acceptable for display values
that do not affect user understanding of timing.
**Rationale**: Two incidents (charge 2026-04-23, discharge 2026-04-22)
showed UI displaying incorrect phase and countdown values when sensor
formulas omitted parameters the listener used (headroom, taper profile,
net consumption, BMS temperature, grid export limit). The listener's
full algorithm accounts for all these factors; sensors that omit them
show a different deferred start time than the one the listener acts on,
violating C-020 (operational transparency).
**Violation consequence**: User sees "Scheduled" when the listener has
transitioned to "Charging", or wrong countdown values in dashboard cards.
Erodes trust and leads to unnecessary manual intervention.
**Traces**: (no dedicated D-NNN — enforced by code review + the
sensor-parity regression tests. Related:  D-002 / D-043 / D-044 all
depend on the listener algorithm and any sensor that displays their
phase/timing must use the same formula);
`tests/test_charge_deferred_sensor.py` (7 tests),
`tests/test_discharge_deferred_sensor.py` (4 tests);
fixes: c70addae (charge), 8e10b9a (discharge)

## Invariants — Testing

### C-028: Simulator over mocks, with instance isolation
**Priority enforced**: P-007 (engineering process integrity) — a
test double that matches the real API's behaviour is the foundation
for trusting the tests that verify the runtime priorities
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
**Priority enforced**: P-007 (engineering process integrity) —
HA-level behaviour is invisible to unit tests, so the tests that
protect P-005 operational transparency require a real HA
**Statement**: Behaviour that depends on HA's runtime environment —
config flow, entity lifecycle, service registration, Lovelace card
rendering, auth, coordinator polling — must have E2E tests against
a real HA instance (containerised). Unit tests with mocked HA
internals are insufficient for these code paths.
**Rationale**: HA's internal APIs (config entries, entity platforms,
service calls, frontend WebSocket) have undocumented behaviours and
version-specific quirks that mocks cannot reproduce. The E2E suite
(`tests/e2e/`) uses a Podman HA container with pre-seeded config to test
the actual integration lifecycle.
**Violation consequence**: Code works in unit tests but fails in a
real HA installation due to auth, entity discovery, shadow DOM, or
service registration differences.
**Traces**: `tests/e2e/test_e2e.py`, `tests/e2e/test_ui.py`

### C-030: E2E tests run in parallel before tagging
**Priority enforced**: P-007 (engineering process integrity) — a
slow feedback loop causes tests to be run less, defeating their
purpose as gates for the runtime priorities
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
**Priority enforced**: P-007 (engineering process integrity) — a
flaky test is evidence of a real race; masking it lets the race
persist into production behaviour
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
HA-dependent behaviour); `tests/e2e/ha_client.py::HAEventStream`
(drain-before-wait pattern eliminates event ordering races)

### C-032: Reproduce failure before fixing
**Priority enforced**: P-007 (engineering process integrity) —
without a failing test, "fixed" is unverifiable
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
**Priority enforced**: P-007 (engineering process integrity) —
every simulator deviation from production is a blind spot in the
test suite
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
**Priority enforced**: P-001 (no grid import) and P-002 (min SoC) —
a short safe horizon bounds the damage of HA downtime mid-session
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

## Invariants — Architecture

Structural rules enforced by automated tooling (semgrep, pre-commit
hooks) to prevent tech debt recurrence.

### C-034: Module size budget
**Priority enforced**: P-007 (engineering process integrity) —
monolithic modules breed coupling that makes future reviews less
able to notice constraint violations
**Statement**: No single `.py` file in `custom_components/foxess_control/`
may exceed 2000 lines. When a module approaches this limit, extract
cohesive functionality into a dedicated module.
**Rationale**: `__init__.py` grew to ~2500 lines by accretion before the
2026-04-21 remediation. Automated enforcement prevents recurrence.
**Violation consequence**: Pre-commit hook `check-module-size` fails;
code cannot be committed.
**Traces**: `.githooks/check-module-size`

### C-035: Typed config access
**Priority enforced**: P-007 (engineering process integrity) —
typed access catches key typos at lint time; scattered raw access
was the prior source of config-related defects
**Statement**: Config values must be read via `IntegrationConfig`
(accessed through `_cfg(hass)` in the brand layer), not via raw
`entry.options` access. New config fields must be added to
`IntegrationConfig` in `domain_data.py` before use. Only `__init__.py`
(builds config), `domain_data.py` (defines config), `config_flow.py`
(reads options for UI), and `diagnostics.py` (dumps raw options) may
access `entry.options` directly.
**Rationale**: Raw `entry.options` access scatters default values and
type conversions across multiple files, creating inconsistency. The
`IntegrationConfig` frozen dataclass provides a single source of truth,
built once at setup time.
**Violation consequence**: Semgrep rule `no-raw-entry-options` fails
pre-commit.
**Traces**: `.semgrep/foxess-architecture.yaml::no-raw-entry-options`

### C-036: Typed domain data access
**Priority enforced**: P-007 (engineering process integrity) — same
rationale as C-035 at a different layer
**Statement**: Runtime state in `hass.data[DOMAIN]` must be accessed via
the `_dd(hass)` helper, not by raw dict lookup. Only `__init__.py`
(setup/teardown), `_helpers.py` (helper definition), and `domain_data.py`
(type definition) may reference `hass.data[DOMAIN]` directly.
**Rationale**: Raw dict access was the source of the bridge-layer tech
debt. Typed access via `_dd()` catches key typos at lint time and
provides IDE autocomplete.
**Violation consequence**: Semgrep rule `no-raw-hass-data-access` fails
pre-commit.
**Traces**: `.semgrep/foxess-architecture.yaml::no-raw-hass-data-access`

### C-040: Brand-agnostic code has brand-agnostic tests
**Priority enforced**: P-006 (brand portability) — the test-level
complement to C-021 (where code lives) and C-039 (how modules are
decoupled)
**Statement**: Tests exercising code under `smart_battery/` must be
runnable without loading any brand-specific module. The canonical
test double is `smart_battery.testing.FakeAdapter` (satisfies the
`InverterAdapter` Protocol; records every call). Cross-layer tests
that deliberately exercise the FoxESS adapter + simulator
integration are allowed but go in separate files whose names
explicitly signal the coupling (e.g. `test_foxess_adapter.py`,
`test_inverter.py`) — they are NOT "smart_battery tests".
`tests/test_smart_battery_agnostic.py` is the template for a
brand-agnostic test module.
**Rationale**: A test that reaches through FakeAdapter into a
FoxESS-specific response shape cannot prove "the listener is
brand-agnostic" — it only proves "the listener works against
FoxESS". When Huawei / SolaX / Sungrow adapters arrive, every
`smart_battery/` behaviour needs to be re-tested against them; if
the existing tests are coupled to FoxESS, that re-testing means
either duplicating the tests per brand or discovering at
integration time that the listener had latent FoxESS assumptions.
The FakeAdapter pattern proves the agnostic contract holds
*before* the second brand exists.
**Violation consequence**: Brand-agnostic regressions that slip
through until the second brand integration, where they surface
as integration-time failures requiring retrofit fixes — exactly
the P-006 rework pattern C-021 / C-039 were meant to prevent.
**Traces**: C-021 (where code lives), C-039 (import-direction
invariant), D-022 (adapter as injection seam);
`smart_battery/testing.py::FakeAdapter`,
`tests/test_smart_battery_agnostic.py` (11 tests proving the
FakeAdapter satisfies the Protocol + canonical recording
patterns; 1 inline check that asserts this module does not
import brand-specific code).

### C-039: No brand-layer imports in smart_battery/
**Priority enforced**: P-006 (brand portability) — the
dependency-inversion form of C-021
**Statement**: The `smart_battery/` package must not import from any
brand-specific module. Concretely, no file under `smart_battery/` (or
the vendored `custom_components/foxess_control/smart_battery/`) may
`import` or `from ... import` any of:
`custom_components.foxess_control.foxess.*`,
`custom_components.foxess_control.foxess_adapter`,
`custom_components.foxess_control.coordinator`,
`custom_components.foxess_control._services`, or
`custom_components.foxess_control._helpers`. Brand-specific state or
behaviour enters `smart_battery/` only through (a) the
`InverterAdapter` Protocol passed as a parameter, (b) the
`EntityAdapter` / `EntityCoordinator` generic helpers, or (c) typed
data values crossing the boundary via the protocol's method
signatures.
**Rationale**: C-021 says where brand-agnostic code goes; C-039 says
how the layers stay decoupled. Import direction is the concrete
mechanism for dependency inversion: `smart_battery/` depends on the
abstract `InverterAdapter` Protocol; brand packages depend on
`smart_battery/` (and implement the Protocol); nothing flows the
other way. A `from custom_components.foxess_control.foxess import
X` in `smart_battery/` would couple the common package to a FoxESS
type and silently prevent Huawei / SolaX / Sungrow from reusing it
without modification.
**Violation consequence**: Pre-commit fails. The semgrep rule
`no-brand-imports-in-smart-battery` in
`.semgrep/foxess-architecture.yaml` greps for the forbidden import
patterns and blocks the commit.
**Traces**: C-021 (where code belongs — the companion principle),
C-015 (vendored copy sync);
`.semgrep/foxess-architecture.yaml::no-brand-imports-in-smart-battery`

### C-037: Grid export limit awareness in discharge timing
**Priority enforced**: P-003 (energy target) and P-004 (feed-in
maximisation) — without this awareness, feed-in targets go unmet
on export-limited sites; the constraint also protects P-001 by
preventing late starts that would force low paced power near window
end
**Statement**: When a hardware grid export limit is configured
(`grid_export_limit_w > 0`), the discharge deferral calculation must
cap the effective export rate at `grid_export_limit_w / 1000` kW in
both the SoC deadline and feed-in energy deadline calculations.
During active discharge, the system must request `max_power_w`
directly instead of computing paced discharge power, allowing the
inverter's built-in export limiter to enforce the constraint without
double-limiting.
**Rationale**: When a hardware export limit exists (DNO enforcement
or inverter firmware config), actual grid export becomes
`min(discharge_power, hardware_limit)`. Software pacing assumes
`discharge ≈ export`, which is violated by the hardware limit.
Uncapped deferral starts too late (insufficient time to export at the
limited rate). Paced active discharge double-limits — the inverter
delivers less export AND less house-load contribution than possible.
**Violation consequence**: Deferral deadline too late — discharge
session incomplete or SoC target missed. Paced active discharge
under-utilises battery contribution to house load and export.
**Traces**: C-001 (deferral timing affects no-import), D-002
(deferred start), D-005 (feedin deadline), D-044;
`smart_battery/algorithms.py::calculate_discharge_deferred_start`
(lines 538–539 SoC cap, 566–567 feedin cap);
`smart_battery/listeners.py::_discharge_listener` (max power path);
`tests/test_smart_battery_algorithms.py::TestGridExportLimitDeferral`,
`tests/test_sensor.py::test_deferred_countdown_with_grid_export_limit_and_consumption`

## Invariants — Hardware Behaviour

Constraints satisfied by inverter firmware behaviour, validated via
the simulator model (C-028, C-033) and soak test scenarios.

### C-023: Solar-first power routing during ForceCharge
**Priority enforced**: P-003 (energy target) — the invariant is
about charging efficiency: solar should reach the battery before
grid energy does, so the energy target is met with minimal grid
spend
**Statement**: During forced charging, the inverter firmware routes
solar generation to house load first, then to the battery, before
drawing from the grid. The effective grid import is
`grid_import = bat_charge - solar_to_bat + grid_to_load`, where
`solar_to_bat = solar - min(solar, load)`.
**Rationale**: Charging at full grid power while solar is producing
wastes free energy — solar output goes to the grid at the feed-in
rate (often zero or low) instead of into the battery for free.
The inverter handles this at the hardware level; no software charge
reduction is needed. D-043 (charge re-deferral) provides the
software-side complement: when solar pushes SoC ahead of schedule,
the listener switches to self-use to maximise free solar contribution.
**Status**: Satisfied by hardware. Discharge observation (2026-04-15)
confirmed `grid_export = discharge + solar - load`. Simulator model
(`simulator/model.py` ForceCharge block) implements the same
solar-first routing. Three soak test scenarios validate the
end-to-end behaviour with solar present during charging.
**Violation consequence**: User pays for grid energy that solar could
have provided for free, directly reducing ROI.
**Traces**: D-043 (charge re-deferral);
`simulator/model.py::InverterModel.tick` (ForceCharge solar routing);
`tests/soak/test_scenarios.py::test_charge_with_solar`,
`tests/soak/test_scenarios.py::test_charge_solar_exceeds_target`,
`tests/soak/test_scenarios.py::test_charge_solar_then_spike`
