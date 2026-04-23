# Architecture Review — 2026-04-21

Comprehensive critical evaluation of the project's information architecture,
identifying incremental accretion, suboptimal patterns, and simulator/E2E
fidelity gaps. Three parallel analyses were conducted: `__init__.py`
architecture, test patterns & simulator fidelity, and domain data & coordinator.

---

## 1. God Module: `__init__.py` (2581 lines)

The integration entry point has grown into a monolith with multiple
responsibilities that should be separated.

**Symptoms:**
- **39 module-level functions** taking `hass: HomeAssistant` — procedural style
  rather than encapsulated in classes
- **7 config accessor functions** (`_get_min_soc_on_grid`,
  `_get_smart_headroom`, `_is_entity_mode`, `_get_max_power_w`,
  `_get_api_min_soc`, `_get_battery_capacity_kwh`, `_get_min_power_change`)
  — each doing the same pattern of `entry.options.get(CONF_X, DEFAULT_X)`
- **Duplicated charge/discharge paths**: `_recover_charge_session` (lines
  698–843) and `_recover_discharge_session` (lines 846–991) are structurally
  near-identical, differing only in direction-specific parameters

**Recommendation (HIGH impact):**
- Extract `FoxESSSessionManager` class to encapsulate session recovery,
  override management, and schedule manipulation — unifies the duplicated
  charge/discharge paths behind a direction parameter
- Consolidate the 7 config accessors into a single `ConfigAccessor` or a
  `@property`-based wrapper on the config entry
- Long-term: break `__init__.py` into `setup.py`, `services.py`,
  `session_manager.py`

---

## 2. Half-Finished Domain Data Bridge

**File: `custom_components/foxess_control/domain_data.py` (179 lines)**

`FoxESSControlData` has 18 typed fields and a 23-entry `_KEY_MAP` that maps
underscore string keys to dataclass attributes. It implements the full dict
protocol (`__getitem__`, `__setitem__`, `__contains__`, `get`, `pop`,
`setdefault`, `__iter__`, `items`).

**Symptoms:**
- **64 dict-style accesses** across the codebase via the bridge layer (20
  `domain_data[`, 34 `domain_data.get(`, 10 `domain_data.pop(`) — code that
  should use `domain_data.some_field` instead uses `domain_data["some_field"]`
- **Dead keys**: `_smart_charge_session_id` and `_smart_discharge_session_id`
  map to empty strings, never used
- **Dead field**: `FoxESSEntryData.adapter` is defined but never referenced
- The dict protocol exists solely to avoid updating all callers at once — a
  migration that was started but never finished

**Recommendation (HIGH impact):**
- Complete the migration: replace all `domain_data["key"]` accesses with
  `domain_data.key` attribute access
- Remove the dict protocol methods (`__getitem__`, `__setitem__`, etc.)
- Remove dead keys and the unused `adapter` field
- This is mechanical but high-value: eliminates a layer of indirection that
  makes the code harder to follow and defeats IDE navigation

---

## 3. Coordinator Responsibility Sprawl

**File: `custom_components/foxess_control/coordinator.py` (605 lines)**

The coordinator has accumulated 7+ distinct responsibilities:

1. REST polling (cloud API)
2. WebSocket data injection
3. BMS temperature fetching (web portal)
4. SoC interpolation
5. SoC extrapolation
6. Feed-in integration
7. Override cleanup retry
8. BMS compound ID rediscovery (newly added)

**Recommendation (MEDIUM impact):**
- The coordinator legitimately orchestrates these, but the BMS concerns
  (temperature fetch, compound ID discovery/rediscovery) could be extracted
  into a `BMSMonitor` class that the coordinator delegates to
- SoC interpolation/extrapolation could be a `SoCTracker` helper
- These extractions would reduce the coordinator to its core job: scheduling
  updates and dispatching to specialists

---

## 4. Simulator Fidelity Gaps

**File: `simulator/model.py` (426 lines)**

### Missing fdSoc enforcement in tick paths
The simulator validates fdSoc >= 11 on the API boundary (`set_scheduler`) but
does **not enforce fdSoc during ForceCharge/ForceDischarge tick execution**.
Real inverters stop charging/discharging when fdSoc is reached. This means
tests can't verify that pacing algorithms correctly anticipate the inverter
stopping at fdSoc.

### No battery efficiency losses
Line 264: `delta_pct = delta_kwh / battery_capacity_kwh * 100.0` — assumes
100% round-trip efficiency. Real batteries lose ~5–10%. Tests will
overestimate charge capability and may not catch efficiency-dependent bugs.

### No grid export limit simulation
The simulator doesn't model grid export limits. Real systems may be
constrained by CT clamp settings or network operator limits.

### Static environmental values
- `batTemperature`: always 25.0 (line 318)
- `ambientTemperation`: always 20.0 (line 323)
- `invTemperation`: always 35.0 (line 324)
- `RVolt`: always 240.0 (line 333)
- `RFreq`: always 50.0 (line 335)
- PV split: always 50/50 between strings

Tests can't exercise thermal derating or voltage/frequency edge cases.

### Missing error codes
- `errno=44098` (write failed) — discovered during live API testing but not
  simulated
- Backup mode physics not simulated (enum exists in `inverter.py:WorkMode.BACKUP`
  but no model logic)
- `device/list` endpoint exists in server.py (line 381) but `set_min_soc` does
  not

### What IS well-modeled
- Power balance for 4 work modes (SelfUse, ForceCharge, ForceDischarge, Feedin)
- Taper factors for charge and discharge
- Min/max SoC clamping
- Cumulative energy counters
- Grid import/export with solar/load prioritisation
- SoC returned as integer (matches real API)
- SoC fuzzing at +/-2%
- WS message format with correct field names, timeDiff, multipleBatterySoc
- Error codes: 40257, 40400, 42023, 41038
- Schedule overlap detection
- Placeholder group format

**Recommendation:**
- **HIGH**: Add fdSoc enforcement in ForceCharge/ForceDischarge tick —
  stop charging when SoC >= fdSoc, stop discharging when SoC <= fdSoc
- **MEDIUM**: Add configurable efficiency factor (default 0.95) to SoC calc
- **LOW**: Add errno 44098 simulation, document Backup mode absence

---

## 5. E2E Test Infrastructure

**File: `tests/e2e/conftest.py`**

### Strengths
- Container isolation per test (per-worker container names)
- Fresh HA config directory per test
- Custom component mounted read-only
- Both cloud and entity modes tested
- Proper async handling via `HAEventStream`
- `wait_for_state()`, `wait_for_numeric_state()`, `wait_for_attribute()` helpers

### Shortcuts
- **Config flow skipped**: Uses pre-seeded `.storage/core.config_entries` —
  tests don't verify that the user-driven config flow works end-to-end
- **No network latency simulation**: Tests run unrealistically fast; timeout
  bugs may not surface
- **Permissive auth**: trusted_networks with allow_bypass_login — not realistic
  but pragmatic for testing

### Polling interval alignment
E2E tests use standard HA coordinator, respecting `DEFAULT_POLLING_INTERVAL =
300s`. This is correct production fidelity.

---

## 6. Test Patterns

### What's good
- Simulator used over mocks for client/inverter tests (C-028 compliance)
- `FoxESSControlData` used consistently in domain tests
- Mock usage is appropriate — primarily time control (`dt_util.now`), not
  business logic mocking
- Comprehensive schedule validation coverage
- All 4 work modes tested with realistic power flows (118 charge/discharge tests)

### Gaps
- **Very sparse parametrisation**: Only 4 parametrised tests across 14K+ lines.
  Missing coverage for: different SoC ranges, battery capacities, max power
  settings, charge-vs-discharge as a parameter
- **No cross-scenario matrix**: Charge at 10% vs 90% SoC, 5kWh vs 15kWh
  battery, etc.
- **Config flow not exercised** in E2E (uses pre-seeded config)
- **No non-default capacity E2E tests** — always uses default battery size

**Recommendation (MEDIUM impact):**
- Parametrise charge/discharge tests by direction (reduces 118 tests to ~60
  with better coverage)
- Add SoC-range and battery-capacity parameters to key scenario tests
- Add at least one E2E test that exercises the config flow

---

## 7. Incremental Accretion Summary

These patterns each made sense when added but have accumulated into
unnecessary complexity:

| Pattern | Origin | Simplification |
|---------|--------|----------------|
| Dict bridge in FoxESSControlData | Gradual migration from plain dict | Complete migration, remove bridge |
| 7 config accessor functions | Each added as needed | Single accessor class or property wrapper |
| Duplicated charge/discharge recovery | Copy-paste with minor changes | Unify behind direction parameter |
| 39 module-level functions in __init__ | HA integration pattern + growth | Extract into focused modules |
| BMS concerns in coordinator | Added incrementally (temp, compound ID, rediscovery) | Extract BMSMonitor |
| Static simulator temps/voltages | "Good enough" defaults | Make configurable, add edge-case tests |

---

## Priority Ranking

### High impact, moderate effort
1. Complete domain_data migration (mechanical, high clarity gain)
2. Extract FoxESSSessionManager from __init__.py (unifies charge/discharge)
3. Add fdSoc enforcement to simulator tick (correctness)

### Medium impact, low effort
4. Consolidate config accessors
5. Parametrise charge/discharge tests
6. Add battery efficiency to simulator

### Low impact, low effort
7. Remove dead fields/keys from domain_data
8. Document simulator gaps (temps, Backup mode)
9. Add errno 44098 to simulator
