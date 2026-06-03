# Entity Mode at First-Time Setup (modbus-aware config flow) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `foxess_modbus` is installed, the FoxESS Control setup flow offers an explicit "Cloud vs entity (no API key)" choice and lets the user fully configure entity mode at setup — instead of forcing a cloud API key and hiding entity mode in the options flow.

**Architecture:** `async_step_user` becomes a router: if a `foxess_modbus` config entry is present it shows a native HA menu (cloud / entity); otherwise it goes straight to the cloud API-key step (today's behaviour). The entity branch reuses the existing standalone schema builders (`entity_mapping_schema`, `battery_options_schema`, `detect_entities` in `smart_battery/config_flow_base.py`) across two sub-steps and creates an entry with no API key. Cloud-only users see no new step.

**Tech Stack:** Python 3.14, Home Assistant config flow (`ConfigFlow`, `async_show_menu`, `async_show_form`), voluptuous, pytest. Brand-agnostic schema code in `smart_battery/` (vendored copy synced by pre-commit, C-015).

**Spec:** `docs/superpowers/specs/2026-06-03-entity-mode-setup-flow-design.md`

**Verified facts:**
- `config_flow.py::FoxessControlConfigFlow` (VERSION 2): `async_step_user` currently IS the API-key form (`CONF_API_KEY` + `CONF_DEVICE_SERIAL`, both `vol.Required`, cloud-validated via `_validate_credentials`), then `async_step_web_credentials` creates the entry with `unique_id = device_serial`.
- `_detect_foxess_modbus_entities(hass)` already exists (wraps `detect_entities(hass, "foxess_modbus", _MODBUS_NAME_MAP)`).
- `entity_mapping_schema(config_entry, detected, *, default_inverter_power=...)` and `battery_options_schema(config_entry)` both read `config_entry.options` — they do NOT currently tolerate `config_entry=None` (the setup-time case). Task 1 fixes that.
- `ENTITY_KEYS`, `build_entity_map` are in `config_flow_base.py`. The options flow's `async_step_modbus` shows the entity step today (unchanged by this work).
- strings: `strings.json` has `config.step` (`user`, `web_credentials`, `reauth_confirm`) + `config.abort`/`config.error`. Locale files: `translations/{en,de,es,fr,it,ja,nl,pl,zh-Hans,pt}.json` (10 total). New keys MUST be mirrored into all 10 + strings.json (locale-parity test enforces it).
- Tests: `tests/test_config_flow.py` drives the flow handler with a mock hass (no container).

---

## File Structure

- `smart_battery/config_flow_base.py` (modify) — make `entity_mapping_schema` and `battery_options_schema` accept `config_entry: ConfigEntry | None` (treat `None` as empty options). Vendored copy auto-synced.
- `custom_components/foxess_control/config_flow.py` (modify) — `async_step_user` becomes a router; extract today's API-key form into `async_step_cloud`; add `async_step_entity` + `async_step_entity_battery`.
- `custom_components/foxess_control/strings.json` (modify) — menu + new step strings.
- `custom_components/foxess_control/translations/{en,de,es,fr,it,ja,nl,pl,pt,zh-Hans}.json` (modify) — mirror new strings (10 files).
- `tests/test_config_flow.py` (modify) — routing + entity-branch + unchanged-cloud tests.
- `README.md` + `docs/knowledge/` (modify, Task 6) — D-060 + setup-UX doc.

---

## Task 1: Schema builders tolerate `config_entry=None`

**Files:**
- Modify: `smart_battery/config_flow_base.py` (`entity_mapping_schema`, `battery_options_schema`)
- Test: `tests/test_config_flow.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_config_flow.py`:

```python
class TestSchemaBuildersNoEntry:
    """entity_mapping_schema / battery_options_schema must work at setup time
    (no config entry exists yet)."""

    def test_entity_mapping_schema_accepts_none_entry(self) -> None:
        from custom_components.foxess_control.smart_battery.config_flow_base import (
            entity_mapping_schema,
        )
        # detected pre-fills defaults; no entry to read existing options from.
        schema = entity_mapping_schema(None, {CONF_WORK_MODE_ENTITY: "select.wm"})
        defaults = schema({})  # voluptuous applies declared defaults
        assert defaults[CONF_WORK_MODE_ENTITY] == "select.wm"

    def test_battery_options_schema_accepts_none_entry(self) -> None:
        from custom_components.foxess_control.smart_battery.config_flow_base import (
            battery_options_schema,
        )
        schema = battery_options_schema(None)
        defaults = schema({})
        assert CONF_MIN_SOC_ON_GRID in defaults
        assert CONF_BATTERY_CAPACITY_KWH in defaults
```

- [ ] **Step 2: Run to verify FAIL** — `pytest tests/test_config_flow.py::TestSchemaBuildersNoEntry -v` → `AttributeError: 'NoneType' object has no attribute 'options'`.

- [ ] **Step 3: Implement** — in `smart_battery/config_flow_base.py`, change both signatures to accept `ConfigEntry | None` and guard the `.options` read:

In `entity_mapping_schema`:
```python
def entity_mapping_schema(
    config_entry: ConfigEntry | None,
    detected: dict[str, str],
    *,
    default_inverter_power: int = DEFAULT_INVERTER_POWER,
) -> vol.Schema:
    """Build the vol.Schema for the entity mapping step.

    *detected* maps ``CONF_*_ENTITY`` keys to auto-detected entity IDs.
    *config_entry* is ``None`` during first-time setup (no entry yet); in
    that case defaults come purely from *detected*.
    """
    opts = config_entry.options if config_entry is not None else {}
```
(The existing `_default(conf_key)` already does `opts.get(conf_key) or detected.get(conf_key, "")` — leave it.)

In `battery_options_schema`:
```python
def battery_options_schema(
    config_entry: ConfigEntry | None,
) -> vol.Schema:
    """Build the vol.Schema for the shared battery options step.

    *config_entry* is ``None`` during first-time setup; defaults are then
    the built-in defaults (e.g. capacity 0.0, min-SoC default).
    """
    opts = config_entry.options if config_entry is not None else {}
```
(The rest of the body already reads `opts.get(...)`.)

- [ ] **Step 4: Sync vendored + run** — `pre-commit run sync-vendored-smart-battery --all-files || true` then `pytest tests/test_config_flow.py::TestSchemaBuildersNoEntry -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add smart_battery/config_flow_base.py custom_components/foxess_control/smart_battery/config_flow_base.py tests/test_config_flow.py
git commit -m "feat(config-flow): schema builders accept config_entry=None for first-time setup"
```

---

## Task 2: `async_step_user` router + extract `async_step_cloud`

**Files:**
- Modify: `custom_components/foxess_control/config_flow.py`
- Test: `tests/test_config_flow.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config_flow.py`:

```python
def _make_hass_modbus(has_modbus: bool) -> MagicMock:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda domain: (
            [MagicMock()] if domain == "foxess_modbus" and has_modbus else []
        )
    )
    return hass


class TestConfigFlowRouting:
    @pytest.mark.asyncio
    async def test_user_step_shows_menu_when_modbus_present(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass_modbus(True)
        flow.async_show_menu = MagicMock(return_value={"type": "menu"})
        result = await flow.async_step_user(None)
        assert result["type"] == "menu"
        # cloud + entity options offered
        _, kwargs = flow.async_show_menu.call_args
        assert set(kwargs["menu_options"]) == {"cloud", "entity"}

    @pytest.mark.asyncio
    async def test_user_step_goes_straight_to_cloud_when_no_modbus(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass_modbus(False)
        flow.async_show_form = MagicMock(return_value={"type": "form", "step_id": "cloud"})
        result = await flow.async_step_user(None)
        # No menu; the cloud (api-key) form is shown directly.
        assert result["type"] == "form"
        _, kwargs = flow.async_show_form.call_args
        assert kwargs["step_id"] == "cloud"
```

- [ ] **Step 2: Run to verify FAIL** — `pytest tests/test_config_flow.py::TestConfigFlowRouting -v` → fails (`async_step_user` currently renders the api-key form under `step_id="user"`, has no menu).

- [ ] **Step 3: Implement** — in `config_flow.py`, replace the current `async_step_user` body. Rename the existing api-key form logic into `async_step_cloud` (step_id `"cloud"`), and make `async_step_user` the router:

```python
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: route on foxess_modbus presence."""
        if self.hass.config_entries.async_entries("foxess_modbus"):
            return self.async_show_menu(
                step_id="user",
                menu_options=["cloud", "entity"],
            )
        return await self.async_step_cloud()

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cloud setup: API key + serial (validated against the cloud API)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    _validate_credentials,
                    user_input[CONF_API_KEY],
                    user_input[CONF_DEVICE_SERIAL],
                )
            except FoxESSApiError as err:
                _LOGGER.warning("FoxESS API rejected credentials: %s", err)
                errors["base"] = "invalid_auth"
            except requests.RequestException as err:
                _LOGGER.warning("Could not reach FoxESS Cloud API: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_SERIAL])
                self._abort_if_unique_id_configured()
                self._api_data = user_input
                return await self.async_step_web_credentials()

        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Required(CONF_DEVICE_SERIAL): str,
                }
            ),
            errors=errors,
        )
```
NOTE: HA routes a menu selection of `"cloud"` to `async_step_cloud` and `"entity"` to `async_step_entity` by the step_id convention. The menu's `step_id="user"` is the step the menu is *attached* to (so re-entry works). Verify against the installed HA version's `async_show_menu` semantics; the menu_options names MUST match the `async_step_<name>` method names.

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_config_flow.py::TestConfigFlowRouting -v` → PASS. Also run the existing cloud tests, which now hit `async_step_cloud` — UPDATE them: the existing tests call `flow.async_step_user(user_input)` with api-key input and assert on `step_id="user"`. Change those existing tests to call `flow.async_step_cloud(user_input)` (since `async_step_user` no longer takes the api-key form) OR keep calling `async_step_user` for the no-modbus path (router delegates to cloud) — but the form's step_id is now `"cloud"`. Adjust the existing assertions from `step_id="user"` → `step_id="cloud"`. Run `pytest tests/test_config_flow.py -v` → all green.

- [ ] **Step 5: Commit**
```bash
git add custom_components/foxess_control/config_flow.py tests/test_config_flow.py
git commit -m "feat(config-flow): async_step_user routes to menu (modbus) or cloud step"
```

---

## Task 3: `async_step_entity` + `async_step_entity_battery`

**Files:**
- Modify: `custom_components/foxess_control/config_flow.py`
- Test: `tests/test_config_flow.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config_flow.py`:

```python
class TestConfigFlowEntityBranch:
    @pytest.mark.asyncio
    async def test_entity_step_shows_mapping_form(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass_modbus(True)
        flow.async_show_form = MagicMock(return_value={"type": "form", "step_id": "entity"})
        with patch(
            "custom_components.foxess_control.config_flow._detect_foxess_modbus_entities",
            return_value={CONF_WORK_MODE_ENTITY: "select.wm"},
        ):
            result = await flow.async_step_entity(None)
        assert result["type"] == "form"
        _, kwargs = flow.async_show_form.call_args
        assert kwargs["step_id"] == "entity"

    @pytest.mark.asyncio
    async def test_entity_then_battery_creates_entry_without_api_key(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass_modbus(True)
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        created = {}
        def _create(**kw):
            created.update(kw)
            return {"type": "create_entry"}
        flow.async_create_entry = MagicMock(side_effect=_create)

        # entity-mapping submit → goes to battery step
        await flow.async_step_entity({CONF_WORK_MODE_ENTITY: "select.wm",
                                      CONF_SOC_ENTITY: "sensor.soc"})
        # battery submit → creates entry
        await flow.async_step_entity_battery({
            CONF_BATTERY_CAPACITY_KWH: 10.0,
            CONF_MIN_SOC_ON_GRID: 11,
            CONF_MIN_POWER_CHANGE: 200,
        })
        assert created  # entry created
        data = created["data"]
        assert CONF_API_KEY not in data            # NO api key
        assert data[CONF_WORK_MODE_ENTITY] == "select.wm"   # entity mode active
        # battery options carried (data or options — assert capacity present somewhere)
        merged = {**created.get("data", {}), **created.get("options", {})}
        assert merged[CONF_BATTERY_CAPACITY_KWH] == 10.0
```

- [ ] **Step 2: Run to verify FAIL** — `pytest tests/test_config_flow.py::TestConfigFlowEntityBranch -v` → `AttributeError: ... has no attribute 'async_step_entity'`.

- [ ] **Step 3: Implement** — add to `config_flow.py` (`__init__` already has `self._api_data`; add `self._entity_data: dict[str, Any] = {}` to `__init__`):

```python
    async def async_step_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entity mode: map foxess_modbus entities (no API key)."""
        if user_input is not None:
            self._entity_data = dict(user_input)
            return await self.async_step_entity_battery()

        detected = _detect_foxess_modbus_entities(self.hass)
        return self.async_show_form(
            step_id="entity",
            data_schema=entity_mapping_schema(None, detected),
        )

    async def async_step_entity_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entity mode: battery options, then create the entry."""
        if user_input is not None:
            # Stable unique_id for the API-key-less entity entry: derive
            # from the first foxess_modbus config entry id (falls back to a
            # fixed sentinel) so duplicate entity setups abort cleanly.
            modbus_entries = self.hass.config_entries.async_entries("foxess_modbus")
            uid = (
                f"entity-{modbus_entries[0].entry_id}"
                if modbus_entries
                else "entity-foxess-control"
            )
            await self.async_set_unique_id(uid)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="FoxESS Control (entity mode)",
                data=dict(self._entity_data),
                options=dict(user_input),
            )

        return self.async_show_form(
            step_id="entity_battery",
            data_schema=battery_options_schema(None),
        )
```
NOTE on `unique_id`: `MagicMock().entry_id` is itself a Mock in the test; the f-string coerces it to a string, so `async_set_unique_id` is still called with a str — fine for the test (which mocks `async_set_unique_id`). In production `entry_id` is a real string.
NOTE on data vs options: cloud entries store everything in `data`; the options flow reads entity mappings from `entry.options` via `build_entity_map`. CONFIRM where the runtime reads entity mappings from (data or options): grep `build_entity_map` callers / how `_cfg`/IntegrationConfig reads `CONF_WORK_MODE_ENTITY`. Put the entity MAPPINGS where the runtime actually reads them (likely `options`, matching the options-flow path), and battery options alongside. If the runtime reads entity mappings from `options`, change the create_entry to `data={}, options={**self._entity_data, **user_input}`. **Verify this before finalising — getting data-vs-options wrong means entity mode silently doesn't activate.** Add a test asserting the work-mode entity lands where the runtime reads it.

- [ ] **Step 4: Run to verify PASS** — `pytest tests/test_config_flow.py::TestConfigFlowEntityBranch -v` → PASS (after fixing data-vs-options per the verification above).

- [ ] **Step 5: Commit**
```bash
git add custom_components/foxess_control/config_flow.py tests/test_config_flow.py
git commit -m "feat(config-flow): entity branch — mapping + battery steps create API-key-less entry"
```

---

## Task 4: Strings + locale parity

**Files:**
- Modify: `custom_components/foxess_control/strings.json`
- Modify: `custom_components/foxess_control/translations/{en,de,es,fr,it,ja,nl,pl,pt,zh-Hans}.json`
- Test: `tests/test_config_flow.py` (or the existing locale-parity test covers it)

- [ ] **Step 1: Add strings to `strings.json`** under `config`:
  - `config.step.user` becomes the MENU step — add a `menu_options` block:
    ```json
    "user": {
      "title": "FoxESS Control",
      "description": "How is your inverter connected?",
      "menu_options": {
        "cloud": "FoxESS Cloud (API key)",
        "entity": "Use my foxess_modbus inverter (no API key needed)"
      }
    }
    ```
  - `config.step.cloud`:
    ```json
    "cloud": {
      "title": "FoxESS Control",
      "description": "Enter your FoxESS Cloud API credentials. If you use the foxess_modbus integration instead, you can run without a cloud API key — see the docs.",
      "data": {"api_key": "API Key", "device_serial": "Device Serial Number"}
    }
    ```
  - `config.step.entity`:
    ```json
    "entity": {
      "title": "FoxESS Control — entity mode",
      "description": "Map the controls from your foxess_modbus inverter. Fields are auto-filled where detected; adjust as needed. Setting the Work Mode entity enables entity mode.",
      "data": {"work_mode_entity": "Work Mode entity", "...": "..."}
    }
    ```
    (Include a `data` label for each `CONF_*_ENTITY` key in `ENTITY_KEYS` — read `ENTITY_KEYS` and the options-flow `modbus` step's existing `data` labels in `strings.json` and reuse those exact labels for consistency.)
  - `config.step.entity_battery`:
    ```json
    "entity_battery": {
      "title": "FoxESS Control — battery",
      "description": "Battery settings. Battery capacity is required for discharge pacing.",
      "data": {"min_soc_on_grid": "Reserve SoC (%)", "battery_capacity_kwh": "Battery capacity (kWh)", "min_power_change": "Minimum power change (W)"}
    }
    ```
    (Reuse the exact `data` labels the options flow uses for the battery step — read them from `strings.json` `options.step.init` and copy.)

- [ ] **Step 2: Mirror into all 10 `translations/*.json`.** For `en.json`, copy the strings.json `config` block verbatim. For the 9 non-EN locales, translate the new strings to match each file's established tone (units like `kW`/`SoC` and tokens stay canonical). The keys/structure MUST match en.json exactly.

- [ ] **Step 3: Run the locale-parity + flow tests**
Run: `pytest tests/test_runtime_translations_issues.py tests/test_config_flow.py -v 2>&1 | tail -15`
Expected: PASS. If a parity test asserts config-step coverage and fails on a missing locale key, fix the locale file. If no test covers config-step parity specifically, add one mirroring the existing issues-parity test that asserts every locale has the same `config.step` keys as en.json.

- [ ] **Step 4: Commit**
```bash
git add custom_components/foxess_control/strings.json custom_components/foxess_control/translations/ tests/test_config_flow.py
git commit -m "feat(config-flow): menu + entity/cloud/battery step strings, all 10 locales"
```

---

## Task 5: Full-suite verification

**Files:** none.

- [ ] **Step 1:** `pytest tests/ -m "not slow" --tb=short` → all pass (config-flow + locale-parity green; no regressions).
- [ ] **Step 2:** `pre-commit run --all-files` → clean (ruff/ruff-format/semgrep/mypy/vendored-sync). Pre-existing unrelated mypy error in `tests/test_sensor_listener_safety.py:284` is not introduced here; if it appears, confirm it's on the base.
- [ ] **Step 3:** Manual reasoning check (no container needed): confirm the cloud-only path (`_make_hass_modbus(False)`) is behaviourally identical to today — router → `async_step_cloud` → web creds → cloud entry with `unique_id=serial`. Confirm the existing cloud tests still assert that.
- [ ] **Step 4:** Report (no commit): suite result; confirmation entity entry has no api_key + work-mode entity where the runtime reads it; data-vs-options decision made; locales covered.

---

## Task 6: Knowledge tree + README

**Files:**
- Modify: `docs/knowledge/04-design/session-management.md` (or wherever D-022 entity-mode design lives) — add D-060.
- Modify: `docs/knowledge/02-constraints.md` (if a setup-UX constraint is touched), `05-coverage.md`, `06-tests.md`.
- Modify: `README.md` (setup section ~line 112).

- [ ] **Step 1: Add D-060** to the design doc that hosts D-022 (entity mode). Content: early `foxess_modbus` detection → cloud/entity menu in the config flow; entity mode reachable at first-time setup (not options-only); entity branch creates an API-key-less entry with mapping + battery steps. Priority served P-005; classification `other`; traces the new config-flow tests. Note it resolves the investigation's hypothesis (d) (cloud-validation blocking modbus-only users).
- [ ] **Step 2: Update README** setup section: entity mode is now offered at setup when foxess_modbus is detected (a cloud-vs-entity choice), with the options flow still available to change mappings later. Update the line that currently says entity mode appears in the options flow.
- [ ] **Step 3: Update `06-tests.md`** — add the new config-flow test classes; **05-coverage.md** — D-060 in the P-005 row + counts.
- [ ] **Step 4: Run** `python scripts/knowledge_audit.py` → no new gaps/collisions. Bump `last_verified` on edited docs + META workflow_state + reflection-log entry.
- [ ] **Step 5: Commit**
```bash
git add docs/ README.md
git commit -m "Docs: D-060 entity mode at first-time setup (modbus-aware config flow)"
```

---

## Self-Review

- **Spec coverage:** router + no-modbus-straight-to-cloud → Task 2. menu (cloud/entity labels) → Task 2 + Task 4 strings. entity branch full setup (mapping + battery, no API key) → Task 3. schema builders tolerate no-entry → Task 1. unique_id → Task 3. copy + 10-locale parity → Task 4. D-060 + README + investigation-(d)-resolved note → Task 6. Cloud-only unchanged → Task 2 Step 4 + Task 5 Step 3. All spec sections covered.
- **Placeholder scan:** real code in every step. The two spec-flagged unknowns are addressed with explicit verification instructions, not hand-waving: Task 1 fixes `config_entry=None`; Task 3 pins unique_id AND flags data-vs-options as a MUST-VERIFY with a guarding test (getting it wrong = entity mode silently inactive). The `entity` step's per-key `data` labels say "read ENTITY_KEYS and reuse the options-flow labels" — a concrete instruction (copy existing), acceptable since the exact label set is long and already exists in strings.json.
- **Type/signature consistency:** `entity_mapping_schema(config_entry: ConfigEntry | None, detected, *, ...)` and `battery_options_schema(config_entry: ConfigEntry | None)` consistent Task 1↔3. Step ids: `user`(menu), `cloud`, `entity`, `entity_battery` — menu_options `["cloud","entity"]` match `async_step_cloud`/`async_step_entity` method names. `_entity_data` added in Task 3.
- **Scope:** single subsystem (config flow). Focused. Detection-brittleness (2b) explicitly out of scope per spec.
