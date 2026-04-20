# FoxESS Control

A Home Assistant custom integration for monitoring and controlling FoxESS inverter battery modes.

FoxESS Control polls real-time inverter data (battery SoC, charge/discharge power, solar generation, house load, temperature) and provides actions for force charge, force discharge, smart charge/discharge with SoC targets, and feed-in management. It supports two backends: the **FoxESS Cloud API** and **local entity mode** via [foxess_modbus](https://github.com/nathanmarlor/foxess_modbus). It includes comprehensive polled sensors and can fully replace the [foxess-ha](https://github.com/macxq/foxess-ha) integration — see [Migrating from foxess-ha](#migrating-from-foxess-ha).

## Gallery
### A dashboard overview card

| English | 简体中文 |
| ------- | ------- |
| <img width="512" height="332" alt="image" src="https://github.com/user-attachments/assets/d31eda08-ca11-49df-8163-989c6a8271f3" /> | <img width="517" height="335" alt="image" src="https://github.com/user-attachments/assets/cddc1941-bd21-44b5-ba33-c0dbd8c6cad8" /> |

### Smart charge: optimally ensure SoC is reached at a given time
<img width="508" height="891" alt="image" src="https://github.com/user-attachments/assets/16f47ed2-2656-4ec8-92ec-40474f852d62" />

### Smart discharge: defer forced discharge as long as possible, then discharge to meet SoC and feed-in targets by the end of the window
<img width="510" height="1038" alt="image" src="https://github.com/user-attachments/assets/471f6ce7-55dc-4a84-972d-83b8b58775ea" />


### A dashboard card showing the state of the current smart operation
| Before | During |
| ------ | ------ |
| Before a smart charge or discharge operations begins, a countdown is shown along with a few details | Despite the inverter being capable of 5kW export, the smart discharge operation lowers the export rate to spread out the 3kW discharge over the discharge period |
| <img width="475" height="377" alt="image" src="https://github.com/user-attachments/assets/997135c5-cbcf-4a4f-b223-b102564a3c1f" /> | <img width="477" height="407" alt="image" src="https://github.com/user-attachments/assets/ab05c800-cace-48a8-993d-f57be56f6768" /> |




## Prerequisites

- A FoxESS inverter connected to FoxESS Cloud
- A FoxESS Cloud API key (generate one at [foxesscloud.com](https://www.foxesscloud.com/) under User Profile > API Management)
- Your inverter's device serial number

> **Note:** If you use [foxess_modbus](https://github.com/nathanmarlor/foxess_modbus), the cloud API key is optional. See [Entity mode](#entity-mode-foxess_modbus-interop) for details.

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

### Web credentials (optional)

After entering your API key and serial, an optional second step allows you to provide your **FoxESS Cloud web portal** username and password (the same credentials you use to log in at [foxesscloud.com](https://www.foxesscloud.com/)). These enable the real-time WebSocket data feature (see below). You can skip this step and add credentials later via **Configure > Reconfigure**.

The web portal API uses an obfuscated signature algorithm (shipped as a WebAssembly module) for request authentication. See [docs/wasm-signature.md](docs/wasm-signature.md) for a full explanation of why this is necessary and how it works.

You can enter either your raw password or its MD5 hash. If you prefer not to type your password into the HA UI, generate the hash beforehand:

```bash
echo -n 'YourPassword' | md5sum | cut -d' ' -f1
```

> **Important:** Use `echo -n` (no trailing newline). Plain `echo` adds a newline which produces a different hash.

### Real-time WebSocket data

When web credentials are configured, the integration can connect to an undocumented FoxESS Cloud WebSocket that streams inverter data every ~5 seconds (vs the standard 5-minute REST API polls). This is used during smart sessions to detect and react to load spikes faster, reducing the risk of accidental grid import.

The **WebSocket real-time data mode** option controls when the WebSocket is used:

| Mode | Behaviour |
|---|---|
| **Auto** (default) | WS connects only during active forced discharge — the highest-risk window where a load spike between polls can cause grid import. |
| **All smart sessions** | WS also connects during smart charge and deferred discharge phases, so the overview card updates every ~5 seconds during any smart operation. |
| **Always connected** | WS connects at startup and stays connected regardless of session state. Useful for dashboards showing real-time power flow. Includes a watchdog that recovers the connection after transient failures. |

The WebSocket is best-effort: if the connection fails, the integration falls back to standard REST polling with no loss of functionality. Entity mode users (foxess_modbus) already have fast local data and are unaffected.

### Options

After setup, click **Configure** on the integration entry to adjust:

| Option | Default | Range | Description |
|---|---|---|---|
| Minimum SoC on Grid | 15% | 0-100% | The minimum battery state of charge to maintain when on grid. Applied to all schedule operations. |
| API Polling Interval | 300 s (cloud) / 30 s (entity mode) | 60-600 s | How often to poll for real-time data. In cloud mode, defaults to 5 minutes to stay within the FoxESS API quota. In entity mode, defaults to 30 seconds since reads are local. |
| Battery Capacity | 0.0 kWh | 0-100 kWh | Total usable battery capacity in kWh. Required for `smart_charge` power calculations. |
| Min Power Change | 500 W | 0-5000 W | Minimum watt change before updating the charge schedule during `smart_charge`. Lower values improve SoC tracking, higher values reduce API calls. |
| Minimum API fdSoc | 11% | 0-11% | The minimum `fdSoc` value sent to the FoxESS API. The API normally rejects values below 11 (errno 40257). Only lower this if you know your firmware supports it. |
| Smart Headroom | 10% | 0-25% | Spare capacity reserved during `smart_charge` and `smart_discharge` for transient load variation. Applied as both a time buffer (plan to finish in 90% of the window) and a power multiplier (request 110% of the calculated rate). For smart charge, lower values charge more slowly and defer longer; higher values start earlier and charge faster. For smart discharge, the headroom determines how long the deferred self-use phase lasts — lower values defer longer (start forced discharge later), higher values start earlier. When a feed-in energy limit is set, the headroom is doubled (up to 40%) to account for household consumption reducing the effective export rate. Set to 0 for no headroom (not recommended — transient loads may prevent reaching the target). |
| WebSocket real-time data mode | Auto | Auto / All smart sessions / Always connected | When web credentials are configured, controls when the real-time WebSocket is used. See [Real-time WebSocket data](#real-time-websocket-data). |

> **Warning:** The inverter's behaviour when it reaches this SoC level during force discharge or feed-in is unintuitive. Consider using an automation to cancel the override before the battery reaches this level. See [Known limitations](#known-limitations).

### Entity mode (foxess_modbus interop)

If you use [foxess_modbus](https://github.com/nathanmarlor/foxess_modbus) for local Modbus control, foxess_control can optionally read inverter state from and write mode changes to foxess_modbus's HA entities instead of the FoxESS Cloud API. This gives you fast local control combined with foxess_control's smart charge/discharge algorithms — no cloud API connection required.

**Setup:** When foxess_modbus is installed, the options flow automatically shows a second step with entity mappings. Entities are **auto-detected** from the foxess_modbus entity registry and pre-populated — in most cases no manual configuration is needed. You can override any mapping if the auto-detection picks the wrong entity.

If foxess_modbus is not installed, the entity mapping step is hidden entirely.

| Option | Domain | Required | Description |
|---|---|---|---|
| Work Mode Entity | `select` | Yes | foxess_modbus work mode select entity. Setting this enables entity mode. |
| Charge Power Entity | `number` | For smart charge | foxess_modbus charge power number entity. |
| Discharge Power Entity | `number` | For smart discharge | foxess_modbus discharge power number entity. |
| Min SoC Entity | `number` | No | foxess_modbus min SoC number entity. |
| SoC Entity | `sensor` | No | Battery SoC sensor (overrides cloud polling). |
| Loads Power Entity | `sensor` | No | House load sensor (improves consumption-aware charging). |
| PV Power Entity | `sensor` | No | Solar generation sensor (improves charge deferral). |
| Feed-in Energy Entity | `sensor` | No | Cumulative feed-in energy sensor (for discharge energy limits). |
| Inverter Rated Power | — | No | Inverter's maximum power in watts (default 12000). Used as the default power limit when no explicit power is specified in actions. In cloud mode this is queried from the API automatically. |

When entity mode is active:

- **No cloud connection required.** The FoxESS Cloud API key is not needed. If provided, it is unused while entity mode is active.
- **Reads** come from HA entity states (polled every 30 seconds by default) instead of the FoxESS Cloud API.
- **Writes** use `select.select_option` and `number.set_value` service calls to foxess_modbus entities instead of the cloud API scheduler.
- All actions (`force_charge`, `smart_charge`, `smart_discharge`, `feedin`, `clear_overrides`) work identically — only the underlying transport changes.
- Schedule merging and multi-window management are not used; foxess_control sets the mode directly.
- Smart session recovery after HA restart works without checking cloud schedule state.

## Actions

The integration registers six actions (services) under the `foxess_control` domain. These are intended to be called from automations.

Most actions accept a `replace_conflicts` parameter. In cloud mode, the inverter's schedule can hold multiple time-windowed override groups (e.g. a `ForceCharge` window and a `ForceDischarge` window). If a new override's time window overlaps with an existing override of a **different** mode, the action aborts by default to prevent conflicts. Setting `replace_conflicts: true` silently removes the overlapping overrides instead. In entity mode (no multi-window schedule), this parameter has no effect.

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

Prioritises feeding excess solar to the grid for a specified duration, similar to self-use but with grid export priority. The inverter automatically reverts to self-use when the window ends. Does not cancel running smart charge/discharge sessions.

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

Forces the inverter to charge the battery for a specified duration. The inverter automatically reverts to self-use when the window ends. Cancels any running `smart_charge` session, since it replaces the underlying `ForceCharge` schedule.

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

Forces the inverter to discharge the battery for a specified duration. The inverter automatically reverts to self-use when the window ends. Cancels any running `smart_discharge` session, since it replaces the underlying `ForceDischarge` schedule.

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

1. Calculates the latest possible start time by estimating the effective charge rate — inverter max power minus current household consumption (read from the polled `loadsPower` and `pvPower` data), with a minimum headroom (configurable via **Smart Headroom**, default 10%) reserved for transient loads. This consumption-aware calculation means the deferral adapts to real-time site conditions rather than using a fixed buffer.
2. **Deferred phase:** Until the calculated start time, no `ForceCharge` schedule is set. The inverter stays in its current mode (typically self-use), allowing solar generation to charge the battery naturally.
3. **Charging phase:** When the deferred start time arrives, sets a `ForceCharge` schedule with `fdSoc` set high (100%) so the inverter never stops charging on its own — HA is the sole authority for stopping. Charge power targets finishing within the configured headroom buffer and accounts for current household consumption, so the inverter typically runs below full capacity.
4. Every 5 minutes, re-reads the current SoC, household consumption, and solar generation, then recalculates. When available, the inverter's `ResidualEnergy` sensor (direct kWh measurement) is used for higher precision than integer SoC% × capacity. During the deferred phase, if solar has raised the SoC, the start time is pushed later. During the charging phase, power is adjusted up or down based on both the remaining energy deficit and current net consumption. If the power change is below the configured **Min Power Change** threshold, the update is skipped to avoid unnecessary API calls. If the actual energy stored is significantly behind the ideal headroom-adjusted trajectory (a linear ramp from the starting energy to the target, completing within the headroom-shortened window), the charge rate temporarily jumps to full power until the trajectory is regained. The deficit must exceed a tolerance derived from the **Min Power Change** setting to avoid premature bursting from minor measurement fluctuations.
5. When the SoC reaches the target (whether from solar during the deferred phase or grid charging), the `ForceCharge` group is removed from the schedule immediately to stop unnecessary charging. The session continues monitoring for one more reading: if the SoC is confirmed at or above target, the session ends; if it drops back below, the charge override is re-applied. This prevents a single SoC spike from prematurely ending the session while avoiding overcharging during the confirmation period. Other modes' schedule groups (e.g. a standing `ForceDischarge` window) are preserved.
6. When the time window ends, the `ForceCharge` group is removed from the schedule and listeners are cancelled. This prevents the schedule from replaying the next day.

If the battery capacity is too large or the SoC too low to reach the target within the window (accounting for current consumption), charging starts immediately (no deferral).

When the BMS battery temperature is below 16°C, the maximum charge power is automatically capped at 80A × live battery voltage (~4 kW at 50 V) to match the BMS's physical current limit. This prevents over-requesting, which causes the inverter to oscillate. The effective limit is exposed via the `charge_effective_max_power_w` sensor attribute.

Only one smart charge session can be active at a time. Starting a new `smart_charge` cancels any previous session, and also cancels any active `smart_discharge` session to prevent schedule conflicts. A `force_charge` action also cancels any running smart charge, since it replaces the underlying `ForceCharge` schedule.

**Stopping a running smart charge:** Call `foxess_control.clear_overrides` (with no mode, or with `mode: ForceCharge`). This removes the schedule **and** cancels the background listeners.

**HA restart:** Smart charge sessions are persisted to `.storage`. If HA restarts mid-session, the session is automatically resumed if still within the time window, or cleaned up if expired. You do not need to re-trigger the automation.

**Requires** Battery Capacity to be configured in the integration options.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `start_time` | Yes | | Time of day to start charging (e.g. `"02:00:00"`). |
| `end_time` | Yes | | Time of day to stop charging (e.g. `"06:00:00"`). Must be after start time, within 4 hours. |
| `target_soc` | Yes | | Charge the battery to this SoC level (5-100%). Charging stops and reverts to self-use when reached. |
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

Discharges the battery within a time window, deferring forced discharge as long as possible to keep the battery serving household load naturally. Only switches to forced discharge when necessary to meet the SoC or feed-in target by the end of the window.

**How it works:**

1. **Deferred phase** (requires `battery_capacity_kwh` in options): Calculates the latest possible start time for forced discharge, considering two independent deadlines — (a) the time needed at full power to drain from the current SoC to `min_soc`, and (b) if `feedin_energy_limit_kwh` is set, the time needed to export the required energy (with doubled headroom, since household consumption reduces the effective export rate). The inverter stays in self-use during this phase, so the battery naturally serves household load without grid export. This prevents accidental grid import that can occur when paced discharge power is set below the house load. Without `battery_capacity_kwh` configured, forced discharge starts immediately.
2. **Discharging phase:** When the deferred deadline arrives, sets a `ForceDischarge` schedule with `fdSoc` set low (11%) so the inverter never stops discharging on its own — HA is the sole authority for stopping. The initial discharge power is calculated based on the remaining energy to discharge and time remaining, then re-evaluated every 5 minutes. Power is adjusted if the change exceeds the Minimum Power Change threshold. When `feedin_energy_limit_kwh` is set, pacing factors in the remaining export budget so the feed-in limit is not exhausted early. When a `power` value is provided, it acts as a ceiling — pacing still operates but never exceeds the specified limit. When available, the inverter's `ResidualEnergy` sensor (direct kWh measurement) is used for higher precision than integer SoC% × capacity.
3. Monitors the battery SoC periodically. When the SoC drops to the `min_soc` threshold for two consecutive readings, the `ForceDischarge` group is removed from the schedule, all listeners are cancelled, and the session ends. Requiring two readings prevents a single SoC dip from prematurely ending the session. Other modes' schedule groups (e.g. a standing `ForceCharge` window) are preserved.
4. If `feedin_energy_limit_kwh` is set, the cumulative grid feed-in counter is snapshot at the start and compared each interval. When the exported energy reaches the limit, the session ends. This uses the API's lifetime `feedin` counter rather than integrating instantaneous power, so it is accurate across HA restarts.
5. When the time window ends, the `ForceDischarge` group is removed from the schedule and listeners are cancelled. This prevents the schedule from replaying the next day.

The session stops at whichever condition is reached first: time window end, SoC threshold, or feed-in energy limit.

Only one smart discharge session can be active at a time. Starting a new `smart_discharge` cancels any previous session, and also cancels any active `smart_charge` session to prevent schedule conflicts. A `force_discharge` action also cancels any running smart discharge, since it replaces the underlying `ForceDischarge` schedule.

**Stopping a running smart discharge:** Call `foxess_control.clear_overrides` (with no mode, or with `mode: ForceDischarge`). This removes the schedule **and** cancels the background listeners.

**HA restart:** Smart discharge sessions are persisted to `.storage`. If HA restarts mid-session, the session is automatically resumed if still within the time window, or cleaned up if expired.

Battery SoC is read from the integration's polled data.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `start_time` | Yes | | Time of day to start discharging (e.g. `"17:00:00"`). |
| `end_time` | Yes | | Time of day to stop discharging (e.g. `"20:00:00"`). Must be after start time, within 4 hours. |
| `power` | No | Inverter max | Discharge power limit in watts (min 100). |
| `min_soc` | Yes | | Stop discharging and revert to self-use when the battery reaches this SoC level (0-100%). |
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

The integration polls inverter data at a configurable interval and creates the following sensor entities. In cloud mode, data comes from the FoxESS Cloud API (default: 5 minutes). In entity mode, data comes from foxess_modbus HA entities (default: 30 seconds) — only sensors with a mapped entity are available.

| Entity | Description | Unit |
|---|---|---|
| `sensor.foxess_battery_soc` | Battery state of charge | % |
| `sensor.foxess_charge_rate` | Battery charge power | kW |
| `sensor.foxess_discharge_rate` | Battery discharge power | kW |
| `sensor.foxess_house_load` | Current house load | kW |
| `sensor.foxess_solar_power` | Current solar generation | kW |
| `sensor.foxess_residual_energy` | Residual energy in battery | kWh |
| `sensor.foxess_battery_temperature` | Battery temperature (inverter sensor) | °C |
| `sensor.foxess_bms_battery_temperature` | BMS cell temperature (web portal) | °C |
| `sensor.foxess_grid_consumption` | Power drawn from grid | kW |
| `sensor.foxess_grid_feed_in` | Power fed to grid | kW |
| `sensor.foxess_generation` | Total generation power | kW |
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
| `sensor.foxess_data_freshness` | Current data source (`ws`, `api`, or `modbus`) with `last_update` and `age_seconds` attributes | — |

The cumulative energy sensors use `SensorStateClass.TOTAL_INCREASING` and are compatible with Home Assistant's Energy Dashboard.

These sensors update automatically and are always available (not dependent on an active smart operation). They are backed by Home Assistant's `DataUpdateCoordinator`, so all entities update atomically from a single poll. In cloud mode, the work mode sensor makes an additional API call per poll cycle to read the active schedule. In entity mode, work mode is read from the mapped select entity.

> **Note:** Diagnostic-only sensors (temperatures, voltages, currents, grid frequency, EPS, throughput) are categorised as `DIAGNOSTIC` so they don't clutter default dashboards. Rarely-used sensors (PV1/PV2, battery voltage/current, ambient/inverter temp, grid current/frequency, EPS, throughput) are disabled by default — enable them in Settings > Devices > Entities as needed.

### Debug log sensors

| Entity | Description |
|---|---|
| `sensor.foxess_debug_log` | Rolling buffer of the last 75 debug log messages. Enabled via `input_boolean.foxess_debug_log`. |
| `sensor.foxess_init_debug_log` | Non-wrapping buffer preserving the first 75 log messages after startup. |
| `sensor.foxess_info_log` | Rolling buffer of the last 75 INFO+ messages, retaining operational context longer than the debug log. |

### Smart operation sensors

The following sensors track active smart charge/discharge sessions. They are unavailable when no smart operation is active.

#### Overview sensors

| Entity | Description | Example value |
|---|---|---|
| `sensor.foxess_status` | Compact status for Android Auto. Dynamic icon reflects current state. | `Chg 6kW→80%`, `Wait→80%`, `Dchg@18:00`, `Dchg 5kW→20:00`, `Dchg 5kW 5.0kWh`, `Idle` |
| `sensor.foxess_smart_operations` | Dashboard overview with rich attributes for templating (see below). | `Charging to 80%`, `Deferred charge to 80%`, `Discharge scheduled at 18:00`, `Discharging until 20:00`, `Discharging 5.0 kWh feed-in`, `Idle` |

**`sensor.foxess_smart_operations` attributes:**

Always present:

| Attribute | Type | Description |
|---|---|---|
| `charge_active` | bool | Whether a smart charge session is running. |
| `discharge_active` | bool | Whether a smart discharge session is running. |

When `charge_active` is true:

| Attribute | Type | Description |
|---|---|---|
| `charge_phase` | string | `"charging"` or `"deferred"`. |
| `charge_power_w` | int | Current charge power in watts. |
| `charge_max_power_w` | int | Configured maximum charge power. |
| `charge_effective_max_power_w` | int | Effective max power after cold-temperature curtailment. |
| `charge_target_soc` | int | Target SoC percentage. |
| `charge_current_soc` | float | Current battery SoC. |
| `charge_window` | string | Time window (e.g. `"02:00 – 06:00"`). |
| `charge_remaining` | string | Time remaining or deferred status (e.g. `"1h 30m"`, `"starts in 2h 15m"`). |
| `charge_end_time` | string | End time in ISO format. |
| `charge_target_reachable` | bool | `false` when even max power can't reach the target SoC in remaining time. |

When `discharge_active` is true:

| Attribute | Type | Description |
|---|---|---|
| `discharge_phase` | string | `"discharging"` or `"deferred"`. |
| `discharge_power_w` | int | Current discharge power in watts. |
| `discharge_target_power_w` | int | Target discharge power before feed-in pacing adjustments. |
| `discharge_min_soc` | int | Minimum SoC threshold. |
| `discharge_current_soc` | float | Current battery SoC. |
| `discharge_window` | string | Time window (e.g. `"17:00 – 20:00"`). |
| `discharge_remaining` | string | Time remaining or status (e.g. `"45m"`, `"1.0 kWh left"`). |
| `discharge_end_time` | string | End time in ISO format. |

When any session is active, error state attributes are also available:

| Attribute | Type | Description |
|---|---|---|
| `has_error` | bool | `true` when the session has encountered errors. |
| `last_error` | string | Most recent error message. |
| `last_error_at` | string | Timestamp of the most recent error. |
| `error_count` | int | Number of consecutive errors (circuit breaker opens at 3). |

#### Smart charge sensors

| Entity | Description | Example value |
|---|---|---|
| `sensor.foxess_charge_power` | Current charge power in watts. | `6000` |
| `sensor.foxess_charge_window` | Charge time window. | `02:00 – 06:00` |
| `sensor.foxess_charge_remaining` | Time remaining in the charge window, or time until deferred charging begins. | `1h 30m`, `starts in 2h 15m`, `starting` |

#### Smart discharge sensors

| Entity | Description | Example value |
|---|---|---|
| `sensor.foxess_discharge_power` | Current discharge power in watts. | `5000` |
| `sensor.foxess_discharge_window` | Discharge time window. | `17:00 – 20:00` |
| `sensor.foxess_discharge_remaining` | Time remaining in the discharge window, energy remaining if energy limit is closer, or time until discharge begins. | `45m`, `1h 20m`, `1.0 kWh left`, `starts in 3h 45m` |

#### Battery forecast sensor

| Entity | Description |
|---|---|
| `sensor.foxess_battery_forecast` | Projected battery SoC (%) over time. The `forecast` attribute contains a list of `{"time": <epoch_ms>, "soc": <float>}` data points (5-minute intervals) for charting. |

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

### Control card

The integration includes a custom Lovelace card that displays the current smart operation status with a battery gauge, progress indicators, and action buttons for starting/cancelling sessions. Both a visual editor and YAML configuration are supported.

The card is auto-registered as a Lovelace resource when the integration loads (storage mode dashboards). No manual resource setup is needed.

```yaml
type: custom:foxess-control-card
```

That's it — no configuration required. The card auto-discovers the `sensor.foxess_smart_operations` and `sensor.foxess_battery_soc` entities.

**What the card shows:**

- **Header**: Battery SoC gauge with colour-coded fill (green/orange/red by level), data source badge with staleness indicator
- **Action buttons**: Charge and Discharge buttons open inline parameter forms (start time, end time, SoC target). Cancel button (with double-tap confirmation) appears during active sessions.
- **Smart Charge** (green section): Time window, power, target SoC with progress bar, remaining time badge. Shows "Charge Scheduled" with a dim indicator when deferred, "Smart Charge" with a pulsing dot when actively charging.
- **Smart Discharge** (orange section): Time window, power, min SoC, feed-in energy limit. Shows "Discharge Scheduled" before the window opens, "Discharge Deferred" during the deferred self-use phase, and "Smart Discharge" with a pulsing dot when actively discharging. Power is hidden during the deferred phase since no forced discharge is active.
- **Idle**: Clean message when no smart operation is active.

To hide the card when no smart operation is active, wrap it in a conditional card:

```yaml
type: conditional
conditions:
  - condition: state
    entity: sensor.foxess_smart_operations
    state_not: Idle
card:
  type: custom:foxess-control-card
```

**Configuration options:**

| Option | Default | Description |
|---|---|---|
| `operations_entity` | Auto-discovered | Smart operations sensor entity ID. |
| `soc_entity` | Auto-discovered | Battery SoC sensor entity ID. |
| `freshness_entity` | Auto-discovered | Data freshness sensor for staleness badge. |
| `show_cancel` | `true` | Show the cancel button during active sessions. Set to `false` to hide it. |

```yaml
type: custom:foxess-control-card
show_cancel: false
```

### Overview card

A second built-in card shows live energy flows between solar, battery, grid and house in a responsive grid layout. Both a visual editor and YAML configuration are supported.

```yaml
type: custom:foxess-overview-card
```

No configuration required — all entities are auto-discovered. The card shows:

- **Solar**: Total solar power with PV1/PV2 breakdown (clickable for history)
- **House**: Household consumption (clickable for history)
- **Grid**: Import/export power with direction indicator, voltage and frequency (clickable for history)
- **Battery**: SoC gauge, charge/discharge rate with direction indicator, BMS cell temperature, inverter temperature, residual energy (clickable for history)
- **Work mode**: Current inverter work mode badge in the header
- **Data source badge**: Shows current data source (WS/API/Modbus) with staleness indicator

Clicking any energy flow node opens the HA entity history dialog. Sub-details (cell temperature, PV strings, grid voltage/frequency, residual energy) are individually clickable.

#### Box customisation

Show, hide, reorder, relabel, and re-icon the four energy flow boxes via the visual editor or YAML `boxes` config. The layout adapts responsively for 1, 3, or 4 visible boxes.

```yaml
type: custom:foxess-overview-card
boxes:
  - battery
  - type: solar
    label: PV
    icon: "☀️"
```

Each entry in `boxes` can be a string shorthand (`"solar"`) or an object with `type`, `label`, and `icon` properties. Boxes not listed are hidden. Omit `boxes` entirely to show all four with defaults.

#### Entity overrides

All entities are auto-discovered. To override:

```yaml
type: custom:foxess-overview-card
solar_entity: sensor.foxess_solar_power
house_entity: sensor.foxess_house_load
grid_import_entity: sensor.foxess_grid_consumption
grid_export_entity: sensor.foxess_grid_feed_in
battery_charge_entity: sensor.foxess_charge_rate
battery_discharge_entity: sensor.foxess_discharge_rate
soc_entity: sensor.foxess_battery_soc
work_mode_entity: sensor.foxess_work_mode
pv1_entity: sensor.foxess_pv1_power
pv2_entity: sensor.foxess_pv2_power
grid_voltage_entity: sensor.foxess_grid_voltage
grid_frequency_entity: sensor.foxess_grid_frequency
bat_temp_entity: sensor.foxess_battery_temperature
bms_temp_entity: sensor.foxess_bms_battery_temperature
residual_entity: sensor.foxess_residual_energy
data_freshness_entity: sensor.foxess_data_freshness
```

### Forecast card

An SVG-based card showing the projected SoC trajectory with target/min SoC markers.

```yaml
type: custom:foxess-forecast-card
```

No configuration required — entities are auto-discovered. To override:

```yaml
type: custom:foxess-forecast-card
forecast_entity: sensor.foxess_battery_forecast
operations_entity: sensor.foxess_smart_operations
```

### Session history card

A 24-hour horizontal timeline with coloured session bars and SoC trace overlay.

```yaml
type: custom:foxess-history-card
```

| Option | Default | Description |
|---|---|---|
| `hours` | `24` | Timeline span: `12`, `24`, or `48`. |
| `operations_entity` | Auto-discovered | Smart operations sensor entity ID. |
| `soc_entity` | Auto-discovered | Battery SoC sensor entity ID. |

```yaml
type: custom:foxess-history-card
hours: 48
```

> **YAML mode dashboards:** If you use YAML-mode Lovelace (not the default storage mode), add the resources manually to your `configuration.yaml`:
> ```yaml
> lovelace:
>   resources:
>     - url: /foxess_control/foxess-control-card.js
>       type: module
>     - url: /foxess_control/foxess-overview-card.js
>       type: module
>     - url: /foxess_control/foxess-forecast-card.js
>       type: module
>     - url: /foxess_control/foxess-history-card.js
>       type: module
> ```

## Supported languages

The integration UI — entity names, service descriptions, config options, and both Lovelace cards — is fully translated into the following languages:

| Language | Code |
|---|---|
| English | `en` |
| German | `de` |
| French | `fr` |
| Dutch | `nl` |
| Spanish | `es` |
| Italian | `it` |
| Polish | `pl` |
| Portuguese | `pt` |
| Simplified Chinese | `zh-Hans` |
| Japanese | `ja` |

Home Assistant automatically selects the language based on the user's profile language setting. Lovelace cards use `hass.language` for card-level UI elements (labels, durations, status text).

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

## Automation examples

> **New to FoxESS Control?** See [EXAMPLES.md](EXAMPLES.md) for a quick-start guide with copy-pasteable automations and dashboard setup.

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

### Cloud mode (default)

- Force charge/discharge actions write a time-windowed override to the inverter's scheduler via the FoxESS Cloud API. Outside scheduled windows, the inverter defaults to self-use mode.
- Each force action only replaces existing overrides of the **same mode** (e.g. force charge replaces previous force charge windows, but leaves force discharge windows intact). Overrides of a different mode are always preserved, even if their time window has already passed today — this allows standing daily schedules (e.g. a free-electricity charge window) to coexist with evening discharge overrides.
- If the new window would overlap with an existing override of a **different** mode, the action aborts with an error to prevent conflicts.
- When a smart action ends (SoC target reached, time window expired), it removes **only its own mode's groups** from the schedule. Other modes' groups are preserved. For example, a smart discharge ending does not remove a standing `ForceCharge` window. If no groups remain after removal, the schedule reverts to self-use.
- Smart sessions are persisted to Home Assistant's `.storage` directory. If HA restarts during an active session, it is automatically resumed if still within its time window, or cleaned up (schedule groups removed) if expired. Sessions from a previous day are discarded.
- The API client throttles requests (minimum 5 seconds between calls) and retries with exponential backoff on rate limits.

### Entity mode

- Force charge/discharge actions set the inverter's work mode directly via foxess_modbus entity service calls. foxess_control manages time windows using Home Assistant timers — no cloud schedule involved.
- There is no multi-window schedule management. Each action sets a single mode; `clear_overrides` returns to Self Use.
- Smart sessions use the same algorithms (consumption-aware deferral, SoC monitoring, power adjustment) as cloud mode — only the read/write transport differs.
- Session recovery after HA restart resumes based on persisted state without needing to verify cloud schedule groups.

### Session resilience

Smart sessions are designed to survive transient failures:

- **Transient API errors**: A single cloud API failure is retried on the next timer tick. Only 3 consecutive failures open the circuit breaker.
- **Circuit breaker**: After 3 consecutive adapter errors, the session holds position (keeps the current schedule). After 5 more ticks without recovery, the session aborts to self-use to protect the battery.
- **Automatic replay after outage**: When the circuit breaker aborts a session and the time window is still open, the integration probes the API every 5 minutes and restarts the session on recovery (up to 6 attempts).
- **HA restart recovery**: Smart sessions are persisted to `.storage`. If HA restarts mid-session, the session is automatically resumed if still within its time window, or cleaned up if expired.
- **Schedule safety horizon**: The discharge schedule end time is set to a dynamically computed safe horizon based on current SoC, discharge rate, and safety factor. If HA loses connectivity, the inverter's schedule expires and reverts to self-use — the battery is protected without HA intervention.

## Known limitations

- **Minimum SoC behaviour is unintuitive**: When the battery reaches the minimum SoC during force discharge or feed-in, the inverter's behaviour may not match expectations. Smart actions work around this by setting `fdSoc` to an extreme value (100% for charge, 11% for discharge) so the inverter never triggers its own threshold — HA monitors SoC and stops the action at the user's configured target. For plain `force_charge`/`force_discharge`, consider using an automation to cancel the override before the battery reaches the minimum SoC level.

- **Schedule race condition** (cloud mode only): Force charge/discharge actions read the current schedule, modify it, then write it back. If the schedule is changed between the read and write (e.g. via the FoxESS app), those changes will be overwritten. Enable debug logging for `foxess_control` to see before/after state if schedules change unexpectedly. Entity mode does not have this issue.
- **FoxESS Cloud API latency** (cloud mode only): All commands go through the FoxESS Cloud API, which throttles requests to one every 5 seconds. Actions are not instantaneous. For faster local control, enable [entity mode](#entity-mode-foxess_modbus-interop) with foxess_modbus.
- **FoxESS mode scheduler bugs** (cloud mode only): The FoxESS Cloud API has known issues with schedule validation (e.g. rejecting its own saved schedules due to overlap detection on disabled groups). This integration works around known issues, but the API may introduce new ones.

## Migrating from foxess-ha

FoxESS Control includes all the polled sensors that foxess-ha provides (SoC, charge/discharge power, solar generation, house load, grid power, temperatures, cumulative energy counters, and more), plus smart battery management. The legacy foxess-ha integration is read-only, so it is unlikely to be deeply wired into automations or dashboards.

The cleanest migration path is to **remove foxess-ha first**, then install FoxESS Control:

1. **Remove the foxess-ha integration** from Settings > Devices & Services. This avoids entity name collisions and gives FoxESS Control the cleanest possible entity IDs.
2. **Clean up orphaned entities** (if any remain after removal): go to Settings > Devices & Services > Entities, filter by "unavailable" or search for `foxess`, and delete any leftover entities from the old integration.
3. **Install FoxESS Control** via HACS and configure it. Sensor entity IDs will follow the `sensor.foxess_*` pattern — see the [Sensors](#sensors) section above for the full list.
4. **Update any automations or dashboards** that referenced foxess-ha entities to use the new entity IDs. Since foxess-ha was read-only, these are typically just sensor references on dashboard cards.

> **Note:** If you prefer to run both integrations side-by-side temporarily, keep the polling interval at 300 seconds (the default) on both to avoid exceeding the FoxESS API quota (~1440 requests/day).

## FAQ

See [FAQ.md](FAQ.md) for answers to common questions: back-to-back sessions, working without web credentials, HA restart behaviour, API quota, entity mode vs cloud mode, battery capacity, and more.

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for decision-tree guides to common issues: sessions not starting, grid import during discharge, WebSocket problems, early session aborts, and stale data.

**Diagnostics:** Click "Download Diagnostics" on the integration page (Settings > Devices & Services > FoxESS Control) to export coordinator data, session state, WebSocket status, taper profile, and config — with API keys and credentials redacted.

**Repair issues:** Actionable errors (unmanaged work mode, session aborts) surface in HA's Repairs panel instead of just logs. Issues auto-clear when the problem is resolved or a new session starts.

**Reauthentication:** When the FoxESS API key expires or becomes invalid, HA shows a "Reconfigure" prompt instead of silently failing. Enter a new key without removing and re-adding the integration.

## Support

If you find this integration useful, consider buying me a coffee:

[![Donate](https://img.shields.io/badge/Donate-PayPal-blue.svg)](https://www.paypal.com/donate/?hosted_button_id=3NEP4LZAHLH6W)

## License

MIT
