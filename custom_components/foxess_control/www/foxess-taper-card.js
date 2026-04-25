// FoxESS Taper Profile card
// Renders the BMS charge/discharge acceptance-ratio histogram
// exposed via sensor.foxess_smart_operations.attributes.taper_profile
// (UX #5).  A standalone card rather than a section inside the
// control card because the taper profile is always-useful, not
// session-state.

// -- i18n --------------------------------------------------------------------

const TRANSLATIONS = {
  en: {
    taper_profile_title: "Taper profile",
    taper_subtitle: "BMS acceptance ratio per 5% SoC bin",
    taper_charge: "Charge",
    taper_discharge: "Discharge",
    taper_no_observations: "No observations yet",
    taper_low_confidence: "Low-confidence bin (fewer than 3 observations)",
  },
  de: {
    taper_profile_title: "Taper-Profil",
    taper_subtitle: "BMS-Annahmeverhältnis pro 5%-SoC-Intervall",
    taper_charge: "Laden",
    taper_discharge: "Entladen",
    taper_no_observations: "Noch keine Beobachtungen",
    taper_low_confidence: "Unsicherer Wert (weniger als 3 Beobachtungen)",
  },
  fr: {
    taper_profile_title: "Profil de dégressivité",
    taper_subtitle: "Taux d'acceptation BMS par tranche SoC 5%",
    taper_charge: "Charge",
    taper_discharge: "Décharge",
    taper_no_observations: "Aucune observation",
    taper_low_confidence: "Valeur peu fiable (moins de 3 observations)",
  },
  nl: {
    taper_profile_title: "Taper-profiel",
    taper_subtitle: "BMS-acceptatiepercentage per SoC-stap van 5%",
    taper_charge: "Laden",
    taper_discharge: "Ontladen",
    taper_no_observations: "Nog geen metingen",
    taper_low_confidence: "Onzeker (minder dan 3 metingen)",
  },
  es: {
    taper_profile_title: "Perfil de atenuación",
    taper_subtitle: "Relación de aceptación del BMS por franja de 5% SoC",
    taper_charge: "Carga",
    taper_discharge: "Descarga",
    taper_no_observations: "Sin observaciones",
    taper_low_confidence: "Dato poco fiable (menos de 3 observaciones)",
  },
  it: {
    taper_profile_title: "Profilo di attenuazione",
    taper_subtitle: "Rapporto di accettazione BMS per fascia 5% SoC",
    taper_charge: "Ricarica",
    taper_discharge: "Scarica",
    taper_no_observations: "Nessuna osservazione",
    taper_low_confidence: "Dato poco affidabile (meno di 3 osservazioni)",
  },
  pl: {
    taper_profile_title: "Profil ograniczenia",
    taper_subtitle: "Współczynnik akceptacji BMS na 5% SoC",
    taper_charge: "Ładowanie",
    taper_discharge: "Rozładowanie",
    taper_no_observations: "Brak obserwacji",
    taper_low_confidence: "Mało wiarygodne (mniej niż 3 obserwacje)",
  },
  pt: {
    taper_profile_title: "Perfil de redução",
    taper_subtitle: "Taxa de aceitação do BMS por 5% de SoC",
    taper_charge: "Carga",
    taper_discharge: "Descarga",
    taper_no_observations: "Sem observações",
    taper_low_confidence: "Dado pouco confiável (menos de 3 observações)",
  },
  "zh-hans": {
    taper_profile_title: "Taper 曲线",
    taper_subtitle: "BMS 按 5% SoC 接受比例",
    taper_charge: "充电",
    taper_discharge: "放电",
    taper_no_observations: "暂无观测",
    taper_low_confidence: "低置信 (观测少于 3 次)",
  },
  ja: {
    taper_profile_title: "テーパー プロファイル",
    taper_subtitle: "BMS受入率 (5% SoC刻み)",
    taper_charge: "充電",
    taper_discharge: "放電",
    taper_no_observations: "観測データなし",
    taper_low_confidence: "信頼度低 (観測3件未満)",
  },
};

function _getStrings(lang) {
  if (!lang) return TRANSLATIONS.en;
  if (TRANSLATIONS[lang]) return TRANSLATIONS[lang];
  // Try family prefix: "en-US" -> "en", "zh-CN" -> "zh-hans".
  const prefix = lang.split("-")[0].toLowerCase();
  if (prefix === "zh") return TRANSLATIONS["zh-hans"];
  return TRANSLATIONS[prefix] || TRANSLATIONS.en;
}

// -- Card --------------------------------------------------------------------

class FoxESSTaperCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
  }

  setConfig(config) {
    this._config = Object.assign(
      { entity: "sensor.foxess_smart_operations" },
      config || {},
    );
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 4;
  }

  _t(key) {
    const lang =
      this._hass &&
      (this._hass.language ||
        (this._hass.locale && this._hass.locale.language));
    const strings = _getStrings(lang);
    return strings[key] || TRANSLATIONS.en[key] || key;
  }

  _render() {
    if (!this._hass || !this._config) return;
    const state = this._hass.states[this._config.entity];
    const taper = state && state.attributes && state.attributes.taper_profile;
    const chargeBins = (taper && taper.charge) || [];
    const dischargeBins = (taper && taper.discharge) || [];
    const hasAny = chargeBins.length + dischargeBins.length > 0;

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="card-header">
          <div class="title">${this._t("taper_profile_title")}</div>
          <div class="subtitle">${this._t("taper_subtitle")}</div>
        </div>
        <div class="card-body">
          ${
            hasAny
              ? `
            ${this._renderSection(this._t("taper_charge"), chargeBins, "charge")}
            ${this._renderSection(this._t("taper_discharge"), dischargeBins, "discharge")}
          `
              : `<div class="empty">${this._t("taper_no_observations")}</div>`
          }
        </div>
      </ha-card>
    `;
  }

  _renderSection(title, bins, kind) {
    if (!bins || bins.length === 0) {
      return `
        <div class="section-title">${title}</div>
        <div class="empty">${this._t("taper_no_observations")}</div>
      `;
    }
    const rows = bins
      .map((b) => {
        const pct = Math.max(0, Math.min(100, Math.round((b.ratio || 0) * 100)));
        const lowConf = (b.count || 0) < 3;
        return `
        <div class="bar-row ${lowConf ? "low-conf" : ""}"
             title="${lowConf ? this._t("taper_low_confidence") : ""}">
          <span class="soc-label">${b.soc}%</span>
          <div class="bar-track">
            <div class="bar-fill bar-${kind}" style="width:${pct}%"></div>
          </div>
          <span class="ratio-label">${pct}%</span>
          <span class="count">(${b.count || 0})${lowConf ? " ·" : ""}</span>
        </div>
      `;
      })
      .join("");
    return `
      <div class="section-title">${title}</div>
      <div class="bars">${rows}</div>
    `;
  }

  _styles() {
    return `
      ha-card { padding: 16px; }
      .card-header { margin-bottom: 12px; }
      .title { font-size: 1.1em; font-weight: 500; }
      .subtitle {
        font-size: 0.85em; opacity: 0.7; margin-top: 2px;
      }
      .section-title {
        font-size: 0.95em; font-weight: 500;
        margin: 12px 0 6px; opacity: 0.85;
      }
      .empty {
        padding: 12px 0; opacity: 0.6; font-style: italic;
      }
      .bars { display: flex; flex-direction: column; gap: 4px; }
      .bar-row {
        display: flex; align-items: center;
        gap: 8px; font-size: 0.9em;
      }
      .bar-row.low-conf { opacity: 0.55; }
      .soc-label {
        min-width: 3em; text-align: right; font-variant-numeric: tabular-nums;
      }
      .bar-track {
        flex: 1; height: 10px; background: var(--divider-color, #e0e0e0);
        border-radius: 2px; overflow: hidden;
      }
      .bar-fill {
        height: 100%; transition: width 0.3s ease;
      }
      .bar-charge { background: var(--success-color, #43a047); }
      .bar-discharge { background: var(--warning-color, #f0b400); }
      .ratio-label {
        min-width: 3em; text-align: right; font-variant-numeric: tabular-nums;
      }
      .count {
        min-width: 2.5em; text-align: left;
        opacity: 0.55; font-size: 0.85em;
      }
    `;
  }
}

customElements.define("foxess-taper-card", FoxESSTaperCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "foxess-taper-card",
  name: "FoxESS Taper Profile",
  description:
    "Visualises BMS charge/discharge acceptance ratios per SoC bin.",
});
