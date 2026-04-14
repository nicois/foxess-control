---
project: FoxESS Control
level: 4
feature: Smart Charge
last_verified: 2026-04-14
traces_up: [../02-constraints.md, ../03-architecture.md]
traces_down: [../05-coverage.md, ../06-tests.md]
---
# Design: Smart Charge

## Overview

Smart charge paces grid charging power across a time window to reach a
target SoC by the window end. It defers the start of forced charging
until a calculated deadline, accounts for household consumption
(which reduces effective charge rate), and catches up by bursting to
max power when falling behind the ideal trajectory.

## Design Decisions

### D-006: Trajectory tracking with catch-up burst
**Decision**: Compare actual energy charged against an ideal linear
trajectory. When behind schedule (actual < ideal - tolerance), burst
to max power until caught up.
**Context**: BMS taper at high SoC reduces actual charge acceptance.
External loads consume grid power intended for the battery. Both cause
the charge to fall behind.
**Rationale**: Pacing alone can't guarantee the target is reached if
the effective charge rate is lower than expected. The catch-up burst
provides a self-correcting mechanism.
**Alternatives considered**:
- Increase headroom globally: rejected because it wastes cheap-rate
  hours (charges too fast, idles at the end)
- PID controller: rejected as over-engineered for a 5-minute update
  interval
**Traces**: C-014;
`tests/test_smart_battery_algorithms.py::TestCalculateChargePower::test_trajectory_behind_triggers_max_power`

### D-007: Consumption headroom in deferred start
**Decision**: When calculating deferred start, reduce effective charge
power by `max(consumption, 10% * max_power)`.
**Context**: Household consumption during charging reduces the power
available to the battery. If deferred start doesn't account for this,
charging starts too late.
**Rationale**: The 10% floor ensures margin even when current
consumption is low (it may spike overnight, e.g., hot water heater).
**Alternatives considered**:
- Use actual consumption only: rejected because overnight loads are
  unpredictable
- Fixed consumption estimate: rejected in favour of hybrid approach
**Known issue**: When a taper profile is available, the deferred start
calculation uses `taper_profile.estimate_charge_hours()` which does NOT
subtract consumption from the charge power. The time buffer
(`hours / (1 - headroom)`) partially compensates but is insufficient.
Example: with 5kW max power and 1kW consumption, the non-taper path
reserves 1kW for consumption (20% reduction) but the taper path only
applies an 11% time buffer. This can cause the taper-aware deferred
start to be ~20 minutes too late, risking a missed charge target.
**Traces**:
`tests/test_smart_battery_algorithms.py::TestCalculateDeferredStart::test_consumption_brings_start_earlier`

## Key Behaviours

- Charge power adjustment interval is 5 minutes (vs 1 minute for
  discharge) because charge has lower immediate risk than discharge.
- Negative consumption (solar excess) is treated as zero — conservative
  choice to avoid over-deferring charge start.
- Tolerance for trajectory check shrinks as window closes (smaller
  deficit is tolerated early, but any deficit late triggers burst).

## Edge Cases

- **Already at target SoC**: Returns minimum power (100W), effectively
  idling until the window ends.
- **Zero remaining time**: Returns max power (best effort).
- **Taper corruption**: Plausibility check auto-resets corrupted profiles
  that would cause permanent max-power burst.
