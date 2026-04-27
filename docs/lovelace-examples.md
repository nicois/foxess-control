---
# This file contains Home Assistant Jinja2 template examples (see
# the ``content: |`` blocks below). Jekyll's Liquid preprocessor
# does not understand Jinja's ``{% set %}`` / ``{% if %}`` / ``{% for %}``
# tags and errors out on them, which breaks the GitHub Pages build.
# ``render_with_liquid: false`` tells Jekyll to skip its Liquid
# pass for this one file — markdown rendering still happens
# normally, so the page still publishes, just without Liquid
# interpretation of the HA template examples (which is what we
# want: users should see the literal HA syntax to copy/paste).
render_with_liquid: false
---
# Lovelace templates for FoxESS Control attributes

These templates consume the 2026-04-25 data-surface attributes
shipped for UX #4 / #5 / #6 / #8 (see `docs/ux-improvements.md`).
They are drop-in starting points — copy, adapt, and wire your own
colours / layouts on top.

All examples below target a single entity, `sensor.foxess_smart_operations`,
which carries the pacing-transparency attributes on its
`extra_state_attributes`.

> **⚠️ Locale note — the entity_id may differ on non-English installs**
>
> Home Assistant derives entity_ids from the *translated* friendly name
> at entity-creation time. On a German install this sensor is
> `sensor.foxess_intelligente_steuerung`; on a French install it's
> `sensor.foxess_operations_intelligentes`; and so on. If the templates
> below show "unknown" or your card says "no data", open
> **Developer Tools → States**, filter by `foxess`, and substitute your
> actual entity_id in each `state_attr(...)` / `states.sensor.foxess_...`
> reference. The native FoxESS cards (`foxess-control-card`,
> `foxess-taper-card`, etc.) auto-discover the entity via the
> `foxess_control/entity_map` WS command and do not need this
> substitution — these markdown-card templates do because HA's
> templating engine has no concept of "role-based entity lookup".

## All-in-one reasoning card (no external dependencies)

This markdown card exercises every new attribute with built-in
conditional rendering — sections disappear cleanly when the
attribute is absent, so it works in every session phase (idle,
charge, discharge, deferred, suspended) without error.

```yaml
type: markdown
title: FoxESS session reasoning
content: |
  {% set a = state_attr('sensor.foxess_smart_operations', 'discharge_phase')
             or state_attr('sensor.foxess_smart_operations', 'charge_phase')
             or 'idle' %}
  {% set attrs = states.sensor.foxess_smart_operations.attributes %}

  **Phase:** {{ a }}

  {# UX #4 — why is the session deferred? #}
  {% if attrs.discharge_deferred_reason %}
  🕒 *Discharge:* {{ attrs.discharge_deferred_reason }}
  {% endif %}
  {% if attrs.charge_deferred_reason %}
  🕒 *Charge:* {{ attrs.charge_deferred_reason }}
  {% endif %}

  {# UX #6 — safety floor during discharge #}
  {% if attrs.discharge_active and attrs.discharge_peak_consumption_kw is defined %}
  **C-001 safety floor**
  - Peak load tracked: {{ attrs.discharge_peak_consumption_kw }} kW
  - Floor (peak × 1.5): {{ attrs.discharge_safety_floor_w }} W
  - Paced target (unclamped): {{ attrs.discharge_paced_target_w }} W
  - Actually discharging: {{ attrs.discharge_power_w }} W
  {% if attrs.discharge_paced_target_w and attrs.discharge_paced_target_w < attrs.discharge_safety_floor_w %}
  ⚠️ *Floor is clamping paced power upward — below floor would risk grid import*
  {% endif %}
  {% endif %}

  {# UX #8 — hardware export clamp acknowledgement #}
  {% if attrs.discharge_grid_export_limit_w is defined %}
  **Export clamp**
  - Inverter discharge: {{ attrs.discharge_power_w }} W
  - Grid export limit: {{ attrs.discharge_grid_export_limit_w }} W
  - Clamp active: {{ '✓ limiting export' if attrs.discharge_clamp_active else '— within limit' }}
  {% endif %}

  {# UX #5 — taper profile summary (top 5 bins each direction) #}
  {% if attrs.taper_profile %}
  **Taper profile (BMS acceptance ratios)**
  {% set chg = attrs.taper_profile.charge %}
  {% if chg|length > 0 %}
  *Charge*: {% for b in chg[-5:] %}{{ b.soc }}%→{{ (b.ratio*100)|int }}%{% if b.count < 5 %}·{% endif %}{% if not loop.last %} | {% endif %}{% endfor %}
  {% else %}
  *Charge*: no observations yet
  {% endif %}
  {% set dch = attrs.taper_profile.discharge %}
  {% if dch|length > 0 %}
  *Discharge*: {% for b in dch[:5] %}{{ b.soc }}%→{{ (b.ratio*100)|int }}%{% if b.count < 5 %}·{% endif %}{% if not loop.last %} | {% endif %}{% endfor %}
  {% else %}
  *Discharge*: no observations yet
  {% endif %}
  *(lower ratio = BMS accepting less than requested; `·` marks low-count bins)*
  {% endif %}
```

### What each section does

- **UX #4** — the deferred-reason lines render only when the
  session is in the `deferred` phase (the attribute is absent
  otherwise, so the `if` branch collapses cleanly). Present-when-
  relevant, absent-when-not.
- **UX #6** — the safety-floor block only shows during an active
  discharge. It compares peak → floor → paced target → actual
  side by side so users can see the causal chain. The ⚠️ line
  fires only when the floor is actively *raising* paced power
  above what the energy math alone would suggest — which is the
  whole point of making C-001 visible.
- **UX #8** — shows clamp status as a boolean outcome ("✓ limiting"
  vs "— within limit"). The `is defined` guard means sites with
  `grid_export_limit=0` (or unconfigured) see nothing — no false
  alarms for users who don't have an export cap.
- **UX #5** — renders the last 5 charge bins + first 5 discharge
  bins as a compact string (`81%→40% | 85%→25% | 90%→10% …`).
  The `·` dot marks low-observation bins where users should be
  sceptical of the ratio.

## Taper profile chart (apexcharts-card)

For a richer view of UX #5, with [apexcharts-card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
header:
  title: BMS taper — charge acceptance
graph_span: 1h
series:
  - entity: sensor.foxess_smart_operations
    name: Acceptance ratio
    type: area
    data_generator: |
      return (entity.attributes.taper_profile?.charge || []).map(
        b => [b.soc * 3600000, b.ratio]
      );
    curve: stepline
```

`graph_span: 1h` is a hack — apexcharts plots against time, and
we're using SoC as a fake timestamp
(`soc * 3600_000 ms` so each 1% SoC becomes 1 second). The
X-axis labels come out meaningless ("03:33 AM" = 82% SoC) but
the *shape* of the curve is correct. A cleaner solution would be
apexcharts-card's `xaxis` override, or ditching apexcharts for
chart.js, which natively handles arbitrary X values.

## Caveats

1. **Attributes refresh on coordinator update** — every 5 minutes
   by default in cloud mode, 60 s in entity mode. A
   deferred-reason that changes mid-tick won't appear on the
   card until the next poll.
2. **`state_attr` returns `None` for missing attributes**, so
   every `if attrs.X is defined` test is the safe form.
   `attrs.X` alone would Jinja-fail if the attribute is absent.
3. **The safety-floor ⚠️ logic above is slightly imprecise**:
   `discharge_paced_target_w` can legitimately be below the
   floor — that's what the floor is there to lift. The warning
   correctly triggers whenever the floor is *actively lifting*
   paced power, which is the informative case.
4. **`discharge_power_w` is actual, not target**: if the hardware
   hasn't yet accepted the new value, `discharge_power_w` lags
   `discharge_paced_target_w` by one tick. Within one poll
   cycle. Not worth a separate warning.

## Related

- `README.md` — canonical sensor + attribute reference
- `docs/ux-improvements.md` — the backlog these attributes
  support, including the Lovelace-rendering follow-up items
  still outstanding for each.
- `docs/knowledge/04-design/lovelace-cards.md` — design
  decisions for the shipped Lovelace cards.
