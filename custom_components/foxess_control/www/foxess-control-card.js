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

const CARD_VERSION = "1.2.0";

// -- i18n --------------------------------------------------------------------

const TRANSLATIONS = {
  en: {
    title: "FoxESS Control",
    smart_charge: "Smart Charge",
    charge_scheduled: "Charge Scheduled",
    smart_discharge: "Smart Discharge",
    discharge_scheduled: "Discharge Scheduled",
    discharge_suspended: "Discharge Suspended",
    window: "Window",
    power: "Power",
    target: "Target",
    min_soc: "Min SoC",
    feedin: "Feed-in",
    no_active: "No active operations",
    idle_hint: "Call <b>smart_charge</b> or <b>smart_discharge</b> to begin",
    progress: "Progress",
    soc: "SoC",
    time: "Time",
    energy: "Energy",
    starts_in: "starts in {0}",
    ending: "ending",
    kwh_left: "{0} kWh left",
    dur_hm: "{0}h {1}m",
    dur_h: "{0}h",
    dur_m: "{0}m",
  },
  de: {
    title: "FoxESS Steuerung",
    smart_charge: "Intelligentes Laden",
    charge_scheduled: "Laden geplant",
    smart_discharge: "Intelligente Entladung",
    discharge_scheduled: "Entladung geplant",
    discharge_suspended: "Entladung pausiert",
    window: "Zeitfenster",
    power: "Leistung",
    target: "Ziel",
    min_soc: "Min. SoC",
    feedin: "Einspeisung",
    no_active: "Keine aktiven Vorgänge",
    idle_hint: "Starte <b>smart_charge</b> oder <b>smart_discharge</b>",
    progress: "Fortschritt",
    soc: "SoC",
    time: "Zeit",
    energy: "Energie",
    starts_in: "startet in {0}",
    ending: "endet",
    kwh_left: "{0} kWh verbl.",
    dur_hm: "{0} Std. {1} Min.",
    dur_h: "{0} Std.",
    dur_m: "{0} Min.",
  },
  fr: {
    title: "FoxESS Contrôle",
    smart_charge: "Charge intelligente",
    charge_scheduled: "Charge programmée",
    smart_discharge: "Décharge intelligente",
    discharge_scheduled: "Décharge programmée",
    discharge_suspended: "Décharge suspendue",
    window: "Fenêtre",
    power: "Puissance",
    target: "Objectif",
    min_soc: "SoC min",
    feedin: "Injection",
    no_active: "Aucune opération active",
    idle_hint: "Appelez <b>smart_charge</b> ou <b>smart_discharge</b> pour commencer",
    progress: "Progression",
    soc: "SoC",
    time: "Temps",
    energy: "Énergie",
    starts_in: "commence dans {0}",
    ending: "fin",
    kwh_left: "{0} kWh restants",
    dur_hm: "{0}h {1}min",
    dur_h: "{0}h",
    dur_m: "{0}min",
  },
  nl: {
    title: "FoxESS Besturing",
    smart_charge: "Slim laden",
    charge_scheduled: "Laden gepland",
    smart_discharge: "Slim ontladen",
    discharge_scheduled: "Ontladen gepland",
    discharge_suspended: "Ontladen gepauzeerd",
    window: "Tijdvenster",
    power: "Vermogen",
    target: "Doel",
    min_soc: "Min SoC",
    feedin: "Teruglevering",
    no_active: "Geen actieve bewerkingen",
    idle_hint: "Start <b>smart_charge</b> of <b>smart_discharge</b>",
    progress: "Voortgang",
    soc: "SoC",
    time: "Tijd",
    energy: "Energie",
    starts_in: "start over {0}",
    ending: "eindigt",
    kwh_left: "{0} kWh over",
    dur_hm: "{0}u {1}m",
    dur_h: "{0}u",
    dur_m: "{0}m",
  },
  es: {
    title: "FoxESS Control",
    smart_charge: "Carga inteligente",
    charge_scheduled: "Carga programada",
    smart_discharge: "Descarga inteligente",
    discharge_scheduled: "Descarga programada",
    discharge_suspended: "Descarga suspendida",
    window: "Ventana",
    power: "Potencia",
    target: "Objetivo",
    min_soc: "SoC mín",
    feedin: "Inyección",
    no_active: "Sin operaciones activas",
    idle_hint: "Llame a <b>smart_charge</b> o <b>smart_discharge</b> para iniciar",
    progress: "Progreso",
    soc: "SoC",
    time: "Tiempo",
    energy: "Energía",
    starts_in: "comienza en {0}",
    ending: "finalizando",
    kwh_left: "{0} kWh restantes",
    dur_hm: "{0}h {1}min",
    dur_h: "{0}h",
    dur_m: "{0}min",
  },
  it: {
    title: "FoxESS Controllo",
    smart_charge: "Ricarica intelligente",
    charge_scheduled: "Ricarica programmata",
    smart_discharge: "Scarica intelligente",
    discharge_scheduled: "Scarica programmata",
    discharge_suspended: "Scarica sospesa",
    window: "Finestra",
    power: "Potenza",
    target: "Obiettivo",
    min_soc: "SoC min",
    feedin: "Immissione",
    no_active: "Nessuna operazione attiva",
    idle_hint: "Avvia <b>smart_charge</b> o <b>smart_discharge</b> per iniziare",
    progress: "Progresso",
    soc: "SoC",
    time: "Tempo",
    energy: "Energia",
    starts_in: "inizia tra {0}",
    ending: "in chiusura",
    kwh_left: "{0} kWh rimasti",
    dur_hm: "{0}h {1}min",
    dur_h: "{0}h",
    dur_m: "{0}min",
  },
  pl: {
    title: "FoxESS Sterowanie",
    smart_charge: "Inteligentne ładowanie",
    charge_scheduled: "Ładowanie zaplanowane",
    smart_discharge: "Inteligentne rozładowanie",
    discharge_scheduled: "Rozładowanie zaplanowane",
    discharge_suspended: "Rozładowanie wstrzymane",
    window: "Okno czasowe",
    power: "Moc",
    target: "Cel",
    min_soc: "Min. SoC",
    feedin: "Oddawanie",
    no_active: "Brak aktywnych operacji",
    idle_hint: "Wywołaj <b>smart_charge</b> lub <b>smart_discharge</b>, aby rozpocząć",
    progress: "Postęp",
    soc: "SoC",
    time: "Czas",
    energy: "Energia",
    starts_in: "start za {0}",
    ending: "kończy się",
    kwh_left: "{0} kWh pozostało",
    dur_hm: "{0} godz. {1} min",
    dur_h: "{0} godz.",
    dur_m: "{0} min",
  },
  pt: {
    title: "FoxESS Controlo",
    smart_charge: "Carga inteligente",
    charge_scheduled: "Carga agendada",
    smart_discharge: "Descarga inteligente",
    discharge_scheduled: "Descarga agendada",
    discharge_suspended: "Descarga suspensa",
    window: "Janela",
    power: "Potência",
    target: "Objetivo",
    min_soc: "SoC mín",
    feedin: "Injeção",
    no_active: "Sem operações ativas",
    idle_hint: "Chame <b>smart_charge</b> ou <b>smart_discharge</b> para iniciar",
    progress: "Progresso",
    soc: "SoC",
    time: "Tempo",
    energy: "Energia",
    starts_in: "começa em {0}",
    ending: "terminando",
    kwh_left: "{0} kWh restantes",
    dur_hm: "{0}h {1}min",
    dur_h: "{0}h",
    dur_m: "{0}min",
  },
};

function _getStrings(lang) {
  if (!lang) return TRANSLATIONS.en;
  const lc = lang.toLowerCase();
  // Try exact match (e.g. "de"), then base language (e.g. "de" from "de-AT")
  return TRANSLATIONS[lc] || TRANSLATIONS[lc.split("-")[0]] || TRANSLATIONS.en;
}

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

  _t(key) {
    const lang = this._hass && (this._hass.language || (this._hass.locale && this._hass.locale.language));
    const strings = _getStrings(lang);
    return strings[key] || TRANSLATIONS.en[key] || key;
  }

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
    if (ms <= 0) return this._t("dur_m").replace("{0}", "0");
    const totalMin = Math.round(ms / 60000);
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    if (h === 0) return this._t("dur_m").replace("{0}", m);
    if (m === 0) return this._t("dur_h").replace("{0}", h);
    return this._t("dur_hm").replace("{0}", h).replace("{1}", m);
  }

  _translateRemaining(text) {
    if (!text) return "";
    // "starts in Xh Ym" / "starts in Xm"
    const startsMatch = text.match(/^starts in (.+)$/);
    if (startsMatch) {
      const dur = this._translateDurationStr(startsMatch[1]);
      return this._t("starts_in").replace("{0}", dur);
    }
    // "ending"
    if (text === "ending") return this._t("ending");
    // "X.X kWh left"
    const kwhMatch = text.match(/^([\d.]+) kWh left$/);
    if (kwhMatch) return this._t("kwh_left").replace("{0}", kwhMatch[1]);
    // bare duration "Xh Ym" / "Xm" / "Xh"
    return this._translateDurationStr(text);
  }

  _translateDurationStr(text) {
    // "Xh Ym"
    const hm = text.match(/^(\d+)h (\d+)m$/);
    if (hm) return this._t("dur_hm").replace("{0}", hm[1]).replace("{1}", hm[2]);
    // "Xh"
    const ho = text.match(/^(\d+)h$/);
    if (ho) return this._t("dur_h").replace("{0}", ho[1]);
    // "Xm"
    const mo = text.match(/^(\d+)m$/);
    if (mo) return this._t("dur_m").replace("{0}", mo[1]);
    return text;
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
          <div class="title">${this._t("title")}</div>
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
            <span class="section-title">${deferred ? this._t("charge_scheduled") : this._t("smart_charge")}</span>
          </div>
          <span class="section-badge charge-badge">${this._translateRemaining(remaining)}</span>
        </div>
        <div class="section-body">
          <div class="detail-row">
            <span class="detail-label">${this._t("window")}</span>
            <span class="detail-value">${window}</span>
          </div>
          ${!deferred ? `
          <div class="detail-row">
            <span class="detail-label">${this._t("power")}</span>
            <span class="detail-value">${this._formatPower(power)}</span>
          </div>` : ""}
          <div class="detail-row">
            <span class="detail-label">${this._t("target")}</span>
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
            <span class="section-title">${scheduled ? this._t("discharge_scheduled") : suspended ? this._t("discharge_suspended") : this._t("smart_discharge")}</span>
          </div>
          <span class="section-badge discharge-badge">${this._translateRemaining(remaining)}</span>
        </div>
        <div class="section-body">
          <div class="detail-row">
            <span class="detail-label">${this._t("window")}</span>
            <span class="detail-value">${window}</span>
          </div>
          ${!scheduled ? `
          <div class="detail-row">
            <span class="detail-label">${this._t("power")}</span>
            <span class="detail-value">${this._formatPower(power)}</span>
          </div>` : ""}
          <div class="detail-row">
            <span class="detail-label">${this._t("min_soc")}</span>
            <span class="detail-value">${minSoc != null ? minSoc + "%" : "—"}</span>
          </div>
          ${feedinLimit != null ? `
          <div class="detail-row">
            <span class="detail-label">${this._t("feedin")}</span>
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
        <div class="idle-text">${this._t("no_active")}</div>
        <div class="idle-sub">${this._t("idle_hint")}</div>
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

  _energyScheduleBar(label, value, actualPct, expectedPct) {
    // Show energy progress with a coloured gap segment indicating
    // whether discharge is ahead of or behind the ideal schedule.
    const lo = Math.min(actualPct, expectedPct);
    const hi = Math.max(actualPct, expectedPct);
    const gapWidth = hi - lo;
    const ahead = actualPct >= expectedPct;
    const gapClass = ahead ? "energy-ahead" : "energy-behind";

    return `
      <div class="progress-row">
        <div class="detail-row">
          <span class="detail-label">${label}</span>
          <span class="detail-value">${value}</span>
        </div>
        <div class="progress-track">
          <div class="energy-fill" style="width:${lo}%"></div>${gapWidth > 0.5 ? `<div class="${gapClass}" style="width:${gapWidth}%"></div>` : ""}
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

      bars += this._progressBar(this._t("soc"), socLabel, socPct, "charge-fill");
      bars += this._progressBar(this._t("time"), time.label, time.pct, "time-fill");
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

      bars += this._progressBar(this._t("soc"), socLabel, socPct, "discharge-fill");

      const time = this._timeProgress(a.discharge_start_time, a.discharge_end_time, now);

      const feedinLimit = a.discharge_feedin_limit_kwh;
      if (feedinLimit != null && feedinLimit > 0) {
        const used = a.discharge_feedin_used_kwh ?? 0;
        const energyPct = Math.min(100, Math.max(0, (used / feedinLimit) * 100));
        bars += this._energyScheduleBar(this._t("energy"), `${used} / ${feedinLimit} kWh`, energyPct, time.pct);
      }

      bars += this._progressBar(this._t("time"), time.label, time.pct, "time-fill");
    }

    return `
      <div class="progress-section">
        <div class="progress-label">${this._t("progress")}</div>
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
        display: flex;
        height: 6px;
        background: var(--secondary-background-color, rgba(0, 0, 0, 0.08));
        border-radius: 3px;
        overflow: hidden;
        margin-top: 4px;
      }
      .progress-fill {
        height: 100%;
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
      .energy-ahead {
        height: 100%;
        background: rgba(76, 175, 80, 0.55);
        transition: width 0.6s ease;
      }
      .energy-behind {
        height: 100%;
        background: rgba(244, 67, 54, 0.55);
        transition: width 0.6s ease;
      }
      .time-fill {
        background: linear-gradient(90deg, var(--primary-text-color, #666), var(--secondary-text-color, #999));
        opacity: 0.3;
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
