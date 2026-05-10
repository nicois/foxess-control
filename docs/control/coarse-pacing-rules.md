---
project: FoxESS Control
audience: contributors implementing smart-charge / smart-discharge in systems without WebSocket access (SaaS, third-party clients)
sources: smart_battery/algorithms.py, docs/knowledge/04-design/smart-discharge.md
last_verified: 2026-05-10
---

# Coarse-Pacing Rules

> **READ THIS FIRST.** When the data feed is coarser than HA's, the
> algorithm fails safe by **suspending discharge earlier**, NOT by
> discharging more aggressively. A naive reading — "less data, less
> precision, push harder to hit the target" — inverts the priority
> chain and is wrong. P-001 (no grid import) outranks P-003 (energy
> target). Coarser data means we cannot react to a load spike for
> several minutes; the response is to *pre-pay* that reaction window
> with extra SoC headroom, not to *post-pay* it by chasing the target
> through grid imports we no longer detect.

## 1. Purpose & non-goals

This document tells implementers — particularly the future Cloudflare
Workers SaaS, but also any third-party client that talks only to the
FoxESS Open API — how to run a smart-charge or smart-discharge session
safely when the only data source is REST polling.

Concretely, "this document applies" means:

- The implementer has **no persistent process** that can subscribe to
  a WebSocket, run a 1-minute timer, or react to inverter telemetry
  faster than its own scheduled poll cadence.
- The implementer's poll cadence is at best **5 minutes** (e.g. a
  Cloudflare Workers Cron Trigger, which has a 5-minute minimum
  granularity).
- The implementer cannot rely on Home Assistant's runtime services,
  the FoxESS WebSocket adapter, or any of the brand-layer fast paths
  documented in `04-design/websocket-realtime.md`.

### Non-goals

- **Matching HA's per-tick precision.** The HA integration combines
  a 60-second discharge tick with a ~5-second WebSocket telemetry
  feed (D-008). A SaaS without that infrastructure will pace more
  conservatively, end sessions earlier, and leave a small amount of
  feed-in revenue on the table. That is the correct trade — see §3.
- **Approximating WebSocket data via faster polling.** The Open API
  rate limits forbid this, and short of a persistent host running an
  HA-class poller it is not achievable anyway. The right answer is
  to widen the safety margins, not to fake the data feed.
- **Tariff optimisation, solar forecasting, or grid services.** These
  are external concerns (see `01-vision.md` Non-Goals); they are
  invariant across cadence and out of scope here.

## 2. Cadence comparison

The HA integration and a 5-minute SaaS implementation differ by an
order of magnitude in reaction time. The numbers below are not
aspirational; they are operational facts that the algorithm must
account for.

| Capability                              | HA integration            | SaaS (cloud-only, this doc)        |
| --------------------------------------- | ------------------------- | ---------------------------------- |
| Realtime power feed                     | WebSocket ~5 s (D-008)    | None                               |
| Discharge tick                          | 60 s (D-001..D-005, key behaviour note in `04-design/smart-discharge.md` §Key Behaviours) | 5 min (REST poll cadence) |
| Charge tick                             | 5 min                     | 5 min                              |
| First-frame latency on session start    | < 5 s (WS push)           | up to 5 min (next REST poll)       |
| Adverse-event reaction time             | 60 s + 5 s ≈ 65 s worst case | up to 5 min                     |
| Inter-tick blind window during discharge | ~5 s (WS) / 60 s (no WS) | 5 min                              |
| Effective sampling rate (Hz)            | ~0.2 (WS) / 0.017 (1-min) | 0.0033                             |

The relevant ratio is the last row: a SaaS at 5-minute cadence sees
roughly **60× less data** than HA with WebSocket, or **5× less** than
HA's no-WS fallback. The next sections quantify what that ratio
forces the algorithm to do differently.

## 3. The fail-safe direction (read twice)

This is the single load-bearing rule of this document. It is restated
in the implementer's checklist (§9) and again in the closing summary
because it is the rule a careless reader is most likely to invert.

**When uncertain, SUSPEND discharge earlier; do NOT discharge harder.**

### 3.1 Why a careless reader gets this wrong

The naive misread runs roughly: "I have less data per minute than the
HA integration. The HA integration uses its data to pace toward the
target precisely. Therefore I should pace harder — start earlier,
stay longer, push more power — to compensate for not being able to
fine-tune."

That reasoning inverts P-001 (no grid import) and P-003 (energy
target). The HA integration's per-tick precision is what *protects*
P-001: it can suspend within ~65 s of an adverse event. A SaaS
without that precision cannot suspend for up to 5 minutes. Pacing
"harder" to compensate would extend the C-001 import-risk window
exactly when the system is least able to detect the import it is
about to cause.

The priority chain (`01-vision.md` §Priorities) makes the correct
direction unambiguous:

> P-001: No grid import during forced discharge.
> P-002: Respect minimum state of charge.
> P-003: Meet the user's energy target.
> P-004: Maximise feed-in revenue.

Lower-numbered priorities win. When fewer data points make P-001 and
P-002 harder to defend, P-003 and P-004 are the goals that yield —
not the safety priorities.

### 3.2 The pre-pay vs post-pay distinction

A 5-minute reaction window cannot be eliminated; it can only be paid
for. There are two ways to pay:

1. **Pre-pay** — end discharge with more SoC headroom, double the
   safety floor, double the feed-in headroom. The session leaves
   energy unspent and reaches min SoC later (or not at all). This
   sacrifices P-003 (energy target) and P-004 (feed-in revenue), in
   that order, in exchange for keeping P-001 and P-002 inviolate.
2. **Post-pay** — keep discharging until the next poll detects an
   import, then react. This works fine on HA at ~65 s reaction; on
   a 5-minute cadence it means up to 5 minutes of grid import per
   adverse event. **This is a P-001 violation. It is forbidden.**

Coarse pacing pre-pays. Always. The whole rest of this document is
specifying the price.

### 3.3 Concrete numeric example

Suppose the household load spikes from 1 kW to 6 kW at time `t`
(kettle, oven, EV charger starting). The discharge session is paced
at 4 kW.

- **HA integration with WebSocket**: at `t + 5 s`, the next WS frame
  reports the new load. At `t + 65 s` (next 1-minute tick), the
  listener recomputes the safety floor at `peak × 1.5 = 9 kW`,
  which exceeds the 4 kW pace; the session either ramps to 9 kW
  (if max power allows) or invokes C-017 (end-of-discharge guard)
  and suspends. **Worst-case grid import: ~65 s × 2 kW shortfall =
  ~36 Wh.** (Negligible.)
- **HA integration without WebSocket** (REST only, but still the
  60-second discharge tick): same as above with ~5 s extra latency.
- **SaaS at 5-min cadence**: the spike is invisible until the next
  poll. **Worst-case grid import: 5 min × 2 kW shortfall = ~167 Wh
  per spike.** Across a session, repeated spikes compound. A single
  EV charging cycle could produce 1+ kWh of grid import — visible
  on the smart meter and unambiguously a P-001 violation.

The SaaS cannot detect this in time. Its only option is to
**price the spike into the safety floor up front**, sizing for a
worst-case 5-minute load excursion rather than the observed
instantaneous load. That is what §4.1 specifies.

### 3.4 Refrain

**Coarser data → suspend earlier, defer later, end with more SoC
headroom. NOT discharge harder. NOT pace tighter. NOT chase the
target into uncertainty.**

## 4. Specific rule adjustments under coarse cadence

For each of the algorithmic rules used in HA's discharge path, the
SaaS-cadence equivalent is given below as math with citations.
English explanations follow each formula.

### 4.1 Safety floor (C-001 / D-001 / D-004)

**HA formula** (`smart_battery/algorithms.py::safety_floor_w`,
referenced by `calculate_discharge_power`):

```
safety_floor_w_HA = peak_kw × DISCHARGE_SAFETY_FACTOR × 1000
                  = peak_kw × 1.5 × 1000
```

where `peak_kw` is the EMA-smoothed peak load over recent ticks
(D-004), updated every 60 seconds against either WebSocket data
(~5 s freshness) or REST polls (~5 min freshness, but read by a
60 s tick that fails-safe by holding the previous peak).

**SaaS adjustment**: under 5-minute cadence the highest load
*observed between polls* is unobservable by definition. A 1 kW
average load may have peaked at 4 kW for two minutes between
samples; the algorithm must size for the worst plausible 5-minute
excursion, not the observed instant.

**Conservative substitution**: treat the worst-case load as
`peak_kw × 2.0`. The factor of 2.0 is justified as follows:

- Residential load distributions are heavy-tailed: short bursts
  (kettles, ovens, EV chargers, heat-pump compressor inrush) routinely
  exceed the 5-minute mean by 2–4×.
- A 2.0× multiplier covers the 5-minute mean of a 4 kW burst lasting
  ~2.5 minutes between samples — a kettle boil or oven preheat. This
  is the modal residential spike, not an outlier.
- 2.0× is conservative against the modal spike but not paranoid; a 4×
  multiplier would over-defer on most installations and erode P-003
  unnecessarily.

**SaaS formula**:

```
safety_floor_w_SaaS = peak_kw × 2.0 × DISCHARGE_SAFETY_FACTOR × 1000
                    = peak_kw × 2.0 × 1.5 × 1000
                    = peak_kw × 3000
```

That is, the SaaS safety floor is **2× the HA safety floor**. Where
HA pours a 1 kW peak into a 1.5 kW floor, the SaaS pours the same
peak into a 3 kW floor. The energy implication: at the same paced
power, the SaaS reaches the floor sooner, suspends sooner, and ends
the session with more residual SoC. That is the pre-paid reaction
window.

**Cite**: P-001, C-001, D-001, D-004. The 2.0× excursion multiplier
is a SaaS-specific addition; the underlying 1.5× safety factor is
unchanged from HA.

### 4.2 End-of-discharge guard (C-017 / D-003)

**HA formula** (`smart_battery/algorithms.py::should_suspend_discharge`,
end-of-discharge guard, lines around 404–408):

```
floor_kw_HA   = consumption × DISCHARGE_SAFETY_FACTOR
              = consumption × 1.5
guard_kwh_HA  = floor_kw_HA × (_END_GUARD_MINUTES / 60.0)
              = floor_kw_HA × (10 / 60)
suspend if    energy_kwh_above_min < guard_kwh_HA
```

The guard says: if the energy remaining above min SoC cannot sustain
the safety floor for 10 minutes, suspend. Ten minutes is exactly the
HA reaction window plus a small margin (D-003).

**SaaS adjustment**: the SaaS reaction window is 5 minutes — the
poll cadence. The guard threshold must widen to cover the new
reaction window plus the new (doubled) safety floor.

**SaaS formula**:

```
floor_kw_SaaS  = consumption × 2.0 × DISCHARGE_SAFETY_FACTOR
               = consumption × 2.0 × 1.5
               = consumption × 3.0
guard_kwh_SaaS = floor_kw_SaaS × (15 / 60)
               = floor_kw_SaaS × 0.25
suspend if     energy_kwh_above_min < guard_kwh_SaaS
```

Two changes from the HA formula: the floor uses the doubled-cadence
safety factor (§4.1), and the guard duration extends from 10 minutes
to **15 minutes** (the SaaS reaction window of 5 minutes plus a
margin equal to the HA guard's 10 minutes). The combined effect is
that the SaaS guard fires at roughly `2.0 × 1.5 = 3.0×` the HA
threshold for any given consumption.

**Cite**: P-001, C-017, D-003. The 15-minute guard window is a
SaaS-specific addition; the 10-minute HA window is unchanged for
HA installations.

### 4.3 Deferred-start margin (D-005 / D-044)

**HA behaviour** (`smart_battery/algorithms.py::calculate_discharge_deferred_start`,
referenced by D-044):

The HA integration applies a **conditional headroom** to the feed-in
deadline. When a hardware export clamp is configured AND the clamp
slack (`max_power_kw − grid_export_limit_kw`) is wide enough that
typical load spikes cannot erode net export below the clamp, the
single 10% headroom is used. Otherwise the doubled 40% headroom is
used. See D-044 for the exact predicate.

The conditional logic is HA-specific: it depends on observing
`net_consumption_kw` and `consumption_peak_kw` against the clamp slack
on every tick, which requires per-tick visibility of household load.

**SaaS adjustment**: the conditional is meaningless under 5-minute
cadence because we cannot observe the load between polls. The
predicate "typical load spikes cannot erode net export" requires
real-time evidence the SaaS does not have. Therefore:

**SaaS rule**: the doubled headroom is **always** applied,
unconditionally.

```
headroom_SaaS = 0.40   (always)
```

vs HA:

```
headroom_HA = 0.10 if (clamp_active AND peak ≤ clamp_slack) else 0.40
```

The mathematical effect on the deferred start: the SaaS computes
the deferred-start time using a 40% margin on top of the feed-in
drain estimate, never the 10% margin. This shifts the deferred
start earlier (less self-use time, more discharge time at higher
power), but more importantly it shifts the start time *predictably*,
without depending on inter-poll observations the SaaS cannot make.

**Cite**: P-001, P-004, D-005, D-044. The unconditional 40%
headroom is a SaaS-specific simplification of HA's conditional
logic; HA's conditional remains unchanged.

### 4.4 Peak consumption EMA (D-004)

**HA formula** (`smart_battery/algorithms.py`, `PEAK_DECAY_PER_TICK`
referenced by listener):

```
peak ← max(peak × PEAK_DECAY_PER_TICK, current_consumption)
PEAK_DECAY_PER_TICK = 0.85
```

At HA's 1-minute discharge tick, the half-life is
`log(0.5) / log(0.85) ≈ 4.27` ticks ≈ **4.3 minutes**. At HA's
5-minute charge tick, the same factor gives a half-life of
`4.27 × 5 ≈ 21.3` minutes — which is why the docstring on
`PEAK_DECAY_PER_TICK` cites "~21 minutes" (it documents the charge
context, where the same constant runs at a 5-minute tick).

**SaaS adjustment**: at 5-minute discharge ticks, applying the
HA factor of 0.85 verbatim gives the 21-minute half-life shown in
the docstring — too long for a discharge floor that needs to track
recent spikes.

**SaaS formula** (general half-life conversion):

```
α = 1 − 0.5^(tick_seconds / half_life_seconds)
peak ← max(peak × (1 − α), current_consumption)
     = max(peak × 0.5^(tick_seconds / half_life_seconds), current_consumption)
```

For SaaS at `tick_seconds = 300` (5 minutes) and a target half-life
of **15 minutes** = `900` seconds (3 ticks):

```
0.5^(300/900) = 0.5^(1/3) ≈ 0.7937
PEAK_DECAY_PER_TICK_SaaS ≈ 0.79
```

A 15-minute half-life is justified as follows:

- Three SaaS ticks (15 min) is the shortest window that gives the
  EMA enough samples to be meaningful. A shorter window approaches
  "trust the last poll", which is unstable on a heavy-tailed load
  distribution.
- 15 minutes is short enough that residential loads have changed
  character (kettle done, oven cycled, EV charger ramped up) — so
  the peak adapts to the current load regime.
- 15 minutes is comparable to the HA charge half-life (21 minutes)
  but shorter, reflecting the higher stakes on discharge.

**Cite**: D-004. The `α = 1 − 0.5^(tick_seconds / half_life_seconds)`
identity is the textbook EMA half-life formula; the choice of
15-minute half-life is a SaaS-specific calibration.

### 4.5 Re-deferral threshold (D-043 / charge)

**Status**: no SaaS-specific adjustment.

The charge re-deferral logic (D-043) already operates at HA's
5-minute tick. The SaaS runs charge at the same cadence. The math,
the thresholds, and the re-evaluation logic transfer unchanged.

This is one of the few places in the algorithm where the SaaS and
HA are at parity — and the reason is that charge has lower P-001
exposure (it does not risk grid import in the way discharge does;
its primary risk is wasted feed-in or grid energy, which is
P-003/P-004 not P-001). The HA integration accordingly never paid
for sub-5-minute charge ticks, so the SaaS inherits the same risk
profile.

**Cite**: D-043. Unchanged.

## 5. Refusal rule (the most important defensive line)

The adjustments in §4 assume the SaaS achieves a 5-minute poll
cadence reliably. If that cadence cannot be achieved, the
adjustments do not save the system — they were designed for 5 min
and degrade non-linearly past it.

**Rule**: if the system's poll cadence exceeds 5 minutes — for any
reason, including missed cron triggers, API rate limiting, transient
network failures, or platform-imposed limits — **the system MUST
refuse to start a new session, and MUST abort any session in
progress at the next opportunity**.

### 5.1 Why 10 minutes is operationally identical to "no cadence"

A 10-minute reaction window is twice the worst-case kettle-cycle
duration. A spike that begins moments after a poll runs uncorrected
for the full 10 minutes — long enough to drain the safety margin
designed for 5 minutes, run through the doubled end-guard, and
start importing from the grid. There is no margin doubling that
recovers safety at 10-minute cadence, because the load excursion
distribution itself stretches with the longer window: the worst-case
5-minute excursion is much smaller than the worst-case 10-minute
excursion, which is much smaller than the worst-case 30-minute
excursion. Each doubling of the cadence requires a more-than-doubled
safety floor, and we run out of inverter headroom before we run out
of plausible load excursions.

In short: at 5 minutes the safety doubles work. At 10 minutes they
do not. The boundary is operational, not aesthetic.

### 5.2 What the SaaS must verify

Before starting a session:

1. **Cadence pre-flight**: confirm that the scheduled cron trigger
   runs at 5 minutes or shorter. Cloudflare Workers Cron Triggers
   have a 5-minute minimum granularity; verify the configured
   schedule string is `*/5 * * * *` or stricter.
2. **Recent-cadence check**: confirm that the previous N triggers
   (suggest N ≥ 3) actually fired within their expected windows. A
   provider that *promises* 5-minute cadence and *delivers* drift
   to 7+ minutes is operationally a 7+ minute provider.
3. **Adapter health**: confirm the FoxESS Open API has responded
   within the last expected poll. A failed last poll is a missed
   sample, indistinguishable from a missed trigger.

During a session:

1. **Cadence monitoring**: each tick records `now − previous_tick`.
   If that delta exceeds 5 minutes plus a small margin (say 30
   seconds), the SaaS must abort the session and remove all
   overrides at the next available opportunity.
2. **Explicit abort path**: aborting on cadence failure is the same
   class of action as C-024 (safe state on failure) — the inverter
   is returned to self-use, the schedule overrides are removed,
   and the session is recorded as ended-on-cadence-fault. The user
   is surfaced this state via the SaaS UI (per C-026 / P-005).

This refusal is not a degradation; it is the safety contract. A SaaS
that runs sessions without verifying its own cadence is an
unsupervised forced-discharge actuator on the inverter — exactly
the failure mode C-024 was written to prevent.

**Cite**: P-001, P-002, C-024, C-026.

## 6. Approximations to discharge precision (honest disclosure)

The doubled margins of §4 buy P-001/P-002 inviolability at the cost
of P-003/P-004 precision. Implementers must be honest with users
about what they are giving up. The trades, in priority order:

### 6.1 P-004 (feed-in revenue) — modest reduction

The unconditional 40% headroom on the deferred start (§4.3) means
the SaaS begins forced discharge slightly earlier than HA would,
running at higher paced power for a slightly longer window. This is
strictly an export-revenue reduction in the typical case where HA
would have applied the 10% headroom: the SaaS is leaving a few
percent of feed-in revenue on the table on each export-clamped
session.

On non-clamped sessions the SaaS and HA both apply 40% headroom,
and there is no revenue gap.

**Magnitude**: a few percent of feed-in revenue per session on
clamped installations; zero on non-clamped installations. Small.

### 6.2 P-003 (energy target hit rate) — modest reduction on adversarial sessions

The doubled safety floor (§4.1) and widened end-guard (§4.2) mean
the SaaS will suspend earlier than HA when residual SoC is thin.
On benign sessions (low and stable household load), this is
invisible. On adversarial sessions (high or volatile load), the
SaaS may suspend with several percent of remaining target energy
unspent.

The user-visible outcome: the energy target is missed by a few
percent on adversarial sessions, and the SaaS UI must surface this
honestly per C-022 (unreachable target surfaced) and C-026
(proactive error surfacing).

**Magnitude**: a few percent of target energy on adversarial
sessions; zero on benign ones.

### 6.3 P-001 (no grid import) — NOT sacrificed

This is the priority the doubled margins exist to protect. The
SaaS cadence must produce *zero* grid import across the session,
exactly as on HA. Every one of the §4 adjustments is in service of
this invariant.

If P-001 is observed to be violated under the §4 rules — i.e. the
user's smart meter shows grid import during a session — the rules
are wrong, not the priority. Re-tune the multipliers (§4.1's 2.0,
§4.2's 15-minute window, §4.3's 40% headroom) upward and reissue
the SaaS. **Do not relax them downward to reduce false suspends.**

### 6.4 P-002 (min SoC) — NOT sacrificed

Same argument. The widened end-guard (§4.2) is specifically a
P-002 protection: it ensures the discharge ends with margin above
min SoC under coarse cadence, exactly as the HA end-guard does
under fine cadence.

### 6.5 What the SaaS UI should communicate

A short, honest statement on the dashboard:

> Pacing is coarser without local Home Assistant; the system is
> slightly more conservative to guarantee no grid import. Feed-in
> targets may be missed by a few percent on volatile-load sessions.

This satisfies P-005 (operational transparency) without paranoia.
Users who run both HA and SaaS will see the difference; users who
run SaaS only will see consistent, predictable, conservative
behaviour. Both groups get the truth.

## 7. What about charge?

Charge is naturally less risky than discharge in the priority chain.
P-001 (no grid import) is a discharge-specific invariant; the
analogue for charge is "no overcharging past target", which is a
P-003 concern, not a P-001 one. Charge wastes feed-in or grid energy
when it goes wrong; discharge causes grid import. The stakes scale
accordingly.

The HA integration's charge tick is already 5 minutes — there is no
cadence gap to bridge. Under SaaS cadence:

- **Pacing math**: unchanged. The charge-power calculation, the
  feedforward adjustment for solar, the trajectory-tracking against
  target SoC — all run at 5-minute cadence on HA already, and
  transfer to the SaaS verbatim.
- **Re-deferral and trajectory tracking**: unchanged. D-043's
  re-deferral logic still works fine; the cold-temp curtailment
  (BMS taper temperature correction) still works fine; the
  feedforward solar adjustment still works fine.
- **BMS temperature granularity**: HA polls BMS temperature via
  REST at 5-minute cadence (it is *not* on the WebSocket — the
  BMS endpoint is `/dew/v0/device/detail`, not a WS topic). So
  even on HA, BMS temperature is 5-minute data. The SaaS does not
  lose BMS-temp granularity by switching off WebSocket; this is
  parity with HA, not a SaaS regression.
- **Session boundary cleanliness (C-025)**: unchanged. The SaaS
  must remove all overrides at session end exactly as HA does.

The single adjustment a SaaS implementer should make for charge is
**none**. Run the canonical algorithm at 5-minute cadence; it is
designed for that cadence.

**Cite**: D-006 (charge pacing), D-007 (charge taper), D-043
(charge re-deferral), C-025 (session boundary cleanliness). All
unchanged.

## 8. Cross-references

Each adjustment in §4 maps to the corresponding HA-integration
source. The tabular form below is the implementer's index:

| SaaS rule (§)              | HA reference                                                                     | Constraint   | Design       |
| -------------------------- | -------------------------------------------------------------------------------- | ------------ | ------------ |
| §4.1 Safety floor (×2)     | `smart_battery/algorithms.py::safety_floor_w`, `calculate_discharge_power`       | C-001        | D-001, D-004 |
| §4.2 End-guard (15 min)    | `smart_battery/algorithms.py::should_suspend_discharge` (end-of-discharge guard) | C-017        | D-003        |
| §4.3 Headroom (40% always) | `smart_battery/algorithms.py::calculate_discharge_deferred_start`                | C-001, C-037 | D-005, D-044 |
| §4.4 EMA decay (15-min HL) | `smart_battery/algorithms.py::PEAK_DECAY_PER_TICK`                               | C-001        | D-004        |
| §4.5 Charge re-deferral    | `smart_battery/algorithms.py` (charge listener path)                             | (none new)   | D-043        |
| §5 Refusal rule            | `smart_battery/listeners.py` (circuit breaker, C-024 path)                       | C-024, C-026 | (no D)       |
| §7 Charge unchanged        | `smart_battery/algorithms.py` (charge pacing)                                    | (unchanged)  | D-006, D-007 |

Source-of-truth files:

- `docs/knowledge/01-vision.md` §Priorities — P-001 through P-007.
- `docs/knowledge/02-constraints.md` — C-001, C-017, C-024, C-026,
  C-037 in particular.
- `docs/knowledge/04-design/smart-discharge.md` — D-001 through
  D-005, D-023, D-044, D-047. Read the "Key Behaviours" section in
  particular for the 1-minute-vs-5-minute tick rationale.
- `docs/knowledge/04-design/websocket-realtime.md` — D-008, D-009,
  D-041 for what the WebSocket buys HA that the SaaS does not have.
- `custom_components/foxess_control/smart_battery/algorithms.py` —
  the canonical formulae, peak EMA, end-guard math.
- `custom_components/foxess_control/smart_battery/listeners.py` —
  the discharge tick cadence and circuit-breaker integration.

## 9. Implementer's checklist

A short final reference for the JS / Workers implementer. The SAFE
items are non-negotiable; the UNSAFE items are common naive
shortcuts that violate the priority chain.

### SAFE — do these

1. **Refuse to run if poll cadence > 5 minutes.** Pre-flight check
   on cron schedule, recent-trigger latency, and Open API
   responsiveness. (§5)
2. **Abort the session if a tick is missed by more than ~30 s
   beyond cadence.** A missed tick is a missed safety check; treat
   it as a C-024 fault and revert to self-use. (§5.2)
3. **Double the safety floor.** Use `peak_kw × 2.0 × 1.5 × 1000`
   instead of `peak_kw × 1.5 × 1000`. (§4.1)
4. **Widen the end-guard to 15 minutes.** Instead of HA's
   10-minute guard window. (§4.2)
5. **Always apply 40% feed-in headroom on the deferred start.**
   Drop the conditional; the conditional needs per-tick observation
   data the SaaS does not have. (§4.3)
6. **Re-tune the EMA decay constant for the new tick.** Use
   `PEAK_DECAY_PER_TICK ≈ 0.79` for a 15-minute half-life at
   5-minute ticks; do not reuse HA's 0.85, which gives a
   21-minute half-life at the SaaS cadence. (§4.4)
7. **Run charge unchanged.** HA's charge logic is already
   5-minute-cadence; transfer it verbatim. (§7)
8. **Surface the conservative trade in the UI.** Tell the user
   the system is more conservative without HA, so users do not
   misread P-003 misses as bugs. (§6.5)
9. **Treat circuit-breaker fault states as session aborts.** Three
   consecutive adapter errors → hold position; five more without
   recovery → abort to self-use. Same as HA per C-024.

### UNSAFE — do NOT do these

1. **Do NOT discharge harder to compensate for less data.** This
   inverts P-001 vs P-003 and is the single most-likely reading
   error. **Coarser data → suspend earlier, not push harder.**
   (§3 — read it again.)
2. **Do NOT smooth the safety floor lower** "because the doubled
   floor seems pessimistic on benign sessions". The doubled floor
   is calibrated against worst-case load excursions the SaaS
   cannot see; smoothing it lower re-opens the C-001 import
   window the SaaS cannot detect closing. (§4.1)
3. **Do NOT treat instant load as worst-case.** Between two
   5-minute polls, an unobserved spike could exceed the observed
   load by a factor of 4 or more. Always size for the inferred
   peak (`peak × 2.0`), not the instantaneous reading. (§4.1)
4. **Do NOT run a session if a poll has been missed.** A missed
   poll is a blind window the safety margins do not cover. Abort
   to self-use; resume the next session after the cadence is
   reliable again. (§5)
5. **Do NOT relax the refusal rule** "because cron sometimes
   drifts and we want to keep sessions running". The refusal rule
   exists because relaxed cadence is operationally equivalent to
   no safety contract. Drift is a fault; respond with abort, not
   tolerance. (§5)
6. **Do NOT extend the doubled headroom into HA's conditional
   logic** without per-tick load observation. The conditional
   depends on real-time data the SaaS does not have. The
   unconditional 40% headroom is the correct simplification, not
   a defect to be optimised away. (§4.3)
7. **Do NOT skip C-022 surfacing.** When the energy target cannot
   be met under coarse pacing, surface that to the user. Silent
   under-shoot violates P-005 (operational transparency). (§6.2,
   §6.5)
8. **Do NOT assume `peak_kw` from one poll generalises.** The EMA
   exists to smooth across multiple ticks; use it (§4.4) and do
   not short-circuit it with a single-poll heuristic.

## 10. Closing summary

Coarse pacing is not "HA pacing with bigger constants". It is HA
pacing **redirected toward the safety priorities** at the cost of
the target priorities, calibrated to the cadence available. The
direction of the redirect is the one rule a careless reader is
likely to invert, so the rule is restated one final time, in bold,
because nothing else in the document is more important:

> **When the data feed is coarser, the algorithm fails safe by
> SUSPENDING discharge earlier — NOT by discharging harder. P-001
> wins. Pre-pay the reaction window with margin; never post-pay
> it with grid imports.**

If a future reader finds themselves implementing or reviewing a
SaaS pacing path and the math seems to want to push harder to hit
P-003 — stop, re-read §3, and check the priority chain in
`01-vision.md`. The math is wrong; the priority chain is right.
