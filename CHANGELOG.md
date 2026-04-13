# Changelog

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
