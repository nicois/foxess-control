/**
 * FoxESS Forecast Card — projected SoC chart.
 *
 * Reads the battery_forecast entity's `forecast` attribute
 * ([{time: epoch_ms, soc: float}]) and renders an SVG line chart.
 *
 * Usage:
 *   type: custom:foxess-forecast-card
 */

const FORECAST_VERSION = "1.0.0";

const _FC_TRANSLATIONS = {
  en: { title: "Battery Forecast", soc: "SoC", now: "Now", target: "Target", min: "Min", no_data: "No forecast data" },
  de: { title: "Batterieprognose", soc: "SoC", now: "Jetzt", target: "Ziel", min: "Min", no_data: "Keine Prognosedaten" },
  fr: { title: "Prévision batterie", soc: "SoC", now: "Maint.", target: "Objectif", min: "Min", no_data: "Aucune prévision" },
  nl: { title: "Batterijprognose", soc: "SoC", now: "Nu", target: "Doel", min: "Min", no_data: "Geen prognosedata" },
  es: { title: "Previsión batería", soc: "SoC", now: "Ahora", target: "Obj.", min: "Mín", no_data: "Sin previsión" },
  it: { title: "Previsione batteria", soc: "SoC", now: "Ora", target: "Obiet.", min: "Min", no_data: "Nessuna previsione" },
  pl: { title: "Prognoza baterii", soc: "SoC", now: "Teraz", target: "Cel", min: "Min", no_data: "Brak prognozy" },
  pt: { title: "Previsão bateria", soc: "SoC", now: "Agora", target: "Obj.", min: "Mín", no_data: "Sem previsão" },
  "zh-hans": { title: "电池预测", soc: "SoC", now: "现在", target: "目标", min: "最小", no_data: "无预测数据" },
  ja: { title: "バッテリー予測", soc: "SoC", now: "現在", target: "目標", min: "最小", no_data: "予測データなし" },
};

function _fcGetStrings(lang) {
  if (!lang) return _FC_TRANSLATIONS.en;
  const lc = lang.toLowerCase();
  return _FC_TRANSLATIONS[lc] || _FC_TRANSLATIONS[lc.split("-")[0]] || _FC_TRANSLATIONS.en;
}

class FoxESSForecastCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._entityMap = null;
    this._fetchPending = false;
  }

  setConfig(config) {
    this._config = config || {};
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entityMap && !this._fetchPending) {
      this._fetchPending = true;
      this._fetchEntityMap();
    }
    this._render();
  }

  async _fetchEntityMap() {
    try {
      this._entityMap = await this._hass.callWS({ type: "foxess_control/entity_map" });
    } catch (_e) {
      this._entityMap = {};
    }
    this._fetchPending = false;
    this._render();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return {};
  }

  _t(key) {
    const lang = this._hass && (this._hass.language || (this._hass.locale && this._hass.locale.language));
    return _fcGetStrings(lang)[key] || _FC_TRANSLATIONS.en[key] || key;
  }

  _resolve(key) {
    if (this._config[key]) return this._config[key];
    const roleMap = {
      forecast_entity: "battery_forecast",
      operations_entity: "smart_operations",
    };
    const role = roleMap[key];
    if (role && this._entityMap && this._entityMap[role]) return this._entityMap[role];
    return null;
  }

  _render() {
    if (!this._hass) return;

    const forecastEntity = this._resolve("forecast_entity") || "sensor.foxess_battery_forecast";
    const opsEntity = this._resolve("operations_entity") || "sensor.foxess_smart_operations";

    const fState = this._hass.states[forecastEntity];
    const oState = this._hass.states[opsEntity];
    const forecast = fState?.attributes?.forecast || [];
    const targetSoc = oState?.attributes?.charge_target_soc ?? oState?.attributes?.discharge_min_soc ?? null;
    const isCharge = oState?.attributes?.charge_active === true;

    const lang = this._hass.language || (this._hass.locale && this._hass.locale.language);
    const strings = _fcGetStrings(lang);

    if (forecast.length < 2) {
      this.shadowRoot.innerHTML = `
        <style>${FoxESSForecastCard._styles()}</style>
        <ha-card>
          <div class="header">${strings.title}</div>
          <div class="empty">${strings.no_data}</div>
        </ha-card>
      `;
      return;
    }

    const W = 320;
    const H = 160;
    const PAD = { top: 10, right: 10, bottom: 28, left: 34 };
    const cw = W - PAD.left - PAD.right;
    const ch = H - PAD.top - PAD.bottom;

    const tMin = forecast[0].time;
    const tMax = forecast[forecast.length - 1].time;
    const tRange = tMax - tMin || 1;

    const x = (t) => PAD.left + ((t - tMin) / tRange) * cw;
    const y = (s) => PAD.top + ch - (s / 100) * ch;

    // Forecast line
    const lineColor = isCharge ? "#4caf50" : "#ff9800";
    const fillColor = isCharge ? "rgba(76,175,80,0.15)" : "rgba(255,152,0,0.15)";
    const pathD = forecast.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.time).toFixed(1)},${y(p.soc).toFixed(1)}`).join(" ");
    const areaD = `${pathD} L${x(forecast[forecast.length - 1].time).toFixed(1)},${y(0).toFixed(1)} L${x(forecast[0].time).toFixed(1)},${y(0).toFixed(1)} Z`;

    // Now marker
    const nowMs = Date.now();
    const nowX = x(Math.max(tMin, Math.min(tMax, nowMs)));
    const showNow = nowMs >= tMin && nowMs <= tMax;

    // Y-axis grid (0%, 25%, 50%, 75%, 100%)
    const yGridLines = [0, 25, 50, 75, 100].map((v) => `
      <line x1="${PAD.left}" y1="${y(v).toFixed(1)}" x2="${W - PAD.right}" y2="${y(v).toFixed(1)}" stroke="var(--divider-color,#e0e0e0)" stroke-width="0.5"/>
      <text x="${PAD.left - 4}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end" fill="var(--secondary-text-color)" font-size="9">${v}</text>
    `).join("");

    // X-axis time labels
    const hours = new Set();
    for (const p of forecast) {
      const d = new Date(p.time);
      hours.add(d.getHours());
    }
    const xLabels = [];
    const seenHours = new Set();
    for (const p of forecast) {
      const d = new Date(p.time);
      const h = d.getHours();
      if (!seenHours.has(h)) {
        seenHours.add(h);
        xLabels.push(`<text x="${x(p.time).toFixed(1)}" y="${H - 4}" text-anchor="middle" fill="var(--secondary-text-color)" font-size="9">${String(h).padStart(2, "0")}:00</text>`);
      }
    }

    // Target line
    let targetLine = "";
    if (targetSoc != null) {
      const tY = y(targetSoc).toFixed(1);
      const label = isCharge ? strings.target : strings.min;
      targetLine = `
        <line x1="${PAD.left}" y1="${tY}" x2="${W - PAD.right}" y2="${tY}" stroke="${lineColor}" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>
        <text x="${W - PAD.right + 2}" y="${(parseFloat(tY) + 3).toFixed(1)}" fill="${lineColor}" font-size="8" opacity="0.8">${label} ${targetSoc}%</text>
      `;
    }

    this.shadowRoot.innerHTML = `
      <style>${FoxESSForecastCard._styles()}</style>
      <ha-card>
        <div class="header">${strings.title}</div>
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart">
          ${yGridLines}
          <path d="${areaD}" fill="${fillColor}"/>
          <path d="${pathD}" fill="none" stroke="${lineColor}" stroke-width="2" stroke-linejoin="round"/>
          ${targetLine}
          ${showNow ? `
            <line x1="${nowX.toFixed(1)}" y1="${PAD.top}" x2="${nowX.toFixed(1)}" y2="${(H - PAD.bottom).toFixed(1)}" stroke="var(--primary-text-color)" stroke-width="1" stroke-dasharray="3,2" opacity="0.5"/>
            <text x="${(nowX + 3).toFixed(1)}" y="${PAD.top + 8}" fill="var(--primary-text-color)" font-size="8" opacity="0.6">${strings.now}</text>
          ` : ""}
          ${xLabels.join("")}
        </svg>
      </ha-card>
    `;
  }

  static _styles() {
    return `
      :host { display: block; }
      ha-card { padding: 0; overflow: hidden; }
      .header {
        padding: 12px 16px 4px;
        font-size: 14px;
        font-weight: 500;
        color: var(--primary-text-color);
      }
      .chart {
        width: 100%;
        height: auto;
        display: block;
        padding: 4px 8px 8px;
        box-sizing: border-box;
      }
      .empty {
        text-align: center;
        padding: 32px 16px;
        font-size: 13px;
        color: var(--secondary-text-color);
        opacity: 0.7;
      }
    `;
  }
}

customElements.define("foxess-forecast-card", FoxESSForecastCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "foxess-forecast-card",
  name: "FoxESS Forecast",
  description: "Projected battery SoC chart for smart sessions",
  preview: true,
});

console.info(`%c FoxESS Forecast Card v${FORECAST_VERSION} `, "color:#fff;background:#ff9800;font-weight:bold;border-radius:4px;padding:2px 6px");
