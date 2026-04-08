# FoxESS Control

A Home Assistant custom integration for controlling FoxESS inverter battery modes via the FoxESS Cloud API.

This integration is designed to complement the existing [foxess](https://github.com/macxq/foxess-ha) integration, which handles sensor polling and read-only state. FoxESS Control adds write actions: force charge, force discharge, and clearing overrides back to self-use mode.

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
| Minimum SoC on Grid | 15% | 11-100% | The minimum battery state of charge to maintain when on grid. Applied to all schedule operations. |
| Battery SoC Entity | _(none)_ | | A Home Assistant sensor entity that reports the battery state of charge. Required for `smart_charge` and `smart_discharge`. |
| Battery Capacity | 0.0 kWh | 0-100 kWh | Total usable battery capacity in kWh. Required for `smart_charge` power calculations. |
| Min Power Change | 500 W | 0-5000 W | Minimum watt change before updating the charge schedule during `smart_charge`. Lower values improve SoC tracking, higher values reduce API calls. |

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

```yaml
action: foxess_control.force_discharge
data:
  duration: "02:00:00"
  power: 5000
```

### `foxess_control.smart_charge`

Charges the battery within a time window, periodically adjusting the charge rate to reach a target SoC by the end of the interval. This avoids drawing more grid power than necessary during off-peak windows.

**How it works:**

1. Calculates the charge power needed to reach the target SoC in the remaining time, based on the configured battery capacity.
2. Sets a `ForceCharge` schedule with the calculated power and `fdSoc` set to the target.
3. Every 5 minutes, re-reads the current SoC and adjusts the charge power up or down. If the power change is below the configured **Min Power Change** threshold, the update is skipped to avoid unnecessary API calls.
4. When the SoC reaches the target, the session ends: the schedule reverts to self-use and all listeners are cancelled.
5. When the time window ends, listeners are cancelled (the schedule entry itself expires naturally).

Only one smart charge session can be active at a time. Starting a new `smart_charge` cancels any previous session. A `force_charge` action also cancels any running smart charge, since it replaces the underlying `ForceCharge` schedule.

**Stopping a running smart charge:** Call `foxess_control.clear_overrides` (with no mode, or with `mode: ForceCharge`). This removes the schedule **and** cancels the background listeners.

**Requires** the Battery SoC Entity and Battery Capacity to be configured in the integration options.

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

1. Sets a `ForceDischarge` schedule with `fdSoc` set to the minimum SoC threshold.
2. Monitors the Battery SoC Entity in real time. When the SoC drops to the `min_soc` threshold, the session ends: the schedule reverts to self-use and all listeners are cancelled.
3. When the time window ends, listeners are cancelled.

Only one smart discharge session can be active at a time. Starting a new `smart_discharge` cancels any previous session. A `force_discharge` action also cancels any running smart discharge, since it replaces the underlying `ForceDischarge` schedule.

**Stopping a running smart discharge:** Call `foxess_control.clear_overrides` (with no mode, or with `mode: ForceDischarge`). This removes the schedule **and** cancels the background listeners.

**Requires** the Battery SoC Entity to be configured in the integration options.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `start_time` | Yes | | Time of day to start discharging (e.g. `"17:00:00"`). |
| `end_time` | Yes | | Time of day to stop discharging (e.g. `"20:00:00"`). Must be after start time, within 4 hours. |
| `power` | No | Inverter max | Discharge power limit in watts (min 100). |
| `min_soc` | Yes | | Stop discharging and revert to self-use when the battery reaches this SoC level (11-100%). |
| `replace_conflicts` | No | false | Remove conflicting overrides instead of aborting. |

```yaml
action: foxess_control.smart_discharge
data:
  start_time: "17:00:00"
  end_time: "20:00:00"
  min_soc: 30
  power: 5000
```

## Binary sensors

The integration creates two binary sensors that track whether a smart charge or smart discharge session is currently active:

| Entity | State | Attributes when on |
|---|---|---|
| `binary_sensor.foxess_smart_charge_active` | `on` while a smart charge session is running | `target_soc`, `current_power_w`, `max_power_w`, `end_time`, `soc_entity` |
| `binary_sensor.foxess_smart_discharge_active` | `on` while a smart discharge session is running | `min_soc`, `last_power_w`, `end_time`, `soc_entity` |

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
- The API client throttles requests (minimum 5 seconds between calls) and retries with exponential backoff on rate limits.

## Known limitations

- **Minimum SoC behaviour is unintuitive**: When the battery reaches the minimum SoC during force discharge or feed-in, the inverter's behaviour may not match expectations. It is recommended to define an automation that cancels the mode override (via `foxess_control.clear_overrides`) before the battery reaches this level, rather than relying on the inverter's built-in minimum SoC handling.

- **Schedule race condition**: Force charge/discharge actions read the current schedule, modify it, then write it back. If the schedule is changed between the read and write (e.g. via the FoxESS app), those changes will be overwritten. Enable debug logging for `foxess_control` to see before/after state if schedules change unexpectedly.
- **FoxESS Cloud API latency**: All commands go through the FoxESS Cloud API, which throttles requests to one every 5 seconds. Actions are not instantaneous. For faster local control, consider modbus-based integrations.
- **FoxESS mode scheduler bugs**: The FoxESS Cloud API has known issues with schedule validation (e.g. rejecting its own saved schedules due to overlap detection on disabled groups). This integration works around known issues, but the API may introduce new ones.

## Compatibility with foxess-ha

This integration uses its own config entry and does not read configuration from the foxess-ha sensor integration. You will need to enter your API key and serial number separately. Both integrations can run side-by-side without conflict.

## Support

If you find this integration useful, consider buying me a coffee:

[![Donate](https://img.shields.io/badge/Donate-PayPal-blue.svg)](https://www.paypal.com/donate/?hosted_button_id=3NEP4LZAHLH6W)

## License

MIT
