---
project: FoxESS Control
level: 3
last_verified: 2026-04-19
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
**Path**: `custom_components/foxess_control/__init__.py` (112K)
**Responsibility**: HA `async_setup_entry` / `async_unload_entry`,
service handler registration, smart charge/discharge session
orchestration (FoxESS-specific schedule merging, override application,
WebSocket lifecycle, power adjustment callbacks).
**Why large**: Partly intentional (HA integration pattern requires setup
and service registration in `__init__.py`), partly tech debt. The
FoxESS-specific session orchestration (schedule merging, override
tracking, WS lifecycle) should be extracted into a FoxESS session
manager, leaving `__init__.py` as a thin setup/teardown layer.
Entity-mode dispatch (`_apply_mode_via_entities`) delegates to
`FoxESSEntityAdapter` — the inline mode-map/dispatch was consolidated
into the adapter class in beta.32.

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
                    v
              FoxESS Cloud API / HA entity writes
```

## Key Abstractions

### InverterAdapter (Protocol)
**What**: Interface for controlling an inverter's work mode and power.
**Why**: Decouples smart session logic from brand-specific API calls.
The same `listeners.py` state machine works with any brand that
implements `apply_mode()`, `remove_override()`, `get_max_power_w()`.
**Implemented by**: `EntityAdapter` (generic, entity-based),
FoxESS `__init__.py` (cloud API, wraps `Inverter` class).

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
`hass.data[DOMAIN]` dict.
**Why**: HA 2024.x+ best practice. Typed `entry.runtime_data` gives
IDE autocomplete, catches key typos at lint time, and enables
`_dd()` helper for consistent typed access throughout `__init__.py`.
A bridge layer (`__getitem__`, `__contains__`, `get`) preserves
backward compatibility during incremental migration.
**Implemented by**: `domain_data.py`, `smart_battery/domain_data.py`.

### DataUpdateCoordinator
**What**: HA polling coordinator that fetches data on an interval and
notifies listeners.
**Why**: Standard HA pattern. WebSocket data is injected between polls
via `async_set_updated_data()`, giving sensors immediate updates.
**Implemented by**: `FoxESSDataCoordinator`, `EntityCoordinator`.

## External Dependencies

| Dependency | Role | Why chosen |
|---|---|---|
| `wasmtime` | WASM runtime for signature generation | FoxESS web portal requires a specific signature algorithm; WASM is the only available implementation (reverse-engineered from JS) |

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
| `aiohttp` | WebSocket client | Standard async HTTP/WS library; HA already uses it |
| `requests` | REST API client (sync) | Used in the FoxESS client for synchronous API calls within HA's executor |
| `voluptuous` | Schema validation | Used for service call and config flow schemas; ships with HA but used directly |
| `homeassistant` | HA framework | Target platform |
