---
project: FoxESS Control
audience: contributors implementing or maintaining FoxESS Cloud API clients (any language)
sources: API_DEVIATIONS.md, docs/wasm-signature.md, foxess/inverter.py, foxess/web_session.py, knowledge/04-design/foxess-api.md
last_verified: 2026-05-10
---

# FoxESS Cloud API — Reference

Language-agnostic reference for the FoxESS Cloud API. Audience: anyone
implementing a JavaScript / TypeScript / Go / Rust / etc. client.

This document is the consolidation of the reverse-engineering notes
gathered while building the Python integration in this repository.
Where invariants are cited inline (e.g. `C-008`, `D-014`) the reference
is the project's constraint and design-decision register; the
"Cross-references" section at the end of this document maps each
section back to the canonical Python source so a maintainer can
re-verify a claim without grepping.

---

## 1. Overview

FoxESS Cloud exposes **two distinct API surfaces**. They share a host
suffix but have different auth, different paths, and different
intended consumers.

| Surface | Host | Path prefix | Documented? | Auth | Use case |
|---|---|---|---|---|---|
| Open API | `open.foxesscloud.com` | `/op/v0/*` | Yes (FoxESS public docs) | API key + HMAC-MD5 signature | Schedule management, polled telemetry |
| Web-portal API | `www.foxesscloud.com` | `/dew/v0/*`, `/basic/v0/*` | No (extracted from web-portal JS) | Username + MD5(password) → session token + WASM signature | Real-time WebSocket, BMS cell temperature |

### Which surface do I need?

For most automation use cases — including a multi-tenant SaaS that
manages charge / discharge schedules — only the **Open API** is
required. It supports:

- Reading and writing the inverter's schedule (the central control
  surface; one API call sets work mode, time windows, SoC targets,
  and force-discharge power).
- Polling battery state (SoC, charge / discharge power, voltage,
  current, etc.) at intervals no faster than ~60s.
- Reading device metadata (model, rated capacity in kW, etc.).

The **web-portal API** adds two things the Open API does not provide:

- A real-time WebSocket (`/dew/v0/wsmaitian`) that streams telemetry
  every ~5 seconds. Useful for fine-grained closed-loop control.
- BMS cell temperature via `/dew/v0/device/detail` (the Open API's
  `batTemperature` is the inverter's own enclosure sensor and
  diverges from cell temperature by 5–10 °C in winter — see D-033).

Web-portal credentials are tied to a single user account and the
session token has a 12-hour TTL. They are awkward to hold in a
multi-tenant context: each tenant must surrender their FoxESS portal
password (or its MD5 digest), and the WASM signature module must run
per-tenant. **Most SaaS deployments will skip the web-portal API
entirely** and use Open API polling at the documented cadence. The
sections that follow are structured accordingly: the Open API
sections (2, 4, 5, 6) are the load-bearing ones; the web-portal
section (3) is included for completeness.

### Response envelope

Both surfaces use the same envelope shape:

```json
{
  "errno": 0,
  "msg": "success",
  "result": { /* endpoint-specific */ }
}
```

`errno` is 0 on success; any non-zero value is a failure. `msg` is
human-readable. `result` is endpoint-specific and may be `null`
(see §6 and §8 for the cases where this surprises clients).

### Out of scope for this document

- The real-time WebSocket framing and message shapes. The WebSocket
  exists at `wss://www.foxesscloud.com/dew/v0/wsmaitian?plantId=...&token=...`
  and emits ~5s telemetry; clients that need it should treat each
  message as authoritative for `timeDiff <= 30s` (older messages are
  stale, per `C-005`). Pacing-cadence rules are documented separately
  in `docs/coarse-pacing-rules.md`.
- Modbus / local-network access. Out of scope; not part of the cloud
  API.

---

## 2. Auth — Open API

### Obtaining credentials

The user must:

1. Log in to the FoxESS Cloud web portal.
2. Navigate to *Account → API Management* and generate an **API
   Key** (an opaque string, treated as the bearer secret).
3. Note the **device serial number** (`deviceSN`) of the inverter
   they want to control. The serial is visible in the portal's
   device list and on the inverter's physical label.

The API key is the only credential the Open API needs. It is **not**
an OAuth token — it does not expire and cannot be scoped or
revoked individually. Treat it as a long-lived shared secret.

### Request signing

Every Open API request carries a signature in headers. The signature
proves the request was assembled by a holder of the API key.

Required headers on every request:

| Header | Value |
|---|---|
| `token` | The API key, verbatim |
| `timestamp` | Current Unix epoch in **milliseconds**, decimal string |
| `lang` | Language code, typically `en` |
| `signature` | MD5 hex digest computed from path + token + timestamp |
| `Content-Type` | `application/json` for POST bodies |

The `signature` value is computed as:

```
signature = lowercase_hex(MD5(path + SEP + token + SEP + timestamp))
```

where:

- `path` is the request path **without** scheme, host, or query
  string. For example, for `POST https://open.foxesscloud.com/op/v0/device/scheduler/enable`,
  `path` is `/op/v0/device/scheduler/enable`.
- `token` is the API key.
- `timestamp` is the same epoch-ms string used in the `timestamp`
  header. Signing and header **must use the same value**.
- `SEP` is the four-character literal sequence `\r\n` (backslash, `r`,
  backslash, `n`) — **not** the two-byte CRLF sequence (`0x0D 0x0A`).
  This deviates from the official documentation, which describes the
  separator as CRLF. Sending real CRLF bytes returns
  `errno 40256 "illegal signature"`.

The MD5 digest is rendered as a 32-character lowercase hex string.

#### Sample signature payload

For:

- path = `/op/v0/device/real/query`
- token = `abcdef0123456789`
- timestamp = `1715000000000`

The string fed to MD5 is the literal:

```
/op/v0/device/real/query\r\n abcdef0123456789\r\n 1715000000000
```

(without the spaces — shown here only for legibility; the actual
input has no whitespace around the separators).

Encoded as UTF-8, this is 67 bytes: 25 bytes of path, 4 bytes of
literal `\r\n`, 16 bytes of token, 4 more bytes of literal `\r\n`,
13 bytes of timestamp string.

The MD5 digest of that byte sequence, lowercase hex, is what goes
into the `signature` header.

### Why the `\r\n` confusion?

The signature algorithm is **FoxESS-internal**. It was originally
implemented inside the FoxESS web portal's obfuscated JavaScript and
later mirrored in the Open API documentation. The docs describe the
separator using the two-character escape sequence `\r\n`, which a
human reader naturally interprets as CRLF; the actual implementation
treats those four characters as a literal byte sequence in the
plaintext.

Any language can implement the Open API signature with a few lines
of code: concatenate the strings using literal `\r\n`, take the MD5,
hex-encode it. The Python wrapper used by this repository does
exactly that and is one line long. **The WASM module (`signature.wasm`)
referenced elsewhere in this codebase is for the web-portal API only
and is not needed here** — it is required only because the web-portal
signature algorithm is more elaborate and is not specified in any
public document. See `docs/wasm-signature.md` for that case; it does
not apply to Open API signing.

### Error returns related to auth

| `errno` | Meaning |
|---|---|
| `40256` | Illegal signature (most often: wrong separator, mis-encoded path, or signature timestamp ≠ header timestamp) |
| `40257` | Parameters do not meet expectations (often surfaces auth-adjacent failures with malformed payloads) |
| `41808` | Invalid token |
| `41809` | Expired signature (timestamp skew, or stale token) |

`41808` and `41809` should be retried **once** with a fresh signature
(and, if applicable, a fresh API key — see D-042 for the web-portal
analogue).

---

## 3. Auth — Web-portal API

The web-portal API is the surface the user's browser uses when
logged into `www.foxesscloud.com`. It is **not** required for
schedule management; skip this section unless you need the
WebSocket telemetry stream or BMS cell temperature.

### Login flow

1. **POST** `https://www.foxesscloud.com/basic/v0/user/login`
2. Body:

   ```json
   {
     "user": "<username>",
     "password": "<MD5_hex_of_password>",
     "type": 1,
     "verification": 1
   }
   ```

   `password` must be the **lowercase 32-char MD5 hex digest** of the
   raw password, *not* the raw password.

3. Headers: every web-portal request — including login — must carry
   the WASM-computed `signature` header (see below).

4. On success the response envelope has `errno: 0` and
   `result.token` is the session token. The token is then sent in
   the `token` header on subsequent requests, **and** it is included
   in the input to subsequent signature computations.

The token has an effective TTL of ~12 hours. Cache it; refresh
proactively. Errno `41808` (invalid) or `41809` (expired) on any
follow-up request means re-login.

### WASM signature

The web-portal API requires a `signature` header generated by an
algorithm that is **only** present in the portal's WebAssembly
module. The algorithm:

- takes `(path, token, lang, timestamp_ms)` as input
- produces an opaque hex string (~40 chars) as output
- is compiled (with Emscripten) into a `signature.wasm` binary
  served by the portal

Reverse-engineering it from obfuscated WASM bytecode is a moving
target. Two viable approaches:

1. **Run the WASM as-is** — load the same `signature.wasm` extracted
   from the portal in any WebAssembly runtime (wasmtime, wasmer,
   Node's built-in WASM, browser `WebAssembly`). This is what this
   project does (Python + wasmtime). Total module size: ~16 KB.
2. **Reimplement in your language of choice** — possible but fragile;
   the algorithm depends on internal state that has historically
   been altered when FoxESS rotates portal builds.

A pure-Go or pure-JS reimplementation is strictly possible. The WASM
exists as a maintenance choice, not because the algorithm is somehow
WASM-only. JavaScript clients running in the browser can simply
import and call the same WASM the portal already loads.

### Not for SaaS

A web-portal session token is bound to:

- A single user account
- A single source IP (loosely — there is some leniency)
- A single 12-hour window

It is therefore impractical for a multi-tenant SaaS to hold
web-portal sessions on behalf of all users: each tenant must hand
over their FoxESS password (or MD5), the SaaS must store an MD5 of
each tenant's password, and the SaaS becomes responsible for token
rotation per tenant. Most SaaS clients will deliberately stick to
the Open API and accept the slower (~60s) polling cadence.

---

## 4. Endpoint inventory

This section lists the Open API endpoints needed for schedule
management, plus the two web-portal endpoints relevant for
optional features. Endpoints not load-bearing for the SaaS use
case are omitted.

### `POST /op/v0/plant/list`

Discovery / health check. Returns the list of plants (sites) the
API key has access to. Used as the canonical "is the API key
valid?" probe, since a 0-errno response with any plant list confirms
both signature correctness and key validity.

Request body: `{}` is sufficient; pagination fields (`currentPage`,
`pageSize`) are accepted.

Response (`result`):

```json
{
  "data": [
    {"plantId": "...", "name": "...", ...}
  ],
  "currentPage": 1,
  "pageSize": 10,
  "total": 1
}
```

### `GET /op/v0/device/detail?sn=<deviceSN>`

Per-device metadata. The most important field is `capacity`, the
inverter's rated power **in kW** as an integer (e.g. `10` for a
10 kW unit). The FoxESS app writes `capacity * 1050` watts as the
default force-discharge power, but **that is not a reliable ceiling
for `fdPwr` on every model** — see
`POST /op/v3/device/scheduler/get` below for the authoritative,
device-declared limit (`C-042`).

Other useful fields: `deviceType` (model name string),
`hasBattery`, `hasPV`, `status`, `productType`, and
`function` (a capability map, e.g. `{"scheduler": true}` — false on
devices with no scheduler at all, such as batteryless
micro-inverters).

The response is single-shot per session for most clients; cache the
result. Capacity does not change at runtime.

### `POST /op/v0/device/real/query`

Pull current values for a list of telemetry variables. Request
body:

```json
{
  "sn": "<deviceSN>",
  "variables": ["SoC", "batChargePower", "batDischargePower"]
}
```

Response shape (`result`):

```json
[
  {
    "deviceSN": "<deviceSN>",
    "time": "2026-04-07 10:44:04 AEST+1000",
    "datas": [
      {"variable": "SoC", "value": 23.0, "name": "SoC", "unit": "%"},
      {"variable": "batChargePower", "value": 3.706, "name": "Charge Power", "unit": "kW"}
    ]
  }
]
```

Two things to know that diverge from the public docs:

- `result` is a **list** with one entry per device, each entry has a
  nested `datas` array. The docs imply a flat `[{variable, value}]`.
- Each `datas` entry has `name` (human-readable) and `unit` fields
  not mentioned in the docs. Treat extra fields as advisory.

**Instantaneous power variables** (kW floats): `SoC` (%),
`batChargePower`, `batDischargePower`, `loadsPower`, `pvPower`,
`pv1Power`, `pv2Power`, `pv3Power`, `pv4Power`,
`gridConsumptionPower`, `feedinPower`,
`generationPower` (inverter AC *output*, not PV — see the
`generation` / `PVEnergyTotal` note below), `meterPower`,
`meterPower2`, `epsPower`,
`batVolt` (V), `batCurrent` (A), `batTemperature` /
`ambientTemperation` / `invTemperation` (°C), `RVolt` / `RCurrent` /
`RFreq`.

`meterPower2` — Second grid-meter / CT channel. On AC-coupled installs
this commonly carries a separate inverter's generation; the integration
can be configured (cloud-mode option `additional_pv_power_variable`) to
add it to `pvPower` so the control algorithm sees true total generation.
Sign depends on CT orientation; the integration adds it raw (no clamp).

**Cumulative energy counters** (kWh, lifetime, monotonic
`total_increasing` — take the delta between two readings for an
interval, or use the underlying meter's own statistics):

| Variable | Meaning |
|---|---|
| `generation` | Lifetime inverter **AC output** energy — everything the inverter put out, whatever the source. **Not** photovoltaic yield: it rises while the battery discharges, with zero sun. Use `PVEnergyTotal` for solar. |
| `PVEnergyTotal` | Lifetime **photovoltaic-only** yield — the genuine solar counter, and the correct HA Energy-dashboard *solar* source. Not reported by every model. |
| `chargeEnergyToTal` | Lifetime **battery charge** energy *(note the `ToTal` capitalisation — a FoxESS API quirk, not a typo to "fix")* |
| `dischargeEnergyToTal` | Lifetime **battery discharge** energy (everything the battery put out, into house load **and** export combined) |
| `feedin` | Lifetime **grid feed-in (export)** energy — energy exported to the grid, i.e. discharge/solar **beyond** household self-consumption |
| `gridConsumption` | Lifetime grid **import** energy |
| `loads` | Lifetime house-load energy |
| `energyThroughput` | Lifetime total throughput |

**`dischargeEnergyToTal` vs `feedin` — do not confuse them.**
Battery *discharge* energy (`dischargeEnergyToTal`) is everything the
battery delivered, most of which typically serves house load.
Grid *feed-in* energy (`feedin`) is only the portion exported to the
grid. The smart-discharge "feed-in energy limit" / `feedin_target_kwh`
control input (see `docs/control/smart-discharge-contract.md`) is
measured from **`feedin`**, NOT from `dischargeEnergyToTal` — a
discharge-energy cap would strand usable battery energy and cannot
measure export. Both variables exist and are pollable; pick the one
that matches the quantity you actually want to bound.

**`generation` vs `PVEnergyTotal` — do not confuse them either.**
The variable catalogue (`GET /op/v0/device/variable/get`) labels
`generation` "Cumulative power generation" and `generationPower`
"Output Power", which reads like solar but is not: both are the
inverter's AC **output** side. On a live KH10 over one night
(2026-08-25 18:00 → 2026-08-26 08:00 AEST, `pvPower` flat at 0 and the
battery discharging), `generation` rose 18.2 kWh while `PVEnergyTotal`
rose 0.2 kWh — and Δ`generation` reconciled exactly as Δ`loads` +
Δ`feedin` − Δ`gridConsumption`. Wiring `generation` into an energy
dashboard as the *solar* source therefore double-counts battery
discharge (once as solar, once as battery). Use `PVEnergyTotal`.

**`todayYield` is not a substitute.** The catalogue labels it "Today's
power generation" (unit `kW`, which is already suspicious for a yield
figure) and it reads **0.0** on the KH10 — for all 167 samples of the
night above, and it is omitted entirely from `real/query` responses.
Do not rely on it; derive today's yield from `PVEnergyTotal` deltas.

**Unsupported variables are omitted, not rejected.** Requesting a
variable the device does not report (or a misspelled name — the names
are case-sensitive) does **not** fail the request: `errno` stays `0`
and the variable is simply absent from `datas`. Probed read-only
against a live KH10 on 2026-08-26 with a deliberately bogus name
alongside real ones; requesting only unsupported names returns
`result: []`. This is what makes a shared poll list containing
model-specific variables such as `PVEnergyTotal` safe — consumers must
treat absence as "unavailable", never as zero.

Power values from this endpoint are in **kilowatts** (floats). Note
this differs from the WebSocket and from schedule writes — see §8.
This list reflects the integration's `POLLED_VARIABLES`
(`custom_components/foxess_control/const.py`); FoxESS may expose
further variables not polled here.

### `POST /op/v0/device/scheduler/enable`

**The central control surface.** Replaces the inverter's schedule
with the supplied groups. This is how you switch work mode, set
force-charge / force-discharge windows, change the on-grid SoC
floor, etc.

Request body:

```json
{
  "deviceSN": "<deviceSN>",
  "groups": [
    {
      "enable": 1,
      "startHour": 0,
      "startMinute": 0,
      "endHour": 23,
      "endMinute": 59,
      "workMode": "SelfUse",
      "minSocOnGrid": 15,
      "fdSoc": 100,
      "fdPwr": 10500
    }
  ]
}
```

Schedule group invariants are documented in §5. The response is the
empty envelope (`errno: 0`, `result: null` or `{}`); a `0` errno is
the signal of success.

This endpoint is **full-replacement**: every write replaces the
entire schedule. There is no patch / merge endpoint. Read-modify-write
is the contract — see §5.

### `POST /op/v0/device/scheduler/get`

Read the current scheduler configuration. Request body:

```json
{ "deviceSN": "<deviceSN>" }
```

Response (`result`):

```json
{
  "enable": 1,
  "groups": [
    {
      "enable": 1,
      "startHour": 0,
      "startMinute": 0,
      "endHour": 23,
      "endMinute": 59,
      "workMode": "SelfUse",
      "minSocOnGrid": 15,
      "fdSoc": 100,
      "fdPwr": 10500,
      "id": "<some_id>",
      "properties": {}
    }
  ],
  "properties": {}
}
```

The top-level `enable` flag is the master scheduler switch. The
groups list contains all configured slots, including unused
"placeholder" slots (recognisable by `workMode: "Invalid"` and
`enable: 0`) that the inverter pre-allocates. Group dicts also
contain extra fields (`id`, `properties`) that the write endpoint
**rejects** if echoed back.

When the work mode was set via the FoxESS mobile app rather than the
scheduler API, this endpoint returns `result: null`. Treat `null`
as the empty schedule `{"enable": 0, "groups": []}` — see §6 and §8.

### The Mode Scheduler master switch

**Master switch.** `POST /op/v1/device/scheduler/get/flag` returns
`{"enable": bool, "support": bool}` — whether Mode Scheduler is on, and
whether the device supports it at all. `POST /op/v0/device/scheduler/set`
with `{"deviceSN": …, "enable": 0|1}` sets it.

Removing every group does **not** turn the switch off (issue #16: FoxCloud
still showed the inverter as scheduler-controlled with no groups left).
Whether `scheduler/enable` turns the switch *on* from off is **unverified**
against real hardware — so the integration enables it explicitly before
writing groups rather than relying on the coupling.

Schedule groups are **inert while the switch is off**: the inverter
ignores the group list and behaves as SelfUse. This is a silent failure —
`scheduler/enable` still answers `errno 0` and `scheduler/get` still
returns the groups — so a client that assumes the coupling writes
schedules that never fire, with nothing to show the user. Verifying the
coupling on real hardware would mean writing to a production home
battery, which is why the integration removes the dependency on the
answer instead: `Inverter._ensure_scheduler_enabled` issues
`scheduler/set enable=1` before every `scheduler/enable`, and tolerates
failure (the endpoint is absent on some firmware/regions, and a device
reporting `support: false` rejects it) so a failed enable can never abort
a write that would otherwise have worked.

Note the two distinct `enable` flags: the top-level `enable` of
`scheduler/get` reflects the schedule, whereas this master switch is
separate device state that survives an empty group list.

### `POST /op/v0/device/setting/{get,set}` — direct device settings

Undocumented by FoxESS. Reaches the inverter's **own settings**, entirely
bypassing the Mode Scheduler. `get` takes `{"sn": …, "key": …}` and `set`
takes `{"sn": …, "key": …, "value": …}` — note `sn`, where the scheduler
endpoints take `deviceSN`.

`get` returns the current value **plus what the device declares it will
accept**. Shapes verified read-only against a live KH10 (2026-08-26):

```json
// key: "WorkMode"
{"enumList": ["PeakShaving", "Feedin", "Backup", "SelfUse"],
 "unit": "", "precision": 1.0, "value": "SelfUse"}

// key: "MinSocOnGrid"
{"unit": "%", "precision": 1.0, "range": {"min": 0.0, "max": 100.0}, "value": "11"}

// key: "MinSoc"
{"unit": "%", "precision": 1.0, "range": {"min": 0.0, "max": 100.0}, "value": "0"}
```

The shape varies by key: `WorkMode` carries an `enumList` and no `range`,
the SoC keys the reverse. `value` is a **string** even for numeric
settings, and `set` takes a string too. Do not assume either key is
present.

Two properties of this surface are load-bearing, and explain design
constraints that otherwise look arbitrary:

**1. The direct `WorkMode` enumeration has no forced modes.** There is no
`ForceCharge` and no `ForceDischarge` here — only `PeakShaving`, `Feedin`,
`Backup`, `SelfUse` (and it offers `PeakShaving`, which the schedule
enumeration does not). A forced charge or discharge can *only* be
expressed as a schedule group, so any client doing battery scheduling must
keep using the Mode Scheduler for its sessions no matter what else it
does. The direct surface can govern the **idle** state and nothing more.
This is worth an assertion in a test suite rather than a comment: if
FoxESS ever adds forced modes here, that constraint disappears.

**2. `MinSocOnGrid` accepts 0 here, but not in a schedule group.** The
same device that declares `range.min = 0.0` for the `MinSocOnGrid`
*setting* declares `minsocongrid.range.min = 10.0` in
`/op/v3/device/scheduler/get` (§5), and rejects a group below it with
`errno 40257`. The 10 % floor is therefore a **Mode Scheduler
restriction, not a hardware limit** — the only way a lower floor can hold
is to turn the master switch off and set the value directly.

Because both endpoints reach device state that no schedule group touches,
they are independent of the scheduler in both directions: writing a
setting neither creates nor alters groups, and writing groups does not
move `WorkMode` or `MinSocOnGrid`. A client must not confuse a work-mode
*setting* write with a work-mode *schedule group* write; they are
different control paths onto the same inverter.

`MinSocOnGrid` / `MinSoc` are the same device values that
`/op/v0/device/battery/soc/{get,set}` reads and writes — two API surfaces
onto one register, not two registers.

### Read-modify-write summary

To change one part of the schedule (say, switch to ForceCharge for
the next hour) the client must:

1. `GET` the current schedule.
2. Drop placeholder groups (`workMode: "Invalid"`).
3. Strip unknown fields from each remaining group.
4. Merge / replace as needed.
5. Verify invariants (§5).
6. `POST` the full sanitised group list back to `scheduler/enable`.

### Web-portal endpoints (optional)

- **`POST /basic/v0/user/login`** — login, returns session token.
  See §3.
- **`GET /dew/v0/device/detail?id=<batteryCompoundId>&category=battery`** —
  BMS cell temperature lookup, where `batteryCompoundId` is
  `{batteryId}@{batSn}` discovered from the WebSocket `bat` node.
  Temperature is at `result.battery.temperature.value`. See `D-033`
  for the design rationale; do not use the Open API's
  `batTemperature` as a substitute (it reports the inverter
  enclosure temperature, not BMS cells, and diverges by up to 10 °C
  in winter).

The real-time WebSocket at `/dew/v0/wsmaitian` is not documented in
this reference; see `docs/coarse-pacing-rules.md` for cadence rules
and the live-trace harness for sample messages.

---

## 5. Schedule format

This is the load-bearing section of the document. Get this wrong and
writes either silently corrupt the schedule or are rejected with
opaque error codes.

### Group structure

A schedule group is a JSON object with **exactly these nine fields**
on the wire:

| Field | Type | Range | Meaning |
|---|---|---|---|
| `enable` | int | `0` or `1` | `1` = active, `0` = disabled-but-retained |
| `startHour` | int | `0`–`23` | Window start hour (account-local time) |
| `startMinute` | int | `0`–`59` | Window start minute |
| `endHour` | int | `0`–`23` | Window end hour (account-local time) |
| `endMinute` | int | `0`–`59` | Window end minute |
| `workMode` | string | enum below | Behaviour during the window |
| `minSocOnGrid` | int | `0`–`100` | Minimum SoC the inverter will hold while on grid (%) |
| `fdSoc` | int | `11`–`100` | Force-discharge / force-charge target SoC (%) |
| `fdPwr` | int | `1`–declared max W | Force-discharge / force-charge power **in watts** |

The `minSocOnGrid` / `fdSoc` / `fdPwr` ranges above are the *general*
shape; the exact bounds are **declared per device** — see
`POST /op/v3/device/scheduler/get`.

### `POST /op/v3/device/scheduler/get` — declared field ranges

Undocumented by FoxESS. Body `{"deviceSN": "<sn>"}`. Returns the
schedule plus a `properties` map giving, for **this specific device**,
the accepted range of every group field and the work modes it
supports:

```json
{
  "enable": 1,
  "maxGroupCount": 96,
  "properties": {
    "fdpwr":        {"unit": "W", "precision": 1.0, "range": {"min": 0.0, "max": 10500.0}},
    "fdsoc":        {"unit": "%", "precision": 1.0, "range": {"min": 10.0, "max": 100.0}},
    "minsocongrid": {"unit": "%", "precision": 1.0, "range": {"min": 10.0, "max": 100.0}},
    "workmode":     {"enumList": ["SelfUse", "Feedin", "Backup",
                                  "ForceCharge", "ForceDischarge"]}
  }
}
```

Values outside a declared range, and work modes outside `enumList`,
are rejected by `scheduler/enable` with `errno 40257`. **Clamp to
these ranges before writing** (`C-042`): `capacity * 1050` matches
`fdpwr.range.max` on a KH10 but overshoots on H3 / EVO families,
where the declared ceiling is the plain nameplate rating — the cause
of "40257 on every write" reports (issues #12, #14, #17).

Group shape differs from `/op/v0/`: value fields are nested under
`extraParam`. Read this endpoint for the ranges; keep writing via
`/op/v0/device/scheduler/enable` with flat fields.

Companion: `POST /op/v1/device/scheduler/get/flag` →
`{"enable": true, "support": true}` — the Mode Scheduler master switch;
see §4 "The Mode Scheduler master switch".

The declared `minsocongrid.range.min` of `10.0` is a restriction of the
scheduler alone: the *setting* of the same name accepts `0`. Likewise the
`workmode` `enumList` here is the only place forced modes appear. See §4
"Direct device settings" for both asymmetries.

### `workMode` enum

Observed values, all UpperCamelCase strings:

| Value | Behaviour |
|---|---|
| `SelfUse` | Default. Battery follows house load. |
| `ForceCharge` | Charge battery up to `fdSoc` from grid + PV. |
| `ForceDischarge` | Discharge battery down to `fdSoc`, capped at `fdPwr` watts. |
| `Feedin` | Prioritise grid export. |
| `Backup` | Reserve battery for outage. **Do not overwrite without explicit user consent — see C-018 / D-016.** |
| `Invalid` | API placeholder for unused slots. Drop on read; never write. |

### Hard invariants

These are enforced by the API and produce opaque rejections if
violated. **Validate them client-side before writing.**

- **`fdSoc >= 11`** (`C-008`). Sending `fdSoc < 11` returns
  `errno 40257`. Clamp client-side.
- **`minSocOnGrid <= fdSoc`** (`C-008`). Same error code on
  violation. Clamp `minSocOnGrid = min(minSocOnGrid, fdSoc)`.
- **`fdPwr > 0`** (`C-008`-adjacent). Sending `fdPwr: 0` returns
  `errno 40257` despite the docs implying `0` means "no limit". The
  FoxESS app writes `capacity_kw * 1050` watts as the default; do
  the same. (`capacity_kw` comes from `/op/v0/device/detail`.)
- **End time strictly after start time** (`C-009`). Each group must
  satisfy `endHour:endMinute > startHour:startMinute`. **Schedules
  must not cross midnight.** A "22:00–06:00" window must be split
  into two groups: `22:00–23:59` and `00:00–06:00`.
- **No overlapping windows.** All groups must have non-overlapping
  time ranges. A 00:00–23:59 SelfUse "catch-all" cannot coexist
  with a narrower slot (e.g. 18:00–21:00 ForceDischarge); the
  SelfUse window must end before the narrower slot begins. Errno
  `42023` ("Time overlap, please reselect time") on violation.
- **`fdPwr <= declared ceiling`** (`C-042`). Above the device's
  `properties.fdpwr.range.max` the write returns `errno 40257`.
  `capacity_kw * 1050` exceeds that ceiling on several model
  families, so clamp to the declared value when it is available.
- **`fdPwr` is in watts.** Write `10500` for 10.5 kW. This
  contradicts user intuition (especially users coming from
  `foxess_modbus`, which expresses the same field in kW). Always
  multiply by 1000 if your internal value is in kW.

### Critical: writes are full-replacement (read-modify-write)

`scheduler/enable` does not merge with the existing schedule. It
**replaces** the entire group list. To change one slot you must:

1. **Read** with `scheduler/get`.
2. **Sanitise** each group:
   - Drop groups whose `workMode == "Invalid"` (placeholders).
   - Strip every field not in the nine-field list above. The read
     endpoint returns extras (`id`, `properties`, etc.); the write
     endpoint rejects them with `errno 40257`. (`D-014`.)
   - Clamp `fdSoc = max(fdSoc, 11)`.
   - Clamp `minSocOnGrid = min(minSocOnGrid, fdSoc)`.
3. **Merge / replace** as needed for the change you want to make.
4. **Re-validate** invariants (§5 hard invariants).
5. **Write** with `scheduler/enable`.

The full-replacement model means: if you skip a group on the way
back, you lose it. Always echo back unrelated groups (after
sanitisation) when modifying one.

### Auto-disabled groups (gotcha)

Groups whose time window has passed for the day appear in the
`scheduler/get` response with `enable: 0`. They are **not** "deleted
and should be dropped"; FoxESS auto-flips them off so they don't
re-trigger today, then re-enables them tomorrow. Filtering on
`enable == 1` to identify "active" groups will silently destroy any
recurring daily slot whose window has already elapsed.

The right filter is `workMode != "Invalid"`. Treat only `Invalid`
as the placeholder marker.

### Preserving `Backup` mode

If `scheduler/get` returns any group with `workMode: "Backup"`,
**refuse to write** unless the user has explicitly opted into
overwriting it (`C-018` / `D-016`). Backup mode is a user-configured
outage protection setting; silently overwriting it could leave a
home unprotected during a power cut. The safe default for an
automation tool is to surface an error and require explicit
acknowledgement.

### Sample sanitised group list

Minimum legal write — single SelfUse all day:

```json
{
  "deviceSN": "ABCD1234567890",
  "groups": [
    {
      "enable": 1,
      "startHour": 0,
      "startMinute": 0,
      "endHour": 23,
      "endMinute": 59,
      "workMode": "SelfUse",
      "minSocOnGrid": 11,
      "fdSoc": 11,
      "fdPwr": 10500
    }
  ]
}
```

A force-discharge window at peak rate, 18:00–21:00, with
SelfUse on either side:

```json
{
  "deviceSN": "ABCD1234567890",
  "groups": [
    {
      "enable": 1, "startHour": 0, "startMinute": 0,
      "endHour": 17, "endMinute": 59,
      "workMode": "SelfUse",
      "minSocOnGrid": 15, "fdSoc": 11, "fdPwr": 10500
    },
    {
      "enable": 1, "startHour": 18, "startMinute": 0,
      "endHour": 20, "endMinute": 59,
      "workMode": "ForceDischarge",
      "minSocOnGrid": 15, "fdSoc": 20, "fdPwr": 5000
    },
    {
      "enable": 1, "startHour": 21, "startMinute": 0,
      "endHour": 23, "endMinute": 59,
      "workMode": "SelfUse",
      "minSocOnGrid": 15, "fdSoc": 11, "fdPwr": 10500
    }
  ]
}
```

Note: the second group's `fdSoc: 20` means "stop discharging when
SoC reaches 20 %". `fdPwr: 5000` caps discharge at 5 kW. The
algorithm's higher-level `peak_consumption * 1.5` floor (`C-001`) is
**not** the API's concern — it's the client's responsibility to
ensure `fdPwr` is large enough to never need grid import during
the window.

---

## 6. Response handling

### Envelope

```json
{
  "errno": 0,
  "msg": "success",
  "result": ...
}
```

- `errno: 0` — success. `result` is the payload.
- `errno != 0` — failure. `msg` carries a human message; `result` is
  usually `null`.

### Common error codes

| `errno` | Meaning | Recommended action |
|---|---|---|
| `0` | Success | continue |
| `40256` | Illegal signature | Check separator (literal `\r\n`), header/sig timestamp match, path encoding. Don't retry blindly. |
| `40257` | Parameters do not meet expectations | Inspect schedule invariants (§5). Don't retry blindly. |
| `40400` | Rate limited | Back off, retry with delay. (`RATE_LIMIT_RETRIES` in this codebase.) |
| `41808` | Invalid token | Re-auth once, retry once. (`D-042`.) |
| `41809` | Expired signature | Re-sign with fresh timestamp; retry once. |
| `42023` | Time overlap | Fix overlapping windows (§5). Don't retry. |

Transient HTTP-level errors (502, 503) should also be retried with
backoff (this codebase uses `TRANSIENT_RETRIES` ≈ 3).

### `result: null` on success

Several endpoints return `result: null` with `errno: 0` as a normal
case:

- `scheduler/get` when the work mode was last set via the FoxESS
  mobile app (rather than the scheduler API). Treat as
  `{"enable": 0, "groups": []}`.
- Some write endpoints (`scheduler/enable`) on success. Treat as
  acknowledgement; do not assume failure.

### 3-strike circuit-breaker pattern

The Python integration in this repository implements a **3-strike
circuit breaker** at the consumer layer (`C-024`): three consecutive
adapter errors trip the breaker, holding the inverter's last
position; five further failed ticks abort the session and revert to
`SelfUse`. This is **consumer policy**, not API contract — there is
no rule from the API that requires this — but it is a recommended
pattern for any client that runs unattended. Reasoning: the FoxESS
cloud occasionally returns spurious errors on otherwise healthy
sessions; a single failure should not yank control from the user,
but a sustained outage should fail safe.

---

## 7. Rate limiting

The Open API does not publish a rate-limit budget. Empirically:

- **Polling (`real/query`, `scheduler/get`, etc.)**: do not poll
  faster than once per **60 seconds** per device. Bursts of
  high-frequency polls (e.g. `> 1 / second`) tend to start
  returning `errno 40400` after a few requests.
- **Writes (`scheduler/enable`)**: bursts of writes are rejected.
  The integration in this repository enforces a **5-second minimum
  inter-call interval** as a soft floor and writes far less
  frequently in practice (one write per pacing decision, typically
  every several minutes).
- **Per-account ceilings**: not published; not observed to bite a
  single-device client running at the cadences above. Multi-device
  clients running the WebSocket plus aggressive Open API polling
  have been observed to trip rate limits.

The `simulator/server.py` in this repository models the observed
rate-limit behaviour and is the recommended target for client
development. Use it before pointing your client at the live API.

---

## 8. Quirks

A non-exhaustive list of things that surprise client implementers.

### Time zones

Schedule windows (`startHour:startMinute`–`endHour:endMinute`) are
**account-local time**, not UTC. The account's time zone is
configured in the FoxESS portal (typically the inverter's physical
location). Two consequences:

- The client must know the account's local TZ to compute "is the
  current time inside this window?". Real-time queries return the
  device timestamp suffixed with the offset
  (e.g. `2026-04-07 10:44:04 AEST+1000`), which is sometimes the
  cleanest way to discover the configured TZ.
- DST transitions can produce gaps or overlaps. The API does not
  perform DST adjustment for schedule windows; if you write
  `02:00–03:00` on a spring-forward day, the window may execute
  for zero minutes. Avoid windows that straddle DST.

### Units inconsistency

Same physical quantity, different units depending on endpoint:

| Source | `batChargePower` etc. | `fdPwr` |
|---|---|---|
| `/op/v0/device/real/query` | kilowatts (float) | n/a |
| `/op/v0/device/scheduler/enable` (write) | n/a | **watts (int)** |
| `/op/v0/device/scheduler/get` (read) | n/a | watts (int) |
| WebSocket `/dew/v0/wsmaitian` | watts (string!) | n/a |

The WebSocket transmits power values as **strings** that must be
parsed as integers and treated as watts (`C-004`). Schedule writes
use **integer watts** for `fdPwr`. Real-time query returns floats
in kilowatts. Convert at the client boundary; do not propagate
mixed units inward.

### `null` minSocOnGrid in some reads

Some accounts return `minSocOnGrid: null` (rather than an integer)
in `scheduler/get` group data, generally only on placeholder slots
or slots created by the FoxESS app. Treat `null` as "unset" and
substitute the system minimum (`11`) before echoing back.

### `result` is sometimes a list, sometimes a dict

`real/query` returns `result` as a **list of devices**, each with a
nested `datas` list. `scheduler/get` returns `result` as a **dict**.
Don't write a generic envelope unwrapper that assumes one shape.

### Placeholder groups

The inverter pre-allocates a fixed number of group slots (typically
8 on KH-series units). Slots not in use are returned as
`{"workMode": "Invalid", "enable": 0, ...}`. Drop them before
echoing back; the write endpoint rejects `Invalid` as a workMode
value.

### SoC as percentage strings on some endpoints

A handful of less-common endpoints return SoC as `"23"` (string)
rather than `23` (number) or `23.0` (float). Defensively coerce to
int / float at the parse boundary.

### WebSocket: stale messages

The real-time WebSocket sometimes sends backfilled messages with
`timeDiff > 30s` after a reconnect. **Discard** any message with
`timeDiff > 30s` (`C-005`); using stale data for control decisions
can fight the user (the integration writes a setting based on
30-second-old reality, then writes it again 5 s later when fresh
data arrives, hammering the API).

### `fdPwr` cannot be zero

Despite the documentation implying `fdPwr: 0` means "no limit", the
API rejects `0` with `errno 40257`. The FoxESS mobile app uses
`capacity_kw * 1050` watts (e.g. `10500` for a 10 kW unit) as the
"unlimited" sentinel. Cache `capacity_kw` from `/op/v0/device/detail`
and use the same product.

---

## 9. Implementation tips

### Test against the simulator

This repository ships a FoxESS Cloud API simulator at
`simulator/server.py`. It models:

- The signature algorithm (so the client's signing code is exercised).
- The schedule invariants (rejects `fdSoc < 11`,
  `fdPwr == 0`, midnight crossings, overlaps).
- The placeholder-group quirk and `null`-on-app-mode-set behaviour.
- Approximate rate-limit thresholds.

Develop and test against the simulator before pointing the client at
production. The simulator catches the largest class of integration
bugs (schedule sanitisation, `\r\n` separator, units mismatches)
without consuming live API quota.

### Don't poll faster than 60s

Even if your client *can* poll faster, the inverter's underlying
data refresh rate is typically 30–60 s. Polling at 1 Hz produces
the same numbers repeatedly and burns rate-limit budget.

### Don't write `fdSoc < 11` or midnight crossings

These produce opaque `errno 40257`. Validate client-side. Splitting
a midnight-crossing window into two groups is the canonical fix.

### Always round-trip through sanitisation

Even if you've just written the schedule and "know" what's in it,
re-read before the next write. The mobile app, the web portal, or
another integration may have changed the schedule between writes.
Read-modify-write with a sanitisation pass is the only safe pattern.

### Cache, but bound the cache

Cache `capacity_kw` (it doesn't change). Don't cache the schedule
itself across writes — re-read each time. Don't cache `errno`
responses either; some are transient.

### Use the project's events / replay harness if available

The Python integration emits `SCHEDULE_WRITE` structured events at
two layers (intent + wire) for replay debugging — see `D-049`. A
non-Python client implementing the same surface may want a similar
record so production write traces can be replayed against the
simulator for regression testing.

---

## 10. Cross-references

Each section above is grounded in a specific place in the Python
implementation. Consult these when re-verifying a claim.

| Section | Canonical Python source |
|---|---|
| §1 Overview | `custom_components/foxess_control/foxess/client.py` (Open API client), `custom_components/foxess_control/foxess/web_session.py` (web-portal client) |
| §2 Auth — Open API | `custom_components/foxess_control/foxess/client.py::_signed_headers`; deviation notes in `API_DEVIATIONS.md` (Authentication Signature) |
| §3 Auth — web-portal API | `custom_components/foxess_control/foxess/web_session.py::async_login`, `_make_headers`; signature wrapper at `custom_components/foxess_control/foxess/signature.py`; rationale in `docs/wasm-signature.md` |
| §4 Endpoint inventory | `custom_components/foxess_control/foxess/inverter.py` (`get_schedule`, `set_schedule`, `set_work_mode`, `get_real_time`, `get_current_mode`, `auto_detect`, `get_scheduler_flag`, `set_scheduler_enabled`); `web_session.py::async_get_battery_temperature` |
| §5 Schedule format | `custom_components/foxess_control/__init__.py::_sanitize_group`, `_merge_with_existing`; `custom_components/foxess_control/foxess/inverter.py::_post_schedule`; type definitions in `custom_components/foxess_control/smart_battery/types.py` (`ScheduleGroup`, `WorkMode`); deviation notes in `API_DEVIATIONS.md` (Scheduler sections) |
| §6 Response handling | `custom_components/foxess_control/foxess/client.py` (errno mapping, retry logic); circuit breaker in `custom_components/foxess_control/smart_battery/session.py` (search for `C-024`) |
| §7 Rate limiting | `custom_components/foxess_control/foxess/client.py` (`RATE_LIMIT_RETRIES`, `TRANSIENT_RETRIES`, `MIN_REQUEST_INTERVAL`); simulator at `simulator/server.py` |
| §8 Quirks | `API_DEVIATIONS.md` (single source); `_parse_real_time` in `custom_components/foxess_control/foxess/inverter.py`; WebSocket stale handling in `custom_components/foxess_control/foxess/realtime_ws.py` |
| §9 Implementation tips | `simulator/server.py`; `D-049` in `docs/knowledge/04-design/foxess-api.md` |

Constraint and design-decision register:

- **Constraints** (`C-NNN`): `docs/knowledge/02-constraints.md`
- **Design decisions** (`D-NNN`): `docs/knowledge/04-design/foxess-api.md`
  (this surface), and the broader knowledge tree at
  `docs/knowledge/04-design/`

The `API_DEVIATIONS.md` file at the repository root is the single
canonical record of how the live API diverges from the published
documentation; it is the source of truth for §5 and §8 above.
