/**
 * FoxESS Control — custom Lovelace card.
 *
 * Renders smart charge / discharge status with a battery gauge
 * and progress bars showing time elapsed vs goal completion.
 *
 * Usage:
 *   type: custom:foxess-control-card
 *   # Optional overrides (auto-discovered by default):
 *   # operations_entity: sensor.foxess_smart_operations
 *   # soc_entity: sensor.foxess_battery_soc
 */

const CARD_VERSION = "1.1.0";

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

  _formatDuration(ms) {
    if (ms <= 0) return "0m";
    const totalMin = Math.round(ms / 60000);
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    if (h === 0) return `${m}m`;
    if (m === 0) return `${h}h`;
    return `${h}h ${m}m`;
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
          ${this._renderProgress(a)}
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
    const opsState = this._state(this._config.operations_entity) || "";
    const suspended = opsState.includes("suspended");

    // Feed-in energy progress
    const feedinLimit = a.discharge_feedin_limit_kwh;
    const feedinUsed = a.discharge_feedin_used_kwh;
    const feedinProjected = a.discharge_feedin_projected_kwh;

    return `
      <div class="section discharge">
        <div class="section-header">
          <div class="section-icon-group">
            <span class="dot ${scheduled || suspended ? "dot-waiting" : "dot-active dot-discharge"}"></span>
            <span class="section-title">${scheduled ? "Discharge Scheduled" : suspended ? "Discharge Suspended" : "Smart Discharge"}</span>
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
            <span class="detail-label">Feed-in</span>
            <span class="detail-value">${feedinUsed != null ? feedinUsed : "—"} / ${feedinLimit} kWh${feedinProjected != null ? ` (→${feedinProjected})` : ""}</span>
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

  _progressBar(label, value, pct, fillClass) {
    return `
      <div class="progress-row">
        <div class="detail-row">
          <span class="detail-label">${label}</span>
          <span class="detail-value">${value}</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill ${fillClass}" style="width:${pct}%"></div>
        </div>
      </div>
    `;
  }

  _timeProgress(startIso, endIso, now) {
    const startTime = startIso ? new Date(startIso).getTime() : null;
    const endTime = endIso ? new Date(endIso).getTime() : null;
    if (!startTime || !endTime || endTime <= startTime) return { pct: 0, label: "" };
    const elapsed = now - startTime;
    const total = endTime - startTime;
    return {
      pct: Math.min(100, Math.max(0, (elapsed / total) * 100)),
      label: `${this._formatDuration(elapsed)} / ${this._formatDuration(total)}`,
    };
  }

  _renderProgress(a) {
    const chargeActive = a.charge_active === true;
    const dischargeActive = a.discharge_active === true;
    if (!chargeActive && !dischargeActive) return "";

    const now = Date.now();
    let bars = "";

    if (chargeActive) {
      const startSoc = a.charge_start_soc;
      const current = a.charge_current_soc;
      const target = a.charge_target_soc;

      let socPct = 0;
      if (startSoc != null && target != null && current != null && target > startSoc) {
        socPct = Math.min(100, Math.max(0, ((current - startSoc) / (target - startSoc)) * 100));
      }
      const socLabel = `${current != null ? Math.round(current) : "?"}% → ${target != null ? target : "?"}%`;
      const time = this._timeProgress(a.charge_start_time, a.charge_end_time, now);

      bars += this._progressBar("SoC", socLabel, socPct, "charge-fill");
      bars += this._progressBar("Time", time.label, time.pct, "time-fill");
    }

    if (dischargeActive) {
      const startSoc = a.discharge_start_soc;
      const current = a.discharge_current_soc;
      const minSoc = a.discharge_min_soc;

      let socPct = 0;
      if (startSoc != null && minSoc != null && current != null && startSoc > minSoc) {
        socPct = Math.min(100, Math.max(0, ((startSoc - current) / (startSoc - minSoc)) * 100));
      }
      const socLabel = `${current != null ? Math.round(current) : "?"}% → ${minSoc != null ? minSoc : "?"}%`;

      bars += this._progressBar("SoC", socLabel, socPct, "discharge-fill");

      const feedinLimit = a.discharge_feedin_limit_kwh;
      if (feedinLimit != null && feedinLimit > 0) {
        const used = a.discharge_feedin_used_kwh ?? 0;
        const energyPct = Math.min(100, Math.max(0, (used / feedinLimit) * 100));
        bars += this._progressBar("Energy", `${used} / ${feedinLimit} kWh`, energyPct, "energy-fill");
      }

      const time = this._timeProgress(a.discharge_start_time, a.discharge_end_time, now);
      bars += this._progressBar("Time", time.label, time.pct, "time-fill");
    }

    return `
      <div class="progress-section">
        <div class="progress-label">Progress</div>
        ${bars}
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
        --fc-energy: #2196f3;
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

      /* Progress bars */
      .progress-section {
        margin-top: 12px;
        padding-top: 10px;
        border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.08));
      }
      .progress-label {
        font-size: 11px;
        font-weight: 600;
        color: var(--secondary-text-color);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 8px;
      }
      .progress-row {
        margin-bottom: 8px;
      }
      .progress-row:last-child {
        margin-bottom: 0;
      }
      .progress-track {
        height: 6px;
        background: rgba(0, 0, 0, 0.08);
        border-radius: 3px;
        overflow: hidden;
        margin-top: 4px;
      }
      .progress-fill {
        height: 100%;
        border-radius: 3px;
        transition: width 0.6s ease;
      }
      .charge-fill {
        background: linear-gradient(90deg, var(--fc-charge), #81c784);
      }
      .discharge-fill {
        background: linear-gradient(90deg, var(--fc-discharge), #ffb74d);
      }
      .energy-fill {
        background: linear-gradient(90deg, var(--fc-energy), #64b5f6);
      }
      .time-fill {
        background: linear-gradient(90deg, rgba(0,0,0,0.25), rgba(0,0,0,0.15));
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
  description: "Smart charge & discharge status with progress tracking",
  preview: true,
});

console.info(`%c FoxESS Control Card v${CARD_VERSION} `, "color:#fff;background:#4caf50;font-weight:bold;border-radius:4px;padding:2px 6px");
