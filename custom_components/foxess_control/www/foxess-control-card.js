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

const CARD_VERSION = "1.4.0";

// -- i18n --------------------------------------------------------------------

const TRANSLATIONS = {
  en: {
    title: "FoxESS Control",
    smart_charge: "Smart Charge",
    charge_scheduled: "Charge Scheduled",
    smart_discharge: "Smart Discharge",
    discharge_scheduled: "Discharge Scheduled",
    discharge_suspended: "Discharge Suspended",
    discharge_deferred: "Discharge Deferred",
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
    tip_soc_charge: "{0}% of {1}% target — {2}% remaining",
    tip_soc_discharge: "{0}% of {1}% minimum — {2}% above min",
    tip_time: "{0} elapsed of {1} — {2} remaining",
    tip_energy: "{0} of {1} kWh — {2} kWh remaining",
    tip_energy_ahead: "{0} kWh ahead of schedule",
    tip_energy_behind: "{0} kWh behind schedule",
    tip_power_active: "{0} of {1} max",
    tip_power_deferred: "Self-use — no forced export",
    tip_power_suspended: "Suspended — protecting min SoC",
    tip_power_charge_deferred: "Deferred — waiting for optimal time",
    self_use: "Self-use",
  },
  de: {
    title: "FoxESS Steuerung",
    smart_charge: "Intelligentes Laden",
    charge_scheduled: "Laden geplant",
    smart_discharge: "Intelligente Entladung",
    discharge_scheduled: "Entladung geplant",
    discharge_suspended: "Entladung pausiert",
    discharge_deferred: "Entladung verzögert",
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
    tip_soc_charge: "{0}% von {1}% Ziel — {2}% verbleibend",
    tip_soc_discharge: "{0}% von {1}% Minimum — {2}% über Min.",
    tip_time: "{0} vergangen von {1} — {2} verbleibend",
    tip_energy: "{0} von {1} kWh — {2} kWh verbleibend",
    tip_energy_ahead: "{0} kWh vor dem Zeitplan",
    tip_energy_behind: "{0} kWh hinter dem Zeitplan",
    tip_power_active: "{0} von {1} max.",
    tip_power_deferred: "Eigenverbrauch — kein Zwangsexport",
    tip_power_suspended: "Pausiert — min. SoC schützen",
    tip_power_charge_deferred: "Verzögert — optimale Zeit abwarten",
    self_use: "Eigenverbr.",
  },
  fr: {
    title: "FoxESS Contrôle",
    smart_charge: "Charge intelligente",
    charge_scheduled: "Charge programmée",
    smart_discharge: "Décharge intelligente",
    discharge_scheduled: "Décharge programmée",
    discharge_suspended: "Décharge suspendue",
    discharge_deferred: "Décharge différée",
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
    tip_soc_charge: "{0}% sur {1}% cible — {2}% restants",
    tip_soc_discharge: "{0}% sur {1}% minimum — {2}% au-dessus du min.",
    tip_time: "{0} écoulé sur {1} — {2} restant",
    tip_energy: "{0} sur {1} kWh — {2} kWh restants",
    tip_energy_ahead: "{0} kWh en avance",
    tip_energy_behind: "{0} kWh en retard",
    tip_power_active: "{0} sur {1} max",
    tip_power_deferred: "Auto-consommation — pas d'export forcé",
    tip_power_suspended: "Suspendu — protection SoC min.",
    tip_power_charge_deferred: "Différée — en attente du moment optimal",
    self_use: "Auto-conso.",
  },
  nl: {
    title: "FoxESS Besturing",
    smart_charge: "Slim laden",
    charge_scheduled: "Laden gepland",
    smart_discharge: "Slim ontladen",
    discharge_scheduled: "Ontladen gepland",
    discharge_suspended: "Ontladen gepauzeerd",
    discharge_deferred: "Ontlading uitgesteld",
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
    tip_soc_charge: "{0}% van {1}% doel — {2}% resterend",
    tip_soc_discharge: "{0}% van {1}% minimum — {2}% boven min.",
    tip_time: "{0} verstreken van {1} — {2} resterend",
    tip_energy: "{0} van {1} kWh — {2} kWh resterend",
    tip_energy_ahead: "{0} kWh voor op schema",
    tip_energy_behind: "{0} kWh achter op schema",
    tip_power_active: "{0} van {1} max.",
    tip_power_deferred: "Eigenverbruik — geen gedwongen export",
    tip_power_suspended: "Onderbroken — min. SoC beschermen",
    tip_power_charge_deferred: "Uitgesteld — wachten op optimaal moment",
    self_use: "Eigenverbr.",
  },
  es: {
    title: "FoxESS Control",
    smart_charge: "Carga inteligente",
    charge_scheduled: "Carga programada",
    smart_discharge: "Descarga inteligente",
    discharge_scheduled: "Descarga programada",
    discharge_suspended: "Descarga suspendida",
    discharge_deferred: "Descarga diferida",
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
    tip_soc_charge: "{0}% de {1}% objetivo — {2}% restante",
    tip_soc_discharge: "{0}% de {1}% mínimo — {2}% sobre mín.",
    tip_time: "{0} transcurrido de {1} — {2} restante",
    tip_energy: "{0} de {1} kWh — {2} kWh restantes",
    tip_energy_ahead: "{0} kWh adelantado",
    tip_energy_behind: "{0} kWh atrasado",
    tip_power_active: "{0} de {1} máx.",
    tip_power_deferred: "Autoconsumo — sin exportación forzada",
    tip_power_suspended: "Suspendido — protegiendo SoC mín.",
    tip_power_charge_deferred: "Diferida — esperando momento óptimo",
    self_use: "Autocons.",
  },
  it: {
    title: "FoxESS Controllo",
    smart_charge: "Ricarica intelligente",
    charge_scheduled: "Ricarica programmata",
    smart_discharge: "Scarica intelligente",
    discharge_scheduled: "Scarica programmata",
    discharge_suspended: "Scarica sospesa",
    discharge_deferred: "Scarica differita",
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
    tip_soc_charge: "{0}% di {1}% obiettivo — {2}% rimanente",
    tip_soc_discharge: "{0}% di {1}% minimo — {2}% sopra min.",
    tip_time: "{0} trascorso di {1} — {2} rimanente",
    tip_energy: "{0} di {1} kWh — {2} kWh rimanenti",
    tip_energy_ahead: "{0} kWh in anticipo",
    tip_energy_behind: "{0} kWh in ritardo",
    tip_power_active: "{0} di {1} max",
    tip_power_deferred: "Autoconsumo — nessun export forzato",
    tip_power_suspended: "Sospeso — protezione SoC min.",
    tip_power_charge_deferred: "Differita — in attesa del momento ottimale",
    self_use: "Autocons.",
  },
  pl: {
    title: "FoxESS Sterowanie",
    smart_charge: "Inteligentne ładowanie",
    charge_scheduled: "Ładowanie zaplanowane",
    smart_discharge: "Inteligentne rozładowanie",
    discharge_scheduled: "Rozładowanie zaplanowane",
    discharge_suspended: "Rozładowanie wstrzymane",
    discharge_deferred: "Rozładowanie odroczone",
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
    tip_soc_charge: "{0}% z {1}% celu — {2}% pozostało",
    tip_soc_discharge: "{0}% z {1}% minimum — {2}% powyżej min.",
    tip_time: "{0} minęło z {1} — {2} pozostało",
    tip_energy: "{0} z {1} kWh — {2} kWh pozostało",
    tip_energy_ahead: "{0} kWh przed harmonogramem",
    tip_energy_behind: "{0} kWh za harmonogramem",
    tip_power_active: "{0} z {1} maks.",
    tip_power_deferred: "Autokonsumpcja — brak wymuszonego eksportu",
    tip_power_suspended: "Wstrzymany — ochrona min. SoC",
    tip_power_charge_deferred: "Odroczone — oczekiwanie na optymalny czas",
    self_use: "Autokons.",
  },
  pt: {
    title: "FoxESS Controlo",
    smart_charge: "Carga inteligente",
    charge_scheduled: "Carga agendada",
    smart_discharge: "Descarga inteligente",
    discharge_scheduled: "Descarga agendada",
    discharge_suspended: "Descarga suspensa",
    discharge_deferred: "Descarga adiada",
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
    tip_soc_charge: "{0}% de {1}% objetivo — {2}% restante",
    tip_soc_discharge: "{0}% de {1}% mínimo — {2}% acima do mín.",
    tip_time: "{0} decorrido de {1} — {2} restante",
    tip_energy: "{0} de {1} kWh — {2} kWh restantes",
    tip_energy_ahead: "{0} kWh adiantado",
    tip_energy_behind: "{0} kWh atrasado",
    tip_power_active: "{0} de {1} máx.",
    tip_power_deferred: "Autoconsumo — sem exportação forçada",
    tip_power_suspended: "Suspenso — protegendo SoC mín.",
    tip_power_charge_deferred: "Adiado — aguardando momento ideal",
    self_use: "Autocons.",
  },
  "zh-hans": {
    title: "FoxESS 控制",
    smart_charge: "智能充电",
    charge_scheduled: "充电已计划",
    smart_discharge: "智能放电",
    discharge_scheduled: "放电已计划",
    discharge_suspended: "放电暂停",
    discharge_deferred: "放电延迟",
    window: "时段",
    power: "功率",
    target: "目标",
    min_soc: "最低电量",
    feedin: "馈网",
    no_active: "无进行中的操作",
    idle_hint: "调用 <b>smart_charge</b> 或 <b>smart_discharge</b> 开始",
    progress: "进度",
    soc: "电量",
    time: "时间",
    energy: "电量",
    starts_in: "{0}后开始",
    ending: "即将结束",
    kwh_left: "剩余 {0} kWh",
    dur_hm: "{0}时{1}分",
    dur_h: "{0}时",
    dur_m: "{0}分",
    tip_soc_charge: "{0}% / {1}% 目标 — 剩余 {2}%",
    tip_soc_discharge: "{0}% / {1}% 最低 — 高于最低 {2}%",
    tip_time: "已过 {0} / 共 {1} — 剩余 {2}",
    tip_energy: "{0} / {1} kWh — 剩余 {2} kWh",
    tip_energy_ahead: "超前计划 {0} kWh",
    tip_energy_behind: "落后计划 {0} kWh",
    tip_power_active: "{0} / 最大 {1}",
    tip_power_deferred: "自用模式 — 无强制输出",
    tip_power_suspended: "暂停 — 保护最低电量",
    tip_power_charge_deferred: "延迟 — 等待最佳时间",
    self_use: "自用",
  },
  ja: {
    title: "FoxESS コントロール",
    smart_charge: "スマート充電",
    charge_scheduled: "充電予定",
    smart_discharge: "スマート放電",
    discharge_scheduled: "放電予定",
    discharge_suspended: "放電一時停止",
    discharge_deferred: "放電遅延中",
    window: "時間帯",
    power: "電力",
    target: "目標",
    min_soc: "最低残量",
    feedin: "売電",
    no_active: "実行中の操作なし",
    idle_hint: "<b>smart_charge</b> または <b>smart_discharge</b> を呼び出して開始",
    progress: "進捗",
    soc: "残量",
    time: "時間",
    energy: "電力量",
    starts_in: "{0}後に開始",
    ending: "終了間近",
    kwh_left: "残り {0} kWh",
    dur_hm: "{0}時間{1}分",
    dur_h: "{0}時間",
    dur_m: "{0}分",
    tip_soc_charge: "{0}% / {1}% 目標 — 残り {2}%",
    tip_soc_discharge: "{0}% / {1}% 最低 — 最低より {2}% 上",
    tip_time: "経過 {0} / 全体 {1} — 残り {2}",
    tip_energy: "{0} / {1} kWh — 残り {2} kWh",
    tip_energy_ahead: "スケジュールより {0} kWh 先行",
    tip_energy_behind: "スケジュールより {0} kWh 遅延",
    tip_power_active: "{0} / 最大 {1}",
    tip_power_deferred: "自家消費モード — 強制輸出なし",
    tip_power_suspended: "一時停止 — 最低SoC保護中",
    tip_power_charge_deferred: "遅延中 — 最適なタイミングを待機",
    self_use: "自家消費",
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
    this._expandedTips = new Set();
    this.shadowRoot.addEventListener("click", (e) => {
      const row = e.target.closest(".progress-row.has-tip");
      if (!row) return;
      const key = row.dataset.tipKey;
      if (key) {
        if (this._expandedTips.has(key)) {
          this._expandedTips.delete(key);
          row.classList.remove("expanded");
        } else {
          this._expandedTips.add(key);
          row.classList.add("expanded");
        }
      }
    });
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

  /** Read the data_source attribute from the SoC entity. */
  _getDataSource() {
    const e = this._entity(this._config.soc_entity);
    return e && e.attributes && e.attributes.data_source ? e.attributes.data_source : null;
  }

  _dataSourceBadge(source) {
    if (!source) return "";
    const labels = { ws: "WS", api: "API", modbus: "Modbus" };
    const label = labels[source] || source;
    return `<span class="data-source" title="Data: ${label}">${label}</span>`;
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

    const dataSource = this._getDataSource();

    return `
      <div class="header">
        <div class="header-left">
          <div class="title">${this._t("title")}${this._dataSourceBadge(dataSource)}</div>
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
    const deferred = opsState.includes("deferred");

    // Feed-in energy progress
    const feedinLimit = a.discharge_feedin_limit_kwh;
    const feedinUsed = a.discharge_feedin_used_kwh;
    const feedinProjected = a.discharge_feedin_projected_kwh;

    return `
      <div class="section discharge">
        <div class="section-header">
          <div class="section-icon-group">
            <span class="dot ${scheduled || suspended || deferred ? "dot-waiting" : "dot-active dot-discharge"}"></span>
            <span class="section-title">${scheduled ? this._t("discharge_scheduled") : deferred ? this._t("discharge_deferred") : suspended ? this._t("discharge_suspended") : this._t("smart_discharge")}</span>
          </div>
          <span class="section-badge discharge-badge">${this._translateRemaining(remaining)}</span>
        </div>
        <div class="section-body">
          <div class="detail-row">
            <span class="detail-label">${this._t("window")}</span>
            <span class="detail-value">${window}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">${this._t("power")}</span>
            <span class="detail-value">${deferred ? this._t("self_use") : scheduled ? "—" : this._formatPower(power)}</span>
          </div>
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

  _progressBar(label, value, pct, fillClass, tooltip, tipKey) {
    const hasTip = !!tooltip;
    const expanded = hasTip && tipKey && this._expandedTips.has(tipKey);
    return `
      <div class="progress-row${hasTip ? " has-tip" : ""}${expanded ? " expanded" : ""}"${tipKey ? ` data-tip-key="${tipKey}"` : ""}>
        <div class="detail-row">
          <span class="detail-label">${label}</span>
          <span class="detail-value">${value}</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill ${fillClass}" style="width:${pct}%"></div>
        </div>
        ${hasTip ? `<div class="progress-tip">${tooltip}</div>` : ""}
      </div>
    `;
  }

  _energyScheduleBar(label, value, actualPct, expectedPct, tooltip, tipKey) {
    // Show energy progress with a coloured gap segment indicating
    // whether discharge is ahead of or behind the ideal schedule.
    const lo = Math.min(actualPct, expectedPct);
    const hi = Math.max(actualPct, expectedPct);
    const gapWidth = hi - lo;
    const ahead = actualPct >= expectedPct;
    const gapClass = ahead ? "energy-ahead" : "energy-behind";
    const hasTip = !!tooltip;
    const expanded = hasTip && tipKey && this._expandedTips.has(tipKey);

    return `
      <div class="progress-row${hasTip ? " has-tip" : ""}${expanded ? " expanded" : ""}"${tipKey ? ` data-tip-key="${tipKey}"` : ""}>
        <div class="detail-row">
          <span class="detail-label">${label}</span>
          <span class="detail-value">${value}</span>
        </div>
        <div class="progress-track">
          <div class="energy-fill" style="width:${lo}%"></div>${gapWidth > 0.5 ? `<div class="${gapClass}" style="width:${gapWidth}%"></div>` : ""}
        </div>
        ${hasTip ? `<div class="progress-tip">${tooltip}</div>` : ""}
      </div>
    `;
  }

  _timeProgress(startIso, endIso, now) {
    const startTime = startIso ? new Date(startIso).getTime() : null;
    const endTime = endIso ? new Date(endIso).getTime() : null;
    if (!startTime || !endTime || endTime <= startTime) return { pct: 0, label: "", remaining: 0 };
    const elapsed = now - startTime;
    const total = endTime - startTime;
    return {
      pct: Math.min(100, Math.max(0, (elapsed / total) * 100)),
      label: `${this._formatDuration(elapsed)} / ${this._formatDuration(total)}`,
      remaining: Math.max(0, total - elapsed),
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
      const curStr = current != null ? Math.round(current) + "%" : "?%";
      const tgtStr = target != null ? target + "%" : "?%";
      const socLabel = startSoc != null && Math.round(startSoc) !== Math.round(current ?? startSoc)
        ? `${Math.round(startSoc)}% → ${curStr} → ${tgtStr}`
        : `${curStr} → ${tgtStr}`;
      const time = this._timeProgress(a.charge_start_time, a.charge_end_time, now);

      const socTip = current != null && target != null
        ? this._t("tip_soc_charge").replace("{0}", Math.round(current)).replace("{1}", target).replace("{2}", Math.max(0, target - Math.round(current)))
        : "";
      const timeTip = time.label
        ? this._t("tip_time").replace("{0}", this._formatDuration(now - new Date(a.charge_start_time).getTime())).replace("{1}", this._formatDuration(new Date(a.charge_end_time).getTime() - new Date(a.charge_start_time).getTime())).replace("{2}", this._formatDuration(time.remaining))
        : "";

      const chargePower = a.charge_power_w || 0;
      const chargeMax = a.charge_max_power_w || 1;
      const chargePowerPct = Math.min(100, Math.max(0, (chargePower / chargeMax) * 100));
      const chargePhase = a.charge_phase;
      const chargePowerTip = chargePhase === "deferred"
        ? this._t("tip_power_charge_deferred")
        : this._t("tip_power_active").replace("{0}", this._formatPower(chargePower)).replace("{1}", this._formatPower(chargeMax));

      bars += this._progressBar(this._t("soc"), socLabel, socPct, "charge-fill", socTip, "charge-soc");
      bars += this._progressBar(this._t("power"), this._formatPower(chargePower), chargePowerPct, "charge-fill", chargePowerTip, "charge-power");
      bars += this._progressBar(this._t("time"), time.label, time.pct, "time-fill", timeTip, "charge-time");
    }

    if (dischargeActive && a.discharge_phase !== "scheduled") {
      const startSoc = a.discharge_start_soc;
      const current = a.discharge_current_soc;
      const minSoc = a.discharge_min_soc;

      let socPct = 0;
      if (startSoc != null && minSoc != null && current != null && startSoc > minSoc) {
        socPct = Math.min(100, Math.max(0, ((startSoc - current) / (startSoc - minSoc)) * 100));
      }
      const curStr = current != null ? Math.round(current) + "%" : "?%";
      const minStr = minSoc != null ? minSoc + "%" : "?%";
      const socLabel = startSoc != null && Math.round(startSoc) !== Math.round(current ?? startSoc)
        ? `${Math.round(startSoc)}% → ${curStr} → ${minStr}`
        : `${curStr} → ${minStr}`;

      const socTip = current != null && minSoc != null
        ? this._t("tip_soc_discharge").replace("{0}", Math.round(current)).replace("{1}", minSoc).replace("{2}", Math.max(0, Math.round(current) - minSoc))
        : "";
      bars += this._progressBar(this._t("soc"), socLabel, socPct, "discharge-fill", socTip, "discharge-soc");

      const dischPower = a.discharge_power_w || 0;
      const dischMax = a.discharge_max_power_w || 1;
      const dischPowerPct = Math.min(100, Math.max(0, (dischPower / dischMax) * 100));
      const dischPhase = a.discharge_phase;
      let dischPowerTip;
      if (dischPhase === "deferred") {
        dischPowerTip = this._t("tip_power_deferred");
      } else if (dischPhase === "suspended") {
        dischPowerTip = this._t("tip_power_suspended");
      } else {
        dischPowerTip = this._t("tip_power_active").replace("{0}", this._formatPower(dischPower)).replace("{1}", this._formatPower(dischMax));
      }
      const dischPowerLabel = dischPhase === "deferred" ? this._t("self_use") : this._formatPower(dischPower);
      bars += this._progressBar(this._t("power"), dischPowerLabel, dischPowerPct, "discharge-fill", dischPowerTip, "discharge-power");

      const time = this._timeProgress(a.discharge_start_time, a.discharge_end_time, now);

      const feedinLimit = a.discharge_feedin_limit_kwh;
      if (feedinLimit != null && feedinLimit > 0) {
        const used = a.discharge_feedin_used_kwh ?? 0;
        const energyPct = Math.min(100, Math.max(0, (used / feedinLimit) * 100));
        const remaining = Math.max(0, feedinLimit - used).toFixed(1);
        let energyTip = this._t("tip_energy").replace("{0}", used.toFixed(1)).replace("{1}", feedinLimit.toFixed(1)).replace("{2}", remaining);
        const diff = Math.abs(energyPct - time.pct) * feedinLimit / 100;
        if (diff > 0.05) {
          energyTip += " · " + (energyPct >= time.pct
            ? this._t("tip_energy_ahead").replace("{0}", diff.toFixed(1))
            : this._t("tip_energy_behind").replace("{0}", diff.toFixed(1)));
        }
        bars += this._energyScheduleBar(this._t("energy"), `${used} / ${feedinLimit} kWh`, energyPct, time.pct, energyTip, "discharge-energy");
      }

      const timeTip = time.label
        ? this._t("tip_time").replace("{0}", this._formatDuration(now - new Date(a.discharge_start_time).getTime())).replace("{1}", this._formatDuration(new Date(a.discharge_end_time).getTime() - new Date(a.discharge_start_time).getTime())).replace("{2}", this._formatDuration(time.remaining))
        : "";
      bars += this._progressBar(this._t("time"), time.label, time.pct, "time-fill", timeTip, "discharge-time");
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
      .progress-row.has-tip {
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
      }
      .progress-tip {
        max-height: 0;
        overflow: hidden;
        font-size: 0.78em;
        color: var(--secondary-text-color, #888);
        line-height: 1.4;
        transition: max-height 0.2s ease, margin-top 0.2s ease, opacity 0.2s ease;
        opacity: 0;
      }
      .progress-row.expanded .progress-tip {
        max-height: 3em;
        margin-top: 4px;
        opacity: 1;
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
