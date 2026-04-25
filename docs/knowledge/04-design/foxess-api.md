---
project: FoxESS Control
level: 4
feature: FoxESS Cloud API Integration
last_verified: 2026-04-21
traces_up: [../02-constraints.md, ../03-architecture.md]
traces_down: [../05-coverage.md, ../06-tests.md]
---
# Design: FoxESS Cloud API Integration

## Overview

The FoxESS Cloud API is the control plane for inverter mode management.
It has numerous undocumented behaviours and quirks that require careful
handling. These are documented in `API_DEVIATIONS.md` and encoded as
constraints and sanitisation logic.

## Design Decisions

### D-014: Schedule group sanitisation on read-before-write
**Decision**: Before writing schedule groups back to the API, strip
unknown fields, filter placeholders, clamp `fdSoc >= 11`, and ensure
`minSocOnGrid <= fdSoc`.
**Context**: The API's read endpoint returns groups with extra fields
and sometimes-invalid values that the write endpoint rejects.
**Rationale**: Defensive programming against an inconsistent API.
Without sanitisation, common operations (set force charge) fail silently.
**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: safety
**Alternatives considered**:
- Build groups from scratch: rejected because existing non-conflicting
  groups must be preserved
- Cache last-written state: rejected because the app/web portal may
  modify schedules between reads
**Traces**: C-008, C-010, C-011;
`tests/test_init.py::TestSanitizeGroup`,
`tests/test_init.py::TestMergeWithExisting`

### D-015: WASM signature generation
**Decision**: Use a pre-built WASM module (reverse-engineered from the
FoxESS web portal JavaScript) for request signing, loaded via `wasmtime`.
**Context**: The FoxESS web portal API requires a specific signature
header. The algorithm is obfuscated in the portal's JavaScript.
**Rationale**: WASM is the only reliable way to reproduce the exact
signature algorithm. Pure Python re-implementation would be fragile
against portal updates.
**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: other
**Alternatives considered**:
- Pure Python port: rejected as too fragile; algorithm is obfuscated
- Headless browser: rejected as too heavy for HA environment
**Traces**: `tests/test_realtime_ws.py::TestGenerateSignature`

### D-016: Unmanaged mode protection
**Decision**: Service calls check for unmanaged modes (e.g., Backup) in
the existing schedule and raise a validation error rather than
overwriting them.
**Context**: Users may configure Backup mode via the FoxESS app for
outage protection. The integration assumes SelfUse as baseline.
**Rationale**: Silently overwriting a Backup schedule could leave the
home unprotected during an outage.
**Priority served**: P-002 (Respect minimum state of charge)
**Trades against**: none
**Classification**: safety
**Alternatives considered**:
- Force-overwrite with warning: rejected because the consequence
  (no backup during outage) is too severe
- Manage all modes: rejected as scope creep
**Traces**: C-018;
`tests/test_init.py::TestMergeWithExisting::test_rejects_schedule_with_backup_mode`

### D-033: BMS battery temperature via web portal API
**Decision**: Expose the BMS cell temperature as a sensor
(`sensor.foxess_bms_battery_temperature`) by querying
`GET /dew/v0/device/detail?id=<compound_id>&category=battery` where
the compound ID is `{batteryId}@{batSn}` discovered from the WebSocket
`bat` node. Discovery uses a one-shot WebSocket connection at startup
(`async_discover_battery_id` on `FoxESSWebSession`), reading the first
non-stale message and extracting the compound ID. The temperature is
at `result.battery.temperature.value`.
**Context**: The Open API's `batTemperature` reports the inverter's
own temperature sensor, not the BMS cell temperature. Low BMS cell
temperatures (e.g. 14.9°C in winter) inhibit charge rate — the BMS
limits current to protect cell health — but this is invisible when
only the inverter sensor (~22°C) is displayed.
**Rationale**: The BMS temperature is operationally critical for
understanding why charge rates are lower than expected. It's only
available via the web portal, not the Open API. The compound ID
discovery via WebSocket avoids needing the internal device UUID
(which required a separate `/generic/v0/device/list` call that
rejected tokens from some accounts).
**Evolution**: Originally used `POST /generic/v0/device/list` +
`POST /generic/v0/device/battery/info` (device UUID lookup + battery
info). Changed to `/dew/v0/device/detail` after discovering the
`/generic/v0/` endpoints rejected tokens for some accounts.
**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: other
**Alternatives considered**:
- Use the Open API `batTemperature` as an approximation: rejected
  because the 7°C discrepancy observed in production makes it
  misleading
- Wait for Modbus BMS register: rejected because not all users have
  Modbus hardware
- `/generic/v0/device/battery/info` via device UUID: replaced because
  some accounts' tokens are rejected by `/generic/v0/` endpoints
**Traces**: C-020 (operational transparency);
`tests/test_web_session.py::TestBMSBatteryTemperature`

### D-034: HA-managed aiohttp session for web operations
**Decision**: `FoxESSWebSession` accepts an optional
`aiohttp.ClientSession` from HA's `async_get_clientsession()`.
When provided, the session is shared with HA for proper SSL, proxy,
and lifecycle management. Tracks `_owns_session` to avoid closing a
shared session.
**Context**: The web session was previously creating its own
`aiohttp.ClientSession`, bypassing HA's SSL certificate handling,
proxy configuration, and lifecycle tracking.
**Rationale**: HA best practice — shared sessions respect system-wide
configuration and are properly cleaned up on shutdown.
**Priority served**: P-007 (Engineering process integrity)
**Trades against**: none
**Classification**: other
**Alternatives considered**:
- Always create own session: rejected because it bypasses HA's SSL
  and proxy settings, causing failures in some environments
**Traces**: C-024 (safe state — proper cleanup on unload)

### D-042: Automatic auth retry on web portal API errors
**Decision**: Both `async_get` and `async_post` on `FoxESSWebSession`
retry once on auth errors (errno 41808 or 41809) by invalidating the
cached token and re-authenticating before the second attempt. WASM
signature generation is offloaded to the executor via
`_async_make_headers` to avoid blocking the event loop.
**Context**: The FoxESS web portal occasionally rejects a previously
valid token (errno 41808 = invalid token, 41809 = expired signature).
This happens mid-session when the cloud rotates credentials. Before
the retry, any BMS temperature fetch or battery ID discovery that hit
this error would fail permanently until the next login cycle.
**Rationale**: A single retry with re-authentication handles the common
case (token rotated) without open-ended retry loops. The executor wrap
for WASM signatures ensures the CPU-bound signature computation doesn't
block the HA event loop.
**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: other
**Alternatives considered**:
- Proactive token refresh on every request: rejected as wasteful
- Multiple retries with backoff: rejected because auth errors are
  binary (token valid or not), not transient
**Traces**: C-024 (safe state — resilience to transient auth failures);
`tests/test_web_session.py::TestRetryOnAuthError`

### D-049: Dual-layer SCHEDULE_WRITE emission
**Decision**: Inverter schedule writes emit the `SCHEDULE_WRITE`
structured event at **two** layers:
1. **Listener/service layer** (`smart_battery/listeners.py`,
   `smart_battery/services.py` via `emit_schedule_write()`): fires
   when the pacing algorithm or a service handler *decides* to
   write a schedule. Payload: `{mode, power_w, fd_soc, call_site}`
   — the intent-level record (one per listener/service call).
2. **API layer** (`custom_components/foxess_control/foxess/inverter.py::_post_schedule`
   via `emit_event(SCHEDULE_WRITE, ...)` ): fires when the
   schedule write actually reaches the FoxESS
   `/op/v0/device/scheduler/enable` endpoint. Payload:
   `{groups, response, endpoint, call_site}` — the wire-level
   record (one per HTTP POST). Routed through `_post_schedule`
   as the single funnel so `Inverter.set_schedule` and
   `Inverter.set_work_mode` both produce exactly one API-level
   event per write.
**Context**: The replay + simulator-validation infrastructure
(D-027's structured events) needs to disambiguate "the
integration decided to write" from "the integration actually
wrote". The listener-layer event captures decisions that may
later be coalesced, retried, or cancelled; the API-layer event
captures what actually crossed the wire. Live-trace collection
on 2026-04-25 confirmed both layers are observable and they
emit independently: a charge-adjust decision may emit one
listener-layer event and (because of D-014 sanitisation and
retries) zero or one API-layer event.
**Rationale**: Simulator validation needs the wire-level record
to assert the simulator's HTTP response matches what the real
API returned; algorithm-regression replay needs the intent-level
record to re-invoke the decision with the exact pre-sanitisation
inputs. Emitting at both layers is the simplest way to satisfy
both consumers without the consumer having to reconstruct
missing context from adjacent records. Cost: two records per
write instead of one; the events are small and the debug-log
sensor's ring buffer handles the volume fine.
**Priority served**: P-007 (Engineering process integrity)
**Trades against**: none — both records are cheap; no runtime
behaviour changes
**Classification**: other
**Alternatives considered**:
- Emit only at the API layer: rejected because the groups passed
  to the API have already been sanitised by D-014, so the replay
  harness can't reconstruct the pre-sanitisation inputs the
  algorithm actually produced.
- Emit only at the listener layer: rejected because the API
  response is not visible there, so simulator-validation loses
  the ability to assert response agreement.
- Combine both into one event: rejected because the listener-layer
  emission fires first (synchronous decision), then the API call
  runs in an executor thread (D-050 path). Emitting one combined
  record would require awaiting the API call from the listener
  (blocking) or queueing the listener-layer half until the API
  response arrives (stateful). Two independent events are simpler.
**Priority served**: P-007 (Engineering process integrity)
**Trades against**: none
**Classification**: other
**Traces**: C-008, C-009, C-010, C-011 (the API contracts each
schedule write must respect);
`smart_battery/events.py::emit_schedule_write` (listener-layer helper),
`smart_battery/listeners.py` (6 call sites),
`smart_battery/services.py` (6 call sites),
`custom_components/foxess_control/foxess/inverter.py::_post_schedule` (API-layer funnel);
`tests/test_events.py::TestInverterScheduleWriteEmission` (4),
`tests/test_events.py::TestInverterScheduleWriteReachesParentHandler` (3),
`tests/replay_traces/sample_schedule_write.jsonl` (replay regression fixture).

## Key Behaviours

- Rate limit handling: errno 40400 retried up to `RATE_LIMIT_RETRIES`
  times with backoff.
- Transient HTTP errors (502, 503) retried up to `TRANSIENT_RETRIES`.
- Auth errors (errno 41808/41809): single retry with re-authentication.
- WASM signatures computed in executor to avoid blocking event loop.
- Minimum request interval: 5 seconds between API calls.
- Device capacity cached after first query (avoids repeated API calls).
- Battery compound ID discovered via one-shot WebSocket at startup.

## Edge Cases

- **Null schedule response**: Some inverter modes (set via app) return
  null from `scheduler/get`. Normalised to empty list.
- **Past groups retained**: Groups with `endHour` in the past are kept
  because FoxESS schedules recur daily.
- **Full-day SelfUse baseline**: A 00:00-23:59 SelfUse group (default
  schedule) is dropped to make room for force actions.
