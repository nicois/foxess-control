/**
 * FoxESS History Card — session timeline over 24h.
 *
 * Reads HA history API for smart_operations state changes and
 * battery SoC, rendering a horizontal timeline with session bars
 * and an SoC overlay line.
 *
 * Usage:
 *   type: custom:foxess-history-card
 *   # Optional:
 *   # hours: 24          (12, 24, or 48)
 */

const HISTORY_VERSION = "1.0.0";

const _HI_TRANSLATIONS = {
  en: { title: "Session History", no_data: "No session history", hours_label: "{0}h ago", charging: "Charging", discharging: "Discharging", deferred: "Deferred", idle: "Idle" },
  de: { title: "Sitzungsverlauf", no_data: "Kein Verlauf", hours_label: "vor {0}h", charging: "Laden", discharging: "Entladen", deferred: "Verzögert", idle: "Leerlauf" },
  fr: { title: "Historique", no_data: "Aucun historique", hours_label: "il y a {0}h", charging: "Charge", discharging: "Décharge", deferred: "Différée", idle: "Repos" },
  nl: { title: "Sessiegeschiedenis", no_data: "Geen geschiedenis", hours_label: "{0}u geleden", charging: "Laden", discharging: "Ontladen", deferred: "Uitgesteld", idle: "Inactief" },
  es: { title: "Historial", no_data: "Sin historial", hours_label: "hace {0}h", charging: "Cargando", discharging: "Descargando", deferred: "Diferida", idle: "Inactivo" },
  it: { title: "Cronologia", no_data: "Nessun dato", hours_label: "{0}h fa", charging: "Ricarica", discharging: "Scarica", deferred: "Differita", idle: "Inattivo" },
  pl: { title: "Historia sesji", no_data: "Brak historii", hours_label: "{0}h temu", charging: "Ładowanie", discharging: "Rozładowanie", deferred: "Odroczone", idle: "Bezczynny" },
  pt: { title: "Histórico", no_data: "Sem histórico", hours_label: "há {0}h", charging: "A carregar", discharging: "A descarregar", deferred: "Adiado", idle: "Inativo" },
  "zh-hans": { title: "会话历史", no_data: "无历史记录", hours_label: "{0}小时前", charging: "充电中", discharging: "放电中", deferred: "延迟", idle: "空闲" },
  ja: { title: "セッション履歴", no_data: "履歴なし", hours_label: "{0}時間前", charging: "充電中", discharging: "放電中", deferred: "遅延", idle: "待機" },
};

function _hiGetStrings(lang) {
  if (!lang) return _HI_TRANSLATIONS.en;
  const lc = lang.toLowerCase();
  return _HI_TRANSLATIONS[lc] || _HI_TRANSLATIONS[lc.split("-")[0]] || _HI_TRANSLATIONS.en;
}

const _STATE_COLORS = {
  charging: "#4caf50",
  discharging: "#ff9800",
  deferred: "#9e9e9e",
  suspended: "#f44336",
};

class FoxESSHistoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._entityMap = null;
    this._fetchPending = false;
    this._historyData = null;
    this._socHistory = null;
    this._lastFetch = 0;
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
    const now = Date.now();
    if (now - this._lastFetch > 60000) {
      this._lastFetch = now;
      this._fetchHistory();
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
    this._fetchHistory();
  }

  getCardSize() {
    return 3;
  }

  static getStubConfig() {
    return {};
  }

  _resolve(key) {
    if (this._config[key]) return this._config[key];
    const roleMap = {
      operations_entity: "smart_operations",
      soc_entity: "battery_soc",
    };
    const role = roleMap[key];
    if (role && this._entityMap && this._entityMap[role]) return this._entityMap[role];
    return null;
  }

  async _fetchHistory() {
    if (!this._hass) return;
    const opsEntity = this._resolve("operations_entity") || "sensor.foxess_smart_operations";
    const socEntity = this._resolve("soc_entity") || "sensor.foxess_battery_soc";
    const hours = this._config.hours || 24;
    const start = new Date(Date.now() - hours * 3600000).toISOString();

    try {
      const resp = await this._hass.callApi(
        "GET",
        `history/period/${start}?filter_entity_id=${opsEntity},${socEntity}&minimal_response&no_attributes`
      );
      if (Array.isArray(resp)) {
        for (const series of resp) {
          if (series.length === 0) continue;
          const eid = series[0].entity_id;
          if (eid === opsEntity) this._historyData = series;
          if (eid === socEntity) this._socHistory = series;
        }
      }
    } catch (_e) {
      // History API may not be available
    }
    this._render();
  }

  _render() {
    if (!this._hass) return;
    const lang = this._hass.language || (this._hass.locale && this._hass.locale.language);
    const strings = _hiGetStrings(lang);
    const hours = this._config.hours || 24;

    if (!this._historyData || this._historyData.length < 2) {
      this.shadowRoot.innerHTML = `
        <style>${FoxESSHistoryCard._styles()}</style>
        <ha-card>
          <div class="header">${strings.title}</div>
          <div class="empty">${strings.no_data}</div>
        </ha-card>
      `;
      return;
    }

    const W = 320;
    const H = 100;
    const PAD = { top: 8, right: 10, bottom: 20, left: 34 };
    const cw = W - PAD.left - PAD.right;
    const ch = H - PAD.top - PAD.bottom;

    const now = Date.now();
    const tMin = now - hours * 3600000;
    const tMax = now;
    const tRange = tMax - tMin;

    const x = (t) => PAD.left + ((t - tMin) / tRange) * cw;
    const y = (s) => PAD.top + ch - (s / 100) * ch;

    // Session bars
    const bars = [];
    for (let i = 0; i < this._historyData.length; i++) {
      const entry = this._historyData[i];
      const state = entry.state;
      const color = _STATE_COLORS[state];
      if (!color) continue;
      const t1 = new Date(entry.last_changed).getTime();
      const t2 = i + 1 < this._historyData.length
        ? new Date(this._historyData[i + 1].last_changed).getTime()
        : now;
      const x1 = Math.max(x(t1), PAD.left);
      const x2 = Math.min(x(t2), W - PAD.right);
      if (x2 <= x1) continue;
      bars.push(`<rect x="${x1.toFixed(1)}" y="${PAD.top}" width="${(x2 - x1).toFixed(1)}" height="${ch}" fill="${color}" opacity="0.3" rx="2"/>`);
    }

    // SoC line
    let socPath = "";
    if (this._socHistory && this._socHistory.length >= 2) {
      const points = this._socHistory
        .filter((p) => p.state !== "unavailable" && p.state !== "unknown")
        .map((p) => ({
          t: new Date(p.last_changed).getTime(),
          soc: parseFloat(p.state),
        }))
        .filter((p) => !isNaN(p.soc));
      if (points.length >= 2) {
        socPath = points
          .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.soc).toFixed(1)}`)
          .join(" ");
      }
    }

    // Y grid
    const yGrid = [0, 50, 100].map((v) => `
      <line x1="${PAD.left}" y1="${y(v).toFixed(1)}" x2="${W - PAD.right}" y2="${y(v).toFixed(1)}" stroke="var(--divider-color,#e0e0e0)" stroke-width="0.5"/>
      <text x="${PAD.left - 4}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end" fill="var(--secondary-text-color)" font-size="8">${v}</text>
    `).join("");

    // X labels (every N hours)
    const xLabels = [];
    const step = hours <= 12 ? 2 : hours <= 24 ? 4 : 8;
    for (let h = step; h <= hours; h += step) {
      const t = now - (hours - h) * 3600000;
      const d = new Date(t);
      xLabels.push(`<text x="${x(t).toFixed(1)}" y="${H - 4}" text-anchor="middle" fill="var(--secondary-text-color)" font-size="8">${String(d.getHours()).padStart(2, "0")}:00</text>`);
    }

    // Legend
    const legendItems = [
      { color: _STATE_COLORS.charging, label: strings.charging },
      { color: _STATE_COLORS.discharging, label: strings.discharging },
      { color: _STATE_COLORS.deferred, label: strings.deferred },
    ];
    const legend = legendItems.map((it) =>
      `<span class="legend-item"><span class="legend-dot" style="background:${it.color}"></span>${it.label}</span>`
    ).join("");

    this.shadowRoot.innerHTML = `
      <style>${FoxESSHistoryCard._styles()}</style>
      <ha-card>
        <div class="header">${strings.title}<div class="legend">${legend}</div></div>
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart">
          ${yGrid}
          ${bars.join("")}
          ${socPath ? `<path d="${socPath}" fill="none" stroke="#2196f3" stroke-width="1.5" stroke-linejoin="round" opacity="0.8"/>` : ""}
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
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px 4px;
        font-size: 14px;
        font-weight: 500;
        color: var(--primary-text-color);
      }
      .legend {
        display: flex;
        gap: 10px;
        font-size: 10px;
        font-weight: 400;
        color: var(--secondary-text-color);
      }
      .legend-item {
        display: flex;
        align-items: center;
        gap: 3px;
      }
      .legend-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 2px;
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

customElements.define("foxess-history-card", FoxESSHistoryCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "foxess-history-card",
  name: "FoxESS History",
  description: "Session timeline with SoC trace over the last 24 hours",
  preview: true,
});

console.info(`%c FoxESS History Card v${HISTORY_VERSION} `, "color:#fff;background:#2196f3;font-weight:bold;border-radius:4px;padding:2px 6px");
