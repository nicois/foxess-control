# Changelog

## 0.13.2-beta.6

- Add entity-based backend for foxess_modbus interop: optionally read inverter state from and write mode changes to foxess_modbus entities instead of the FoxESS Cloud API
- New config options: Work Mode Entity, Charge/Discharge Power Entity, Min SoC Entity, SoC Entity, Loads Power Entity, PV Power Entity, Feed-in Energy Entity
- All smart charge/discharge algorithms work in entity mode without a cloud API connection
- Add FoxESSEntityCoordinator for reading inverter state from HA entity states

## 0.13.2-beta.5

- Battery forecast accounts for discharge energy limit (SoC flattens when limit reached)

## 0.13.2-beta.4

- Show 0W discharge power before window opens
- Smart operations shows "Discharge scheduled at" before window opens
- Smart operations attributes use proper remaining estimates
- Android Auto shows "Dchg@HH:MM" before discharge starts

## 0.13.2-beta.3

- Discharge remaining shows "starts in" before window opens
- Discharge remaining shows kWh left when energy limit is closer than time window
- Update README for discharge remaining sensor description

## 0.13.2-beta.2

- Fix ApexCharts crash when no smart operation active (forecast attribute always present)
- Update README for charge remaining and target SoC confirmation behavior

## 0.13.2-beta.1

- Remove ForceCharge override immediately when target SoC reached (confirm before ending session)
- Fix charge remaining estimate using window end time instead of inflated power-based calculation

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
