---
project: FoxESS Control
level: 6
last_verified: 2026-04-24
traces_up: [02-constraints.md, 04-design/]
---
# Test Inventory

863 unit + 136 E2E + 19 soak = 1018 total (authoritative count via
`pytest --co -q` 2026-04-24).

Unit tests run with pytest-xdist (`-n auto`, randomised via
pytest-randomly). E2E tests use Podman containers with a FoxESS
simulator and Playwright browser automation. Soak tests run real-time
charge/discharge scenarios through containerised HA + simulator
(marked `slow`, run nightly via systemd timer).

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
| `TestCalculateDischargeDeferredStart::*` (14 tests) | Deferred start timing | C-001 |
| `TestCalculateDischargeDeferredStart::test_tight_window_feedin_does_not_over_defer` | Feedin cap skipped in tight windows | C-001 |
| `TestFeedinHeadroomAccountsForExportClamp::*` (6 tests) | Doubled feed-in headroom skipped when export clamp slack exceeds peak load (D-044 2026-04-24 refinement) | C-037 |

## Export-Limit Actuator (D-047)

**Constraints**: C-001, C-037
**Source**: `tests/test_export_limit.py` (24 tests)

| Test | Verifies | Constraint |
|---|---|---|
| `TestClampExportLimitW::*` (6 tests) | C-001 floor enforcement and hardware-max upper clamp | C-001 |
| `TestListenerWriteSuppression::test_small_delta_suppressed` | Sub-threshold changes suppressed to avoid actuator churn | D-047 |
| `TestListenerWriteSuppression::test_large_delta_written` | Over-threshold changes do write | D-047 |
| `TestListenerStartsAtHardwareMax::test_start_writes_hw_max` | Session start pins actuator at hardware max | D-047 |
| `TestListenerOverwriteExternalChanges::test_external_change_overwritten` | External changes to the actuator are reasserted | D-047 |
| `TestSmartDischargeExportLimitSensor::*` (2 tests) | Sensor surfaces modulated + max; unavailable when no session | C-020 |
| `TestAdapterExportLimitInterface::test_get_unavailable_returns_none` | Adapters without entity return None | D-047 |
| `TestSmartOperationsOverviewAttribute::*` | Overview attribute reflects last written | C-020 |
| `TestExportLimitThreshold::test_default_threshold_is_50w` | Default `export_limit_min_change_w` | D-047 |

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

## Smart Charge Re-deferral (D-043)

**Constraints**: D-043, D-006
**Source**: `tests/test_charge_redeferral.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestChargeRedeferral::test_ahead_of_schedule_switches_to_self_use` | Re-defer when SoC ahead of schedule | D-043 |
| `TestChargeRedeferral::test_at_or_behind_schedule_keeps_charging` | No re-deferral when on track | D-043, D-006 |
| `TestChargeRedeferral::test_redeferral_clears_charging_started` | `charging_started` cleared on re-defer | D-043 |
| `TestChargeRedeferral::test_redeferral_saves_session` | Session persisted after re-defer | D-043 |
| `TestChargeRedeferral::test_resumes_charging_after_redeferral` | Forced charge resumes when deadline arrives | D-043 |

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
| `TestMapWsToCoordinator::test_grid_balance_unreliable_unmeasured_generation` | Balance diverges from grid (external gen) → gridStatus fallback | C-006 |
| `TestMapWsToCoordinator::test_grid_balance_unreliable_importing` | Balance diverges from grid (importing) → gridStatus fallback | C-006 |
| `TestStaleness::test_stale_messages_skipped` | timeDiff > 30 filtered | C-005 |
| `TestIsPlausible::*` (11 tests) | >10x divergence filter | C-004 |
| `TestWsPlausibilityFilter::*` (3 tests) | Plausibility in WS listen loop | C-004 |

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
| `TestRecordChargeTemp::*` | Temperature factor recording | C-014 |
| `TestTempFactor::*` | Temperature factor query + nearest-neighbour | C-014 |
| `TestEstimateChargeHours::*` | Taper-aware time estimation | C-014 |

## Taper Observation Recording

**Constraints**: C-014
**Source**: `tests/test_taper_observation.py`

| Test | Verifies | Constraint |
|---|---|---|
| `TestTaperObservation::test_records_ratio_using_max_power_denominator` | max_power_w denominator (not paced) | C-014 |
| `TestTaperObservation::test_full_power_charge_records_correct_ratio` | Correct ratio at full power | C-014 |
| `TestTaperObservation::test_temp_recording_uses_max_power_denominator` | Temperature factor uses max_power_w | C-014 |

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

**Constraints**: C-003, C-012, C-013, C-016, C-024, C-025
**Source**: `tests/test_init.py`, `tests/test_services.py` (108 tests)

| Test | Verifies | Constraint |
|---|---|---|
| `TestResolveStartEnd::test_exceeds_max_hours` | 4-hour cap | C-013 |
| `TestResolveStartEnd::test_zero_duration_rejected` | Positive duration required | C-013 |
| `TestHandleSmartCharge::test_soc_unavailable_aborts_after_threshold` | SoC unavailable cancels charge | C-012 |
| `TestHandleSmartCharge::test_soc_available_resets_unavailable_count` | Available SoC resets counter | C-012 |
| `TestSessionPersistence::*` (3 tests) | Session survives restarts | C-003 |
| `TestRecoverSessions::*` (13 tests) | Session recovery on startup | C-003, C-024, C-027 |
| `TestSocStabilityCounters::*` (4 tests) | Below-min confirmation counter | C-002 |
| `TestCheckScheduleSafe::*` (7 tests) | Unmanaged mode rejection | C-018 |
| `TestTransientApiErrorResilience::test_charge_survives_transient_api_error` | Single API error retried | C-024 |
| `TestTransientApiErrorResilience::test_discharge_survives_transient_api_error` | Single API error retried | C-024 |
| `TestTransientApiErrorResilience::test_charge_aborts_after_repeated_errors` | Consecutive errors abort | C-024 |
| `TestStaleWorkModeAfterCleanupFailure::test_clear_overrides_clears_work_mode_immediately` | Session cancel hook fires from all paths | C-024, C-025 |
| `TestStaleWorkModeAfterCleanupFailure::test_failed_cleanup_schedules_pending_retry` | Failed cleanup stores pending retry | C-024, C-025 |
| `TestHandleSmartDischarge::test_deferred_to_discharging_triggers_ws` | WS connects on deferred→active transition | C-024 |

## Structured Session Logging

**Constraints**: C-020
**Source**: `tests/test_structured_logging.py` (12 tests)

| Test | Verifies | Constraint |
|---|---|---|
| `TestSessionContextFilter::test_injects_charge_session_context` | Charge fields on log record | C-020 |
| `TestSessionContextFilter::test_injects_discharge_session_context` | Discharge fields on log record | C-020 |
| `TestSessionContextFilter::test_empty_dict_when_no_session_active` | No session = empty context | C-020 |
| `TestSessionContextFilter::test_never_suppresses_records` | Filter never drops records | C-020 |
| `TestSessionContextFilter::test_discharge_wins_session_type_when_both_active` | Discharge takes priority | C-020 |
| `TestSessionContextFilter::test_survives_getter_exception` | Graceful degradation | C-020 |
| `TestSessionContextFilter::test_missing_fields_are_skipped` | Sparse state handled | C-020 |
| `TestInstallRemove::test_roundtrip` | Install/remove lifecycle | C-020 |
| `TestInstallRemove::test_filter_enriches_records_through_logger` | End-to-end enrichment | C-020 |
| `TestDebugLogHandlerWithSession::test_handler_includes_session_in_buffer` | Session in debug sensor | C-020 |
| `TestDebugLogHandlerWithSession::test_handler_omits_session_when_empty` | No session = no key | C-020 |
| `TestDebugLogHandlerWithSession::test_handler_omits_session_when_attr_is_empty_dict` | Empty dict = no key | C-020 |

## Sensor Display

**Constraints**: --
**Source**: `tests/test_sensor.py` (86 tests)

Key tests:
- Override status formatting (charge/discharge/deferred/idle)
- Progress bar trajectory (charge rises, discharge falls)
- Deferred start display (time until start, clamped to window)
- Grid export limit: 3 tests verifying deferred countdown accounts for
  `grid_export_limit_w` (D-044) — capped export rate, consumption
  interaction, feedin deadline
- Graceful degradation when data missing

## Entity Mode (Modbus Interop)

**Constraints**: --
**Source**: `tests/test_entity_mode.py` (25 tests)

Key tests:
- Work mode mapping (SelfUse/ForceCharge/ForceDischarge)
- Entity state reading with unavailable fallback
- Power and SoC entity writes
- Unit conversion: W→kW, Wh→kWh via HA's built-in converters
- All 9 entity mappings populated via `build_entity_map` 3-tuples

## Config Flow

**Constraints**: --
**Source**: `tests/test_config_flow.py` (16 tests)

Key tests:
- API key validation, error handling
- Web credential hashing on save
- foxess_modbus entity auto-detection
- Reconfigure flow for web credentials

## BMS Temperature (Web Portal)

**Constraints**: C-020
**Source**: `tests/test_web_session.py` (12 tests)

| Test | Verifies | Constraint |
|---|---|---|
| `TestBMSBatteryTemperature::test_temperature_returned_from_device_detail` | Temperature from GET /dew/v0/device/detail | C-020 |
| `TestBMSBatteryTemperature::test_temperature_as_plain_number` | Plain numeric temperature format | C-020 |
| `TestBMSBatteryTemperature::test_temperature_as_string_value` | String temperature value (real API format) | C-020 |
| `TestBMSBatteryTemperature::test_temperature_none_when_no_battery_data` | Graceful degradation (missing battery) | -- |
| `TestBMSBatteryTemperature::test_temperature_none_when_temp_field_missing` | Graceful degradation (missing temp) | -- |
| `TestBMSBatteryTemperature::test_temperature_none_when_endpoint_errors` | Graceful degradation (API error) | -- |
| `TestBatteryDetailEndpoint::test_uses_get_with_compound_id_and_category` | GET method, compound ID query param, category=battery | C-020 |
| `TestBatteryDetailEndpoint::test_token_plumbing_end_to_end` | Token plumbing: login → GET request headers | C-020 |
| `TestBatteryDetailEndpoint::test_login_failure_returns_none` | Graceful degradation (login failure) | -- |
| `TestCompoundIdFromWebSocket::test_compound_id_extracted_from_ws_message` | batteryId@batSn extracted from WS bat node | C-020 |
| `TestCompoundIdFromWebSocket::test_no_compound_id_without_battery_id` | No compound ID when batteryId missing | -- |
| `TestCompoundIdFromWebSocket::test_no_compound_id_without_bat_sn` | No compound ID when multipleBatterySoc empty | -- |

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

## Simulator Model

**Constraints**: C-028, C-033
**Source**: `tests/test_simulator_model.py` (22 tests)

Key tests:
- Charge/discharge taper modelling (BMS CV phase)
- Battery efficiency factor (round-trip losses)
- SelfUse solar routing at full SoC
- ForceCharge solar contribution
- Power fuzzing jitter

## Vendored Code Sync

**Constraints**: C-015, C-016, C-021
**Source**: `tests/test_smart_battery_sync.py`

| Test | Verifies | Constraint |
|---|---|---|
| `test_vendored_copy_matches_canonical` | Byte-identical copies | C-015 |
| `test_smart_battery_has_no_brand_imports` | No foxess imports in smart_battery/ | C-021 |
| `test_cancel_smart_session_is_synchronous` | cancel_smart_session is sync (no awaits) | C-016 |

## E2E Tests (Containerised HA + Simulator + Playwright)

**Source**: `tests/e2e/test_e2e.py` (62 tests), `tests/e2e/test_ui.py` (80 tests)
**Infrastructure**: Podman HA container, FoxESS simulator, Playwright Chromium

| Test | Verifies | Constraint |
|---|---|---|
| `TestSmartDischarge::test_discharge_starts` | Service → schedule → state transition | C-001 |
| `TestSmartDischarge::test_discharge_drains_battery` | SoC decreases during discharge | C-001, C-002 |
| `TestSmartCharge::test_charge_starts` | Charge service + state transition | -- |
| `TestEntityMode::test_self_use_on_clear` | Entity mode override cleanup | C-025 |
| `TestEntityMode::test_work_mode_entity_updated` | Entity mode write | -- |
| `TestEntityMode::test_power_entity_written` | Entity mode power write | -- |
| `TestFaultInjection::test_ws_unit_mismatch_handled` | WS kW/W unit detection | C-004 |
| `TestDataSource::test_api_source_when_idle` | data_source attribute = "api" | C-020 |
| `TestDataSource::test_ws_always_connects_without_session` | ws_mode=always activates WS at startup | C-020 |
| `TestDataSource::test_ws_connects_after_deferred_start` | WS connects on deferred→active | C-001 |
| `TestDataSource::test_ws_connects_on_second_session` | WS reconnects for new session | C-003 |
| `TestDataSource::test_ws_recovers_after_stream_stolen` | WS reconnects after theft | C-024 |
| `TestDataSource::test_ws_reconnects_after_reload_at_max_power` | WS reconnects after reload | C-024 |
| `TestDataSource::test_ws_mode_persists_via_options_flow` | ws_mode saved in options | C-020 |
| `TestDataSource::test_ws_linger_captures_post_discharge_data` | Linger captures post-session data after override removal | C-007, C-020 |
| `TestFeedinPacing::test_feedin_power_adjusts_over_time` | Feed-in pacing E2E | C-001 |
| `TestReloadRecovery::test_discharge_resumes_after_reload` | Discharge survives HA restart | C-024, C-025 |
| `TestReloadRecovery::test_charge_resumes_after_reload` | Charge survives HA restart | C-024, C-025 |
| `TestReloadRecovery::test_ws_reconnects_after_discharge_reload` | WS reconnects after restart | C-024 |
| `TestReloadRecovery::test_ws_reconnects_after_charge_reload` | WS reconnects after restart | C-024 |
| `TestReloadRecovery::test_idle_after_reload_with_no_session` | Clean state after no-session restart | C-025 |
| `TestReloadRecovery::test_entity_mode_discharge_resumes_after_reload` | Entity mode restart recovery | C-024 |
| `TestReloadRecovery::test_session_clears_after_window_expires_during_reload` | Expired session cleaned on restart | C-025 |
| `TestOverviewCard::test_card_renders` | Overview card in shadow DOM | -- |
| `TestOverviewCard::test_shows_soc` | SoC displayed on card | -- |
| `TestOverviewCard::test_house_load_never_greyed` | House node active at low load | C-020 |
| `TestOverviewCard::test_data_source_badge_matches_mode[api/ws/entity]` | Badge reflects active data path + staleness suffix | C-020 |
| `TestOverviewCard::test_pv_values_consistent_with_solar_total[api/ws/entity]` | PV1+PV2 ≈ total solar | C-020 |
| `TestOverviewCard::test_stale_badge_shown_for_old_api_data[api/ws/entity]` | Staleness indicator on old data | C-020 |
| `TestControlCard::test_card_renders` | Control card in shadow DOM | -- |
| `TestControlCard::test_soc_displayed` | SoC percentage in header | -- |
| `TestControlCard::test_progress_hidden_when_idle` | No progress section when idle | C-020 |
| `TestControlCard::test_progress_visible_during_discharge[api/ws/entity]` | Progress section during discharge | C-020 |
| `TestControlCard::test_schedule_horizon_during_discharge` | Schedule horizon displayed | C-027 |
| `TestFormInputPersistence::test_time_input_survives_rerender` | Form values persist through hass update | C-020 |
| `TestFormInputPersistence::test_time_input_survives_multiple_rerenders` | Form values persist through 3 rapid updates | C-020 |
| `TestFormInputPersistence::test_rerender_between_field_edits` | Interleaved re-render preserves earlier values | C-020 |
| `TestFormInputPersistence::test_time_picker_stays_open_during_rerender` | DOM identity and focus preserved (sentinel check) | C-020, D-040 |
| `TestFormInputPersistence::test_form_recovers_from_page_navigation` | Form recovery after HA page navigation | C-020 |
| `TestScreenshots::test_idle_screenshot` | Visual regression capture | -- |
| `TestScreenshots::test_discharging_screenshot` | Visual regression capture | -- |

Tests are parametrized across `[cloud, entity]` connection modes and
`[api, ws, entity]` data sources. The `ws_refuse` simulator fault blocks
WS connections for API-only mode. Invalid parametrisation combos
(`entity-api`, `entity-ws`, `cloud-entity`) are deselected at collection
time. Total E2E count is 130.

## Soak Tests (Real-Time Charge/Discharge Scenarios)

**Source**: `tests/soak/test_scenarios.py` (17 tests)
**Infrastructure**: Podman HA container, FoxESS simulator with auto-tick (5s),
per-worker PID-prefixed containers, systemd nightly timer.

| Test | Verifies | Constraint |
|---|---|---|
| `test_charge_basic` | 20%→80%, 4h, flat load, no solar | C-001 |
| `test_charge_with_solar` | Charge + 4kW solar, D-043 re-deferral | C-001, D-043 |
| `test_charge_spiky_load` | Charge + intermittent high load spikes | C-001 |
| `test_charge_high_soc_taper` | 70%→100%, BMS taper above 90% | C-014 |
| `test_charge_cold_battery` | Charge at 5°C, BMS current limiting | C-014 |
| `test_charge_large_battery` | 42kWh battery, extended charging | C-001 |
| `test_charge_solar_exceeds_target` | Solar alone meets target, session idles | D-043 |
| `test_charge_solar_then_spike` | Solar meets target, spike drops below, resume | D-043 |
| `test_charge_heavy_load_during_deferral` | 3kW load drains SoC during deferral | C-001 |
| `test_charge_tight_window` | 45-min window barely fits 20%→80% charge | C-022 |
| `test_discharge_basic` | 80%→20%, 4h, flat load | C-001, C-002 |
| `test_discharge_with_solar` | Discharge + 3kW solar extends battery | C-001 |
| `test_discharge_solar_exceeds_load` | 5kW solar > 1.5kW load, SoC rises | C-001 |
| `test_discharge_spiky_load` | Load spikes exceeding inverter max | C-001 |
| `test_discharge_near_min_soc` | Start near min_soc, end-of-discharge guard | C-002, C-017 |
| `test_discharge_large_battery` | 42kWh battery, extended discharge | C-001 |
| `test_charge_then_discharge` | Full cycle: charge to 90%, discharge to 20% | C-001, C-002 |

Soak tests use `SoakRecorder` to capture samples every 10s, check
invariants (monotonic SoC progress, no charge overshoot via monotonic
energy counters, no grid import during discharge). SQLite inflection-point
store (`tests/soak/results_db.py`) records state transitions and SoC
direction changes for cross-run comparison.

## Test Quality Rules

Enforced by ruff (`S110`, `BLE001`) and CLAUDE.md constraints:

| Rule | Enforced by | Rationale |
|---|---|---|
| No `except Exception: pass` in tests/simulator | ruff `S110` + `BLE001` | Swallows real failures, makes flakes undiagnosable |
| No `time.sleep()` / `wait_for_timeout()` in tests | CLAUDE.md | Use `wait_for_state`, `wait_for_attribute`, `expect()` |
| No bare `page.reload()` in Playwright tests | CLAUDE.md | Use `_robust_reload()` — `goto` + `networkidle` avoids `net::ERR_ABORTED` |
| Prefer element waits over sleeps after state changes | CLAUDE.md | Fixed delays are fragile under load |

Production code (`custom_components/`, `smart_battery/`) is exempt from `BLE001`
because broad catches there are intentional graceful degradation.

## Unmapped Tests

Tests not yet traced to a specific constraint. ~140 tests across multiple
files verify operational correctness, display logic, and setup plumbing.

| Test | Appears to verify |
|---|---|
| `test_sensor.py::TestBatteryForecastSensor` (8 tests) | Forecast trajectory calculations |
| `test_sensor.py::TestDebugLog` (7 tests) | Debug sensor lifecycle (rolling + init) |
| `test_sensor.py::TestFoxESSPolledSensor` (5 tests) | Sensor plumbing |
| `test_sensor.py::TestFoxESSWorkModeSensor` (5 tests) | Work mode sensor |
| `test_sensor.py` display tests (~25 tests) | Charge/discharge remaining, power, window formatting |
| `test_inverter.py` (10 tests) | Inverter API interactions |
| `test_entity_mode.py` (21 tests) | Entity-mode interop |
| `test_services.py::TestHandleClearOverrides` (9 tests) | Override clearing |
| `test_services.py::TestHandleForceDischarge` (8 tests) | Force discharge service |
| `test_services.py::TestHandleForceCharge` (7 tests) | Force charge service |
| `test_services.py::TestSmartChargeCoordinatorFallback` (2 tests) | Coordinator SoC fallback |
| `test_services.py::TestRemainingZeroCancels` (1 test) | Expired window cleanup |
| `test_services.py::TestSetupEntry` (3 tests) | Service registration lifecycle |
| `test_services.py::TestUnloadEntry` (2 tests) | Unload cleanup |
| `test_services.py::TestGetMinSocOnGrid` (2 tests) | MinSocOnGrid config |
| `test_services.py::TestHandleFeedin` (2 tests) | Feed-in service |
| `test_services.py::TestFeedinBaselineDeferred` (1 test) | Feed-in baseline capture timing |
| `test_services.py::TestHandleSmartDischarge` (14 tests) | Smart discharge lifecycle |
| `test_services.py::TestFeedinEnergyLimit` (6 tests) | Feed-in energy tracking |
| `test_init.py::TestRemoveModeFromSchedule` (7 tests) | Schedule mode removal |
| `test_init.py::TestGetNetConsumption` (6 tests) | Consumption calculation |
| `test_init.py::TestGetFeedinEnergyKwh` (6 tests) | Feed-in energy reading |
| `test_smart_battery_algorithms.py::TestDischargePowerPeakSafetyFloor` (5 tests) | Peak safety floor |
