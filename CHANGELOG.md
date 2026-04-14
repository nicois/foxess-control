# Changelog

## 1.0.1-beta.27

### Fixed
- **WebSocket mixed unit handling**: the FoxESS cloud sends battery power in kW (`unit: "kW"`) but load/solar/grid in watts (`unit: "W"`) within the same message. Now respects the per-field `unit` property instead of assuming all fields use the same unit. Replaces the all-or-nothing heuristic from beta.25 which couldn't handle mixed units.

## 1.0.1-beta.26

### Fixed
- **SoC shows "—" after session ends**: when the REST API is temporarily failing, the coordinator now keeps the last-known data instead of marking all entities unavailable. Previously, WS data masked REST failures during smart sessions, and entities would flash unavailable the moment WS disconnected.

## 1.0.1-beta.25

### Fixed
- **WebSocket kW/watts unit mismatch**: the FoxESS cloud WebSocket sometimes sends power values in kW instead of watts. The integration now auto-detects this (when all raw power values are < 50) and skips the /1000 conversion. Also adds warning-level logging when the mismatch is detected and when WS values diverge >10x from existing coordinator values.

## 1.0.1-beta.24

### Fixed
- **Taper-path consumption bypass (D-007)**: deferred start calculations using the adaptive taper model now account for household consumption, matching the linear path. Previously the taper path ignored consumption, causing charge to start too late and discharge to start too early.
- **Data source badge hidden for API (D-021)**: the data source badge now shows "API" when WebSocket credentials are configured but WS is not active. Previously API was treated as "no badge", leaving users unable to tell whether WS was connected. Single-source (API-only) users continue to see no badge.

## 1.0.1-beta.23

### Fixed
- **WebSocket stale data filter**: messages are now skipped when `timeDiff` exceeds 30 seconds. The first message after connecting is typically 30-200+ seconds old (stale cached data from the cloud); fresh updates have `timeDiff ≈ 5`. Previously the stale first message overwrote valid REST data, causing grid and battery values to briefly show "—" on the overview card.

## 1.0.1-beta.22

### Fixed
- **WebSocket active only when discharge power is paced**: the WebSocket now connects only when discharge power is paced below the inverter maximum (the window where house load could exceed discharge power and cause grid import). At full power there is plenty of headroom and 5-minute REST polling is sufficient. The connection is re-evaluated after each power adjustment so it starts/stops dynamically as pacing changes.

## 1.0.1-beta.21

### Fixed
- **WebSocket connecting before session starts**: with `ws_all_sessions` enabled, the WebSocket now only connects once charging/discharging has actually started, not when the session is merely scheduled or deferred
- **Progress bars shown for scheduled discharge**: the control card no longer renders progress bars during the pre-window "scheduled" phase
- **Data source badge shown for API-only users**: the `WS`/`Modbus` badge is now hidden when the source is plain API (no badge = API)

## 1.0.1-beta.20

### Added
- **Data source indicator on Lovelace cards**: both the overview and control cards now show a small badge (`WS` or `Modbus`) next to the title indicating the current data source. Exposed via a `data_source` attribute on all polled sensor entities.

### Fixed
- **WebSocket power values 1000x too large**: the WebSocket sends values in watts, not kW — restored the /1000 conversion that was incorrectly removed in beta.14. Overview card was showing e.g. "340 kW" house load instead of 340 W.

## 1.0.1-beta.17

### Added
- **WebSocket real-time data**: optional ~5-second real-time power data from the FoxESS Cloud WebSocket during smart sessions, reducing the risk of accidental grid import from load spikes between 5-minute REST polls. Requires FoxESS web portal credentials (configured via a new optional config flow step). Connects automatically during active forced discharge; an opt-in toggle extends coverage to all smart sessions (charge and discharge).
- **End-of-discharge guard**: prevents tail-end grid import when paced discharge power drops below house load near session end
- **Reconfigure flow for web credentials**: add or update FoxESS web portal credentials after initial setup without re-creating the config entry
- **Feed-in energy integration from WebSocket**: trapezoidal integration of instantaneous feed-in power between REST polls for more accurate cumulative energy tracking during discharge

### Fixed
- **WebSocket token URL encoding**: tokens containing `+` and `=` were not URL-encoded, causing the FoxESS server to reject the WebSocket handshake (HTTP 200 instead of 101 Upgrade)
- **WebSocket power values 1000x too small** (beta.14, reverted in beta.20): the /1000 removal was based on incorrect assumptions — the WebSocket does send watts, and the division is required
- **WebSocket grid direction**: replaced unreliable `gridStatus` field with power-balance-derived direction (load + charge - discharge - solar) for correct import/export detection
- **Taper profile corruption from unit mismatch**: the 1000x power error caused taper observations with ~0.1% acceptance ratios, making the behind-schedule detector always fire at max power. Added minimum actual power guard (50W) and plausibility check on profile load to auto-reset corrupted profiles
- **Smart charge finishing early**: consequence of the corrupted taper profile — charge rate was pinned at maximum every tick instead of pacing to the target
- **SoC progress bar showing current SoC**: `start_soc` was missing from initial smart charge session creation (only set during recovery and discharge). Progress bar now correctly shows the SoC at session start.
- **Overview card greying out**: entity map discovery failed during integration reload and cached empty result, never retrying. Now retries every 10 seconds until entities are discovered.
- **Session recovery `start_soc`**: persisted in charge and discharge session serialization so it survives HA restarts; falls back to current SoC for pre-fix sessions

## 1.0.1-beta.4

### Added
- **Peak consumption tracking with safety floor**: tracks highest observed consumption with exponential decay (~21 min half-life), floors discharge power at peak × 1.5 to prevent grid import from inter-poll load spikes
- **Priority-weighted discharge**: strict P1 no-import > P2 min-SoC > P3 energy-target > P4 maximise-feed-in ordering with peak-aware deferred start and suspension

## 1.0.1-beta.3

### Added
- **Deferred self-use for smart discharge**: stays in self-use mode as long as possible, then switches to forced discharge only when a calculated deadline requires it — prevents accidental grid import when paced discharge power would be below house load. Two independent deadlines: SoC target (standard headroom) and feed-in energy target (doubled headroom to account for variable household consumption reducing net export). The Lovelace card shows "Discharge Deferred" during the self-use phase.

### Fixed
- **Discharge power floor at house consumption**: during forced discharge, power is now floored at house load to prevent grid import. Previously, near the end of the window when pacing reduced power below house consumption, the shortfall was drawn from the grid.
- GitHub release page empty for v1.0.0 — release workflow was reading changelog from wrong path

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
