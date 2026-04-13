# Modbus Support Viability Assessment

## Context

foxess_control currently communicates exclusively via the FoxESS Cloud API. This document assesses how to support users with local Modbus hardware, so that foxess_control's smart charge/discharge logic works regardless of whether the user has cloud-only, modbus-only, or both.

## Current Architecture

The `Inverter` class (`foxess/inverter.py`) is the central API surface. Every method directly calls `self.client.post()` or `self.client.get()` on the `FoxESSClient` (cloud HTTP client). The class handles:

1. **Reads** — `get_real_time(variables)`, `get_soc()`, `get_battery_status()`, `get_schedule()`, `get_min_soc()`, `get_detail()`, `get_current_mode()`
2. **Writes** — `set_schedule(groups)`, `set_work_mode(mode, ...)`, `set_min_soc(min_soc, min_soc_on_grid)`, convenience methods (`self_use`, `force_charge`, `force_discharge`)

The `DataUpdateCoordinator` polls `get_real_time(POLLED_VARIABLES)` + `get_current_mode()` every 5 minutes. Smart charge/discharge logic in `__init__.py` calls `set_schedule()` periodically.

**Existing cross-integration pattern**: The `soc_entity` config option already lets foxess_control read SoC from any HA entity (e.g., a foxess_modbus sensor) instead of the cloud API.

## The `foxess_modbus` Integration

`nathanmarlor/foxess_modbus` (268 stars) is a mature HA integration providing local Modbus for FoxESS inverters (H1, H3, KH series). It requires a Modbus TCP adapter or RS-485 connection.

### What It Exposes

**Readable entities (sensors):**
- SoC, battery charge/discharge power, voltage, current, temperature
- PV string power (PV1-PV6), grid voltage/current/power/frequency
- Feed-in power, grid consumption, EPS power
- Cumulative energy totals (generation, charge, discharge, feed-in, grid consumption)
- Inverter/ambient temperature, fault codes, connection status

**Writable entities:**
- `select.*work_mode` — Self Use, Feed-in First, Back-up, Peak Shaving (model-dependent)
- `number.*` entities — charge/discharge power limits, min/max SoC thresholds
- Force charge/discharge via select entities (H1 LAN) or work mode register

**Services:**
- `foxess_modbus.update_charge_period` — set charge window (start/end time, enable force charge, enable grid charging)
- `foxess_modbus.update_all_charge_periods` — batch set all periods
- `foxess_modbus.write_registers` — direct register writes (fallback)

### Model Support Matrix

| Feature | H1 LAN | H1 RS485 | H3/AC3 | KH/KA | H3-Smart |
|---------|--------|----------|--------|-------|----------|
| Real-time sensors | Yes | Yes | Yes | Yes | Yes |
| Work mode control | No* | Yes | Yes | Yes | Yes |
| Force charge/discharge | Yes | Yes | Yes | Yes | Yes |
| Charge period config | No | Yes | No | No | No |
| Min/Max SoC control | No | Yes | Yes | Yes | Yes |

*H1 LAN uses remote control mode (Force Charge/Discharge only)

### Known Issues
- Register compatibility varies across firmware versions (47 open issues)
- Some users report incorrect values (10x scaling, load/EPS confusion)
- Intermittent disconnections, brief zero readings

## Recommended Approach: Entity-Based Backend

Instead of implementing Modbus directly, foxess_control can optionally consume foxess_modbus's HA entities and services as an alternative backend. This extends the existing `soc_entity` pattern to cover all operations.

### How It Works

foxess_control already reads SoC from an arbitrary HA entity via `soc_entity`. The same pattern generalises to all read/write operations:

```
Cloud mode (current):
  Reads:  Inverter.get_real_time() → Cloud API → coordinator → sensors
  Writes: Inverter.set_schedule()  → Cloud API → inverter

Entity mode (new):
  Reads:  hass.states.get("sensor.foxess_modbus_*") → coordinator → sensors
  Writes: hass.services.async_call("select", "select_option", ...) → foxess_modbus → inverter
          hass.services.async_call("number", "set_value", ...) → foxess_modbus → inverter
```

### Config Flow Changes

Add an optional "Control mode" selector to the options flow:

- **Cloud API** (default, current behaviour) — requires API key
- **Local entities** — user provides entity IDs for: work mode select, charge power number, discharge power number, min SoC number. API key becomes optional (needed only as fallback or for features unavailable via modbus).

### Mapping foxess_control Operations to foxess_modbus Entities

| foxess_control operation | Cloud API call | Entity-mode equivalent |
|---|---|---|
| Read SoC | `get_real_time(["SoC"])` | `hass.states.get(soc_entity)` (already works) |
| Read real-time data | `get_real_time(POLLED_VARIABLES)` | `hass.states.get()` for each mapped entity |
| Set work mode | `set_schedule(groups)` | `select.select_option` on work mode entity |
| Set charge power | `fdPwr` in schedule group | `number.set_value` on charge power entity |
| Set discharge power | `fdPwr` in schedule group | `number.set_value` on discharge power entity |
| Set min SoC | `set_min_soc()` | `number.set_value` on min SoC entity |
| Force charge | `force_charge()` via schedule | `select.select_option` → ForceCharge |
| Force discharge | `force_discharge()` via schedule | `select.select_option` → ForceDischarge |
| Return to self-use | `self_use()` via schedule | `select.select_option` → Self Use |
| Read current mode | `get_current_mode()` via scheduler | `hass.states.get(work_mode_entity)` |

### What Changes in Smart Charge/Discharge

The smart charge algorithm in `__init__.py` currently:
1. Calls `inverter.set_schedule(groups)` to activate ForceCharge with a time window
2. Periodically adjusts power via `set_schedule()` with updated `fdPwr`
3. Cancels by removing the ForceCharge group and falling back to SelfUse

In entity mode, this becomes:
1. Call `select.select_option` to set ForceCharge mode
2. Call `number.set_value` to adjust charge power
3. Call `select.select_option` to return to Self Use

The key difference: **no multi-window scheduling**. The cloud API atomically sets "ForceCharge 01:00-05:00 then SelfUse 05:00-23:59". With entities, foxess_control must manage the time window itself using `async_track_point_in_time()` to switch back to SelfUse when the window ends.

This is a minor change — foxess_control's smart sessions already track time windows and cancel at the end. The only new requirement is ensuring the mode reverts even if HA restarts mid-session (which session persistence already handles).

### Advantages Over Direct Modbus Implementation

1. **Zero Modbus code** — no pymodbus dependency, no register maps, no firmware compatibility testing
2. **foxess_modbus handles the hard parts** — register compatibility, connection management, model-specific quirks
3. **Works with any local integration** — not limited to foxess_modbus; any integration that exposes work mode and power entities would work
4. **Incremental adoption** — users can start with cloud, add modbus hardware later, switch foxess_control to entity mode without reconfiguring smart sessions
5. **Graceful fallback** — if entity mode entities become unavailable, fall back to cloud API (if API key configured)

### Implementation Scope

**Modified files:**
- `config_flow.py` — add entity-mode option with entity ID fields
- `__init__.py` — add entity-based write functions alongside cloud API calls; smart charge/discharge uses whichever is configured
- `coordinator.py` — entity-mode coordinator reads from HA states instead of cloud API
- `const.py` — new config constants for entity IDs

**Not modified:**
- `foxess/inverter.py` — cloud API wrapper stays as-is
- `foxess/client.py` — unchanged
- `sensor.py` — sensors consume coordinator data regardless of source

**New dependencies:** None

**Estimated effort:** Medium — mostly plumbing in `__init__.py` to route operations through HA entity calls instead of `Inverter` methods.

### Risks and Mitigations

| Risk | Mitigation |
|---|---|
| foxess_modbus entity IDs change between versions | User configures entity IDs explicitly; no auto-discovery magic |
| foxess_modbus entity unavailable (adapter disconnected) | Fall back to cloud API if API key configured; log warning |
| Work mode names differ between models | Map foxess_control's `WorkMode` enum to foxess_modbus's select options via config |
| No multi-window scheduling in entity mode | foxess_control manages time windows via HA timers (already does this for smart sessions) |
| Race condition: foxess_modbus and foxess_control both writing | foxess_control is the sole writer; foxess_modbus just exposes the entities |

## Summary

| Criterion | Cloud Only (now) | Entity Backend (proposed) | Direct Modbus |
|---|---|---|---|
| Dev effort | None | Medium | Large |
| Modbus maintenance burden | None | None (foxess_modbus owns it) | High |
| New dependencies | None | None | pymodbus |
| Read latency | ~5 min | Sub-second (local) | Sub-second |
| Write capability | Full (cloud scheduler) | Mode + power (no multi-window) | Mode + power |
| Offline operation | No | Full (if foxess_modbus running) | Full |
| Smart charge works | Yes | Yes (HA-managed time windows) | Yes (HA-managed time windows) |
| Cloud fallback | N/A | Yes (if API key provided) | Separate implementation |
| Works without cloud API | No | Yes | Yes |
