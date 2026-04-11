/**
 * FoxESS Overview — custom Lovelace card.
 *
 * Shows live energy flows between solar, battery, grid and house
 * with power values and key inverter stats.
 *
 * Entity discovery uses a WebSocket call to the foxess_control
 * integration, which returns the exact entity_ids from the HA
 * entity registry. No name guessing required.
 *
 * Usage:
 *   type: custom:foxess-overview-card
 *   # All entities auto-discovered; override any manually:
 *   # solar_entity: sensor.foxess_solar_power
 *   # house_entity: sensor.foxess_house_load
 *   # etc.
 */

const OVERVIEW_VERSION = "2.0.0";

// Config key → role name returned by the foxess_control/entity_map WS command.
const _ROLE_MAP = {
  solar_entity:             "solar_power",
  house_entity:             "house_load",
  grid_import_entity:       "grid_consumption",
  grid_export_entity:       "grid_feed_in",
  battery_charge_entity:    "charge_rate",
  battery_discharge_entity: "discharge_rate",
  soc_entity:               "battery_soc",
  work_mode_entity:         "work_mode",
  pv1_entity:               "pv1_power",
  pv2_entity:               "pv2_power",
  grid_voltage_entity:      "grid_voltage",
  grid_frequency_entity:    "grid_frequency",
  bat_temp_entity:          "battery_temperature",
  residual_entity:          "residual_energy",
};

class FoxESSOverviewCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._userConfig = {};
    this._hass = null;
    this._entityMap = null;      // role → entity_id from WS
    this._fetchPending = false;
  }

  setConfig(config) {
    this._userConfig = config || {};
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entityMap && !this._fetchPending) {
      this._fetchEntityMap();
    }
    this._render();
  }

  getCardSize() {
    return 5;
  }

  static getStubConfig() {
    return {};
  }

  // -- Entity discovery via WebSocket -----------------------------------------

  async _fetchEntityMap() {
    this._fetchPending = true;
    try {
      this._entityMap = await this._hass.callWS({
        type: "foxess_control/entity_map",
      });
    } catch (e) {
      // Integration may not be loaded yet or WS command not registered
      console.warn("FoxESS Overview: could not fetch entity map", e);
      this._entityMap = {};
    }
    this._fetchPending = false;
    this._render();
  }

  /** Resolve a config key to an entity_id. */
  _resolve(key) {
    // 1. User explicitly set this entity
    if (this._userConfig[key]) return this._userConfig[key];
    // 2. WS entity map
    const role = _ROLE_MAP[key];
    if (role && this._entityMap && this._entityMap[role]) {
      return this._entityMap[role];
    }
    return null;
  }

  // -- Helpers ---------------------------------------------------------------

  _exists(entityId) {
    return entityId && this._hass && entityId in this._hass.states;
  }

  _num(entityId) {
    if (!entityId || !this._hass) return null;
    const e = this._hass.states[entityId];
    if (!e || e.state === "unavailable" || e.state === "unknown") return null;
    const v = parseFloat(e.state);
    return Number.isNaN(v) ? null : v;
  }

  _str(entityId) {
    if (!entityId || !this._hass) return null;
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

    // Resolve entity IDs
    const eid = {};
    for (const key of Object.keys(_ROLE_MAP)) {
      eid[key] = this._resolve(key);
    }

    const solar = this._num(eid.solar_entity);
    const house = this._num(eid.house_entity);
    const gridImport = this._num(eid.grid_import_entity);
    const gridExport = this._num(eid.grid_export_entity);
    const batCharge = this._num(eid.battery_charge_entity);
    const batDischarge = this._num(eid.battery_discharge_entity);
    const soc = this._num(eid.soc_entity);
    const workMode = this._str(eid.work_mode_entity);
    const pv1 = this._num(eid.pv1_entity);
    const pv2 = this._num(eid.pv2_entity);
    const gridV = this._num(eid.grid_voltage_entity);
    const gridHz = this._num(eid.grid_frequency_entity);
    const batTemp = this._num(eid.bat_temp_entity);
    const residual = this._num(eid.residual_entity);

    const solarFound = this._exists(eid.solar_entity);
    const houseFound = this._exists(eid.house_entity);
    const gridFound = this._exists(eid.grid_import_entity) || this._exists(eid.grid_export_entity);
    const batFound = this._exists(eid.battery_charge_entity) || this._exists(eid.battery_discharge_entity);

    const batNet = (batCharge || 0) - (batDischarge || 0);
    const gridNet = (gridImport || 0) - (gridExport || 0);

    const solarActive = solar != null && solar > 0.01;
    const houseActive = house != null && house > 0.01;
    const gridImporting = gridNet > 0.01;
    const gridExporting = gridNet < -0.01;
    const batCharging = batNet > 0.01;
    const batDischarging = batNet < -0.01;

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
        <div class="flow-grid">
          ${this._renderNode("solar", "☀️", "Solar", solarFound, this._formatKw(solar), solarActive, pv1 != null || pv2 != null ? this._pvDetail(pv1, pv2) : "", eid.solar_entity)}
          ${this._renderNode("house", "🏠", "House", houseFound, this._formatKw(house), houseActive, "", eid.house_entity)}
          ${this._renderNode("grid", "⚡", "Grid", gridFound, this._formatKw(Math.abs(gridNet)), gridImporting || gridExporting, this._gridSub(gridImporting, gridExporting, gridV, gridHz), eid.grid_import_entity)}
          ${this._renderBatteryNode(soc, socPct, socColor, batNet, batCharging, batDischarging, batTemp, residual, batFound)}
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
          <div class="node-sub">${entityId ? entityId + " not found" : "not discovered"}</div>
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
          <div class="node-sub">not discovered</div>
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
    // SVG absolutely positioned over the grid.
    // Node centres at (25%,25%), (75%,25%), (25%,75%), (75%,75%).
    const nodes = {
      solar:   { x: 25, y: 25 },
      house:   { x: 75, y: 25 },
      grid:    { x: 25, y: 75 },
      battery: { x: 75, y: 75 },
    };
    const cx = 50, cy = 50;

    const flows = [];
    if (solarActive) flows.push({ from: nodes.solar, to: { x: cx, y: cy }, color: "var(--fo-solar)" });
    if (houseActive) flows.push({ from: { x: cx, y: cy }, to: nodes.house, color: "var(--fo-house)" });
    if (gridImporting) flows.push({ from: nodes.grid, to: { x: cx, y: cy }, color: "var(--fo-grid-import)" });
    else if (gridExporting) flows.push({ from: { x: cx, y: cy }, to: nodes.grid, color: "var(--fo-grid-export)" });
    if (batCharging) flows.push({ from: { x: cx, y: cy }, to: nodes.battery, color: "var(--fo-bat-charge)" });
    else if (batDischarging) flows.push({ from: nodes.battery, to: { x: cx, y: cy }, color: "var(--fo-bat-discharge)" });

    const lines = flows.map(f => {
      const dx = f.to.x - f.from.x;
      const dy = f.to.y - f.from.y;
      const len = Math.sqrt(dx * dx + dy * dy);
      return `
        <line x1="${f.from.x}" y1="${f.from.y}" x2="${f.to.x}" y2="${f.to.y}"
              stroke="${f.color}" stroke-width="0.8" stroke-linecap="round" opacity="0.3"/>
        <circle r="1.5" fill="${f.color}">
          <animateMotion dur="${(len / 20).toFixed(1)}s" repeatCount="indefinite"
            path="M${f.from.x},${f.from.y} L${f.to.x},${f.to.y}"/>
        </circle>
      `;
    }).join("");

    return `<svg class="flow-overlay" viewBox="0 0 100 100" preserveAspectRatio="none">${lines}</svg>`;
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

      .flow-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
        padding: 8px 16px 16px;
        position: relative;
      }

      .flow-overlay {
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
      }

      .node {
        border-radius: 12px;
        padding: 12px;
        text-align: center;
        transition: opacity 0.3s;
      }
      .node.inactive { opacity: 0.45; }
      .node.not-found { opacity: 0.3; }

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

      .node-icon { font-size: 22px; line-height: 1; margin-bottom: 4px; }
      .node-value { font-size: 16px; font-weight: 700; color: var(--primary-text-color); margin-bottom: 2px; }
      .node-label { font-size: 11px; font-weight: 600; color: var(--secondary-text-color); text-transform: uppercase; letter-spacing: 0.03em; }
      .node-sub { font-size: 10px; color: var(--secondary-text-color); margin-top: 3px; opacity: 0.8; }

      .bat-header { display: flex; align-items: center; justify-content: center; gap: 6px; margin-bottom: 4px; }
      .bat-svg { color: var(--primary-text-color); }
      .bat-soc { font-size: 16px; font-weight: 700; color: var(--primary-text-color); }
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
