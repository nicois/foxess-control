---
project: FoxESS Control
level: 4
feature: Adaptive BMS Taper Model
last_verified: 2026-04-21
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

### D-014: Multiplicative temperature correction factor
**Decision**: Temperature effects are modelled as an independent multiplicative
factor applied to the SoC-based taper ratio: `effective_ratio = soc_ratio * temp_factor`.
Temperature bins are keyed by integer °C on the same TaperProfile.
**Context**: At low temperatures (< 20°C), the BMS reduces charge acceptance
to prevent lithium plating. At 19°C the observed charge rate is ~8.5 kW
instead of 10 kW nominal — a reduction invisible to SoC-only profiling.
**Rationale**: SoC taper (electrochemical CV phase) and temperature taper
(kinetic lithium plating protection) are physically independent phenomena
that multiply naturally. A 2D model (SoC × temp) would have O(100 × 40)
bins with extremely sparse data. The multiplicative model keeps the existing
SoC profile useful day-one while temperature converges independently in
~10-15 integer °C bins. The temperature factor is isolated by dividing out
the SoC ratio: `temp_factor = (actual/requested) / soc_ratio`.
**Alternatives considered**:
- Temperature-bucketed profiles (separate profile per range): 4× convergence
  time, data sparsity at uncommon temperatures
- 2D SoC×temp indexing: O(4000) bins, years to converge
- Hard threshold clamp (the removed `_apply_cold_temp_limit`): binary step
  at 16°C, prevented taper learning, one-directional data
**Traces**: C-014;
`tests/test_taper.py::TestRecordChargeTemp`, `tests/test_taper.py::TestTempFactor`

### D-015: 10-minute stability gate for temperature observations
**Decision**: Temperature data points are only recorded after the inverter
has delivered < 95% of requested power for 10 consecutive minutes (measured
as consecutive polling ticks via a streak counter).
**Context**: Transient power reductions (ramp-up, cloud cover, grid
fluctuations) are not BMS temperature limiting. Recording them would
corrupt the temperature profile with false positives.
**Rationale**: 10 minutes filters most transients while capturing genuine
BMS curtailment (which is sustained). The 95% threshold provides noise
margin. The streak-counter approach works with both the 5-minute charge
and 1-minute discharge polling intervals (2 and 10 ticks respectively).
SoC-based taper recording continues unconditionally — only temperature
recording is gated.
**Traces**: C-014;
`smart_battery/listeners.py::_record_taper_observation`

## Key Behaviours

- Nearest-neighbour interpolation within +/-5 SoC for missing bins.
- Edge extrapolation: if all data is in [80, 100], queries at SoC 75
  use the ratio from SoC 80.
- Separate charge and discharge dictionaries (BMS taper is asymmetric).
- Profile persists to HA Store, surviving restarts.
- Temperature correction: multiplicative factor from integer-°C-indexed bins.
- Temperature bins use MIN_TEMP_TRUST_COUNT (3) and TEMP_NEIGHBOR_RANGE (3).
- Graceful degradation: temp_c=None returns factor 1.0 (no correction).
- 10-minute stability gate filters transient power reductions from temp data.

## Edge Cases

- **Empty profile**: All ratios return 1.0 (no taper assumed).
- **Corrupt profile on load**: Auto-reset via plausibility check.
- **SoC > 100 or < 0**: Clamped to [0, 100] bucket.
- **No BMS temperature**: All temp factors return 1.0 (transparent fallback).
- **Cold temp with empty SoC profile**: SoC ratio defaults to 1.0, so temp
  factor captures the full observed taper until SoC data accumulates.
