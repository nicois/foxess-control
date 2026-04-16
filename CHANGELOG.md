# Changelog

## 1.0.5-beta.2

### Changed
- **Min SoC floor lowered to 0%**: `min_soc` (smart discharge) and `min_soc_on_grid` (config) now accept 0, removing the previous 5% floor. The FoxESS API minimum (`fdSoc >= 11`) is enforced separately — HA monitors SoC and stops the session at the user's chosen threshold.
- **WebSocket activates during force operations**: force charge, force discharge, and feed-in now activate the WebSocket when `ws_all_sessions` is enabled. Previously only smart sessions triggered WS, so force operations were stuck on 5-minute REST polling.
- **SoC extrapolation between REST polls**: coordinator pushes interpolated SoC updates every 30 seconds between REST polls, so progress bars advance smoothly instead of jumping in integer steps. Automatically stops when WebSocket takes over.

### Added
- **Entity-mode E2E tests**: HA input helpers simulate modbus entities for testing the entity adapter path end-to-end. `connection_mode` fixture parametrizes tests across cloud and entity modes. 34 tests pass (18 skipped cross-product combos).
- **HA WebSocket event stream** (`HAEventStream`): subscribes to `state_changed` events for instant test notifications instead of REST polling. Drain-before-wait pattern eliminates event ordering races.
- **Entity adapter service domain routing**: `input_select` / `input_number` entities now use the correct HA service domain (`input_select.select_option` instead of `select.select_option`). Previously, writes to input helpers failed silently.

### Fixed
- **SoC interpolation clamp on tick change**: when the API reports a new integer SoC (e.g. 90→91), the interpolated value is clamped to [new, new+1) at that instant, preventing the display from briefly showing a value below the authoritative tick. Power integration may drift freely after the clamp.
- **Progress bar start SoC wrong after deferral**: smart charge and smart discharge `start_soc` was captured at service call time; now updated to actual SoC when charging/discharging begins, so the progress bar range is accurate for deferred sessions.

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
