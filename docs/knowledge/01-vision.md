---
project: FoxESS Control
level: 1
last_verified: 2026-04-24
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

## Priorities

A strictly ordered list of goals, used to resolve conflicts between
design choices. When two goals disagree, the **lower-numbered**
priority wins. Every C-NNN constraint names the priority it
enforces; every D-NNN design decision names the priority it serves
and, if it trades anything away, the lower-priority goal it
sacrifices. A trade-off that inverts this order is a bug.

### P-001: No grid import during forced discharge
**Statement**: While a smart-discharge session is active, the
integration must not cause the house to import energy from the grid.
**Rationale**: Import during discharge is a direct economic and
behavioural defect — the user explicitly asked to discharge, and the
system silently did the opposite. This is the single invariant we
will sacrifice any other goal to protect.
**Enforced by**: C-001 (discharge power floor), C-017 (end-of-discharge
guard), C-037 (grid export limit awareness)
**Served by**: D-001..D-005 (discharge pacing), D-044 (export-limit
awareness)

### P-002: Respect minimum state of charge
**Statement**: Forced discharge must not drive the battery below the
configured minimum SoC, preserving reserves for outages and battery
longevity.
**Rationale**: Min SoC reflects a user decision about backup headroom
and long-term battery health. A session that chases a feed-in target
past the min SoC violates that decision.
**Enforced by**: C-002 (min SoC floor), C-012 (SoC unavailability
cancels session), C-019 (discharge SoC unavailability abort)
**Served by**: D-003 (SoC deadline), D-004 (suspend at min SoC)

### P-003: Meet the user's energy target
**Statement**: Within each session window, the system should charge
to the configured SoC target or discharge the configured energy
target.
**Rationale**: The target is the user's stated goal. Hitting it
matters, but only after no-import and min-SoC invariants are
guaranteed.
**Enforced by**: C-022 (unreachable target surfacing — the user must
know if the target cannot be met)
**Served by**: D-002 (deferred start), D-005 (feed-in budget), D-006
(charge pacing), D-007 (charge taper)

### P-004: Maximise feed-in revenue
**Statement**: When the other priorities allow, prefer earlier /
fuller feed-in to later self-use, since the feed-in window is bounded
while self-use opportunities are continuous.
**Rationale**: Feed-in is the user's economic optimisation goal.
Pacing buffers that sacrifice feed-in to protect invariants are
correct; buffers that sacrifice feed-in for their own sake must be
scrutinised against this priority.
**Enforced by**: (none — aspirational)
**Served by**: D-005 (feed-in budget), D-044 (export-limit awareness)

### P-005: Operational transparency
**Statement**: The user can determine the system's state and
reasoning from the UI alone, without inspecting logs or code.
**Rationale**: A black-box pacing system is un-auditable: users
cannot tell whether the system is working or misbehaving. This
priority sits below the safety and target goals but above any
implementation convenience.
**Enforced by**: C-020 (operational transparency), C-022
(unreachable target surfaced), C-026 (proactive error surfacing)
**Served by**: D-029 (sensors), D-039 (Lovelace cards)

### P-006: Brand portability
**Statement**: The pacing algorithms, session management, and sensor
framework must run on non-FoxESS inverters with only an adapter
change.
**Rationale**: The strategic direction of the project is to support
multiple brands from a single algorithm core. This priority shapes
architecture but cannot override safety invariants during a given
session.
**Enforced by**: C-021 (brand-agnostic code belongs in common
package), C-015 (vendored smart_battery parity)
**Served by**: architecture decisions (adapter protocol, smart_battery
package)

### P-007: Engineering process integrity
**Statement**: The tests, tooling, and architectural rules that
guarantee the above priorities can be verified and preserved over
time must themselves be treated as invariants: realistic test
doubles over mocks, no flaky tests, reproduce-before-fix, typed
access patterns, module size budgets.
**Rationale**: A system that claims to meet P-001..P-006 without a
disciplined test and review process is making an unverifiable
claim. This priority sits below the runtime priorities — it is
never acceptable to sacrifice a production invariant to avoid
engineering pain — but it is the meta-invariant that keeps the
others honest.
**Enforced by**: C-015 (vendored parity), C-028..C-033 (testing
discipline), C-034..C-036 (architectural enforcement)
**Served by**: `simulator/`, `/regression-test` skill, pre-commit
hooks, semgrep rules

## Success Criteria

Success criteria are derived from the priorities above. They are
observable outcomes that demonstrate each priority is met in
production.

For smart charge (primarily serves P-003):
- Battery reaches target SoC within the configured window.
- Pacing adapts to real-time consumption/generation to avoid
  unnecessary energy import, giving solar maximum opportunity to
  provide as much as possible.
- Integration survives HA restarts mid-session (session recovery).
- Same pacing logic works across multiple inverter brands (P-006).

For smart discharge (primarily serves P-001, P-002, P-003, P-004):
- No grid import during forced discharge (P-001).
- Battery does not breach minimum SoC during the discharge window
  (P-002).
- Energy target is met when reachable; unreachable targets are
  surfaced (P-003).
- Remaining feed-in budget is used, subject to the above invariants
  (P-004).
- Pacing adapts to real-time consumption without operator
  intervention.
- Integration survives HA restarts mid-session (session recovery).
- Same pacing logic works across multiple inverter brands (P-006).

Operational (P-005):
- The system is operationally transparent — the user can understand
  what the system is doing and why without inspecting logs or code.
