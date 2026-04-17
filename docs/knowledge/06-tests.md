---
project: FoxESS Control
level: 6
last_verified: 2026-04-16
traces_up: [02-constraints.md, 04-design/]
---
# Test Inventory

555 unit tests + 19 E2E tests = 574 total.

Unit tests run with pytest-xdist (`-n auto`, randomised via
pytest-randomly). E2E tests use Podman containers with a FoxESS
simulator and Playwright browser automation.

> **Note**: This inventory covers the major constraint-mapped tests.
> Many tests (particularly in `test_services.py`, `test_sensor.py`, and
> `test_init.py`) verify operational correctness without tracing to a
> specific constraint. These are listed under Unmapped Tests.

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
| `TestCalculateDischargeDeferredStart::*` (13 tests) | Deferred start timing | C-001 |

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
| `TestCalculateDeferredStart::test_consumption_affects_deferral` | Load awareness | -- |
| `TestCalculateDeferredStart::test_taper_consumption_affects_deferral` | Taper path consumption headroom | C-014 |
| `TestCalculateDeferredStart::test_taper_starts_earlier_than_linear` | Taper ratios extend charge time | C-014 |

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

## Session Management & Service Handlers

**Constraints**: C-003, C-012, C-013, C-016, C-024
**Source**: `tests/test_init.py`, `tests/test_services.py` (82 tests)

| Test | Verifies | Constraint |
|---|---|---|
| `TestResolveStartEnd::test_exceeds_max_hours` | 4-hour cap | C-013 |
| `TestResolveStartEnd::test_zero_duration_rejected` | Positive duration required | C-013 |
| `TestHandleSmartCharge::test_soc_unavailable_aborts_after_threshold` | SoC unavailable cancels charge | C-012 |
| `TestHandleSmartCharge::test_soc_available_resets_unavailable_count` | Available SoC resets counter | C-012 |
| `TestSessionPersistence::*` (3 tests) | Session survives restarts | C-003 |
| `TestRecoverSessions::*` (10 tests) | Session recovery on startup | C-003 |
| `TestSocStabilityCounters::*` (4 tests) | Below-min confirmation counter | C-002 |
| `TestCheckScheduleSafe::*` (7 tests) | Unmanaged mode rejection | C-018 |
| `TestTransientApiErrorResilience::test_charge_survives_transient_api_error` | Single API error retried | C-024 |
| `TestTransientApiErrorResilience::test_discharge_survives_transient_api_error` | Single API error retried | C-024 |
| `TestTransientApiErrorResilience::test_charge_aborts_after_repeated_errors` | Consecutive errors abort | C-024 |

## Sensor Display

**Constraints**: --
**Source**: `tests/test_sensor.py` (85 tests)

Key tests:
- Override status formatting (charge/discharge/deferred/idle)
- Progress bar trajectory (charge rises, discharge falls)
- Deferred start display (time until start, clamped to window)
- Graceful degradation when data missing

## Entity Mode (Modbus Interop)

**Constraints**: --
**Source**: `tests/test_entity_mode.py` (18 tests)

Key tests:
- Work mode mapping (SelfUse/ForceCharge/ForceDischarge)
- Entity state reading with unavailable fallback
- Power and SoC entity writes

## Config Flow

**Constraints**: --
**Source**: `tests/test_config_flow.py` (16 tests)

Key tests:
- API key validation, error handling
- Web credential hashing on save
- foxess_modbus entity auto-detection
- Reconfigure flow for web credentials

## API Client

**Constraints**: --
**Source**: `tests/test_client.py` (9 tests)

Key tests:
- Request signing (MD5 with CRLF separators)
- Rate limit retry (errno 40400)
- Transient error retry (502/503)

## Binary Sensors

**Constraints**: --
**Source**: `tests/test_binary_sensor.py` (14 tests)

Key tests:
- Smart charge/discharge active status
- Graceful off when data missing

## Vendored Code Sync

**Constraints**: C-015
**Source**: `tests/test_smart_battery_sync.py`

| Test | Verifies | Constraint |
|---|---|---|
| `test_vendored_copy_matches_canonical` | Byte-identical copies | C-015 |

## E2E Tests (Containerised HA + Simulator + Playwright)

**Source**: `e2e/test_e2e.py` (5 tests), `e2e/test_ui.py` (14 tests)
**Infrastructure**: Podman HA container, FoxESS simulator, Playwright Chromium

| Test | Verifies | Constraint |
|---|---|---|
| `TestSmartDischarge::test_discharge_starts` | Service → schedule → state transition | C-001 |
| `TestSmartDischarge::test_discharge_drains_battery` | SoC decreases during discharge | C-001, C-002 |
| `TestSmartCharge::test_charge_starts` | Charge service + state transition | -- |
| `TestFaultInjection::test_ws_unit_mismatch_handled` | WS kW/W unit detection | C-004 |
| `TestDataSource::test_api_source_when_idle` | data_source attribute = "api" | C-020 |
| `TestOverviewCard::test_card_renders` | Overview card in shadow DOM | -- |
| `TestOverviewCard::test_shows_soc` | SoC displayed on card | -- |
| `TestOverviewCard::test_house_load_never_greyed` | House node active at low load | C-020 |
| `TestOverviewCard::test_data_source_badge_matches_mode[api/ws]` | Badge reflects active data path | C-020 |
| `TestOverviewCard::test_pv_values_consistent_with_solar_total[api/ws]` | PV1+PV2 ≈ total solar | C-020 |
| `TestControlCard::test_card_renders` | Control card in shadow DOM | -- |
| `TestControlCard::test_soc_displayed` | SoC percentage in header | -- |
| `TestControlCard::test_progress_hidden_when_idle` | No progress section when idle | C-020 |
| `TestControlCard::test_progress_visible_during_discharge[api/ws]` | Progress section during discharge | C-020 |
| `TestScreenshots::test_idle_screenshot` | Visual regression capture | -- |
| `TestScreenshots::test_discharging_screenshot` | Visual regression capture | -- |

Tests parametrized with `[api/ws]` run under both data sources via the
`data_source` fixture, which uses the `ws_refuse` simulator fault to
block WS connections for API-only mode.

## Unmapped Tests

Tests not yet traced to a specific constraint. ~80+ tests across multiple
files verify operational correctness, display logic, and setup plumbing.

| Test | Appears to verify |
|---|---|
| `test_sensor.py::TestBatteryForecastSensor` (8 tests) | Forecast trajectory calculations |
| `test_sensor.py::TestDebugLog` (6 tests) | Debug sensor lifecycle |
| `test_sensor.py::TestFoxESSPolledSensor` (5 tests) | Sensor plumbing |
| `test_sensor.py::TestFoxESSWorkModeSensor` (5 tests) | Work mode sensor |
| `test_sensor.py` display tests (~25 tests) | Charge/discharge remaining, power, window formatting |
| `test_inverter.py` (11 tests) | Inverter API interactions |
| `test_entity_mode.py` (18 tests) | Entity-mode interop |
| `test_services.py::TestHandleClearOverrides` (9 tests) | Override clearing |
| `test_services.py::TestHandleFeedin` (2 tests) | Feed-in service |
| `test_services.py::TestHandleForceCharge` (3 tests) | Force charge service |
| `test_services.py::TestHandleForceDischarge` (4 tests) | Force discharge service |
| `test_services.py::TestHandleSmartDischarge` (11 tests) | Smart discharge lifecycle |
| `test_services.py::TestFeedinEnergyLimit` (6 tests) | Feed-in energy tracking |
| `test_init.py::TestRemoveModeFromSchedule` (7 tests) | Schedule mode removal |
| `test_init.py::TestGetNetConsumption` (6 tests) | Consumption calculation |
| `test_init.py::TestGetFeedinEnergyKwh` (6 tests) | Feed-in energy reading |
| `test_smart_battery_algorithms.py::TestDischargePowerPeakSafetyFloor` (5 tests) | Peak safety floor |
