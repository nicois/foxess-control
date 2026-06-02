---
project: FoxESS Control
level: 4
feature: Smart Charge
last_verified: 2026-06-02
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
**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: pacing
**Alternatives considered**:
- Increase headroom globally: rejected because it wastes cheap-rate
  hours (charges too fast, idles at the end)
- PID controller: rejected as over-engineered for a 5-minute update
  interval
**Traces**: C-014;
`tests/test_smart_battery_algorithms.py::TestCalculateChargePowerTrajectory::test_behind_schedule_returns_max`

### D-007: Consumption headroom in deferred start
**Decision**: When calculating deferred start, reduce effective charge
power by `max(consumption, 10% * max_power)`.
**Context**: Household consumption during charging reduces the power
available to the battery. If deferred start doesn't account for this,
charging starts too late.
**Rationale**: The 10% floor ensures margin even when current
consumption is low (it may spike overnight, e.g., hot water heater).
**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: pacing
**Alternatives considered**:
- Use actual consumption only: rejected because overnight loads are
  unpredictable
- Fixed consumption estimate: rejected in favour of hybrid approach
**Traces**: C-001 (discharge deferred start timing);
`tests/test_smart_battery_algorithms.py::TestCalculateDeferredStart::test_consumption_affects_deferral`,
`tests/test_smart_battery_algorithms.py::TestCalculateDeferredStart::test_taper_consumption_affects_deferral`

### D-028: Unreachable charge target detection
**Decision**: Expose `is_charge_target_reachable` as a boolean attribute
(`charge_target_reachable`) on the Smart Battery Status sensor during
active charge sessions.
**Context**: When the target SoC becomes unreachable mid-session (BMS
taper, consumption spike, late start), the user has no way to know
until the window ends and the target was missed.
**Rationale**: The check reuses the same formula as deferred start
(energy-needed vs effective-power * remaining-time), accounting for
taper profile and consumption headroom — zero additional computation.
Exposing it as a sensor attribute rather than a separate entity keeps
the entity count low and avoids lifecycle complexity.
**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: safety
**Alternatives considered**:
- Separate binary sensor: rejected because entity lifecycle management
  for a transient per-session value adds complexity
- HA persistent notification: rejected as too intrusive for an
  informational signal
**Traces**: C-022, C-020;
`tests/test_smart_battery_algorithms.py::TestIsChargeTargetReachable`

### D-046: Outlier-robust feasibility check for reachability
**Decision**: `is_charge_target_reachable` blends the taper-integrated
charge-hours estimate with a median-ratio linear estimate across the
traversed SoC range and takes the minimum.  Pacing (`calculate_charge_power`,
`calculate_deferred_start`) continues to use the full taper-integrated
estimate unchanged.
**Context**: Live 2026-04-24 02:53 UTC — a smart charge with plentiful
solar surplus and ~65 min remaining for a 15% uplift on a 42 kWh battery
was reported as unreachable.  The BMS was empirically accepting ~10.2 kW;
linear: 40 min needed.  The taper profile contained several isolated
outlier observations (bins 81:0.05 count=1, 83:0.41 count=3, 85:0.16
count=2, 90:0.21 count=7) surrounded by 0.87-1.0 neighbours.  The
per-SoC integration summed these outliers and exceeded the remaining
window by ~5 min — spurious Repair issue, user trust eroded (C-022).
**Rationale**: `is_charge_target_reachable` is a *feasibility* check, not
a pacing prediction.  Its contract is "no plausible scenario reaches
the target" — a stronger bar than the pacing estimate, which is a
point estimate.  The median of trusted ratios across the traversed
range represents the typical scenario; it cannot be pulled arbitrarily
low by a single noisy observation (unlike a sum).  Taking `min(integrated,
median)` biases toward false negatives (only flag genuinely unreachable
targets), matching the C-020 "no false alarms" principle — when ratios
are uniformly low (true unreachability), the median is also low and the
verdict correctly remains False.  Pacing keeps the full integration
because the integrated estimate is the right input for per-tick power
adjustments.
**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: pacing
**Alternatives considered**:
- Credit solar surplus as additional effective charge rate: rejected
  because the BMS limit is the binding constraint (inverter + solar >
  BMS acceptance) — solar does speed up charging at the margin, but
  the mental-model mismatch is better resolved by not firing a spurious
  alarm in the first place.
- Outlier-detection in `TaperProfile._estimate_hours`: rejected as a
  wider blast radius (affects pacing too).  Pacing's tolerance for
  outliers is limited by the 5-min adjust interval and the trajectory
  catch-up burst (D-006); the feasibility check has no such
  self-correcting mechanism and needs the outlier-robust bound.
- Raising `MIN_TRUST_COUNT` globally: rejected — genuinely rare
  observations at high-SoC bins are still valuable, but should not
  dominate a feasibility bound.
**Traces**: C-022, C-020, C-014;
`tests/test_smart_battery_algorithms.py::TestIsChargeTargetReachable::test_outlier_taper_does_not_falsely_fail_live_2026_04_24`

### D-043: Re-deferral when ahead of schedule
**Decision**: Once forced charging has started, if the current SoC is
far enough ahead that `calculate_deferred_start()` says "not yet",
switch back to self-use and clear `charging_started`. Resume forced
charging when the deferred start deadline arrives again.
**Context**: During paced charging, solar generation charges the battery
on top of the grid power the listener requested. The BMS accepts power
from all sources up to its limit regardless of the paced request. This
causes SoC to advance faster than the pacing algorithm predicted.
Without re-deferral, the listener keeps reducing power (bottoming out
at 100W) but can never pause — the target is reached well before the
window ends, wasting cheap-rate self-use time.
**Rationale**: Re-deferral reuses the existing `calculate_deferred_start`
logic (no new algorithm needed) and mirrors the discharge side where
deferral is re-evaluated every tick. Switching to self-use during the
surplus period lets the inverter supply house load from solar/battery
without grid import — the same benefit as initial deferral (D-002
analogue for charge).
**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: pacing
**Alternatives considered**:
- Reduce paced power to near-zero: rejected because 100W floor still
  causes ForceCharge mode to draw from grid; self-use is cleaner
- Subtract solar from power request: rejected because solar forecast
  is unavailable and instantaneous solar is volatile
**Traces**: C-023 (solar-first during ForceCharge — D-043 is the
software complement); D-006 (trajectory tracking still applies after
re-deferral);
`tests/test_smart_battery_algorithms.py::TestCalculateChargePower`

### D-055: Listener commits `deferred_start` for sensor stability

**Decision**: The charge listener writes its tick-local
`calculate_deferred_start()` result to the charge-session state as
`deferred_start_committed` on every tick (normal path, re-deferral
path, and post-completion clear). The phase-display helper
`is_effectively_charging()` (`smart_battery/sensor_base.py`) reads
the committed value instead of independently recomputing the
deferral. Both sides still call the same algorithm with the same
parameters, preserving C-038 parameter parity — the sensor
becomes a **stable read-only view of the listener's most recent
decision**, not a second recomputation. When the committed value is
absent (never populated, or post-completion clear), the sensor
falls back to the pre-commit recomputation for the next tick at
most and degrades gracefully.

**Context**: Observed 2026-04-27 on a live charge session
(11:00–13:59 window) — `sensor.foxess_smart_operations` flipped
phase many times per minute (including a 5-second flip at
02:39:01 → 02:39:36) while the inverter's actual work mode only
transitioned twice in the same 3 hours. The control-card title
("Smart Charge" vs "Charge Deferred") faithfully tracked the
sensor's thrashing state, so the user saw the card flap with no
corresponding real-world state change. Root cause:
`is_effectively_charging()` recomputed `calculate_deferred_start()`
from live coordinator data on every ~5s WS refresh; input jitter
of ±1 kW in `net_consumption_kw` (appliances cycling, solar
flicker) swung the computed `deferred_start` by 10–30 minutes
tick-to-tick, crossing the `now >= deferred` threshold in both
directions. SoC jitter of 0.1% (BMS reporting granularity /
interpolation noise) had the same effect. The listener itself,
which runs at the slower charge-adjustment cadence (5 min), was
not flapping — the sensor was.

**Rationale**: The listener's per-tick decision IS the state that
matters; the display should reflect it rather than race it with a
parallel computation at a different cadence. Writing
`deferred_start_committed` to the same session-state dict the
sensor reads is a single scalar field with no additional cost.
C-038 is preserved at a finer granularity: both sides use the same
formula and the same inputs, but the sensor no longer recomputes
with different (stale or jittery) inputs than the listener saw.
This mirrors the discharge-side pattern introduced earlier, where
pacing-transparency attributes are committed by the listener and
read verbatim by the sensor (D-051).

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Alternatives considered**:
- **Hysteresis in the sensor's recomputed threshold** — rejected:
  adds a tuning parameter, still lets the sensor's phase diverge
  from the listener's last actual decision under pathological
  input sequences, and violates the single-source-of-truth
  intuition.
- **Low-pass filter on `net_consumption_kw`** — rejected: the
  listener legitimately needs the raw value to pace correctly
  (C-001, C-017); smoothing would affect pacing as well as
  display.
- **Move phase computation fully into the listener and eliminate
  the sensor-side fallback** — considered but rejected for this
  change: preserving a graceful fallback for the first tick after
  startup / re-enable is worthwhile; removing the fallback would
  block the sensor from ever reporting phase before the first
  committed value landed.

**Traces**: C-038 (sensor-listener parameter parity — both sides
call the same algorithm with the same inputs; the sensor now
reads the listener's committed result rather than re-running it);
`tests/test_is_effectively_charging_stability.py::TestIsEffectivelyChargingStability`
(four cases: no flip under ±0.1% SoC + ±0.4 kW consumption noise,
plus neighbourhood cases confirming real qualitative changes
still flip phase promptly).

### D-058: Point-in-time wake at the committed deferred-start deadline

**Decision**: When the charge listener commits a future
`deferred_start` (D-055), it also schedules a one-shot
`async_track_point_in_time` wake at that deadline
(`_schedule_deferred_wake`), which re-runs `_adjust_charge_power`
through the session-id guard and circuit breaker. The wake is
idempotent (no-op when the deadline is unchanged), reschedules when
D-043 re-deferral moves the deadline, and is cancelled once the
transition to active charging occurs.

**Context**: Observed 2026-06-02 (live, on v1.0.17): smart charge
flipped `scheduled → charging` at 01:00:10Z but the WebSocket did
not connect until 01:04:04Z (~3m54s later), leaving the dashboard on
stale REST data. This is the same ~4-minute symptom 1.0.17-beta.2
(D-008's `on_session_started` hook) targeted — but a deeper cause.
The beta.2 fix made the WS-startup *hook* event-driven and correctly
wired; it fires when `charging_started` flips True. But the
listener's deferred→active transition itself was NOT event-driven:
`_adjust_charge_power` only re-evaluated on its periodic
`async_track_time_interval` (`SMART_CHARGE_ADJUST_SECONDS` = 300 s).
So the *sensor* flipped to "charging" the instant `now >=
deferred_start_committed` (recomputed every ~5 s coordinator refresh,
D-055), while the *listener* didn't set `charging_started=True` /
fire the hook until its next 300 s tick — up to a full interval
late. Sensor and WS startup were driven by different clocks.

**Rationale**: The transition must be driven by an event at the
deadline, not polled at a coarse cadence. A point-in-time timer at
the committed deadline makes the listener re-evaluate within seconds
of the deadline, so `charging_started` flips and the
`on_session_started` hook fires promptly — closing the
sensor-vs-listener clock gap. Reusing the already-committed
`deferred_start` (D-055) as the wake time keeps a single source of
truth.

**Priority served**: P-005 (Operational transparency — the
data-freshness badge and WS-fed live data reflect the actual session
state within seconds of the transition).
**Trades against**: none.
**Classification**: other (transparency/latency; the underlying
WS startup is a P-005 concern, not a safety invariant).
**Alternatives considered**:
- Shrink `SMART_CHARGE_ADJUST_SECONDS`: rejected — masks the
  structural lag with a faster poll, costing API calls and still
  leaving up-to-one-interval latency.
- Drive WS startup from the sensor's transition: rejected — the
  sensor is a read-only view (D-055); side effects belong in the
  listener.
**Note**: the **discharge** deferred-start path has the analogous
structural gap but additional WS-startup triggers (per-tick WS-aware
wrapper, WS-message arrival, auto-mode targeting) make it
lower-severity — flagged as a follow-up, not yet fixed.
**Traces**: C-020 (user determines state from UI alone), D-055
(committed deferred_start), D-008 (`on_session_started` hook this
wake triggers promptly);
`smart_battery/listeners.py::_schedule_deferred_wake`;
`tests/test_ws_startup_charge_transition.py::TestTransitionFiresPromptlyAtDeferredDeadline`
(wake scheduled at deadline; firing it performs the transition; no
spurious wake when already active).

## Key Behaviours

- Charge power adjustment interval is 5 minutes (vs 1 minute for
  discharge) because charge has lower immediate risk than discharge.
- Negative consumption (solar excess) is treated as zero — conservative
  choice to avoid over-deferring charge start.
- Tolerance for trajectory check shrinks as window closes (smaller
  deficit is tolerated early, but any deficit late triggers burst).
- Temperature-aware time estimates: `bms_temp_c` is passed through to
  `calculate_charge_power`, `is_charge_target_reachable`, and
  `calculate_deferred_start`. The taper model's multiplicative
  temperature correction factor adjusts charge time estimates for BMS
  current limiting at low temperatures (D-014).
- Cold-temperature BMS curtailment (D-037): when BMS temperature is
  below 16°C, max charge power is pre-capped at 80A × battery voltage,
  anticipating the BMS's physical current limit.
- Circuit breaker protection (D-025): charge checks are wrapped in
  `_with_circuit_breaker`. With 5-minute ticks, tier 1 opens at 15 min,
  tier 2 aborts at 40 min.

### C-023: Solar-first during ForceCharge (hardware-satisfied)
**Status**: Satisfied by hardware.
**Discharge observation (2026-04-15)**: Confirmed
`grid_export = discharge + solar - load` — the inverter manages
power flow internally.
**Charge behaviour**: The simulator model (`simulator/model.py`
ForceCharge block) implements solar-first routing:
`solar_to_load = min(solar, load)`, `solar_to_bat = solar - solar_to_load`,
`grid_charge = bat_charge - solar_to_bat`. Three soak tests
(`test_charge_with_solar`, `test_charge_solar_exceeds_target`,
`test_charge_solar_then_spike`) validate end-to-end behaviour.
D-043 (charge re-deferral) handles the software side: when solar
pushes SoC ahead of schedule, the listener switches to self-use.

## Edge Cases

- **Already at target SoC**: Returns minimum power (100W), effectively
  idling until the window ends. (With D-043, the listener switches to
  self-use before reaching this state.)
- **Ahead of schedule (D-043)**: When SoC is ahead enough that
  `calculate_deferred_start` says forced charging isn't needed yet,
  the listener clears `charging_started` and reverts to self-use. The
  next tick re-evaluates deferral. This prevents reaching the target
  30+ minutes early when solar supplements grid charging.
- **Zero remaining time**: Returns max power (best effort).
- **Taper corruption**: Plausibility check auto-resets corrupted profiles
  that would cause permanent max-power burst.
