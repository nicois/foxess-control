---
project: FoxESS Control
audience: contributors implementing the smart-discharge algorithm in any language
sources: smart_battery/algorithms.py, smart_battery/listeners.py, docs/knowledge/04-design/smart-discharge.md
last_verified: 2026-05-10
---

# Smart Discharge Contract

This document is the language-independent specification of the FoxESS
Control smart-discharge algorithm. The Python implementation under
`smart_battery/` is the reference, but a reader implementing the same
algorithm in JavaScript (Cloudflare Workers), Rust, Go, or any other
host language must be able to produce a correct port from this
document alone, without ever opening Python source.

The contract is described as decisions, math, and tables — not as
pseudocode. Every decision is annotated with the priority (P-NNN), the
runtime invariant (C-NNN), and the design decision (D-NNN) it derives
from. Those identifiers are stable and must be cited in any port that
diverges from this document so the audit trail is preserved.

---

## 1. Purpose & priority chain

### 1.1 Mission

Smart discharge runs forced battery discharge during a user-specified
time window, paced to hit a feed-in energy target without ever causing
the house to import from the grid. The algorithm runs on a fixed
cadence (1 minute in the FoxESS Home Assistant integration; 5 minutes
in the SaaS variant) and at every tick produces a small set of
decisions: should we run now, at what power, when should we
re-evaluate, and what is the safety floor below which we must not pace
the inverter.

The core problem the algorithm solves: without smart pacing, forced
discharge runs at the inverter's full power until the energy budget is
exhausted, then the schedule continues to hold force-discharge mode
even though paced power has collapsed below house load. The shortfall
is supplied by the grid — exactly the import the user paid the
algorithm to avoid. Smart discharge defers the start as long as
possible (staying in self-use mode), then runs forced discharge for
the shortest interval that meets the user's feed-in target, with a
safety floor that guarantees no import for the duration.

### 1.2 Priorities (verbatim from `01-vision.md`)

The following ordering is normative. Lower-numbered priorities win
when goals conflict. **Every clamp, deadline, suspend rule, deferral,
and headroom margin in this document derives from one of these
priorities.** When implementing the algorithm in any language, a
reviewer should be able to trace any clamp back to its priority; if a
clamp cannot be traced, the implementation has invented behaviour the
contract does not authorise.

- **P-001: No grid import during forced discharge.** While a
  smart-discharge session is active, the integration must not cause
  the house to import energy from the grid. This is the single
  invariant we will sacrifice any other goal to protect.
- **P-002: Respect minimum state of charge.** Forced discharge must
  not drive the battery below the configured minimum SoC, preserving
  reserves for outages and battery longevity.
- **P-003: Meet the user's energy target.** Within each session
  window, the system should discharge the configured energy target.
- **P-004: Maximise feed-in revenue.** When the other priorities
  allow, prefer earlier / fuller feed-in to later self-use.

This is what makes the algorithm decidable: when two pressures
conflict — for example, "extend forced-discharge to hit the feed-in
target" (P-003) vs "the safety floor would force a low-paced burst
that risks import" (P-001) — the lower-numbered priority wins, full
stop. There is no weighted combination, no "small" import allowed for
"large" feed-in revenue. Implementers porting this algorithm must
preserve the strictness; weighted multi-objective formulations have
been considered and rejected (see D-001 alternatives).

P-005 (operational transparency) and P-006 (brand portability) are
also relevant, but they shape *how* the algorithm reports state and
how the code is structured rather than *what* the algorithm does on a
given tick. A correct port satisfies P-001..P-004 first and exposes
its outputs in a way that makes P-005 achievable second.

---

## 2. Inputs (the algorithm's universe)

The discharge tick consumes the following inputs. Units are normative
— a port that reads watts where this table specifies kilowatts will
produce numerically wrong outputs (see C-004 for the canonical example
of a unit error breaking the system).

| Input                    | Type / unit               | Source              | Notes                                                           |
|--------------------------|---------------------------|---------------------|-----------------------------------------------------------------|
| `current_soc`            | percent (0–100, float)    | inverter            | Per-tick reading. May be missing — see C-019.                   |
| `min_soc`                | percent (0–100, int)      | user config         | Absolute floor. Discharge must never drive SoC below this.      |
| `window_start`           | tz-aware datetime         | service call        | Session boundary. Must equal the same calendar day as `end`.    |
| `window_end`             | tz-aware datetime         | service call        | Session boundary. C-009: must not cross midnight.               |
| `feedin_target_kwh`      | kWh (float, optional)     | service call        | When set, governs deferral via the feed-in deadline (D-005).    |
| `current_load_kw`        | kW                        | inverter            | Per-tick reading.                                               |
| `current_solar_kw`       | kW                        | inverter            | Per-tick reading. Subtracted from load to get net consumption.  |
| `net_consumption_kw`     | kW                        | derived             | `max(0, current_load_kw − current_solar_kw)`. See §7 edge cases.|
| `consumption_peak_kw`    | kW                        | tracked (EMA)       | Decaying peak — see §5.                                         |
| `battery_capacity_kwh`   | kWh                       | config              | Static. Used for SoC↔energy conversion.                          |
| `max_power_w`            | W                         | config              | Inverter hardware ceiling on discharge power.                   |
| `grid_export_limit_w`    | W (≥ 0; 0 = unset)        | config              | Hardware grid-export cap. See D-044, C-037.                     |
| `headroom`               | fraction (0–1)            | user config         | Default 0.10 (10%). Used as time-buffer and power multiplier.   |
| `bms_temp_c`             | °C (optional)             | inverter            | Affects the taper model's discharge-time estimate.              |
| `taper_profile`          | TaperProfile (optional)   | persisted state     | Per-SoC charge/discharge ratio observations. Optional input.    |
| `poll_cadence_s`         | seconds                   | runtime             | 60s for the HA discharge tick; 5min for SaaS coarse-pacing.     |
| `now`                    | tz-aware datetime         | runtime             | Current wall-clock time. Used in deferral and safe-horizon math.|

**Derived per-session state** (initialised at session start, mutated
across ticks):

| State                            | Type             | Initial value                    |
|----------------------------------|------------------|----------------------------------|
| `discharging_started`            | bool             | false                            |
| `consumption_peak_kw`            | kW               | 0.0 (or `current_load_kw` on first tick — see §7) |
| `feedin_start_kwh`               | kWh / null       | null until first tick after force-discharge starts |
| `feedin_prev_kwh`                | kWh / null       | null                             |
| `last_power_w`                   | W                | 0                                |
| `last_export_limit_written_w`    | W / null         | null                             |
| `soc_unavailable_count`          | int              | 0                                |
| `soc_below_min_count`            | int              | 0                                |
| `consecutive_adapter_errors`     | int              | 0                                |
| `circuit_breaker_ticks_held`     | int              | 0                                |
| `suspended`                      | bool             | false                            |

**Runtime constants** (must match these values for behavioural parity):

| Constant                            | Value | Rationale                                          |
|-------------------------------------|-------|----------------------------------------------------|
| `DISCHARGE_SAFETY_FACTOR`           | 1.5   | C-001 floor multiplier on peak consumption          |
| `PEAK_DECAY_PER_TICK`               | 0.85  | Half-life ≈ 4.27 min at 1-min ticks                |
| `_END_GUARD_MINUTES`                | 10    | C-017: switch to self-use when energy < 10 min of floor |
| `MIN_DISCHARGE_POWER_W`             | 100   | Floor for the integer power output                 |
| `MAX_FEEDIN_HEADROOM`               | 0.40  | Cap on the doubled feed-in headroom                |
| `FEEDIN_FALLBACK_RATIO`             | 0.10  | Used when effective export rate would be ≤ 0       |
| `MAX_SOC_UNAVAILABLE_COUNT`         | 3     | C-019: 3 missed SoC reads abort                    |
| `MAX_CONSECUTIVE_ADAPTER_ERRORS`    | 3     | C-024 tier 1 — open circuit breaker                |
| `CIRCUIT_BREAKER_TICKS_BEFORE_ABORT`| 5     | C-024 tier 2 — abort to self-use                   |

---

## 3. Outputs (the algorithm's verdict)

At every tick the algorithm produces a verdict. A correct
implementation MAY package these as a single struct, multiple return
values, or method calls — but every value listed below MUST be
computable from the inputs.

| Output                | Type           | Meaning                                                                                    |
|-----------------------|----------------|--------------------------------------------------------------------------------------------|
| `should_run_now`      | bool           | True if forced-discharge is the right action this tick. False ⇒ stay in self-use.           |
| `paced_power_w`       | int            | The watts to request when `should_run_now` is true. Always ≥ `MIN_DISCHARGE_POWER_W`.       |
| `deferred_start`      | datetime/null  | When `should_run_now` is false, the next time the algorithm believes forced-discharge will be required. The caller uses this for UI countdown and for scheduling the next re-evaluation. |
| `should_suspend`      | bool           | True if SoC has reached/breached `min_soc` (with confirmation), or the end-of-discharge guard has triggered. The session must release any forced-discharge override and revert to self-use. Distinct from `should_run_now=false` because suspension is a stronger signal — it implies the session may end. |
| `safety_floor_w`      | int            | The C-001-derived clamp: `peak_consumption_kw × 1.5 × 1000`. Always computed and surfaced (sensor display, dashboard) even when the session is not currently running, so the user can see the boundary the algorithm is enforcing (P-005). |
| `safe_schedule_end`   | datetime       | The schedule end time the inverter should be programmed with — see D-023 / C-027. Capped at `window_end`. Re-extended on every power change. |
| `effective_export_rate_kw` | kW (debug)| The export rate used in the feed-in deadline calculation. Useful for surfacing the deferral reasoning to the user (P-005). |

---

## 4. The decision tree (in priority order)

This section is the body of the contract. The guards run in the
listed order. The first guard that fires determines the tick's
verdict; later guards are not consulted on that tick. This ordering is
mandatory: it is the implementation of the priority chain in §1.2 —
G1..G3 enforce P-001 / P-002 (the absolute invariants), G4..G6
implement P-003 / P-004 (the targets and the revenue maximisation).

A reviewer porting this algorithm must preserve the order. Reordering
the guards inverts the priority chain and produces a different
algorithm — for example, computing the feed-in budget before the
end-of-discharge guard would let a feed-in-paced burst run during the
last 10 minutes of available energy, which is exactly the import
condition C-017 prevents.

### G1 — Min SoC suspension (P-002 / C-002)

**Condition.**

```
current_soc <= min_soc
```

**Action.** Set `should_suspend = true`. Release any forced-discharge
override; the inverter reverts to self-use. The session enters
suspended state but is NOT terminated — if SoC recovers (e.g.
incoming solar), G1 will not re-fire and pacing resumes.

**Confirmation rule.** The listener layer requires **2 consecutive**
ticks with `current_soc <= min_soc` before ending the session. A
single below-threshold sample increments `soc_below_min_count` and
returns; the second consecutive sample ends the session. This is the
listener's anti-flap protection — a transient SoC dip (one stale
reading, one inter-poll fluctuation) must not terminate a multi-hour
session.

**Why it is first.** P-002 sits above P-003 and P-004 in the priority
chain. No feed-in target or revenue calculation is allowed to drive
the battery past `min_soc`. Note that the condition uses `<=` (not
`<`): once SoC is *equal* to `min_soc`, any further forced discharge
would breach the floor.

**Cite.** P-002, C-002, D-001, D-004.

### G2 — End-of-discharge guard (P-001 / C-017 / D-003)

**Condition.** Compute the energy still available above the min-SoC
floor and the safety floor in kW; if the available energy cannot
sustain the safety floor for at least 10 minutes, suspend.

```
energy_above_min_kwh = (current_soc − min_soc) / 100 × battery_capacity_kwh
consumption          = max(0, net_consumption_kw, consumption_peak_kw)
safety_floor_kw      = consumption × DISCHARGE_SAFETY_FACTOR
guard_kwh            = safety_floor_kw × (_END_GUARD_MINUTES / 60)
```

If `consumption > 0` AND `energy_above_min_kwh < guard_kwh`, set
`should_suspend = true`.

If `consumption <= 0` (solar-dominated, no house load), the guard is
not triggered — there is no import risk because there is no load to
match.

**Why.** Near the tail of a discharge window, paced power collapses
toward `MIN_DISCHARGE_POWER_W` (100 W) because the energy budget is
nearly exhausted. 100 W is well below typical house load, so forced
discharge at that rate guarantees grid import for the remaining
minutes. Switching to self-use 10 minutes early lets the inverter
serve house load directly from the battery without the
forced-discharge floor — at the cost of a tiny amount of forgone
export. The trade-off is explicit: P-001 (no import) > P-003 (energy
target) > P-004 (feed-in revenue). The 10-minute horizon is
empirically chosen — long enough to cover one or two load spikes,
short enough that the forgone export is negligible.

**Cite.** P-001, C-017, D-003.

### G3 — Safety floor (P-001 / C-001 / D-001)

**Condition.** Always evaluated when the algorithm is about to
*request* power (i.e. when G1 and G2 did not fire and the algorithm
is in active discharge). Compute the safety floor:

```
peak       = max(0, net_consumption_kw, consumption_peak_kw)
safety_floor_w = int(peak × DISCHARGE_SAFETY_FACTOR × 1000)
            = int(peak × 1.5 × 1000)
```

**Action.** When constructing `paced_power_w`, ensure it is **never
below `safety_floor_w`** unless the floor itself exceeds
`max_power_w`. The clamp is:

```
if 0 < safety_floor_w <= max_power_w:
    paced_power_w = max(paced_power_w, safety_floor_w)
```

When the safety floor exceeds `max_power_w` — i.e. peak load alone
exceeds the inverter's discharge capacity — grid import is
unavoidable on a load spike. In that pathological case the algorithm
clamps to `max_power_w` and accepts the residual import; this is the
only condition under which P-001 cannot be guaranteed by the
algorithm itself, and it must be surfaced as a session warning
(P-005).

**Why.** When paced discharge power is below house load, the
shortfall comes from the grid. A 1.5× multiplier above the *observed
peak* (not just the current load) provides margin against inter-poll
spikes — a kettle, an oven element, or HVAC compressor cycling
between samples. The choice of 1.5× and the observed-peak input
together absorb the most common residential load volatility patterns
without permanently inflating the floor.

**Cite.** P-001, C-001, D-001, D-004.

### G4 — Feed-in budget pacing (P-003 / D-005)

**Condition.** `feedin_target_kwh` is set and remaining export budget
exists.

When a feed-in target is configured, the algorithm caps the target
energy that drives pacing so the export budget is spread across the
full window, rather than exhausted in the first hour. Without this
spreading, the session would fire at maximum power, hit the budget
quickly, and stop — failing to reach `min_soc` and forfeiting the
remainder of the window's potential.

**Math.** Given `current_soc`, `min_soc`, `battery_capacity_kwh`, and
the user's feed-in budget remaining (`feedin_remaining_kwh`),
compute:

```
energy_to_drain_kwh    = (current_soc − min_soc) / 100 × battery_capacity_kwh
house_absorption_kwh   = max(0, net_consumption_kw) × remaining_hours
max_drain_kwh          = feedin_remaining_kwh + house_absorption_kwh
target_energy_kwh      = min(energy_to_drain_kwh, max_drain_kwh)
```

`max_drain_kwh` is the total energy the battery can deliver in the
remaining window, accounting for the fact that house load absorbs
energy from the inverter without contributing to the user's feed-in
counter. If `target_energy_kwh <= 0` the budget is already exhausted;
the algorithm returns `safety_floor_w` (or `MIN_DISCHARGE_POWER_W` if
the floor is unset/zero).

**Pacing.** Once the target energy is determined, the paced power is
the trapezoidal time-distribution:

```
effective_hours    = remaining_hours × (1 − headroom)         # finish early
battery_power_kw   = target_energy_kwh / effective_hours
battery_power_kw  -= max(0, net_consumption_kw)               # house assists
battery_power_kw  *= 1 + headroom                              # over-provision
paced_power_w      = int(battery_power_kw × 1000)
```

The `headroom` parameter (default 10%) does dual duty: it shrinks the
*effective* window by 10% so the session aims to finish slightly
early, AND it inflates the requested power by 10% so under-delivery
recovers automatically. After this calculation, G3's safety floor
clamp is applied. The final value is then bounded:

```
paced_power_w = max(MIN_DISCHARGE_POWER_W, min(paced_power_w, max_power_w))
```

**Early-stop scheduler.** Between ticks, observed feed-in energy
(read from the cumulative export counter) is used to detect
imminent budget exhaustion. If the observed export rate predicts the
remaining budget will be hit before the next tick, schedule a
one-shot stop at the projected completion time. Math:

```
observed_rate_kw       = (feedin_now − feedin_prev) / poll_hours
seconds_to_target_kwh  = (remaining_kwh / observed_rate_kw) × 3600
```

The early-stop fires once; subsequent ticks observe `feedin_now ≥
feedin_target` and end the session normally. The early-stop avoids up
to one full poll-cadence of overshoot — important on tariffs that
penalise export beyond the daily cap.

**Cite.** P-003, P-004, D-005, C-001.

### G5 — Progressive schedule extension (D-023 / C-027)

**Condition.** Always evaluated whenever the algorithm writes a
forced-discharge schedule.

**Math.** The schedule end time written to the inverter is **not**
the user's `window_end`. It is a safe horizon — the time at which the
battery would reach `min_soc` at the current discharge rate, divided
by the safety factor:

```
energy_above_min_kwh = (current_soc − min_soc) / 100 × battery_capacity_kwh
drain_kw             = paced_power_w / 1000
safe_hours           = energy_above_min_kwh / drain_kw / DISCHARGE_SAFETY_FACTOR
safe_schedule_end    = min(now + safe_hours, window_end)
```

If `drain_kw <= 0` or `energy_above_min_kwh <= 0` the safe horizon
collapses to `window_end` (no extension needed — the schedule is
already at its limit, or there is no energy to drain).

**Why.** If Home Assistant (or the SaaS controller) loses
connectivity mid-session, the inverter continues executing the
schedule unsupervised. With the user's full window written, the
inverter would drain to `fdSoc` (≈ `min_soc` per C-008) for the
entire remaining window — exactly the over-discharge C-002 prevents.
With the safe horizon, the schedule expires within minutes and the
inverter reverts to self-use automatically. The 1.5× safety factor
provides 33% margin: between adjustments, SoC drops proportionally
but the schedule end stays fixed, so the margin *grows* — no
heartbeat extensions are needed on every tick, only when power is
actually adjusted.

**Cite.** D-023, C-024, C-027.

### G6 — Deferred-start computation (P-001 / P-004 / D-002 / D-044)

**Condition.** When the algorithm is *not yet* in active discharge
(`discharging_started == false`) AND the current time is before
`window_end`, compute the latest moment the session could start and
still hit the user's targets. The session stays in self-use mode
during the deferred phase — the inverter intelligently supplies house
load from the battery without exporting, which is the safest place to
spend time.

The computation produces two independent deadlines; **the earlier
wins**:

#### G6a — SoC deadline

How long does full-power forced discharge take to drain from
`current_soc` to `min_soc`?

```
energy_to_discharge_kwh = (current_soc − min_soc) / 100 × battery_capacity_kwh
consumption             = max(0, net_consumption_kw)
effective_kw            = max_power_kw − consumption
```

If `effective_kw <= 0`, house load alone exceeds maximum discharge —
self-use will drain the battery without any forced discharge needed,
so `discharge_hours = 0` (the deadline is already met by self-use).

If a `taper_profile` is provided, use the per-SoC taper integration:

```
discharge_hours = taper_profile.estimate_discharge_hours(
                     current_soc, min_soc, battery_capacity_kwh,
                     int(effective_kw × 1000), temp_c=bms_temp_c)
```

Otherwise:

```
discharge_hours = energy_to_discharge_kwh / effective_kw
```

**Feed-in cap on SoC deadline.** When `feedin_target_kwh` is set, the
session will stop at the feed-in target — not at `min_soc`. The full
SoC drain is therefore not the binding constraint. Compute the
feed-in-time alternative and take the smaller:

```
export_rate_kw = effective_kw
if grid_export_limit_w > 0:
    export_rate_kw = min(export_rate_kw, grid_export_limit_w / 1000)
feedin_hours    = feedin_target_kwh / export_rate_kw
discharge_hours = min(discharge_hours, feedin_hours)
```

Apply the headroom buffer:

```
buffered_hours = discharge_hours / (1 − headroom)
soc_deadline   = window_end − buffered_hours
```

#### G6b — Feed-in deadline

When `feedin_target_kwh` is set, compute a separate deadline based on
how long it takes to *export* the target energy. This deadline differs
from G6a because in self-use mode the battery serves house load (which
counts toward the SoC drain) but the inverter does **not export** — so
all required grid export must come from forced discharge. House load
*reduces* net export: `grid_export = discharge − house_load`.

Use the worst-case consumption:

```
consumption          = max(0, net_consumption_kw, consumption_peak_kw)
effective_export_kw  = max_power_kw − consumption
if grid_export_limit_w > 0:
    effective_export_kw = min(effective_export_kw, grid_export_limit_w / 1000)
if effective_export_kw <= 0:
    effective_export_kw = max_power_kw × FEEDIN_FALLBACK_RATIO   # fallback
```

**Headroom selection (D-044, the 2026-04-24 refinement).** The
feed-in deadline normally uses *doubled* headroom because house
consumption is variable and all export must come from forced
discharge — load spikes reduce net grid export during the burst, so
we start earlier to absorb them.

The exception is when a hardware export clamp is configured strictly
below the inverter's max power. The clamp slack absorbs load spikes
up to its size before the export rate degrades. Define:

```
clamp_active     = 0 < grid_export_limit_w < max_power_w
clamp_slack_kw   = max_power_kw − grid_export_limit_w / 1000
projected_load_kw = max(0, net_consumption_kw, consumption_peak_kw)
```

Apply the doubled-vs-single headroom decision:

```
if clamp_active and projected_load_kw <= clamp_slack_kw:
    feedin_headroom = min(headroom,     MAX_FEEDIN_HEADROOM)   # single
else:
    feedin_headroom = min(headroom × 2, MAX_FEEDIN_HEADROOM)   # doubled
```

**Worked example (live observation, 2026-04-24).** Inverter max
10.5 kW, hardware export clamp 5 kW, feed-in target 1 kWh, peak load
0 kW. Clamp slack is 5.5 kW; projected load (0 kW) is well within the
slack. A load spike would have to exceed 5.5 kW before any reduction
in net export occurs — physically impossible at 0 kW baseline. The
single (10%) headroom applies; the system defers to ~12 min before
window end. Under the previous unconditional doubled headroom, the
system would have deferred to ~15 min before end, eating 3 minutes of
self-use time for protection against a non-existent threat.

Compute the deadline:

```
feedin_hours    = feedin_target_kwh / effective_export_kw
buffered_hours  = feedin_hours / (1 − feedin_headroom)
feedin_deadline = window_end − buffered_hours
```

#### G6c — Combine

```
deferred = min(soc_deadline, feedin_deadline)
if start is not None and deferred < window_start:
    deferred = window_start
```

**Decision.**

```
should_run_now = (now >= deferred)
deferred_start = deferred  if not should_run_now else null
```

When the algorithm transitions from deferred → run, it writes the
forced-discharge schedule for the first time, seeds the export-limit
actuator (if configured) at the hardware max, and sets
`discharging_started = true`.

**Why.** Maximises the self-use period — exactly the period in which
P-001 cannot be violated, because self-use mode never sets a forced
discharge below house load. Defers the C-001 import-risk window to
the latest moment that still meets P-003 and P-004. The two
independent deadlines (SoC vs feed-in) capture the two different
binding constraints — for large batteries with small feed-in caps,
feed-in is binding; for small batteries with large or absent feed-in
caps, SoC is binding. Taking the earlier wins ensures both targets
remain reachable.

The pre-2026-04-22 design used an uncapped SoC deadline, which on
feedin-limited sessions started immediately at low paced power for
the full window — creating the longest possible C-001 exposure
window. The current design (D-005) eliminates that anti-pattern.

**Cite.** P-001, P-003, P-004, D-002, D-005, D-044, C-001, C-037.

---

## 5. Peak consumption tracking (D-004)

The safety floor (G3) and the deferral peak input (G6b) depend on a
tracked peak consumption value. The peak is updated on every tick
using an exponentially-weighted scheme that decays old observations
and immediately respects new spikes:

```
new_peak = max(current_consumption_kw, old_peak × PEAK_DECAY_PER_TICK)
```

Where `PEAK_DECAY_PER_TICK = 0.85`. At the discharge tick cadence of
60 seconds, this decay has a half-life of approximately
**4.27 minutes** — derived from `0.85^4.27 ≈ 0.5`.

Equivalent form using the half-life convention:

```
α = 1 − 0.5^(tick_seconds / half_life_seconds)
half_life_seconds ≈ 256.4   # ≈ 4.27 minutes at 60-s ticks
```

The tracker has two important properties for portability:

1. **Spike response is instantaneous.** A new high-water observation
   replaces the decayed peak immediately because the update is `max(
   current, decayed)` — not a weighted average. A kettle boil is
   reflected on the next tick.

2. **Decay is geometric.** Without new spikes, the peak decays by a
   factor of 0.85 per tick. Five minutes after a one-off spike of
   5 kW, the peak has decayed to `5 × 0.85^5 ≈ 2.22 kW`. After ten
   minutes (10 ticks), `5 × 0.85^10 ≈ 0.98 kW`.

The 1.5× multiplier in G3 then provides additional margin above the
tracked peak. The combined effect: the safety floor reacts within one
tick to load spikes, then slowly relaxes back toward house load as
the spike decays from the peak tracker — exactly the response curve
required to absorb residential load volatility without permanently
inflating the floor.

**Bootstrap.** On the first discharge tick of a session,
`old_peak` is 0.0; the formula reduces to `new_peak =
current_consumption_kw`. This is intentional: the algorithm starts
from the current load and lets the spike-response logic accumulate
peaks over subsequent ticks.

**Cadence dependence.** A SaaS port running at 5-minute ticks must
either (a) keep `PEAK_DECAY_PER_TICK = 0.85` and accept a longer
effective half-life (≈ 21 min at 5-min ticks), or (b) re-derive the
decay factor from the desired half-life. Option (a) is the
charge-tick approximation already used in this codebase (see the
`PEAK_DECAY_PER_TICK` docstring); it is acceptable but not required.
A coarse-pacing port may also choose to update the peak from a
sub-tick rolling buffer of fast-poll observations — see
`coarse-pacing-rules.md` for the cadence-tightening rules.

**Cite.** D-004, C-001.

---

## 6. Trade-offs (priority audit trail)

For each guard, this table makes explicit the priorities served and
sacrificed. Reviewers and SaaS implementers can use this to verify
that no port has accidentally inverted the priority chain. A port
that adds a clamp not in this table must either justify the clamp
against the priority chain or remove it.

| Guard | Serves     | Trades against | Notes                                                                       |
|-------|------------|----------------|-----------------------------------------------------------------------------|
| G1    | P-002      | P-003, P-004   | Suspending at min SoC abandons the energy target; required by C-002.        |
| G2    | P-001      | P-003, P-004   | 10-min early switch sacrifices last-mile feed-in to avoid tail-end import.   |
| G3    | P-001      | P-004          | Floor caps how low paced power can go; trades feed-in revenue for no-import. |
| G4    | P-003      | P-004          | Spreading the budget reduces peak feed-in revenue rate but ensures the SoC target is reachable. Net P-001 benefit (shorter low-power tail). |
| G5    | P-001, P-002 | none         | Safe horizon limits HA-downtime damage. No revenue or target trade-off.    |
| G6a   | P-001, P-003 | P-004        | Deferring at the SoC deadline; trade-off is implicit (less time at full feed-in but more at safe self-use). |
| G6b   | P-003, P-004 | P-005 (length of self-use) | Doubled headroom on volatile-load installations; the conditional refinement (D-044) returns self-use time to clamp-protected installations. |

P-005 (operational transparency) is not directly traded against in
the decision tree but constrains the *outputs*: every guard above
must be inspectable from the UI (see C-038 for the requirement that
sensor display formulas use the same parameter lists as the
listener). A port that produces correct internal verdicts but does
not expose them as user-visible state has only completed half the
contract.

---

## 7. Edge cases

These cases were identified during the original design and during
live-session monitoring (D-001..D-005 alternatives sections, plus the
discharge listener implementation). A correct port must handle them
identically to the reference implementation.

### 7.1 First tick of a session — peak bootstrap

`consumption_peak_kw` starts at `0.0`. On the first tick the formula
`new_peak = max(current_consumption_kw, 0 × 0.85)` simplifies to
`new_peak = current_consumption_kw`. The safety floor is therefore
derived from the *current* consumption on the first tick; subsequent
ticks accumulate peaks via the exponential tracker. A port that
initialises `consumption_peak_kw` to anything other than 0 (e.g. an
inflated default) will compute a wrong floor on tick 0.

### 7.2 Solar exceeds load — negative net consumption

The net consumption used by the algorithm is
`max(0, current_load_kw − current_solar_kw)`. When solar generation
exceeds house load, the raw value is negative; the algorithm clamps
to 0 because solar cannot *contribute* to the safety floor — the
floor is a downside-risk number, not an energy-balance number. A
negative clamp would flatter the floor and risk import on a sudden
solar drop.

This is the reason for the `consumption = max(0, net_consumption_kw)`
pattern repeated throughout §4. Implementers must preserve the clamp;
a port that allows negative consumption to reduce the floor is
introducing an import risk.

### 7.3 SoC unavailable (sensor returns None)

If `current_soc` is missing on a tick, the algorithm increments
`soc_unavailable_count`. After **3 consecutive misses**
(`MAX_SOC_UNAVAILABLE_COUNT = 3`), the session aborts: any
forced-discharge override is removed and the inverter reverts to
self-use. With 1-minute discharge ticks, abort fires after 3 minutes.

This matches the charge-side behaviour (C-012). The rationale is
P-002: operating blind without SoC data during forced discharge is
the exact scenario that risks breaching the min-SoC invariant. A
single missed read is tolerated (transient sensor blip); three
consecutive misses indicate a real problem and forced discharge must
not continue under that condition.

A *successful* SoC read resets the counter to 0.

### 7.4 Export limit configured but inverter returns its current
limit setting differently — read-back tolerance

Implementations that drive a hardware export-limit actuator (D-047)
must tolerate read-back values that differ from the last-written
value. The actuator may quantise to the inverter's native register
resolution (typically 100 W steps), so writing 4350 W and reading
back 4400 W is normal. The `export_limit_min_change_w` threshold
(default 50 W) suppresses sub-threshold writes, which means the
algorithm should compare against the *last written* value, not the
*read-back* value, when deciding whether to write again.

After resumption from suspension, the next write should always fire
regardless of delta. This ensures the actuator is never left at a
stale value after pacing re-engages — for example, if the actuator
was overwritten externally by a Modbus tool while the session was
suspended.

### 7.5 Window crosses midnight — split into two writes

Per C-009, a single FoxESS schedule group cannot cross midnight. A
discharge session whose `window_start` and `window_end` straddle
00:00 must be split into two consecutive groups: one ending at
23:59:59 on day N, one starting at 00:00 on day N+1. The split is the
schedule-writer's responsibility; the discharge algorithm itself
operates on a single contiguous window and trusts the writer to
materialise it correctly.

A port that targets a non-FoxESS inverter without this restriction
may skip the split. The contract is: the algorithm sees one window;
the brand layer is responsible for any per-API materialisation.

### 7.6 Feed-in budget already exhausted

If `feedin_remaining_kwh ≤ 0` at the start of a tick (e.g. a previous
tick's burst overshot), the pacing returns the safety floor (or
`MIN_DISCHARGE_POWER_W` when the floor is unset/zero). The session
typically ends on the next tick when `feedin_now ≥ feedin_target`
fires; the floor power on the in-between tick is non-zero because
abruptly setting power to zero would create a momentary dispatch hole
that the listener treats as a power-change event.

### 7.7 Zero remaining time

If `remaining_hours <= 0` (the algorithm is being called after
`window_end`), the algorithm returns `max_power_w` as a best-effort
fallback. In practice the listener layer guarantees this case is
unreachable — `_on_timer_expire` fires at `window_end` and removes
the override before the next tick — but the algorithm itself must be
robust to the boundary.

### 7.8 House load exceeds maximum discharge power

If `peak_consumption × 1.5` exceeds `max_power_w`, the safety floor
clamp is skipped: requesting more than the inverter can deliver is
nonsensical, and the inverter accepts whatever maximum it can produce.
In this regime grid import on a load spike is unavoidable; the
algorithm accepts the residual import and surfaces a warning. A port
should log this state and ideally raise an HA Repair issue (C-026)
because the user's combination of inverter sizing and household load
profile cannot meet P-001 without hardware change.

### 7.9 Effective export rate ≤ 0 in feed-in deadline

When `effective_export_kw <= 0` (peak load already exceeds inverter
max power, *and* a hardware clamp also reduces the rate), the
algorithm uses a fallback rate of `max_power_kw × FEEDIN_FALLBACK_RATIO`
(default 10% of inverter max). This avoids a divide-by-zero or a
nonsensically-late deadline. The fallback is intentionally
conservative — it produces an earlier feed-in deadline than would
otherwise compute, biasing the algorithm toward starting forced
discharge sooner under unfavourable conditions.

### 7.10 Dynamic min_soc from external automation

`min_soc` is not necessarily a fixed configuration value — external
automations (cold-night heating reserves, pre-cloudy-day charging
policies) legitimately raise it during a session to encode knowledge
the integration does not have. The discharge algorithm treats the
*current* value of `min_soc` as authoritative on every tick. The
expected behaviour when the session runs at or near the elevated
floor: G1 (or G6a) defers / refuses to force-discharge, the SoC
drains only via SelfUse to meet house load, and the session ends
cleanly with possibly zero forced export. This is C-002 working *with*
the external policy — no drama, no grid import, no attempt to
negotiate past the floor. The user may see sessions that achieve no
export; that is the correct outcome when the reserve policy leaves no
spare energy for the session's window.

---

## 8. Failure modes & circuit breaker (C-024)

The discharge tick is wrapped in a two-tier circuit breaker that
absorbs transient adapter failures (single API timeout, brief DNS
outage) without aborting the session. Persistent failures abort to
self-use to satisfy C-024 (safe state on failure).

### 8.1 Tier 1 — open circuit breaker

After **3 consecutive adapter errors**
(`MAX_CONSECUTIVE_ADAPTER_ERRORS = 3`) on the discharge tick, the
breaker opens. While the breaker is open, no further adapter calls
are made on this tick — the session *holds position* with the last
written power level and last written export limit. The tick still
runs the algorithm internally (peak update, suspend evaluation), but
network-bound side effects are suppressed.

A successful adapter call on any tick during this window resets the
breaker and normal operation resumes.

**Timing at 1-minute discharge ticks.** The breaker opens after
3 minutes of failures.

### 8.2 Tier 2 — abort to self-use

If the adapter does not recover within
`CIRCUIT_BREAKER_TICKS_BEFORE_ABORT = 5` additional ticks, the
session aborts. Abort path:

1. Cancel the smart-discharge listeners.
2. Remove any forced-discharge override (`apply_mode(SELF_USE)`).
3. Restore the export-limit actuator to its configured hardware max.
4. Surface the abort as an HA Repair issue (P-005, C-026).

**Timing at 1-minute discharge ticks.** Total time before abort:
3 min (tier 1) + 5 min (tier 2) = **8 minutes**.

### 8.3 Why two tiers

A single transient API error (a few seconds of DNS instability) would
otherwise kill multi-hour sessions unnecessarily — the error would
self-resolve on the next tick. Holding position for up to 8 minutes
is a tolerable safety trade: the inverter continues executing the
last-written schedule, which has its own safe horizon (G5) capped at
roughly the time the battery would reach `min_soc / safety_factor`,
so even a complete loss of HA leaves the inverter in a bounded state.

### 8.4 Session boundary cleanliness (C-025)

On every session-exit path — timer expiry, SoC threshold, feed-in
limit hit, abort, suspend, early stop — the algorithm MUST:

1. Cancel all listeners synchronously *before* any awaits (C-016 —
   prevents stale callbacks from re-enabling the override during
   teardown).
2. Remove the forced-discharge override (`apply_mode(SELF_USE)`).
3. Restore the export-limit actuator to its configured hardware max.

Per-session state (peak tracker, taper tick counters, feed-in
baseline, export-limit last-write) MUST NOT leak into the next
session. A new session starts with the fresh state listed in §2.

---

## 9. Cross-references

The reference Python implementation lives in
`custom_components/foxess_control/smart_battery/` (canonical copy at
the repo root `smart_battery/`). The line ranges below are accurate
as of the `last_verified` date in the frontmatter; the exact line
numbers will drift, but the function names are stable.

| Section / guard | Reference function                                 | File                                   | Function lines    | Tightened by coarse pacing? |
|-----------------|----------------------------------------------------|----------------------------------------|-------------------|-----------------------------|
| §3 outputs      | `_check_discharge_soc_inner`                       | `smart_battery/listeners.py`           | ~1598–1673        | yes — coarser tick cadence  |
| G1 (min SoC)    | `_check_soc_threshold`                             | `smart_battery/listeners.py`           | ~1568–1596        | no                          |
| G1 + G2         | `should_suspend_discharge`                         | `smart_battery/algorithms.py`          | 362–413           | no                          |
| G2 (end guard)  | `_END_GUARD_MINUTES = 10`                          | `smart_battery/algorithms.py`          | 282–294 (constant) | no                         |
| G3 (safety floor) | `safety_floor_w`, `clamp_export_limit_w`         | `smart_battery/algorithms.py`          | 333–359           | no                          |
| G3 + G4         | `calculate_discharge_power`                        | `smart_battery/algorithms.py`          | 416–499           | yes — feed-in pacing changes under coarse cadence |
| G4 (feed-in)    | `_check_feedin_limit`, `_maybe_schedule_feedin_stop` | `smart_battery/listeners.py`         | ~1194–1288        | yes — early-stop scheduler runs at finer cadence than tick |
| G5 (safe end)   | `compute_safe_schedule_end`                        | `smart_battery/algorithms.py`          | 297–330           | no                          |
| G6 (deferral)   | `calculate_discharge_deferred_start`               | `smart_battery/algorithms.py`          | 559–708           | yes — deadline must include sub-tick safety margin under coarse cadence |
| §5 peak tracker | `PEAK_DECAY_PER_TICK = 0.85`                       | `smart_battery/algorithms.py`          | 266–271 (constant) | yes — half-life changes with cadence |
| §7.3 SoC unavail | `_handle_soc_unavailable`                         | `smart_battery/listeners.py`           | ~1290–1315        | yes — abort threshold scales with tick cadence |
| §8 circuit breaker | `_with_circuit_breaker`                         | `smart_battery/listeners.py`           | ~297–388          | yes — tier 1/2 timing scales with cadence |
| §8.4 boundary cleanup | `_remove_discharge_override`, `_restore_export_limit` | `smart_battery/listeners.py`    | ~986–1022         | no                          |

**Constants reference:** `smart_battery/const.py` (canonical
single-source-of-truth for `MIN_DISCHARGE_POWER_W`,
`SMART_DISCHARGE_CHECK_SECONDS`, `MAX_SOC_UNAVAILABLE_COUNT`,
`MAX_CONSECUTIVE_ADAPTER_ERRORS`, `CIRCUIT_BREAKER_TICKS_BEFORE_ABORT`,
`MAX_FEEDIN_HEADROOM`, `FEEDIN_FALLBACK_RATIO`).

**Knowledge-tree references:**

- Priority chain: `docs/knowledge/01-vision.md` §Priorities (P-001..P-007).
- Constraint definitions: `docs/knowledge/02-constraints.md` (C-001,
  C-002, C-009, C-016, C-017, C-018, C-019, C-024, C-025, C-026,
  C-027, C-037).
- Design rationale and alternatives:
  `docs/knowledge/04-design/smart-discharge.md` (D-001..D-005, D-023,
  D-044, D-047).
- Companion documents:
  - `docs/control/coarse-pacing-rules.md` — cadence-tightening rules
    for SaaS / 5-min-tick implementations. Cells in the table above
    marked "yes" indicate sections whose constants or thresholds
    change under coarse cadence; the rules in that doc supersede the
    1-minute-tick numbers here for those sections.
  - `docs/control/smart-charge-contract.md` — the symmetric contract
    for the charge algorithm (separate decision tree, shared peak
    tracker / circuit-breaker / session-cleanliness conventions).
  - `docs/api/foxess-cloud-api.md` — schedule-write field set,
    midnight-split rule (C-009), errno catalogue.

---

## 10. Conformance checklist

A port of this algorithm conforms to the contract when ALL of the
following are demonstrably true:

- [ ] G1..G6 fire in the listed order; the first guard wins.
- [ ] G1 requires 2 consecutive below-threshold SoC reads before
  ending the session.
- [ ] G2 uses `max(0, net_consumption_kw, consumption_peak_kw)` for
  consumption; the guard does not fire on solar-dominated zero-load
  conditions.
- [ ] G3's safety floor uses `peak × 1.5 × 1000`; the floor is
  *never* applied when it would exceed `max_power_w`.
- [ ] G4 caps target energy by `feedin_remaining + house_absorption`,
  not by `feedin_remaining` alone.
- [ ] G4 schedules a one-shot early stop when observed export rate
  would overshoot before the next tick.
- [ ] G5 uses `safety_factor = 1.5` and caps at `window_end`.
- [ ] G6 returns the *earlier* of SoC and feed-in deadlines.
- [ ] G6b applies single headroom only when
  `clamp_active AND projected_load <= clamp_slack`; otherwise doubled.
- [ ] G6b uses `MAX_FEEDIN_HEADROOM = 0.40` as the doubled-headroom cap.
- [ ] §5 peak tracker uses `max(current, old × 0.85)`; spikes are
  reflected on the next tick without averaging.
- [ ] §7.2 negative net consumption is clamped to zero — never used
  to reduce the floor.
- [ ] §7.3 three consecutive missed SoC reads abort the session.
- [ ] §8 circuit breaker opens at 3 consecutive errors, aborts after
  5 more ticks of holding position.
- [ ] §8.4 session-end paths cancel listeners synchronously before
  awaits (C-016) and restore the export-limit actuator to the
  configured hardware max (C-025).
- [ ] All outputs in §3 are computable and surfaced — including
  `safety_floor_w` even when the session is not running (P-005).
- [ ] Every clamp in the implementation traces to a P-NNN; no
  invented clamps without a priority justification.
