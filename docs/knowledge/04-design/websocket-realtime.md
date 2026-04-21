---
project: FoxESS Control
level: 4
feature: WebSocket Real-Time Data
last_verified: 2026-04-21
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
  (`discharging_started=True` and `last_power_w < max_power_w`).
  Requires web credentials and cloud mode (not entity mode).
- **smart_sessions**: WS connects during any started smart session
  (charge or discharge, including deferred phases) or force operation.
- **always**: WS connects at integration startup and stays connected
  regardless of session state. A watchdog timer (at the polling
  interval) re-establishes the connection after transient failures.

All modes require web credentials and cloud mode. WS-triggered
recalculations are debounced at 10 seconds (`_WS_DEBOUNCE_SECONDS`).
The FoxESS-specific `__init__.py` wraps the brand-agnostic discharge
callback to call `_maybe_start_realtime_ws` after every check, ensuring
WS activates on deferred→active transitions.

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
**Alternatives considered**:
- Keep the boolean toggle: rejected because it couldn't express
  "always connected" without overloading the meaning
- Per-session-type toggles: rejected as too granular
**Traces**: C-005, C-020;
`tests/test_realtime_ws.py::TestStaleness`,
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
**Alternatives considered**:
- Server-side staleness sensor: rejected because it adds entity
  overhead for a display-only concern
- Separate "last updated" text below the badge: rejected because it
  clutters the card for the common (non-stale) case
**Traces**: C-020;
`tests/e2e/test_ui.py::TestOverviewCard::test_data_source_badge_matches_mode`

## Key Behaviours

- WebSocket requires web portal credentials (username + MD5(password)),
  not the Open API key.
- Token URL-encoded to handle `+` and `=` characters.
- Exponential backoff reconnection: 5 attempts, base 5s, max 60s, jitter.
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
