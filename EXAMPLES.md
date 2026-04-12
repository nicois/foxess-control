# FoxESS Control — Examples

Practical examples to get you started quickly. For full documentation, see the [README](README.md).

**Prerequisite:** Install the integration via HACS or manually, then configure **Battery Capacity** in the integration options (Settings > Devices & Services > FoxESS Control > Configure). Smart operations won't work without this.

---

## Dashboard Cards

Both cards auto-discover your entities — just add them to a dashboard, no configuration needed.

### Energy Flow Overview

Shows live power flow between solar, battery, grid and house with real-time values.

```yaml
type: custom:foxess-overview-card
```

![Overview card showing solar, battery, grid and house power flow](docs/images/overview-card.png)

### Smart Operations

Shows active charge/discharge sessions with a battery SoC forecast chart.

```yaml
type: custom:foxess-control-card
```

![Smart operations card showing discharge session with forecast](docs/images/smart-ops-card.png)

> **Note:** If you use a YAML-mode dashboard, add the card resources manually — see the [README](README.md#custom-dashboard-cards) for details.

---

## Example 1: Off-Peak Charge + Evening Discharge

**Scenario:** Your electricity tariff has cheap overnight rates (1am–5am) and expensive evening peak rates (5pm–8pm). Charge the battery overnight and discharge it during the peak to avoid importing expensive grid power.

```yaml
automation:
  - alias: "Off-peak smart charge"
    trigger:
      - platform: time
        at: "01:00:00"
    action:
      - action: foxess_control.smart_charge
        data:
          start_time: "01:00:00"
          end_time: "05:00:00"
          target_soc: 90
          replace_conflicts: true

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
          replace_conflicts: true
```

**What happens:**

- **Charge (1am–5am):** The integration calculates how much energy is needed to reach 90% and defers the start of grid charging as late as possible within the window. If the battery is already at 70%, it might not start charging until 3:30am. Power is paced so the battery reaches 90% right at 5am — no wasted energy.
- **Discharge (5pm–8pm):** The battery discharges to power your house instead of importing from the grid. Power is paced so you reach 30% at 8pm rather than hitting the floor early and importing for the rest of the evening.
- **Safety:** If SoC reaches the target (charge) or floor (discharge) early, the session monitors and can resume if conditions change (e.g. a cloud passes and SoC dips, or unexpected consumption draws the battery down).

![Card showing deferred charge session](docs/images/example-charge.png)

---

## Example 2: Solar Self-Consumption Maximiser

**Scenario:** You want to capture as much solar energy as possible during the day and use it in the evening, minimising grid imports.

```yaml
automation:
  - alias: "Midday solar charge"
    trigger:
      - platform: time
        at: "10:00:00"
    action:
      - action: foxess_control.smart_charge
        data:
          start_time: "10:00:00"
          end_time: "15:00:00"
          target_soc: 100
          replace_conflicts: true

  - alias: "Evening self-consumption discharge"
    trigger:
      - platform: time
        at: "17:00:00"
    action:
      - action: foxess_control.smart_discharge
        data:
          start_time: "17:00:00"
          end_time: "21:00:00"
          min_soc: 20
          replace_conflicts: true
```

**What happens:**

- **Charge (10am–3pm):** Smart charge defers grid charging as long as possible. On a sunny day, solar production alone fills the battery and grid charging never kicks in. On a cloudy day, grid charging tops up whatever solar didn't cover — but only in the last portion of the window.
- **Discharge (5pm–9pm):** The stored solar energy powers your evening consumption. Pacing spreads the discharge across 4 hours so you aren't left importing at 8pm.

**Tip:** The charge window doesn't mean the inverter charges from the grid for 5 hours — it's the *available window*. Smart charge waits as long as it can, giving solar the first opportunity.

---

## Example 3: Feed-In Energy Budget

**Scenario:** Your export tariff pays well during the afternoon peak (3pm–5pm). You want to export exactly 2 kWh to the grid and stop — preserving battery for your own evening use.

```yaml
automation:
  - alias: "Afternoon peak export"
    trigger:
      - platform: time
        at: "15:00:00"
    action:
      - action: foxess_control.smart_discharge
        data:
          start_time: "15:00:00"
          end_time: "17:00:00"
          min_soc: 30
          feedin_energy_limit_kwh: 2.0
          replace_conflicts: true
```

**What happens:**

- The integration paces discharge power so that 2 kWh of grid export is spread evenly across the 2-hour window.
- Your house consumption is served from the battery *in addition to* the 2 kWh export — the limit only counts energy that actually reaches the grid.
- The session stops at whichever comes first: the 2 kWh export limit, reaching 30% SoC, or 5pm.
- The dashboard card shows live progress: **Feed-in: 0.85 / 2.0 kWh (projected 1.92)**

![Card showing feed-in progress](docs/images/example-feedin.png)

---

## Example 4: Weather-Conditional Charging

**Scenario:** Only charge from the grid overnight if tomorrow's solar forecast is poor. On sunny days, let solar handle it.

This example uses [Solcast](https://github.com/BJReplay/ha-solcast-solar) but works with any forecast integration that exposes a sensor.

```yaml
automation:
  - alias: "Conditional overnight charge"
    trigger:
      - platform: time
        at: "01:00:00"
    condition:
      - condition: numeric_state
        entity_id: sensor.solcast_pv_forecast_forecast_tomorrow
        below: 15  # kWh — adjust to your system size
    action:
      - action: foxess_control.smart_charge
        data:
          start_time: "01:00:00"
          end_time: "05:00:00"
          target_soc: 80
          replace_conflicts: true
```

**What happens:**

- At 1am, the automation checks tomorrow's solar forecast.
- If less than 15 kWh is expected (a cloudy day for your system), it charges to 80% overnight on cheap rates.
- If it's going to be sunny, it skips — solar will handle charging for free.

**Tip:** Adjust the threshold to your panel capacity. A 6.6 kW system might use 20 kWh as the threshold; a 10 kW system might use 30 kWh.

---

## Example 5: Price-Driven Discharge

**Scenario:** You're on a dynamic tariff (Amber Electric, Octopus Agile, etc.) and want to discharge when the price spikes above a threshold.

```yaml
automation:
  - alias: "Discharge on high price"
    trigger:
      - platform: numeric_state
        entity_id: sensor.amber_general_price
        above: 30  # cents/kWh — adjust to your tariff
    action:
      - action: foxess_control.smart_discharge
        data:
          start_time: "{{ now().strftime('%H:%M:%S') }}"
          end_time: "{{ (now() + timedelta(hours=2)).strftime('%H:%M:%S') }}"
          min_soc: 25
          replace_conflicts: true

  - alias: "Stop discharge when price drops"
    trigger:
      - platform: numeric_state
        entity_id: sensor.amber_general_price
        below: 15  # cents/kWh
    condition:
      - condition: state
        entity_id: binary_sensor.foxess_smart_discharge_active
        state: "on"
    action:
      - action: foxess_control.clear_overrides
```

**What happens:**

- When the electricity price exceeds 30c/kWh, a 2-hour discharge session starts immediately.
- `min_soc: 25` protects the battery from being fully drained.
- If the price drops below 15c/kWh while discharging, the second automation cancels the session and reverts to self-use.
- `replace_conflicts: true` means a new price spike re-triggers discharge even if a previous session is running.

**Tip:** Use the `binary_sensor.foxess_smart_discharge_active` condition to avoid cancelling a session that isn't running.

---

## Example 6: Free Electricity Window + Evening Discharge

**Scenario:** Your tariff includes a free electricity period (e.g. Power Shout, 11am–2pm). Charge at full power during free hours, then discharge in the evening.

```yaml
automation:
  - alias: "Free electricity charge"
    trigger:
      - platform: time
        at: "11:00:00"
    action:
      - action: foxess_control.force_charge
        data:
          duration: "03:00:00"
          replace_conflicts: true

  - alias: "Evening discharge"
    trigger:
      - platform: time
        at: "17:00:00"
    action:
      - action: foxess_control.smart_discharge
        data:
          start_time: "17:00:00"
          end_time: "20:00:00"
          min_soc: 30
          replace_conflicts: true
```

**What happens:**

- **Force charge (11am–2pm):** Uses `force_charge` instead of `smart_charge` because the electricity is free — no need to defer or pace. Charges at maximum power for the full 3 hours.
- **Smart discharge (5pm–8pm):** Paced discharge through the evening peak as usual.

**Why force_charge here?** `smart_charge` tries to minimise grid charging by deferring. When electricity is free, you *want* to charge immediately at full power — that's what `force_charge` does.

---

## Tips & Tricks

### Use `replace_conflicts: true` for automated schedules

When automations run on timers, previous sessions may still be active. Setting `replace_conflicts: true` lets the new session cleanly replace any overlapping schedule entries instead of failing with a conflict error.

### Prevent conflicting automations with binary sensors

Use the built-in binary sensors as conditions to avoid automations stepping on each other:

```yaml
condition:
  - condition: state
    entity_id: binary_sensor.foxess_smart_charge_active
    state: "off"
```

### The cards need no template sensors

The smart operations card reads everything directly from the `sensor.foxess_smart_operations` entity attributes. You don't need to create template sensors for power, SoC, remaining time, or forecast data — it's all built in.

### Combine force and smart operations

`force_charge` and `force_discharge` are for fixed-rate, fixed-duration windows (like free electricity periods). `smart_charge` and `smart_discharge` are for variable-rate windows where pacing and deferral add value. Mix them freely — each new session cleanly replaces the previous one.

### Session recovery

If Home Assistant restarts during an active session, the integration automatically recovers it. No automation re-trigger needed — the session resumes where it left off, recalculates power, and continues until its end condition is met.
