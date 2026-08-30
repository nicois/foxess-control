---
project: FoxESS Control
audience: contributors implementing the smart-charge algorithm in any language
sources: smart_battery/algorithms.py, smart_battery/listeners.py, docs/knowledge/04-design/smart-charge.md, taper-model.md
last_verified: 2026-06-04
---

# Smart Charge Contract

This document defines the smart-charge pacing algorithm as decisions and
math, **without referencing a specific programming language**. A reader
implementing the algorithm in any language should be able to produce a
correct implementation from this document alone.

The companion `smart-discharge-contract.md` describes the discharge
algorithm. Charge and discharge share the brand-agnostic core
(`smart_battery/`), the `InverterAdapter` abstraction, the session
machinery, and the taper model — but they have **different priority
chains** and therefore different shapes.

---

## 1. Purpose & priority chain

Smart charge runs **forced charging** of a battery from a household-
visible grid (or solar + grid) supply across a user-defined time
window, paced so the battery reaches a configured target SoC by the
window end.

Without smart pacing, a forced-charge schedule runs at the inverter's
maximum charge power until the target is reached, then idles for the
remainder of the window. This wastes the time window — the charge
finishes too early, missing later cheap-rate hours, or, worse, draws
grid power when free solar would otherwise have arrived.

### How charge differs from discharge

The discharge algorithm exists primarily to **prevent grid import**
during a forced-discharge session (P-001) and to **respect the
minimum SoC** (P-002). Both invariants are safety-flavoured: getting
them wrong directly violates the user's stated economic and
operational guarantees.

Charge is structurally lower-stakes:

- A forced charge cannot import "the wrong way" — pulling from grid
  *is* the point of the operation. The reasons P-001 ranks above
  P-003 simply do not apply.
- A charge that overshoots the target by a few percent is recoverable
  — the next discharge or self-use period absorbs the excess. A
  discharge that under-shoots min SoC is **not** recoverable on the
  same time scale (battery longevity, no backup reserve).
- The control loop runs at **5-minute cadence** for charge (vs
  1-minute for discharge), reflecting the lower urgency.

### Priority chain that applies to charge

Restated from `docs/knowledge/01-vision.md`:

| Rank | Priority | Charge relevance |
|---|---|---|
| P-001 | No grid import during forced **discharge** | Not applicable to charge sessions. Solar-first hardware routing (C-023) and re-deferral (D-043) do help solar reach the battery before grid does — these are charge's *analogue* to the no-import principle, but the bar is "use solar efficiently", not "guarantee zero import". |
| P-002 | Respect minimum SoC | Marginal. Charge cannot drive SoC below min; it could only drive *above* the target. The user-configured target SoC is the only floor charge cares about, and it is upward-only. |
| **P-003** | **Meet the user's energy target** | **The primary goal of charge.** Target SoC by window end. |
| P-004 | Maximise feed-in revenue | Indirect. By not over-charging on grid power when solar will arrive, charge preserves later feed-in opportunities (D-043). |
| P-005 | Operational transparency | Charge surfaces unreachable targets via UI (C-022) and exposes deferred-start phase via committed state (D-055). |
| P-006 | Brand portability | Algorithm and taper model live in `smart_battery/`, decoupled from FoxESS via the `InverterAdapter` Protocol. |
| P-007 | Engineering process integrity | Reproduce-before-fix, simulator-over-mocks, no-flaky-tests. Universal. |

The charge algorithm therefore optimises **P-003** as its primary
goal, with **P-005** (transparency) shaping how state is committed
and surfaced. P-001/P-002 are not load-bearing for charge.

---

## 2. Inputs

The pure pacing functions take the following inputs every tick. Brand
adapters are responsible for sourcing them (the FoxESS adapter pulls
from the cloud REST coordinator + WebSocket; an entity-mode adapter
pulls from HA entities). The pacing algorithm itself sees only the
numbers.

| Input | Type / unit | Source | Notes |
|---|---|---|---|
| `current_soc` | percent (float) | inverter / coordinator | Per-tick. Source of truth for "where are we". |
| `target_soc` | integer percent | session config | Stable for the session. Accepted range 5–100; **not** clamped below 100 in the reference (see §7.3). |
| `start`, `end` | timestamps | session config | Window boundary. |
| `now` | timestamp | system clock | Per-tick. |
| `current_load_kw` | kW (float) | inverter / coordinator | Household consumption. Per-tick. |
| `current_solar_kw` | kW (float) | inverter / coordinator | Solar generation. Per-tick. |
| `net_consumption_kw` | kW (float) | derived | `current_load_kw − current_solar_kw`. Negative values (solar excess) are treated as zero. |
| `battery_capacity_kwh` | kWh | static config | Nameplate or user-tuned. |
| `inverter_max_power_w` | watts | static config | Hardware ceiling for charge power. |
| `bms_temp_c` | °C (float, optional) | inverter / coordinator | Per-tick. Drives the temperature factor in the taper model. May be `None` — algorithm degrades to factor 1.0. |
| `taper_profile` | observed acceptance histogram | persisted store | Loaded once per HA boot, updated each tick the listener decides to record (gated by D-012 / D-015). May be empty for a fresh install. |
| `headroom` | fraction (e.g. 0.10 for 10 %) | session config | Both a time-buffer and a power-multiplier; default 10 %. |
| `min_power_change_w` | watts | session config | Hysteresis on power adjustments — small adjustments are skipped. Default 500 W. Also drives the trajectory-tolerance band. |
| `poll_cadence_s` | seconds | runtime constant | 300 s for charge (vs 60 s for discharge). |

The state the listener carries between ticks (per session):

| State | Meaning |
|---|---|
| `session_id` | Identity token; stale callbacks are silently dropped (C-003). |
| `charging_started` | False during deferred-start phase, True once forced-charge is active. |
| `charging_started_at` | Timestamp of the transition from deferred → active. |
| `charging_started_energy_kwh` | Battery energy at the moment forced-charge began — anchor for trajectory tracking. |
| `last_power_w` | Most recent power request sent to the inverter; for hysteresis. |
| `target_reached` | True when SoC ≥ target during the window; charge stops, session keeps running until window end. |
| `unreachable_issued` | Whether the unreachable-target Repair has already been raised. |
| `deferred_start_committed` | Listener's most recent `calculate_deferred_start()` result, surfaced verbatim by the sensor (D-055). |
| `consecutive_error_count`, `circuit_open`, `circuit_open_ticks` | Circuit-breaker state (C-024). |
| `soc_unavailable_count` | C-019 / D-019 strike counter for SoC entity unavailability. |
| `taper_deficit_streak` | D-015 stability gate counter for temperature recording. |

---

## 3. Outputs

Per tick the algorithm produces:

| Output | Type | Meaning |
|---|---|---|
| `should_run_now` | bool | False during the deferred-start phase or when ahead-of-schedule re-deferred (D-043); True when forced-charge should be active. |
| `paced_power_w` | integer watts, clamped to `[100, inverter_max_power_w]` | Power to request from the inverter. |
| `deferred_start` | timestamp or `None` | The latest moment forced-charge can start to still hit the target. Committed to session state for sensor stability (D-055). |
| `target_reached` | bool | True when `current_soc ≥ target_soc`; the session stops actively charging but keeps the listener alive until window end (so it can re-arm if SoC falls back below target). |
| `is_target_reachable` | bool | C-022 surfacing. False does **not** abort the session — the user wants a best-effort. |

The implementing listener is also responsible for the side-effects
that surface state to the user: writing `deferred_start_committed` to
session state, raising / clearing the
`charge_target_unreachable` HA Repair issue, and emitting
`session_transition` events at start / end.

---

## 4. The decision tree (per tick)

Every gate is identified `G1..G6`. Run them top-to-bottom. The first
gate that returns "stop here" terminates the tick.

### G1. Target reached — end-of-charge boundary

```
if current_soc >= target_soc:
    if not target_reached:
        if charging_started:
            adapter.remove_override(FORCE_CHARGE)
            groups = []
        # NOTE: charging_started is intentionally left True (see below).
        target_reached = True
        log: "SoC %.1f%% >= target %d%%, charge stopped, monitoring"
    return  # no further action this tick
if target_reached and current_soc < target_soc:
    target_reached = False
    log: "SoC dropped below target, resuming"
```

**Why `charging_started` is *not* cleared here.** When the target is
reached the listener removes the active override and clears its
`groups`, but it deliberately leaves `charging_started == True`. This
is load-bearing for the resume path: when SoC later drifts back below
target and `target_reached` flips to False, the next tick sees
`charging_started == True` and therefore re-enters via the **G4
re-deferral / G5–G6 adjust path** — it does *not* re-run the G3
deferred-start gate. A re-implementer who clears `charging_started`
here would instead re-enter the deferred-start computation on resume
(recomputing a deferred start, possibly idling in self-use) — a
materially different behaviour. Keep `charging_started` True.

The session **continues** to its scheduled window end so the listener
can re-arm if SoC drifts back below target (e.g. battery self-discharge
or a household spike during the post-target idle period). Cite: source
listener `_adjust_charge_power_inner`, `algorithms.calculate_charge_power`
returns `MIN_CHARGE_POWER_W` when energy needed ≤ 0.

### G2. Feasibility check (D-046, surfaces C-022)

The feasibility check determines whether `target_soc` can plausibly
be reached in the remaining window. **It does not abort the session.**
It updates `is_target_reachable` so the user sees a Repair issue.

Compute:

```
energy_needed_kwh = (target_soc − current_soc) / 100 · battery_capacity_kwh
if energy_needed_kwh ≤ 0:        return True   # already met
if remaining_hours ≤ 0:          return False  # window expired

max_power_kw         = inverter_max_power_w / 1000
consumption_headroom = max(0, net_consumption_kw)
min_headroom_kw      = max_power_kw · headroom
headroom_kw          = max(consumption_headroom, min_headroom_kw)
effective_charge_kw  = max_power_kw − headroom_kw
if effective_charge_kw ≤ 0:
    effective_charge_kw = max_power_kw · headroom    # fallback
```

Then, **with a taper profile**, compute two estimates and take the
minimum:

```
# Estimate A — taper-integrated hours.
# Sum, over each integer SoC step from current_soc to target_soc,
# of energy_per_pct / (effective_charge_kw · soc_ratio(soc) · temp_factor).
integrated_hours = taper_profile.estimate_charge_hours(
    current_soc, target_soc,
    battery_capacity_kwh,
    int(effective_charge_kw · 1000),
    bms_temp_c)

# Estimate B — outlier-robust median-ratio linear estimate.
median_ratio  = median{ b.ratio | b ∈ taper_profile.charge,
                                   b.count ≥ MIN_TRUST_COUNT,
                                   current_soc ≤ b.soc < target_soc }
                  (defaults to 1.0 if no trusted bins in range)
temp_factor   = taper_profile.charge_temp_factor(bms_temp_c)
                  (defaults to 1.0 if no trusted temp bins)
effective_ratio = clamp(median_ratio · temp_factor, 0.05, 1.0)
median_hours  = energy_needed_kwh
                / (effective_charge_kw · effective_ratio)

charge_hours = min(integrated_hours, median_hours)
```

Without a taper profile, the linear fallback is used:

```
charge_hours = energy_needed_kwh / effective_charge_kw
```

Apply the time-buffer headroom and compare to remaining time:

```
buffered_hours    = charge_hours / (1 − headroom)
is_target_reachable = (buffered_hours ≤ remaining_hours)
```

**Shared numeric core (C-038 parity).** In the reference, the
buffered-hours computation above lives in a single helper
(`_buffered_charge_hours` in `algorithms.py`) consumed by **both** the
listener's `is_charge_target_reachable` *and* the sensor's
`charge_reachability_slack_minutes` attribute. This is the C-038
mechanism: the UI slack figure and the listener's feasibility verdict
are guaranteed to agree because they call the same function with the
same parameters. A re-implementer should factor this computation once
and share it between the control path and the display path rather than
duplicating the math.

**Why median-min?** This is a *feasibility* check, not a *pacing*
prediction. A single anomalous taper observation (one tick where the
BMS reported near-zero acceptance) can dominate the integrated
estimate, pushing it above the remaining window even when the typical
scenario comfortably fits. The median across the traversed range
represents a typical scenario; it cannot be pulled arbitrarily low by
a single noisy bin (unlike a sum). Taking `min(integrated, median)`
biases toward false negatives — the algorithm only flags a target as
unreachable when *no plausible scenario* reaches it. Cite: D-046,
C-022, C-020.

**Pacing** (G5) keeps using the full integrated estimate, because the
integrated estimate is the right input for per-tick power adjustments
where outlier noise is filtered by the 5-minute cadence and the
catch-up mechanism in G5.

When `is_target_reachable` flips False, raise the
`charge_target_unreachable` HA Repair issue (idempotent — once per
session). When it flips back to True, dismiss the issue.

### G3. Deferred-start computation (D-007, D-055)

If `charging_started == False`, the listener is in the **deferred
phase**: the inverter is in self-use, and the listener is waiting
until the latest moment that still permits hitting the target.
Compute `deferred_start` and compare with `now`.

```
energy_needed_kwh = (target_soc − current_soc) / 100 · battery_capacity_kwh
if energy_needed_kwh ≤ 0:
    deferred_start = end          # no need to charge

# Consumption headroom — same effective-charge computation as G2.
max_power_kw         = inverter_max_power_w / 1000
consumption_headroom = max(0, net_consumption_kw)
min_headroom_kw      = max_power_kw · headroom
headroom_kw          = max(consumption_headroom, min_headroom_kw)
effective_charge_kw  = max_power_kw − headroom_kw
if effective_charge_kw ≤ 0:
    effective_charge_kw = max_power_kw · headroom
if effective_charge_kw ≤ 0:
    deferred_start = start         # degenerate; cannot defer

# Time required to reach the target at effective_charge_kw.
if taper_profile available:
    charge_hours = taper_profile.estimate_charge_hours(
        current_soc, target_soc,
        battery_capacity_kwh,
        int(effective_charge_kw · 1000),
        bms_temp_c)
else:
    charge_hours = energy_needed_kwh / effective_charge_kw

# Apply time-buffer headroom and walk back from window end.
buffered_hours = charge_hours / (1 − headroom)
deferred_start = end − buffered_hours
deferred_start = max(deferred_start, start)
```

**Net-charge-rate intuition.** The `effective_charge_kw` is the
**net** charge rate available for the battery — household load
competes with the battery for the inverter's charge capacity, so
positive `net_consumption_kw` is subtracted up to a 10 % floor. (Solar
is *not* added to `effective_charge_kw` — instantaneous solar is
volatile, and the BMS limit usually binds before the inverter does;
solar is handled in the hardware path C-023 and the re-deferral path
D-043.) The 10 % floor ensures the calculation reserves margin even
overnight when current load is near zero, because midnight loads
(hot-water boost, EV charging start) can spike unpredictably.
Cite: D-007.

**Commit the value.** Whichever branch above ran, the listener
**writes** `deferred_start_committed = deferred_start` into session
state. The display side (sensor / Lovelace) reads
`deferred_start_committed` verbatim, instead of recomputing on every
~5 s coordinator refresh. Without this commit, the sensor's recompute
oscillates: live `net_consumption_kw` jitters by ± 1 kW (appliance
cycling, solar flicker), and that jitter swings the calculated
`deferred_start` by 10–30 minutes tick-to-tick, crossing the
`now ≥ deferred` boundary in both directions. Cite: D-055.

**Decision.**

```
if now < deferred_start:
    log: "deferring until ~HH:MM"
    return  # next tick
# else: time to start — fall through to G6 (initial power) and start.
```

**Transition into active charging.** When `now ≥ deferred_start` for
the first time:

1. Compute the initial paced power via G6 below (as `calculate_charge_power`
   without the trajectory parameters — none of `charging_started_energy_kwh`,
   `elapsed_since_charge_started`, `effective_charge_window` are populated yet).
2. Send the inverter into **ForceCharge** at the computed power.
3. Set `charging_started = True`, `charging_started_at = now`,
   `start_soc = current_soc`,
   `charging_started_energy_kwh = current_soc / 100 · battery_capacity_kwh`,
   `last_power_w = new_power`,
   `deferred_start_committed = None` (sensor now reads `charging_started`).
4. Persist the session.

### G4. Re-deferral when ahead of schedule (D-043)

If `charging_started == True`, **also** compute `deferred_start`
again from the current SoC. Solar arriving on top of grid charge
pushes SoC ahead of where the pacing algorithm expected. If the
just-computed `deferred_start` is still in the future:

```
deferred = calculate_deferred_start(current_soc, target_soc, …)
if now < deferred:
    log: "ahead of schedule, re-deferring until ~HH:MM"
    adapter.remove_override(FORCE_CHARGE)        # back to self-use
    charging_started        = False
    charging_started_at     = None
    deferred_start_committed = deferred
    persist session
    return
```

The next tick re-enters at G3 and decides again whether to start.

**What this trades.** Re-deferral trades P-003 (constant progress
toward target) for **P-001-flavoured efficiency** — when self-use
can cover the household load from solar + battery, ForceCharge would
draw grid power that solar will provide for free. The BMS limit is
the binding constraint (inverter + solar > BMS acceptance), so
sustaining ForceCharge while solar is producing wastes the solar
contribution to feed-in or self-use. The re-deferral check is run
**every tick** (not just at start), mirroring the discharge side
where deferral is re-evaluated continuously. Cite: D-043, C-023.

**Why not subtract solar from the power request instead?** Considered
and rejected — solar forecast is unavailable at this layer, and
instantaneous solar is too volatile to drive a 5-minute pacing
decision. Cite: D-043 alternatives.

**Why not lower paced power further toward zero?** Considered and
rejected — the 100 W minimum (`MIN_CHARGE_POWER_W`) still keeps the
inverter in ForceCharge mode and *will* draw from grid because of how
ForceCharge balances PV/grid. Switching to self-use is cleaner.
Cite: D-043 alternatives.

### G5. Trajectory tracking with catch-up burst (D-006)

If `charging_started == True` and we did **not** re-defer, compute
the new paced power. The first thing the algorithm checks is whether
we are **on schedule** vs the trajectory established at
`charging_started_at`.

Define:

```
elapsed         = now − charging_started_at                  (hours)
window_from_start = end − charging_started_at                (hours)
effective_window  = window_from_start · (1 − headroom)
energy_to_add    = target_energy_kwh − charging_started_energy_kwh
actual_energy    = current_soc / 100 · battery_capacity_kwh
tolerance_kwh    = min_power_change_w / 1000 · remaining_hours
```

**With taper data**, compare *proportional* progress (the taper curve
is non-linear, so a linear time-vs-energy comparison would falsely
flag the high-SoC tail as behind schedule):

```
total_hours = taper_profile.estimate_charge_hours(
    start_soc = charging_started_energy_kwh / battery_capacity_kwh · 100,
    target_soc, battery_capacity_kwh,
    inverter_max_power_w, bms_temp_c)

if total_hours > 0:
    time_frac    = min(elapsed / effective_window, 1.0)
    energy_frac  = (actual_energy − charging_started_energy_kwh) / energy_to_add
    deficit_kwh  = (time_frac − energy_frac) · energy_to_add
    if deficit_kwh > tolerance_kwh:
        return inverter_max_power_w     # CATCH-UP BURST
```

**Without taper data**, fall back to a linear ideal:

```
progress         = min(elapsed / effective_window, 1.0)
ideal_energy_now = charging_started_energy_kwh + progress · energy_to_add
deficit_kwh      = ideal_energy_now − actual_energy
if deficit_kwh > tolerance_kwh:
    return inverter_max_power_w     # CATCH-UP BURST
```

**Tolerance.** `tolerance_kwh = min_power_change_w / 1000 · remaining_hours`.
Early in the window, `remaining_hours` is large and the tolerance is
generous — the algorithm tolerates a small early deficit. Late in the
window, `remaining_hours` shrinks and the tolerance closes around zero
— any deficit triggers max power. The threshold *X* the spec asks
about is therefore **not a fixed percentage**; it is the
power-change-hysteresis × remaining-window expressed in kWh. With
defaults (`min_power_change_w = 500`, window 4 h) the early-window
tolerance is 2 kWh, shrinking to 0 as the window closes.

**Why catch-up burst rather than higher headroom globally?** Higher
global headroom wastes cheap-rate hours when the BMS *isn't* taper-
limiting — the charge finishes early. Catch-up is self-correcting: it
only fires when actual progress falls behind, and it stops as soon as
the deficit closes. Cite: D-006 alternatives.

**Why not a PID controller?** Over-engineered for a 5-minute update
loop — by the time PID converges, the window has moved on. Cite:
D-006 alternatives.

### G6. Paced power calculation

If G5 did **not** return (we are on schedule), compute paced power:

```
target_energy_kwh    = target_soc / 100 · battery_capacity_kwh
energy_needed_kwh    = target_energy_kwh − current_energy_kwh
if energy_needed_kwh ≤ 0:    return MIN_CHARGE_POWER_W
if remaining_hours ≤ 0:      return inverter_max_power_w

effective_hours      = remaining_hours · (1 − headroom)
if effective_hours ≤ 0:
    effective_hours = remaining_hours
battery_power_kw     = energy_needed_kwh / effective_hours
total_power_kw       = battery_power_kw + max(0, net_consumption_kw)
total_power_kw      *= (1 + headroom)         # over-provision
power_w              = clamp(total_power_kw · 1000,
                             MIN_CHARGE_POWER_W,
                             inverter_max_power_w)
```

**Why both `(1 − headroom)` and `(1 + headroom)`?** They guard
different things. `(1 − headroom)` plans to finish in less time than
the full window, leaving slack for unanticipated taper or load.
`(1 + headroom)` over-provisions the requested power, leaving slack
for the BMS to throttle without missing the schedule. Together they
form a buffered design: even if both effects materialise, the target
still hits.

**Hysteresis.** If `|new_power − last_power_w| < min_power_change_w`
**and** `new_power ≠ inverter_max_power_w`, skip the write — small
adjustments would flap the schedule pointlessly. Catch-up bursts
(`new_power == inverter_max_power_w`) always write. Cite:
listener `_adjust_charge_power_inner`.

**Apply.** Send `paced_power_w` to the inverter via the adapter
(`apply_mode(FORCE_CHARGE, power_w, fd_soc=100)`). After the await,
re-check session identity (a stale callback may have raced — C-003)
before persisting.

### G7. Cold-temperature curtailment (D-037)

> Status note: D-037 is described in the design corpus as a charge-
> curtailment cap published via the `charge_effective_max_power_w`
> sensor attribute, with formula `min(configured_max,
> 80 A · battery_voltage)` activated when `bms_temp_c < 16 °C`. In
> the current `smart_battery/listeners.py` reference implementation
> the listener simply assigns `effective_max = cur_state["max_power_w"]`
> — i.e. the explicit cap has been **superseded** by the multiplicative
> temperature factor in the taper model (D-014). The sensor attribute
> remains for operational transparency.

A correct re-implementation should treat cold-temp curtailment as
follows:

```
if bms_temp_c is not None and bms_temp_c < 16:
    cold_limit_w = 80 · battery_voltage_v       # ~4 kW at 50 V
    effective_max_power_w = min(inverter_max_power_w, cold_limit_w)
else:
    effective_max_power_w = inverter_max_power_w
```

This `effective_max_power_w` is then used **everywhere** the
algorithm currently uses `inverter_max_power_w` — G2 (feasibility),
G3 (deferral), G5 (trajectory ideal), G6 (paced power), and the
sensor attribute. The 80 A figure is the BMS's documented maximum
charge current at low temperatures; voltage varies with SoC so the
product is computed at runtime from a live battery-voltage reading.

**Preferred long-term path.** The taper model's multiplicative
temperature factor (D-014) replaces the hard 16 °C step with a
*learned* curve. New implementations should record
`temp_factor(temp_c)` from observed data (D-014, D-015), then drop
the hard-cap curtailment in favour of letting `temp_factor` shrink
the effective acceptance ratio multiplicatively. The hard cap is
documented here for completeness and as a fallback for cold-start
implementations with empty temperature bins. Cite: D-037, D-014,
META reflection on `_apply_cold_temp_limit` removal.

---

## 5. The taper model

The taper model is the charge algorithm's most subtle dependency. It
is its **own subsystem** with its own state, its own recording rules,
its own consistency checks, and its own degradation behaviour. The
algorithms above call into it but treat it as a black box: a learned
function `(soc, temp) → effective_acceptance_ratio ∈ [0.05, 1.0]`.

### 5.1 What the model represents

A lithium-ion BMS does **not** accept the full requested charge power
across the entire SoC range:

- At high SoC (typically > 80 %) the cells enter constant-voltage
  (CV) phase. Acceptance drops to 60–80 % of the nominal maximum.
- At low temperatures (typically < 20 °C) the BMS limits current to
  prevent lithium plating. Acceptance can drop to 80–90 % of nominal
  even at mid-range SoC.

These two effects are **physically independent**: the SoC-induced
taper is electrochemical CV; the temperature-induced taper is kinetic
plating protection. They multiply. So the model represents both as
**multiplicative factors** on a single observation:

```
effective_acceptance_ratio(soc, temp)
    = soc_ratio(soc) · temp_factor(temp)
effective_charge_rate
    = inverter_max_power_w · effective_acceptance_ratio(soc, temp)
```

Each factor is learned from observation. There is no hard-coded
curve — the BMS varies by manufacturer, age, cell chemistry, and
unit-to-unit variation. Cite: D-011, D-014.

### 5.2 The denominator question — the 2026-04-24 fix

The recording rule (D-011) defines the observed ratio as:

```
observed_ratio = actual_w / inverter_max_power_w
```

**Not** `actual_w / paced_power_w`. The distinction is critical.

The taper ratio represents *what fraction of maximum the BMS accepts
at this SoC*, not *what fraction of the request was delivered*. When
the pacing algorithm deliberately reduces `paced_power_w` below the
BMS limit (e.g. on a long window where the target SoC is reached
comfortably at low power), `actual_w / paced_power_w` exceeds 1.0,
gets clamped to 1.0, and the underlying taper becomes **invisible**.

Live observation 2026-04-24: a user's profile recorded ratio 1.0 at
81 % SoC despite the BMS empirically limiting charge to 6.38 kW on a
10.5 kW inverter. Root cause: the listener used `last_power_w`
(4.55 kW after pacing) as the denominator instead of `max_power_w`
(10.5 kW). The actual ratio 6380 / 4552 = 1.40 was clamped to 1.0,
discarding all evidence of taper. Cite: META reflection
"Taper recording denominator fix (v1.0.11-beta.1)".

A re-implementer **must** use the inverter's nameplate `max_power_w`
as the denominator regardless of the paced value sent that tick.

### 5.3 EMA smoothing per SoC bin (D-011)

Each integer SoC percent (0…100) is its own bucket:

```
clamped_ratio = clamp(observed_ratio, 0.05, 1.0)
if bucket has prior:
    new_ratio = α · clamped_ratio + (1 − α) · prior_ratio
    new_count = prior_count + 1
else:
    new_ratio = clamped_ratio
    new_count = 1
```

with `α = 0.30` (`EMA_ALPHA`). 0.30 adapts in 3–5 observations per
bucket while smoothing single-tick noise. Slower α (0.1) takes too
long to converge for a system that may only see one observation per
bucket per session; faster α (0.5) is too noisy. Cite: D-011.

### 5.4 Quality gates — D-012

Before recording, drop garbage:

| Gate | Threshold | Source |
|---|---|---|
| `paced_request < 500 W` | reject — ramp-up / ramp-down transient | listener pre-filter on `last_power_w`; `MIN_REQUESTED_W` |
| `actual < 50 W` | reject — sensor error or unit mismatch | `MIN_ACTUAL_W` |
| `count < 2` | bucket "untrusted"; queries fall through to nearest neighbour | `MIN_TRUST_COUNT` |
| Discharge listener observes `suspended == True` | skip — actual is zero by construction | listener |

The 50 W floor specifically guards against the unit-mismatch class
of bug (W vs kW) that produced ~0.001 ratios on Beta 14.

The profile is persisted to HA Store every N observations: the charge
listener passes `save_every = 3`, the discharge listener `save_every
= 5`. These are inline literals in the listener (no named constant).
Cite: D-012.

### 5.5 The 10-minute stability gate — D-015

Temperature observations are **gated** more strictly than SoC
observations: a temperature point is recorded **only after the actual
power has been < 95 % of requested for 10 consecutive minutes**.

Mechanically, the listener increments a streak counter:

```
if actual_w < requested_w · 0.95:
    taper_deficit_streak += 1
else:
    taper_deficit_streak = 0

streak_seconds = taper_deficit_streak · poll_cadence_s
if streak_seconds >= 600:
    record temp observation
```

The 10-minute window filters most transients (cloud cover, grid
fluctuation, ramp-up) while preserving genuine BMS curtailment
(which is sustained over many minutes). The 95 % threshold provides
noise margin — a few percent of measurement jitter is tolerated. The
streak counter works at both the 5-minute charge cadence (2 ticks)
and the 1-minute discharge cadence (10 ticks), so the same model
shape works for both directions. Cite: D-015.

The temperature factor is computed by *factoring out* the SoC
contribution from the observed ratio:

```
temp_factor = (actual_w / max_power_w) / soc_ratio(soc)
clamped     = clamp(temp_factor, 0.05, 1.0)
```

If `soc_ratio(soc) ≤ 0.05` (degenerate), the temperature observation
is dropped — division would amplify noise. Cite: D-014.

### 5.6 Plausibility check on load — C-014 / D-013

When the persisted profile is loaded at startup, run:

```
for bins in [charge, discharge, charge_temp, discharge_temp]:
    trusted = [b.ratio for b in bins if b.count >= MIN_TRUST_COUNT]
    if trusted is empty:
        continue
    median = sorted(trusted)[len(trusted) // 2]
    if median <= MIN_RATIO * 2:     # MIN_RATIO = 0.05 → threshold 0.10
        return PROFILE_CORRUPT
return OK
```

A corrupt profile is **discarded** and the system starts fresh — the
opposite of what most "load and validate" patterns do, but justified
because:

- A profile dominated by ~0.001 ratios (the 2026 unit-mismatch
  scenario) makes the trajectory check fire at max power **every
  single tick**, defeating pacing entirely.
- Auto-reset is preferable to requiring user intervention — the user
  doesn't see the difference between "pacing working with stale
  profile" and "pacing broken" until the window misses its target.
- A corrupted profile can re-converge to truth in a few sessions; a
  silently-broken pacing session cannot.

Cite: C-014, D-013.

### 5.7 Querying — interpolation and edge extrapolation

When the algorithm asks for `soc_ratio(soc)`:

1. Look up `int(clamp(soc, 0, 100))`. If the bin's `count ≥
   MIN_TRUST_COUNT`, return its ratio.
2. **Nearest-neighbour search**: walk outward by ± 1, ± 2, … up to
   ± 5 buckets. Return the first trusted neighbour.
3. **Edge extrapolation**: if all data lives in (say) [80, 100] and
   the query is at 75, return the 80 bucket. BMS taper only gets
   *worse* at extremes — using the closest observed edge is
   conservative and far better than assuming 1.0.
4. **Total cold-start**: no trusted bins exist anywhere → return
   1.0 (no taper assumed). The pacing algorithms degrade gracefully:
   trajectory tracking falls back to the linear ideal, deferred-start
   falls back to `energy_needed / effective_charge_kw`, etc.

Same pattern for the temperature factor with bucket range [-20, 60]
and `MIN_TEMP_TRUST_COUNT = 3`, walking up to `TEMP_NEIGHBOR_RANGE = 3`
neighbours. Cite: `taper.py::_ratio`, `taper.py::_temp_factor`.

### 5.8 Hours estimation — `estimate_charge_hours`

```
hours = 0
energy_per_pct = battery_capacity_kwh / 100
max_power_kw   = effective_charge_kw          # caller's effective rate
temp_factor    = charge_temp_factor(temp_c)   # 1.0 if no temp data
for soc_pct in range(int(from_soc), int(to_soc)):
    soc_ratio    = trusted_ratio(soc_pct)     # interpolated as above
    ratio        = clamp(soc_ratio · temp_factor, 0.05, 1.0)
    effective_kw = max_power_kw · ratio
    hours       += energy_per_pct / effective_kw
return hours
```

This is the **integrated estimate** used by pacing (G3, G5). The
median-ratio estimate (G2) is a *robustified shortcut* over the same
data, used only by feasibility because feasibility is more sensitive
to outliers than per-tick pacing is. Cite: `taper.py::_estimate_hours`.

---

## 6. Trade-offs (audit trail)

| Choice | Serves | Sacrifices | Why it's the right call |
|---|---|---|---|
| Pace charge across the window rather than running flat-out at the start | P-003 (target by window end) | A few minutes of "spare buffer" if the BMS is unexpectedly slow | The catch-up burst (D-006) recovers the buffer when needed; static buffer would waste cheap-rate hours every session, not just the rare slow ones. |
| Re-defer when ahead of schedule (D-043) | P-003 efficiency, charge-side analogue of P-001 | Minor extra logic, occasional brief mode flips at the boundary | The committed `deferred_start_committed` (D-055) absorbs the boundary noise. Without re-deferral, a sunny morning charge wastes cheap-rate by drawing grid while solar is producing. |
| Median-ratio floor on feasibility (D-046) | P-003, P-005 (no false alarms) | Slightly more permissive than pure integration when the profile has many bins below median | Bias is intentional — feasibility is "no plausible scenario reaches" not "the pessimistic scenario reaches". Spurious Repair issues erode trust faster than missed-target detections do. |
| 5-minute charge cadence vs 1-minute discharge | Engineering simplicity, lower P-007 risk | Slower reaction to consumption changes during charge | Charge has **no safety floor** (no P-001 / P-002 stake). The 5-minute interval is sufficient because deficits are fixed by the catch-up burst, not by reactive per-tick adjustment. |
| Multiplicative SoC × temperature taper (D-014) over a 2-D SoC × temp grid | P-003, fast convergence | Slight loss of fidelity at corners (e.g. high SoC + low temp where the two effects might interact non-linearly) | A 2-D grid has O(100 × 40) ≈ 4 000 bins and would take *years* of observations to populate. The multiplicative model is wrong-in-theory but right-in-practice for BMS curves observed in the field. |
| Auto-reset corrupt profiles (D-013) | P-003 | Loses learning history when the heuristic mis-fires | A mis-fire costs a few sessions to re-converge; a kept-corrupted profile breaks every session indefinitely. Asymmetric cost. |
| Use `inverter_max_power_w` as taper denominator, not `paced_power_w` (META 2026-04-24) | P-003, P-007 | Slightly more arithmetic per recording | Using `paced_power_w` makes taper invisible during paced operation — the entire purpose of the model — so this is non-negotiable. |
| Commit `deferred_start` to session state (D-055) | P-005 | One extra scalar of state | Without commit, the sensor recomputes from jittery live inputs and visibly thrashes; with commit, the sensor is a stable read of the listener's last decision. |
| Set fdSoc to 100 during forced charge | P-003 | None at this layer | The schedule layer enforces the API constraint `fdSoc ≥ 11` (C-008); during charge, 100 means "charge as far as the schedule end allows". Termination is by SoC ≥ target_soc check (G1), not by fdSoc. |

---

## 7. Edge cases

### 7.1 Solar exceeds load while charging

Hardware-level (C-023): the inverter routes solar to load first, then
to battery, then to grid. Effective grid import during ForceCharge is

```
solar_to_load = min(solar, load)
solar_to_bat  = solar − solar_to_load
grid_charge   = bat_charge − solar_to_bat
```

The algorithm itself does not need to model this — the inverter
handles it. The software-side complement is **D-043 re-deferral**:
when `current_soc` is far enough ahead (because solar topped up the
battery during the deferred phase or during a previous charge tick),
the listener switches back to self-use rather than continuing to
draw grid. Cite: C-023, D-043.

### 7.2 Cold-temp curtailment intersects with cold-night automation

A user automation may raise `min_soc` on a cold night to keep more
battery energy in reserve for heating loads. Charge does not consult
`min_soc` directly (charge can't drive *down*), so this composes
cleanly: charge still targets `target_soc`, the cold-night
automation operates on the discharge floor independently. The two
loops do not interfere. The only intersection is via `bms_temp_c`:
cold weather slows charge acceptance through the temperature factor
(D-014) **and** raises the effective discharge floor through the
user automation, both correctly representing "battery is colder
today, behave more conservatively".

### 7.3 Charge target above max safe SoC

A target of 100 % pushes the BMS into saturation phase, where
acceptance can drop below 5 % and the trajectory check would fire max
power for the entire tail of the charge — wasteful and hard on cell
longevity.

> **Implementation note (no clamp currently enforced).** The current
> reference implementation does **not** clamp `target_soc`. The
> `smart_charge` service declares `target_soc` with `min: 5, max:
> 100` (`services.yaml`) and the `force_charge` service hard-codes
> `target_soc = 100` (`_services.py`). No layer caps the value below
> 100. The saturation concern above is real but presently
> unmitigated. A re-implementer *may* choose to add a cap (e.g. ≤
> 98 %) at the service-validation boundary — but should be aware that
> the FoxESS reference does not, and a 100 % target is accepted and
> charged to.

### 7.4 Taper profile cold start (no observations)

```
soc_ratio        → 1.0 everywhere (no taper assumed)
temp_factor      → 1.0 everywhere (no temp effect assumed)
estimate_charge_hours → linear (energy / power)
median_ratio     → 1.0
```

All algorithms degrade to the no-taper linear forms. The first
session converges enough buckets to be useful from the second
session onward. Cite: `taper.py::_ratio` final fallback.

### 7.5 Window expired during a tick

Between the listener's previous tick and this one, the wall clock
crossed `end`:

```
remaining_hours = (end − now) / 3600
if remaining_hours <= 0:
    log: "window expired during adjustment, reverting"
    cancel_smart_charge()
    if charging_started:
        adapter.remove_override(FORCE_CHARGE)
    return
```

This is in addition to the scheduled timer at `end_utc` that fires
`_on_charge_timer_expire`. Both paths are needed: HA's time tracking
can drift slightly, and a tick that started before `end` may
arithmetically discover the window is over.

### 7.6 SoC briefly drops below target after `target_reached`

If `target_reached == True` and a subsequent tick reads
`current_soc < target_soc` (e.g. battery self-discharge, large house
load that pulled energy out of a battery that's connected to load),
flip `target_reached = False` and resume the normal flow. Because
`charging_started` was left True at target-reached (see G1), the
resume re-enters via the **G4 re-deferral / G5–G6 adjust path** — not
the G3 deferred-start gate — and produces a fresh paced power
directly. This is not an error path; it's the design for sessions
whose window is much longer than the time required to charge.

### 7.7 SoC entity already at or above target at session start

Before a session is even created, the service validator rejects (on
the **paced** smart-charge path only):

```
if not full_power:                       # paced smart-charge only
    if current_soc >= target_soc:
        raise ServiceValidationError("Current SoC at or above target")
```

— so the paced listener never sees this case freshly. The
`force_charge` / full-power path **bypasses** this check (along with
the SoC-availability and capacity checks), since it intentionally
drives the inverter at max power regardless of headroom. If during
a paced session the user changes `target_soc` to a value below
current, G1 catches it on the next tick and stops charging.

---

## 8. Failure modes

### 8.1 Adapter / API errors — the circuit breaker (C-024 / D-025)

Transient failures must be tolerated; persistent failures must
**not** leave the inverter stuck in ForceCharge. The two-tier circuit
breaker:

```
on adapter exception:
    consecutive_error_count += 1
    # D-059: record to the diagnostics ring buffer, category
    # "adapter_error", with the errno and the payload the brand layer
    # annotated onto the exception.  Repeats of the same failure
    # signature collapse into one entry with a repeat count, so a
    # session retrying every tick cannot evict the buffer.
    record operational error (warning) "n/m consecutive, will retry"
    if consecutive_error_count < 3:
        return    # try again next tick
    # 3 consecutive errors → open circuit breaker
    circuit_open       = True
    circuit_open_ticks = 0
    # Separate category "circuit_breaker_open" so the escalation is
    # never collapsed into the underlying failure.
    record operational error (error) "circuit breaker open, holding position"

while circuit_open:
    on each tick:
        circuit_open_ticks += 1
        if circuit_open_ticks >= 5:        # CIRCUIT_BREAKER_TICKS_BEFORE_ABORT
            log error "circuit breaker open for 5 ticks, aborting"
            record session error for UI
            notify replay subsystem
            cancel session
            return
        # else: just hold position, do nothing
        return

on adapter recovery (any successful tick):
    consecutive_error_count = 0
    if circuit_open:
        log info "adapter recovered, circuit breaker reset"
        circuit_open       = False
        circuit_open_ticks = 0
```

With the 5-minute charge cadence: tier-1 opens after 15 minutes,
tier-2 aborts after a further 25 minutes. A ~40-minute combined
budget is safe for charge — overshooting the target by 40 minutes of
charging at max power is recoverable; an aborted session that spent
those 40 minutes in self-use is also recoverable. Both contrast
sharply with the discharge case where tier-2 is much tighter.

### 8.2 SoC entity unavailable — C-019 / D-019

```
on tick:
    if current_soc is None:
        soc_unavailable_count += 1
        if soc_unavailable_count >= MAX_SOC_UNAVAILABLE_COUNT (3):
            log warning "SoC unavailable for %d checks, aborting"
            record session error for UI
            cancel session, remove override
            return
        # else: skip this tick's power adjustment
        return
    # SoC available — reset counter
    soc_unavailable_count = 0
```

3 ticks × 5 minutes = 15 minutes of blind operation tolerance, the
same boundary as the discharge side (parity, D-019). Charge does
**not** strictly *need* to see SoC to avoid harm — over-charging
above target is benign — but operating blind for longer than 15
minutes accumulates uncertainty about whether `target_reached` has
silently happened, and the guard provides parity with discharge so
brand integrators only have to learn one rule.

### 8.3 Override removal failed during cleanup

When `adapter.remove_override(FORCE_CHARGE)` raises, the listener
stores a `pending_override_cleanup` marker in domain data and logs a
warning. A separate retry path (outside the listener loop) clears
the marker once the override actually removes. Pre-cleanup state is
not safe to leak into a new session — see C-025 (session-boundary
cleanliness): all overrides must be fully removed before a new
session can start.

### 8.4 Window timer expires while a tick is in flight

The `_on_charge_timer_expire` callback at `end_utc` and the periodic
`_adjust_charge_power` callback can race. The pattern:

1. Both check `_is_my_session()` before any side effect.
2. The timer callback runs `cancel_smart_charge()` first, which
   unsubscribes the periodic callback synchronously (before any
   await — C-016). A periodic tick already in flight is cancelled
   on its next `_is_my_session()` check.
3. The timer callback then awaits `remove_override`, which is
   idempotent.

Stale callbacks from cancelled sessions verify
`session_id == my_session_id` and silently drop (C-003).

### 8.5 Persistence failure

`save_session(...)` is awaited inside the listener; if it raises,
the listener logs and continues — the session continues from
in-memory state. On HA restart, an unsaved tick is "lost", but the
algorithm is stateless enough that a fresh tick re-establishes
all per-tick state from coordinator data + `target_soc` + the
scheduled `end`. The taper profile is persisted on its own cadence
(every N observations) and is the only piece of state where data
loss across restarts has long-term cost.

---

## 9. Cross-references

Each section maps to the canonical Python implementation in the
brand-agnostic core. Line ranges are approximate as of `last_verified`
above; the section headings inside the source are stable across
minor edits.

| Section | Source file | Lines | Function / area |
|---|---|---|---|
| §3 outputs | `smart_battery/algorithms.py` | 32–158 | `calculate_charge_power` |
| G1 target reached | `smart_battery/listeners.py` | 705–724 | `_adjust_charge_power_inner` (target check; `charging_started` left True) |
| G1 target reached (algo) | `smart_battery/algorithms.py` | 67–73 | `calculate_charge_power` early-return at `energy_needed_kwh ≤ 0` |
| G2 feasibility | `smart_battery/algorithms.py` | 229–311 | `is_charge_target_reachable` + `_median_trusted_charge_ratio` |
| G2 shared buffered-hours helper (C-038) | `smart_battery/algorithms.py` | 161–226 | `_buffered_charge_hours` (also consumed by sensor `charge_reachability_slack_minutes`) |
| G2 surfacing (Repair issue) | `smart_battery/listeners.py` | 260–311, 1043–1075 | `_create_unreachable_issue`, `_clear_unreachable_issue`, post-adjust check |
| G3 deferred-start computation | `smart_battery/algorithms.py` | 550–604 | `calculate_deferred_start` |
| G3 commit (D-055) | `smart_battery/listeners.py` | 820, 837 | `cur_state["deferred_start_committed"] = deferred` |
| G3 deferred-start wake (point-in-time) | `smart_battery/listeners.py` | 645–671, 837 | `_schedule_deferred_wake` (fixes deferred→active lag) |
| G3 transition (deferred → active) | `smart_battery/listeners.py` | 838–921 | `_adjust_charge_power_inner` (deferred-start branch) |
| G4 re-deferral | `smart_battery/listeners.py` | 925–976 | `_adjust_charge_power_inner` (re-defer branch) |
| G5 trajectory (taper-aware) | `smart_battery/algorithms.py` | 84–126 | `calculate_charge_power` (taper trajectory block) |
| G5 trajectory (linear fallback) | `smart_battery/algorithms.py` | 127–145 | `calculate_charge_power` (else branch of trajectory block) |
| G6 paced power | `smart_battery/algorithms.py` | 147–158 | `calculate_charge_power` (final block) |
| G6 hysteresis | `smart_battery/listeners.py` | 1012–1022 | `_adjust_charge_power_inner` (min_power_change skip) |
| G7 cold-temp curtailment (legacy) | (removed; see D-037 and META) | — | superseded by D-014 multiplicative temp factor |
| §5 taper recording | `smart_battery/taper.py` | 72–106 | `record_charge`, `_record` |
| §5.4 save cadence | `smart_battery/listeners.py` | 757, 1843 | `save_every=3` (charge), `save_every=5` (discharge) — inline literals |
| §5.5 stability gate | `smart_battery/listeners.py` | 434–503 | `_record_taper_observation`, constants `TEMP_STABILITY_SECONDS` (430), `TEMP_DEFICIT_THRESHOLD` (431) |
| §5.5 temp recording | `smart_battery/taper.py` | 110–168 | `record_charge_temp`, `_record_temp` |
| §5.6 plausibility | `smart_battery/taper.py` | 339–364 | `is_plausible` |
| §5.7 interpolation | `smart_battery/taper.py` | 192–217 | `_ratio` |
| §5.8 hours estimate | `smart_battery/taper.py` | 259–335 | `estimate_charge_hours`, `_estimate_hours` |
| §8.1 circuit breaker | `smart_battery/listeners.py` | 316–405 | `_with_circuit_breaker` |
| §8.2 SoC unavailable | `smart_battery/listeners.py` | 678–702 | `_adjust_charge_power_inner` (SoC unavailability branch) |
| §8.3 cleanup retry marker | `smart_battery/listeners.py` | 574–591 | `_remove_charge_override` (`pending_override_cleanup`) |
| §8.4 timer / tick race | `smart_battery/listeners.py` | 593–604, 1096–1100 | `_on_charge_timer_expire`, listener registration |
| §8.5 persistence | `smart_battery/listeners.py` | 1085–1092 | `save_session(...)` calls |
| Constants — cadence, thresholds | `smart_battery/const.py` | 69–88 | `SMART_CHARGE_ADJUST_SECONDS` (69), `MAX_SOC_UNAVAILABLE_COUNT` (74), `MAX_CONSECUTIVE_ADAPTER_ERRORS` (79), `CIRCUIT_BREAKER_TICKS_BEFORE_ABORT` (84), `MIN_CHARGE_POWER_W` (87) |
| Taper constants | `smart_battery/taper.py` | 26–50 | `EMA_ALPHA`, `MIN_TRUST_COUNT`, `MIN_RATIO`, `MAX_RATIO`, `MIN_REQUESTED_W`, `MIN_ACTUAL_W`, `MIN_TEMP_TRUST_COUNT`, `TEMP_NEIGHBOR_RANGE` |

### Design / constraint anchors

- **C-008** — fdSoc ≥ 11 and minSocOnGrid ≤ fdSoc (FoxESS API; enforced by schedule layer outside this contract).
- **C-014** — Taper profile plausibility (auto-reset on load).
- **C-019** — Discharge SoC unavailability aborts — this contract documents the charge-side parity.
- **C-022** — Unreachable charge target must be surfaced to the user.
- **C-023** — Solar-first power routing during ForceCharge (hardware-satisfied; D-043 is the software complement).
- **C-024** — Safe state on failure (two-tier circuit breaker).
- **C-025** — Session boundary cleanliness (no override leakage between sessions).
- **C-038** — Sensor-listener parameter parity (sensor reads listener's committed state).
- **D-006** — Trajectory tracking with catch-up burst.
- **D-007** — Consumption headroom in deferred start.
- **D-011..D-015** — Taper model (EMA, gates, plausibility, multiplicative temp factor, stability gate).
- **D-019** — SoC unavailability abort (parity with charge side).
- **D-028** — `is_charge_target_reachable` exposed as a sensor attribute.
- **D-037** — Cold-temp curtailment cap (legacy hard cap, superseded by multiplicative temp factor in current implementation).
- **D-043** — Re-deferral when ahead of schedule.
- **D-046** — Outlier-robust median-ratio floor on feasibility.
- **D-055** — Listener commits `deferred_start` for sensor stability.
- **P-003** — Meet the user's energy target (charge's primary goal).
- **P-005** — Operational transparency (deferred-start commit, unreachable surfacing).

A re-implementer who maps each entry in this table to their target
language's equivalent module / function / state field will produce a
charge controller behaviourally equivalent to the FoxESS Control
reference.
