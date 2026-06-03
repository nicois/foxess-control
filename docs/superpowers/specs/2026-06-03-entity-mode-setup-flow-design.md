# Design: Entity mode reachable at first-time setup (modbus-aware config flow)

**Date:** 2026-06-03
**Status:** Approved (brainstorming) — pending implementation plan
**Priority served:** P-005 (operational transparency — the user can reach
the right mode from the UI without hidden knowledge). Resolves the
investigation's hypotheses (c) user-confusion and (d) cloud-validation
blocks modbus-only users.

## Problem

A user with `foxess_modbus` installed could not find how to configure
"entity mode" during FoxESS Control setup. Investigation
(`docs/superpowers/audit` / agent report, 2026-06-02) found:

- Entity mode is currently **only** configurable in the OPTIONS flow
  (`Configure`), never the initial config (setup) flow.
- The initial flow's first step (`async_step_user`) makes `CONF_API_KEY`
  and `CONF_DEVICE_SERIAL` **`vol.Required`** and **hard-validates them
  against the cloud API** before creating an entry — with no hint they
  are skippable. A modbus-only user with no valid API key cannot complete
  setup at all → no entry → no `Configure` button → entity mode
  unreachable. This contradicts the README's "API key optional for
  modbus users" promise.

Not a regression — this has been the shape since entity mode was
introduced (2026-04-10).

## Goal

Make entity mode a first-class, discoverable choice at setup time for
users who have `foxess_modbus`, without disrupting the cloud-only
majority. Remove the "required API key with no escape" confusion at its
source.

## Design (Approach A: modbus-aware router + mode menu)

The config flow's entry point becomes a router:

```
async_step_user (router):
  if foxess_modbus config entry detected (hass.config_entries.async_entries("foxess_modbus")):
       → async_show_menu(step_id="user", menu_options=["cloud", "entity"])
  else:
       → async_step_cloud()        # straight to the API-key form (today's behaviour)
```

- **Cloud-only users (majority):** no `foxess_modbus` → go straight to the
  API-key step exactly as today (no new menu, no extra click). Only the
  copy is clarified.
- **Modbus users:** see an explicit, labelled choice.

### Steps

1. **`async_step_cloud`** — the current API-key + serial form (the body of
   today's `async_step_user`, extracted/renamed), cloud-validated, then
   → `async_step_web_credentials` → create cloud entry. Logic unchanged.
   Copy clarified (see Copy section).

2. **`async_step_entity`** (new) — first-time entity setup:
   - On show: `detected = _detect_foxess_modbus_entities(hass)`;
     `schema = entity_mapping_schema(config_entry=None, detected)`.
     Pre-fills mapping fields (per `ENTITY_KEYS`: work-mode,
     force-charge/discharge power, min-SoC, export-limit, …) from
     name-based auto-detection; blanks where unmatched (user picks
     manually).
   - On submit → `async_step_entity_battery`.
   - **`entity_mapping_schema` must tolerate `config_entry=None`** (no
     entry exists yet at setup); it has a `_default()` helper — adapt it
     minimally to fall back to the `detected` dict when no entry.

3. **`async_step_entity_battery`** (new) — battery options
   (`battery_options_schema`: capacity, min-SoC, max power, headroom),
   then `async_create_entry(data={...entity mappings...},
   options={...battery...})` with **NO api_key**. Work-mode entity present
   ⇒ runtime treats the entry as entity mode.
   - Rationale for a dedicated battery step (not defaults-then-Configure):
     `battery_capacity_kwh` defaulting to 0 **disables pacing** (and the
     min-SoC suspend guard — see the 2026-06-02 P-001 discharge work).
     Shipping a user into entity mode with capacity unset would reproduce
     the unpaced-discharge / floor-import class of bug. The battery step
     guarantees a fully-configured, safe entry.

4. **`async_step_menu`** uses HA's native `async_show_menu` — idiomatic;
   renders as a labelled option list.

### unique_id

The cloud path sets `unique_id = device_serial`. The entity path has no
serial. Use a stable id derived from the foxess_modbus config entry id
(or a fixed sentinel if multiple are unsupported) so
`_abort_if_unique_id_configured` prevents duplicate entity-mode entries.
**Implementation must pin this down explicitly** — it is the one
non-obvious wiring detail.

## Reuse (low refactor risk)

The entity-mapping building blocks are ALREADY standalone in
`custom_components/foxess_control/smart_battery/config_flow_base.py`:
`entity_mapping_schema`, `detect_entities`, `build_entity_map`,
`battery_options_schema`, `ENTITY_KEYS`. The options flow's
`async_step_modbus` calls them. The config flow reuses the same functions
— this is wiring a new branch, not extracting shared logic.

## Copy / localisation

New/changed strings (in `strings.json` AND mirrored into
`translations/en.json` + all 9 non-EN locales — MANDATORY; the
locale-parity regression test fails CI otherwise):

- **Menu step** — title "How is your inverter connected?"; options:
  - `cloud`: "FoxESS Cloud (API key)"
  - `entity`: "Use my foxess_modbus inverter (no API key needed)"
- **Cloud step** — description gains a safety-net line for the no-menu
  (cloud-only) path: e.g. "Enter your FoxESS Cloud API credentials. If you
  use the foxess_modbus integration instead, you can run without a cloud
  API key — see the docs."
- **Entity-mapping step** — description: these map to your foxess_modbus
  entities; auto-filled where detected, adjust as needed.
- **Battery-options step** — note that battery capacity is needed for
  discharge pacing.

## Testing (config-flow handler tests; no container needed)

- **Routing (core guard):** `async_step_user` with a `foxess_modbus`
  entry → shows the menu; with none → goes straight to the cloud
  (API-key) step.
- **Entity branch:** menu→entity → entity-mapping + battery steps create
  an entry with NO `api_key`, work-mode entity set (entity mode active),
  and `battery_capacity_kwh > 0`.
- **Cloud branch unchanged:** menu→cloud and the no-modbus direct path
  still validate credentials and create a cloud entry; existing
  config-flow tests keep passing.
- **Auto-detection:** entity branch pre-fills mappings from foxess_modbus
  entity names (reuses `detect_entities`).
- **unique_id:** entity entry gets a stable unique_id;
  `_abort_if_unique_id_configured` prevents duplicates.
- **Locale parity:** new keys present in `strings.json`,
  `translations/en.json`, and all 9 non-EN locales.

## Knowledge tree

- **New D-060**: early `foxess_modbus` detection → mode-choice menu in the
  config flow; entity mode reachable at first-time setup (not options
  only). Cites P-005, D-022 (entity-mode design). Classification: other
  (UX/setup).
- **README**: the setup section (currently "entity mode is shown in the
  options flow", `README.md:112`) updated to "detected at setup with a
  mode choice; options flow still available to change later."
- Note the investigation's hypothesis (d) (cloud-validation blocking
  modbus-only users) is RESOLVED by the entity branch.

## Out of scope (noted follow-ups)

- **Brittle name-based detection (investigation hypothesis 2b):**
  auto-detection still matches `entity.original_name` exactly. Not changed
  here — it degrades gracefully (unmatched field shown blank, user picks
  manually). Separate hardening if warranted.
- Options flow's existing `async_step_modbus` is unchanged (it remains the
  way to *edit* entity mode after setup).
