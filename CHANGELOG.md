# Changelog

## 1.0.6-beta.2

### Added
- **Graceful setup retry on cloud outage**: raises `ConfigEntryNotReady` when the FoxESS Cloud is unreachable during startup, so HA retries with exponential backoff instead of failing permanently.
- **PARALLEL_UPDATES = 0**: sensor and binary_sensor platforms declare no parallel updates since all data comes from the coordinator.
- **Repair issues for actionable errors**: unmanaged work mode (C-018) and session aborts now surface in HA's Repairs panel instead of just logs. Issues auto-clear when the problem is resolved or a new session starts.
- **Unrecorded attributes on high-churn sensors**: `SmartOperationsOverviewSensor`, `OverrideStatusSensor`, and `BatteryForecastSensor` mark frequently-changing attributes as unrecorded to prevent database bloat.
- **Clean removal**: `async_remove_entry` deletes the session Store file when the integration is removed entirely, preventing stale data if re-added later.

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
- **Containerised E2E tests** (`e2e/`): real HA instance in Podman container with Playwright browser automation. 20 tests covering card rendering, discharge/charge lifecycle, PV consistency, data source badge (API and WS modes), schedule horizon marker, and screenshot regression. Runs in ~70s with 10 parallel workers.
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
