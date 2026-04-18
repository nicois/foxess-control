---
project: FoxESS Control
level: 1
last_verified: 2026-04-18
traces_up: []
traces_down: [02-constraints.md]
---
# Vision

To provide owners of battery-backed solar inverters a Home Assistant
integration which simplifies getting maximum value from their investment.

## Context
People with battery-backed inverters (with or without solar) primarily purchase
them for the financial return on investment, with a secondary benefit of
providing energy security when their grid connection suffers from acute or chronic outages.

The reasons for force-charging:
- the electricity is free
- the electricity is cheaper to buy now than it will be to sell later
- the electricity is cheaper to buy now than to buy later
- the electricity may not be available later

The reasons for force-discharging:
- the feed-in price is greater now than it will be to buy electricity later
- the feed-in price is positive and it will be free to get more energy later (via solar or a free tariff)

These points are nuanced: application of these criteria are complicated by the need to know current
energy levels and usage, and to anticipate future usage.

## Purpose

FoxESS Control is a Home Assistant integration that provides smart battery
charge and discharge management for FoxESS inverters. It paces battery
power to hit SoC and energy targets within user-defined time windows,
adapting to real-time household consumption and BMS taper behaviour.

The core problem: without smart pacing, forced charge/discharge runs at
full power until the target is hit, then idles for the remainder of the
window. This wastes the time window (charge finishes too early, missing
cheap-rate hours) or causes grid import (discharge power drops below
house load near the end).

## Strategic Direction

Extract the brand-agnostic smart battery algorithms into a shared library
(`smart_battery/`) that can be reused by integrations for other inverter
brands. The pacing algorithms, session management, sensor framework, and
Lovelace cards are brand-independent. Only the inverter control layer
(API client, schedule management, WebSocket) is brand-specific.

Target brand order: Huawei, GoodWe (already split out), SolaX, Sungrow.

## Non-Goals

- **Solar forecasting**: forecast data (e.g. Solcast) is consumed via
  external HA automations that call the smart_charge/smart_discharge
  services. The integration does not fetch or model solar production.
- **Tariff optimisation**: tariff-aware scheduling belongs in external
  automations. The integration provides time-windowed services; the
  caller decides when to invoke them.
- **Grid services / demand response**: no virtual power plant or
  aggregator integration.
- **Direct Modbus implementation**: local control is achieved via the
  entity-mode adapter (reading/writing foxess_modbus entities), not by
  implementing Modbus protocol directly.
- **Energy accounting / billing**: the integration tracks energy for
  pacing purposes (feed-in budget, trajectory), not for billing or
  reporting.

## Success Criteria

For smart charge:
- Battery reaches target SoC within the configured window.
- Pacing adapts to real-time consumption/generation to avoid unnecessary energy import, giving solar maximum opportunity to provide as much as possible
- Integration survives HA restarts mid-session (session recovery).
- Same pacing logic works across multiple inverter brands.

For smart discharge:
- Battery does not breach minimum SoC during the discharge window
- No grid import during forced discharge (P1 safety priority).
- Pacing adapts to real-time consumption without operator intervention.
- Integration survives HA restarts mid-session (session recovery).
- Same pacing logic works across multiple inverter brands.

Operational:
- The system is operationally transparent — the user can understand
  what the system is doing and why without inspecting logs or code.
