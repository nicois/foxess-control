---
project: FoxESS Control
level: 4
last_verified: 2026-05-03
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

### D-052: "No solar yet seen" detection with Gen Load fallback

**Decision**: `FoxESSDataCoordinator` tracks a single boolean
`solar_seen` flag initialised `False` on every process start. The
first `pvPower > 0` observation on either the REST update path
(`_async_update_data`) or the WS injection path
(`inject_realtime_data`) flips the flag to `True`; it remains `True`
for the lifetime of the process and cannot revert. Zero, `None`,
negative, and missing `pvPower` readings are defensive no-ops (the
helper `_observe_pv_power` early-returns once the flag is set, so
subsequent ticks pay no further cost). The flag is surfaced on
every coordinator payload as `data["_solar_seen"]` and lifted onto
the `pv_power` sensor via `extra_state_attributes["solar_seen"]` —
no other sensor carries it (no attribute pollution), no new HA
entity is introduced, and no persistence crosses restarts per the
feature spec. `foxess-overview-card` reads the attribute: while it
is `False`, `_renderBox("solar")` substitutes label `"Gen Load"`
(i18n key `gen_load`, translated across all 10 locales), value
`loadsPower`, and icon `⚡` in place of the usual sun. Once the
flag flips, the box reverts to the canonical Solar rendering.
User-supplied `box.label` / `box.icon` overrides still win; a
missing attribute defaults to `True` in the card so legacy installs
that pre-date this change see no visible difference.

**Context**: AC-coupled FoxESS models (AC1 series) have no MPPT
inputs; battery-only hybrid installs may have the PV strings
physically disconnected. On all these configurations the overview
card showed a permanently-stuck `0.0 kW` solar reading with a sun
icon, which is pure noise — a user glancing at the dashboard
couldn't tell whether their panels had failed or whether the
inverter genuinely had no PV capability.

**Rationale**: The cheapest signal that distinguishes "panels
attached but dark" from "no panels" is "has the inverter ever
reported positive PV power since HA started?". A fresh start in
daylight will flip the flag within one poll cycle on any working
PV install; an AC-coupled or unwired install never flips. Storing
process-lifetime runtime state (not persisted) is deliberate: on
restart we optimistically re-check rather than locking in a
historical mode that may have become wrong (e.g. new panels
commissioned). Making the flag sticky WITHIN a process prevents
night-time / cloud flicker from re-hiding the solar display on
normal installs. Placing it on the brand-specific coordinator
rather than `smart_battery/` respects C-039 (no brand leakage) —
the helper is trivially liftable when a second brand needs the
same behaviour.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Alternatives considered**:
- **Persisted flag** (survives HA restarts) — rejected: locks in
  historical state against today's hardware; a newly commissioned
  PV install would stay in Gen Load mode forever without a manual
  reset.
- **New HA sensor entity** for solar-seen — rejected: adds an entity
  with a single boolean value the card is the only consumer of, and
  pollutes the entity registry. Surfacing as an `extra_state_attribute`
  on the existing `pv_power` sensor gives the card exactly what it
  needs without a registry entry.
- **Query `pv1Volt`/`pv2Volt` from Open API** to definitively detect
  unwired MPPT strings — rejected as premature: the daylight-power
  signal is sufficient for the UX problem, adds no new API calls, and
  doesn't require a separate sunny-noon sampling heuristic. The
  voltage query remains available if a future brand needs hardware-
  definitive detection.
- **AC-coupled model list** — rejected: hard-coded model maps rot as
  FoxESS ships variants, and brand-portability (P-007) is easier to
  maintain with a behavioural detector.

**Traces**: C-020 (replace stuck-zero noise with actionable state),
C-026 (meaningful state surfaced via sensor attribute rather than
log inspection), tests in `tests/test_solar_seen.py`
(TestCoordinatorSolarSeenFlag, TestPvPowerSensorSolarSeenAttribute,
TestOverviewCardGenLoadMode).

### D-053: Locale-safe operations_entity via `_resolve(key)`

**Decision**: Both `foxess-control-card` and `foxess-taper-card`
resolve the smart_operations sensor entity ID through the shared
`_resolve(key)` helper used by the forecast and history cards.
Resolution order is explicit: (1) user-supplied
`operations_entity:` YAML config if present; (2)
`_entityMap["smart_operations"]` returned by the
`foxess_control/entity_map` WS command; (3) the English default
`sensor.foxess_smart_operations` as a last-resort fallback. The
taper card previously didn't fetch the entity map at all — the
fix wires the WS subscription into its `hass.connection.subscribeMessage`
call so the map is available before first render.

**Context**: A user running HA in German saw "Keine aktiven
Vorgänge" on the control card despite `charge_active=true` and
`charge_phase=scheduled` on the backing sensor. HA derives entity
IDs from the *translated* friendly name when the entity is first
created — in DE the real entity is
`sensor.foxess_intelligente_steuerung`, in FR
`sensor.foxess_operations_intelligentes`, and so on. The control
and taper cards had a hard-coded English default for
`operations_entity`, so on every non-English install they read an
entity that didn't exist and rendered the "idle" placeholder.

**Rationale**: The integration already publishes an authoritative
`entity_map` via a WS command; two of the four custom cards
(forecast, history) already consulted it via `_resolve(key)`. The
remaining two cards were inconsistently hard-coded. Centralising
resolution through the same helper (a) fixes the bug everywhere it
manifests, (b) preserves explicit YAML overrides for users with
pinned dashboards, and (c) structurally prevents regression into
direct `this._config.operations_entity` reads — enforced by a
source-level test that greps the card JS for the disallowed
pattern.

**Priority served**: P-005 (Operational transparency)
**Trades against**: none
**Classification**: other

**Alternatives considered**:
- **Hard-code every supported locale** — rejected: rots with every
  FoxESS rename, and new HA locales silently break. The entity_map
  is the authoritative runtime source; reading it is O(1) per
  render.
- **Ask the user to pin `operations_entity` in YAML** — rejected:
  non-English users were the ones hitting the bug, and asking them
  to debug an entity-registry mismatch before the card works is a
  direct C-020 violation (users can't determine system state from
  the UI alone).
- **Compute entity ID from `translation_key`** — rejected: HA exposes
  translation keys but the transform from key to entity ID happens
  inside the registry using the friendly-name slugification rules at
  *entity creation time*, which a card can't replay deterministically.

**Traces**: C-020 (operational transparency across locales),
tests in `tests/test_card_entity_resolution.py` — four cases fail
against pre-fix card code (DE/FR control card, DE taper card,
taper map subscription), three passed throughout (backwards-compat
YAML override, graceful degradation when WS command fails, source-
level guard against direct `this._config.operations_entity`
reads).
