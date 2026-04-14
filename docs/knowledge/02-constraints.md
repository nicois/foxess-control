---
project: FoxESS Control
level: 2
last_verified: 2026-04-14
traces_up: [01-vision.md]
traces_down: [03-architecture.md, 04-design/]
---
# Constraints

Invariants that must hold across all implementations. Each constraint
has a stable ID, a precise statement, a rationale, and the consequence
of violation.

> **Status**: Initial generation — awaiting owner review and correction.

## Safety Constraints

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
safety floor; (b) the listener layer (`__init__.py`) requires 2
consecutive checks with SoC at/below min_soc before ending the session,
preventing premature termination from transient SoC dips.
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

## Data Integrity Constraints

### C-004: WebSocket values are in watts; coordinator expects kW
**Statement**: All WebSocket power values (`solar.power.value`,
`grid.power.value`, `bat.power.value`, `load.power.value`) are strings
representing **watts**. They must be divided by 1000 before injection
into the coordinator (which uses kW).
**Rationale**: The REST API returns kW. The WebSocket returns watts as
strings. Verified empirically 2026-04-14: WS `bat=553` vs REST
`batDischargePower=0.722 kW`.
**Violation consequence**: Power values displayed 1000x too large or
too small on dashboard cards; taper profile corruption from implausible
observations.
**Traces**: D-010;
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_real_world_sample`

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

## FoxESS API Constraints

### C-008: fdSoc >= 11 and minSocOnGrid <= fdSoc
**Statement**: The FoxESS Cloud API rejects schedule writes where
`fdSoc < 11` or `minSocOnGrid > fdSoc` (errno 40257). All schedule
groups must be sanitised before writing.
**Rationale**: Undocumented API hard limits discovered empirically.
The API sometimes returns groups violating its own limits on read, but
rejects them on write.
**Violation consequence**: Schedule write fails with errno 40257,
leaving stale schedule on inverter.
**Traces**: D-011;
`tests/test_init.py::TestSanitizeGroup::test_clamps_fd_soc_to_api_minimum`

### C-009: Schedule windows must not cross midnight
**Statement**: All schedule group time windows must start and end on
the same calendar day (no midnight crossing). This is a FoxESS API
limitation, not a universal `smart_battery` constraint.
**Rationale**: The FoxESS schedule API uses `startHour/startMinute` and
`endHour/endMinute` without a date component. Midnight-crossing windows
are ambiguous.
**Violation consequence**: Undefined schedule behaviour on the inverter.
**Traces**: D-011;
`tests/test_init.py::TestResolveStartEnd::test_crosses_midnight_rejected`

### C-010: Placeholder schedule groups must be filtered
**Statement**: The FoxESS API always returns 8 schedule groups. Unused
slots appear as `workMode: "Invalid"`, empty string, or zero-duration
SelfUse groups. These must be filtered before writing back.
**Rationale**: Leaving zero-duration SelfUse groups causes API error
42023 ("Time overlap").
**Violation consequence**: Schedule write fails; stale schedule persists.
**Traces**: D-011;
`tests/test_init.py::TestIsPlaceholder`

### C-011: Extra fields must be stripped from schedule groups
**Statement**: Schedule groups returned by `scheduler/get` include extra
fields (`id`, `properties`) that `scheduler/enable` rejects. Groups
must be sanitised to the known-good field set before writing.
**Rationale**: API read/write field sets are inconsistent.
**Violation consequence**: Schedule write fails with errno 40257.
**Traces**: D-011;
`tests/test_init.py::TestSanitizeGroup::test_strips_unknown_keys`

## Operational Constraints

### C-012: SoC unavailability cancels session after 15 minutes
**Statement**: If the SoC entity is unavailable for 3 consecutive checks
(3 x 5 min = 15 min), the smart session is cancelled.
**Rationale**: Operating blind without SoC data risks over-charging or
over-discharging.
**Violation consequence**: Battery damage from uncontrolled charge/discharge.
**Traces**: D-019; `smart_battery/const.py:52`;

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

### C-019: Discharge SoC unavailability is unprotected
**Statement**: Unlike the charge path (C-012), the discharge listener
does not count consecutive SoC-unavailable checks and has no abort
threshold. When SoC is unavailable, the discharge listener silently
skips the check and returns.
**Rationale**: Unknown — likely an omission rather than a deliberate
design choice. The inverter remains in forced discharge mode with no
SoC monitoring.
**Violation consequence**: Inverter stays in forced discharge
indefinitely with no SoC feedback, risking over-discharge.
**Traces**: -- (no test, no design doc — **known gap**)

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
**Traces**: D-021

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
