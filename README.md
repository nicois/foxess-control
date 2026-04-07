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

## Actions

The integration registers three actions (services) under the `foxess_control` domain. These are intended to be called from automations.

### `foxess_control.clear_overrides`

Clears overrides and returns the inverter to self-use mode. If `mode` is specified, only overrides of that mode are removed; other overrides are retained.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `mode` | No | All | Only clear overrides of this mode (`ForceCharge`, `ForceDischarge`, etc.). |

```yaml
# Clear all overrides
action: foxess_control.clear_overrides
```

```yaml
# Clear only force-charge overrides, keeping others
action: foxess_control.clear_overrides
data:
  mode: ForceCharge
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

## Automation example

Charge during off-peak hours, then discharge during the evening peak:

```yaml
automation:
  - alias: "Off-peak charge"
    trigger:
      - platform: time
        at: "02:00:00"
    action:
      - action: foxess_control.force_charge
        data:
          duration: "04:00:00"

  - alias: "Evening peak discharge"
    trigger:
      - platform: time
        at: "17:00:00"
    action:
      - action: foxess_control.force_discharge
        data:
          duration: "03:00:00"

  - alias: "Clear overrides at end of peak"
    trigger:
      - platform: time
        at: "20:00:00"
    action:
      - action: foxess_control.clear_overrides
```

## How it works

- Force charge/discharge actions write a time-windowed override to the inverter's scheduler via the FoxESS Cloud API. Outside scheduled windows, the inverter defaults to self-use mode.
- Each force action only replaces existing overrides of the **same mode** (e.g. force charge replaces previous force charge windows, but leaves force discharge windows intact).
- If the new window would overlap with an existing override of a **different** mode, the action aborts with an error to prevent conflicts.
- The API client throttles requests (minimum 5 seconds between calls) and retries with exponential backoff on rate limits.

## Known limitations

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
