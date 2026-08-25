# FoxESS Cloud API — Deviations from Official Documentation

This document records differences between the
[official FoxESS Open API documentation](https://www.foxesscloud.com/public/i18n/en/OpenApiDocument.html)
and the observed behaviour of the live API, discovered while building this module.

## Authentication Signature

**Documentation says:** The signature is `MD5(path + "\r\n" + token + "\r\n" + timestamp)`,
implying the separator is a carriage-return + newline (bytes `0x0D 0x0A`).

**Actual behaviour:** The separator is the **four literal characters** `\`, `r`, `\`, `n` —
not real CRLF bytes. In Python this means using a raw f-string (`fr'...'`) or escaped
backslashes (`\\r\\n`).

```python
# Correct — literal characters
signature = hashlib.md5(fr"{path}\r\n{token}\r\n{timestamp}".encode()).hexdigest()

# Wrong — actual CRLF bytes, returns errno 40256 "illegal signature"
signature = hashlib.md5(f"{path}\r\n{token}\r\n{timestamp}".encode()).hexdigest()
```

## Real-Time Variable Query (`/op/v0/device/real/query`)

**Documentation implies:** The `result` field is a flat list of `{variable, value}` objects.

**Actual behaviour:** The `result` is a list containing one object per device, each with a
nested `datas` array:

```json
[
  {
    "datas": [
      {"variable": "SoC", "value": 23.0, "name": "SoC", "unit": "%"},
      {"variable": "batChargePower", "value": 3.706, "name": "Charge Power", "unit": "kW"}
    ],
    "deviceSN": "XXXXXXXXXX",
    "time": "2026-04-07 10:44:04 AEST+1000"
  }
]
```

Each entry in `datas` also includes `name` (human-readable) and `unit` fields not
mentioned in the docs.

## Scheduler Get (`/op/v0/device/scheduler/get`)

**Documentation implies:** Returns a list of schedule groups.

**Actual behaviour:** Returns a dict with top-level `enable` flag, `groups` list, and
a `properties` object:

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
      "fdPwr": 10500
    }
  ],
  "properties": {}
}
```

The top-level `enable` field acts as a master switch for the entire scheduler.
The `properties` object has been observed as empty but its purpose is undocumented.

Unused group slots are returned with `"workMode": "Invalid"` and `"enable": 0`.

## Scheduler field ranges are declared per device (`/op/v3/device/scheduler/get`)

**Documentation says:** nothing — the `/op/v3/` scheduler namespace is undocumented.

**Actual behaviour:** `POST /op/v3/device/scheduler/get` with `{"deviceSN": "..."}`
returns, alongside the groups, a `properties` object describing **the accepted range of
every schedule-group field for that specific device**, plus the work modes it supports:

```json
{
  "enable": 1,
  "maxGroupCount": 96,
  "properties": {
    "fdpwr":        {"unit": "W", "precision": 1.0, "range": {"min": 0.0, "max": 10500.0}},
    "fdsoc":        {"unit": "%", "precision": 1.0, "range": {"min": 10.0, "max": 100.0}},
    "minsocongrid": {"unit": "%", "precision": 1.0, "range": {"min": 10.0, "max": 100.0}},
    "maxsoc":       {"unit": "%", "precision": 1.0, "range": {"min": 10.0, "max": 100.0}},
    "pvlimit":      {"unit": "W", "precision": 1.0, "range": {"min": 0.0, "max": 20000.0}},
    "importlimit":  {"unit": "W", "precision": 1.0, "range": {"min": 0.0, "max": 100000.0}},
    "exportlimit":  {"unit": "W", "precision": 1.0, "range": {"min": 0.0, "max": 100000.0}},
    "reactivepower":{"unit": "Var", "precision": 1.0, "range": {"min": -6000.0, "max": 6000.0}},
    "starthour":    {"unit": "", "precision": 1.0, "range": {"min": 0.0, "max": 23.0}},
    "workmode":     {"enumList": ["ForceDischarge", "Feedin", "ForceCharge(BAT)",
                                  "ForceDischarge(BAT)", "Backup", "SelfUse",
                                  "ForceCharge"], "unit": "", "precision": 1.0}
  }
}
```

Any value outside its declared range — and any `workMode` outside `enumList` — is
rejected by `/op/v0/device/scheduler/enable` with errno `40257` ("Parameters do not
meet expectations"). **`properties` is the authoritative source for these limits; the
`capacity * 1050` heuristic below is not.**

Note the `/op/v3/` group shape differs from `/op/v0/`: the value fields are nested
under `extraParam` (`{"startHour": .., "workMode": .., "extraParam": {"fdPwr": ..,
"minSocOnGrid": .., "maxSoc": ..}}`). This module still *writes* via
`/op/v0/device/scheduler/enable` (flat fields) and reads `/op/v3/` only for the
declared ranges.

Companion endpoint: `POST /op/v1/device/scheduler/get/flag` returns
`{"enable": true, "support": true}` — `support` is false on devices with no scheduler
at all (e.g. batteryless micro-inverters). `/op/v0/device/detail` reports the same
capability as `function: {"scheduler": true}`.

## Scheduler Enable (`/op/v0/device/scheduler/enable`) — `fdPwr` ceiling is per device

**Documentation says:** nothing about an upper bound on `fdPwr`.

**Actual behaviour:** `fdPwr` above the device's declared `fdpwr.range.max` is rejected
with errno `40257`. The `capacity * 1050` value the FoxESS app writes matches that
ceiling **only on some model families**: a KH10 reports `capacity: 10` and declares
`fdpwr.range.max: 10500`, so `10 * 1050` is exactly right. Other families declare the
plain nameplate rating, so the same arithmetic overshoots and *every* scheduler write
fails — including the SelfUse baseline written on session teardown:

| Model | `capacity` | `capacity * 1050` | declared `fdpwr` max | result |
|---|---|---|---|---|
| KH10 | 10 | 10500 | 10500 | accepted |
| H3-12.0-M | 12 | 12600 | 12000 (inferred) | **40257** |
| H3-15.0-Smart | 15 | 15750 | 15000 (inferred) | **40257** |
| EVO 10-5-H | 5 | 5250 | 5000 (inferred) | **40257** |

Always clamp `fdPwr` to the declared ceiling before writing (see C-042).

## Scheduler Enable (`/op/v0/device/scheduler/enable`) — `fdPwr` must be non-zero

**Documentation says:** `fdPwr` is the force discharge power limit in watts, with `0`
implying no limit.

**Actual behaviour:** Sending `fdPwr: 0` causes errno `40257` ("Parameters do not meet
expectations"). A positive value must always be supplied. The inverter's own schedule
consistently uses the inverter's rated power (e.g. `10500` for a 10.5 kW unit) as the
default.

The inverter's rated power can be derived from the `capacity` field in the device detail
response (`/op/v0/device/detail`), which returns the rating in kW (e.g. `10` for a KH10).
The FoxESS app uses `capacity * 1050` as the `fdPwr` value. This module queries device
detail on first use and caches the result as `Inverter.max_power_w`.

## Scheduler Enable — time segments must not overlap

**Documentation does not mention** any constraint on overlapping time windows.

**Actual behaviour:** Sending groups with overlapping time ranges causes errno `42023`
("Time overlap, please reselect time"). All groups must have non-overlapping time windows.
A "catch-all" SelfUse slot (e.g. `00:00–23:59`) cannot coexist with a narrower slot —
the SelfUse window must start after the preceding slot ends.

## Scheduler Enable — `fdSoc` minimum is 11

**Documentation does not mention** a minimum value for `fdSoc`.

**Actual behaviour:** Sending `fdSoc` below `11` causes errno `40257`. Additionally,
`minSocOnGrid` must be less than or equal to `fdSoc`, otherwise the same error occurs.
This module clamps values accordingly: `fdSoc = max(fdSoc, 11)` and
`minSocOnGrid = min(minSocOnGrid, fdSoc)`.

## Scheduler Enable — extra fields are rejected

**Documentation implies:** Only the documented group fields are relevant.

**Actual behaviour:** Groups returned by `scheduler/get` include extra fields (e.g. `id`,
`properties`) that are not accepted by `scheduler/enable`. Sending them back verbatim
causes errno `40257`. All groups must be sanitized to include only the known fields
(`enable`, `startHour`, `startMinute`, `endHour`, `endMinute`, `workMode`,
`minSocOnGrid`, `fdSoc`, `fdPwr`) before writing.

## Scheduler Get — null response when mode set via app

**Documentation implies:** `scheduler/get` always returns a schedule object.

**Actual behaviour:** When the work mode has been set via the FoxESS mobile app (rather
than the scheduler API), `scheduler/get` returns `null` in the `result` field instead of
a schedule object. This module normalises `null` to `{"enable": 0, "groups": []}`.

## Scheduler — groups may be auto-disabled after their time window

**Documentation does not mention** any automatic state changes to schedule groups.

**Observed behaviour:** Groups appear to be auto-disabled (`enable` set to `0`) by the
system after their scheduled time window passes for the day. However, they remain in the
schedule and are re-activated the following day. This means filtering on `enable` to
identify "active" groups will incorrectly drop recurring daily schedules that have already
run today. This module filters on `workMode` instead, treating only `"Invalid"` groups as
API placeholders.
