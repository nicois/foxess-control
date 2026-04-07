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
| Minimum SoC on Grid | 15% | 5-100% | The minimum battery state of charge to maintain when on grid. Applied to all schedule operations. |

## Actions

The integration registers three actions (services) under the `foxess_control` domain. These are intended to be called from automations.

### `foxess_control.clear_overrides`

Clears any active override and returns the inverter to self-use mode.

No parameters.

```yaml
action: foxess_control.clear_overrides
```

### `foxess_control.force_charge`

Forces the inverter to charge the battery for a specified duration.

| Parameter | Required | Description |
|---|---|---|
| `duration` | Yes | How long to force charge. Maximum 4 hours. Must not extend past midnight. |

```yaml
action: foxess_control.force_charge
data:
  duration: "01:30:00"
```

### `foxess_control.force_discharge`

Forces the inverter to discharge the battery for a specified duration.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `duration` | Yes | | How long to force discharge. Maximum 4 hours. Must not extend past midnight. |
| `min_soc` | No | 10 | Stop discharging when the battery reaches this SoC (%). Range: 5-100. |

```yaml
action: foxess_control.force_discharge
data:
  duration: "02:00:00"
  min_soc: 20
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
          min_soc: 15

  - alias: "Clear overrides at end of peak"
    trigger:
      - platform: time
        at: "20:00:00"
    action:
      - action: foxess_control.clear_overrides
```

## How it works

- Force charge/discharge actions write a single time-windowed override to the inverter's scheduler via the FoxESS Cloud API. Outside the scheduled window, the inverter defaults to self-use mode.
- Calling an action while an override is already active replaces the existing override.
- The API client throttles requests (minimum 5 seconds between calls) and retries with exponential backoff on rate limits.

## Compatibility with foxess-ha

This integration uses its own config entry and does not read configuration from the foxess-ha sensor integration. You will need to enter your API key and serial number separately. Both integrations can run side-by-side without conflict.

## License

MIT
