---
project: FoxESS Control
level: 5
last_verified: 2026-04-16
traces_up: [02-constraints.md, 04-design/]
traces_down: [06-tests.md]
---
# Coverage Matrix

Traceability from constraints through design decisions to tests.

## Matrix

| Constraint | Design | Tests | Status |
|---|---|---|---|
| C-001 No grid import during discharge | D-001, D-002, D-003, D-004, D-005 | `TestCalculateDischargePower` (21), `TestShouldSuspendDischarge` (21), `TestDischargePowerFeedinConstraint` (10), `TestCalculateDischargeDeferredStart` (13) | COVERED |
| C-002 Never discharge below min SoC | D-001 | `TestShouldSuspendDischarge::test_soc_at_min_suspends`, `test_soc_below_min_suspends` | COVERED |
| C-003 Session identity prevents races | D-017, D-018 | -- (tested via integration, not isolated unit tests) | PARTIAL |
| C-004 WS watts / coordinator kW | D-010 | `TestMapWsToCoordinator::test_real_world_sample`, `test_basic_mapping_export` | COVERED |
| C-005 WS stale message filter | D-008 | `TestStaleness::test_stale_messages_skipped` | COVERED |
| C-006 Grid direction from power balance | D-010 | `TestMapWsToCoordinator::test_grid_importing_from_balance`, `test_grid_exporting_from_balance` | COVERED |
| C-007 REST resets WS integration state | D-009 | `TestInjectRealtimeData::test_rest_poll_resets_integration_state` | COVERED |
| C-008 fdSoc >= 11, minSocOnGrid <= fdSoc | D-014 | `TestSanitizeGroup::test_clamps_fd_soc_to_api_minimum` | COVERED |
| C-009 No midnight-crossing schedules | D-014 | `TestResolveStartEnd::test_crosses_midnight_rejected` | COVERED |
| C-010 Placeholder groups filtered | D-014 | `TestIsPlaceholder` (8 tests) | COVERED |
| C-011 Extra fields stripped | D-014 | `TestSanitizeGroup::test_strips_unknown_keys` | COVERED |
| C-012 SoC unavailability cancels charge session | D-019 | `TestHandleSmartCharge::test_soc_unavailable_aborts_after_threshold`, `test_soc_available_resets_unavailable_count` | COVERED |
| C-013 4-hour max override duration | -- | `TestResolveStartEnd::test_exceeds_max_hours` | PARTIAL |
| C-014 Taper profile plausibility | D-011, D-012, D-013 | `TestIsPlausible` (5), `TestRecordCharge` (8), `TestSerialization` (6) | COVERED |
| C-015 Vendored smart_battery matches canonical | -- | `test_vendored_copy_matches_canonical` | COVERED |
| C-016 Cancel listeners before awaits | D-018 | -- | GAP |
| C-017 End-of-discharge guard | D-003 | `TestShouldSuspendDischarge::test_high_consumption_suspends` | COVERED |
| C-018 Unmanaged work mode protection | D-016 | `TestCheckScheduleSafe` (7), `test_rejects_schedule_with_backup_mode` | COVERED |
| C-020 Operational transparency | D-021 | `TestDataSourceTracking` (3), `TestFoxESSPolledSensor::test_data_source_*` (2) | COVERED |
| C-021 Brand-agnostic code in common package | -- | `test_vendored_copy_matches_canonical` (indirect) | PARTIAL |
| C-026 Proactive error surfacing | -- | `TestErrorSurfacing` (2) | COVERED |
| C-025 Session boundary cleanliness | -- | `TestSessionBoundaryCleanness` (2) | COVERED |
| C-024 Safe state on failure | -- | `TestCallbackExceptionSafety` (2), C-012, unload_entry | COVERED |
| C-023 Solar-aware charge reduction | -- | -- | GAP |
| C-022 Unreachable charge target surfaced | -- | `TestIsChargeTargetReachable` (7) | COVERED |
| C-019 Discharge SoC unavailability abort | D-019 | `TestDischargeSocUnavailability` (2) | COVERED |
| C-027 Progressive schedule extension | D-023 | `compute_safe_schedule_end` tested via `TestHandleSmartDischarge` | COVERED |
| C-028 Simulator over mocks | -- | `test_client.py`, `test_inverter.py` use simulator | COVERED |
| C-029 E2E for HA-dependent behaviour | -- | `e2e/test_e2e.py` (5), `e2e/test_ui.py` (14) | COVERED |
| C-030 E2E parallel before tagging | -- | `.githooks/pre-push` enforces gate | COVERED |

## Gaps

### Constraints without tests (GAP)
- **C-016**: Cancel-before-await ordering — the race-prevention pattern
  in `__init__.py` is structural and not verified by any test. A test
  would need to simulate concurrent timer firing during cancellation.
- ~~C-019~~: Fixed — discharge SoC unavailability now has counter+abort.
- **C-022**: Unreachable charge target — no detection or user
  notification when the target SoC cannot be reached in the remaining
  window. Proposed feature.
- **C-023**: Solar-aware charge reduction — grid charge power is not
  reduced when solar surplus is available. Proposed feature.
- ~~C-026~~: Fixed — error state surfaced via sensor attributes.

### Constraints without design docs (PARTIAL)
- **C-013**: 4-hour max override — this is a simple constant guard, not
  a significant design decision. Covered by test but no design doc.
- **C-021**: Brand-agnostic code in common package — architectural
  constraint enforced by code review, not a testable invariant. C-015
  (vendored sync) provides indirect verification.
- **C-024**: Safe state on failure — `async_unload_entry` cleans up on
  integration unload, C-012 handles SoC unavailability, but no
  systematic guarantee across all failure paths (uncaught exceptions,
  API failures mid-session).
- **C-025**: Session boundary cleanliness — cancellation removes
  overrides and new sessions initialise fresh state, but no test
  verifies that transient state (peak consumption, taper ticks,
  feed-in baselines) doesn't leak between back-to-back sessions.

### Design decisions without tests (UNVERIFIED)
- **D-002**: Deferred start with self-use — the deferred start
  *calculation* is tested, but the actual mode-switching behaviour
  (self-use -> forced discharge at deadline) is only tested via
  integration flow.
- **D-009**: Post-session linger — the 30-second linger timeout after
  session end is not tested in isolation.
- **D-016**: Unmanaged mode protection — tested by
  `test_rejects_schedule_with_backup_mode` but only for Backup mode.
  Other unmanaged modes not tested.

### Tests without traced constraints (ORPHAN)
- `test_sensor.py::TestBatteryForecastSensor` (8 tests) — may protect
  an unstated constraint about forecast shape accuracy
- `test_sensor.py::TestDebugLog` (6 tests) — operational utility
- `test_inverter.py::test_max_power_cached` — performance optimisation

## Summary

- **Total constraints**: 30
- **Fully covered**: 25 (83%)
- **Partial**: 3 (11%)
- **Gaps**: 2 (7%) — C-016 (structural), C-023 (investigation needed)
- **Orphan tests**: 80+ unit (test_services.py largely unmapped)
- **E2E tests**: 19 (5 REST + 14 Playwright) across API and WS modes
