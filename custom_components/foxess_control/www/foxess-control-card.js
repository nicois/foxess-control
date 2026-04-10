/**
 * FoxESS Control — custom Lovelace card.
 *
 * Renders smart charge / discharge status with a battery gauge,
 * progress indicators, and an SVG forecast sparkline.
 *
 * Usage:
 *   type: custom:foxess-control-card
 *   # Optional overrides (auto-discovered by default):
 *   # operations_entity: sensor.foxess_smart_operations
 *   # forecast_entity: sensor.foxess_battery_forecast
 *   # soc_entity: sensor.foxess_battery_soc
 */

const CARD_VERSION = "1.0.0";

class FoxESSControlCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
  }

  // -- Lovelace lifecycle ----------------------------------------------------

  setConfig(config) {
    this._config = {
      operations_entity:
        config.operations_entity || "sensor.foxess_smart_operations",
      forecast_entity:
        config.forecast_entity || "sensor.foxess_battery_forecast",
      soc_entity: config.soc_entity || "sensor.foxess_battery_soc",
      ...config,
    };
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return {};
  }

  // -- Helpers ---------------------------------------------------------------

  _entity(id) {
    return this._hass && this._hass.states[id];
  }

  _attr(id) {
    const e = this._entity(id);
    return e ? e.attributes : {};
  }

  _state(id) {
    const e = this._entity(id);
    return e ? e.state : null;
  }

  _formatPower(watts) {
    if (watts == null) return "";
    const w = Number(watts);
    if (Number.isNaN(w)) return "";
    if (w >= 1000) {
      const kw = w / 1000;
      return kw === Math.floor(kw) ? `${kw} kW` : `${kw.toFixed(1)} kW`;
    }
    return `${w} W`;
  }

  // -- Rendering -------------------------------------------------------------

  _render() {
    if (!this._hass) return;

    const ops = this._config.operations_entity;
    const a = this._attr(ops);
    const soc = a.charge_current_soc ?? a.discharge_current_soc ?? this._getSoc();
    const chargeActive = a.charge_active === true;
    const dischargeActive = a.discharge_active === true;

    this.shadowRoot.innerHTML = `
      <style>${FoxESSControlCard._styles()}</style>
      <ha-card>
        ${this._renderHeader(soc)}
        <div class="content">
          ${chargeActive ? this._renderCharge(a) : ""}
          ${dischargeActive ? this._renderDischarge(a) : ""}
          ${!chargeActive && !dischargeActive ? this._renderIdle() : ""}
          ${this._renderForecast()}
        </div>
      </ha-card>
    `;
  }

  _getSoc() {
    const s = this._state(this._config.soc_entity);
    return s != null && s !== "unavailable" && s !== "unknown"
      ? parseFloat(s)
      : null;
  }

  _renderHeader(soc) {
    const socVal = soc != null ? Math.round(soc) : null;
    const socPct = socVal != null ? Math.max(0, Math.min(100, socVal)) : 0;

    // Battery bar colour
    let barColor = "var(--success-color, #4caf50)";
    if (socPct <= 15) barColor = "var(--error-color, #f44336)";
    else if (socPct <= 30) barColor = "var(--warning-color, #ff9800)";

    return `
      <div class="header">
        <div class="header-left">
          <div class="title">FoxESS Control</div>
        </div>
        <div class="header-right">
          <div class="soc-group">
            <svg class="battery-icon" viewBox="0 0 24 14" width="32" height="18">
              <rect x="0.5" y="0.5" width="20" height="13" rx="2" ry="2"
                    fill="none" stroke="var(--primary-text-color)" stroke-width="1"/>
              <rect x="20.5" y="4" width="3" height="6" rx="1" ry="1"
                    fill="var(--primary-text-color)"/>
              <rect x="2" y="2" width="${(socPct / 100) * 17}" height="10" rx="1" ry="1"
                    fill="${barColor}"/>
            </svg>
            <span class="soc-text">${socVal != null ? socVal + "%" : "—"}</span>
          </div>
        </div>
      </div>
    `;
  }

  _renderCharge(a) {
    const phase = a.charge_phase;
    const deferred = phase === "deferred";
    const power = a.charge_power_w || 0;
    const target = a.charge_target_soc;
    const current = a.charge_current_soc;
    const remaining = a.charge_remaining || "";
    const window = a.charge_window || "";

    let progressPct = 0;
    if (target && current != null) {
      // Progress from 0% (session start assumed at some lower SoC) toward target
      progressPct = Math.min(100, Math.max(0, (current / target) * 100));
    }

    return `
      <div class="section charge">
        <div class="section-header">
          <div class="section-icon-group">
            <span class="dot ${deferred ? "dot-waiting" : "dot-active"}"></span>
            <span class="section-title">${deferred ? "Charge Scheduled" : "Smart Charge"}</span>
          </div>
          <span class="section-badge charge-badge">${remaining}</span>
        </div>
        <div class="section-body">
          <div class="detail-row">
            <span class="detail-label">Window</span>
            <span class="detail-value">${window}</span>
          </div>
          ${!deferred ? `
          <div class="detail-row">
            <span class="detail-label">Power</span>
            <span class="detail-value">${this._formatPower(power)}</span>
          </div>` : ""}
          <div class="detail-row">
            <span class="detail-label">Target</span>
            <span class="detail-value">${current != null ? Math.round(current) : "?"}% → ${target != null ? target : "?"}%</span>
          </div>
          ${target && current != null && !deferred ? `
          <div class="progress-track">
            <div class="progress-fill charge-fill" style="width:${progressPct}%"></div>
          </div>` : ""}
        </div>
      </div>
    `;
  }

  _renderDischarge(a) {
    const power = a.discharge_power_w || 0;
    const minSoc = a.discharge_min_soc;
    const current = a.discharge_current_soc;
    const remaining = a.discharge_remaining || "";
    const window = a.discharge_window || "";
    const beforeStart = remaining.startsWith && remaining.startsWith("starts");
    const scheduled = beforeStart || remaining.startsWith("scheduled");

    // Feed-in energy progress (from the overview state text)
    const opsState = this._state(this._config.operations_entity) || "";
    const feedinMatch = opsState.match(/Discharging ([\d.]+) kWh/);
    let feedinLimit = null;
    if (feedinMatch) feedinLimit = parseFloat(feedinMatch[1]);

    return `
      <div class="section discharge">
        <div class="section-header">
          <div class="section-icon-group">
            <span class="dot ${scheduled ? "dot-waiting" : "dot-active dot-discharge"}"></span>
            <span class="section-title">${scheduled ? "Discharge Scheduled" : "Smart Discharge"}</span>
          </div>
          <span class="section-badge discharge-badge">${remaining}</span>
        </div>
        <div class="section-body">
          <div class="detail-row">
            <span class="detail-label">Window</span>
            <span class="detail-value">${window}</span>
          </div>
          ${!scheduled ? `
          <div class="detail-row">
            <span class="detail-label">Power</span>
            <span class="detail-value">${this._formatPower(power)}</span>
          </div>` : ""}
          <div class="detail-row">
            <span class="detail-label">Min SoC</span>
            <span class="detail-value">${minSoc != null ? minSoc + "%" : "—"}</span>
          </div>
          ${feedinLimit != null ? `
          <div class="detail-row">
            <span class="detail-label">Feed-in limit</span>
            <span class="detail-value">${feedinLimit} kWh</span>
          </div>` : ""}
        </div>
      </div>
    `;
  }

  _renderIdle() {
    return `
      <div class="idle">
        <svg class="idle-icon" viewBox="0 0 24 24" width="40" height="40">
          <path fill="var(--secondary-text-color)"
                d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48
                   10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10
                   14.17l7.59-7.59L19 8l-9 9z"/>
        </svg>
        <div class="idle-text">No active operations</div>
        <div class="idle-sub">Call <b>smart_charge</b> or <b>smart_discharge</b> to begin</div>
      </div>
    `;
  }

  _renderForecast() {
    const forecastEntity = this._config.forecast_entity;
    const attrs = this._attr(forecastEntity);
    const points = attrs.forecast;
    if (!points || !Array.isArray(points) || points.length < 2) return "";

    const fmt = (ms) => {
      const d = new Date(ms);
      const h = d.getHours();
      const m = d.getMinutes();
      return `${h}:${m < 10 ? "0" + m : m}`;
    };

    // Build SVG sparkline
    const width = 280;
    const height = 72;
    const padTop = 4;
    const padBottom = 14; // room for time labels
    const padX = 4;
    const chartHeight = height - padTop - padBottom;
    // Scale Y-axis to the data extent with some padding
    const socValues = points.map((p) => p.soc);
    const rawMin = Math.min(...socValues);
    const rawMax = Math.max(...socValues);
    const socMargin = Math.max(5, (rawMax - rawMin) * 0.1);
    const minSoc = Math.max(0, Math.floor((rawMin - socMargin) / 5) * 5);
    const maxSoc = Math.min(100, Math.ceil((rawMax + socMargin) / 5) * 5);

    const now = Date.now();
    const times = points.map((p) => p.time);
    const tMin = Math.min(...times);
    const tMax = Math.max(...times);
    const tRange = tMax - tMin || 1;

    const toX = (t) => padX + ((t - tMin) / tRange) * (width - 2 * padX);
    const toY = (s) =>
      padTop + (1 - (s - minSoc) / (maxSoc - minSoc)) * chartHeight;

    const pathParts = points.map(
      (p, i) => `${i === 0 ? "M" : "L"}${toX(p.time).toFixed(1)},${toY(p.soc).toFixed(1)}`
    );
    const linePath = pathParts.join(" ");

    // Area fill
    const chartBottom = padTop + chartHeight;
    const areaPath =
      linePath +
      ` L${toX(times[times.length - 1]).toFixed(1)},${chartBottom.toFixed(1)}` +
      ` L${toX(times[0]).toFixed(1)},${chartBottom.toFixed(1)} Z`;

    // Now marker
    const nowX = toX(now);
    const showNow = now >= tMin && now <= tMax;

    // Time axis labels — start, end, and optionally "now"
    const labelY = height - 2;
    let timeLabels = `
      <text x="${padX}" y="${labelY}" font-size="7" text-anchor="start"
            fill="var(--secondary-text-color)" opacity="0.6">${fmt(tMin)}</text>
      <text x="${width - padX}" y="${labelY}" font-size="7" text-anchor="end"
            fill="var(--secondary-text-color)" opacity="0.6">${fmt(tMax)}</text>
    `;
    if (showNow) {
      // Only show "now" label if it won't overlap start/end
      const nowPct = (now - tMin) / tRange;
      if (nowPct > 0.15 && nowPct < 0.85) {
        timeLabels += `
          <text x="${nowX.toFixed(1)}" y="${labelY}" font-size="7" text-anchor="middle"
                fill="var(--primary-text-color)" opacity="0.5">now</text>
        `;
      }
    }

    return `
      <div class="forecast">
        <div class="forecast-label">Forecast</div>
        <svg class="forecast-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
          <defs>
            <linearGradient id="fg" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="var(--primary-color)" stop-opacity="0.3"/>
              <stop offset="100%" stop-color="var(--primary-color)" stop-opacity="0.02"/>
            </linearGradient>
          </defs>
          <path d="${areaPath}" fill="url(#fg)"/>
          <path d="${linePath}" fill="none" stroke="var(--primary-color)"
                stroke-width="1.5" stroke-linejoin="round"/>
          ${showNow ? `<line x1="${nowX.toFixed(1)}" y1="${padTop}" x2="${nowX.toFixed(1)}" y2="${chartBottom}" stroke="var(--primary-text-color)" stroke-width="0.5" stroke-dasharray="2,2" opacity="0.4"/>` : ""}
          <!-- Y-axis labels -->
          <text x="${padX}" y="${padTop + 6}" font-size="7"
                fill="var(--secondary-text-color)" opacity="0.6">${maxSoc}%</text>
          <text x="${padX}" y="${chartBottom}" font-size="7"
                fill="var(--secondary-text-color)" opacity="0.6">${minSoc}%</text>
          <!-- Time axis -->
          ${timeLabels}
        </svg>
      </div>
    `;
  }

  // -- Styles ----------------------------------------------------------------

  static _styles() {
    return `
      :host {
        --fc-charge: #4caf50;
        --fc-charge-bg: rgba(76, 175, 80, 0.08);
        --fc-discharge: #ff9800;
        --fc-discharge-bg: rgba(255, 152, 0, 0.08);
        --fc-radius: 12px;
        --fc-section-radius: 10px;
      }

      ha-card {
        overflow: hidden;
      }

      /* Header */
      .header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 16px 20px 12px;
      }
      .title {
        font-size: 16px;
        font-weight: 600;
        color: var(--primary-text-color);
        letter-spacing: -0.01em;
      }
      .header-right {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .soc-group {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .soc-text {
        font-size: 18px;
        font-weight: 700;
        color: var(--primary-text-color);
      }

      /* Content */
      .content {
        padding: 0 16px 16px;
      }

      /* Sections (charge / discharge) */
      .section {
        border-radius: var(--fc-section-radius);
        padding: 14px 16px;
        margin-bottom: 10px;
      }
      .section:last-child {
        margin-bottom: 0;
      }
      .charge {
        background: var(--fc-charge-bg);
        border: 1px solid rgba(76, 175, 80, 0.18);
      }
      .discharge {
        background: var(--fc-discharge-bg);
        border: 1px solid rgba(255, 152, 0, 0.18);
      }

      .section-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 10px;
      }
      .section-icon-group {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .section-title {
        font-size: 14px;
        font-weight: 600;
        color: var(--primary-text-color);
      }

      /* Pulsing status dot */
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
      }
      .dot-active {
        background: var(--fc-charge);
        box-shadow: 0 0 0 0 rgba(76, 175, 80, 0.5);
        animation: pulse 2s ease-in-out infinite;
      }
      .dot-active.dot-discharge {
        background: var(--fc-discharge);
        box-shadow: 0 0 0 0 rgba(255, 152, 0, 0.5);
        animation: pulse-discharge 2s ease-in-out infinite;
      }
      .dot-waiting {
        background: var(--secondary-text-color);
        opacity: 0.5;
      }

      @keyframes pulse {
        0%, 100% { box-shadow: 0 0 0 0 rgba(76, 175, 80, 0.4); }
        50% { box-shadow: 0 0 0 6px rgba(76, 175, 80, 0); }
      }
      @keyframes pulse-discharge {
        0%, 100% { box-shadow: 0 0 0 0 rgba(255, 152, 0, 0.4); }
        50% { box-shadow: 0 0 0 6px rgba(255, 152, 0, 0); }
      }

      /* Badge (remaining time) */
      .section-badge {
        font-size: 12px;
        font-weight: 600;
        padding: 3px 10px;
        border-radius: 20px;
        white-space: nowrap;
      }
      .charge-badge {
        background: rgba(76, 175, 80, 0.15);
        color: var(--fc-charge);
      }
      .discharge-badge {
        background: rgba(255, 152, 0, 0.15);
        color: var(--fc-discharge);
      }

      /* Detail rows */
      .section-body {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .detail-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 13px;
      }
      .detail-label {
        color: var(--secondary-text-color);
      }
      .detail-value {
        color: var(--primary-text-color);
        font-weight: 500;
      }

      /* Progress bar */
      .progress-track {
        height: 6px;
        background: rgba(0, 0, 0, 0.08);
        border-radius: 3px;
        overflow: hidden;
        margin-top: 6px;
      }
      .progress-fill {
        height: 100%;
        border-radius: 3px;
        transition: width 0.6s ease;
      }
      .charge-fill {
        background: linear-gradient(90deg, var(--fc-charge), #81c784);
      }

      /* Idle state */
      .idle {
        text-align: center;
        padding: 24px 16px;
      }
      .idle-icon {
        opacity: 0.3;
        margin-bottom: 8px;
      }
      .idle-text {
        font-size: 15px;
        font-weight: 500;
        color: var(--primary-text-color);
        opacity: 0.7;
      }
      .idle-sub {
        font-size: 12px;
        color: var(--secondary-text-color);
        margin-top: 4px;
      }

      /* Forecast sparkline */
      .forecast {
        margin-top: 12px;
        padding-top: 10px;
        border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.08));
      }
      .forecast-label {
        font-size: 11px;
        font-weight: 600;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 6px;
      }
      .forecast-svg {
        width: 100%;
        height: 72px;
        display: block;
      }
    `;
  }
}

// Register the card
customElements.define("foxess-control-card", FoxESSControlCard);

// Register with HA's card picker
window.customCards = window.customCards || [];
window.customCards.push({
  type: "foxess-control-card",
  name: "FoxESS Control",
  description: "Smart charge & discharge status with battery forecast",
  preview: true,
});

console.info(`%c FoxESS Control Card v${CARD_VERSION} `, "color:#fff;background:#4caf50;font-weight:bold;border-radius:4px;padding:2px 6px");
