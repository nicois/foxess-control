---
project: FoxESS Control
level: 4
feature: Local gateway backend (foxess-local)
last_verified: 2026-11-19
traces_up: [../02-constraints.md, ../03-architecture.md]
traces_down: [../05-coverage.md, ../06-tests.md]
---
# Design: Local Gateway Backend (foxess-local)

## Overview

foxess-local is a separately developed gateway that owns the inverter's
connection on the LAN: it intercepts the inverter's cloud uplink and
serves a REST + Server-Sent Events API (`/api/v1`, contract at
`<gateway>/api/v1/openapi.yaml`). The integration's **local backend**
talks to that API instead of the FoxESS Cloud.

Reasons it exists: no cloud round-trip, no Open API rate limits, live
5 s telemetry without the web-portal WebSocket, and control that keeps
working when the gateway is set to deny cloud writes
(`forward_mode: telemetry_only`), which blocks cloud-mode control.

What the integration needed from the gateway, beyond its initial
contract, is in
[`docs/api/foxess-local-gateway-requirements.md`](../../api/foxess-local-gateway-requirements.md):
lifetime energy counters and write-budget visibility. The gateway
implemented both on 2026-11-19.

## Data flow

```
foxess-local gateway (LAN)
   |  GET /api/v1/state (60 s)        GET /api/v1/events (SSE, ~5 s)
   v                                   v
FoxESSLocalClient  --map_state-->  FoxESSLocalCoordinator.data  <--map_snapshot_event--
   (foxess/local_client.py)            (coordinator.py)
        |
        +--> sensors, cards, diagnostics (same keys as the cloud path)
```

`map_state` / `map_snapshot_event` translate the gateway's signed watts
into the cloud-shaped coordinator variables every consumer already reads
(directional kW, `SoC`, cumulative kWh, `_work_mode`). So sensors,
cards and listeners need no backend-specific branches. `_data_source`
is `"local"`, and `_data_last_update` is the inverter frame's own
timestamp, never "now".

## Design Decisions

### D-064: Local backend via the foxess-local gateway, read-only first
**Decision**: A third connection mode (`entry.data.backend = "local"`,
plus `local_url` / `local_token`), chosen from the config-flow menu next
to Cloud and Entity. Phase 1 is **read-only**. Setup builds
`FoxESSLocalCoordinator` and no cloud `Inverter`. Every control service
reaches the inverter through `_helpers._get_inverter`, and that refuses
with `local_control_unavailable`. Phase 2 adds control by giving the
local client the same `Inverter` methods the integration already uses
(`get_schedule`, `set_schedule`, `set_scheduler_enabled`, `get_min_soc`,
…). That way `FoxESSCloudAdapter`'s schedule merge, the C-018
unmanaged-mode guard, C-025 cleanup and the scheduler handback are
reused, not re-implemented.
**Context**: The gateway's schedule API is a whole-table replace of the
same 8-group structure the cloud scheduler exposes. Its writes are
confirmed by read-back, and rationed to `limit` writes per setting per
rolling hour (HTTP 429 `write_budget` beyond that).
**Rationale**: The data path can be verified against a live gateway
before anything writes to the inverter. The unique id is the inverter
serial, as in cloud mode, so one inverter cannot be configured twice
through different backends. The gateway's `battery.capacity_wh`
pre-fills `battery_capacity_kwh`, which discharge pacing needs.
**Priority served**: P-005 (operational transparency): a local, rate-limit-free
data source. Trades against: P-001/P-002/P-003 are not served by the
local backend until phase 2; it refuses rather than degrades.
**Open for phase 2**: pacing against `write_budget` (smart discharge can
otherwise exhaust the schedule budget, after which a 429 counts as an
adapter error toward C-024's circuit breaker). The last writes must be
kept for P-001/P-002 safety actions. Also: re-baselining a session's
feed-in start on a `counter-reset` system event.

### D-065: Always-on event stream, liveness from the gateway plus a heartbeat
**Decision**: `FoxESSLocalCoordinator.async_run_stream` runs for the life
of the config entry, as an entry background task, so HA cancels it on
unload (C-025). It is not gated on session state the way the cloud
WebSocket is (D-008). Snapshots overlay power/SoC/counters in place
without resetting the `/state` poll timer. `config` events apply the
full state. `resync`/`gap` trigger a refresh. Liveness comes from the
gateway's own `online` flag and `connection` events (server-computed;
never recomputed from timestamps, because of clock skew). As well, a
stream silent for longer than 20 s marks the inverter offline and
reconnects, with exponential backoff to 60 s, resuming with
`Last-Event-ID`.
**Context**: The cloud WebSocket's session gating was the source of the
1.0.21 idle-leak series, and exists only because that stream costs
cloud resources. A LAN SSE stream costs nothing. A half-open TCP
connection, though, would otherwise freeze the last values on screen
while still looking connected.
**Rationale**: One reconciliation-free lifecycle (start on setup, stop
on unload) leaves no gate for paths to disagree on. The 5 s snapshot is
the heartbeat the gateway documents, so silence is the signal. A
refused or unreachable `/state` raises `UpdateFailed` (entities go
unavailable). The cloud path instead keeps the last values; the
gateway is on the LAN, so failing loudly is cheap and honest (C-020). The
card's stale threshold for `local` is 180 s: three missed 60 s polls,
which is what remains when the stream is down.
**Priority served**: P-005 (C-020, C-026).
