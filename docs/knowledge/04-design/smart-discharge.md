---
project: FoxESS Control
level: 4
feature: Smart Discharge
last_verified: 2026-04-24
traces_up: [../02-constraints.md, ../03-architecture.md]
traces_down: [../05-coverage.md, ../06-tests.md]
---
# Design: Smart Discharge

## Overview

Smart discharge paces battery discharge power across a user-defined time
window to meet SoC and feed-in energy targets without importing from
the grid. It defers the start of forced discharge as long as possible
(staying in self-use mode), then switches to forced discharge only when
a calculated deadline requires it.

## Design Decisions

### D-001: Strict priority ordering (P1 > P2 > P3 > P4)
**Decision**: Discharge power is computed using strict priorities:
P1 no-import floor > P2 min-SoC protection > P3 energy target >
P4 maximise feed-in. Lower priorities never override higher ones.
**Context**: Users have conflicting objectives: maximise feed-in revenue,
avoid grid import costs, protect battery longevity. A single power value
must satisfy all.
**Rationale**: Safety constraints (no import, min SoC) must be absolute,
not traded off against revenue. A weighted multi-objective approach would
allow small grid imports for marginal feed-in gains, which users find
unacceptable.
**Alternatives considered**:
- Weighted multi-objective: rejected because even small grid imports are
  visible on smart meters and frustrate users
- Configurable priority order: rejected as too complex for the benefit
**Traces**: C-001, C-002;
`tests/test_smart_battery_algorithms.py::TestCalculateDischargePower`

### D-002: Deferred start with self-use
**Decision**: Stay in self-use mode as long as possible, switching to
forced discharge only when a deadline calculation shows it's necessary.
**Context**: During forced discharge, if paced power is below house load,
the grid supplies the shortfall. Self-use mode lets the inverter
intelligently supply house load from battery without exporting.
**Rationale**: Maximises the self-use period (avoiding grid import risk)
while still hitting the discharge target by the end of the window.
For feedin-limited sessions, the feedin deadline (not the SoC deadline)
governs deferral because the session stops at the feedin target, not at
min_soc — see D-005. This is critical because large batteries with
small feedin limits would otherwise start immediately at low paced
power, creating a long window of C-001 import risk.
**Alternatives considered**:
- Immediate forced discharge at window start: rejected because low
  paced power causes grid import when house load exceeds it
- Complex house-load-aware forced discharge: rejected in favour of
  the simpler deferred-start approach
**Traces**: C-001;
`tests/test_smart_battery_algorithms.py::TestCalculateDischargeDeferredStart`,
`tests/test_smart_battery_algorithms.py::TestCalculateDischargeDeferredStart::test_taper_consumption_affects_soc_deadline`

### D-003: End-of-discharge guard (10 min early switch to self-use)
**Decision**: When remaining energy above min_soc can't sustain the
safety floor for ~10 minutes, switch from forced discharge to self-use.
**Context**: Near the end of a discharge window, paced power drops to
~100W minimum — well below typical house load. Continuing forced
discharge at this point causes grid import.
**Rationale**: Switching to self-use 10 minutes early lets the inverter
serve house load directly from the battery without the forced-discharge
power floor constraint.
**Alternatives considered**:
- Continue forced discharge to the end: rejected because the last
  10 minutes of grid import offset the feed-in revenue
**Traces**: C-001;
`tests/test_smart_battery_algorithms.py::TestShouldSuspendDischarge`

### D-004: Peak consumption tracking with exponential decay
**Decision**: Track highest observed household consumption with
`PEAK_DECAY_PER_TICK` applied at the discharge check interval
(1 minute). The decay factor is 0.85 (half-life ~4.3 min). Floor
discharge power at `peak * 1.5`. The peak value is updated via
`peak = max(peak * PEAK_DECAY_PER_TICK, current_consumption)` on
each discharge check tick, ensuring the peak tracks upward immediately
on spikes and decays exponentially between them.
**Context**: House load varies unpredictably. A single spike shouldn't
permanently inflate the discharge floor, but recent spikes should
be respected.
**Rationale**: At 1-minute ticks, 0.85 decay gives half-life of ~4.3
minutes (`0.85^4.27 = 0.5`). This is responsive enough to protect
against active spikes while adapting within minutes when loads decrease.
The 1.5x safety factor provides margin above the tracked peak.
**Alternatives considered**:
- Fixed consumption estimate: rejected because household load is highly
  variable
- Sliding window max: rejected in favour of simpler EMA approach
**Traces**: C-001;
`tests/test_smart_battery_algorithms.py::TestCalculateDischargePower`,
`tests/test_smart_battery_algorithms.py::TestDischargePowerPeakSafetyFloor`

### D-005: Feed-in energy budget spreading with early-stop
**Decision**: When `feedin_energy_limit_kwh` is set, cap the discharge
rate so the export budget is spread across the full window rather than
exhausted early. Additionally, track the observed export rate between
polls and schedule a one-shot stop when the remaining budget will be
exhausted before the next poll — preventing overshoot of the limit.

When a feedin limit is set, the deferred start SoC deadline is always
computed from the feedin drain time (not the full SoC drain), because
the session will stop at the feedin target — the full SoC drain is not
the binding constraint. This maximises self-use time (D-002) and
minimises the forced-discharge window where paced power is below max,
which is the primary import risk vector (C-001).

The deferred start is re-evaluated on every listener tick (60s), so if
conditions change during the deferred phase (e.g. a load spike causes
the SoC drain to become the binding constraint), the system starts
forced discharge earlier than originally planned.

**Context**: Some tariffs limit export kWh per day. Without spreading,
the session exhausts the limit in the first hour and stops, failing to
reach min_soc. Without the early-stop, the 5-minute polling interval
means up to 5 minutes of excess export after the limit is reached.
**Rationale**: Spreading ensures both the energy target and the SoC
target are achievable within the window. The early-stop uses the
observed rate (from the cumulative feed-in counter) rather than the
configured discharge power, since actual export is reduced by house
consumption and inverter grid-export limits. A one-shot timer at the
projected completion time stops discharge precisely.

Running at low paced power for a long window (e.g. 1.5 kW for 51 min)
creates sustained import risk: any household load spike above the paced
power draws from the grid for the entire spike duration. Running at
full power for a short burst (e.g. 10.5 kW for 7 min) has massive
headroom and much shorter exposure. Deferring until the feedin deadline
and then discharging at higher power is therefore strictly safer than
starting immediately at low paced power (C-001, D-001 P1 priority).
**Alternatives considered**:
- Export at max until limit hit, then switch to self-use: rejected
  because min_soc target would be missed
- Stop at next poll after limit reached: rejected because up to
  5 minutes of excess export may incur penalties on capped tariffs
- Skip feedin cap on SoC deadline for tight windows (pre-2026-04-22):
  rejected — caused immediate start at low paced power for the full
  window, creating sustained C-001 violation risk. The original
  concern (over-deferring) was based on the uncapped SoC deadline,
  which is irrelevant when feedin is the binding constraint
The feed-in baseline (the coordinator's `feedin` value at session
start) is captured on the listener's first tick rather than at session
setup time, because the coordinator data may not be populated yet when
the service call runs.
**Traces**: C-001, D-002;
`tests/test_smart_battery_algorithms.py::TestDischargePowerFeedinConstraint`,
`tests/test_smart_battery_algorithms.py::TestCalculateDischargeDeferredStart::test_tight_window_feedin_does_not_over_defer`,
`tests/test_smart_battery_algorithms.py::TestCalculateDischargeDeferredStart::test_large_battery_feedin_defers_by_feedin_deadline`,
`tests/test_services.py::TestFeedinEnergyLimit`,
`tests/test_services.py::test_feedin_baseline_not_captured_at_session_start`,
`tests/e2e/test_e2e.py::TestFeedinPacing::test_feedin_power_adjusts_over_time`

### D-023: Progressive schedule extension (safe horizon)
**Decision**: Instead of setting the inverter schedule end time to the
full user-requested window, `FoxESSCloudAdapter.apply_mode` computes a
safe horizon: `energy_above_min / drain_rate / safety_factor`. The
schedule end is `min(now + safe_hours, window_end)`. This is
recomputed on each power adjustment (both fast and slow paths).
**Context**: If HA loses connectivity, the inverter runs the schedule
unsupervised. With the full window end, it drains to fdSoc. With a
safe horizon, the schedule expires and the inverter reverts to self-use.
**Rationale**: The safety factor (1.5×) provides 33% margin. Between
power adjustments the margin *grows* (SoC drops proportionally but the
schedule end stays fixed), so there is no need for heartbeat extensions
on every tick — only when `apply_mode` is already called.
**Alternatives considered**:
- Fixed 5-minute extension on every tick: rejected — unnecessary API
  calls and doesn't scale with battery headroom
- HA-side watchdog timer: rejected — doesn't protect against HA
  being completely unreachable
**Traces**: C-024, C-027;
`smart_battery/algorithms.py::compute_safe_schedule_end`,
`tests/test_smart_battery_algorithms.py` (via `compute_safe_schedule_end`)

### D-044: Grid export limit awareness
**Decision**: When a `grid_export_limit_w` is configured (> 0), the
discharge system adapts in two ways:
1. **Deferral**: `calculate_discharge_deferred_start` caps the
   effective export rate at `grid_export_limit_w / 1000` kW in both
   the SoC deadline and feed-in energy deadline calculations, producing
   a more conservative (earlier) deferred start.
2. **Active discharge**: The listener uses `max_power_w` directly
   instead of computing a paced power value. The inverter's own
   grid-export limiter constrains actual export, so pacing would
   double-limit — requesting less than the inverter can deliver while
   the grid-export limiter independently caps export, resulting in
   under-utilisation.
**Context**: Some inverter installations have grid export limits
enforced by the DNO or configured in the inverter firmware. The
software must account for this when estimating how long discharge
will take and when to start, but must not fight the hardware limiter
during active discharge.
**Rationale**: Software pacing assumes discharge_power ≈ grid_export.
With a hardware export limit, actual export is `min(discharge, limit)`,
making pacing calculations incorrect. Running at max power and letting
the hardware limit handle export produces the correct export rate
while maximising the battery's contribution to house load.
**Alternatives considered**:
- Software pacing to the export limit: rejected because it would
  reduce both export AND house-load contribution, when only export
  needs limiting
- Ignoring the limit in deferral: rejected because the deferred
  start would be too late — the session would run out of time to
  export the required energy at the limited rate
**Traces**: C-001 (no-import), D-002 (deferred start), D-005 (feedin budget);
`tests/test_sensor.py::TestDischargeDeferredCountdown::test_deferred_countdown_with_grid_export_limit_and_consumption`,
`tests/test_sensor.py::TestDischargeDeferredCountdown::test_deferred_countdown_grid_export_limit_caps_export_rate`,
`tests/test_sensor.py::TestDischargeDeferredCountdown::test_deferred_countdown_grid_export_limit_feedin_deadline`

## Key Behaviours

- Deferred start has doubled headroom when feed-in limit is set (up to
  40% vs normal 10%) because variable house consumption reduces net
  export unpredictably.
- Discharge check interval is 1 minute (vs 5 minutes for charge)
  because discharge power changes have immediate grid-import risk.
- After suspension (SoC at min), the session re-evaluates on every
  check and resumes if SoC recovers (e.g., from solar input).
- Temperature-aware deferred start: `bms_temp_c` is passed through to
  `calculate_discharge_deferred_start` and `estimate_discharge_hours`,
  allowing the taper model's temperature correction factor to adjust
  discharge time estimates for BMS current limiting at low temperatures.
- Circuit breaker protection (D-025): discharge checks are wrapped in
  `_with_circuit_breaker`. With 1-minute ticks, tier 1 opens at 3 min,
  tier 2 aborts at 8 min.

## Edge Cases

- **House load > max discharge power**: Floor at consumption × 1.5
  means requesting more than inverter can deliver. Clamped to
  max_power_w.
- **Feed-in budget already exhausted**: Returns minimum power (100W)
  plus any house load offset.
- **Zero remaining time**: Returns max_power_w (best effort).
