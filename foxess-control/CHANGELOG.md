# Changelog

## 0.13.4

- Configurable smart charge headroom (default 10%, max 25%) — controls time buffer and power multiplier for deferred charge calculations
- Add `http` and `lovelace` dependencies to manifest (fixes hassfest validation)

## 0.13.3

- **Custom Lovelace card:** built-in card with battery gauge, smart charge/discharge status, progress indicators, and SVG forecast sparkline — zero-config, auto-discovers entities
- Card auto-registers as Lovelace resource with versioned cache-busting URL
- Sensors grouped under a single FoxESS Inverter device instead of generic "Sensors"
- Forecast sparkline with time axis labels and dynamic Y-axis scaling to fit data range
- Smart discharge: replace power tapering with early-stop timer based on observed export rate extrapolation
- Clamp deferred charge start time to the configured window opening (prevents "starts in" showing a time before the window)
- Refuse to modify schedule when unmanaged work mode detected (e.g. manual changes via FoxESS app)
- Fix API error 40257 by clamping fdSoc in `_sanitize_group`
- Improved diagnostic context in exception and debug logging
- Clarify self-use fallback in force charge/discharge service descriptions
- Comprehensive README updates: attribute documentation, replace_conflicts explanation, session recovery notes, custom card documentation

## 0.13.2

- **Entity mode (foxess_modbus interop):** optionally read inverter state from and write mode changes to foxess_modbus entities instead of the FoxESS Cloud API — fully cloud-free, no API key required
- Auto-detect foxess_modbus entities from the entity registry; entity mapping step hidden when foxess_modbus is not installed
- New config options: Work Mode Entity, Charge/Discharge Power Entity, Min SoC Entity, SoC Entity, Loads Power Entity, PV Power Entity, Feed-in Energy Entity, Inverter Rated Power
- Default polling interval is 30s in entity mode (vs 300s cloud)
- Smart session recovery works without cloud schedule validation in entity mode
- Battery forecast accounts for discharge energy limit (SoC flattens when limit reached)
- Discharge sensors show "starts in" / "scheduled at" before window opens, kWh left when energy limit is closer than time window
- Smart operations attributes use proper remaining estimates
- Android Auto shows "Dchg@HH:MM" before discharge starts, 0W discharge power before window opens
- Remove ForceCharge override immediately when target SoC reached (confirm before ending session)
- Fix charge remaining estimate using window end time instead of inflated power-based calculation
- Fix ApexCharts crash when no smart operation active (forecast attribute always present)

## 0.13.1

- Fix smart session recovery: rebuild schedule groups after HA restart
- Fix smart sessions lost on config entry options reload
- Fix charge power entity showing max power before charging begins on session recovery
- Fix race conditions in async charge/discharge callbacks
- Harden binary sensor attributes against incomplete state dicts
- Persist session state after power adjustments and feedin baseline capture
- 10% charge power headroom to absorb unexpected household load
- 10% time buffer on deferred charge start for household load volatility
- Smart charge/discharge cross-cancellation (starting one cancels the other)
- Allow max power changes to bypass minimum power change threshold
- Deduplicate flat segments in battery forecast time series
- Downgrade first transient API retry to debug logging (warn only on repeated retries)
- Downgrade unchanged charge power log to debug level
- Warn when start_time is in the past
- Warn when multiple config entries detected (single-inverter limitation)
- Updated dashboard card examples with visibility-conditional cards
- Documentation updates for new parameters, sensors, and features

## 0.13.0

- Feed-in energy limit option for smart discharge (using cumulative API counter)
- 12 new polled sensors: cumulative energy counters (feed-in, grid consumption, generation, charge/discharge totals, loads, throughput), grid connection (meter power, voltage, current, frequency), EPS power
- Improved discharge status messages to reflect actual stop conditions
- Smart charge/discharge cross-cancellation (starting one cancels the other)
- Session persistence after power adjustments and feedin baseline capture
- Hardened callbacks against concurrent cancellation race conditions
- Warning when start_time is in the past
- Warning when multiple config entries detected (single-inverter limitation)
- Missing polled variables logged at debug level

## 0.12.1

- Fix battery forecast to defer discharge projection until start time
- Add-on release with AppArmor and cosign signing

## 0.12.1-beta.2

- Add cosign keyless image signing to add-on CI
- Add AppArmor security profile for add-on

## 0.12.1-beta.1

- Fix battery forecast to defer discharge projection until start time
- Initial add-on release

## 0.12.0

- Consumption-aware smart charge (factors in household load and solar generation)
- 10% time headroom in charge power calculation
- SoC stability counters (2 consecutive readings before cancelling sessions)
- Robust session recovery with corrupted data handling
- Expanded polled sensors (grid, PV, battery voltage/current, temperatures)
- Work mode sensor
- SoC fallback to coordinator data
