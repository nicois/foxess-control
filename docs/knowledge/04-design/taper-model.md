---
project: FoxESS Control
level: 4
feature: Adaptive BMS Taper Model
last_verified: 2026-04-14
traces_up: [../02-constraints.md, ../03-architecture.md]
traces_down: [../05-coverage.md, ../06-tests.md]
---
# Design: Adaptive BMS Taper Model

## Overview

The battery management system (BMS) limits charge/discharge throughput
at high and low SoC (constant-voltage phase). The taper model learns
actual acceptance ratios at each SoC level via exponential moving average,
improving time estimates and power pacing.

## Design Decisions

### D-011: SoC-indexed histogram with EMA smoothing
**Decision**: Maintain a per-SoC-percent histogram of observed
`actual_power / requested_power` ratios, smoothed with EMA
(alpha = 0.3).
**Context**: At SoC > 90%, the BMS may accept only 60-80% of requested
charge power. At SoC < 15%, discharge is similarly limited. Without
this knowledge, time estimates are wrong.
**Rationale**: EMA adapts in 3-5 observations per SoC bin. The per-SoC
granularity captures the non-linear taper curve. Alpha 0.3 balances
responsiveness vs noise.
**Alternatives considered**:
- Fixed taper curve: rejected because BMS behaviour varies by battery
  age, temperature, and cell chemistry
- Machine learning model: rejected as over-engineered for the available
  data rate (one observation per SoC per session)
**Traces**: C-014;
`tests/test_taper.py::TestRecordCharge`

### D-012: Quality gates on taper observations
**Decision**: Ignore observations where `requested < 500W` (transients),
`actual < 50W` (sensor errors), or `count < 2` (insufficient trust).
The listener layer pre-filters at the same 500W threshold and also
skips recording during suspended discharge (where actual power is
zero). The taper profile is persisted to HA Store every
`_TAPER_SAVE_EVERY_N` (5) observations in both charge and discharge
paths.
**Context**: During ramp-up/ramp-down, power readings are noisy. Sensor
errors can report near-zero actual when real power is flowing.
**Rationale**: Garbage-in protection. One bad observation with EMA
alpha 0.3 takes ~5 observations to wash out, so prevention is better
than correction. The listener pre-filter avoids a coordinator lookup
when the observation would be rejected anyway. The save interval
balances persistence (surviving restarts) against HA Store I/O.
**Alternatives considered**:
- Lower thresholds: rejected after observing 1000x unit mismatch bug
  that corrupted profiles with ~0.1% ratios
- Outlier detection: rejected in favour of simpler hard thresholds
**Traces**: C-014;
`tests/test_taper.py::TestRecordCharge::test_ignores_implausibly_low_actual`

### D-013: Plausibility check with auto-reset
**Decision**: On profile load, check `is_plausible()` — if median
trusted ratio <= 0.1, discard the profile and start fresh.
**Context**: Beta.14 had a unit mismatch bug (WS watts treated as kW)
that recorded ratios of ~0.001. These persisted across restarts and
permanently broke pacing.
**Rationale**: Auto-reset is preferable to requiring user intervention.
The median check catches systemic corruption while allowing normal
per-bin variation.
**Alternatives considered**:
- Manual reset only: rejected because users may not notice degraded
  pacing
- Per-bin reset: rejected because systemic corruption affects all bins
**Traces**: C-014;
`tests/test_taper.py::TestIsPlausible`

## Key Behaviours

- Nearest-neighbour interpolation within +/-5 SoC for missing bins.
- Edge extrapolation: if all data is in [80, 100], queries at SoC 75
  use the ratio from SoC 80.
- Separate charge and discharge dictionaries (BMS taper is asymmetric).
- Profile persists to HA Store, surviving restarts.

## Edge Cases

- **Empty profile**: All ratios return 1.0 (no taper assumed).
- **Corrupt profile on load**: Auto-reset via plausibility check.
- **SoC > 100 or < 0**: Clamped to [0, 100] bucket.
