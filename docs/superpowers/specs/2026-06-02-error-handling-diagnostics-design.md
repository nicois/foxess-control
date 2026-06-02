# Design: Structured error capture for self-sufficient diagnostics

**Date:** 2026-06-02
**Status:** Approved (brainstorming) — pending implementation plan
**Priority served:** P-005 (operational transparency); strengthens C-026
(proactive error surfacing)
**Motivating report:** GH issue #8 — "Battery ID discovery failure"
(`WSServerHandshakeError: 200, message='Invalid response status'` on the
`wsmaitian` WS). The maintainer could not act without asking the reporter
for facts the integration already knows (cloud region/host, ws_mode,
whether it works during an active session, the exception type).

## Goal

When a user hits an error and reports it, the information a maintainer
would otherwise have to ask for should **already be in hand** — reducing
back-and-forth. Two channels:

1. **Better log/error messages** — the pasted log line alone names the
   exception type, what was attempted, and the likely cause.
2. **Richer HA Diagnostics download** — a single attachable file
   containing recent errors + the environment facts maintainers
   repeatedly request.

Out of scope (deliberately): expanding HA Repair issues (already used
where they fit; not the right surface for transient/diagnostic detail),
and a stable error-*code* taxonomy (premature — may graduate later).

## Current state (surveyed 2026-06-02)

- **65 broad `except Exception` / bare `except`** clauses in production
  code (`custom_components/foxess_control/` + canonical `smart_battery/`).
  Dominant logging pattern: `_LOGGER.warning("X failed: %s", exc)` — no
  exception-type discrimination, no actionable hint. The issue-#8 line is
  textbook.
- **`diagnostics.py`** already exports (redacted): entry config,
  coordinator data, session states, `error_state`, WS info, taper profile.
  Missing: recent errors, and environment facts like the resolved cloud
  host/region (the exact issue-#8 gap).
- **Debug-log sensors** (`sensor.foxess_debug_protokoll` / `_log`) capture
  `{t, level, msg, session}` but are **opt-in** — absent unless the user
  enabled `input_boolean.foxess_control_debug_log` before the failure.
- **`FoxESSControlData`** is a dataclass (`domain_data.py`) using
  `field(default_factory=list)` — a clean home for an always-on ring
  buffer.
- **`SessionContextFilter`** (`smart_battery/logging.py`) already attaches
  structured session context to log records.

## Approach (B): single capture point feeding both surfaces

A thin `record_error(...)` helper that BOTH logs (with type +
what-attempted + hint) AND appends to an always-on ring buffer.
`diagnostics.py` exports that buffer plus a new environment section. One
capture, two surfaces, no drift. Incremental adoption — highest-value
sites first, not a 65-site big-bang.

Rejected alternatives:
- **A — two independent tracks** (separate log fixes + diagnostics buffer):
  overlapping info captured by different code → drift risk.
- **C — full error-code taxonomy now**: too much upfront cataloguing
  before the audit shows the real distribution of error types. Codes can
  graduate later if warranted.

## Phase 0 — Audit (drives everything)

Classify all 65 broad excepts (file:line → bucket → recommended action),
committed as a table in/with this spec:

| Bucket | Meaning | Action |
|---|---|---|
| transient-retryable | single API timeout, brief DNS blip; self-heals next tick | keep broad-ish; log at debug/info, not warning |
| config-or-environment | wrong region/host, expired token, missing entity, unreachable endpoint (issue #8) | **narrow the except; `record_error` with actionable hint + context** |
| genuine-bug | shouldn't happen; our defect | `record_error` at `error` severity; never silently swallow |
| intentional-suppression | best-effort cleanup where failure truly doesn't matter | document why with a comment; leave as-is |

The distribution decides how many sites adopt the helper in Phase 1
(config-or-environment + genuine-bug first). `smart_battery/` excepts get
the brand-agnostic treatment (C-039); brand-layer excepts use the brand
logger directly.

## Phase 1 — `record_error` helper + ring buffer + diagnostics

### `record_error` (capture point)

```python
record_error(
    logger,                    # module _LOGGER
    category="ws_discovery",   # greppable category string
    attempted="battery ID discovery via wsmaitian WS",
    exc=exc,
    hint="server returned HTTP 200 not 101 — possible regional endpoint "
         "mismatch (configured host: www.foxesscloud.com) or rejected token",
    context={"plant_id": plant_id, "host": ws_base},  # redacted on export
    severity="warning",        # debug | info | warning | error
)
```

Behaviour:
1. **Logs** a consistent line:
   `[{category}] {attempted}: {exc.__class__.__name__}: {exc}` plus
   ` — {hint}` when present. The pasted log line is now self-sufficient.
2. **Appends** a structured record to an always-on `deque(maxlen≈30)` on
   `domain_data`:
   `{t, category, attempted, exc_type, exc_str, hint, context}`.
   Always-on — independent of the opt-in debug-log sensor.

Constraints:
- **C-039 (dependency inversion):** the buffer lives on the
  brand-agnostic `SmartBatteryDomainData`, so the helper lives in
  `smart_battery/` and takes the buffer (or a getter) as a parameter — no
  brand import. The brand layer passes its `_dd(hass)`.
- **Exception narrowing happens at the call site** during the audit (e.g.
  `except aiohttp.WSServerHandshakeError`), not inside the helper.
  Truly-unexpected exceptions still get a broad catch but are recorded
  with `severity="error"`, `category="unexpected"` so genuine bugs surface
  rather than hide.

### Diagnostics enrichment (`diagnostics.py`)

Two new sections in `async_get_config_entry_diagnostics`:

**`recent_errors`** — the ring buffer, newest first, each entry
`{t, category, attempted, exc_type, exc_str, hint, context}`, run through
`async_redact_data`.

**`environment`** — derived at export time:
```
integration_version, cloud_base_url (the issue-#8 gap), ws_mode,
ws_connected, battery_compound_id_status ("discovered"|"missing"|present-redacted),
plant_id_present, inverter_model (if known), max_power_w, data_source (ws|api)
```
Most read from existing `domain_data`/`coordinator`/`inverter`;
`cloud_base_url` is the one genuinely new fact.

**Redaction (safety-critical):** extend `REDACT_KEYS` to cover any
token/serial that could appear in `context` or the compound id. The
compound id itself is redacted (embeds a serial); its *status* is exposed.

## Phase 2 — Adopt at high-value sites

Migrate the **config-or-environment** and **genuine-bug** sites first:
issue-#8 WS/discovery path, REST poll, schedule writes, token/login.
Transient + intentional-suppression sites only get log-level / comment
adjustments. Remaining sites migrate opportunistically later — not
blocking.

Optional ride-along (not required by this design): narrow the issue-#8
except to `WSServerHandshakeError` and make `BASE_URL` region-configurable.

## Testing

Brand-agnostic where the code is (C-040):
- `record_error`: logs the consistent format (caplog: exc-type +
  attempted + hint); appends a correctly-shaped record; ring buffer
  respects `maxlen` (oldest evicted); records regardless of debug-log
  opt-in. Brand-agnostic via the buffer-as-parameter signature.
- `diagnostics`: `recent_errors` + `environment` present and shaped;
  **redaction holds** — assert no token/password/serial/compound-id leaks
  into output (safety-critical; outward-facing file).
- Narrowed call sites: a test that the narrowed exception type is caught
  and recorded — only where behaviour changed.

## Knowledge tree

Strengthens C-026, serves P-005. Likely one new design decision (D-059:
structured error capture feeding logs + diagnostics) + coverage/test-doc
update, handled via the project-overview update workflow after
implementation.

## Effect on issue #8

Does NOT fix #8 (root cause is cloud/region-side). It makes the *next*
report of its kind arrive with the resolved host, ws_mode, WS-connected
state, and the typed error + hint already in the diagnostics file — the
stated goal.
