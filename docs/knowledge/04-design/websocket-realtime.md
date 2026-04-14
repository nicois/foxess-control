---
project: FoxESS Control
level: 4
feature: WebSocket Real-Time Data
last_verified: 2026-04-14
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

### D-008: Conditional WebSocket activation
**Decision**: The WebSocket connects only when discharge power is paced
below inverter maximum (`last_power_w < max_power_w`). At full power,
5-minute REST polling is sufficient because there's ample headroom.
**Context**: WebSocket uses a separate web session (username + MD5
password) from the Open API key. It's an extra connection with
reconnect complexity.
**Rationale**: The risk window is specifically when paced power is near
house load — that's when a load spike can cause grid import. At full
power, there's >10x headroom and no risk.
**Alternatives considered**:
- Always-on WebSocket: rejected as unnecessary connection overhead
  and complexity
- WebSocket during all sessions: available via `ws_all_sessions` toggle
  but off by default
**Traces**: C-005;
`tests/test_realtime_ws.py::TestStaleness`

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
**Traces**: C-007

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
