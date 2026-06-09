---
project: FoxESS Control
level: 4
feature: WebSocket Real-Time Data
last_verified: 2026-05-03
traces_up: [../02-constraints.md, ../03-architecture.md]
traces_down: [../05-coverage.md, ../06-tests.md]
---
# Design: WebSocket Real-Time Data

## Overview

The FoxESS Cloud provides an undocumented WebSocket endpoint
(`/dew/v0/wsmaitian`) that streams inverter power data every ~5 seconds.
This supplements the 5-minute REST polling during active forced discharge,
where stale data risks grid import from undetected load spikes.

## Design Decisions

### D-008: WebSocket activation modes (`ws_mode`)
**Decision**: WebSocket activation is governed by a 3-state `ws_mode`
option (replacing the former boolean `ws_all_sessions`):

- **auto** (default): WS connects only during paced forced discharge
  (`discharging_started=True` and `0 < last_power_w < max_power_w`).
  Requires web credentials and cloud mode (not entity mode).
- **smart_sessions**: WS connects during any started smart session
  (charge, or *actively discharging* with `last_power_w > 0`, including
  deferred phases that have transitioned to active) or force operation.

  `last_power_w == 0` is **not** an active session for WS purposes: it is
  the self-use floor the discharge listener parks the inverter at during a
  session (the suspend path and the feed-in self-use path both set
  `last_power_w = 0` while leaving `discharging_started=True` and the
  window open until the end-of-window timer fires). In self-use there is
  no ForceDischarge override, so no P-001 import window to monitor and
  nothing to pace — the WS must stay down. Treating `last_power_w == 0`
  as paced (the pre-1.0.21 `last_pw < max_pw` test) kept the WS streaming
  through self-use lulls, producing a `ws → api → ws` `data_freshness`
  badge oscillation on the REST-poll cadence during effective idle — a
  C-020 leak (live 2026-06-02). The gate now requires `last_power_w > 0`
  in both the auto "paced" branch and the smart_sessions discharge
  fall-through.
- **always**: WS connects at integration startup and stays connected
  regardless of session state. A watchdog timer (at the polling
  interval) re-establishes the connection after transient failures.

All modes require web credentials and cloud mode. WS-triggered
recalculations are debounced at 10 seconds (`_WS_DEBOUNCE_SECONDS`).
The FoxESS-specific `__init__.py` wraps the brand-agnostic discharge
callback to call `_maybe_start_realtime_ws` after every check, ensuring
WS activates on deferred→active transitions.

`_maybe_start_realtime_ws` *reconciles* the connection to the gate — it
is not start-only. When the gate returns False it tears down any
still-running WS (delegating to `_stop_realtime_ws`) rather than
returning early. This is required because the gate is consulted only to
*start*; nothing else stops a running WS on a gate→False transition. A
connection opened during genuine paced discharge would otherwise keep
streaming through every subsequent self-use lull (`last_power_w == 0`
while the window stays open) and after the session window-end timer
fires, leaving the WS data-source badge lit during effective idle (a
C-020 leak, live 2026-06-02: ~5 s frames for 2h46m after the last
session ended; `data_freshness` sawtoothing ws→api→ws on the REST-poll
cadence). The sibling fix corrected the gate's *return value* during the
self-use lull; this reconcile makes the start chokepoint *act* on that
value for an already-running connection. `_stop_realtime_ws` (the
session-cancel/end hook) remains the other teardown path — the reconcile
covers the gate→False-while-running case it does not reach. Inverse: an
active paced session (gate True) must leave a running WS untouched —
the reconcile must not be over-eager.

The gate is also **bounded at both ends of the session window**, not
just the start. The scheduled-phase guard blocks WS while `now < start`
(service called before the window opens); a symmetric window-end guard
blocks WS once `now >= end` for both the charge and discharge branches.
The end guard matters because session teardown is *asynchronous*: the
end-of-window timer (`_on_charge_timer_expire` / `_on_timer_expire`)
awaits override removal **plus** the 30 s WS linger (D-009) before
`cancel_smart_*` clears the session state. During that interval the
state still satisfies `charging_started` / `discharging_started` and the
paced-power test, so without a `now >= end` guard the gate returns True
past the window end. The WS-aware listener interval
(`_ws_aware_charge_cb` / `_ws_aware_discharge_cb`) keeps ticking on its
own HA timer until `cancel_smart_*` unsubscribes it, and each tick
routes through `_maybe_start_realtime_ws` — re-arming (or refusing to
reconcile down) the very connection the teardown is concurrently
stopping. The observable symptom was a `data_freshness` `ws → api → ws`
sawtooth on the REST-poll cadence and the WebSocket streaming for
*hours* of effective idle after a CHARGE session ended (live
2026-06-02, v1.0.21-beta.1, both the self-use-lull gate fix and the
reconcile-down fix already present — this is a third, distinct leak).
The window-end guard closes it deterministically: once `now >= end` the
gate returns False, so the next `_maybe_start_realtime_ws` call (any
listener tick still in flight) reconciles the WS down rather than
holding it up — independent of whether the session-cancel hook's own
teardown wins or loses the race. C-020 (UI reflects true state) and
C-025 (session boundary cleanliness — no resource left streaming past
the window end).

The reconcile is **also wired into the WS object's own reconnect loop**,
not just the start chokepoint. `FoxESSRealtimeWS` takes a
`should_reconnect` predicate (wired by `_maybe_start_realtime_ws` to
`_should_start_realtime_ws(hass)` — the *same* gate). `_try_reconnect`
consults it (via `_reconnect_allowed`) before scheduling any reconnect
I/O. Before this, the reconnect loop was fully autonomous: it gated only
on the instance-local `_no_reconnect` / `_stop_event` flags and never
saw the gate's answer. During confirmed idle (no active session) the
listen loop's 30 s stale-timeout (C-005) fired `_try_reconnect`, which —
with `_no_reconnect` unset — re-established the connection ~6 s later,
*below the start-gate's and coordinator's visibility*. The result was a
self-perpetuating connect → stale(30 s) → disconnect → reconnect(6 s)
cycle every ~5.5 min with no session running, every frame `timeDiff=61`
(discarded per C-005, so the WS delivered nothing useful) — the
`data_freshness` ws↔api sawtooth (C-020 leak, live 2026-06-07, deployed
v1.0.21-beta.2 with all three prior start-gate fixes present; this is a
*fourth*, distinct leak in a layer the start-gate fixes never touched).
Routing the reconnect decision through the same gate makes "the live WS
connection state matches `_should_start_realtime_ws`" a single
reconciliation contract honoured by every path: session start/end, the
periodic gate-evaluation tick, the reconcile-down in
`_maybe_start_realtime_ws`, AND the WS object's own reconnect loop. The
legitimate case is preserved: during an ACTIVE session the gate returns
True, so a stale/dropped WS still reconnects (D-009). A `None` predicate
(no gate supplied) keeps the legacy unconditional-reconnect behaviour for
`always` mode, where `_should_start_realtime_ws` returns True
unconditionally anyway. C-020, C-025.

Existing configurations with the old `ws_all_sessions=True` boolean
are migrated to `ws_mode=smart_sessions` automatically.
**Context**: WebSocket uses a separate web session (username + MD5
password) from the Open API key. It's an extra connection with
reconnect complexity. Entity mode uses local Modbus with faster
polling, making the cloud WebSocket unnecessary. Users running
real-time dashboards wanted WS data continuously, not just during
sessions.
**Rationale**: The 3-state model serves different user profiles:
casual users get the safe default (auto), power users tracking
all sessions enable smart_sessions, and dashboard users get always.
The watchdog in always mode ensures the connection recovers from
transient cloud outages without manual intervention.
**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other
**Alternatives considered**:
- Keep the boolean toggle: rejected because it couldn't express
  "always connected" without overloading the meaning
- Per-session-type toggles: rejected as too granular
**Traces**: C-005, C-020, C-025;
`tests/test_realtime_ws.py::TestStaleness`,
`tests/test_init.py::TestWsGateClosesAtWindowEnd`,
`tests/test_services.py::TestHandleSmartDischarge::test_deferred_to_discharging_triggers_ws`,
`tests/e2e/test_e2e.py::TestDataSource::test_ws_always_connects_without_session`,
`tests/e2e/test_e2e.py::TestDataSource::test_ws_mode_persists_via_options_flow`

### D-009: Post-session linger timeout
**Decision**: After a smart session ends, keep the WebSocket open for
30 seconds to capture one more fresh data push before disconnecting.
The cancel hook (`_on_session_cancel`) returns the WS stop coroutine
instead of scheduling it as a fire-and-forget task; callers await it
AFTER the override removal API call completes. This ensures the linger
only captures data after the inverter has reverted to self-use.
**Context**: After the session ends and the inverter reverts to self-use,
the REST API may still return the old snapshot for up to 5 minutes.
**Rationale**: One more WebSocket push (~5s) injects fresh
post-session values so the overview card immediately reflects reality.
**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other
**Alternatives considered**:
- Immediate disconnect: rejected because UI shows stale state for
  minutes
- Keep WS open until next REST poll: rejected as wasteful (up to 5 min)
- Fire-and-forget linger (original implementation): replaced because
  the linger raced with the override removal — the WS push arrived
  before the API removed the override, capturing stale high-power
  values. See `session-management.md` async flow diagrams for the
  race analysis. The `always` ws_mode was unaffected because the WS
  stays connected and delivers fresh post-session data within ~5s.
**Traces**: C-007, C-020;
`tests/e2e/test_e2e.py::TestDataSource::test_ws_linger_captures_post_discharge_data`

### D-010: Power balance for grid direction
**Decision**: Derive grid import/export from power balance
(`load + charge - discharge - solar`) rather than the `gridStatus` field,
but fall back to `gridStatus` when the balance-predicted magnitude
diverges >3× from the actual grid reading.
**Context**: The `gridStatus` field from the WebSocket has inconsistent
meaning across firmware versions. However, the power balance assumes
FoxESS sees all generation and load, which fails when external sources
(e.g. a separate grid-tied solar inverter) are present.
**Rationale**: Power balance is physically correct by conservation of
energy when all sources are visible. When the predicted and actual grid
magnitudes diverge significantly, an unmeasured source is skewing the
balance — `gridStatus` is more reliable in that case despite firmware
inconsistencies.
**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: safety
**Alternatives considered**:
- Trust `gridStatus` always: rejected after observing incorrect values
  with certain firmware
- Trust balance always: rejected after GitHub issue #3 showed external
  generation causing persistent direction swap
- Ignore grid direction entirely: rejected because feed-in energy
  integration requires it
**Traces**: C-006;
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_importing_from_balance`,
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_balance_unreliable_unmeasured_generation`


### D-021: Visibility of data source on lovelace cards
**Decision**: Whenever the user has configured more than one potential
data source, each lovelace card displays a badge indicating which
source is currently driving displayed values. The source is tracked
in the coordinator (`_data_source` field) and exposed as a
`data_source` state attribute on all polled sensors.
**Context**: FoxESS can be configured with cloud API only, cloud API +
WebSocket credentials, or Modbus entities via foxess_modbus. Data
freshness varies significantly: API polls every 5 minutes, WebSocket
pushes every ~5 seconds, Modbus polls at the foxess_modbus interval.
Without an indicator, the user cannot tell whether displayed values
are 5 seconds or 5 minutes old.
**Rationale**: Ambiguity is from the user's perspective. If they have
configured WebSocket credentials, they need to know whether WS is
currently active or whether the system has fallen back to API — even
(especially) when the answer is API. A missing badge when multiple
sources are configured is itself a source of confusion.
**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other
**Alternatives considered**:
- Show freshness timestamp instead of source: rejected because the
  source identity is more actionable than a raw timestamp
- Hide badge when only one source is configured: accepted — no
  ambiguity exists in the single-source case
**Traces**: C-020;
`tests/test_coordinator.py::TestDataSourceTracking`,
`tests/test_sensor.py::TestFoxESSPolledSensor::test_data_source_exposed_as_attribute`,
`tests/test_sensor.py::TestFoxESSPolledSensor::test_data_source_absent_when_not_set`

### D-041: WS anomaly plausibility filter
**Decision**: Before injecting a WebSocket message into the coordinator,
check all power keys against the last accepted message. If any value
diverges by more than 10× from the last accepted value, drop the entire
message. Edge cases: first message (no reference) is always accepted;
near-zero reference (≤ 0.1 kW) is accepted (ramp-up from idle);
candidate value of 0 is accepted (genuine stop).
**Context**: The FoxESS WebSocket occasionally sends anomalous messages
where a single power value spikes to an impossible level (e.g. 50 kW
discharge from a 10 kW inverter). These corrupt the overview card,
taper profiles, and feed-in energy integration for the duration of the
bad value.
**Rationale**: The 10× threshold is large enough to accommodate genuine
rapid changes (e.g. cloud burst, EV charger starting) while catching
physically impossible values. Filtering at the WS layer (in
`realtime_ws.py`) rather than the coordinator keeps the coordinator
agnostic to data source quirks. The filter maintains its own
`_last_accepted` state that resets on reconnection.
**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: safety
**Alternatives considered**:
- Coordinator-level filter (original implementation): moved to WS layer
  because it mixed data-source-specific logic into the brand-agnostic
  coordinator
- Per-key clamping to inverter max: rejected because max power varies
  by installation and is not always known
**Traces**: C-004, C-005;
`tests/test_realtime_ws.py::TestIsPlausible` (11 tests),
`tests/test_realtime_ws.py::TestWsPlausibilityFilter` (3 tests)

### D-030: Data staleness indicator on Lovelace cards
**Decision**: Both Lovelace cards (overview and control) compute data
age client-side from the `_data_last_update` ISO timestamp stored by
the coordinator on each REST poll or WS push. When the age exceeds
30 seconds, the data source badge gains a `stale` CSS class (red
styling) and appends a human-readable age suffix (e.g. "ws 45s",
"api 3m"). The age is recomputed on each LitElement render cycle.
**Context**: The data source badge (D-021) tells the user WHICH source
is active, but not HOW FRESH the data is. During WS disconnection or
API polling gaps, the displayed values may be minutes old with no
visual indication. Users monitoring live discharge sessions need to
know whether displayed power values are current.
**Rationale**: Client-side computation avoids adding a new sensor
entity for a purely cosmetic concern. The 30-second threshold matches
C-005 (WS stale message filter) — if the WS itself considers data
stale at 30s, the UI should too. Red styling is a clear warning
without being disruptive.
**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other
**Alternatives considered**:
- Server-side staleness sensor: rejected because it adds entity
  overhead for a display-only concern
- Separate "last updated" text below the badge: rejected because it
  clutters the card for the common (non-stale) case
**Traces**: C-020;
`tests/e2e/test_ui.py::TestOverviewCard::test_data_source_badge_matches_mode`

### D-054: Rolling-median filter on WS-fed display power sensors

**Decision**: `FoxESSPolledSensor` applies a 3-sample rolling median
to the seven WS-fed instantaneous power channels at the **display
layer only**: `batChargePower`, `batDischargePower`, `loadsPower`,
`pvPower`, `gridConsumptionPower`, `feedinPower`, `meterPower`
(declared in `_WS_MEDIAN_FILTERED_VARIABLES`). Each filtered
sensor owns its own `collections.deque(maxlen=3)`; `native_value`
appends the latest coordinator value and returns `_median_of_three()`
— the most recent sample while the window holds fewer than three
entries, otherwise the median. Cumulative energy counters, SoC,
voltage, current, temperature, and frequency are **not** filtered
because smoothing distorts their semantics (long-term statistics,
monotonicity, physical lag). `coordinator.data[...]` retains the
raw values at all times; listeners, safety guards, and the pacing
algorithms continue to read unfiltered data via
`_get_coordinator_value`. When the raw reading is `None` the
filter window is preserved (so recovery returns to a clean median
as soon as data resumes) but the displayed state becomes
unavailable — unavailability surfaces through the filter rather
than being papered over with a stale sample.

**Context**: Production incident 2026-04-27 during a smart
discharge session — the display sensors for discharge power, grid
export and house load showed 49 single-sample dips within a 2-hour
window (11 below 2 kW, down to 0.82 kW while real output was
~5.4 kW). Root cause was a mix of partial / stale ~5 s WS frames
and energy-counter quantisation glitches. The control loop was
unaffected because it reads unfiltered `gridConsumptionPower`,
which stayed at 0 throughout — C-001 (no grid import) held — but
the dashboard experience was severely degraded.

**Rationale**: A 3-sample rolling median masks exactly the class
of defect observed (single-frame outliers) while introducing at
most one WS tick of display latency — ~5 s on the cadence the WS
already runs at, and zero latency for the very first sample after
a reconnect. The filter sits **below the C-038 listener-formula-
parity boundary**: listeners and the sensor's public display path
both still compute the same quantity; the filter intercepts only
the sensor's `native_value` output. Energy totals and non-power
channels are deliberately excluded — any smoothing on a monotonic
total would either inject monotonicity violations or lag the HA
statistics integration. An alternative location (coordinator-level
filtering before the listener reads it) was rejected because the
listener's safety math (C-001, C-017) must see the raw high-frequency
truth; any coordinator-level smoothing would mask genuine grid
events that pacing is supposed to react to.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none (within-priority precision/latency tradeoff;
display lag bounded by one WS tick)
**Classification**: pacing

**Alternatives considered**:
- **Coordinator-level smoothing** — rejected: would change what
  listeners and safety guards see, breaking C-038 parity and
  potentially masking the real-time truth pacing needs to react
  to.
- **Longer window (5 or 7 samples)** — rejected: the single-frame
  defect is by construction a 1-sample event; a 3-median
  eliminates it while keeping the display within one WS tick of
  truth. A longer window adds perceptible lag during genuine
  rapid changes (cloud burst, EV charger starting).
- **IIR low-pass filter** (exponentially-weighted mean) — rejected:
  bleeds the outlier's effect across subsequent frames and
  requires a decay-rate tuning parameter the median avoids
  entirely.
- **Filter only the specific channels that dipped in 2026-04-27**
  — rejected: the defect mechanism (partial frames, quantisation)
  is shared by all seven WS power channels; filtering only some
  would leave the others unprotected.

**Traces**: C-020 (display quality under glitchy WS frames),
C-038 (filter sits below the parity boundary — same formula both
sides, sensor path adds a post-hoc smoother);
`tests/test_sensor.py::TestFoxESSPolledSensor` rolling-median
cases.

### D-061: Additive external-solar source for AC-coupled installs
**Decision**: A cloud-mode-only optional config
`additional_pv_power_variable` (e.g. `meterPower2`) names a FoxESS
telemetry variable whose value is **added to `pvPower`** so the control
algorithm sees true total generation. The REST poll fetches the named
variable and adds it to `pvPower`; the added value is **cached and
re-added on every WS frame** (the WS `wsmaitian` schema does not carry
the extra variable). The term is **raw additive — no clamp** — and the
feature is **off by default** (blank value). A persistently-missing
configured variable surfaces **one** config-category operational error
after **3 consecutive polls** (C-020/C-026), then stays quiet.
**Context**: On AC-coupled installs a separate grid-tied inverter's
generation is invisible to the hybrid inverter's `pvPower`, but FoxESS
sometimes reports it on a second CT channel (`meterPower2`). With that
generation unmeasured, the power-balance grid-direction inference
(D-010) diverges and persistently swaps direction (GH issue #3, the
case D-010's fallback was added to detour). Without a way to fold the
external term in, the algorithm under-counts generation and the WS
direction inference stays degraded.
**Rationale**: Reading the variable on the REST poll and *holding* the
value across WS frames keeps the brand layer from coupling to the
undocumented `wsmaitian` frame schema — the extra term lives entirely
in the cloud poll path, while WS frames simply re-apply the last-known
hold. Adding raw (no clamp) keeps the result CT-orientation honest: the
installer's CT wiring decides the sign, and the integration does not
second-guess it. Feeding the external term into `pvPower` is also a
side benefit for the D-010 balance: with generation now measured, the
grid-direction inference that previously diverged on unmeasured
external solar converges. Config is read via `IntegrationConfig`
(C-035), never raw `entry.options`.
**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other
**Boundary**: brand-layer (coordinator) only. The algorithm reads
`pvPower` unchanged; `smart_battery/` is untouched, so C-021/C-039
(brand-agnostic core, dependency inversion) are preserved — the core
never learns that `pvPower` may include an external term.
**Alternatives considered**:
- Read the extra variable from the WS frame too: rejected — couples
  the brand layer to the undocumented `wsmaitian` schema; the REST-hold
  avoids it.
- Clamp the added term to >= 0: rejected — CT orientation is the
  installer's call; clamping would hide a reversed CT instead of
  reporting it honestly.
- Push the summation into `smart_battery/`: rejected — would force the
  brand-agnostic core to know about a FoxESS-specific telemetry
  variable, violating C-021/C-039.
- Hard error / refuse setup when the variable is missing: rejected —
  surface one diagnostics error after 3 polls (C-020/C-026) and keep
  running on `pvPower` alone rather than break the integration.
**Traces**: C-035, C-021, C-039, C-020, C-026; D-010 (grid-direction
balance the external term improves).

## Key Behaviours

- WebSocket requires web portal credentials (username + MD5(password)),
  not the Open API key.
- Token URL-encoded to handle `+` and `=` characters.
- Exponential backoff reconnection: 5 attempts, base 5s, max 60s, jitter.
  The reconnect loop consults the `should_reconnect` gate (wired to
  `_should_start_realtime_ws`) before each attempt — it will not revive a
  connection the gate says should be down (no active session), closing the
  idle reconnect leak (C-020, 2026-06-07).
- Feed-in energy is integrated trapezoidally between REST polls for
  more accurate cumulative tracking.
- Interpolated SoC (`_soc_interpolated`) is stored at full float
  precision in coordinator data. Rounding is applied only for change
  detection (2dp gate to prevent entity update storms).

## Edge Cases

- **Web credentials not configured**: WebSocket silently disabled.
  Integration falls back to REST-only mode.
- **Token expired**: `FoxESSWebSession` refreshes proactively (12h TTL).
- **Connection lost**: Reconnects with backoff. After 5 failures, gives
  up and calls `on_disconnect`.
- **First message stale**: Filtered by `timeDiff > 30` check.

## UI Principles

Lightweight patterns that enforce C-020 (operational transparency) on
the Lovelace cards. These don't warrant full D-NNN entries but should
be preserved during refactoring.

- **Never hide real data**: if a sensor has a numeric value, show it
  regardless of magnitude. A 3W house load is useful information —
  greying it out or replacing with "—" implies the data is missing.
- **Progress section only when meaningful**: the progress header and
  bars are hidden during "scheduled" phase (before the session window
  opens) to avoid an empty section.
- **Data source badge only when ambiguous**: the badge appears when
  the user has configured multiple potential sources (WS credentials
  or entity mode). Single-source users see no badge.
- **Error state over false idle**: when no session is active but the
  last session ended with an error, show "error" rather than "idle"
  so the user knows something went wrong.
- **SoC precision matches confidence**: the card shows integer SoC
  until the first confirmed integer change (e.g. 93→92), then switches
  to 2dp. Before the first change, the interpolated value is just an
  estimate; after the change, the real SoC is known to be near X.5,
  making interpolation meaningful.
