# Architecture Remediation Plan — 2026-04-21

5 phases, ordered by dependency. Each phase is independently shippable.

---

## Phase 1: Complete the domain_data Migration + Remove Dead Code

**Effort**: Medium | **Risk**: Low | **Dependency**: None

This is the foundation — later phases produce cleaner code when typed attributes
are already in use. The `cancel_smart_session` function in `smart_battery/session.py`
currently takes `domain_data: dict[str, Any]` and accesses it via dict keys. Completing
the migration changes these to typed attribute access, making the SessionManager
extraction in Phase 3 cleaner.

### 1a: Remove dead keys and dead field

**Files**:
- `domain_data.py` — remove `_smart_charge_session_id` and
  `_smart_discharge_session_id` from `_KEY_MAP` (lines 83–84). Remove
  `adapter: Any = None` from `FoxESSEntryData` (line 27).

**Safety**: Grep confirms zero references outside `_KEY_MAP` itself.
**Tests**: Existing `test_domain_data.py` passes. No new tests needed.

### 1b: Migrate 64 dict-style accesses to typed attribute access

| File | Accesses | Change |
|------|----------|--------|
| `__init__.py` | ~42 | `hass.data[DOMAIN]["_smart_charge_state"]` → `_dd(hass).smart_charge_state` |
| `coordinator.py` | ~9 | `domain_data.get("_web_session")` → `domain_data.web_session` |
| `diagnostics.py` | ~7 | Same pattern |
| `smart_battery/session.py` | ~4 | Change param type to `SmartBatteryDomainData`, use attribute access |
| `smart_battery/listeners.py` | ~2 | Same |
| Tests | ~6 | Update setup code |

**Design note**: `cancel_smart_session` receives `state_key` and `unsubs_key` as
strings used as dict keys. After migration, change to `getattr`/`setattr` with
mapped attribute names, or change callers to pass attribute names directly. Cleanest:
pass attribute names without underscore prefix (`"smart_charge_state"` not
`"_smart_charge_state"`).

**Constraint**: C-015 — `smart_battery/` edits ONLY in canonical root.
**Tests**: Pure refactor. Existing 30+ `TestRecoverSessions` tests are the safety net.
Rewrite `test_domain_data.py` to verify attribute access instead of dict access.

### 1c: Remove the dict protocol from FoxESSControlData

**Files**:
- `domain_data.py` — remove `_KEY_MAP`, `__post_init__`, `_resolve_key`,
  `__getitem__`, `__setitem__`, `__contains__`, `get`, `pop`, `setdefault`,
  `__iter__`, `items` (lines 74–179).

**Tests**: `test_domain_data.py` rewritten in 1b. Simpler tests verifying typed access.

---

## Phase 2: Consolidate Config Accessors

**Effort**: Small | **Risk**: Low | **Dependency**: Phase 1

### What to change

7 functions all follow the same pattern:
```python
def _get_X(hass: HomeAssistant) -> T:
    entry = _get_first_entry(hass)
    return entry.options.get(CONF_X, DEFAULT_X)
```

Replace with a frozen dataclass built once from `entry.options` at setup time
(rebuilt on options update via `async_setup_entry`). Call sites change from
`_get_min_soc_on_grid(hass)` to `config.min_soc_on_grid`.

**Special cases**: `_is_entity_mode` and `_get_max_power_w` have fallback logic
(entity mode vs cloud mode). Keep as special cases or factory methods on the
config class if they diverge from the simple pattern.

**Files**: `__init__.py` — replace 7 functions + update ~20 call sites.
**Tests**: Update existing accessor tests (e.g. `TestGetMinSocOnGrid`). No new
behavioral tests.

---

## Phase 3: Unify Session Recovery

**Effort**: Medium-Large | **Risk**: Medium (safety-critical: C-024, C-025) | **Dependency**: Phase 1

`_recover_charge_session` (lines 698–843) and `_recover_discharge_session`
(lines 846–991) are 145 lines each, structurally near-identical.

### Differences to parameterise

1. Direction-specific keys: `"smart_charge"` vs `"smart_discharge"`, `WorkMode.FORCE_CHARGE` vs `WorkMode.FORCE_DISCHARGE`
2. Power recalculation: charge uses `_calculate_charge_power` (target_soc, headroom); discharge uses `_calculate_discharge_power` (min_soc, feedin, consumption)
3. State dict fields: charge has `target_soc`, `force`, `soc_above_target_count`; discharge has `min_soc`, `pacing_enabled`, `feedin_energy_limit_kwh`, `consumption_peak_kw`
4. Resume condition: charge checks `has_group or not charging_started`; discharge checks only `has_group`
5. Listener setup: `_setup_smart_charge_listeners` vs `_setup_smart_discharge_listeners`

### Approach

Extract `_recover_session` parameterised by a `RecoveryConfig` dataclass:
```python
@dataclass
class RecoveryConfig:
    storage_key: str            # "smart_charge" / "smart_discharge"
    work_mode: WorkMode
    state_key: str
    setup_listeners: Callable
    recalculate_power: Callable
```

**NOT purely mechanical** — power recalculation paths have different parameter sets.
The common structure (date check, window expiry, schedule match, state rebuild,
listener setup) becomes the single function body.

**Files**: `__init__.py` — replace two functions with one + two config instances.
**Tests**: No new tests. The 30+ `TestRecoverSessions` tests must pass unchanged.
**Constraints**: C-024 (safe state on failure), C-025 (session boundary cleanliness).

---

## Phase 4: Simulator Fidelity

**Effort**: Small-Medium | **Risk**: Low-Medium | **Dependency**: None (parallel with Phases 1–3)

### 4a: fdSoc enforcement in tick (HIGH priority)

**File**: `simulator/model.py`

In `tick()`, after computing power flows for ForceCharge/ForceDischarge, add:
- **ForceCharge**: if `self.soc >= group.fdSoc`, set `bat_charge_kw = 0.0`
- **ForceDischarge**: if `self.soc <= group.fdSoc`, set `bat_discharge_kw = 0.0`

Currently has min_soc clamping (line 252) and 100% clamping (line 258), but no
fdSoc. The `group.fdSoc` is available via `self.get_active_group()` (line 186).

**Tests needed**: New tests in `tests/test_inverter.py` or `tests/test_simulator.py`:
- ForceCharge stops at fdSoc
- ForceDischarge stops at fdSoc
- fdSoc interacts correctly with taper

**Constraint**: C-028 (simulator over mocks), C-031 (no flaky tests — disable
SoC fuzzing in these tests).

### 4b: Configurable battery efficiency factor (MEDIUM priority)

**File**: `simulator/model.py`

Add `efficiency: float = 1.0` to `InverterModel`. At line 264:
```python
if net_bat_kw > 0:  # charging
    delta_kwh = net_bat_kw * dt_hours * self.efficiency
else:  # discharging
    delta_kwh = net_bat_kw * dt_hours / self.efficiency
```

Default 1.0 for backward compatibility. Tests wanting realism set 0.95.

**Tests needed**: New tests verifying efficiency reduces stored energy on charge,
increases consumed energy on discharge.

---

## Phase 5: Coordinator Extraction + __init__.py Module Split (Long-term)

**Effort**: Large | **Risk**: Medium-High | **Dependency**: Phases 1–3

### 5a: Extract BMSMonitor from coordinator

Extract BMS temperature fetching, compound ID discovery, and rediscovery into a
`BMSMonitor` class. Coordinator delegates to `self._bms.fetch_if_due()`.

**Tests**: Existing `test_coordinator.py` BMS tests are the safety net.

### 5b: Split __init__.py into focused modules

- `setup.py` — `async_setup_entry`, `async_unload_entry`, platform forwarding
- `services.py` — service handlers
- `session_manager.py` — unified recovery (from Phase 3), session state, WS lifecycle
- `websocket_api.py` — Lovelace card WS API

`__init__.py` becomes a thin shim re-exporting entry points.

**Risk**: HA custom integration loader expects `async_setup_entry` in `__init__.py`.
Verify re-exports work. All existing tests must pass; test imports will need updating.

---

## Summary

| Phase | Description | Effort | Risk | New Tests? |
|-------|-------------|--------|------|------------|
| 1 | Complete domain_data migration | Medium | Low | Rewrite test_domain_data.py |
| 2 | Consolidate config accessors | Small | Low | Update existing |
| 3 | Unify session recovery | Medium-Large | Medium | None (refactor) |
| 4 | Simulator fdSoc + efficiency | Small-Medium | Low-Medium | New simulator tests |
| 5 | Coordinator + __init__ split | Large | Medium-High | None (refactor) |

## Deferred (orthogonal, do anytime)

- Test parametrisation (best combined with Phase 4 tests)
- Config flow E2E test (small standalone task)
- errno 44098 simulation (low priority)
- Static simulator temperatures (document as known deviation)

## Constraints Impacted

- **C-015**: Phase 1b `smart_battery/` edits only in canonical root
- **C-024, C-025**: Phase 3 must preserve all safety behavior
- **C-028**: Phase 4 uses simulator, consistent with constraint
- **C-031**: Phase 4 tests must be deterministic (disable SoC fuzzing)
