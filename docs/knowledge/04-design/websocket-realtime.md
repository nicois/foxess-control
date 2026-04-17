---
project: FoxESS Control
level: 4
feature: WebSocket Real-Time Data
last_verified: 2026-04-17
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
`e2e/test_e2e.py::TestDataSource::test_ws_always_connects_without_session`,
`e2e/test_e2e.py::TestDataSource::test_ws_mode_persists_via_options_flow`

### D-009: Post-session linger timeout
**Decision**: After a smart session ends, keep the WebSocket open for
30 seconds to capture one more fresh data push before disconnecting.
**Context**: After the session ends and the inverter reverts to self-use,
the REST API may still return the old snapshot for up to 5 minutes.
**Rationale**: One more WebSocket push (~5s) injects fresh
post-session values so the overview card immediately reflects reality.
**Alternatives considered**:
- Immediate disconnect: rejected because UI shows stale state for
  minutes
- Keep WS open until next REST poll: rejected as wasteful (up to 5 min)
**Known issue** (regression): The linger races with the override
removal API call. The cancel hook fires `_stop_realtime_ws` as a
fire-and-forget task, and `_remove_discharge_override()` runs
concurrently. If the WS push arrives before the API removes the
override, the linger captures still-discharging values and then
disconnects — leaving stale high power on the card until the next
REST poll (~5 min). See the sequence diagram in
`session-management.md` (Session cancel with WS linger) for the
full async trace. The `always` ws_mode is unaffected because the
WS stays connected and delivers fresh post-session data within ~5s.
**Traces**: C-007, C-020

### D-010: Power balance for grid direction
**Decision**: Derive grid import/export from power balance
(`load + charge - discharge - solar`) rather than the `gridStatus` field.
**Context**: The `gridStatus` field from the WebSocket has inconsistent
meaning across firmware versions.
**Rationale**: Power balance is physically correct by conservation of
energy. The `gridStatus` field is only used as fallback when solar or
load data is missing.
**Alternatives considered**:
- Trust `gridStatus`: rejected after observing incorrect values with
  certain firmware
- Ignore grid direction entirely: rejected because feed-in energy
  integration requires it
**Traces**: C-006;
`tests/test_realtime_ws.py::TestMapWsToCoordinator::test_grid_importing_from_balance`


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

## Key Behaviours

- WebSocket requires web portal credentials (username + MD5(password)),
  not the Open API key.
- Token URL-encoded to handle `+` and `=` characters.
- Exponential backoff reconnection: 5 attempts, base 5s, max 60s, jitter.
- Feed-in energy is integrated trapezoidally between REST polls for
  more accurate cumulative tracking.

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
