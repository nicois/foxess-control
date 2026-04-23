# Overview Card Box Customisation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users show/hide, reorder, and rename the four overview card boxes (solar, house, grid, battery) via the card editor YAML — with sensible defaults that preserve today's zero-config experience.

**Architecture:** A new `boxes` array in the card config declares which boxes appear and in what order. Each entry can override the icon and label. The render loop iterates over this array instead of hard-coding four calls. The editor gains a visual box-order section. Existing configs without `boxes` render identically to today (backwards compatible).

**Tech Stack:** Vanilla JS (Web Components, Shadow DOM) — same as existing card. No new dependencies.

**Constraints:**
- **C-020 (Operational transparency):** hidden boxes must not hide system problems — if a user hides the grid box, that's their choice, but default config must show all four.
- **C-031 (No flaky tests):** E2E tests must use deterministic waits.

---

## Design Decisions

### Config shape

```yaml
type: custom:foxess-overview-card
# Optional — omitting gives today's default [solar, house, grid, battery]
boxes:
  - type: solar
  - type: house
    icon: "🔌"        # optional override
    label: "Load"      # optional override
  - type: grid
  - type: battery
```

- `boxes` is an array of objects. Each has a required `type` field (`solar | house | grid | battery`) and optional `icon` / `label` overrides.
- Omitting `boxes` entirely gives the default 4-box layout — **full backwards compatibility**.
- Omitting a box type from the array hides it.
- Duplicate types are ignored (first wins).
- Invalid types are silently skipped.
- A single-box config renders in a 1-column layout; 3 boxes use 2 columns with the third spanning full width.

### Why not a `hidden_boxes` deny-list?

An allow-list (`boxes: [...]`) is simpler to reason about: what you see in YAML is what renders, in that order. A deny-list requires the user to know the full set and doesn't support reordering.

### Grid columns

| Box count | Layout |
|-----------|--------|
| 1 | 1 column |
| 2 | 2 columns, 1 row |
| 3 | 2 columns; third box spans full width |
| 4 | 2 columns, 2 rows (today's layout) |

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `custom_components/foxess_control/www/foxess-overview-card.js` | Config parsing, render loop, editor UI, grid layout |
| Modify | `tests/e2e/test_ui.py` | E2E tests for customisation |
| Modify | `tests/e2e/selectors.py` | New selectors if needed |
| Modify | `tests/e2e/ha_config/.storage/lovelace` | Add a second overview card with custom config for E2E |

---

## Task 1: Default box order constant and config parsing

**Files:**
- Modify: `custom_components/foxess_control/www/foxess-overview-card.js` (lines 182-194, setConfig and constructor)

- [ ] **Step 1: Add the default box order constant**

After `_ROLE_MAP`, add:

```javascript
const _DEFAULT_BOXES = [
  { type: "solar" },
  { type: "house" },
  { type: "grid" },
  { type: "battery" },
];

const _VALID_BOX_TYPES = new Set(["solar", "house", "grid", "battery"]);
```

- [ ] **Step 2: Add config parsing in `setConfig`**

Replace the existing `setConfig`:

```javascript
setConfig(config) {
  this._userConfig = config || {};
  this._boxes = this._parseBoxes(this._userConfig.boxes);
}
```

Add the parser method after `setConfig`:

```javascript
_parseBoxes(raw) {
  if (!Array.isArray(raw) || raw.length === 0) return _DEFAULT_BOXES;
  const seen = new Set();
  const result = [];
  for (const entry of raw) {
    const type = typeof entry === "string" ? entry : entry?.type;
    if (!type || !_VALID_BOX_TYPES.has(type) || seen.has(type)) continue;
    seen.add(type);
    result.push({
      type,
      icon: entry?.icon || null,
      label: entry?.label || null,
    });
  }
  return result.length > 0 ? result : _DEFAULT_BOXES;
}
```

- [ ] **Step 3: Verify pre-commit passes**

Run: `pre-commit run --all-files`
Expected: all passed

- [ ] **Step 4: Commit**

```bash
git add custom_components/foxess_control/www/foxess-overview-card.js
git commit -m "feat(overview): add box order config parsing with defaults"
```

---

## Task 2: Refactor render to use the box order array

This is the core change. Instead of hard-coding four render calls, `_render` iterates `this._boxes` and dispatches to the correct render method per type.

**Files:**
- Modify: `custom_components/foxess_control/www/foxess-overview-card.js` (lines 319-401, `_render` method)

- [ ] **Step 1: Extract per-box render dispatching**

Add a new method `_renderBox(type, eid, dataSource, overrides)` that dispatches to the existing render methods:

```javascript
_renderBox(box, eid, dataSource) {
  const t = box.type;
  if (t === "solar") {
    const solar = this._num(eid.solar_entity);
    const pv1 = this._num(eid.pv1_entity);
    const pv2 = this._num(eid.pv2_entity);
    const found = this._exists(eid.solar_entity);
    const active = solar != null && solar > 0.01;
    const sub = dataSource !== "ws" && (pv1 != null || pv2 != null)
      ? this._pvDetail(pv1, pv2, eid.pv1_entity, eid.pv2_entity) : "";
    return this._renderNode(
      "solar", box.icon || "\u2600\uFE0F", box.label || this._t("solar"),
      found, this._formatKw(solar), active, sub, eid.solar_entity
    );
  }
  if (t === "house") {
    const house = this._num(eid.house_entity);
    const found = this._exists(eid.house_entity);
    return this._renderNode(
      "house", box.icon || "\uD83C\uDFE0", box.label || this._t("house"),
      found, this._formatKw(house), house != null, "", eid.house_entity
    );
  }
  if (t === "grid") {
    const gridImport = this._num(eid.grid_import_entity);
    const gridExport = this._num(eid.grid_export_entity);
    const gridNet = (gridImport || 0) - (gridExport || 0);
    const found = this._exists(eid.grid_import_entity) || this._exists(eid.grid_export_entity);
    const gridV = dataSource !== "ws" ? this._num(eid.grid_voltage_entity) : null;
    const gridHz = dataSource !== "ws" ? this._num(eid.grid_frequency_entity) : null;
    return this._renderGridNode(
      found, gridNet, gridNet > 0.01, gridNet < -0.01,
      gridV, gridHz, eid.grid_import_entity,
      eid.grid_voltage_entity, eid.grid_frequency_entity
    );
  }
  if (t === "battery") {
    const batCharge = this._num(eid.battery_charge_entity);
    const batDischarge = this._num(eid.battery_discharge_entity);
    const soc = this._num(eid.soc_entity);
    const batNet = (batCharge || 0) - (batDischarge || 0);
    const found = this._exists(eid.battery_charge_entity) || this._exists(eid.battery_discharge_entity);
    const socPct = soc != null ? Math.max(0, Math.min(100, Math.round(soc))) : 0;
    let socColor = "var(--success-color, #4caf50)";
    if (socPct <= 15) socColor = "var(--error-color, #f44336)";
    else if (socPct <= 30) socColor = "var(--warning-color, #ff9800)";
    const batTemp = dataSource !== "ws" ? this._num(eid.bat_temp_entity) : null;
    const bmsTemp = this._num(eid.bms_temp_entity);
    const residual = dataSource !== "ws" ? this._num(eid.residual_entity) : null;
    return this._renderBatteryNode(
      soc, socPct, socColor, batNet, batNet > 0.01, batNet < -0.01,
      batTemp, bmsTemp, residual, found, eid.soc_entity,
      eid.bat_temp_entity, eid.bms_temp_entity, eid.residual_entity
    );
  }
  return "";
}
```

- [ ] **Step 2: Simplify `_render` to use the dispatch**

Replace the body of `_render` from the entity resolution through the `innerHTML` assignment. The new flow-grid content becomes:

```javascript
_render() {
  if (!this._hass) return;

  const eid = {};
  for (const key of Object.keys(_ROLE_MAP)) {
    eid[key] = this._resolve(key);
  }

  const workMode = this._str(eid.work_mode_entity);
  const dataSource = this._getDataSource(eid);
  const freshnessId = eid.data_freshness_entity;
  const freshnessEntity = freshnessId && this._hass.states[freshnessId];
  const lastUpdate = freshnessEntity && freshnessEntity.attributes && freshnessEntity.attributes.last_update;
  const ageSeconds = lastUpdate ? Math.max(0, Math.round((Date.now() - new Date(lastUpdate).getTime()) / 1000)) : null;

  const boxCount = this._boxes.length;
  const gridCls = boxCount === 1 ? "flow-grid cols-1"
    : boxCount === 3 ? "flow-grid cols-3"
    : "flow-grid";

  this.shadowRoot.innerHTML = `
    <style>${FoxESSOverviewCard._styles()}</style>
    <ha-card>
      <div class="header">
        <div class="title">${this._t("title")}${this._dataSourceBadge(dataSource, ageSeconds)}</div>
        ${workMode && workMode !== "SelfUse" ? `<span class="work-mode">${this._formatWorkMode(workMode)}</span>` : ""}
      </div>
      <div class="${gridCls}">
        ${this._boxes.map(b => this._renderBox(b, eid, dataSource)).join("")}
      </div>
    </ha-card>
  `;

  this.shadowRoot.querySelectorAll(".sub-link[data-entity]").forEach((link) => {
    link.addEventListener("click", (e) => {
      e.stopPropagation();
      this._fireMoreInfo(link.getAttribute("data-entity"));
    });
  });
  this.shadowRoot.querySelectorAll(".node[data-entity]").forEach((node) => {
    node.addEventListener("click", (e) => {
      e.stopPropagation();
      this._fireMoreInfo(node.getAttribute("data-entity"));
    });
  });
}
```

- [ ] **Step 3: Pass icon/label overrides through grid and battery render methods**

`_renderGridNode` and `_renderBatteryNode` currently use hard-coded icons and labels. Add optional override parameters:

For `_renderGridNode`, add `iconOverride` and `labelOverride` params at the end:

```javascript
_renderGridNode(found, gridNet, importing, exporting, voltage, freq, entityId, voltageId, freqId, iconOverride, labelOverride) {
```

Replace the hard-coded `"⚡"` with `iconOverride || "⚡"` and `this._t("grid")` with `labelOverride || this._t("grid")` in both the found and not-found branches.

For `_renderBatteryNode`, add the same:

```javascript
_renderBatteryNode(soc, socPct, socColor, batNet, charging, discharging, temp, bmsTemp, residual, found, socEntityId, tempId, bmsTempId, residualId, iconOverride, labelOverride) {
```

Replace hard-coded `"🔋"` with `iconOverride || "🔋"` and `this._t("battery")` with `labelOverride || this._t("battery")`.

Update the dispatch calls in `_renderBox` to pass `box.icon` and `box.label`:

```javascript
// In grid dispatch:
return this._renderGridNode(
  found, gridNet, gridNet > 0.01, gridNet < -0.01,
  gridV, gridHz, eid.grid_import_entity,
  eid.grid_voltage_entity, eid.grid_frequency_entity,
  box.icon, box.label
);

// In battery dispatch:
return this._renderBatteryNode(
  soc, socPct, socColor, batNet, batNet > 0.01, batNet < -0.01,
  batTemp, bmsTemp, residual, found, eid.soc_entity,
  eid.bat_temp_entity, eid.bms_temp_entity, eid.residual_entity,
  box.icon, box.label
);
```

- [ ] **Step 4: Verify pre-commit passes and existing tests still pass**

Run: `pre-commit run --all-files && pytest tests/ -m "not slow" --tb=short -q`
Expected: all passed, 670 unit tests pass

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/www/foxess-overview-card.js
git commit -m "refactor(overview): render loop driven by box config array"
```

---

## Task 3: Grid layout CSS for 1/3 box counts

**Files:**
- Modify: `custom_components/foxess_control/www/foxess-overview-card.js` (`_styles` method)

- [ ] **Step 1: Add CSS rules for alternative box counts**

In the `_styles()` method, after the existing `.flow-grid` rule, add:

```css
.flow-grid.cols-1 {
  grid-template-columns: 1fr;
}
.flow-grid.cols-3 .node:last-child {
  grid-column: 1 / -1;
}
```

The default `.flow-grid` already has `grid-template-columns: 1fr 1fr` which handles 2 and 4 boxes correctly.

- [ ] **Step 2: Verify pre-commit passes**

Run: `pre-commit run --all-files`
Expected: all passed

- [ ] **Step 3: Commit**

```bash
git add custom_components/foxess_control/www/foxess-overview-card.js
git commit -m "feat(overview): responsive grid for 1/3 box layouts"
```

---

## Task 4: Editor UI for box customisation

**Files:**
- Modify: `custom_components/foxess_control/www/foxess-overview-card.js` (`FoxESSOverviewCardEditor` class, lines 620-689)

- [ ] **Step 1: Add box visibility toggles to the editor**

Add a "Visible Boxes" section at the top of the editor `_render` method, before the entity fields. Each box type gets a checkbox and optional icon/label overrides:

```javascript
_render() {
  const boxes = this._config.boxes || null;
  const boxTypes = ["solar", "house", "grid", "battery"];
  const defaultIcons = { solar: "\u2600\uFE0F", house: "\uD83C\uDFE0", grid: "\u26A1", battery: "\uD83D\uDD0B" };
  const activeBoxes = boxes
    ? boxTypes.map(t => {
        const b = boxes.find(b => (typeof b === "string" ? b : b?.type) === t);
        return { type: t, enabled: !!b, icon: b?.icon || "", label: b?.label || "" };
      })
    : boxTypes.map(t => ({ type: t, enabled: true, icon: "", label: "" }));

  // ... existing entity fields ...

  this.shadowRoot.innerHTML = `
    <style>
      :host { display: block; padding: 8px 0; }
      .section-title { font-size: 13px; font-weight: 600; margin: 8px 0 6px;
                        color: var(--primary-text-color); }
      .box-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
                 padding: 6px 8px; border-radius: 6px;
                 background: var(--secondary-background-color, rgba(0,0,0,0.04)); }
      .box-row input[type="checkbox"] { margin: 0; }
      .box-row .box-type { font-size: 13px; font-weight: 500; min-width: 60px; }
      .box-row input[type="text"] { flex: 1; padding: 4px 6px; border: 1px solid var(--divider-color);
                                     border-radius: 4px; font-size: 12px;
                                     background: var(--card-background-color);
                                     color: var(--primary-text-color); }
      .box-row input[type="text"]::placeholder { color: var(--secondary-text-color); opacity: 0.6; }
      .row { display: flex; flex-direction: column; margin-bottom: 12px; }
      label { font-size: 12px; font-weight: 500; margin-bottom: 4px;
              color: var(--secondary-text-color); }
      input.entity-input { padding: 8px; border: 1px solid var(--divider-color);
              border-radius: 4px; font-size: 14px;
              background: var(--card-background-color);
              color: var(--primary-text-color); }
      .hint { font-size: 11px; color: var(--secondary-text-color);
              margin-top: 2px; }
    </style>
    <div class="section-title">Visible Boxes</div>
    ${activeBoxes.map(b => `
      <div class="box-row">
        <input type="checkbox" data-box-type="${b.type}" ${b.enabled ? "checked" : ""}>
        <span class="box-type">${defaultIcons[b.type]} ${b.type.charAt(0).toUpperCase() + b.type.slice(1)}</span>
        <input type="text" data-box-icon="${b.type}" value="${b.icon}" placeholder="Icon">
        <input type="text" data-box-label="${b.type}" value="${b.label}" placeholder="Label">
      </div>
    `).join("")}
    <div class="section-title">Entity Overrides</div>
    ${fields.map(f => `
      <div class="row">
        <label>${f.label}</label>
        <input class="entity-input" type="text" id="${f.id}"
               value="${this._config[f.id] || ""}"
               placeholder="${f.placeholder}">
        <span class="hint">Auto-discovered if left blank</span>
      </div>
    `).join("")}
  `;

  // Wire up entity field listeners (existing)
  this.shadowRoot.querySelectorAll(".entity-input").forEach((input) => {
    input.addEventListener("input", () => this._valueChanged());
  });
  // Wire up box toggle/override listeners
  this.shadowRoot.querySelectorAll(".box-row input").forEach((input) => {
    input.addEventListener("input", () => this._valueChanged());
  });
}
```

- [ ] **Step 2: Update `_valueChanged` to read box config**

```javascript
_valueChanged() {
  const cfg = { ...this._config };
  const fieldIds = [
    "solar_entity", "house_entity", "grid_import_entity", "grid_export_entity",
    "battery_charge_entity", "battery_discharge_entity", "soc_entity", "work_mode_entity",
  ];
  for (const id of fieldIds) {
    const val = this.shadowRoot.getElementById(id)?.value?.trim();
    if (val) cfg[id] = val;
    else delete cfg[id];
  }

  const boxTypes = ["solar", "house", "grid", "battery"];
  const boxes = [];
  let allDefault = true;
  for (const t of boxTypes) {
    const cb = this.shadowRoot.querySelector(`[data-box-type="${t}"]`);
    const iconInput = this.shadowRoot.querySelector(`[data-box-icon="${t}"]`);
    const labelInput = this.shadowRoot.querySelector(`[data-box-label="${t}"]`);
    const enabled = cb?.checked ?? true;
    const icon = iconInput?.value?.trim() || "";
    const label = labelInput?.value?.trim() || "";
    if (!enabled || icon || label) allDefault = false;
    if (enabled) {
      const entry = { type: t };
      if (icon) entry.icon = icon;
      if (label) entry.label = label;
      boxes.push(entry);
    }
  }

  if (allDefault) {
    delete cfg.boxes;
  } else {
    cfg.boxes = boxes;
  }

  this._config = cfg;
  this.dispatchEvent(
    new CustomEvent("config-changed", { detail: { config: cfg } })
  );
}
```

- [ ] **Step 3: Verify pre-commit passes**

Run: `pre-commit run --all-files`
Expected: all passed

- [ ] **Step 4: Commit**

```bash
git add custom_components/foxess_control/www/foxess-overview-card.js
git commit -m "feat(overview): editor UI for box visibility, icon, and label"
```

---

## Task 5: E2E tests for box customisation

**Files:**
- Modify: `tests/e2e/test_ui.py` (add tests to `TestOverviewCard`)
- Modify: `tests/e2e/ha_config/.storage/lovelace` (add customised card)

- [ ] **Step 1: Add a second overview card with custom config to the E2E lovelace dashboard**

Update `tests/e2e/ha_config/.storage/lovelace`:

```json
{
  "version": 1,
  "minor_version": 1,
  "key": "lovelace",
  "data": {
    "config": {
      "views": [
        {
          "title": "E2E Test",
          "path": "e2e",
          "cards": [
            {
              "type": "custom:foxess-overview-card"
            },
            {
              "type": "custom:foxess-control-card"
            },
            {
              "type": "custom:foxess-overview-card",
              "boxes": [
                { "type": "battery" },
                { "type": "solar", "label": "PV" }
              ]
            }
          ]
        }
      ]
    }
  }
}
```

- [ ] **Step 2: Add E2E test verifying hidden boxes don't render**

```python
def test_custom_boxes_hides_unconfigured(
    self,
    page: Page,
    foxess_sim: SimulatorHandle | None,
    ha_e2e: HAClient,
    connection_mode: str,
) -> None:
    """A card with boxes=[battery, solar] should not render grid or house."""
    set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
    _robust_reload(page, settle_ms=2000)

    result = page.evaluate(
        f"""() => {{
            {_JS_FIND_OVERVIEW_CARD}
            // Find the SECOND overview card (custom config)
            function findAllCards(root) {{
                const cards = [];
                const c = root.querySelectorAll('foxess-overview-card');
                cards.push(...c);
                for (const el of root.querySelectorAll('*')) {{
                    if (el.shadowRoot) {{
                        cards.push(...findAllCards(el.shadowRoot));
                    }}
                }}
                return cards;
            }}
            const cards = findAllCards(document);
            if (cards.length < 2) return null;
            const card = cards[1];
            if (!card.shadowRoot) return null;
            const sr = card.shadowRoot;
            const nodes = sr.querySelectorAll('.node');
            const types = [];
            for (const n of nodes) {{
                if (n.classList.contains('solar')) types.push('solar');
                if (n.classList.contains('house')) types.push('house');
                if (n.classList.contains('grid')) types.push('grid');
                if (n.classList.contains('battery')) types.push('battery');
            }}
            return types;
        }}"""
    )
    assert result is not None, "Second overview card not found"
    assert "battery" in result, "Battery box should be visible"
    assert "solar" in result, "Solar box should be visible"
    assert "house" not in result, "House box should be hidden"
    assert "grid" not in result, "Grid box should be hidden"
```

- [ ] **Step 3: Add E2E test verifying custom label renders**

```python
def test_custom_boxes_label_override(
    self,
    page: Page,
    foxess_sim: SimulatorHandle | None,
    ha_e2e: HAClient,
    connection_mode: str,
) -> None:
    """A box with label override should display the custom label."""
    set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, solar_kw=1.0)
    _robust_reload(page, settle_ms=2000)

    label = page.evaluate(
        f"""() => {{
            {_JS_FIND_OVERVIEW_CARD}
            function findAllCards(root) {{
                const cards = [];
                const c = root.querySelectorAll('foxess-overview-card');
                cards.push(...c);
                for (const el of root.querySelectorAll('*')) {{
                    if (el.shadowRoot) {{
                        cards.push(...findAllCards(el.shadowRoot));
                    }}
                }}
                return cards;
            }}
            const cards = findAllCards(document);
            if (cards.length < 2) return null;
            const card = cards[1];
            if (!card.shadowRoot) return null;
            const solar = card.shadowRoot.querySelector('.node.solar .node-label');
            return solar ? solar.textContent : null;
        }}"""
    )
    assert label is not None, "Solar label not found in custom card"
    assert "PV" in label, f"Expected 'PV' label, got '{label}'"
```

- [ ] **Step 4: Add E2E test verifying box order matches config**

```python
def test_custom_boxes_order(
    self,
    page: Page,
    foxess_sim: SimulatorHandle | None,
    ha_e2e: HAClient,
    connection_mode: str,
) -> None:
    """Boxes render in the order specified in config (battery first, solar second)."""
    set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
    _robust_reload(page, settle_ms=2000)

    order = page.evaluate(
        f"""() => {{
            {_JS_FIND_OVERVIEW_CARD}
            function findAllCards(root) {{
                const cards = [];
                const c = root.querySelectorAll('foxess-overview-card');
                cards.push(...c);
                for (const el of root.querySelectorAll('*')) {{
                    if (el.shadowRoot) {{
                        cards.push(...findAllCards(el.shadowRoot));
                    }}
                }}
                return cards;
            }}
            const cards = findAllCards(document);
            if (cards.length < 2) return null;
            const card = cards[1];
            if (!card.shadowRoot) return null;
            const nodes = card.shadowRoot.querySelectorAll('.node');
            const types = [];
            for (const n of nodes) {{
                if (n.classList.contains('solar')) types.push('solar');
                if (n.classList.contains('house')) types.push('house');
                if (n.classList.contains('grid')) types.push('grid');
                if (n.classList.contains('battery')) types.push('battery');
            }}
            return types;
        }}"""
    )
    assert order == ["battery", "solar"], (
        f"Expected [battery, solar] order, got {order}"
    )
```

- [ ] **Step 5: Run pre-commit and unit tests**

Run: `pre-commit run --all-files && pytest tests/ -m "not slow" --tb=short -q`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/test_ui.py tests/e2e/ha_config/.storage/lovelace
git commit -m "test(overview): E2E tests for box show/hide, order, and label overrides"
```

---

## Task 6: Backwards compatibility and default-config E2E verification

**Files:**
- Modify: `tests/e2e/test_ui.py`

- [ ] **Step 1: Add test that default card (no `boxes` key) still renders all four nodes**

```python
def test_default_config_renders_all_four_boxes(
    self,
    page: Page,
    foxess_sim: SimulatorHandle | None,
    ha_e2e: HAClient,
    connection_mode: str,
) -> None:
    """Card with no boxes config renders all four nodes in default order."""
    set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
    _robust_reload(page, settle_ms=2000)

    types = page.evaluate(
        f"""() => {{
            {_JS_FIND_OVERVIEW_CARD}
            const card = findCard(document);
            if (!card || !card.shadowRoot) return null;
            const nodes = card.shadowRoot.querySelectorAll('.node');
            const types = [];
            for (const n of nodes) {{
                if (n.classList.contains('solar')) types.push('solar');
                if (n.classList.contains('house')) types.push('house');
                if (n.classList.contains('grid')) types.push('grid');
                if (n.classList.contains('battery')) types.push('battery');
            }}
            return types;
        }}"""
    )
    assert types == ["solar", "house", "grid", "battery"], (
        f"Default card should show all 4 boxes in order, got {types}"
    )
```

- [ ] **Step 2: Run full pre-commit + unit tests**

Run: `pre-commit run --all-files && pytest tests/ -m "not slow" --tb=short -q`
Expected: all passed

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_ui.py
git commit -m "test(overview): verify backwards-compatible default 4-box layout"
```

---

## Task 7: Version bump, changelog, and final verification

**Files:**
- Modify: `custom_components/foxess_control/www/foxess-overview-card.js` (version constant)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump version**

The version in the JS file should already have been bumped during development. Verify it reflects the new feature (e.g. `2.8.0` or whatever follows from the current version at implementation time).

- [ ] **Step 2: Add changelog entry**

Add at the top of `CHANGELOG.md`:

```markdown
## <next-beta-version>

### Added
- **Overview card box customisation**: users can show/hide, reorder, and relabel the four overview boxes (solar, house, grid, battery) via a `boxes` array in the card config. The editor gains checkboxes and override fields. Existing configs without `boxes` render identically to before.
```

- [ ] **Step 3: Run full test suite**

Run: `pre-commit run --all-files && pytest tests/ -m "not slow" --tb=short -q`
Expected: all passed

- [ ] **Step 4: Commit**

```bash
git add custom_components/foxess_control/www/foxess-overview-card.js CHANGELOG.md
git commit -m "feat(overview): box customisation — show/hide, reorder, relabel"
```
