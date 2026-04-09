# FoxESS Control

A Home Assistant custom integration for monitoring and controlling FoxESS inverter battery modes via the FoxESS Cloud API.

FoxESS Control polls real-time inverter data (battery SoC, charge/discharge power, solar generation, house load, temperature) and provides actions for force charge, force discharge, smart charge/discharge with SoC targets, and feed-in management. It can run standalone or alongside the [foxess-ha](https://github.com/macxq/foxess-ha) integration.

## Prerequisites

- A FoxESS inverter connected to FoxESS Cloud
- A FoxESS Cloud API key (generate one at [foxesscloud.com](https://www.foxesscloud.com/) under User Profile > API Management)
- Your inverter's device serial number

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant instance.
2. Go to **Integrations**.
3. Click the three-dot menu in the top right and select **Custom repositories**.
4. Add the repository URL (e.g. `https://github.com/nicois/foxess-control`) with category **Integration**.
5. Click **Add**.
6. Search for "FoxESS Control" in the HACS integrations list and click **Download**.
7. Restart Home Assistant.

### Home Assistant Add-on

1. Go to **Settings > Add-ons > Add-on Store**.
2. Click **⋮ > Repositories** and add `https://github.com/nicois/foxess-control`.
3. Install **FoxESS Control** from the store and start it.
4. Restart Home Assistant.

### Manual

1. Copy the `custom_components/foxess_control` directory into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **FoxESS Control**.
3. Enter your **API Key** and **Device Serial Number**.
4. The integration validates your credentials by querying the FoxESS Cloud API. If successful, the integration is added.

### Options

After setup, click **Configure** on the integration entry to adjust:

| Option | Default | Range | Description |
|---|---|---|---|
| Minimum SoC on Grid | 15% | 5-100% | The minimum battery state of charge to maintain when on grid. Applied to all schedule operations. |
| API Polling Interval | 300 s | 60-600 s | How often to poll the FoxESS Cloud API for real-time data (SoC, power, temperature). Default 5 minutes to coexist with foxess-ha without exceeding API quota. Lower for standalone use. |
| Battery Capacity | 0.0 kWh | 0-100 kWh | Total usable battery capacity in kWh. Required for `smart_charge` power calculations. |
| Min Power Change | 500 W | 0-5000 W | Minimum watt change before updating the charge schedule during `smart_charge`. Lower values improve SoC tracking, higher values reduce API calls. |
| Minimum API fdSoc | 11% | 0-11% | The minimum `fdSoc` value sent to the FoxESS API. The API normally rejects values below 11 (errno 40257). Only lower this if you know your firmware supports it. |

> **Warning:** The inverter's behaviour when it reaches this SoC level during force discharge or feed-in is unintuitive. Consider using an automation to cancel the override before the battery reaches this level. See [Known limitations](#known-limitations).

## Actions

The integration registers six actions (services) under the `foxess_control` domain. These are intended to be called from automations.

### `foxess_control.clear_overrides`

Clears overrides and returns the inverter to self-use mode. If `mode` is specified, only overrides of that mode are removed; other overrides are retained.

If a `smart_charge` or `smart_discharge` session is running, `clear_overrides` also cancels its background listeners — the session stops cleanly without fighting the cleared schedule. Clearing `ForceCharge` cancels smart charge; clearing `ForceDischarge` cancels smart discharge; clearing all overrides (no `mode`) cancels both.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `mode` | No | All | Only clear overrides of this mode (`ForceCharge`, `ForceDischarge`, etc.). |

```yaml
# Clear all overrides (also stops any active smart charge/discharge)
action: foxess_control.clear_overrides
```

```yaml
# Clear only force-charge overrides (also stops smart charge), keeping others
action: foxess_control.clear_overrides
data:
  mode: ForceCharge
```

### `foxess_control.feedin`

Prioritises feeding excess solar to the grid for a specified duration, similar to self-use but with grid export priority.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `duration` | Yes | | How long to feed in. Maximum 4 hours. Must not extend past midnight. |
| `power` | No | Inverter max | Feed-in power limit in watts (min 100). |
| `start_time` | No | Now | Time of day to start the override (e.g. `"14:00:00"`). |
| `replace_conflicts` | No | false | Remove conflicting overrides instead of aborting. |

```yaml
action: foxess_control.feedin
data:
  duration: "02:00:00"
  power: 5000
```

### `foxess_control.force_charge`

Forces the inverter to charge the battery for a specified duration.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `duration` | Yes | | How long to force charge. Maximum 4 hours. Must not extend past midnight. |
| `power` | No | Inverter max | Charge power limit in watts (min 100). |
| `start_time` | No | Now | Time of day to start the override (e.g. `"14:30:00"`). |
| `replace_conflicts` | No | false | Remove conflicting overrides instead of aborting. |

```yaml
action: foxess_control.force_charge
data:
  duration: "01:30:00"
  power: 6000
```

### `foxess_control.force_discharge`

Forces the inverter to discharge the battery for a specified duration.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `duration` | Yes | | How long to force discharge. Maximum 4 hours. Must not extend past midnight. |
| `power` | No | Inverter max | Discharge power limit in watts (min 100). |
| `start_time` | No | Now | Time of day to start the override (e.g. `"17:00:00"`). |
| `replace_conflicts` | No | false | Remove conflicting overrides instead of aborting. |

```yaml
action: foxess_control.force_discharge
data:
  duration: "02:00:00"
  power: 5000
```

### `foxess_control.smart_charge`

Charges the battery within a time window, deferring grid charging as long as possible to maximise the opportunity for solar to contribute. Only starts grid charging when necessary to reach the target SoC by the end of the window.

**How it works:**

1. Calculates the latest possible start time by estimating the effective charge rate — inverter max power minus current household consumption (read from the polled `loadsPower` and `pvPower` data), with a minimum 10% headroom reserved for transient loads. This consumption-aware calculation means the deferral adapts to real-time site conditions rather than using a fixed buffer.
2. **Deferred phase:** Until the calculated start time, no `ForceCharge` schedule is set. The inverter stays in its current mode (typically self-use), allowing solar generation to charge the battery naturally.
3. **Charging phase:** When the deferred start time arrives, sets a `ForceCharge` schedule with `fdSoc` set high (100%) so the inverter never stops charging on its own — HA is the sole authority for stopping. Charge power targets finishing with a 10% time buffer and accounts for current household consumption, so the inverter typically runs at around 80% of capacity rather than 100%.
4. Every 5 minutes, re-reads the current SoC, household consumption, and solar generation, then recalculates. During the deferred phase, if solar has raised the SoC, the start time is pushed later. During the charging phase, power is adjusted up or down based on both the remaining energy deficit and current net consumption. If the power change is below the configured **Min Power Change** threshold, the update is skipped to avoid unnecessary API calls.
5. When the SoC reaches the target for two consecutive readings (whether from solar during the deferred phase or grid charging), the `ForceCharge` group is removed from the schedule, all listeners are cancelled, and the session ends. Requiring two readings prevents a single SoC spike from prematurely ending the session. Other modes' schedule groups (e.g. a standing `ForceDischarge` window) are preserved.
6. When the time window ends, the `ForceCharge` group is removed from the schedule and listeners are cancelled. This prevents the schedule from replaying the next day.

If the battery capacity is too large or the SoC too low to reach the target within the window (accounting for current consumption), charging starts immediately (no deferral).

Only one smart charge session can be active at a time. Starting a new `smart_charge` cancels any previous session, and also cancels any active `smart_discharge` session to prevent schedule conflicts. A `force_charge` action also cancels any running smart charge, since it replaces the underlying `ForceCharge` schedule.

**Stopping a running smart charge:** Call `foxess_control.clear_overrides` (with no mode, or with `mode: ForceCharge`). This removes the schedule **and** cancels the background listeners.

**Requires** Battery Capacity to be configured in the integration options.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `start_time` | Yes | | Time of day to start charging (e.g. `"02:00:00"`). |
| `end_time` | Yes | | Time of day to stop charging (e.g. `"06:00:00"`). Must be after start time, within 4 hours. |
| `target_soc` | Yes | | Charge the battery to this SoC level (11-100%). Charging stops and reverts to self-use when reached. |
| `power` | No | Inverter max | Maximum charge power in watts (min 100). The actual power may be lower to pace charging to the end of the window. |
| `replace_conflicts` | No | false | Remove conflicting overrides instead of aborting. |

```yaml
action: foxess_control.smart_charge
data:
  start_time: "02:00:00"
  end_time: "06:00:00"
  target_soc: 80
  power: 6000
```

### `foxess_control.smart_discharge`

Discharges the battery within a time window and automatically reverts to self-use when the battery reaches a minimum SoC. This replaces the need for a separate automation to monitor SoC and cancel the discharge.

**How it works:**

1. Sets a `ForceDischarge` schedule with `fdSoc` set low (11%) so the inverter never stops discharging on its own — HA is the sole authority for stopping.
2. Monitors the battery SoC periodically. When the SoC drops to the `min_soc` threshold for two consecutive readings, the `ForceDischarge` group is removed from the schedule, all listeners are cancelled, and the session ends. Requiring two readings prevents a single SoC dip from prematurely ending the session. Other modes' schedule groups (e.g. a standing `ForceCharge` window) are preserved.
3. If `feedin_energy_limit_kwh` is set, the cumulative grid feed-in counter is snapshot at the start and compared each interval. When the exported energy reaches the limit, the session ends. This uses the API's lifetime `feedin` counter rather than integrating instantaneous power, so it is accurate across HA restarts.
4. When the time window ends, the `ForceDischarge` group is removed from the schedule and listeners are cancelled. This prevents the schedule from replaying the next day.

The session stops at whichever condition is reached first: time window end, SoC threshold, or feed-in energy limit.

Only one smart discharge session can be active at a time. Starting a new `smart_discharge` cancels any previous session, and also cancels any active `smart_charge` session to prevent schedule conflicts. A `force_discharge` action also cancels any running smart discharge, since it replaces the underlying `ForceDischarge` schedule.

**Stopping a running smart discharge:** Call `foxess_control.clear_overrides` (with no mode, or with `mode: ForceDischarge`). This removes the schedule **and** cancels the background listeners.

Battery SoC is read from the integration's polled data.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `start_time` | Yes | | Time of day to start discharging (e.g. `"17:00:00"`). |
| `end_time` | Yes | | Time of day to stop discharging (e.g. `"20:00:00"`). Must be after start time, within 4 hours. |
| `power` | No | Inverter max | Discharge power limit in watts (min 100). |
| `min_soc` | Yes | | Stop discharging and revert to self-use when the battery reaches this SoC level (11-100%). |
| `feedin_energy_limit_kwh` | No | | Stop discharging after this much energy (kWh) has been fed into the grid. This is the excess energy exported beyond household self-consumption. Uses the cumulative `feedin` counter from the API for accuracy across restarts. |
| `replace_conflicts` | No | false | Remove conflicting overrides instead of aborting. |

```yaml
action: foxess_control.smart_discharge
data:
  start_time: "17:00:00"
  end_time: "20:00:00"
  min_soc: 30
  power: 5000
```

```yaml
# Discharge up to 5 kWh of grid feed-in, then stop
action: foxess_control.smart_discharge
data:
  start_time: "17:00:00"
  end_time: "20:00:00"
  min_soc: 10
  feedin_energy_limit_kwh: 5.0
```

## Sensors

### Polled sensors

The integration polls the FoxESS Cloud API at a configurable interval (default: 5 minutes) and creates the following sensor entities:

| Entity | Description | Unit |
|---|---|---|
| `sensor.foxess_battery_soc` | Battery state of charge | % |
| `sensor.foxess_charge_rate` | Battery charge power | kW |
| `sensor.foxess_discharge_rate` | Battery discharge power | kW |
| `sensor.foxess_house_load` | Current house load | kW |
| `sensor.foxess_solar_power` | Current solar generation | kW |
| `sensor.foxess_residual_energy` | Residual energy in battery | kWh |
| `sensor.foxess_battery_temperature` | Battery temperature | °C |
| `sensor.foxess_grid_consumption` | Power drawn from grid | kW |
| `sensor.foxess_feedin_power` | Power fed to grid | kW |
| `sensor.foxess_generation_power` | Total generation power | kW |
| `sensor.foxess_battery_voltage` | Battery voltage | V |
| `sensor.foxess_battery_current` | Battery current | A |
| `sensor.foxess_pv1_power` | PV string 1 power | kW |
| `sensor.foxess_pv2_power` | PV string 2 power | kW |
| `sensor.foxess_ambient_temperature` | Ambient temperature | °C |
| `sensor.foxess_inverter_temperature` | Inverter temperature | °C |
| `sensor.foxess_grid_feed_in_energy` | Cumulative grid feed-in energy (lifetime) | kWh |
| `sensor.foxess_grid_consumption_energy` | Cumulative grid consumption energy (lifetime) | kWh |
| `sensor.foxess_solar_generation_energy` | Cumulative solar generation energy (lifetime) | kWh |
| `sensor.foxess_battery_charge_energy` | Cumulative battery charge energy (lifetime) | kWh |
| `sensor.foxess_battery_discharge_energy` | Cumulative battery discharge energy (lifetime) | kWh |
| `sensor.foxess_house_load_energy` | Cumulative house load energy (lifetime) | kWh |
| `sensor.foxess_battery_throughput` | Cumulative battery throughput (lifetime) | kWh |
| `sensor.foxess_grid_meter_power` | Grid meter power (signed: negative = exporting) | kW |
| `sensor.foxess_grid_voltage` | Grid voltage | V |
| `sensor.foxess_grid_current` | Grid current | A |
| `sensor.foxess_grid_frequency` | Grid frequency | Hz |
| `sensor.foxess_eps_power` | EPS / backup output power | kW |
| `sensor.foxess_work_mode` | Current inverter work mode (SelfUse, ForceCharge, etc.) | — |

The cumulative energy sensors use `SensorStateClass.TOTAL_INCREASING` and are compatible with Home Assistant's Energy Dashboard.

These sensors update automatically and are always available (not dependent on an active smart operation). They are backed by Home Assistant's `DataUpdateCoordinator`, so all entities update atomically from a single API call. The work mode sensor makes an additional API call per poll cycle to read the active schedule.

### Smart operation sensors

The following sensors track active smart charge/discharge sessions. They are unavailable when no smart operation is active.

#### Overview sensors

| Entity | Description | Example value |
|---|---|---|
| `sensor.foxess_status` | Compact status for Android Auto. Dynamic icon reflects current state. | `Chg 6kW→80%`, `Wait→80%`, `Dchg 5kW→20:00`, `Dchg 5kW 5.0kWh`, `Idle` |
| `sensor.foxess_smart_operations` | Dashboard overview with rich attributes for templating. | `Charging to 80%`, `Deferred charge to 80%`, `Discharging until 20:00`, `Discharging 5.0 kWh feed-in`, `Idle` |

#### Smart charge sensors

| Entity | Description | Example value |
|---|---|---|
| `sensor.foxess_charge_power` | Current charge power in watts. | `6000` |
| `sensor.foxess_charge_window` | Charge time window. | `02:00 – 06:00` |
| `sensor.foxess_charge_remaining` | Estimated time until target SoC is reached, or time until deferred charging begins. | `1h 30m`, `starts in 2h 15m`, `starting` |

#### Smart discharge sensors

| Entity | Description | Example value |
|---|---|---|
| `sensor.foxess_discharge_power` | Current discharge power in watts. | `5000` |
| `sensor.foxess_discharge_window` | Discharge time window. | `17:00 – 20:00` |
| `sensor.foxess_discharge_remaining` | Estimated time until min SoC is reached or the window ends, whichever comes first. | `45m`, `1h 20m` |

#### Battery forecast sensor

| Entity | Description |
|---|---|
| `sensor.foxess_battery_forecast` | Projected battery SoC (%) over time. The `forecast` attribute contains timestamped data points for charting. |

The forecast projects SoC based on the active smart operation:
- **Charging**: SoC rises from current level toward target_soc at the current charge power
- **Deferred charge**: SoC stays flat until the estimated start time, then rises
- **Discharging**: SoC drops from current level toward min_soc at the current discharge power

Requires **Battery Capacity** to be configured in the integration options.

#### ApexCharts example

Use the [apexcharts-card](https://github.com/RomRider/apexcharts-card) custom card to display the forecast on a dashboard:

```yaml
type: custom:apexcharts-card
header:
  title: Battery Forecast
  show: true
graph_span: 6h
yaxis:
  - min: 0
    max: 100
    decimals: 0
    apex_config:
      title:
        text: "SoC %"
series:
  - entity: sensor.foxess_battery_forecast
    data_generator: |
      return entity.attributes.forecast.map(p => [p.time, p.soc]);
    name: Forecast
    type: area
    color: "#4CAF50"
    opacity: 0.3
    stroke_width: 2
```

To overlay the forecast on top of actual SoC history:

```yaml
type: custom:apexcharts-card
header:
  title: Battery SoC
  show: true
graph_span: 12h
span:
  start: day
yaxis:
  - min: 0
    max: 100
    decimals: 0
series:
  - entity: sensor.foxess_battery_soc
    name: Actual
    type: area
    color: "#2196F3"
    opacity: 0.2
    stroke_width: 2
  - entity: sensor.foxess_battery_forecast
    data_generator: |
      return entity.attributes.forecast.map(p => [p.time, p.soc]);
    name: Forecast
    type: line
    color: "#FF9800"
    stroke_width: 2
    stroke_dash: 4
```

### Dashboard card examples

All sensors work directly with the standard **Entities** card — no Jinja templates needed:

```yaml
type: entities
title: Inverter
entities:
  - entity: sensor.foxess_work_mode
  - entity: sensor.foxess_battery_soc
  - entity: sensor.foxess_solar_power
  - entity: sensor.foxess_house_load
  - entity: sensor.foxess_charge_rate
  - entity: sensor.foxess_discharge_rate
  - entity: sensor.foxess_grid_consumption
  - entity: sensor.foxess_feedin_power
  - entity: sensor.foxess_residual_energy
  - entity: sensor.foxess_battery_temperature
```

```yaml
type: entities
title: Smart Operations
entities:
  - entity: sensor.foxess_smart_operations
  - entity: sensor.foxess_charge_power
  - entity: sensor.foxess_charge_window
  - entity: sensor.foxess_charge_remaining
  - entity: sensor.foxess_discharge_power
  - entity: sensor.foxess_discharge_window
  - entity: sensor.foxess_discharge_remaining
```

## Binary sensors

The integration creates two binary sensors that track whether a smart charge or smart discharge session is currently active:

| Entity | State | Attributes when on |
|---|---|---|
| `binary_sensor.foxess_smart_charge_active` | `on` while a smart charge session is running | `target_soc`, `current_power_w`, `max_power_w`, `end_time` |
| `binary_sensor.foxess_smart_discharge_active` | `on` while a smart discharge session is running | `min_soc`, `last_power_w`, `end_time` |

These sensors are useful for:
- Dashboard indicators showing active sessions
- Automation conditions (e.g. suppress other actions while a smart charge is in progress)
- Template sensors that expose session attributes like remaining time or current power

## Automation example

Smart charge during off-peak hours to 80%, then smart discharge during the evening peak down to 30%:

```yaml
automation:
  - alias: "Off-peak smart charge"
    trigger:
      - platform: time
        at: "02:00:00"
    action:
      - action: foxess_control.smart_charge
        data:
          start_time: "02:00:00"
          end_time: "06:00:00"
          target_soc: 80

  - alias: "Evening peak smart discharge"
    trigger:
      - platform: time
        at: "17:00:00"
    action:
      - action: foxess_control.smart_discharge
        data:
          start_time: "17:00:00"
          end_time: "20:00:00"
          min_soc: 30
```

## How it works

- Force charge/discharge actions write a time-windowed override to the inverter's scheduler via the FoxESS Cloud API. Outside scheduled windows, the inverter defaults to self-use mode.
- Each force action only replaces existing overrides of the **same mode** (e.g. force charge replaces previous force charge windows, but leaves force discharge windows intact). Overrides of a different mode are always preserved, even if their time window has already passed today — this allows standing daily schedules (e.g. a free-electricity charge window) to coexist with evening discharge overrides.
- If the new window would overlap with an existing override of a **different** mode, the action aborts with an error to prevent conflicts.
- When a smart action ends (SoC target reached, time window expired), it removes **only its own mode's groups** from the schedule. Other modes' groups are preserved. For example, a smart discharge ending does not remove a standing `ForceCharge` window. If no groups remain after removal, the schedule reverts to self-use.
- Smart sessions are persisted to Home Assistant's `.storage` directory. If HA restarts during an active session, it is automatically resumed if still within its time window, or cleaned up (schedule groups removed) if expired. Sessions from a previous day are discarded.
- The API client throttles requests (minimum 5 seconds between calls) and retries with exponential backoff on rate limits.

## Known limitations

- **Minimum SoC behaviour is unintuitive**: When the battery reaches the minimum SoC during force discharge or feed-in, the inverter's behaviour may not match expectations. Smart actions work around this by setting `fdSoc` to an extreme value (100% for charge, 11% for discharge) so the inverter never triggers its own threshold — HA monitors SoC and stops the action at the user's configured target. For plain `force_charge`/`force_discharge`, consider using an automation to cancel the override before the battery reaches the minimum SoC level.

- **Schedule race condition**: Force charge/discharge actions read the current schedule, modify it, then write it back. If the schedule is changed between the read and write (e.g. via the FoxESS app), those changes will be overwritten. Enable debug logging for `foxess_control` to see before/after state if schedules change unexpectedly.
- **FoxESS Cloud API latency**: All commands go through the FoxESS Cloud API, which throttles requests to one every 5 seconds. Actions are not instantaneous. For faster local control, consider modbus-based integrations.
- **FoxESS mode scheduler bugs**: The FoxESS Cloud API has known issues with schedule validation (e.g. rejecting its own saved schedules due to overlap detection on disabled groups). This integration works around known issues, but the API may introduce new ones.

## Compatibility with foxess-ha

This integration uses its own config entry and does not read configuration from the foxess-ha sensor integration. You will need to enter your API key and serial number separately. Both integrations can run side-by-side without conflict.

With the built-in polled sensors, FoxESS Control can operate standalone without foxess-ha. If you run both integrations, set the polling interval to 300 seconds (the default) to avoid exceeding the FoxESS API quota. If you remove foxess-ha, you can lower the polling interval for faster updates.

## Support

If you find this integration useful, consider buying me a coffee:

[![Donate](https://img.shields.io/badge/Donate-PayPal-blue.svg)](https://www.paypal.com/donate/?hosted_button_id=3NEP4LZAHLH6W)

## License

MIT
