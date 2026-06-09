# Alternate Solar Source for AC-Coupled Installs — Design

**Status:** approved (brainstorm 2026-06-09)
**Scope:** cloud mode only. Single implementation plan.

## Problem

On AC-coupled setups, a second inverter's generation is not included in
the FoxESS `pvPower` reading — it shows up in a separate FoxESS cloud
telemetry variable (users report `meterPower2`). The control algorithm
consumes `coordinator.data["pvPower"]` as `current_solar_kw`
(`smart_battery/listeners.py:116`), so it under-counts solar on these
installs: net consumption is overstated, which can mis-pace charge and
discharge. There is currently no way to tell the integration about the
external generation source.

(Related: the WS grid-direction inference in `foxess/realtime_ws.py`
already documents that *"unmeasured external generation makes the
predicted magnitude diverge from the actual grid reading"* — feeding the
external term into `pvPower` improves that inference as a side benefit.)

## Decisions (from brainstorm)

1. **Combine, don't replace:** total solar = FoxESS `pvPower` + the
   alternate variable's value. FoxESS units on these installs still have
   their own strings; replacing would under-count.
2. **Cloud mode, named FoxESS variable:** the alternate source is a
   FoxESS telemetry variable name (default target `meterPower2`), polled
   via `real/query`. Entity mode is out of scope (entity-mode users
   already point `pv_power_entity` at any HA sensor).
3. **REST-poll only, held across WS frames:** the variable is fetched on
   the REST poll (~5 min) and its last value reused during WS streaming
   (~5 s) sessions. Avoids coupling to the undocumented `wsmaitian` WS
   schema, which may not carry the second meter.
4. **Optional Configure-flow field:** blank by default → today's
   behaviour exactly. Not surfaced in initial setup.
5. **Add raw value as-is:** no clamping, no abs(), no sign flip. Honest
   about CT orientation; a reversed CT is surfaced (solar drops), not
   hidden. A configurable sign/scale is a possible *future* follow-up if
   users report reversed-CT issues — explicitly out of scope here.

## Config (C-035)

- New option key `additional_pv_power_variable` (string, default `""`).
- Added to `IntegrationConfig` (`domain_data.py`):
  `additional_pv_power_variable: str | None = None`, populated in
  `build_config`, read via `_cfg(hass)`. Never read raw `entry.options`.
- Surfaced as an **optional free-text field** in the cloud-mode
  options/Configure flow only. Label/help (en + all ten locales):
  *"Additional solar power variable — e.g. `meterPower2`. Leave blank
  unless a second AC-coupled inverter's generation is reported in a
  separate FoxESS variable."*
- Empty/None → no behaviour change for any existing user.

## Data flow

Single integration point: the **coordinator**. No `smart_battery/`
change — the algorithm keeps reading `coordinator.data["pvPower"]`; the
override is invisible downstream (so C-039 / C-021 unaffected, no
vendored-copy edit).

### REST poll (`coordinator._poll`, ~line 135)

```
var = _cfg(hass).additional_pv_power_variable   # None/"" when unset
variables = POLLED_VARIABLES + ([var] if var else [])
data = self.inverter.get_real_time(variables)
...
if var:
    extra_kw = _coerce_kw(data.get(var))        # None/garbage -> 0.0
    base_pv  = float(data.get("pvPower") or 0.0)
    data["pvPower"] = base_pv + extra_kw
    self._additional_pv_kw = extra_kw           # held for the WS path
```

- `_coerce_kw` returns `0.0` for missing / `None` / non-numeric, and on
  the first such occurrence logs via `record_operational_error`
  (category: config) — a typo'd variable degrades to current behaviour,
  surfaced in diagnostics, never crashes the poll.
- The sum is computed **before** the existing grid-direction / balance
  logic that consumes `pvPower`, so that inference benefits too.

### WebSocket frames (`coordinator._inject_realtime_data`)

The WS maps its own `pvPower` from the `solar` node. Before publishing
`ws_data`, add the held REST value:

```
ws_data["pvPower"] = float(ws_data.get("pvPower") or 0.0) + self._additional_pv_kw
```

- `self._additional_pv_kw` defaults to `0.0` (set in `__init__`), so WS
  frames arriving before the first REST poll add nothing — safe.
- Performed **before** the WS grid-direction balance calc, same as REST.
- During a session: FoxESS solar term stays ~5 s live; the external term
  is held from the last poll. Acceptable — additive generation, not a
  safety signal (P-001 is unaffected; this only refines solar/net-load).

## Error handling & edge cases

| Case | Behaviour |
|---|---|
| Config unset (default) | No extra variable polled; `pvPower` untouched; `_additional_pv_kw = 0.0`. Bit-for-bit current behaviour. |
| Variable name typo / not returned | `_coerce_kw` → `0.0`; first occurrence logged (config error); poll succeeds. |
| Non-numeric / `None` value | Treated as `0.0`. |
| Negative value (reversed CT) | Added as-is (raw). Not clamped — surfaces the wiring issue. |
| WS frame before first REST poll | Adds `0.0` (held default). |
| FoxESS removes/renames the variable later | Degrades to `0.0` + logged; no crash. |

## Testing (C-028 simulator over mocks)

Extend the simulator (`simulator/model.py` + `/sim/set`) to return a
configurable `meterPower2` from `real/query`, then:

1. **REST sum:** config set, sim returns `pvPower=2.0`, `meterPower2=3.0`
   → `coordinator.data["pvPower"] == 5.0`.
2. **No regression:** config unset → `meterPower2` never requested,
   `pvPower` equals the sim's `pvPower` exactly.
3. **WS hold:** after a REST poll sets `_additional_pv_kw=3.0`, a WS
   frame with `solar=1.5` → published `pvPower == 4.5`.
4. **Missing/garbage variable:** config set but sim returns no
   `meterPower2` (or a string) → `pvPower` == base `pvPower`, one
   config error recorded, poll OK.
5. **Negative:** `meterPower2=-1.0`, `pvPower=2.0` → `1.0` (raw add).
6. **Config flow:** the optional field round-trips through the
   options flow and lands in `IntegrationConfig`.

## Out of scope (YAGNI)

- Entity mode (already supports an arbitrary `pv_power_entity`).
- WS-native mapping of the second meter (REST-hold is sufficient).
- Configurable sign/scale multiplier (raw-add first; revisit only if
  reversed-CT reports arrive).
- Replace-instead-of-add mode.

## Knowledge-tree touchpoints

- **C-035** (config via `IntegrationConfig`/`_cfg`): new option follows
  the pattern.
- **C-021 / C-039** (brand-agnostic core): unaffected — the fix is
  entirely in the brand-layer coordinator; `smart_battery/` untouched.
- Consider a new **D-NNN** documenting the additive external-solar
  source and the REST-hold-across-WS decision; update
  `docs/api/foxess-cloud-api.md` to note `meterPower2` as a pollable
  variable and its AC-coupled use.
