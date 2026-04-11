/**
 * FoxESS Overview — custom Lovelace card.
 *
 * Shows live energy flows between solar, battery, grid and house
 * with power values and key inverter stats.
 *
 * Usage:
 *   type: custom:foxess-overview-card
 *   # All entities auto-discovered; optional overrides:
 *   # solar_entity: sensor.foxess_solar_power
 *   # house_entity: sensor.foxess_house_load
 *   # grid_import_entity: sensor.foxess_grid_consumption
 *   # grid_export_entity: sensor.foxess_grid_feed_in
 *   # battery_charge_entity: sensor.foxess_charge_rate
 *   # battery_discharge_entity: sensor.foxess_discharge_rate
 *   # soc_entity: sensor.foxess_battery_soc
 *   # work_mode_entity: sensor.foxess_work_mode
 *   # pv1_entity: sensor.foxess_pv1_power
 *   # pv2_entity: sensor.foxess_pv2_power
 *   # grid_voltage_entity: sensor.foxess_grid_voltage
 *   # grid_frequency_entity: sensor.foxess_grid_frequency
 *   # bat_temp_entity: sensor.foxess_battery_temperature
 *   # residual_entity: sensor.foxess_residual_energy
 */

const OVERVIEW_VERSION = "1.0.1";

class FoxESSOverviewCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
  }

  setConfig(config) {
    // User overrides are applied after auto-discovery in _resolveEntities().
    this._userConfig = config || {};
    this._config = {};
    this._resolved = false;
  }

  _resolveEntities() {
    if (this._resolved) return;
    if (!this._hass) return;
    this._resolved = true;

    // Mapping: config key → list of candidate entity_id suffixes to try.
    // First match in hass.states wins.
    const candidates = {
      solar_entity:             ["solar_power", "pv_power"],
      house_entity:             ["house_load", "loads_power"],
      grid_import_entity:       ["grid_consumption"],
      grid_export_entity:       ["grid_feed_in", "feedin_power"],
      battery_charge_entity:    ["charge_rate", "bat_charge_power"],
      battery_discharge_entity: ["discharge_rate", "bat_discharge_power"],
      soc_entity:               ["battery_soc"],
      work_mode_entity:         ["work_mode"],
      pv1_entity:               ["pv1_power"],
      pv2_entity:               ["pv2_power"],
      grid_voltage_entity:      ["grid_voltage"],
      grid_frequency_entity:    ["grid_frequency"],
      bat_temp_entity:          ["battery_temperature", "bat_temperature"],
      residual_entity:          ["residual_energy"],
    };

    const resolved = {};
    for (const [key, suffixes] of Object.entries(candidates)) {
      if (this._userConfig[key]) {
        resolved[key] = this._userConfig[key];
        continue;
      }
      let found = null;
      for (const suffix of suffixes) {
        const eid = `sensor.foxess_${suffix}`;
        if (eid in this._hass.states) {
          found = eid;
          break;
        }
      }
      resolved[key] = found || `sensor.foxess_${suffixes[0]}`;
    }

    this._config = { ...this._userConfig, ...resolved };
  }

  set hass(hass) {
    this._hass = hass;
    this._resolveEntities();
    this._render();
  }

  getCardSize() {
    return 5;
  }

  static getStubConfig() {
    return {};
  }

  // -- Helpers ---------------------------------------------------------------

  _exists(entityId) {
    return this._hass && entityId in this._hass.states;
  }

  _num(entityId) {
    if (!this._hass) return null;
    const e = this._hass.states[entityId];
    if (!e || e.state === "unavailable" || e.state === "unknown") return null;
    const v = parseFloat(e.state);
    return Number.isNaN(v) ? null : v;
  }

  _str(entityId) {
    if (!this._hass) return null;
    const e = this._hass.states[entityId];
    if (!e || e.state === "unavailable" || e.state === "unknown") return null;
    return e.state;
  }

  _formatKw(kw) {
    if (kw == null) return "—";
    if (Math.abs(kw) >= 10) return `${kw.toFixed(1)} kW`;
    if (Math.abs(kw) >= 1) return `${kw.toFixed(2)} kW`;
    const w = Math.round(kw * 1000);
    return `${w} W`;
  }

  // -- Rendering -------------------------------------------------------------

  _render() {
    if (!this._hass) return;

    const c = this._config;
    const solar = this._num(c.solar_entity);
    const house = this._num(c.house_entity);
    const gridImport = this._num(c.grid_import_entity);
    const gridExport = this._num(c.grid_export_entity);
    const batCharge = this._num(c.battery_charge_entity);
    const batDischarge = this._num(c.battery_discharge_entity);
    const soc = this._num(c.soc_entity);
    const workMode = this._str(c.work_mode_entity);
    const pv1 = this._num(c.pv1_entity);
    const pv2 = this._num(c.pv2_entity);
    const gridV = this._num(c.grid_voltage_entity);
    const gridHz = this._num(c.grid_frequency_entity);
    const batTemp = this._num(c.bat_temp_entity);
    const residual = this._num(c.residual_entity);

    // Track which core entities exist in HA at all
    const solarFound = this._exists(c.solar_entity);
    const houseFound = this._exists(c.house_entity);
    const gridFound = this._exists(c.grid_import_entity) || this._exists(c.grid_export_entity);
    const batFound = this._exists(c.battery_charge_entity) || this._exists(c.battery_discharge_entity);

    // Net battery: positive = charging, negative = discharging
    const batNet = (batCharge || 0) - (batDischarge || 0);
    // Net grid: positive = importing, negative = exporting
    const gridNet = (gridImport || 0) - (gridExport || 0);

    // Determine which flows are active (threshold 0.01 kW)
    const solarActive = solar != null && solar > 0.01;
    const houseActive = house != null && house > 0.01;
    const gridImporting = gridNet > 0.01;
    const gridExporting = gridNet < -0.01;
    const batCharging = batNet > 0.01;
    const batDischarging = batNet < -0.01;

    // SoC bar
    const socPct = soc != null ? Math.max(0, Math.min(100, Math.round(soc))) : 0;
    let socColor = "var(--success-color, #4caf50)";
    if (socPct <= 15) socColor = "var(--error-color, #f44336)";
    else if (socPct <= 30) socColor = "var(--warning-color, #ff9800)";

    this.shadowRoot.innerHTML = `
      <style>${FoxESSOverviewCard._styles()}</style>
      <ha-card>
        <div class="header">
          <div class="title">FoxESS Overview</div>
          ${workMode ? `<span class="work-mode">${this._formatWorkMode(workMode)}</span>` : ""}
        </div>
        <div class="flow-container">
          <div class="flow-grid">
            ${this._renderNode("solar", "☀️", "Solar", solarFound, this._formatKw(solar), solarActive, pv1 != null || pv2 != null ? this._pvDetail(pv1, pv2) : "", c.solar_entity)}
            ${this._renderNode("house", "🏠", "House", houseFound, this._formatKw(house), houseActive, "", c.house_entity)}
            ${this._renderNode("grid", "⚡", "Grid", gridFound, this._formatKw(Math.abs(gridNet)), gridImporting || gridExporting, this._gridSub(gridImporting, gridExporting, gridV, gridHz), c.grid_import_entity)}
            ${this._renderBatteryNode(soc, socPct, socColor, batNet, batCharging, batDischarging, batTemp, residual, batFound)}
          </div>
          ${this._renderFlowLines(solarActive, houseActive, gridImporting, gridExporting, batCharging, batDischarging)}
        </div>
      </ha-card>
    `;
  }

  _formatWorkMode(mode) {
    if (!mode) return "";
    return mode.replace(/([a-z])([A-Z])/g, "$1 $2")
               .replace(/_/g, " ")
               .replace(/\b\w/g, c => c.toUpperCase());
  }

  _pvDetail(pv1, pv2) {
    const parts = [];
    if (pv1 != null) parts.push(`PV1 ${this._formatKw(pv1)}`);
    if (pv2 != null) parts.push(`PV2 ${this._formatKw(pv2)}`);
    return parts.join(" · ");
  }

  _gridSub(importing, exporting, voltage, freq) {
    const parts = [];
    if (importing) parts.push("Import");
    else if (exporting) parts.push("Export");
    if (voltage != null) parts.push(`${voltage.toFixed(0)}V`);
    if (freq != null) parts.push(`${freq.toFixed(1)}Hz`);
    return parts.join(" · ");
  }

  _renderNode(cls, icon, label, found, value, active, sub, entityId) {
    if (!found) {
      return `
        <div class="node ${cls} not-found">
          <div class="node-icon">${icon}</div>
          <div class="node-value">—</div>
          <div class="node-label">${label}</div>
          <div class="node-sub">${entityId || "entity"} not found</div>
        </div>
      `;
    }
    return `
      <div class="node ${cls} ${active ? "active" : "inactive"}">
        <div class="node-icon">${icon}</div>
        <div class="node-value">${value}</div>
        <div class="node-label">${label}</div>
        ${sub ? `<div class="node-sub">${sub}</div>` : ""}
      </div>
    `;
  }

  _renderBatteryNode(soc, socPct, socColor, batNet, charging, discharging, temp, residual, found) {
    if (!found) {
      return `
        <div class="node battery not-found">
          <div class="node-icon">🔋</div>
          <div class="node-value">—</div>
          <div class="node-label">Battery</div>
          <div class="node-sub">entity not found</div>
        </div>
      `;
    }
    const batPower = Math.abs(batNet);
    const active = charging || discharging;
    const direction = charging ? "Charging" : discharging ? "Discharging" : "";
    const sub = [];
    if (temp != null) sub.push(`${temp.toFixed(1)}°C`);
    if (residual != null) sub.push(`${residual.toFixed(1)} kWh`);

    return `
      <div class="node battery ${active ? "active" : "inactive"}">
        <div class="bat-header">
          <svg class="bat-svg" viewBox="0 0 24 14" width="28" height="16">
            <rect x="0.5" y="0.5" width="20" height="13" rx="2" ry="2"
                  fill="none" stroke="currentColor" stroke-width="1"/>
            <rect x="20.5" y="4" width="3" height="6" rx="1" ry="1"
                  fill="currentColor"/>
            <rect x="2" y="2" width="${(socPct / 100) * 17}" height="10" rx="1" ry="1"
                  fill="${socColor}"/>
          </svg>
          <span class="bat-soc">${soc != null ? Math.round(soc) + "%" : "—"}</span>
        </div>
        <div class="node-value">${active ? this._formatKw(batPower) : "—"}</div>
        <div class="node-label">Battery${direction ? " · " + direction : ""}</div>
        ${sub.length ? `<div class="node-sub">${sub.join(" · ")}</div>` : ""}
      </div>
    `;
  }

  _renderFlowLines(solarActive, houseActive, gridImporting, gridExporting, batCharging, batDischarging) {
    // Flow lines overlaid on the 2×2 grid.
    // Node centres (percentage-based to match the grid layout):
    //   solar=top-left(25%,30%)  house=top-right(75%,30%)
    //   grid=bottom-left(25%,70%)  battery=bottom-right(75%,70%)
    // Centre hub at (50%,50%).

    const nodes = {
      solar:   { x: 25, y: 28 },
      house:   { x: 75, y: 28 },
      grid:    { x: 25, y: 72 },
      battery: { x: 75, y: 72 },
    };
    const cx = 50, cy = 50;

    const flows = [];

    if (solarActive) {
      flows.push({ from: nodes.solar, to: { x: cx, y: cy }, color: "var(--fo-solar)", cls: "flow-solar" });
    }
    if (houseActive) {
      flows.push({ from: { x: cx, y: cy }, to: nodes.house, color: "var(--fo-house)", cls: "flow-house" });
    }
    if (gridImporting) {
      flows.push({ from: nodes.grid, to: { x: cx, y: cy }, color: "var(--fo-grid-import)", cls: "flow-grid-in" });
    } else if (gridExporting) {
      flows.push({ from: { x: cx, y: cy }, to: nodes.grid, color: "var(--fo-grid-export)", cls: "flow-grid-out" });
    }
    if (batCharging) {
      flows.push({ from: { x: cx, y: cy }, to: nodes.battery, color: "var(--fo-bat-charge)", cls: "flow-bat-in" });
    } else if (batDischarging) {
      flows.push({ from: nodes.battery, to: { x: cx, y: cy }, color: "var(--fo-bat-discharge)", cls: "flow-bat-out" });
    }

    if (flows.length === 0) return "";

    const lines = flows.map(f => {
      const dx = f.to.x - f.from.x;
      const dy = f.to.y - f.from.y;
      const len = Math.sqrt(dx * dx + dy * dy);
      return `
        <line x1="${f.from.x}" y1="${f.from.y}" x2="${f.to.x}" y2="${f.to.y}"
              stroke="${f.color}" stroke-width="0.8" stroke-linecap="round" opacity="0.3"/>
        <circle r="1.5" fill="${f.color}" class="${f.cls}">
          <animateMotion dur="${(len / 20).toFixed(1)}s" repeatCount="indefinite"
            path="M${f.from.x},${f.from.y} L${f.to.x},${f.to.y}"/>
        </circle>
      `;
    }).join("");

    return `
      <svg class="flow-overlay" viewBox="0 0 100 100" preserveAspectRatio="none">
        ${lines}
      </svg>
    `;
  }

  // -- Styles ----------------------------------------------------------------

  static _styles() {
    return `
      :host {
        --fo-solar: #f9a825;
        --fo-house: #42a5f5;
        --fo-grid-import: #ef5350;
        --fo-grid-export: #66bb6a;
        --fo-bat-charge: #66bb6a;
        --fo-bat-discharge: #ff9800;
      }

      ha-card { overflow: hidden; }

      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 16px 20px 8px;
      }
      .title {
        font-size: 16px;
        font-weight: 600;
        color: var(--primary-text-color);
      }
      .work-mode {
        font-size: 11px;
        font-weight: 600;
        padding: 3px 10px;
        border-radius: 20px;
        background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12);
        color: var(--primary-color);
        white-space: nowrap;
      }

      /* Container positions the grid and the overlay SVG on top of each other */
      .flow-container {
        position: relative;
        padding: 8px 16px 16px;
      }

      /* 2×2 node grid */
      .flow-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
        position: relative;
        z-index: 1;
      }

      /* Flow lines overlaid on the grid */
      .flow-overlay {
        position: absolute;
        top: 8px;
        left: 16px;
        right: 16px;
        bottom: 16px;
        z-index: 0;
        pointer-events: none;
      }

      .node {
        border-radius: 12px;
        padding: 12px;
        text-align: center;
        transition: opacity 0.3s;
        background-clip: padding-box;
      }
      .node.inactive {
        opacity: 0.45;
      }
      .node.not-found {
        opacity: 0.3;
      }

      .node.solar {
        background: rgba(249, 168, 37, 0.08);
        border: 1px solid rgba(249, 168, 37, 0.18);
      }
      .node.house {
        background: rgba(66, 165, 245, 0.08);
        border: 1px solid rgba(66, 165, 245, 0.18);
      }
      .node.grid {
        background: rgba(158, 158, 158, 0.08);
        border: 1px solid rgba(158, 158, 158, 0.18);
      }
      .node.battery {
        background: rgba(76, 175, 80, 0.08);
        border: 1px solid rgba(76, 175, 80, 0.18);
      }

      .node-icon {
        font-size: 22px;
        line-height: 1;
        margin-bottom: 4px;
      }
      .node-value {
        font-size: 16px;
        font-weight: 700;
        color: var(--primary-text-color);
        margin-bottom: 2px;
      }
      .node-label {
        font-size: 11px;
        font-weight: 600;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.03em;
      }
      .node-sub {
        font-size: 10px;
        color: var(--secondary-text-color);
        margin-top: 3px;
        opacity: 0.8;
      }

      /* Battery node extras */
      .bat-header {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        margin-bottom: 4px;
      }
      .bat-svg {
        color: var(--primary-text-color);
      }
      .bat-soc {
        font-size: 16px;
        font-weight: 700;
        color: var(--primary-text-color);
      }
    `;
  }
}

customElements.define("foxess-overview-card", FoxESSOverviewCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "foxess-overview-card",
  name: "FoxESS Overview",
  description: "Inverter energy flow overview with solar, battery, grid and house",
  preview: true,
});

console.info(`%c FoxESS Overview Card v${OVERVIEW_VERSION} `, "color:#fff;background:#f9a825;font-weight:bold;border-radius:4px;padding:2px 6px");
