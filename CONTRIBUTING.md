# Contributing

## Local Development Setup

```bash
# Clone the repository
git clone https://github.com/nicois/foxess-control.git
cd foxess-control

# Install in editable mode with dev dependencies
pip install -e '.[dev]'

# Install pre-commit hooks (ruff, mypy, vendor sync)
pre-commit install
```

## Running Tests

```bash
# Unit tests (fast, no containers)
pytest tests/ -m "not slow" --tb=short

# E2E tests (requires podman + playwright)
pytest tests/e2e/ -m slow -n auto --tb=short

# Lint + type checking
pre-commit run --all-files
```

E2E tests are always run in parallel (`-n auto`). Never run them serially.

## Architecture

Two-layer design:

```
smart_battery/          Brand-agnostic core (algorithms, session management, sensors)
  adapter.py            InverterAdapter protocol — the abstraction boundary
  listeners.py          Charge/discharge session loops (tick-based)
  algorithms.py         Pure functions for power calculation, deferred start, etc.
  sensors.py            Base sensor classes for session state
  services.py           Service registration (smart_charge, smart_discharge, etc.)
  session.py            Session dataclasses and persistence

foxess/                 FoxESS-specific implementation
  client.py             REST API client with retry/backoff
  inverter.py           Schedule management, work mode control
  realtime_ws.py        WebSocket real-time data stream
  web_session.py        Web portal authentication + BMS temperature
  signature.py          WASM-based API signature generation
```

Brand integrations implement `InverterAdapter` (3 methods: `apply_mode`, `remove_override`, `get_max_power_w`). Most brands use `EntityAdapter`, which maps `WorkMode` enums to HA entity writes. Cloud-mode brands override for direct API control.

## Simulator

The simulator (`simulator/`) is a standalone aiohttp server that emulates the FoxESS Cloud API for testing. Each test gets an independent simulator instance.

### Endpoints

| Path | Purpose |
|------|---------|
| `/op/v0/device/list` | Device discovery |
| `/op/v0/device/real/query` | Real-time power data |
| `/op/v0/device/battery/soc/get` | Battery SoC |
| `/dew/v0/wsmaitian` | WebSocket real-time stream |
| `/c/v0/user/login` | Web portal auth |
| `/sim/set` | Backchannel: set inverter state |
| `/sim/fault` | Backchannel: inject faults |
| `/sim/clear_fault` | Backchannel: clear injected faults |
| `/sim/fast_forward` | Backchannel: advance simulated time |
| `/sim/tick` | Backchannel: single time step |
| `/sim/reset` | Backchannel: reset to defaults |

### Fault Injection

The simulator supports 7 fault types, injected via `POST /sim/fault`:

| Type | Effect |
|------|--------|
| `api_down` | HTTP 503 on all REST endpoints |
| `rate_limit` | Errno 40400 (FoxESS rate limit response) |
| `api_400` | HTTP 400 Bad Request |
| `api_500` | HTTP 500 Internal Server Error |
| `wrong_password` | Errno 41038 on login |
| `ws_refuse` | HTTP 403 on WebSocket handshake |
| `ws_disconnect` | Closes all active WebSocket connections |

Faults accept a `count` parameter: the fault auto-clears after that many requests. Use `count=0` for permanent faults.

### State Model

The simulator models: battery SoC with charge/discharge taper curves, solar/load/grid power flows, schedule groups with time-based mode switching, and cumulative energy counters. Fuzzing (±2% jitter) is enabled by default.

## E2E Test Infrastructure

E2E tests run a real Home Assistant instance inside a Podman container with the integration installed, plus the simulator as an in-process aiohttp server. Playwright drives a Chromium browser for UI verification.

### Fixture Scoping

- **Session-scoped** (one per xdist worker): container image build, browser context, port allocation
- **Function-scoped** (fresh per test): HA container, simulator state, browser page, event stream

### Parametrization

Tests are parametrized across:
- `connection_mode`: `"cloud"` (simulator REST/WS) or `"entity"` (input_number/input_select helpers)
- `data_source`: `"api"`, `"ws"`, or `"entity"` (cloud mode splits further by data source)

### Adding a New E2E Test

1. Add your test to `tests/e2e/test_e2e.py` or `tests/e2e/test_ui.py`
2. Mark with `@pytest.mark.slow`
3. Use `foxess_sim` fixture for simulator access, `ha_e2e` for the HA client, `page` for Playwright
4. Use `set_inverter_state()` to configure simulator state (works in both cloud and entity modes)
5. Use `wait_for_state()`, `wait_for_attribute()`, or Playwright's `expect()` for assertions — never `time.sleep()`

## Editing smart_battery/

Only edit the canonical `smart_battery/` directory at the project root. The vendored copy under `custom_components/foxess_control/smart_battery/` is automatically synced by a pre-commit hook. Never edit the vendored copy directly.

## Adding a New Brand

To support a new inverter brand:

1. Create a new integration directory (e.g. `custom_components/huawei_control/`)
2. Implement `InverterAdapter` — either subclass `EntityAdapter` for entity-mode brands or write a custom adapter for cloud-mode brands
3. Wire up the config flow, coordinator, and sensor platform following the FoxESS integration as a template
4. The `smart_battery/` library provides all the algorithms, session management, and sensors — your brand integration just needs to provide the adapter and data polling

## Commit Conventions

- Each PR should address one concern
- CI must pass before merging (lint, unit tests, E2E tests)
- No flaky tests — investigate root cause rather than skipping or tuning parameters
- Run `pre-commit run --all-files` before pushing
