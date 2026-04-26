---
project: FoxESS Control
level: 4
last_verified: 2026-04-27
traces_up: [../02-constraints.md]
traces_down: [../06-tests.md]
---
# Lovelace Card Design Decisions

Two custom cards: `foxess-overview-card` (energy flow visualisation)
and `foxess-control-card` (session management UI). Both are vanilla
Web Components using shadow DOM, loaded as static JS resources.

### D-035: Click-to-history on overview card nodes

**Decision**: Each energy flow node (solar, house, grid, battery) is
clickable, firing HA's `hass-more-info` CustomEvent with the relevant
entity ID. Sub-details (cell temperature, PV strings, grid
voltage/frequency, residual energy) fire their own more-info events
with `stopPropagation()` so the parent node click doesn't also fire.

**Context**: Users needed to inspect entity history without navigating
away from the dashboard. The overview card shows live values but
provided no drill-down path.

**Rationale**: `hass-more-info` is HA's standard mechanism for opening
entity detail dialogs. Using `bubbles: true, composed: true` crosses
shadow DOM boundaries. Sub-detail links use `stopPropagation()` to
prevent parent node click handlers from also firing.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Alternatives considered**: Opening a separate history panel — rejected
because HA's built-in more-info dialog is the expected UX pattern and
requires zero additional code.

**Traces**: C-020 (operational transparency), D-021 (data source
visibility)

### D-036: Overview card box customisation

**Decision**: Users can show/hide, reorder, relabel, and re-icon the
four energy flow boxes (solar, house, grid, battery) via YAML `boxes`
config or a visual editor. `_parseBoxes()` validates config with
fallback to `_DEFAULT_BOXES`. `_renderBox()` dispatches rendering by
box type. Responsive CSS grid adapts layout for 1, 3, or 4 boxes.

**Context**: Users with different system configurations (e.g. no solar,
no battery) wanted to hide irrelevant boxes. Power users wanted custom
labels and icons.

**Rationale**: Config-driven rendering with a normalisation layer
(`_parseBoxes`) keeps the render path clean while supporting both
string shorthand (`"solar"`) and object form
(`{type: "solar", label: "PV", icon: "mdi:weather-sunny"}`).
The editor serialises to the minimal config — omitting `boxes` entirely
when all defaults are used, preserving backward compatibility.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Alternatives considered**: Separate card variants per configuration —
rejected because it duplicates rendering logic and requires users to
switch card types when their system changes.

**Traces**: C-020 (operational transparency)

### D-037: Cold-temperature BMS charge curtailment

**Decision**: When BMS battery temperature is below 16 C, the maximum
charge power is capped at 80A x live battery voltage (~4 kW at 50 V).
Uses `min(configured_max, cold_limit)` so the system anticipates the
BMS's physical current limit. Exposed via `charge_effective_max_power_w`
sensor attribute.

**Context**: The BMS physically limits charge current at low
temperatures. Without anticipation, the system over-requests charge
power, causing the inverter to oscillate between the requested rate
and the BMS-imposed limit.

**Rationale**: The 80A threshold matches the BMS's documented maximum
charge current at low temperatures. The voltage-based calculation
(80A x V) accounts for varying battery voltage across SoC range.
The 16 C threshold is conservative — the BMS starts limiting at
lower temperatures but the exact curve is undocumented.

**Priority served**: P-003 (Meet the user's energy target)
**Trades against**: none
**Classification**: pacing

**Traces**: C-001 (no grid import — over-requesting causes oscillation)

### D-038: BMS temperature value preservation on fetch failure

**Decision**: When the web portal returns no temperature value (server
issue) or the fetch throws an exception, the last known BMS
temperature reading is carried forward instead of dropping to
"unknown". The sensor only resets to unknown on integration restart.

**Context**: The FoxESS web portal intermittently returns empty
temperature data during server-side issues while other endpoints
remain functional. Dropping to "unknown" on every transient failure
caused the overview card and charge curtailment logic to lose state.

**Rationale**: Temperature changes slowly (thermal mass of battery
pack). A stale value from minutes ago is more useful than "unknown"
for both display and charge curtailment decisions.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Traces**: C-020 (operational transparency), C-026 (error surfacing)

### D-039: Control card show_cancel option

**Decision**: A `show_cancel` YAML/editor config option (default `true`)
controls whether the cancel button appears during active charge/discharge
sessions. When `false`, the action row is empty during active sessions.
The option is stored only when `false` — omitted config means all
defaults apply, preserving backward compatibility.

**Context**: Some users embed the control card in dashboards shared with
household members who should not cancel sessions. The cancel button's
double-tap confirmation reduces accidental cancels but doesn't prevent
intentional ones.

**Rationale**: A per-card toggle is simpler than HA's per-user dashboard
permissions and doesn't require a separate "read-only" card variant.
Default `true` preserves existing behaviour.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Traces**: C-020 (operational transparency — user controls what UI shows)

### D-040: Targeted DOM updates during form display

**Decision**: When the form overlay is present in the shadow DOM,
`_render()` updates only the header (`outerHTML`), content (`innerHTML`),
and action-row (`innerHTML`) elements. The form overlay DOM is left
entirely untouched. Detection uses `existing.querySelector(".form-overlay")`
rather than the `_showForm` flag, because on the initial form-opening
render the flag is `true` but the overlay doesn't exist yet. A
`_formValues` snapshot captures live input values at the start of every
`_render()` call; an `input` event listener on the shadow root keeps
`_formValues` in sync between renders.

**Context**: The `set hass()` property fires every ~5 seconds with
WebSocket data. The previous implementation did `shadowRoot.innerHTML = ...`
on every call, destroying the entire DOM including open native time
pickers (`<input type="time">`). Users typing in the form had their
input cleared and picker popups closed mid-interaction.

**Rationale**: Shadow DOM is designed for encapsulation, but `innerHTML`
replacement discards it entirely. Targeted updates preserve the form
element identity (same DOM nodes), so focus, selection state, and native
picker popups survive. The `_formValues` snapshot catches
programmatically-set values (browser autocomplete, test automation) that
bypass the `input` event.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Alternatives considered**:
- LitElement migration — attempted and reverted. HA bundles Lit into
  hashed webpack chunks with no import map; custom cards loaded as ES
  module resources cannot `import("lit")` or resolve bare specifiers.
  Extracting Lit from HA's global scope
  (`Object.getPrototypeOf(customElements.get("ha-panel-lovelace"))`)
  gives the base class but not the `html`/`css` tagged-template
  functions, which are separate module-scope exports inaccessible from
  outside the bundle. A build step (Rollup/Webpack) to bundle Lit into
  the card JS would work but adds infrastructure complexity.
- morphdom (~3 KB DOM diffing library) — could replace the manual
  querySelector logic with a single `morphdom(container, newHTML)` call
  that preserves input elements automatically. Worth revisiting if the
  card's DOM structure grows more complex (>5 independently-updating
  regions), but overkill for the current 3-region layout.
- Saving and restoring form values after full re-render — rejected
  because native time picker popup state cannot be saved/restored
  programmatically.

**Known fragility**: `header.outerHTML = headerHtml` replaces the
header element itself, so the next querySelector must re-find it. If
the rendered headerHtml omits the `.header` class, the surgical path
silently falls through to full `innerHTML`. Formalising the containers
as persistent wrapper `<div>`s created once at `connectedCallback` and
updating only their `innerHTML` would eliminate this coupling.

**Traces**: C-020 (operational transparency — user input must not be
lost during background updates)

### D-041: Vanilla HTMLElement constraint for custom cards

**Decision**: All custom Lovelace cards remain vanilla `HTMLElement`
subclasses. No framework or library dependency beyond the Web
Components API.

**Context**: HA's frontend bundles Lit (and all other dependencies)
into content-hashed webpack chunks. There is no import map, no global
`lit` module, and no stable URL for Lit's exports. Custom cards loaded
as `type: module` Lovelace resources can resolve relative URLs and CDN
imports but cannot resolve bare specifiers like `"lit"`.

**Rationale**: Attempted LitElement migration (2026-04-23) confirmed
that:
1. `import("lit")` fails — no import map in HA's `index.html`.
2. Extracting LitElement from HA's prototype chain
   (`Object.getPrototypeOf(customElements.get("ha-panel-lovelace"))`)
   yields the class but not `html`/`css`/`nothing`, which are
   separate module exports inlined into a hashed chunk.
3. CDN imports (e.g. `https://esm.sh/lit@3`) work but introduce a
   runtime dependency on an external service, risk version mismatch
   with HA's internal Lit, and add load latency.
4. Bundling Lit into the card JS (the approach used by mushroom-cards
   and other popular HA cards) requires a build step
   (Rollup/Webpack/esbuild), which this project does not currently
   have for frontend assets.

Vanilla HTMLElement with targeted DOM updates (D-040) is the pragmatic
choice until a frontend build pipeline is introduced.

**Priority served**: P-007 (Engineering process integrity)
**Trades against**: none
**Classification**: other

**Alternatives considered**:
- Bundled Lit (Rollup) — the correct long-term solution if the card
  grows complex enough to justify build infrastructure.
- morphdom (inlined, ~3 KB) — viable intermediate step; provides DOM
  diffing without framework overhead. No build step needed if the
  minified source is vendored into the JS file.

### D-051: Transparency attributes surfaced via card rows, not tooltips

**Decision**: Four pacing-transparency data surfaces
(`discharge_deferred_reason` / `charge_deferred_reason`,
`discharge_safety_floor_w`, `discharge_grid_export_limit_w` +
`discharge_clamp_active`, `taper_profile`) are rendered as *visible
rows or a dedicated card* rather than hover-only tooltips or a single
debug panel:

- **Deferred reason** (UX #4) — a `.detail-row-wide` row on both
  charge and discharge sections, visible only while
  `*_deferred_reason` is populated.
- **Safety floor** (UX #6) — a `safety_floor` detail row on the
  discharge section, visible only when
  `discharge_safety_floor_w > 0`.  An upward-arrow icon appears
  when the paced target is *below* the floor (active clamping).
  The row is *click-expandable* (`.detail-row.has-tip`, reusing
  the progress-bar tip pattern): tapping it reveals a
  translation-aware explainer (`safety_floor_explainer` i18n key)
  with the tracked peak consumption interpolated into the text,
  so users see exactly how the floor was computed (peak × 1.5).
  Expandable form is mobile-first — the earlier hover-only
  tooltip on the arrow icon is invisible on touch devices where
  most ad-hoc dashboard checks happen.
- **Export clamp** (UX #8) — the discharge power row splits into
  inverter + export spans separated by `/`, with a `mdi:fence`
  icon and warning colour on the export side when
  `discharge_clamp_active` is true.
- **Taper profile** (UX #5) — a standalone `foxess-taper-card`
  rendering the BMS acceptance histogram per 5% SoC bin, for
  charge and discharge independently.

**Context**: The underlying attributes were already emitted by the
sensor (dc89f47 / ece71da, 2026-04-25). The question was whether
wiring them onto the UI belonged in the existing control card
(integrated) or as new tooltips / a debug panel / a separate card.

**Rationale**: C-020 (operational transparency) requires that users
determine system state from the UI alone — no log inspection. Hover
tooltips are invisible on mobile and to users who don't know to
hover.  A single debug panel would hide the explanatory context
behind a toggle.  Inline rows keep the information adjacent to the
numbers it explains; the taper profile, always-useful irrespective
of session state, earns a dedicated opt-in card rather than bloating
the control card with a permanent histogram that users without a
BMS-taper concern don't need.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Alternatives considered**:
- Hover-only tooltips — rejected: invisible on mobile (the dominant
  HA viewport for ad-hoc checks) and to users unaware they exist.
- Consolidated debug panel toggle — rejected: hides the
  explanations behind an extra click, defeating the
  glance-friendly goal.
- Taper histogram inside the control card — rejected: control card
  is already 1662 lines (near the 2000-line C-034 budget) and the
  taper profile is *always* informative, not session-scoped.

**Traces**: C-020, C-034 (module size budget), P-005, D-040
(targeted DOM updates — the new rows reuse the same
`detail-row` pattern).

**Traces**: D-040 (targeted DOM updates depend on this constraint)
