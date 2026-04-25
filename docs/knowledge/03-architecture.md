---
project: FoxESS Control
level: 3
last_verified: 2026-04-24
traces_up: [02-constraints.md]
traces_down: [04-design/]
---
# Architecture

## Overview

FoxESS Control is a Home Assistant custom integration split into two
layers: a **brand-agnostic smart battery core** (`smart_battery/`) that
contains all pacing algorithms, session management, sensors, and service
scaffolding; and a **FoxESS-specific client** (`foxess/`) that handles
cloud API authentication, schedule group management, and WebSocket
real-time data. The integration entry point (`__init__.py`) orchestrates
these layers and contains FoxESS-specific session logic that should
eventually be extracted.

## Module Boundaries

### `smart_battery/` — Shared multi-brand core
**Path**: `custom_components/foxess_control/smart_battery/`
(canonical copy also at root `smart_battery/`)
**Responsibility**: Pure pacing algorithms, session state machines,
sensor base classes, service registration, taper model, session
persistence, configuration flow base, entity-mode adapter.
**Why separate**: This code is brand-independent. Extracting it enables
reuse across Huawei, SolaX, Sungrow, etc. without duplicating the
pacing logic. The `InverterAdapter` protocol is the abstraction boundary.
**Key interfaces**: `InverterAdapter` (Protocol), `EntityAdapter` (impl),
`calculate_charge_power()`, `calculate_discharge_power()`,
`should_suspend_discharge()`, `TaperProfile`,
`SessionContextFilter` (structured logging),
`create_charge_session()`, `create_discharge_session()` (factory
functions centralising session state construction).
Entity mode maps 9 coordinator variables (SoC, loadsPower, pvPower,
feedin, _work_mode, batChargePower, batDischargePower,
gridConsumptionPower, feedinPower) from HA entities via
`_ENTITY_VAR_MAP` 3-tuples `(conf_key, var_name, expected_unit)`.
The `EntityCoordinator` reads each entity's `unit_of_measurement`
attribute and converts to the expected unit using HA's built-in
`PowerConverter`, `EnergyConverter`, and `TemperatureConverter`.

### `foxess/` — FoxESS Cloud API client
**Path**: `custom_components/foxess_control/foxess/`
**Responsibility**: HTTP API authentication and request signing (WASM),
inverter device control (work modes, schedule groups), WebSocket
real-time stream, web portal session management.
**Why separate**: All FoxESS-specific API logic is isolated here. Other
brands would have their own equivalent package (e.g., `huawei/`,
`solax/`). Nothing in `smart_battery/` imports from `foxess/`.
**Key interfaces**: `FoxESSClient`, `Inverter`, `FoxESSRealtimeWS`,
`FoxESSWebSession`, `generate_signature()`.

### `__init__.py` — Integration entry point and orchestration
**Path**: `custom_components/foxess_control/__init__.py` (~1600 lines)
**Responsibility**: HA `async_setup_entry` / `async_unload_entry`,
smart charge/discharge session orchestration (FoxESS-specific schedule
merging, override application, WebSocket lifecycle, power adjustment
callbacks).
**Architecture**: Service handlers extracted to `_services.py` (~800
lines) and shared helpers to `_helpers.py` (~340 lines). Config
accessors consolidated into frozen `IntegrationConfig` dataclass in
`domain_data.py`. Entity-mode dispatch delegates to
`FoxESSEntityAdapter`.

### `_services.py` — Service handler registration
**Path**: `custom_components/foxess_control/_services.py`
**Responsibility**: All six HA service handlers (clear_overrides,
force_charge, force_discharge, feedin, smart_charge, smart_discharge)
plus error translation decorator and registration function. Force
operations (`force_charge`, `force_discharge`) delegate to the shared
`_do_smart_charge` / `_do_smart_discharge` with `full_power=True`
(D-045), unifying session lifecycle across force and smart operations.
**Why separate**: Extracted from `__init__.py` to reduce its size from
~2500 to ~1600 lines. Uses late imports to break circular dependencies.

### `_helpers.py` — Shared utility functions
**Path**: `custom_components/foxess_control/_helpers.py`
**Responsibility**: Common helper functions deduplicated from
`__init__.py` and `_services.py` (type construction, domain data
access, schedule utilities).
**Why separate**: Eliminates duplication between `__init__.py` and
`_services.py` without creating circular imports.

### `coordinator.py` — Data coordinators
**Path**: `custom_components/foxess_control/coordinator.py`
**Responsibility**: `FoxESSDataCoordinator` (REST API polling + WS data
injection), `FoxESSEntityCoordinator` (entity-mode wrapper). Both
expose the same `dict[str, float]` data shape to consumers.
**Why separate**: Decouples data acquisition from consumption. Sensors
and algorithms read from the coordinator without knowing the data source.

### `sensor.py` / `binary_sensor.py` — HA platform entities
**Path**: `custom_components/foxess_control/sensor.py`,
`custom_components/foxess_control/binary_sensor.py`
**Responsibility**: Concrete sensor and binary sensor entities.
Subclass `smart_battery/sensor_base.py` base classes.
**Why separate**: HA platform convention — each platform in its own module.

### `config_flow.py` — Setup and options UI
**Path**: `custom_components/foxess_control/config_flow.py`
**Responsibility**: FoxESS-specific config flow (API key, device serial,
web credentials) plus shared battery options from
`smart_battery/config_flow_base.py`.

## Data Flow

```
FoxESS Cloud API (REST, 5-min polls)
        |
        v
FoxESSDataCoordinator.data  <--- FoxESSRealtimeWS (WS, 5-sec pushes)
        |                          (merges via inject_realtime_data)
        v
   FoxESSControlData (hass.data["foxess_control"])
        |
        +---> entry.runtime_data -> FoxESSEntryData (coordinator, inverter)
        +---> Sensors (read coordinator.data for display)
        +---> Smart Session Listeners (read SoC, load, PV for pacing)
                    |
                    v
              algorithms.py (pure functions)
                    |
                    v
              Inverter.set_work_mode() / EntityAdapter.apply_mode()
                    |
                    + (discharge with export-limit actuator configured)
                    |  adapter.set_export_limit_w()  ← D-047
                    |  modulates hardware export each tick
                    v
              FoxESS Cloud API / HA entity writes
```

## Key Abstractions

### InverterAdapter (Protocol)
**What**: Interface for controlling an inverter's work mode, power,
and hardware export limit.
**Why**: Decouples smart session logic from brand-specific API calls.
The same `listeners.py` state machine works with any brand that
implements the protocol's methods.
**Methods**:
- `apply_mode(hass, mode, power_w=None, fd_soc=11)` — set work mode
  and optional forced-discharge/charge power and SoC floor.
- `remove_override(hass, mode)` — revert to self-use.
- `get_max_power_w()` — report the inverter's maximum power (W).
- `set_export_limit_w(hass, value_w)` — write to the hardware
  export-limit actuator; used by smart discharge to modulate feed-in
  at the hardware layer instead of mutating the cloud schedule's
  `fdPwr` field (see D-047). Adapters without a configured
  export-limit entity should no-op.
- `get_export_limit_w(hass)` — read current actuator value (W), or
  `None` when no actuator is configured.
**Implemented by**: `EntityAdapter` (generic, entity-based, including
export-limit entity wiring), FoxESS `foxess_adapter.py` (cloud API,
wraps `Inverter` class + optional FoxESS Modbus export-limit entity).

### TaperProfile
**What**: SoC-indexed histogram of observed charge/discharge acceptance
ratios with EMA smoothing.
**Why**: BMS limits throughput at high/low SoC (constant-voltage phase).
Without taper awareness, time estimates are wrong, causing premature
max-power bursts or sessions finishing early.
**Implemented by**: `smart_battery/taper.py`.

### Typed Domain Data (`domain_data.py`)
**What**: `FoxESSControlData` (domain-level) and `FoxESSEntryData`
(per-config-entry) dataclasses that replace the untyped
`hass.data[DOMAIN]` dict. `IntegrationConfig` is a frozen dataclass
snapshot of config entry options, built once at setup time.
**Why**: HA 2024.x+ best practice. Typed `entry.runtime_data` gives
IDE autocomplete, catches key typos at lint time, and enables
`_dd()` helper for consistent typed access. The bridge layer
(`__getitem__`, `__contains__`, `get`) was fully removed after
completing migration — all access is now via typed attributes.
**Implemented by**: `domain_data.py`, `smart_battery/domain_data.py`.

### DataUpdateCoordinator
**What**: HA polling coordinator that fetches data on an interval and
notifies listeners.
**Why**: Standard HA pattern. WebSocket data is injected between polls
via `async_set_updated_data()`, giving sensors immediate updates.
**Implemented by**: `FoxESSDataCoordinator`, `EntityCoordinator`.

### `FoxESSWebSession` — Web portal API client
**Path**: `custom_components/foxess_control/foxess/web_session.py`
**Responsibility**: Web portal authentication (username/password), device
discovery via `/generic/v0/device/list`, BMS battery temperature retrieval
via `/dew/v0/device/detail?id=<compound_id>&category=battery` (D-033),
WASM signature generation.
**Lifecycle**: Accepts an optional HA-managed `aiohttp.ClientSession` from
`async_get_clientsession()` for SSL/proxy/cleanup integration (D-034). When
HA provides the session, `_owns_session=False` — the session is not closed
on teardown, ensuring HA manages the connection pool lifecycle.
**Why separate from FoxESSClient**: Web portal credentials (username/password)
differ from the Open API key. The web portal exposes data (BMS temperature)
not available through the Open API.

## External Dependencies

| Dependency | Role | Why chosen |
|---|---|---|
| `wasmtime` | WASM runtime for signature generation | FoxESS web portal requires a specific signature algorithm; WASM is the only available implementation (reverse-engineered from JS) |
| `aiohttp` | WebSocket client | Standard async HTTP/WS library; HA already uses it |
| `requests` | REST API client (sync) | Used in the FoxESS client for synchronous API calls within HA's executor |
| `voluptuous` | Schema validation | Used for service call and config flow schemas; ships with HA but used directly |
| `homeassistant` | HA framework | Target platform |

## Simulator Fidelity

The FoxESS simulator (`simulator/model.py`) models real inverter physics
to minimise deviations from production behaviour (C-033):

- **Charge taper**: Linear reduction from 100% acceptance at
  `charge_taper_soc` (90%) to 0% at SoC 100%, modelling BMS
  constant-voltage phase behaviour.
- **Discharge taper**: Linear reduction from 100% output at
  `discharge_taper_soc` (15%) to 0% at `min_soc`, modelling BMS
  low-SoC current limiting.
- **SelfUse at full SoC**: Excess solar routes to grid export when
  battery is at 100% (no phantom charging).
- **ForceCharge solar contribution**: Solar assists grid charging,
  reducing grid import.
- **Fuzzing**: ±2% random jitter on power readings prevents tests
  from overfitting to exact values.
- **Battery efficiency**: Configurable round-trip efficiency factor
  (default 1.0 = lossless). Charging stores less energy, discharging
  draws more.
- **MD5 signature validation**: Optional FoxESS-style request signing
  verification (`model.validate_signatures`). Returns errno 41808 on
  mismatch.
- **Per-endpoint rate limiting**: Configurable per-endpoint throttle
  (`model.rate_limit_seconds`). Returns errno 41807 when exceeded.
- **null_schedule fault**: Simulates real API returning null schedule
  data for testing graceful degradation.
- **fdSoc enforcement**: Simulator validates `fdSoc >= 11` on schedule
  writes, matching the real API constraint (C-008).
- **Hardware export-limit actuator**: Simulator models a per-instance
  export-limit value that clamps net grid export each tick, letting
  E2E tests exercise D-044 pacing and D-047 actuator writes without
  real hardware.

## Soak Test Infrastructure

The soak test suite (`tests/soak/`) runs real-time charge/discharge
sessions through the full HA integration + simulator stack, verifying
invariants throughout multi-hour simulated scenarios.

- **19 scenarios**: charge (basic, solar, spiky load, high-SoC
  taper, cold battery, large battery, solar exceeds target, solar then
  spike, heavy load during deferral, tight window) + discharge
  (basic, solar, solar exceeds load, spiky load, near min SoC, large
  battery) + combined (charge then discharge cycle).
- **ScenarioConfig**: Declarative scenario definition (session type,
  window, initial SoC, target SoC, load/solar profiles, battery
  capacity, temperature).
- **SoakRecorder**: Captures time-series data and violations during
  scenario execution for post-hoc invariant checking.
- **SQLite results store** (`results_db.py`): Persists inflection
  points (SoC, power, timestamps) across runs for cross-run comparison
  and regression detection.
- **PID-prefixed containers**: Each soak run uses PID-prefixed Docker
  container names to avoid collisions when multiple runs execute
  concurrently.
- **Nightly execution**: Triggered by systemd timer or CI workflow on
  tag-based schedules. The ExecStart script lives in
  `systemd/foxess-soak-run.sh` so bash expands `${TS}_${SHORT}` and
  `$$` correctly — the inlined unit-file form silently lost those
  expansions to systemd's own `%`/`$`-substitution layer.

## Live HA Session Collector

Paired with the soak suite, `scripts/collect_ha_session.py` captures
traces from a live HA instance for simulator validation and replay.
Two modes: `live` (polls every 5 s, appends per-session JSONL files,
auto-detects the log sensor across integration versions) and
`history` (reconstructs a past time range via `/api/history/period/`).

Each record is either `kind: "event"` (structured session event as
emitted by `smart_battery/events.py` — see D-027, D-049, D-050) or
`kind: "observation"` (a state change on a FoxESS sensor). The
pairing lets the simulator be validated by feeding the exogenous
observations in, replaying the algorithm decisions, and asserting
the simulator's state matches the HA observation within tolerance.

- **Credentials** live in `~/.config/foxess-collect-ha.env` (not in
  repo); `systemd/foxess-collect-ha.env.example` ships the template.
- **systemd service** (`systemd/foxess-collect-ha.service`):
  `Type=simple`, `Restart=always`, `RestartSec=10min`,
  `StartLimitIntervalSec=0` — survives sustained HA outages
  without hammering the API on retries.
- **Failure recovery**: the script exits non-zero after
  `--max-consecutive-failures` (default 3) poll failures so the
  service supervisor applies its 10-minute backoff rather than the
  script busy-waiting internally.
