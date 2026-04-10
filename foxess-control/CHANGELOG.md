# Changelog

## 0.13.1-beta.4

- Update dashboard card examples with visibility-conditional charge/discharge cards

## 0.13.1-beta.3

- Fix charge power entity showing max power before charging begins on session recovery

## 0.13.1-beta.2

- Harden binary sensor attributes against incomplete state dicts
- Fix race conditions in async charge/discharge callbacks
- Persist session state after power adjustments and feedin baseline capture
- Warn when start_time is in the past
- Smart charge/discharge cross-cancellation (starting one cancels the other)
- Warn when multiple config entries detected (single-inverter limitation)
- Log missing polled variables at debug level
- Documentation updates for new parameters, sensors, and features

## 0.13.1-beta.1

- Fix smart session recovery: rebuild schedule groups after HA restart
- Deduplicate flat segments in battery forecast time series
- Allow max power changes to bypass minimum power change threshold
- Debug logging for discharge stop conditions

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
