# Alternate Solar Source (AC-coupled) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a cloud-mode user name an extra FoxESS telemetry variable (default target `meterPower2`) whose value is *added* to `pvPower`, so the control algorithm sees true total generation on AC-coupled installs where a second inverter's output is not in the FoxESS `pvPower` reading.

**Architecture:** A new optional brand-layer config key `additional_pv_power_variable`, surfaced in the cloud-mode options flow and read via `IntegrationConfig`/`_cfg` (C-035). The coordinator's REST poll fetches the named variable and adds it to `data["pvPower"]`, caching the value on the coordinator; the WS inject path reuses that cached value so streaming frames also reflect total solar. Entirely brand-layer (`custom_components/foxess_control/`) — `smart_battery/` and the algorithm are untouched (the algorithm keeps reading `coordinator.data["pvPower"]`).

**Tech Stack:** Home Assistant config-options flow (voluptuous + HA selectors), the FoxESS cloud `real/query` poll path in `coordinator.py`, the `inject_realtime_data` WS path, pytest with the project simulator (C-028).

**Spec:** `docs/superpowers/specs/2026-06-09-alternate-solar-source-design.md`

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `custom_components/foxess_control/const.py` | add `CONF_ADDITIONAL_PV_POWER_VARIABLE` | brand-layer config key (cloud-specific — NOT in `smart_battery/const.py`, per C-021) |
| `custom_components/foxess_control/domain_data.py` | add field to `IntegrationConfig` + populate in `build_config` | snapshot the option (C-035) |
| `custom_components/foxess_control/coordinator.py` | `__init__` cache field; `_fetch_all` append+sum; `inject_realtime_data` add cached value | the data plumbing (sum into `pvPower`) |
| `custom_components/foxess_control/config_flow.py` | extend the options schema in `async_step_init` | optional cloud-only field (mirrors the `CONF_WS_MODE` extend pattern) |
| `custom_components/foxess_control/strings.json` + `translations/*.json` | option label/description | localised UI text |
| `tests/test_alternate_solar_source.py` | new | REST sum, no-regression, WS hold, garbage, negative |
| `tests/test_config_flow.py` (or existing options-flow test) | add case | field round-trips into `IntegrationConfig` |

**Architectural note (read before Task 4):** `battery_options_schema` lives in the brand-agnostic `smart_battery/config_flow_base.py`. Do **NOT** add this FoxESS-cloud-specific field there — it would couple the brand-agnostic core to a FoxESS variable name (violates C-021/C-039). Add it by `.extend()`-ing the schema inside the brand-layer `config_flow.py` `async_step_init`, exactly as `CONF_WS_MODE` is added today.

---

### Task 1: Config key + IntegrationConfig field

**Files:**
- Modify: `custom_components/foxess_control/const.py`
- Modify: `custom_components/foxess_control/domain_data.py`
- Test: `tests/test_alternate_solar_source.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_alternate_solar_source.py`:

```python
"""Alternate solar source (AC-coupled): additive extra PV variable."""
from __future__ import annotations

from custom_components.foxess_control.const import (
    CONF_ADDITIONAL_PV_POWER_VARIABLE,
)
from custom_components.foxess_control.domain_data import build_config


def test_additional_pv_variable_defaults_to_none() -> None:
    cfg = build_config({})
    assert cfg.additional_pv_power_variable is None


def test_additional_pv_variable_read_from_options() -> None:
    cfg = build_config({CONF_ADDITIONAL_PV_POWER_VARIABLE: "meterPower2"})
    assert cfg.additional_pv_power_variable == "meterPower2"


def test_additional_pv_variable_blank_is_none() -> None:
    # An empty string in options must normalise to None (no extra poll).
    cfg = build_config({CONF_ADDITIONAL_PV_POWER_VARIABLE: ""})
    assert cfg.additional_pv_power_variable is None
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_alternate_solar_source.py -q`
Expected: `ImportError` / `AttributeError` (`CONF_ADDITIONAL_PV_POWER_VARIABLE` and the field don't exist yet).

- [ ] **Step 3: Add the config key**

In `custom_components/foxess_control/const.py`, near the other brand-layer `CONF_*` definitions (e.g. just after `CONF_WS_MODE = "ws_mode"` at line ~122), add:

```python
CONF_ADDITIONAL_PV_POWER_VARIABLE = "additional_pv_power_variable"
```

- [ ] **Step 4: Add the IntegrationConfig field + populate it**

In `custom_components/foxess_control/domain_data.py`, add a field to the `IntegrationConfig` dataclass (after `export_limit_entity`):

```python
    additional_pv_power_variable: str | None = None
```

Then in `build_config`, add to the imports block:

```python
        CONF_ADDITIONAL_PV_POWER_VARIABLE,
```

and in the `return IntegrationConfig(` call, add the argument (normalise blank → None):

```python
        additional_pv_power_variable=(
            entry_options.get(CONF_ADDITIONAL_PV_POWER_VARIABLE) or None
        ),
```

(The `or None` makes `""` → `None`, matching `export_limit_entity`'s pattern.)

- [ ] **Step 5: Run — expect PASS**

Run: `pytest tests/test_alternate_solar_source.py -q`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add custom_components/foxess_control/const.py custom_components/foxess_control/domain_data.py tests/test_alternate_solar_source.py
git commit -m "feat: additional_pv_power_variable config key + IntegrationConfig field (AC-coupled solar)"
```

---

### Task 2: REST poll sums the alternate variable into pvPower

**Files:**
- Modify: `custom_components/foxess_control/coordinator.py` (`__init__`, `_fetch_all`)
- Test: `tests/test_alternate_solar_source.py`

**Context:** `_fetch_all` (coordinator.py ~line 133) calls `self.inverter.get_real_time(POLLED_VARIABLES)`. We append the configured variable to that list, then add its value to `pvPower`. We cache the extra kW on the coordinator (`self._additional_pv_kw`) for the WS path (Task 3). `_fetch_all` runs in an executor job and has access to `self.hass`, so it can call `_cfg(self.hass)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_alternate_solar_source.py`:

```python
from unittest.mock import MagicMock

from custom_components.foxess_control.coordinator import FoxessControlCoordinator


def _coerce_kw(value: object) -> float:
    """Mirror of the production helper, for test clarity."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _make_coordinator(hass, inverter) -> FoxessControlCoordinator:
    return FoxessControlCoordinator(hass, inverter, update_interval_seconds=300)


def test_fetch_all_sums_extra_variable_into_pvpower(monkeypatch) -> None:
    # hass with an IntegrationConfig naming meterPower2
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )

    inverter = MagicMock()
    # Simulator/inverter returns base pv + the extra meter.
    inverter.get_real_time.return_value = {"pvPower": 2.0, "meterPower2": 3.0}
    inverter.get_current_mode.return_value = None

    coord = _make_coordinator(hass, inverter)
    data = coord._fetch_all()

    assert data["pvPower"] == 5.0
    assert coord._additional_pv_kw == 3.0
    # The extra variable was actually requested from the API.
    requested = inverter.get_real_time.call_args.args[0]
    assert "meterPower2" in requested


def test_fetch_all_unset_does_not_request_or_change(monkeypatch) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({})  # unset
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    inverter.get_real_time.return_value = {"pvPower": 2.0}
    inverter.get_current_mode.return_value = None

    coord = _make_coordinator(hass, inverter)
    data = coord._fetch_all()

    assert data["pvPower"] == 2.0
    assert coord._additional_pv_kw == 0.0
    requested = inverter.get_real_time.call_args.args[0]
    assert "meterPower2" not in requested


def test_fetch_all_garbage_extra_value_adds_zero(monkeypatch) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    # Variable absent from response (typo / not supported) and a string case.
    inverter.get_real_time.return_value = {"pvPower": 2.0}  # meterPower2 missing
    inverter.get_current_mode.return_value = None

    coord = _make_coordinator(hass, inverter)
    data = coord._fetch_all()
    assert data["pvPower"] == 2.0
    assert coord._additional_pv_kw == 0.0


def test_fetch_all_negative_extra_added_raw(monkeypatch) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    inverter.get_real_time.return_value = {"pvPower": 2.0, "meterPower2": -1.0}
    inverter.get_current_mode.return_value = None

    coord = _make_coordinator(hass, inverter)
    data = coord._fetch_all()
    assert data["pvPower"] == 1.0  # raw add, no clamp
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_alternate_solar_source.py -k fetch_all -q`
Expected: FAIL — `coord._additional_pv_kw` does not exist; `pvPower` not summed; extra variable not requested.

- [ ] **Step 3: Add the cache field in `__init__`**

In `coordinator.py` `FoxessControlCoordinator.__init__` (the first `__init__`, ~line 55), after `self._ws_feedin_power_kw: float = 0.0`, add:

```python
        # Last REST-polled value of the optional additional PV variable
        # (AC-coupled second inverter, e.g. meterPower2), in kW. Held so
        # WS frames can also reflect total solar between polls. 0.0 when
        # the feature is unconfigured or the variable is missing.
        self._additional_pv_kw: float = 0.0
```

- [ ] **Step 4: Implement the sum in `_fetch_all`**

In `coordinator.py`, add a module-level helper (near the top, after imports) and use it in `_fetch_all`. First, the helper:

```python
def _coerce_kw(value: Any) -> float:
    """Coerce a polled value to float kW; missing/non-numeric -> 0.0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
```

Then replace the body of `_fetch_all` so it reads the config, extends the
variable list, and sums:

```python
    def _fetch_all(self) -> dict[str, Any]:
        """Fetch real-time data and work mode in a single executor job."""
        from ._helpers import _cfg

        extra_var = _cfg(self.hass).additional_pv_power_variable
        variables = (
            [*POLLED_VARIABLES, extra_var] if extra_var else POLLED_VARIABLES
        )
        data = self.inverter.get_real_time(variables)

        missing = [v for v in POLLED_VARIABLES if v not in data]
        if missing:
            _LOGGER.debug("Polled variables missing from API response: %s", missing)

        if extra_var:
            extra_kw = _coerce_kw(data.get(extra_var))
            if extra_var not in data:
                _LOGGER.debug(
                    "Additional PV variable %r not in API response; adding 0",
                    extra_var,
                )
            self._additional_pv_kw = extra_kw
            data["pvPower"] = _coerce_kw(data.get("pvPower")) + extra_kw

        try:
            mode = self.inverter.get_current_mode()
            data["_work_mode"] = mode.value if mode is not None else None
        except Exception:
            _LOGGER.debug("Failed to fetch work mode, skipping", exc_info=True)
            data["_work_mode"] = None

        return data
```

(Note: `Any` is already imported in coordinator.py — confirm; it is used throughout. `_cfg` is imported locally to avoid a circular import, matching the existing local `from ._helpers import _cfg` usage elsewhere in this file.)

- [ ] **Step 5: Run — expect PASS**

Run: `pytest tests/test_alternate_solar_source.py -k fetch_all -q`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add custom_components/foxess_control/coordinator.py tests/test_alternate_solar_source.py
git commit -m "feat: REST poll sums additional PV variable into pvPower (AC-coupled solar)"
```

---

### Task 3: WS inject adds the held additional value

**Files:**
- Modify: `custom_components/foxess_control/coordinator.py` (`inject_realtime_data`)
- Test: `tests/test_alternate_solar_source.py`

**Context:** `inject_realtime_data` (coordinator.py ~line 501) builds `ws_data` (which already contains a WS-mapped `pvPower` from the `solar` node) and merges it into `self.data`. We add `self._additional_pv_kw` to the WS `pvPower` BEFORE the merge/publish and before any grid-direction balance use, so streaming frames reflect total solar. Must happen on a fresh copy (the method already does `ws_data = dict(ws_data)` in places — ensure we don't mutate the caller's dict).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_alternate_solar_source.py`:

```python
def test_ws_inject_adds_held_additional_value(monkeypatch) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    coord = _make_coordinator(hass, inverter)
    coord.data = {"pvPower": 0.0}
    # Simulate a prior REST poll having cached the external term.
    coord._additional_pv_kw = 3.0

    # A WS frame reports the FoxESS solar node as 1.5 kW.
    coord.inject_realtime_data({"pvPower": 1.5})

    assert coord.data["pvPower"] == 4.5


def test_ws_inject_zero_held_value_no_change(monkeypatch) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({})  # unset → _additional_pv_kw stays 0.0
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    coord = _make_coordinator(hass, inverter)
    coord.data = {"pvPower": 0.0}

    coord.inject_realtime_data({"pvPower": 1.5})

    assert coord.data["pvPower"] == 1.5
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_alternate_solar_source.py -k ws_inject -q`
Expected: FAIL — `ws_inject_adds_held_additional_value` gets `1.5`, not `4.5`.

- [ ] **Step 3: Implement the WS add**

In `coordinator.py` `inject_realtime_data`, near the top of the method body (immediately after the initial guard / normalisation that ensures `ws_data` is a fresh dict — i.e. before any grid-direction or merge logic that reads `pvPower`), add:

```python
        # AC-coupled additional solar: add the last REST-polled external
        # PV term to the WS solar reading so streaming frames also reflect
        # total generation. _additional_pv_kw is 0.0 when unconfigured or
        # before the first REST poll, so this is a no-op for everyone else.
        if self._additional_pv_kw and "pvPower" in ws_data:
            ws_data = dict(ws_data)
            ws_data["pvPower"] = (
                float(ws_data.get("pvPower") or 0.0) + self._additional_pv_kw
            )
```

IMPORTANT placement: this must run BEFORE the change-detection `all(...)` short-circuit and before any code that reads `ws_data["pvPower"]` for grid-direction inference. If `inject_realtime_data` reads `ws_data` into local vars early, insert this block above that point. Read the full method (lines ~501–622) and place it as the first transformation of `ws_data`.

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_alternate_solar_source.py -k ws_inject -q`
Expected: 2 passed.

- [ ] **Step 5: Run the whole new test file + coordinator suite (no regression)**

Run: `pytest tests/test_alternate_solar_source.py tests/test_coordinator.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/foxess_control/coordinator.py tests/test_alternate_solar_source.py
git commit -m "feat: WS inject adds held additional PV value to pvPower (AC-coupled solar)"
```

---

### Task 4: Optional field in the cloud-mode options flow

**Files:**
- Modify: `custom_components/foxess_control/config_flow.py` (`async_step_init`)
- Test: `tests/test_config_flow.py` (add a case; create the file only if it does not exist — check first with `ls tests/test_config_flow.py`)

**Context (architectural):** Add the field by `.extend()`-ing the schema in `async_step_init` — NOT in `battery_options_schema` (that's the brand-agnostic `smart_battery/config_flow_base.py`; adding a FoxESS variable name there violates C-021). Mirror the existing `CONF_WS_MODE` extend block. Surface it only in the options flow (not first-time setup), per the spec.

- [ ] **Step 1: Write the failing test**

First check whether an options-flow test exists: `ls tests/test_config_flow.py`. Add this test to it (or create the file with the standard imports if absent):

```python
async def test_options_flow_additional_pv_variable_roundtrips(hass) -> None:
    """The additional PV variable entered in options lands in IntegrationConfig."""
    from custom_components.foxess_control.const import (
        CONF_ADDITIONAL_PV_POWER_VARIABLE,
        DOMAIN,
    )
    from custom_components.foxess_control.domain_data import build_config

    # Build options as the flow would persist them.
    options = {CONF_ADDITIONAL_PV_POWER_VARIABLE: "meterPower2"}
    cfg = build_config(options)
    assert cfg.additional_pv_power_variable == "meterPower2"

    # And the schema must EXPOSE the field with the right key, defaulting
    # to the current option value.
    from custom_components.foxess_control.config_flow import (
        _additional_pv_schema_dict,
    )

    schema_keys = {str(k) for k in _additional_pv_schema_dict({}).keys()}
    assert CONF_ADDITIONAL_PV_POWER_VARIABLE in schema_keys
```

(The test references a small helper `_additional_pv_schema_dict` so the schema fragment is unit-testable without driving the full HA flow. Define it in Step 3.)

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_config_flow.py -k additional_pv -q`
Expected: FAIL — `_additional_pv_schema_dict` does not exist.

- [ ] **Step 3: Implement the schema fragment + wire it into `async_step_init`**

In `config_flow.py`, add the import at the top (with the other `const` imports):

```python
    CONF_ADDITIONAL_PV_POWER_VARIABLE,
```

Add a small helper near the top of the module (after imports):

```python
def _additional_pv_schema_dict(opts: dict[str, Any]) -> dict[Any, Any]:
    """Schema fragment for the optional AC-coupled additional PV variable.

    Cloud-only, brand-specific — kept out of the brand-agnostic
    battery_options_schema (C-021). Blank default preserves current
    behaviour.
    """
    return {
        vol.Optional(
            CONF_ADDITIONAL_PV_POWER_VARIABLE,
            default=opts.get(CONF_ADDITIONAL_PV_POWER_VARIABLE, ""),
        ): str,
    }
```

Then in `async_step_init`, extend the schema (add this right after the existing `schema = battery_options_schema(self._config_entry)` line, before the `CONF_WS_MODE` block):

```python
        schema = schema.extend(
            _additional_pv_schema_dict(self._config_entry.options)
        )
```

- [ ] **Step 4: Run — expect PASS**

Run: `pytest tests/test_config_flow.py -k additional_pv -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/config_flow.py tests/test_config_flow.py
git commit -m "feat: optional additional-PV-variable field in cloud options flow (AC-coupled solar)"
```

---

### Task 5: UI strings + localisation

**Files:**
- Modify: `custom_components/foxess_control/strings.json`
- Modify: `custom_components/foxess_control/translations/en.json` and the other locale files (de, es, fr, it, ja, nl, pl, pt, zh-Hans)

**Context:** HA renders options-flow field labels from `options.step.init.data` / `data_description` in `strings.json` (and the per-locale `translations/*.json`). Add the new key's label + description so it isn't shown as the bare key.

- [ ] **Step 1: Find the options-flow `init` step block**

Run: `grep -n '"init"' custom_components/foxess_control/strings.json`
Read the surrounding `options` → `step` → `init` → `data` block.

- [ ] **Step 2: Add the label + description (en / strings.json)**

In `strings.json`, under `options.step.init.data`, add:

```json
"additional_pv_power_variable": "Additional solar power variable"
```

and under `options.step.init.data_description` (create the block if absent), add:

```json
"additional_pv_power_variable": "Optional. For AC-coupled systems where a second inverter's generation is reported in a separate FoxESS variable (e.g. meterPower2). Its value is added to the solar reading. Leave blank otherwise."
```

Mirror the same two entries into `translations/en.json`.

- [ ] **Step 3: Localise into the nine other locales**

For each of `translations/{de,es,fr,it,ja,nl,pl,pt,zh-Hans}.json`, add the same two keys with a translated label + description. (Translate the meaning; keep the literal token `meterPower2` unchanged.)

- [ ] **Step 4: Validate all JSON parses**

Run:
```bash
python3 -c "import json,glob; [json.load(open(f)) for f in glob.glob('custom_components/foxess_control/translations/*.json')]; json.load(open('custom_components/foxess_control/strings.json')); print('JSON OK')"
```
Expected: `JSON OK`

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/strings.json custom_components/foxess_control/translations/
git commit -m "i18n: localise additional-PV-variable option label + description"
```

---

### Task 6: Simulator integration test (end-to-end sum)

**Files:**
- Modify: `simulator/model.py` and/or `simulator/server.py` (only if needed to serve `meterPower2`)
- Test: `tests/test_alternate_solar_source.py`

**Context (C-028):** Prefer the simulator over mocks for the end-to-end path. The sim already serves arbitrary `real/query` variables from its model state (`/sim/set`). Confirm whether `meterPower2` is already settable; if the sim only returns a fixed variable set, extend it to echo a configurable `meterPower2`. This task proves the real `get_real_time` → `_fetch_all` → `pvPower` path, not just mocked returns.

- [ ] **Step 1: Check whether the simulator can already serve meterPower2**

Run: `grep -n "meterPower2\|real/query\|handle_real_query\|datas" simulator/server.py simulator/model.py`
If `handle_real_query` returns values for *any requested variable from model state*, no sim change is needed. If it returns a fixed dict, note which variables and extend in Step 3.

- [ ] **Step 2: Write the failing integration test**

Append to `tests/test_alternate_solar_source.py` (follow the pattern other tests in the repo use to start the simulator — search an existing E2E/integration test for the simulator fixture, e.g. `grep -rln "simulator" tests/ | head`; reuse that fixture):

```python
import pytest


@pytest.mark.asyncio
async def test_simulator_real_query_sums_meterpower2(sim_inverter) -> None:
    """End-to-end: sim returns meterPower2; get_real_time includes it.

    `sim_inverter` is the project's simulator-backed Inverter fixture
    (reuse the existing one; see other simulator tests for its name/import).
    Adjust the fixture name to match the repo's convention.
    """
    # Seed the sim: base pv 2.0 kW, meterPower2 3.0 kW.
    await sim_inverter.sim_set(pvPower=2.0, meterPower2=3.0)

    result = sim_inverter.get_real_time(["pvPower", "meterPower2"])
    assert result["pvPower"] == 2.0
    assert result["meterPower2"] == 3.0
```

NOTE: if the repo has no reusable simulator-backed `Inverter` fixture at the unit-test layer (the simulator may only be wired into the containerised E2E suite), then SKIP creating a new simulator unit fixture and instead rely on the mock-based `_fetch_all` tests from Task 2 (which already exercise the sum logic), and add the end-to-end coverage to the E2E suite per `tests/` conventions. Report which path you took.

- [ ] **Step 3: If needed, extend the simulator to serve meterPower2**

Only if Step 1 showed `handle_real_query` returns a fixed variable set: in `simulator/model.py` add a `meter_power2` field (default 0.0) and have the real-query handler in `simulator/server.py` include `meterPower2` when requested; expose it via `/sim/set`. Keep changes minimal and match the existing variable-serving pattern.

- [ ] **Step 4: Run — expect PASS (or documented SKIP)**

Run: `pytest tests/test_alternate_solar_source.py -k simulator -q`
Expected: PASS, or the test is omitted with the Step-2 NOTE rationale recorded in the commit message.

- [ ] **Step 5: Commit**

```bash
git add tests/test_alternate_solar_source.py simulator/
git commit -m "test: simulator end-to-end sum of meterPower2 into pvPower (AC-coupled solar)"
```

---

### Task 7: Docs — knowledge tree + API reference

**Files:**
- Modify: `docs/api/foxess-cloud-api.md`
- Modify/Create: `docs/knowledge/04-design/` (a D-NNN note) and `CHANGELOG.md`

- [ ] **Step 1: Document meterPower2 in the API reference**

In `docs/api/foxess-cloud-api.md`, in the `real/query` variables section (the power-variables list / energy-counter table added earlier), add `meterPower2` with a note: *"Second grid-meter / CT channel. On AC-coupled installs this commonly carries a separate inverter's generation; the integration can be configured to add it to `pvPower` (see `additional_pv_power_variable`)."*

- [ ] **Step 2: Add a design note**

Add a short design entry (next free D-NNN) in the appropriate `docs/knowledge/04-design/` file (the one covering coordinator/data sourcing) recording: the additive external-solar source, cloud-only scope, REST-poll-with-WS-hold decision, raw-add (no clamp), and that it's brand-layer-only (algorithm reads `pvPower` unchanged). Cite C-035, C-021.

- [ ] **Step 3: Changelog**

In `CHANGELOG.md`, under `## Unreleased` (create the section if absent, above the top released version), add an `### Added` bullet:

```markdown
- **Additional solar source for AC-coupled installs** (cloud mode). A new optional config `additional_pv_power_variable` (e.g. `meterPower2`) names a FoxESS telemetry variable whose value is added to `pvPower`, so the control algorithm sees true total generation when a second inverter's output is reported in a separate variable rather than the FoxESS `pvPower` reading. REST-polled and held across WebSocket frames; raw additive; off by default (blank). As a side benefit, feeding the external term into `pvPower` improves the WS grid-direction inference that previously diverged on unmeasured external generation.
```

- [ ] **Step 4: Commit**

```bash
git add docs/ CHANGELOG.md
git commit -m "docs: document additional_pv_power_variable (AC-coupled solar) + meterPower2"
```

---

### Task 8: Full verification

**Files:** none (verification).

- [ ] **Step 1: Full unit suite**

Run: `pytest tests/ -m "not slow" --tb=short`
Expected: all pass (existing count + the new `test_alternate_solar_source.py` cases).

- [ ] **Step 2: Pre-commit (ruff/mypy/semgrep/module-size/vendored-sync)**

Run: `pre-commit run --all-files`
Expected: all hooks pass. In particular the semgrep C-039 rule must NOT fire — confirm no `smart_battery/` file was touched (this feature is brand-layer only).

- [ ] **Step 3: Confirm no smart_battery/ change**

Run: `git diff --name-only v1.0.21-beta.3..HEAD | grep smart_battery; echo "exit:$?"`
Expected: no matches (`exit:1`) — the algorithm core is untouched, per the spec.

---

## Self-Review

**1. Spec coverage:**
- Add (sum) to pvPower → Tasks 2 (REST) + 3 (WS). ✔
- Cloud mode, named FoxESS variable → config key (T1), poll append (T2). ✔
- REST-poll only, held across WS → cache field (T2) + WS add of held value (T3). ✔
- Optional Configure-flow field, blank default → T4 (extend in brand layer, not the agnostic schema). ✔
- Raw add (no clamp/abs/sign) → T2 negative-value test asserts raw. ✔
- Error handling (missing/garbage → 0, logged) → T2 garbage test + debug log. ✔
- Coordinator-only, no smart_battery change → T8 Step 3 asserts it. ✔
- Localisation → T5. ✔
- Testing via simulator (C-028) → T6 (with documented fallback to mock-based T2 coverage if no unit-layer sim fixture exists). ✔
- Docs / API reference / D-NNN → T7. ✔

**2. Placeholder scan:** No TBD/TODO. Every code step shows complete content. The one conditional is T6 (simulator fixture may not exist at unit layer) — handled explicitly with a documented decision + fallback, not a placeholder.

**3. Type consistency:** `CONF_ADDITIONAL_PV_POWER_VARIABLE` (const), `additional_pv_power_variable` (IntegrationConfig field + option key value), `self._additional_pv_kw` (coordinator cache), `_coerce_kw` (helper), `_additional_pv_schema_dict` (config_flow helper) — names are consistent across Tasks 1→4. The option *key string* is `"additional_pv_power_variable"` everywhere (the CONF constant's value).

**4. Known soft spots flagged for the implementer:**
- T2: confirm `Any` and a module logger `_LOGGER` already exist in coordinator.py (they do) before using them.
- T3: exact insertion point depends on `inject_realtime_data`'s internal structure — the implementer must read lines ~501–622 and place the add before the change-detection/grid-direction logic. The test pins the observable outcome (4.5) regardless of placement, but placement matters for the grid-direction side benefit.
- T4: verify `vol` and `Any` are imported in config_flow.py; add if missing.
