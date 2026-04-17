# Changelog

## 1.0.5-beta.24

### Added
- **Structural tests for coverage gaps**: AST-based `test_cancel_smart_session_is_synchronous` (C-016) verifies cancel functions cannot yield between unsub and state clear. `test_smart_battery_has_no_brand_imports` (C-021) verifies `smart_battery/` never imports from brand-specific packages.
- **Design decisions D-028, D-029**: documented unreachable charge target detection (C-022) and proactive error surfacing mechanism (C-026).

### Changed
- **Coverage matrix regenerated**: full bidirectional trace analysis with new ACCEPTED status for non-actionable gaps. All 4 actionable PARTIAL gaps closed — COVERED rises from 65% to 77%.

### Fixed
- **Smart discharge starting before window**: the deferred discharge listener omitted the `start=` parameter when calling `calculate_discharge_deferred_start`, bypassing the floor clamp that prevents discharge before the window opens. The inverter received a schedule with `fdPwr=0` but ignored the zero and discharged at full power. Now passes the window start time so the clamp keeps deferred mode active until the window begins.

## 1.0.5-beta.22

### Added
- **Structured session logging**: enrich existing log messages with session context (session_id, session_type, SoC, power levels) via a `logging.Filter`. The debug log sensor exposes structured session data in its attributes for E2E tests and power users.
- **Data freshness sensor**: new `sensor.foxess_data_freshness` exposes the current data source (`ws`, `api`, or `modbus`) as its state, with `last_update` (ISO timestamp) and `age_seconds` attributes. Lovelace cards can use `age_seconds` to indicate when data is stale in API-polling mode (up to 5 min lag) vs live in WS/modbus mode (~5s updates).

### Changed
- **WebSocket mode selector**: replaced the boolean `ws_all_sessions` toggle with a 3-state `ws_mode` dropdown: **Auto** (WS only during paced forced discharge — the default), **All smart sessions** (WS during any smart session or force op), and **Always connected** (WS preferred over REST polling at all times). Existing configurations migrate automatically. "Always" mode includes a watchdog that recovers the WS connection after transient failures.

### Fixed
- **Force operations not cancelling opposite smart session**: `force_charge` only cancelled an active `smart_charge`, leaving `smart_discharge` listeners running (and vice versa). The leftover listener would fight the schedule. Both force operations now cancel both smart session types, matching the behaviour smart operations already had.
- **WebSocket not connecting after deferred discharge start**: when a discharge session started in deferred mode (self-use until the deadline), the periodic timer ran the unwrapped callback that didn't trigger `_maybe_start_realtime_ws`. The timer now fires the WS-aware wrapper, so WebSocket connects as soon as forced discharge begins.
- **Session sensors delayed by ~30s after state changes**: `SmartOperationsOverviewSensor` and `OverrideStatusSensor` relied on HA's ~30s poll cycle instead of subscribing to coordinator updates. Now subscribe via `coordinator.async_add_listener` for instant state propagation.

## 1.0.5-beta.20

### Fixed
- **Stale work mode badge after failed cleanup**: when a session aborted due to API errors and the schedule cleanup also failed (same outage), the overview card showed "Force Charge" or "Force Discharge" indefinitely. Now: (1) override removal stores a pending retry on failure, and the coordinator retries on each successful REST poll until the schedule is clean; (2) all session cancel paths use the brand-agnostic `cancel_smart_session` which fires the `_on_session_cancel` hook to clear `_work_mode` immediately.
- **Smart sessions survive transient API errors**: a single "Device offline" or DNS timeout during `apply_mode` no longer aborts the entire charge/discharge session. Errors are retried on the next timer tick; only 3 consecutive failures trigger an abort. Previously, any transient cloud outage (even a few seconds of DNS instability) would kill a multi-hour session.
- **SoC interpolation overshooting entity value**: the interpolated SoC (used by the Lovelace battery icon and progress bar) could exceed the integer tick by more than 0.5%, causing `Math.round()` to display a higher value than `sensor.foxess_battery_soc`. The clamp is now `[tick − 0.5, tick + 0.44]` so the rounded display always matches the entity.

### Changed
- **Eliminated duplicated cancel functions**: `_cancel_smart_charge` and `_cancel_smart_discharge` in `__init__.py` were replaced with delegates to the brand-agnostic `cancel_smart_charge`/`cancel_smart_discharge` from `listeners.py`, ensuring the `_on_session_cancel` hook fires from all cancel paths (clear_overrides, force_charge, force_discharge, smart_charge, smart_discharge, unload).

### Changed
- **E2E config uses production defaults**: removed non-default overrides (`ws_all_sessions`, `min_power_change`, `smart_headroom`) from E2E seed config. Tests that need non-default options now set them explicitly via the options flow, matching real user setup.
- **GitHub Actions updated**: checkout v4→v6, setup-python v5→v6, upload-artifact v4→v7, download-artifact v4→v8. Removed `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` workaround.

## 1.0.5-beta.15

### Fixed
- **REST poll starvation from SoC extrapolation**: the 30-second SoC extrapolation timer called `async_set_updated_data`, which cancels and reschedules the REST poll timer. During any battery activity, the extrapolation fired more frequently than the poll interval, so REST polls never ran and all entities showed stale data indefinitely. Now updates entity data directly without touching the poll timer.
- **Feed-in pacing stuck at initial power**: when target discharge power was below `min_power_change`, the update was silently skipped and the inverter stayed at the initial power level. Now switches to self-use mode when target is below threshold, so the next tick compares against a 0W baseline and can ramp up when the target exceeds the threshold.

### Added
- **Target power display in Lovelace card**: shows current vs target discharge rate when they differ during feed-in pacing.
- **Diagnostic logging**: version and config logged at startup; detailed WS decision path logging for debugging.

## 1.0.5-beta.12

### Fixed
- **WS data_source badge stuck during stale stream**: when another client (FoxESS app) stole the WS stream, the Lovelace badge stayed on "WS" throughout the stale period and reconnect — the coordinator was never notified that useful data had stopped. Now signals the coordinator immediately when reconnecting, so the badge shows "API" until fresh WS data resumes.

## 1.0.5-beta.8

### Changed
- **Min SoC floor lowered to 0%**: `min_soc` and `min_soc_on_grid` now accept 0, removing the previous 5% floor.
- **WebSocket activates during force operations** when `ws_all_sessions` is enabled.
- **SoC extrapolation between REST polls**: 30-second interpolated updates for smooth progress bars.
- **Simulator WS realism**: push loop only sends data to the newest client; older connections receive stale keepalives. Matches FoxESS cloud behaviour where a new app/web login takes over the data stream.

### Added
- **Entity-mode E2E tests**: input helpers simulate modbus entities; `connection_mode` fixture parametrizes cloud vs entity modes.
- **HA WebSocket event stream** (`HAEventStream`): instant state change notifications for E2E tests.
- **Entity adapter service domain routing**: `input_select`/`input_number` entities use correct HA service domain.
- **SoC interpolation regression tests**: 5 tests verify smooth decline, clamp behaviour, and feedin-absent scenarios.
- **E2E function-scoped containers**: each test gets a fresh HA container and simulator, eliminating cross-test state leaks.
- **E2E test: WS stream-stolen recovery**: verifies the integration reconnects when another client (FoxESS app) steals the data stream.

### Fixed
- **WS stream-stolen recovery**: when another client (FoxESS app/website) opens a WebSocket to the FoxESS cloud, the integration's existing connection goes silent — it receives stale keepalive frames but no useful data. Previously the 30s stale timeout never fired because `receive()` returned for each stale frame. Now tracks last useful data timestamp and reconnects when only stale frames arrive for 30s.
- **WS token invalidation on reconnect failure**: the FoxESS cloud revokes the web session token when another client logs in, causing `WSServerHandshakeError` (HTTP 200 instead of 101). The cached token is now invalidated on reconnect failure, forcing a fresh login on the next attempt.
- **SoC interpolation stuck between ticks**: `_ws_last_time` was set before integration (making elapsed=0) and only when feedinPower was present. Moved after integration and made unconditional.
- **SoC clamp rounding**: upper bound 0.99 rounded to next integer (95.99→96.0 displayed as 96%). Changed to 0.94 so displayed value stays within the authoritative tick.
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
