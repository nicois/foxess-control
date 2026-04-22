---
project: FoxESS Control
level: 4
last_verified: 2026-04-21
traces_up: [../02-constraints.md]
traces_down: [../06-tests.md]
---
# Lovelace Card Design Decisions

Two custom cards: `foxess-overview-card` (energy flow visualisation,
vanilla HTMLElement) and `foxess-control-card` (session management UI,
LitElement). Both use shadow DOM, loaded as static JS resources.

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

**Traces**: C-020 (operational transparency — user controls what UI shows)

### D-040: LitElement for control card

**Decision**: `foxess-control-card` uses LitElement (extracted from HA's
global scope at runtime) instead of vanilla HTMLElement. The `render()`
method returns `html` tagged templates; Lit's DOM diffing preserves
existing elements across re-renders. Form inputs use `.value=` property
bindings with `@input` handlers writing to a non-reactive `_formValues`
map.

**Context**: The `set hass()` property fires every ~5 seconds with
WebSocket data. The previous vanilla implementation did
`shadowRoot.innerHTML = ...` on every call, destroying the entire DOM
including open native time pickers. Users typing in the form had their
input cleared and picker popups closed mid-interaction. Targeted DOM
update patches and value snapshot/restore workarounds were brittle and
grew in complexity.

**Rationale**: LitElement's DOM diffing eliminates the entire class of
form value preservation problems — elements are reused, not recreated,
so focus, selection state, and native picker popups survive
automatically. HA already ships LitElement, so there is no new
dependency. Extracting `LitBase` from HA's global scope
(`Object.getPrototypeOf(customElements.get("ha-panel-lovelace"))`)
avoids bundling a separate Lit copy. `_formValues` is deliberately
non-reactive (plain object, not a reactive property) so form input
changes don't trigger re-renders.

**Alternatives considered**:
- Targeted DOM updates (D-040 prior revision) — worked but required
  fragile querySelector-based patching and couldn't handle all edge
  cases (e.g. picker popup state).
- Value snapshot/restore before innerHTML — couldn't restore native
  picker popup state programmatically.
- Bundling Lit as a dependency — rejected because HA already provides
  it and bundling would increase card size and create version conflicts.

**Traces**: C-020 (operational transparency — user input must not be
lost during background updates)
