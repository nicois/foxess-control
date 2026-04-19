# Changelog

## 1.0.7-beta.14

### Added
- **Two-tier circuit breaker** (C-024): 3 consecutive adapter errors open the circuit breaker and hold position; 5 more ticks without recovery abort the session to self-use. Previously, 3 errors triggered immediate abort.
- **Automatic session replay after outage**: when the circuit breaker aborts a session and the time window is still open, the integration probes the API every 5 minutes and restarts the session on recovery (up to 6 attempts).
- **Proactive schedule conflict detection**: a 30-minute periodic check warns about unmanaged schedule modes (e.g. Backup) before they block a session start. Warnings surface via persistent notification and the `upcoming_conflicts` sensor attribute.
- **Forecast chart card** (`foxess-forecast-card`): SVG-based Lovelace card showing projected SoC trajectory with actual history overlay, target/min SoC markers, and time axis.
- **Session history timeline card** (`foxess-history-card`): 24h horizontal timeline with coloured session bars (charge/discharge/deferred/aborted) and SoC trace overlay.
- **Action buttons on control card**: cancel (with double-tap confirmation), charge, and discharge buttons with inline parameter forms.
- **Visual card editors**: both the control card and overview card now support HA's visual card editor for entity configuration.
- **Troubleshooting guide** (`TROUBLESHOOTING.md`): 5 decision trees for common issues.
- **Contributing guide** (`CONTRIBUTING.md`): local dev setup, simulator architecture, E2E infrastructure, adapter protocol.
- **FAQ** (`FAQ.md`): answers to 7 common user questions.
- **Performance regression gate**: CI job testing algorithm calculation time, taper profile throughput, and AST scan for sync I/O in async functions.
- **Fault recovery E2E tests**: 5 cloud-mode tests covering circuit breaker, transient error survival, WS fallback, and WS reconnection.
- **Entity-mode E2E parity**: discharge-drains-battery, charge lifecycle, and min_soc suspension tests now run in entity mode.

### Fixed
- **Discharge circuit breaker unreliable**: the discharge pacing loop skipped `adapter.apply_mode()` when power was unchanged between ticks, so the circuit breaker could never detect failures during steady-state discharge. Now calls the adapter every tick, matching the charge path.
- **Circuit breaker not visible on dashboard**: `circuit_breaker_active` was only surfaced on the override status sensor, not the primary smart_operations sensor that the dashboard uses.
- **Entity-mode discharge test incorrect**: expected `discharge_suspended` but discharge at min_soc ends the session (→ idle), not suspends it.
- **Entity-mode charge lifecycle timeout**: charge listener monitors until window expires after reaching target; test now uses a 5-min window to avoid timeout.

### Improved
- **Migration guide rewritten**: recommends clean install over side-by-side migration, with guidance for cleaning up orphaned entities.
- **Circuit breaker attributes surfaced**: `circuit_breaker_active` and `circuit_breaker_since` on both the override status and smart_operations sensors for UI observability.
- **Replay attributes surfaced**: `replay_pending`, `replay_type`, `replay_attempts` on the smart_operations sensor.

## 1.0.7-beta.13

### Fixed
- **BMS battery temperature always unknown**: the endpoint requires `GET /dew/v0/device/detail?id={batteryId}@{batSn}&category=battery` — not POST, not the device serial, and the compound battery ID comes from the WebSocket `bat` node. Previous attempts failed because they used POST, used the wrong identifier, or hit `/generic/v0/` endpoints that reject the web session token.

## 1.0.7-beta.12

### Fixed
- **BMS battery temperature always unknown**: the `/generic/v0/` API namespace rejects the web session token with errno=41808 ("Token has expired"), even immediately after login. Switched to `POST /dew/v0/device/detail` which accepts the web session token and returns battery temperature.

## 1.0.7-beta.11

### Fixed
- **Blocking I/O on event loop**: `wasmtime.Module.from_file()` performed a synchronous file read during lazy signature engine init. WASM bytes are now pre-read at module import time (in HA's executor) and the Module constructed from memory.

### Improved
- **HA Integration Quality Scale compliance**:
  - **Bronze — action-setup**: service registration moved from `async_setup_entry` to `async_setup`, so actions are available before any config entry loads.
  - **Gold — exception-translations**: all `ServiceValidationError` and `HomeAssistantError` raises now include `translation_domain`/`translation_key` for HA's i18n framework.
  - **Platinum — async-dependency**: WASM signature generation runs in the default executor via `_async_make_headers`, keeping the event loop unblocked.
  - **Platinum — strict-typing**: added PEP-561 `py.typed` marker file.

## 1.0.7-beta.10

### Fixed
- **BMS battery temperature always unknown**: removed unreliable Open API `get_detail` dependency for battery SN discovery (errno 40256). Now uses web portal `/generic/v0/device/list` for device discovery + `/generic/v0/device/battery/info` for temperature, bypassing the Open API entirely.

### Added
- **Init debug log sensor** (`sensor.foxess_init_debug_log`): non-wrapping buffer that preserves the first 75 log messages after HA startup, complementing the rolling `sensor.foxess_debug_log`. Captures startup exceptions and initialization flow that the rolling buffer would evict.

### Improved
- **E2E test infrastructure hardened**: replaced all hardcoded `time.sleep()` calls with deterministic waits (`wait_for_state`, `wait_for_numeric_state`, `wait_for_attribute`). Narrowed blind `except Exception` to specific types. Deleted duplicate `_reload_integration`. Added `_wait_for_integration_ready` helper.
- **Playwright reload flakiness fixed**: replaced bare `page.reload()` with `_robust_reload()` (`page.goto` + `networkidle`) to avoid `net::ERR_ABORTED` races in CI.
- **Ruff lint rules expanded**: enabled `S110` (try-except-pass), `S112` (try-except-continue), `BLE001` (blind except), `B904` (raise-without-from) for tests and simulator.
- **Test quality constraints**: added CLAUDE.md rules banning hardcoded sleeps, blind exception swallowing, and bare `page.reload()` in tests.
- **Pre-commit vendor sync hook**: automatically syncs `smart_battery/` to `custom_components/foxess_control/smart_battery/` on commit.

## 1.0.7-beta.7

### Added
- **BMS battery temperature sensor**: new `sensor.foxess_bms_battery_temperature` exposes the min cell temperature from the BMS via the FoxESS web portal API. This is operationally critical — low BMS temperatures inhibit charge rate, unlike the Open API's `batTemperature` which reports the inverter's own sensor.
- **E2E tests moved under `tests/`**: ensures they are discovered by default pytest collection; the `slow` marker allows skipping when desired.

### Improved
- **Typed runtime data**: `entry.runtime_data` stores `FoxESSEntryData` (coordinator, inverter, adapter) instead of untyped `hass.data[DOMAIN]` dict. `FoxESSControlData` bridge layer preserves backward compatibility during migration.
- **HA-managed aiohttp session**: `FoxESSWebSession` uses HA's shared HTTP session (`async_get_clientsession`) for proper SSL, proxy, and lifecycle management instead of creating its own.
- **Named background tasks**: all `async_create_task` calls include descriptive names for easier debugging and HA lifecycle tracking.
- **Theme-aware stale data badge**: stale data indicator uses `--primary-text-color` instead of amber, ensuring readability on both light and dark themes.
- **`serial_number` in DeviceInfo**: device serial from config entry is now included for better identification.
- **Platform enum**: `PLATFORMS` uses `Platform.BINARY_SENSOR`/`Platform.SENSOR` instead of raw strings.

## 1.0.6-beta.2

### Added
- **Graceful setup retry on cloud outage**: raises `ConfigEntryNotReady` when the FoxESS Cloud is unreachable during startup, so HA retries with exponential backoff instead of failing permanently.
- **PARALLEL_UPDATES = 0**: sensor and binary_sensor platforms declare no parallel updates since all data comes from the coordinator.
- **Repair issues for actionable errors**: unmanaged work mode (C-018) and session aborts now surface in HA's Repairs panel instead of just logs. Issues auto-clear when the problem is resolved or a new session starts.
- **Unrecorded attributes on high-churn sensors**: `SmartOperationsOverviewSensor`, `OverrideStatusSensor`, and `BatteryForecastSensor` mark frequently-changing attributes as unrecorded to prevent database bloat.
- **Clean removal**: `async_remove_entry` deletes the session Store file when the integration is removed entirely, preventing stale data if re-added later.
- **Diagnostics platform**: "Download Diagnostics" button in the integration page exports coordinator data, session state, WebSocket status, taper profile, and config — with API keys and credentials redacted.
- **Entity categories**: diagnostic-only sensors (temperatures, voltages, currents, grid frequency, EPS, throughput) marked as `DIAGNOSTIC` so they don't clutter default dashboards.
- **Disabled by default**: rarely-used sensors (PV1/PV2, battery voltage/current, ambient/inverter temp, grid current/frequency, EPS, throughput) disabled by default — users can enable them as needed.
- **Display precision**: all polled sensors set `suggested_display_precision` (0 for SoC, 2 for kW/kWh, 1 for °C/V/A/Hz) for clean dashboard values.
- **Enriched DeviceInfo**: device page shows inverter model name (from API device detail) and links to FoxESS Cloud portal.
- **Reauthentication flow**: when the FoxESS API key expires or becomes invalid, HA shows a "Reconfigure" prompt instead of silently failing. Users can enter a new key without removing and re-adding the integration.
- **Service action error handling**: API errors and network failures in service calls (force charge, smart discharge, etc.) now surface as user-friendly HA error toasts instead of generic "An error occurred".
- **icons.json**: all sensors, binary sensors, and service actions have Material Design Icons defined via `icons.json`. Smart Operations sensor uses state-aware icons (charging, discharging, deferred, etc.).

## 1.0.6-beta.1

### Fixed
- **Session recovery fails when schedule uses horizon end time**: after HA restart, `_has_matching_schedule_group` compared the session window end (e.g. 20:01) against the inverter schedule's safe horizon end (e.g. 19:24, set by C-027). Mismatch caused the session to be discarded while the inverter continued discharging. Now matches on work mode only, since any active ForceDischarge/ForceCharge group confirms the session is still live.
- **Debug log sensor exceeds recorder attribute limit**: reduced buffer from 200 to 75 entries and marked `entries` attribute as `_unrecorded_attributes` so the recorder doesn't attempt to persist the large debug payload.

## 1.0.5

### Added
- **WebSocket mode selector** (`ws_mode`): replaced the boolean `ws_all_sessions` toggle with a 3-state dropdown — **Auto** (WS only during paced forced discharge), **All smart sessions** (any smart session or force op), **Always connected** (WS preferred at all times with watchdog recovery). Existing configurations migrate automatically.
- **Data freshness sensor**: `sensor.foxess_data_freshness` exposes the current data source (`ws`, `api`, or `modbus`) as its state, with `last_update` and `age_seconds` attributes for staleness detection.
- **Data staleness indicator on Lovelace cards**: both cards compute data age client-side; badge turns red with elapsed time (e.g. "API · 2m") when data exceeds 30 seconds old.
- **Structured session logging**: session context (ID, type, SoC, power) enriched via `logging.Filter`; debug log sensor exposes structured data for E2E tests and power users.
- **Target power display**: Lovelace card shows current vs target discharge rate when they differ during feed-in pacing.
- **Entity-mode E2E tests**: input helpers simulate modbus entities; `connection_mode` fixture parametrizes cloud vs entity modes with function-scoped containers for full isolation.
- **Reconfigure flow**: add or update web portal credentials without re-creating the config entry. Accepts both raw password and pre-computed MD5 hash.
- **Structural tests**: AST-based verification of synchronous cancel functions (C-016) and brand import boundary (C-021).

### Changed
- **SoC display precision matches confidence**: Lovelace card shows integer SoC until the first confirmed integer change (e.g. 93→92), then switches to 2 decimal places. Before the first change, interpolation is just an estimate; after, the real SoC is known to be near X.5, making interpolation meaningful.
- **Interpolated SoC stored at full float precision**: rounding applied only for change detection (2dp gate to prevent entity update storms), not storage.
- **Session construction via factory functions**: `create_charge_session()` and `create_discharge_session()` ensure consistent field defaults and reduce duplication across callers.
- **Min SoC floor lowered to 0%**: `min_soc` and `min_soc_on_grid` now accept 0, removing the previous 5% floor.
- **Unified cancel functions**: `_cancel_smart_charge` and `_cancel_smart_discharge` replaced with delegates to brand-agnostic `cancel_smart_session`, ensuring the `_on_session_cancel` hook fires from all cancel paths.
- **Simulator fidelity**: charge taper above 90% SoC, discharge taper below 15% SoC, per-app state isolation, stale-stream behaviour matching FoxESS cloud.
- **E2E config uses production defaults**: tests needing non-default options set them explicitly via the options flow, matching real user setup.
- **GitHub Actions updated**: checkout v4→v6, setup-python v5→v6, upload-artifact v4→v7, download-artifact v4→v8.

### Fixed
- **WS linger race captured stale forced-discharge data (D-009)**: `_on_session_cancel` now returns the WS stop coroutine; callers await it after override removal completes, so the linger captures post-session self-use data. Also fixed the `clear_overrides` service path which used fire-and-forget.
- **Entity-mode service domain detection**: `apply_mode()` used hardcoded `"select"` and `"number"` domains, breaking `input_select`/`input_number` entities from foxess_modbus. Added `_entity_service_domain()` helper to derive the correct domain from the entity ID prefix.
- **Charge fdSoc regression**: listener must pass `fd_soc=100` to prevent FoxESS API validation failure (C-008).
- **Smart discharge starting before scheduled window**: deferred discharge listener omitted the `start=` parameter, bypassing the floor clamp. The inverter received `fdPwr=0` but ignored it and discharged at full power.
- **Force operations not cancelling opposite smart session**: both force operations now cancel both session types, preventing leftover listeners from fighting the schedule.
- **WS not connecting after deferred discharge start**: timer now fires the WS-aware wrapper, so WebSocket connects as soon as forced discharge begins.
- **WS reconnect during smart charge**: charge listener wrapper wasn't triggering `_maybe_start_realtime_ws`.
- **Session sensors delayed by ~30s**: now subscribe via `coordinator.async_add_listener` for instant state propagation.
- **Stale work mode badge after failed cleanup**: override removal retries on each successful REST poll; cancel paths clear `_work_mode` immediately.
- **Smart sessions survive transient API errors**: errors retried on next timer tick; only 3 consecutive failures trigger abort. Previously any transient cloud outage killed a multi-hour session.
- **SoC interpolation overshooting entity value**: clamp tightened to `[tick − 0.5, tick + 0.44]` so the rounded display always matches the entity.
- **REST poll starvation from SoC extrapolation**: now updates entity data directly without resetting the poll timer.
- **Feed-in pacing stuck at initial power**: switches to self-use when target is below `min_power_change` threshold, enabling ramp-up on the next tick.
- **Feed-in energy inflated at session start**: baseline deferred to the listener's first tick when fresh data is available.
- **Schedule horizon not set on immediate discharge start**: computed inline before state dict creation.
- **WS data_source badge stuck during stale stream**: coordinator notified immediately when reconnecting, so badge shows "API" until fresh WS data resumes.
- **WS stream-stolen recovery**: tracks last useful data timestamp; reconnects after 30s of only stale frames. Token invalidated on handshake error, forcing fresh login.
- **SoC interpolation stuck between ticks**: timestamp set after integration, made unconditional.
- **SoC clamp rounding**: upper bound changed to 0.94 to prevent displayed value exceeding authoritative tick.
- **Progress bar start SoC wrong after deferral**: updated to actual SoC when session begins.

## 1.0.4

### Added
- **Progressive schedule extension**: discharge schedule end time is set to a dynamically computed safe horizon based on current SoC, discharge rate, and safety factor (1.5×). If HA loses connectivity, the inverter's schedule expires and reverts to self-use — battery protected without HA intervention. Horizon shown on Lovelace card time progress bar as a vertical marker.
- **SoC interpolation in REST-only mode**: coordinator integrates battery power between REST polls for sub-percent SoC estimates, eliminating staircase progress bars when WebSocket is not active.
- **FoxESS simulator** (`simulator/`): standalone aiohttp server with REST API, WebSocket, web auth, and backchannel endpoints for testing. Supports fault injection, fast-forward, and fuzzing (±2% jitter). Unit tests migrated from mock library to simulator.
- **Containerised E2E tests** (`tests/e2e/`): real HA instance in Podman container with Playwright browser automation. 20 tests covering card rendering, discharge/charge lifecycle, PV consistency, data source badge (API and WS modes), schedule horizon marker, and screenshot regression. Runs in ~70s with 10 parallel workers.
- **`_on_session_cancel` hook**: WebSocket stops through all cancel paths (timer, SoC abort, exception, clear_overrides, target reached), and work mode clears immediately for the Lovelace card.

### Changed
- **Stale REST values hidden in WS mode**: overview card suppresses PV1/PV2 detail, grid voltage/frequency, battery temperature, and residual energy when WebSocket is the active data source — these values only update on REST polls and would be misleadingly stale.

### Fixed
- **Work mode label stuck after session ends**: overview card showed "Force Discharge" for minutes after the window finished. Now cleared immediately via `_on_session_cancel`.
- **WS not stopping when session ends via timer**: the brand-agnostic `cancel_smart_session` didn't trigger WebSocket shutdown.
- **WASM signature test ordering dependency**: module singleton heap state caused non-deterministic output across test runs.

## 1.0.3

### Added
- **SoC interpolation between integer ticks**: the coordinator integrates battery power over time to maintain a sub-percent SoC estimate for display. Progress bars and forecasts update smoothly between the ~6-minute integer SoC ticks instead of appearing stuck. Resyncs to the authoritative value on each tick change and REST poll. Algorithm decisions continue to use raw integer SoC.
- **Two-zone SoC progress bar**: solid fill for the inverter-confirmed SoC + semi-transparent extension for the power-integrated estimate. When the next SoC tick arrives, the solid fill catches up to the projected zone.

### Fixed
- **WebSocket not connecting during discharge**: the adapter extraction removed inline `_maybe_start_realtime_ws` calls from the listener code. The discharge callback is now wrapped to trigger WS lifecycle after each check.
- **WebSocket not connecting during smart charge**: the charge listener wrapper didn't trigger `_maybe_start_realtime_ws` when `ws_all_sessions` was enabled.
- **Persistent notifications lost on schedule conflict**: restored `pn_create` for unmanaged work mode detection by pre-checking schedule safety from the async context in the cloud adapter.

## 1.0.1

### Added
- **WebSocket real-time data**: optional ~5-second power data from the FoxESS Cloud WebSocket during smart sessions, reducing grid import risk from load spikes between 5-minute REST polls. Requires web portal credentials (optional config flow step). Connects automatically during paced discharge; `ws_all_sessions` toggle extends to all sessions.
- **Data source indicator on Lovelace cards**: badge shows "WS", "API", or "Modbus" when multiple data sources are configured, so users know which source is driving displayed values. Immediately switches on WS connect/disconnect.
- **Deferred self-use for smart discharge**: stays in self-use mode until a deadline calculation requires forced discharge, preventing grid import from low paced power
- **Peak consumption tracking**: exponential decay (~4.3 min half-life at 1-min ticks), floors discharge power at peak × 1.5 to absorb inter-poll load spikes
- **End-of-discharge guard**: suspends forced discharge ~10 min before window end when paced power would drop below house load
- **Discharge SoC unavailability abort**: discharge sessions now abort after 3 consecutive SoC-unavailable checks, matching charge path behaviour
- **Safe state on failure**: listener callbacks catch unexpected exceptions, cancel the session, and revert to self-use
- **Unreachable charge target detection**: `charge_target_reachable` sensor attribute warns when even max power can't reach the target SoC in remaining time
- **Proactive error surfacing**: session errors surfaced via sensor attributes (`has_error`, `last_error`, `last_error_at`, `error_count`) instead of log-only
- **Reconfigure flow**: add or update web portal credentials without re-creating the config entry. Accepts both raw password and pre-computed MD5 hash.
- **Feed-in energy integration from WebSocket**: trapezoidal integration between REST polls for more accurate cumulative energy tracking
- **Feed-in early-stop**: schedules a one-shot stop based on observed export rate to prevent overshooting the feed-in energy limit

### Changed
- **Session orchestration via adapter pattern**: `__init__.py` reduced from 3056 to ~2030 lines (-34%). FoxESS-specific schedule merging encapsulated in `FoxESSCloudAdapter` and `FoxESSEntityAdapter`, delegating to brand-agnostic `smart_battery/listeners.py`.
- **WebSocket per-field unit handling**: respects the `unit` property on each power field (some sent as kW, others as W within the same message) instead of assuming uniform units
- **REST fallback on poll failure**: coordinator keeps last-known data when REST fails, preventing all entities flashing unavailable

### Fixed
- **Taper-path consumption bypass**: deferred start taper paths now account for household consumption, matching the linear path
- **WebSocket stale data filter**: messages with `timeDiff > 30` seconds are discarded
- **WebSocket grid direction**: power-balance-derived direction replaces unreliable `gridStatus` field
- **Taper profile corruption**: minimum actual power guard (50W) and plausibility check auto-reset corrupted profiles
- **Progress bars during inactive phases**: hidden during charge "deferred" and discharge "scheduled" phases
- **House load greyed out at low values**: overview card always shows actual value at full opacity
- **Reconfigure password whitespace**: `ensure_password_hash` strips trailing newlines from pasted hashes
- **Session recovery `start_soc`**: persisted for accurate progress bars after HA restart

## 1.0.0

### Added
- **Adaptive BMS taper model**: learns actual charge/discharge acceptance at each SoC level via exponential moving average, improving time estimates and power pacing at high/low SoC where BMS limits throughput
- **Full i18n support** (10 languages): English, German, French, Dutch, Spanish, Italian, Polish, Portuguese, Simplified Chinese, Japanese — covering entity names, service descriptions, config UI, Lovelace card labels, durations, and status text
- **Overview Lovelace card** (`custom:foxess-overview-card`): live 2×2 energy flow display with solar, battery, grid, and house nodes, auto-discovered via WebSocket entity map
- **EXAMPLES.md**: quick-start guide with copy-pasteable automations and dashboard setup
- Forecast chart: actual SoC history overlay, locked to configured interval, dashed line for past data
- Discharge progress bar shows energy schedule disparity when feed-in limit is set
- Entity names use HA `translation_key` for native localisation instead of hardcoded English strings
- CI: release gate on lint/test/hassfest/hacs, pre-push hook blocks tags without passing CI

### Changed
- Smart charge/discharge service descriptions now accurately describe rate pacing and deferred start behaviour
- Extracted shared `smart_battery/` library (algorithms, sensors, listeners) for multi-brand reuse
- GoodWe Battery Control moved to its own repository ([goodwe-control](https://github.com/nicois/goodwe-control))
- Refactored Lovelace card progress bar rendering

### Fixed
- SoC progress bar showing `?%` when `start_soc` is unavailable (sessions started before the field existed)
- Dark theme: progress bar tracks and time fills now use HA CSS variables instead of hardcoded rgba
- mypy compatibility between CI and pre-commit environments
