# UX Improvement Backlog

Captured 2026-04-25 after reviewing: the knowledge tree, CHANGELOG
history, live-trace data from `collect_ha_session.py`, and patterns
observed during the 2026-04-24/25 incident investigations. Listed
roughly in priority order.

These are proposals for later reference. None have been scoped
into D-NNN / tests / implementation yet — treat as a backlog to
draw from when the runtime-priorities work (P-001..P-005) has
breathing room.

---

## 1. Pre-flight feasibility preview on service call

**Problem**: Today a user calls `smart_charge` and only finds out
mid-session (via the Repair issue surfaced by D-028/C-022) that
the target isn't reachable.

**Proposal**: Run the same feasibility check at service-call time
and return a structured preview:
*"Window 02:00–06:00, target 90%, starting SoC 35% — will reach
87% at max power (3% short due to BMS taper at 82%+). Options:
extend window by 25 min, reduce target, or accept."*

Service returns a response that automations can branch on; UI
notification on manual invocation.

**Traces**: P-003 (energy target), P-005 (operational transparency).
Related: D-028 (unreachable-target detection — the feasibility
check already exists; reuse it earlier in the pipeline).

**Priority justification**: Shifts "silent failure → Repair issue"
to "informed consent" — users can adjust before the window runs
instead of discovering the shortfall afterward.

---

## 2. Live-session activity timeline card

**Problem**: The control card shows *current state* but not *how
we got here*. When a user asks "why didn't X happen?", the only
path to an answer is reading the debug log sensor.

**Proposal**: Consume the structured events (D-049 listener-layer,
D-050 propagation-fixed) to render a human-readable chronological
log on a Lovelace card:

```
03:28 Charge started (deferred window)
03:28 Forced charge ForceCharge @ 5994 W (paced, -40% headroom)
03:33 Target 90% reached (SoC 89.7%)
03:33 Reverted to SelfUse
```

Events are already flowing; a new card consumes them.

**Traces**: P-005. Related: D-027 (structured logging),
D-049/D-050 (event emission), the Lovelace cards design doc.

---

## 3. Integration health heartbeat

**Problem**: The 2026-04-25 sensor-freeze (50+ min) was invisible
to the user until manual inspection. The Repair surface (D-048)
solves silent-failure detection but is reactive — it only fires
when a sensor write actually raises. A stuck coordinator that
silently delivers the same value repeatedly doesn't trigger it.

**Proposal**: A proactive heartbeat tile / binary_sensor:
- `✓` coordinator update < 2× polling interval ago
- `⚠` 2×–5× polling interval ago (degraded)
- `✗` >5× polling interval ago (failed)

Publish a dedicated `binary_sensor.foxess_coordinator_healthy`
with `last_update_age_seconds` attribute. Also fires an HA Repair
on sustained `✗`.

**Traces**: P-005. New constraint candidate: *"Integration
liveness is user-visible within one polling interval."*

---

## 4. "Why deferred?" inline explanation  ✓ SHIPPED 2026-04-25 (dc89f47, 23fa55b)

**Status**: data surface landed (dc89f47); control-card wiring
landed (23fa55b). `discharge_deferred_reason` and
`charge_deferred_reason` attributes live on
`sensor.foxess_smart_operations`, populated only during the
deferred phase.  The control card renders a wide row with the
reason text inside `.detail-value-wrap` on both the charge and
discharge sections.

**Problem**: `"defers 12m"` is honest but cryptic. Users have no
way to understand why the pacing algorithm has chosen to wait.
The dual-headroom logic (D-044 with its export-clamp-aware
behaviour) is particularly opaque.

**Proposal**: Opt-in verbose attribute on the status sensor:

- *"Waiting for cheaper solar contribution (deferred start 14:30)"*
- *"Export clamp slack 5.5 kW exceeds projected peak 3.0 kW;
  single headroom applied"*
- *"BMS taper at 82%+ extends charge duration by ~20 min"*

Surface via a `reason` attribute on `sensor.foxess_smart_operations`,
rendered on the control card's existing deferred-countdown area.

**Traces**: P-005, D-044 (headroom logic), D-028 (reachability
reasoning).

---

## 5. Taper profile visualisation  ✓ SHIPPED 2026-04-25 (ece71da, 600cd04, 61e5712)

**Status**: data surface landed (ece71da); standalone
`foxess-taper-card` shipped (600cd04 scaffold + 61e5712 E2E).
`taper_profile` attribute on `sensor.foxess_smart_operations`
exposes both `charge` and `discharge` histograms as chart-friendly
`{soc, ratio, count}` lists, sorted by SoC ascending. Marked
`_unrecorded` to avoid recorder bloat.  The custom card renders
horizontal bars per SoC bin with low-confidence markers for bins
having fewer than 3 observations; users opt in by adding
`type: custom:foxess-taper-card` to their dashboard.  An
ApexCharts variant is covered as a user template in
`docs/lovelace-examples.md`.

**Problem**: The taper profile (D-011/D-012/D-014) drives most
pacing decisions and is completely invisible to users. A user who
sees "paced at 4 kW" when they asked for 10 kW doesn't know that
their BMS only accepts 40% above 82% SoC.

**Proposal**: A Lovelace chart showing:
- SoC bins on the X axis (0–100%, 5% granularity matching the
  histogram)
- Acceptance ratio on the Y axis (0–1)
- Observation count per bin as bar thickness / opacity

Turns the taper into an educational tool. Users who understand
their taper profile set realistic window lengths.

**Traces**: P-005, D-011 (histogram), D-014 (temp correction).

---

## 6. Peak-consumption safety floor indicator  ✓ SHIPPED 2026-04-25 (dc89f47, 7072df5)

**Status**: data surface landed (dc89f47); control-card wiring
landed (7072df5). `discharge_safety_floor_w`,
`discharge_peak_consumption_kw`, and `discharge_paced_target_w`
attributes on `sensor.foxess_smart_operations` surface the C-001
floor during a discharge session.  The control card renders a
`safety_floor` row when the floor is non-zero, with an upward-arrow
icon appearing when the paced target is *below* the floor
(indicating active clamping).

**Problem**: During forced discharge, the C-001 floor
(peak × 1.5) dominates paced power in low-load homes. Users see
"paced at 4 kW" and wonder why it's not the 2 kW the energy math
suggests.

**Proposal**: Surface the floor on the control card during
discharge:

```
Discharging 4.0 kW
├── floor: 3.5 kW (peak 2.3 kW × 1.5)
└── paced: 1.2 kW (below floor — clamped up)
```

Makes the safety invariant visible and educates users about
why the system is conservative.

**Traces**: P-001, C-001 (the floor invariant), D-004 (peak
tracking).

---

## 7. Session history card with outcome classification  — DEFERRED

**Status**: scoped but not shipped this pass. Requires a new
persistence schema (session-history ring buffer in HA Store,
restore-on-restart), a new sensor class with a translation key,
and outcome-classification logic that maps session-end reasons
to the 5 outcome tags. Each of these deserves its own test-first
PR rather than bundling with the attribute-only features above.

Re-open as a standalone implementation task when bandwidth allows.
Template: mirror `SmartDischargeExportLimitSensor`'s structure for
the sensor entity and `smart_battery/store.py`'s pattern for
the history persistence.

**Problem**: Users running overnight charges / daily discharges
want a week-over-week view of outcomes, not just "current state".

**Proposal**: A Lovelace card with outcome tags for each session:
- ✓ reached target
- ⊘ hit feed-in limit
- ⌀ hit min SoC
- ✗ aborted (circuit breaker / SoC unavailable)
- ⊗ cancelled by user

Example:
```
Last 7 sessions: ✓✓✓✗✓✓✓
2026-04-24 02:00 Charge → 90% ✓
2026-04-24 17:00 Discharge → 1.0 kWh ⊘
2026-04-25 02:00 Charge → 87% ✗ (3% gap, BMS-taper @ 14°C)
```

The soak-test infrastructure already computes outcomes of this
shape (`tests/soak/conftest.py`); adapt its output model for
real sessions and persist to HA recorder.

**Traces**: P-005. Related: soak test infrastructure in
03-architecture.md.

---

## 8. Export-limit visual acknowledgement on the card  ✓ SHIPPED 2026-04-25 (dc89f47, e022ef0)

**Status**: data surface landed (dc89f47); control-card wiring
landed (e022ef0). `discharge_grid_export_limit_w` and
`discharge_clamp_active` attributes on
`sensor.foxess_smart_operations` populate when
`grid_export_limit` is configured non-zero.  The discharge power
row now renders a split "inverter kW / export kW" layout; the
export side takes the warning colour and shows a `mdi:fence` icon
when `discharge_clamp_active` is true.

**Problem**: A user with `grid_export_limit=5000` and a 10.5 kW
inverter sees "Discharging 8.9 kW" and wonders whether the DNO
just got a 3.9 kW exceedance notice. The hardware actuator
(D-047) handles the clamp, but users can't *see* it is.

**Proposal**: On the control card during discharge, show
both values side by side:

```
Inverter 8.9 kW / Export 5.0 kW (clamp active)
```

Plus a small icon indicating the hardware clamp is engaged.

**Traces**: P-001, P-004, D-044 (clamp awareness), D-047
(hardware actuator), user anxiety about DNO compliance.

---

## 9. HA blueprints for common automations

**Problem**: The README tells users "call these services from
your automations" — the leap from that to a working cheap-rate
overnight charge is where a lot of users disengage.

**Proposal**: Ship HA blueprints:
- "Charge overnight during off-peak tariff window"
- "Discharge at peak feed-in price"
- "Pre-charge before expected outage"
- "Force charge before an expected solar-inverter fault"

Blueprint-level automations lower the barrier from "read the
docs" to "pick your use case". Can live in `blueprints/` in the
repo and be referenced from HACS.

**Traces**: P-005 (discoverability — adjacent to but not
identical to transparency).

---

## 10. Diagnostic self-test service

**Problem**: After HA upgrades, config changes, token rotation,
or a recently-dismissed Repair, users want to know "is it
actually working now?"

**Proposal**: `foxess_control.diagnose` service runs a ~30 s
no-op verification:
- Write current mode, read back, confirm response shape
- Check token validity (cloud API + web portal if configured)
- Check schedule round-trip
- Check WebSocket connects (if enabled)
- Check simulator-vs-cloud-schema parity (C-033 surface)

Returns a structured pass/fail report (notification + service
response). Removes the "did I fix it or is it still broken?"
anxiety.

**Traces**: P-005, P-007 (process integrity — provides a manual
correspondent to the soak suite's nightly verification).

---

## Notes on ranking

The top 4 (#1–#4) address the largest currently-unresolved UX
deficit: the **opacity** of the pacing algorithm. Users trust the
integration because it works, but "why is X happening right now?"
has no UI answer today.

Items #5–#7 are natural extensions: once the pacing reasoning is
visible, surface the inputs (peak, taper) and outcomes (history).

Items #8–#10 address specific friction points that surfaced in
CHANGELOG bugs and support threads.

## Considered but deprioritised

- **Mobile/Android Auto status string enhancement** — existing
  `Chg 10.5kW→90%` is already optimal for single-line rendering.
- **Translation expansion for options flow** — maintenance, not
  a UX leap; handled naturally when languages are added.
- **Forecast improvements (kWh, not just SoC)** — depends on
  Solcast/forecast integration quality that's out of scope per
  the vision's non-goals.
- **"Reconfigure now" button in Repair cards** — HA 2026.x
  already provides Repair action links natively.

## Considered but rejected

None at this time — all items above are additive with no
conflict against the priority hierarchy (P-001..P-007).
