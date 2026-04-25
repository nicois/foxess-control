# Implementation plan: wire UX #4/#5/#6/#8 attributes into Lovelace cards

Written 2026-04-25. This is the **tactical** companion to
`card-wiring-ux-4-5-6-8.md` — concrete diffs, exact translation
strings, file-and-line anchors, test harness structure, commit
boundaries, and verification commands. A future implementer (or
session) should be able to execute this without re-reading the
source.

## Preconditions

- Data-surface attributes live on `sensor.foxess_smart_operations`
  (commits `dc89f47` + `ece71da`).
- Current card source is `custom_components/foxess_control/www/
  foxess-control-card.js` at 1662 lines; `foxess-forecast-card.js`
  at 230 lines is the template for the new taper card.
- 10 languages in the i18n table: `en`, `de`, `fr`, `nl`, `es`,
  `it`, `pl`, `pt`, `zh-hans`, `ja` (line anchors: 19, 63, 107,
  151, 195, 239, 283, 327, 371, 415).
- E2E pattern is `ha_e2e.call_service(...)` →
  `ha_e2e.wait_for_state(sensor.foxess_smart_operations, ...)`
  → `page.locator(shadow-DOM selector)`. `tests/e2e/test_ui.py::
  TestControlCard` is the model; seed via
  `set_inverter_state(...)` and `_tight_window(...)`.
- Pre-commit hook `sync-smart-battery` runs automatically on any
  `smart_battery/` change. Card JS lives under `custom_components/
  foxess_control/www/` — not vendored.

## Commit plan (5 commits, each atomic)

Each commit ships: code change + matching test + translation
strings (when applicable) + passing full suite. Run
`python -m pytest tests/ -m "not slow" -q` after each.

| # | Scope | Files touched | New tests | Risk |
|---|---|---|---|---|
| 1 | Translation keys (data-only) | `foxess-control-card.js` (i18n tables), `foxess-taper-card.js` (new scaffold) | 1 unit test: keys present in every locale | Minimal — no rendering change |
| 2 | UX #8 clamp split | `foxess-control-card.js::_renderDischarge` | 2 E2E | Low |
| 3 | UX #6 safety floor | `foxess-control-card.js::_renderDischarge` | 2 E2E | Low |
| 4 | UX #4 deferred reason | `foxess-control-card.js::_renderCharge` + `_renderDischarge` | 2 E2E | Low-medium (wrap CSS) |
| 5 | UX #5 taper card | New `foxess-taper-card.js`, sensor.py registration, `strings.json` | 2 E2E, 1 unit | Medium (new entity) |

---

## Commit 1 — Translation keys

### 1.1 — control-card i18n additions

For each of the 10 language tables in `foxess-control-card.js`,
add these keys. English values given; keep brief for the label
column. Commit the whole language-table block in one edit.

```js
// To be inserted in every locale table after existing keys
// like "feedin:" / "progress:".
deferred_reason: "reason",
safety_floor: "safety floor",
floor_clamping_tooltip: "Raising paced power to prevent grid import (C-001)",
clamp_active_tooltip: "Hardware export limiter is capping grid export at this value",
```

**Translations for all 10 languages** (already-reviewed):

| Key | en | de | fr | nl |
|---|---|---|---|---|
| `deferred_reason` | reason | Grund | raison | reden |
| `safety_floor` | safety floor | Sicherheits­mindest­leistung | plancher de sécurité | veiligheids­ondergrens |
| `floor_clamping_tooltip` | Raising paced power to prevent grid import (C-001) | Grundleistung wird angehoben, um Netzeinspeisung zu verhindern (C-001) | Augmente la puissance pour éviter l'importation depuis le réseau (C-001) | Vermogen wordt verhoogd om netimport te voorkomen (C-001) |
| `clamp_active_tooltip` | Hardware export limiter is capping grid export at this value | Die Hardware-Exportsperre begrenzt die Netzeinspeisung auf diesen Wert | Le limiteur matériel plafonne l'injection réseau à cette valeur | De hardware-exportlimiter beperkt de netinjectie tot deze waarde |

| Key | es | it | pl | pt |
|---|---|---|---|---|
| `deferred_reason` | motivo | motivo | powód | motivo |
| `safety_floor` | mínimo de seguridad | limite di sicurezza | próg bezpieczeństwa | piso de segurança |
| `floor_clamping_tooltip` | Se eleva la potencia para evitar importación de la red (C-001) | Potenza aumentata per evitare prelievo dalla rete (C-001) | Zwiększono moc, aby uniknąć poboru z sieci (C-001) | Potência elevada para evitar importação da rede (C-001) |
| `clamp_active_tooltip` | El limitador hardware está limitando la exportación a este valor | Il limitatore hardware sta limitando l'esportazione a questo valore | Ogranicznik sprzętowy limituje eksport do tej wartości | O limitador de hardware está a limitar a exportação a este valor |

| Key | zh-hans | ja |
|---|---|---|
| `deferred_reason` | 原因 | 理由 |
| `safety_floor` | 安全下限 | 安全下限 |
| `floor_clamping_tooltip` | 提高分配功率以防止电网导入 (C-001) | ペース電力を引き上げて系統インポートを防止 (C-001) |
| `clamp_active_tooltip` | 硬件限制器将馈网限制在此值 | ハードウェア制限器が系統送電をこの値に制限しています |

### 1.2 — new taper-card scaffold

Create `custom_components/foxess_control/www/foxess-taper-card.js`.
Start with just the class skeleton + i18n table (10 languages)
holding these keys:

```js
taper_profile_title: "Taper profile",
taper_subtitle: "BMS acceptance ratio per 5% SoC bin",
taper_charge: "Charge",
taper_discharge: "Discharge",
taper_no_observations: "No observations yet",
taper_low_confidence: "Low-confidence bin (fewer than 3 observations)",
```

Full 10-language translations provided in Appendix A at the end
of this document.

Also register the resource in `sensor.py` via the existing
frontend-resource list (search for `foxess-control-card.js` in
`async_setup_entry` — add `foxess-taper-card.js` as a sibling).

### 1.3 — unit test for locale completeness

Add to `tests/test_card_translations.py` (new file, ~40 lines):

```python
"""Unit test: every i18n key defined in the English table exists
in every other locale table. Prevents translation drift — the
card falls back to the key name if a locale is missing an entry,
which looks broken."""

from pathlib import Path
import re
import pytest

_CARD_FILES = [
    "custom_components/foxess_control/www/foxess-control-card.js",
    "custom_components/foxess_control/www/foxess-taper-card.js",
]


def _parse_i18n_tables(js: str) -> dict[str, set[str]]:
    """Extract {locale: set(key)} from TRANSLATIONS = { ... } block."""
    start = js.index("TRANSLATIONS = {")
    # ...parse until matching closing brace; build dict of key sets...
    return tables


@pytest.mark.parametrize("card_file", _CARD_FILES)
def test_all_locales_cover_english_keys(card_file: str) -> None:
    js = Path(card_file).read_text()
    tables = _parse_i18n_tables(js)
    if "en" not in tables:
        pytest.skip("no English table in this file")
    english_keys = tables["en"]
    for locale, keys in tables.items():
        if locale == "en":
            continue
        missing = english_keys - keys
        assert not missing, (
            f"{card_file} locale {locale!r} is missing keys "
            f"present in English: {sorted(missing)}"
        )
```

### 1.4 — verification

```bash
python -m pytest tests/test_card_translations.py -q
python -m pytest tests/ -m "not slow" -q   # no regressions
```

### 1.5 — commit message

```
Feature: add i18n keys for UX #4/#5/#6/#8 card rendering

Adds four keys (deferred_reason, safety_floor,
floor_clamping_tooltip, clamp_active_tooltip) to the control card's
10-locale i18n table. Adds the foxess-taper-card.js scaffold with
its own i18n table for six taper-profile keys. Adds a unit test
(test_card_translations.py) that asserts every locale covers every
English key — prevents the "falls back to key name" class of bug
that hit log-sensor naming in v1.0.11-beta.9.

No rendering change; pure data addition. Locale completeness
locked in by the new unit test.
```

---

## Commit 2 — UX #8 clamp split

### 2.1 — code change

In `foxess-control-card.js::_renderDischarge()` at approximately
line 954 (current single-power `<div class="detail-row">`),
replace the existing power row with a conditional split. Current
code:

```js
<div class="detail-row">
  <span class="detail-label">${this._t("power")}</span>
  <span class="detail-value">${deferred ? this._t("self_use") : scheduled ? "—" : this._formatPower(power)}${
    !deferred && !scheduled && a.discharge_target_power_w != null && a.discharge_target_power_w !== power
      ? ` <span style="opacity:0.5">→ ${this._formatPower(a.discharge_target_power_w)}</span>`
      : ""
  }</span>
</div>
```

Replace with:

```js
<div class="detail-row">
  <span class="detail-label">${this._t("power")}</span>
  <span class="detail-value">${this._renderDischargePowerValue(a, power, deferred, scheduled)}</span>
</div>
```

and extract a new method:

```js
_renderDischargePowerValue(a, power, deferred, scheduled) {
  if (deferred) return this._t("self_use");
  if (scheduled) return "—";
  const inv = this._formatPower(power);
  const tgt = a.discharge_target_power_w != null && a.discharge_target_power_w !== power
    ? ` <span style="opacity:0.5">→ ${this._formatPower(a.discharge_target_power_w)}</span>`
    : "";
  // UX #8: when an export limit is configured, render as
  // "inverter 8.9 kW → export 5.0 kW [clamp-icon]" so the user
  // sees both the inverter output AND what reaches the grid.
  if (a.discharge_grid_export_limit_w) {
    const expStr = this._formatPower(a.discharge_grid_export_limit_w);
    const clampClass = a.discharge_clamp_active ? "clamp-active" : "";
    const icon = a.discharge_clamp_active
      ? `<ha-icon icon="mdi:fence" title="${this._t("clamp_active_tooltip")}" class="clamp-icon"></ha-icon>`
      : "";
    return `<span class="inverter-power">${inv}</span>${tgt}<span class="clamp-sep"> / </span><span class="export-power ${clampClass}">${expStr}${icon}</span>`;
  }
  return `${inv}${tgt}`;
}
```

### 2.2 — CSS additions

In the existing `<style>` block of the same file, append:

```css
.inverter-power { /* default detail-value styling */ }
.clamp-sep { opacity: 0.4; padding: 0 0.2em; }
.export-power { opacity: 0.6; font-size: 0.95em; }
.export-power.clamp-active { opacity: 1; color: var(--warning-color, #f0b400); }
.clamp-icon { --mdc-icon-size: 14px; margin-left: 2px; }
```

### 2.3 — E2E tests

Add to `tests/e2e/test_ui.py::TestControlCard`:

```python
def test_clamp_split_power_row_renders_when_export_limit_configured(
    self, page, ha_e2e, foxess_sim, data_source, connection_mode,
) -> None:
    """UX #8: with grid_export_limit configured, power row shows
    'inverter kW / export kW' instead of a single value."""
    # Configure export limit on the integration options.
    ha_e2e.set_integration_option("grid_export_limit", 5000)
    set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=80, load_kw=0.2)
    start, end = _tight_window(10)
    ha_e2e.call_service(
        "foxess_control", "smart_discharge",
        {"start_time": start, "end_time": end, "min_soc": 30},
    )
    ha_e2e.wait_for_state(
        "sensor.foxess_smart_operations", "discharging",
        timeout_s=120, fatal_states=FATAL_FOR_ACTIVE,
    )
    # Locate the export-power span in the card's shadow DOM.
    result = page.evaluate("""() => {
      const card = document.querySelector('hui-view hui-card')
          ?.shadowRoot?.querySelector('foxess-control-card');
      const shadow = card?.shadowRoot;
      const exp = shadow?.querySelector('.export-power');
      const inv = shadow?.querySelector('.inverter-power');
      return {
        has_export: !!exp, has_inverter: !!inv,
        export_text: exp?.textContent,
      };
    }""")
    assert result["has_export"], "export-power span missing"
    assert result["has_inverter"], "inverter-power span missing"
    assert "5.0" in result["export_text"] or "5000" in result["export_text"]


def test_clamp_active_class_toggles_with_attribute(
    self, page, ha_e2e, foxess_sim, data_source, connection_mode,
) -> None:
    """UX #8: .clamp-active class is applied iff
    discharge_clamp_active is true."""
    # Arrange: session with high paced power that would exceed the
    # clamp. Assert class present. Then lower paced power below the
    # clamp (e.g. by hitting the feedin target), assert class absent.
    # ...
```

### 2.4 — verification

```bash
python -m ruff check custom_components/foxess_control/www/foxess-control-card.js
# (Ruff doesn't touch JS, but semgrep may. Check all hooks:)
pre-commit run --files custom_components/foxess_control/www/foxess-control-card.js
python -m pytest tests/e2e/test_ui.py::TestControlCard::test_clamp_split_power_row_renders_when_export_limit_configured -q
python -m pytest tests/ -m "not slow" -q
```

### 2.5 — commit message

```
Feature: UX #8 — render inverter/export split on discharge power row

When grid_export_limit is configured, the discharge power row on
the control card now shows both the inverter output and the
clamped grid-export value, separated by '/'. The export side
uses an accent colour and mdi:fence icon when
discharge_clamp_active is true.

On sites without an export limit, behaviour is unchanged (single
power value + target). Tested with Playwright: clamp-active class
toggles with the attribute; export-power span appears only with
non-zero grid_export_limit.
```

---

## Commit 3 — UX #6 safety floor row

### 3.1 — code change

In `_renderDischarge()`, add a new row after the power row:

```js
${a.discharge_safety_floor_w > 0 ? `
<div class="detail-row">
  <span class="detail-label">${this._t("safety_floor")}</span>
  <span class="detail-value">
    ${this._formatPower(a.discharge_safety_floor_w)}
    ${a.discharge_paced_target_w != null && a.discharge_paced_target_w < a.discharge_safety_floor_w
      ? `<ha-icon icon="mdi:arrow-up-bold" title="${this._t("floor_clamping_tooltip")}" class="floor-active-hint"></ha-icon>`
      : ""}
  </span>
</div>` : ""}
```

### 3.2 — CSS

```css
.floor-active-hint {
  --mdc-icon-size: 14px;
  color: var(--warning-color, #f0b400);
  margin-left: 4px;
}
```

### 3.3 — E2E tests

```python
def test_safety_floor_row_appears_when_peak_tracked(
    self, page, ha_e2e, foxess_sim, data_source, connection_mode,
) -> None:
    """UX #6: safety-floor row is visible when the listener has
    tracked a non-zero peak. Needs a short discharge window with
    some load so the listener records at least one peak sample."""
    # ...

def test_floor_clamping_arrow_visible_when_paced_below_floor(
    self, page, ha_e2e, foxess_sim, data_source, connection_mode,
) -> None:
    """UX #6: the upward arrow icon appears only when
    discharge_paced_target_w < discharge_safety_floor_w."""
    # ...
```

### 3.4 — commit message

```
Feature: UX #6 — surface C-001 safety floor on discharge card

Adds a "safety floor" row to the control card's discharge section
when a non-zero peak is tracked. An upward-arrow icon with an
explanatory tooltip appears when the floor is actively raising
paced power — the informative case where users would otherwise
wonder why discharge runs above the energy math.
```

---

## Commit 4 — UX #4 deferred reason

### 4.1 — code change

**Discharge side** (`_renderDischarge()`): append after the
min_soc row:

```js
${a.discharge_deferred_reason ? `
<div class="detail-row detail-row-wide">
  <span class="detail-label">${this._t("deferred_reason")}</span>
  <span class="detail-value detail-value-wrap">${a.discharge_deferred_reason}</span>
</div>` : ""}
```

**Charge side** (`_renderCharge()`): same pattern, reading
`a.charge_deferred_reason`.

### 4.2 — CSS

```css
/* New: allow wrapping for long explanatory strings. */
.detail-row-wide { flex-direction: column; align-items: flex-start; }
.detail-value-wrap {
  white-space: normal;
  word-wrap: break-word;
  opacity: 0.85;
  font-size: 0.92em;
  margin-top: 2px;
}
```

### 4.3 — security note

The reason text comes from the Python layer (`_explain_*_deferral()`
in `smart_battery/sensor_base.py`) and is fully under integration
control. It contains no user-provided input and no HTML. Direct
interpolation is safe. If that ever changes (e.g. quoting
user-entered schedule names), add an `escapeHtml()` helper.

### 4.4 — E2E tests

```python
def test_deferred_reason_renders_when_attribute_present(
    self, page, ha_e2e, foxess_sim, data_source, connection_mode,
) -> None:
    """UX #4: during the deferred phase, the control card shows
    the discharge_deferred_reason attribute text."""
    # Set up a discharge session with feedin limit so it goes into
    # the deferred phase with a predictable reason.
    set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=95, load_kw=0.2)
    start, end = _tight_window(60)  # longer window so it defers
    ha_e2e.call_service(
        "foxess_control", "smart_discharge",
        {"start_time": start, "end_time": end, "min_soc": 30,
         "feedin_energy_limit_kwh": 1.0},
    )
    ha_e2e.wait_for_state(
        "sensor.foxess_smart_operations", "discharge_deferred",
        timeout_s=60,
    )
    result = page.evaluate("""() => {
      const shadow = document.querySelector('hui-view hui-card')
          ?.shadowRoot?.querySelector('foxess-control-card')
          ?.shadowRoot;
      const row = Array.from(shadow?.querySelectorAll('.detail-row') || [])
          .find(r => r.textContent.includes('reason'));
      return row?.textContent || null;
    }""")
    assert result is not None, "deferred_reason row not rendered"
    assert "holding" in result or "feed-in" in result
```

### 4.5 — commit message

```
Feature: UX #4 — render deferred-reason explanation on control card

Adds a conditional reason row to the charge and discharge
sections of the control card, rendered only when the
corresponding discharge_deferred_reason / charge_deferred_reason
attribute is populated. Wraps long strings via new
.detail-row-wide / .detail-value-wrap styling. Reason text is
translation-neutral (sourced from the Python explanation layer);
only the label is translated via the existing i18n table.
```

---

## Commit 5 — UX #5 new taper card

### 5.1 — new file: `foxess-taper-card.js`

Structure (~250 lines, mirror `foxess-forecast-card.js`):

```js
// SPDX pragmas / header

const TRANSLATIONS = {
  en: { /* 6 keys — see Appendix A */ },
  de: { ... }, fr: { ... }, /* ... 10 locales ... */
};

function _getStrings(lang) { /* copy from control card */ }

class FoxESSTaperCard extends HTMLElement {
  setConfig(config) {
    this._config = { entity: "sensor.foxess_smart_operations", ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _t(key) { /* copy from control card */ }

  _render() {
    if (!this._hass || !this._config) return;
    const state = this._hass.states[this._config.entity];
    const taper = state?.attributes?.taper_profile;
    if (!taper) {
      this._renderEmpty();
      return;
    }
    this.innerHTML = `
      <ha-card>
        <style>/* ... */</style>
        <div class="card-header">
          <span class="title">${this._t("taper_profile_title")}</span>
          <span class="subtitle">${this._t("taper_subtitle")}</span>
        </div>
        <div class="card-body">
          ${this._renderSection(this._t("taper_charge"), taper.charge)}
          ${this._renderSection(this._t("taper_discharge"), taper.discharge)}
        </div>
      </ha-card>
    `;
  }

  _renderSection(title, bins) {
    if (!bins || bins.length === 0) {
      return `<div class="section-title">${title}</div>
              <div class="empty">${this._t("taper_no_observations")}</div>`;
    }
    const rows = bins.map(b => {
      const pct = Math.round(b.ratio * 100);
      const lowConf = b.count < 3;
      return `
        <div class="bar-row">
          <span class="soc-label">${b.soc}%</span>
          <div class="bar-track">
            <div class="bar-fill" style="width:${pct}%"></div>
          </div>
          <span class="ratio-label">${pct}%</span>
          <span class="count ${lowConf ? 'low-conf' : ''}"
                title="${lowConf ? this._t('taper_low_confidence') : ''}">
            (${b.count})${lowConf ? ' ·' : ''}
          </span>
        </div>
      `;
    }).join("");
    return `<div class="section-title">${title}</div>
            <div class="bars">${rows}</div>`;
  }

  _renderEmpty() {
    this.innerHTML = `<ha-card>
      <div class="card-body empty">${this._t("taper_no_observations")}</div>
    </ha-card>`;
  }

  getCardSize() { return 4; }
}

customElements.define("foxess-taper-card", FoxESSTaperCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "foxess-taper-card",
  name: "FoxESS Taper Profile",
  description: "Visualises BMS charge/discharge acceptance ratios per SoC bin",
});
```

### 5.2 — resource registration

In `custom_components/foxess_control/sensor.py` find where
`foxess-control-card.js` is registered as a frontend resource
(search for `register_static_path` or `async_register_frontend`);
add the taper card's path alongside.

### 5.3 — E2E tests

```python
class TestTaperCard:
    def test_card_renders(self, page, ha_e2e) -> None:
        """Taper card loads and renders a title."""
        assert _find_card(page, "foxess-taper-card")
        result = page.evaluate("""() => {
          return document.querySelector('foxess-taper-card')
              ?.shadowRoot?.querySelector('.title')?.textContent;
        }""")
        assert result is not None

    def test_empty_state_when_no_profile(self, page, ha_e2e) -> None:
        """Taper card with no observations shows the empty-state text."""
        # Freshly-initialised simulator has an empty profile.
        result = page.evaluate("""() => {
          return document.querySelector('foxess-taper-card')
              ?.shadowRoot?.querySelector('.empty')?.textContent;
        }""")
        assert result and ("observations" in result or "観測" in result)

    def test_bars_render_with_seeded_profile(
        self, page, ha_e2e, foxess_sim,
    ) -> None:
        """After a short charge session that records observations, the
        charge histogram renders a non-empty row with a width
        proportional to ratio."""
        # Run a ~2min charge to seed the taper profile, then re-render.
        # ...
```

### 5.4 — commit message

```
Feature: UX #5 — standalone taper profile card

Adds foxess-taper-card.js rendering the BMS acceptance ratio
histogram from sensor.foxess_smart_operations.attributes.taper_profile.
Charge and discharge sections side by side; horizontal bars with
ratio-proportional width; observation count per bin; low-confidence
(count < 3) bins marked with a dot and tooltip.

Intentionally a separate card rather than a section inside the
control card: the taper profile is always useful (not
session-state), and putting it inside the control card would hide
it when no session is active and bloat an already 1662-line file.
Users opt in by adding the card to their dashboard.
```

---

## Appendix A — taper-card translation strings (10 languages)

| Key | en | de | fr | nl |
|---|---|---|---|---|
| `taper_profile_title` | Taper profile | Taper-Profil | Profil de dégressivité | Taper-profiel |
| `taper_subtitle` | BMS acceptance ratio per 5% SoC bin | BMS-Annahmeverhältnis pro 5%-SoC-Intervall | Taux d'acceptation BMS par tranche SoC 5% | BMS-acceptatiepercentage per SoC-stap van 5% |
| `taper_charge` | Charge | Laden | Charge | Laden |
| `taper_discharge` | Discharge | Entladen | Décharge | Ontladen |
| `taper_no_observations` | No observations yet | Noch keine Beobachtungen | Aucune observation | Nog geen metingen |
| `taper_low_confidence` | Low-confidence bin (fewer than 3 observations) | Unsicherer Wert (weniger als 3 Beobachtungen) | Valeur peu fiable (moins de 3 observations) | Onzeker (minder dan 3 metingen) |

| Key | es | it | pl | pt |
|---|---|---|---|---|
| `taper_profile_title` | Perfil de atenuación | Profilo di attenuazione | Profil ograniczenia | Perfil de redução |
| `taper_subtitle` | Relación de aceptación del BMS por franja de 5% SoC | Rapporto di accettazione BMS per fascia 5% SoC | Współczynnik akceptacji BMS na 5% SoC | Taxa de aceitação do BMS por 5% de SoC |
| `taper_charge` | Carga | Ricarica | Ładowanie | Carga |
| `taper_discharge` | Descarga | Scarica | Rozładowanie | Descarga |
| `taper_no_observations` | Sin observaciones | Nessuna osservazione | Brak obserwacji | Sem observações |
| `taper_low_confidence` | Dato poco fiable (menos de 3 observaciones) | Dato poco affidabile (meno di 3 osservazioni) | Mało wiarygodne (mniej niż 3 obserwacje) | Dado pouco confiável (menos de 3 observações) |

| Key | zh-hans | ja |
|---|---|---|
| `taper_profile_title` | Taper 曲线 | テーパー プロファイル |
| `taper_subtitle` | BMS 按 5% SoC 接受比例 | BMS受入率 (5% SoC刻み) |
| `taper_charge` | 充电 | 充電 |
| `taper_discharge` | 放电 | 放電 |
| `taper_no_observations` | 暂无观测 | 観測データなし |
| `taper_low_confidence` | 低置信 (观测少于 3 次) | 信頼度低 (観測3件未満) |

## Appendix B — verification commands (per commit)

```bash
# 0. Pre-flight: confirm working tree is clean and on develop
git status --short
git branch --show-current

# 1. After each code change:
pre-commit run --all-files   # or --files <the changed files>

# 2. Run the new test for this commit:
python -m pytest <the new test path> -q

# 3. Full unit suite (fast):
python -m pytest tests/ -m "not slow" -q

# 4. E2E suite (slow — only before merging the full batch):
python -m pytest tests/e2e/test_ui.py::TestControlCard -q

# 5. Linting check (belt and braces):
python -m ruff check .
python -m mypy smart_battery/ custom_components/foxess_control/
```

## Appendix C — knowledge-tree updates on completion

After all 5 commits land, one more commit updating the tree:

- `docs/knowledge/04-design/lovelace-cards.md`: add a new D-NNN
  entry for "two-value discharge power row (D-NNN)" if the
  reviewer judges the clamp split warrants its own design
  decision. Similar judgment call for the taper card.
- `docs/knowledge/05-coverage.md`: add the new E2E tests to the
  C-020 row (operational transparency). Update test counts.
- `docs/knowledge/06-tests.md`: add a new section
  `## UX Attribute Rendering (UX #4/#5/#6/#8)`.
- `docs/ux-improvements.md`: mark the four features as
  `✓ SHIPPED (card)` alongside the existing `✓ SHIPPED (data)`
  annotations.

## Risks and mitigations (unchanged from strategic plan)

- **E2E flake**: reuse the existing staged-wait + retry pattern
  from beta.12/beta.13. Don't introduce new wait primitives.
- **Text length**: the `.detail-row-wide` + `.detail-value-wrap`
  addition handles 100+ character reason strings. Test on a 480px
  mobile viewport.
- **Translation drift**: the commit-1 unit test is load-bearing.
- **Module size (C-034)**: control card 1662 → ~1780 after all
  three additions. Well under 2000. Taper card starts fresh at
  ~250. No concern.
- **No fallback for v1.0.11**: attributes are absent, branches
  collapse to empty strings. No version guard needed.

## What is NOT in this plan

- UX #7 session history card (separate, deferred — see
  `docs/ux-improvements.md`).
- UX #1/#2/#3/#9/#10 — in the backlog, not yet planned.
- Card config-flow schemas for user customisation: the cards ship
  with defaults only; configurability is a later iteration.
- apexcharts-card variant of the taper chart: `docs/lovelace-
  examples.md` already covers this as a user template.
