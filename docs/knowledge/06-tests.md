---
project: FoxESS Control
level: 6
last_verified: 2026-04-14
traces_up: [02-constraints.md, 04-design/]
---
# Test Inventory

~378 tests across 13 files, grouped by behavioural domain.

## Smart Discharge Pacing

**Constraints**: C-001, C-002
**Source**: `tests/test_smart_battery_algorithms.py`, `tests/test_init.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestCalculateDischargePower::test_basic_calculation` | Energy pacing over time window | C-001 |
| `TestCalculateDischargePower::test_consumption_exceeds_needed_floors_at_consumption` | Safety floor at consumption x 1.5 | C-001 |
| `TestCalculateDischargePower::test_consumption_decreases_power` | House load assists discharge | C-001 |
| `TestCalculateDischargePower::test_clamped_to_max` | Never exceeds max_power_w | C-001 |
| `TestCalculateDischargePower::test_soc_at_min` | Returns minimum at min SoC | C-002 |
| `TestCalculateDischargePower::test_soc_below_min` | Returns minimum below min SoC | C-002 |
| `TestDischargePowerFeedinConstraint::test_tight_feedin_reduces_power` | Feed-in limit caps power | C-001 |
| `TestDischargePowerFeedinConstraint::test_feedin_zero_with_no_load_returns_min` | Zero budget = minimum | C-001 |
| `TestShouldSuspendDischarge::test_soc_at_min_suspends` | Suspend at min SoC | C-002 |
| `TestShouldSuspendDischarge::test_soc_below_min_suspends` | Suspend below min SoC | C-002 |
| `TestShouldSuspendDischarge::test_high_consumption_suspends` | End-guard suspension | C-001 |
| `TestCalculateDischargeDeferredStart::*` (11 tests) | Deferred start timing | C-001 |

## Smart Charge Pacing

**Constraints**: C-014
**Source**: `tests/test_smart_battery_algorithms.py`, `tests/test_init.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestCalculateChargePower::test_basic_calculation` | Energy pacing | -- |
| `TestCalculateChargePower::test_trajectory_behind_triggers_max_power` | Catch-up burst | C-014 |
| `TestCalculateChargePower::test_trajectory_ahead_resumes_normal` | Normal pacing after catch-up | C-014 |
| `TestCalculateChargePower::test_consumption_increases_power` | Load adds to charge power | -- |
| `TestCalculateDeferredStart::test_clamps_to_start` | Deferred never before window | -- |
| `TestCalculateDeferredStart::test_consumption_brings_start_earlier` | Load awareness | -- |

## WebSocket Data Mapping

**Constraints**: C-004, C-005, C-006
**Source**: `tests/test_realtime_ws.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestMapWsToCoordinator::test_real_world_sample` | Watts/1000 = kW | C-004 |
| `TestMapWsToCoordinator::test_basic_mapping_export` | Export direction from balance | C-006 |
| `TestMapWsToCoordinator::test_grid_importing_from_balance` | Import direction from balance | C-006 |
| `TestMapWsToCoordinator::test_grid_exporting_from_balance` | Export direction from balance | C-006 |
| `TestMapWsToCoordinator::test_grid_fallback_to_gridstatus` | Fallback when data missing | C-006 |
| `TestStaleness::test_stale_messages_skipped` | timeDiff > 30 filtered | C-005 |

## Coordinator & Data Injection

**Constraints**: C-007
**Source**: `tests/test_coordinator.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestInjectRealtimeData::test_basic_merge` | WS merges into coordinator | -- |
| `TestInjectRealtimeData::test_feedin_trapezoidal_integration` | Trapezoidal feed-in integration | C-007 |
| `TestInjectRealtimeData::test_rest_poll_resets_integration_state` | REST resets WS state | C-007 |
| `TestInjectRealtimeData::test_feedin_not_integrated_on_first_ws_update` | No integration without baseline | C-007 |
| `TestWorkMode::test_work_mode_failure_is_non_fatal` | Graceful degradation | -- |

## Taper Model

**Constraints**: C-014
**Source**: `tests/test_taper.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestRecordCharge::test_ratio_clamped_above` | Ratio capped at MAX_RATIO | C-014 |
| `TestRecordCharge::test_ratio_clamped_below` | Ratio floored at MIN_RATIO | C-014 |
| `TestRecordCharge::test_ignores_implausibly_low_actual` | Quality gate: actual >= 50W | C-014 |
| `TestRecordCharge::test_ignores_low_requested_power` | Quality gate: requested >= 500W | C-014 |
| `TestIsPlausible::test_corrupted_profile_not_plausible` | Plausibility check | C-014 |
| `TestSerialization::test_from_dict_clamps_ratios` | Safe deserialization | C-014 |

## Schedule Merging (FoxESS API)

**Constraints**: C-008, C-009, C-010, C-011
**Source**: `tests/test_init.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestSanitizeGroup::test_strips_unknown_keys` | Extra field stripping | C-011 |
| `TestSanitizeGroup::test_clamps_fd_soc_to_api_minimum` | fdSoc >= 11 | C-008 |
| `TestIsPlaceholder::test_invalid_mode_is_placeholder` | Placeholder detection | C-010 |
| `TestIsPlaceholder::test_zero_duration_selfuse_is_placeholder` | Zero-duration filter | C-010 |
| `TestResolveStartEnd::test_crosses_midnight_rejected` | No midnight crossing | C-009 |
| `TestMergeWithExisting::test_rejects_schedule_with_backup_mode` | Unmanaged mode guard | -- |

## Session Management

**Constraints**: C-003, C-012, C-013, C-016
**Source**: `tests/test_init.py`, `tests/test_services.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestResolveStartEnd::test_exceeds_max_hours` | 4-hour cap | C-013 |
| `TestResolveStartEnd::test_zero_duration_rejected` | Positive duration required | C-013 |

## Sensor Display

**Constraints**: --
**Source**: `tests/test_sensor.py` (93 tests)

Key tests:
- Override status formatting (charge/discharge/deferred/idle)
- Progress bar trajectory (charge rises, discharge falls)
- Deferred start display (time until start, clamped to window)
- Graceful degradation when data missing

## Entity Mode (Modbus Interop)

**Constraints**: --
**Source**: `tests/test_entity_mode.py` (17 tests)

Key tests:
- Work mode mapping (SelfUse/ForceCharge/ForceDischarge)
- Entity state reading with unavailable fallback
- Power and SoC entity writes

## Config Flow

**Constraints**: --
**Source**: `tests/test_config_flow.py` (15 tests)

Key tests:
- API key validation, error handling
- Web credential hashing on save
- foxess_modbus entity auto-detection
- Reconfigure flow for web credentials

## API Client

**Constraints**: --
**Source**: `tests/test_client.py` (13 tests)

Key tests:
- Request signing (MD5 with CRLF separators)
- Rate limit retry (errno 40400)
- Transient error retry (502/503)

## Binary Sensors

**Constraints**: --
**Source**: `tests/test_binary_sensor.py` (12 tests)

Key tests:
- Smart charge/discharge active status
- Graceful off when data missing

## Vendored Code Sync

**Constraints**: C-015
**Source**: `tests/test_smart_battery_sync.py`

| Test | Verifies | Constraint |
|---|---|---|
| `test_vendored_copy_matches_canonical` | Byte-identical copies | C-015 |

## Unmapped Tests

Tests not yet traced to a specific constraint.

| Test | Appears to verify |
|---|---|
| `test_sensor.py::TestBatteryForecastSensor::*` | Forecast trajectory calculations — may encode unstated constraints about forecast shape |
| `test_sensor.py::TestDebugLog::*` | Debug sensor lifecycle — operational utility, no constraint |
| `test_inverter.py::test_max_power_cached` | Capacity-to-power conversion caching — performance optimisation |
