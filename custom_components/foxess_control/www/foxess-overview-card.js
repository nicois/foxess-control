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

const OVERVIEW_VERSION = "2.3.0";

// -- i18n --------------------------------------------------------------------

const _OV_TRANSLATIONS = {
  en: {
    title: "FoxESS Overview",
    solar: "Solar",
    house: "House",
    grid: "Grid",
    battery: "Battery",
    importing: "Importing ↓",
    exporting: "Exporting ↑",
    charging: "Charging",
    discharging: "Discharging",
    not_found: "not found",
    not_discovered: "not discovered",
  },
  de: {
    title: "FoxESS Übersicht",
    solar: "Solar",
    house: "Haus",
    grid: "Netz",
    battery: "Batterie",
    importing: "Bezug ↓",
    exporting: "Einspeisung ↑",
    charging: "Laden",
    discharging: "Entladen",
    not_found: "nicht gefunden",
    not_discovered: "nicht erkannt",
  },
  fr: {
    title: "FoxESS Aperçu",
    solar: "Solaire",
    house: "Maison",
    grid: "Réseau",
    battery: "Batterie",
    importing: "Import ↓",
    exporting: "Export ↑",
    charging: "Charge",
    discharging: "Décharge",
    not_found: "introuvable",
    not_discovered: "non détecté",
  },
  nl: {
    title: "FoxESS Overzicht",
    solar: "Zon",
    house: "Huis",
    grid: "Net",
    battery: "Batterij",
    importing: "Afname ↓",
    exporting: "Teruglevering ↑",
    charging: "Laden",
    discharging: "Ontladen",
    not_found: "niet gevonden",
    not_discovered: "niet ontdekt",
  },
  es: {
    title: "FoxESS Resumen",
    solar: "Solar",
    house: "Casa",
    grid: "Red",
    battery: "Batería",
    importing: "Importando ↓",
    exporting: "Exportando ↑",
    charging: "Cargando",
    discharging: "Descargando",
    not_found: "no encontrado",
    not_discovered: "no detectado",
  },
  it: {
    title: "FoxESS Panoramica",
    solar: "Solare",
    house: "Casa",
    grid: "Rete",
    battery: "Batteria",
    importing: "Importazione ↓",
    exporting: "Esportazione ↑",
    charging: "Ricarica",
    discharging: "Scarica",
    not_found: "non trovato",
    not_discovered: "non rilevato",
  },
  pl: {
    title: "FoxESS Przegląd",
    solar: "Solar",
    house: "Dom",
    grid: "Sieć",
    battery: "Bateria",
    importing: "Pobieranie ↓",
    exporting: "Oddawanie ↑",
    charging: "Ładowanie",
    discharging: "Rozładowanie",
    not_found: "nie znaleziono",
    not_discovered: "nie wykryto",
  },
  pt: {
    title: "FoxESS Visão geral",
    solar: "Solar",
    house: "Casa",
    grid: "Rede",
    battery: "Bateria",
    importing: "Importação ↓",
    exporting: "Exportação ↑",
    charging: "Carregando",
    discharging: "Descarregando",
    not_found: "não encontrado",
    not_discovered: "não detetado",
  },
  "zh-hans": {
    title: "FoxESS 概览",
    solar: "光伏",
    house: "家庭",
    grid: "电网",
    battery: "电池",
    importing: "用电 ↓",
    exporting: "馈电 ↑",
    charging: "充电中",
    discharging: "放电中",
    not_found: "未找到",
    not_discovered: "未发现",
  },
  ja: {
    title: "FoxESS 概要",
    solar: "太陽光",
    house: "家庭",
    grid: "系統",
    battery: "バッテリー",
    importing: "買電 ↓",
    exporting: "売電 ↑",
    charging: "充電中",
    discharging: "放電中",
    not_found: "見つかりません",
    not_discovered: "未検出",
  },
};

function _ovGetStrings(lang) {
  if (!lang) return _OV_TRANSLATIONS.en;
  const lc = lang.toLowerCase();
  return _OV_TRANSLATIONS[lc] || _OV_TRANSLATIONS[lc.split("-")[0]] || _OV_TRANSLATIONS.en;
}

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
    // Retry discovery if the map is missing or empty (e.g. after reload)
    const needsFetch =
      !this._entityMap ||
      (typeof this._entityMap === "object" &&
        Object.keys(this._entityMap).length === 0);
    if (needsFetch && !this._fetchPending) {
      const now = Date.now();
      if (!this._lastFetchAttempt || now - this._lastFetchAttempt > 10000) {
        this._lastFetchAttempt = now;
        this._fetchEntityMap();
      }
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

  _t(key) {
    const lang = this._hass && (this._hass.language || (this._hass.locale && this._hass.locale.language));
    const strings = _ovGetStrings(lang);
    return strings[key] || _OV_TRANSLATIONS.en[key] || key;
  }

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

  /** Read the data_source attribute from any resolved entity. */
  _getDataSource(eid) {
    for (const key of Object.keys(_ROLE_MAP)) {
      const entityId = eid[key];
      if (!entityId || !this._hass) continue;
      const e = this._hass.states[entityId];
      if (e && e.attributes && e.attributes.data_source) {
        return e.attributes.data_source;
      }
    }
    return null;
  }

  _dataSourceBadge(source) {
    if (!source) return "";
    const labels = { ws: "WS", api: "API", modbus: "Modbus" };
    const label = labels[source] || source;
    return `<span class="data-source" title="Data: ${label}">${label}</span>`;
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
    const houseActive = house != null;
    const gridImporting = gridNet > 0.01;
    const gridExporting = gridNet < -0.01;
    const batCharging = batNet > 0.01;
    const batDischarging = batNet < -0.01;

    const socPct = soc != null ? Math.max(0, Math.min(100, Math.round(soc))) : 0;
    let socColor = "var(--success-color, #4caf50)";
    if (socPct <= 15) socColor = "var(--error-color, #f44336)";
    else if (socPct <= 30) socColor = "var(--warning-color, #ff9800)";

    const dataSource = this._getDataSource(eid);

    this.shadowRoot.innerHTML = `
      <style>${FoxESSOverviewCard._styles()}</style>
      <ha-card>
        <div class="header">
          <div class="title">${this._t("title")}${this._dataSourceBadge(dataSource)}</div>
          ${workMode && workMode !== "SelfUse" ? `<span class="work-mode">${this._formatWorkMode(workMode)}</span>` : ""}
        </div>
        <div class="flow-grid">
          ${this._renderNode("solar", "☀️", this._t("solar"), solarFound, this._formatKw(solar), solarActive, pv1 != null || pv2 != null ? this._pvDetail(pv1, pv2) : "", eid.solar_entity)}
          ${this._renderNode("house", "🏠", this._t("house"), houseFound, this._formatKw(house), houseActive, "", eid.house_entity)}
          ${this._renderGridNode(gridFound, gridNet, gridImporting, gridExporting, gridV, gridHz, eid.grid_import_entity)}
          ${this._renderBatteryNode(soc, socPct, socColor, batNet, batCharging, batDischarging, batTemp, residual, batFound)}
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

  _renderGridNode(found, gridNet, importing, exporting, voltage, freq, entityId) {
    if (!found) {
      return `
        <div class="node grid not-found">
          <div class="node-icon">⚡</div>
          <div class="node-value">—</div>
          <div class="node-label">${this._t("grid")}</div>
          <div class="node-sub">${entityId ? entityId + " " + this._t("not_found") : this._t("not_discovered")}</div>
        </div>
      `;
    }
    const active = importing || exporting;
    const direction = importing ? this._t("importing") : exporting ? this._t("exporting") : "";
    const sub = [];
    if (voltage != null) sub.push(`${voltage.toFixed(0)}V`);
    if (freq != null) sub.push(`${freq.toFixed(1)}Hz`);
    return `
      <div class="node grid ${active ? "active" : "inactive"}">
        <div class="node-icon">⚡</div>
        <div class="node-value">${active ? this._formatKw(Math.abs(gridNet)) : "—"}</div>
        <div class="node-label">${this._t("grid")}${direction ? " · " + direction : ""}</div>
        ${sub.length ? `<div class="node-sub">${sub.join(" · ")}</div>` : ""}
      </div>
    `;
  }

  _renderNode(cls, icon, label, found, value, active, sub, entityId) {
    if (!found) {
      return `
        <div class="node ${cls} not-found">
          <div class="node-icon">${icon}</div>
          <div class="node-value">—</div>
          <div class="node-label">${label}</div>
          <div class="node-sub">${entityId ? entityId + " " + this._t("not_found") : this._t("not_discovered")}</div>
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
          <div class="node-label">${this._t("battery")}</div>
          <div class="node-sub">${this._t("not_discovered")}</div>
        </div>
      `;
    }
    const batPower = Math.abs(batNet);
    const active = charging || discharging;
    const direction = charging ? this._t("charging") : discharging ? this._t("discharging") : "";
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
        <div class="node-label">${this._t("battery")}${direction ? " · " + direction : ""}</div>
        ${sub.length ? `<div class="node-sub">${sub.join(" · ")}</div>` : ""}
      </div>
    `;
  }

  // -- Styles ----------------------------------------------------------------

  static _styles() {
    return `
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
      .data-source {
        font-size: 9px;
        font-weight: 600;
        padding: 1px 5px;
        border-radius: 4px;
        background: var(--secondary-background-color, rgba(0,0,0,0.06));
        color: var(--secondary-text-color);
        margin-left: 6px;
        vertical-align: middle;
        letter-spacing: 0.03em;
      }

      .flow-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
        padding: 8px 16px 16px;
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
