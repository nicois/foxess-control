---
project: FoxESS Control
audience: foxess-local gateway developers
status: requested (not yet implemented in the gateway)
written: 2026-11-19
against: foxess-local v0.1.1, /api/v1/openapi.yaml 1.0.0
---

# What the foxess-control local backend needs from the gateway

The foxess-control integration is gaining a **local backend** that talks to
foxess-local instead of the FoxESS cloud. The gateway's current API
covers it except for two things: **lifetime energy counters** and
**write-budget visibility**. This document says what the integration
expects for each, precisely enough to implement and test against.

Units follow the existing API: watts as integers. New energy fields are
kWh (see §1).

## 1. Lifetime energy counters

### Why

The integration's Energy-dashboard sensors are lifetime counters. The
smart-discharge **feed-in energy target** (`feedin_target_kwh`) is also
paced from one: the session records the feed-in counter at its start and
stops once `counter − start ≥ target`. Today these come from the FoxESS
cloud's cumulative variables. The gateway has only instantaneous power
and `/history` totals over fixed ranges. Those totals can't serve
either use, because they aren't monotonic and they reset with the range.

### Fields

Add an `energy` object to `State`, so it appears in `GET /state` and in
`ConfigEvent.state`:

| Field | Meaning | Cloud equivalent | Priority |
|---|---|---|---|
| `grid_export_kwh` | Energy exported to the grid | `feedin` | **required** — paces the feed-in target |
| `grid_import_kwh` | Energy imported from the grid | `gridConsumption` | required |
| `pv_kwh` | Photovoltaic yield only | `PVEnergyTotal` | required |
| `battery_charge_kwh` | Energy into the battery | `chargeEnergyToTal` | required |
| `battery_discharge_kwh` | Energy out of the battery | `dischargeEnergyToTal` | required |
| `load_kwh` | Household consumption | `loads` | nice to have |
| `output_kwh` | Inverter AC output (PV + battery discharge) | `generation` | nice to have |

Plus `energy.as_of` (date-time): when the counters were last read.

### Contract

1. **Source.** Prefer the inverter's own lifetime-total registers, so
   the values match the FoxESS app and survive gateway restarts. If the
   gateway integrates power for any field instead, it must persist the
   running total and say so per field. An `energy.sources` map
   (`register` | `integrated`) would do.
2. **Monotonic.** A counter never decreases. If the inverter's register
   goes backwards (inverter replaced, firmware reset), report the new
   value. Also emit a `system` event (`type: counter-reset`, the field in
   `detail`), so the integration can re-baseline instead of booking a
   negative delta.
3. **Nulls.** Each field is `null` until its first read, and `null`
   (not 0) when this model doesn't report it. The integration shows
   `null` as *unavailable*.
4. **Freshness.** Read at least every **60 s**: the discharge listener
   ticks every 60 s and checks the feed-in target on each tick.
   Resolution of **0.1 kWh** or finer.
5. **Streaming.** Include `energy` in the `snapshot` SSE event whenever
   it has changed since the last snapshot that carried it, or in every
   snapshot (it's small). The integration doesn't want to poll
   `/state` for counters while it's subscribed to `/events`.

## 2. Write-budget visibility

### Why

The gateway accepts at most 10 confirmed writes per setting per hour and
refuses the rest with HTTP 429. Smart discharge adjusts the
force-discharge power every 60 s, writing whenever it moves by 500 W or
more, and every adjustment is a `PUT /schedule`. Hitting a 429 halfway
through a session is dangerous. The integration counts it as an adapter
error, and three in a row open its circuit breaker. Worse, a 429 could
refuse a **safety** write: suspending the discharge at the minimum SoC
or before grid import (foxess-control's P-001 / P-002).

The integration will pace itself against the budget. It will coarsen
its power adjustments as the budget drains and always keep the last
writes for safety actions. To do that it has to *see* the budget.

### Fields

Add `write_budget` to `State`, so it appears in `GET /state` and in
`ConfigEvent.state`. It's keyed by the same feature names as
`identity.writes`:

```json
"write_budget": {
  "schedule":   {"limit": 10, "remaining": 7, "window_s": 3600, "next_free_at": "2026-11-19T01:12:00+11:00"},
  "work_mode":  {"limit": 10, "remaining": 10, "window_s": 3600, "next_free_at": null},
  "power_limits": { ... }
}
```

- `remaining`: real writes still allowed in the current window.
- `next_free_at`: when the oldest counted write leaves the window, so
  `remaining` goes up by one. `null` when `remaining == limit`.
- Only budgeted settings appear. A setting that isn't listed is
  treated as unbudgeted.

### Contract

1. **Which writes count.** Say so in the spec. The integration
   assumes: one `PUT /schedule` counts as **one** `schedule` write,
   however many groups changed. A write whose value already matches the
   inverter (not sent, per `/schedule`'s docs) counts **zero**. A
   refused write (4xx/5xx before sending) counts zero. A `504` with
   `applied: unknown` counts one.
2. **Changes push.** Each counted write's `ConfigEvent` carries the
   updated `write_budget`, so the integration doesn't have to re-read
   `/state`.
3. **The 429 response** is a Problem with `code: write_budget`, the
   `setting`, `remaining: 0`, `next_free_at`, and a `Retry-After`
   header.
4. **The limit is configurable** (ideally per setting), and the
   configured value is what `limit` reports. The integration reads
   `limit` and never hard-codes 10.

## 3. Spec discrepancies noticed while reading v1.0.0

Not blocking. These came up while mapping the live responses to the
schema:

- `429` and `code: write_budget` appear in the descriptions of
  `/current-limits`, `/port-limits`, `/battery-currents`,
  `/power-limit` and `/eps-mode`. They're missing from those responses
  lists, from `/schedule`'s and `/work-mode`'s (which the home-assistant
  doc says are rationed too), and from the `Problem.code` enum.
- `GET /state` returns a top-level `ts` that `State` doesn't declare.
- `GET /config` returns `forward_mode` that `Config` doesn't declare.
- `GET /compatibility` returns `layout` and `third_party` that
  `CompatibilityReport` doesn't declare.
- `Config.time_zone` reads `"Local (AEDT)"` on a live install, so the
  IANA name isn't available. The integration compares schedule windows
  in HA's time zone, so an IANA name (e.g. `Australia/Sydney`) would let
  it detect a host/HA time-zone mismatch.
