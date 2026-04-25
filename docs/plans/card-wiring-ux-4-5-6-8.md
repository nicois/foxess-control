# Plan: wire UX #4/#5/#6/#8 attributes into Lovelace cards

Written 2026-04-25 after inspecting the card source
(`custom_components/foxess_control/www/foxess-control-card.js`
1662 lines, and `foxess-overview-card.js` 852 lines).

The data surfaces for UX #4 / #5 / #6 / #8 shipped as attributes
on `sensor.foxess_smart_operations` in commits `dc89f47` +
`ece71da`. `docs/lovelace-examples.md` shows users how to consume
them with a markdown card. This plan covers native card rendering:
the control card and overview card reading the same attributes
and rendering them in a consistent, translation-aware way.

## Scope of this plan

- **In scope**: control-card rendering for #4 / #6 / #8 (they're
  all session-state details — belong on the control card next to
  the existing `slack` / `power` / `min_soc` rows). Translation
  key coverage across 10 languages matching the existing pattern.
- **Partially in scope**: a **lightweight taper-profile sub-card**
  for #5. The existing cards don't have a natural home for a
  histogram — keeping it as a separate optional card avoids
  bloating the control card.
- **Out of scope**: apexcharts-style taper chart (users
  self-serve via `docs/lovelace-examples.md`); outcome-history
  card (UX #7, still deferred).

## Card inventory (read before editing)

| Card | File | Lines | Purpose |
|---|---|---|---|
| Control | `foxess-control-card.js` | 1662 | Shows the active session — phase, power, window, slack, targets. This is where UX #4 / #6 / #8 belong. |
| Overview | `foxess-overview-card.js` | 852 | Dashboard at-a-glance energy flows. Does NOT render session state today — intentionally out of scope. |
| Forecast | `foxess-forecast-card.js` | 230 | Plots SoC forecast. Orthogonal; unchanged. |
| History | `foxess-history-card.js` | 291 | Event timeline (UX #2 territory). Unchanged by this plan. |

## Per-feature plan

### UX #4 — "Why deferred?" inline explanation (control card)

**Insertion point**: after the existing `slack` row in both
`_renderCharge()` (line 898) and `_renderDischarge()` (line 949).
Conditional on the attribute being present (it's only populated
during the deferred phase, so no explicit phase-check needed in
the card JS).

**Rendering**:

```js
${a.discharge_deferred_reason ? `
<div class="detail-row detail-row-wide">
  <span class="detail-label">${this._t("deferred_reason")}</span>
  <span class="detail-value detail-value-wrap">${escapeHtml(a.discharge_deferred_reason)}</span>
</div>` : ""}
```

Note: the reason text is user-facing English, not from a
translation table. The attribute comes from the Python layer
(`_explain_discharge_deferral()`). If we need translated reasons
later, the Python layer would need to return a structured
`{key, args}` and the card would do the translation — a bigger
change, out of scope for this card-wiring pass.

**New translation key**: `deferred_reason` (label only) — across
the 10 existing languages. The body text stays English until a
structured-reason refactor.

**CSS addition**: `.detail-row-wide` / `.detail-value-wrap` —
allow wrapping for the longer sentence ("waiting for window to
open" is short, "holding self-use so 1 kWh feed-in target is met
in one shorter burst..." is not).

**Test**: E2E test asserting `discharge_deferred_reason` text
appears in the card DOM when the attribute is set, and is absent
when not. Playwright test pattern (mirror the existing
`test_time_picker_stays_open_during_rerender` shape).

### UX #6 — safety-floor indicator (control card, discharge only)

**Insertion point**: `_renderDischarge()` — new block after the
existing `power` row (line 954), before `min_soc`.

**Rendering**:

```js
${a.discharge_safety_floor_w > 0 ? `
<div class="detail-row">
  <span class="detail-label">${this._t("safety_floor")}</span>
  <span class="detail-value">
    ${this._formatPower(a.discharge_safety_floor_w)}
    ${pacedBelowFloor ? `<span class="floor-active-hint" title="${this._t("floor_clamping_tooltip")}">&uarr;</span>` : ""}
  </span>
</div>` : ""}
```

where `pacedBelowFloor = a.discharge_paced_target_w != null &&
a.discharge_paced_target_w < a.discharge_safety_floor_w`.

**Rationale for the hint arrow**: users only need to see the
floor when it's *doing work*. Showing the number always is
noise; showing an arrow when the floor is actively clamping
paced power upward makes the invariant visible.

**New translation keys**: `safety_floor`, `floor_clamping_tooltip`
— 10 languages. Examples:

- English: `"safety floor"` / `"Floor is raising paced power to prevent grid import (C-001)"`
- German: `"Sicherheitsmindestleistung"` / `"Die Grundleistung wird angehoben, um Netzeinspeisung zu verhindern (C-001)"`

**Test**: unit test on a DOM snapshot asserting the floor row
appears only when `discharge_safety_floor_w > 0`, and the hint
arrow appears only when paced target is below the floor.

### UX #8 — export-clamp acknowledgement (control card, discharge only)

**Insertion point**: `_renderDischarge()` — replace the existing
single-`power` row (line 954) with a two-value split when the
export limit is configured:

```js
${a.discharge_grid_export_limit_w ? `
<div class="detail-row">
  <span class="detail-label">${this._t("power")}</span>
  <span class="detail-value">
    <span class="inverter-power">${this._formatPower(power)}</span>
    <span class="clamp-sep">→</span>
    <span class="export-power ${a.discharge_clamp_active ? "clamp-active" : ""}">
      ${this._formatPower(a.discharge_grid_export_limit_w)}
      ${a.discharge_clamp_active ? `<ha-icon icon="mdi:fence" title="${this._t("clamp_active_tooltip")}"></ha-icon>` : ""}
    </span>
  </span>
</div>` : /* existing single-power row */ ""}
```

**Rationale**: the existing single-power row is accurate for
sites without an export limit; only add the split when the
attribute is present. The fence icon is iconic shorthand for
"hardware clamp". Tooltip text explains.

**New translation keys**: `clamp_active_tooltip` — 10 languages.
Example: `"Hardware export limiter is capping grid export at
this value (DNO / inverter firmware setting)"`.

**CSS additions**: `.inverter-power` (standard), `.export-power`
(slightly smaller, muted), `.export-power.clamp-active` (full
colour indicating the clamp is doing work), `.clamp-sep` (gray).

**Test**: existing
`tests/e2e/test_ui.py::TestControlCard` extended — assert the
two-value split renders when the attribute is present, and the
`clamp-active` class is applied only when
`discharge_clamp_active: true`.

### UX #5 — taper profile (new standalone card)

**Why a new card, not a new section**: the taper profile is
always-useful, not session-state. Putting it inside the control
card bloats it (the control card is already 1662 lines) and hides
the chart when no session is active. A standalone card that users
can add to their dashboard independently is cleaner.

**New file**: `www/foxess-taper-card.js` (~250 lines, mirroring
the structure of `foxess-forecast-card.js` which is the simplest
existing card).

**Rendering** (ASCII sketch):

```
┌─ FoxESS taper profile ──────────────────────┐
│ BMS acceptance ratio per 5% SoC bin         │
│                                             │
│ Charge:                                     │
│  50% ████████████████  100% (3)             │
│  65% ████████████████▏ 100% (7)             │
│  80% █████████▏        58% (5)              │
│  85% ██▎              18% (2)  ·            │
│  90% ▏                 5% (1)  ·            │
│                                             │
│ Discharge:                                   │
│  15% ████▊             30% (4)              │
│  20% ████████████████ 100% (8)              │
│                                             │
│ · = low-confidence bin (fewer than 3 obs)   │
└─────────────────────────────────────────────┘
```

Uses inline `<div>` bars with `width:` proportional to ratio —
no external dependencies (apexcharts-card is recommended in
`docs/lovelace-examples.md` for richer users; the built-in
version should always work).

**Card registration**: like the other cards, auto-register via
`sensor.py` frontend resource inclusion. Card config schema:
only `entity` (default `sensor.foxess_smart_operations`).

**Translation keys**: `taper_profile_title`, `taper_charge`,
`taper_discharge`, `taper_no_observations`, `taper_low_confidence`.
10 languages.

**Test**: new `tests/e2e/test_taper_card.py` mirroring
`test_overview_card.py`. Assertions: card renders, bars are
proportional, discharge section renders separately from charge,
low-confidence marker appears for bins with `count < 3`, empty
state ("no observations yet") when both histograms are empty.

## Translation-key additions (summary)

10 new keys × 10 languages = 100 new i18n entries. Follow the
existing translation-table pattern in `foxess-control-card.js`:

| Key | English | Use |
|---|---|---|
| `deferred_reason` | "reason" (short, in label column) | UX #4 row label |
| `safety_floor` | "safety floor" | UX #6 row label |
| `floor_clamping_tooltip` | "Raising paced power to prevent grid import (C-001)" | UX #6 hover |
| `clamp_active_tooltip` | "Hardware export limiter is capping grid export at this value" | UX #8 icon hover |
| `taper_profile_title` | "Taper profile" | UX #5 card title |
| `taper_charge` | "Charge" | UX #5 section |
| `taper_discharge` | "Discharge" | UX #5 section |
| `taper_no_observations` | "No observations yet" | UX #5 empty state |
| `taper_low_confidence` | "Low-confidence bin" | UX #5 marker tooltip |
| `taper_subtitle` | "BMS acceptance ratio per 5% SoC bin" | UX #5 card subtitle |

## Test plan

| Test | Level | Target |
|---|---|---|
| `test_deferred_reason_renders_when_attribute_present` | E2E Playwright | Control card during deferred discharge: reason row text contains expected substring |
| `test_deferred_reason_hidden_when_attribute_absent` | E2E Playwright | Control card during active discharge: reason row absent |
| `test_safety_floor_row_appears_when_non_zero` | E2E Playwright | Control card during discharge with peak > 0: row present |
| `test_floor_clamping_arrow_visible_when_paced_below_floor` | E2E Playwright | Arrow element + tooltip present |
| `test_export_clamp_split_power_row_renders` | E2E Playwright | Power row shows two values joined by arrow when `grid_export_limit` configured |
| `test_clamp_active_class_toggles_on_clamp_active_attribute` | E2E Playwright | CSS class changes based on boolean attribute |
| `test_taper_card_renders_charge_bars` | E2E Playwright | New taper card with a seeded profile: charge bars visible, width proportional to ratio |
| `test_taper_card_empty_state` | E2E Playwright | Taper card with no profile: empty-state message shown |
| `test_translation_keys_present_for_all_locales` | Unit | Every new key exists in every language's translation table |

Follow C-029 (E2E for HA-dependent behaviour): card rendering
tests go in `tests/e2e/test_ui.py` because shadow DOM / HA
frontend integration is what we're asserting.

## Implementation sequencing

1. **Translation keys first** (pure data, no risk): add all 10
   keys × 10 languages to `foxess-control-card.js` + create
   `foxess-taper-card.js` scaffold with its own table.
   Regression-safe — no rendering change yet.
2. **UX #8 (clamp split)**: smallest JS change. Add
   conditional two-value rendering in `_renderDischarge()`, CSS
   additions. E2E test.
3. **UX #6 (safety floor)**: parallel change in
   `_renderDischarge()`. E2E test.
4. **UX #4 (deferred reason)**: parallel change in
   `_renderCharge()` and `_renderDischarge()`. Two E2E tests
   (one each side).
5. **UX #5 (taper card)**: new file, new sensor.py registration,
   new E2E test. Largest — do last.

Each step shippable independently; each has its own commit + test.

## Risks and mitigations

- **Existing E2E flakes** (per beta.12/beta.13 page-fixture
  fixes): use the same staged-wait + retry pattern that the
  existing tests use. Don't introduce new wait primitives.
- **Text length on narrow cards**: the `deferred_reason` string
  can be 100+ characters. The `.detail-row-wide` +
  `.detail-value-wrap` CSS addition handles this without layout
  thrashing. Test on a 480px-wide mobile viewport.
- **Translation drift**: the existing 10-language coverage was
  added one feature at a time. Each new key needs all 10
  languages or the card falls back to the key name. The unit
  test `test_translation_keys_present_for_all_locales` is
  load-bearing here.
- **Module size budget** (C-034, 2000 lines): the control card
  is at 1662 lines. Adding ~80 lines for #4+#6+#8 brings it to
  ~1740 — well under budget. Taper card starts fresh at ~250
  lines. No concern.
- **D-NNN**: these changes are close enough to existing D-039
  (show_cancel) / D-040 (targeted DOM updates) that they may
  not warrant a dedicated D-NNN. Reviewer call at
  implementation time.

## Estimated effort

- Steps 1–4 (control-card wiring): 2–3 hours + E2E cycle time
- Step 5 (taper card): 3–4 hours + E2E cycle time
- Total: ~1 day's focused work

## Not in this plan (deliberate)

- **Mobile-specific card**: the existing card already handles
  viewport changes. A phone-optimised variant would double
  maintenance burden.
- **Configurable layout**: users don't need to toggle the new
  rows. They're all either conditionally-present (attributes
  drive them) or part of the existing information density.
- **Graceful degradation for v1.0.11 and earlier users**: the
  attributes don't exist there, so the card branches simply
  don't render. No version gate needed.
