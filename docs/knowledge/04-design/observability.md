---
project: FoxESS Control
level: 4
feature: Observability & Diagnostics
last_verified: 2026-06-02
traces_up: [../02-constraints.md, ../03-architecture.md]
traces_down: [../05-coverage.md, ../06-tests.md]
---
# Design: Observability & Diagnostics

How operational errors are captured and surfaced so a user filing a bug
report already has the information a maintainer would otherwise have to
ask for — reducing back-and-forth.

## Design Decisions

### D-059: Structured operational-error capture feeding logs + diagnostics

**Decision**: A single brand-agnostic helper
`record_operational_error(logger, buffer, *, category, attempted, exc,
hint=None, context=None, severity="warning")`
(`smart_battery/logging.py`) both (a) logs a self-sufficient line —
`[{category}] {attempted}: {exc_type}: {exc} — {hint}` — and (b) appends
a structured record `{t, category, attempted, exc_type, exc_str, hint,
context, severity}` to an always-on bounded ring buffer
(`SmartBatteryDomainData.recent_errors`, `deque(maxlen=30)`). The
diagnostics platform (`custom_components/foxess_control/diagnostics.py`)
exports that buffer as `recent_errors` plus an `environment` section
(integration version, resolved cloud base URL, `ws_mode`, WS-connected,
battery-compound-id *status*, plant-id presence, inverter model/max
power, data source). All output passes through `async_redact_data` with
an extended `REDACT_KEYS` (`api_key`, `web_password`, `web_username`,
`device_serial`, `token`, `batSn`, `battery_compound_id`).

This is **distinct from** the listener's `_record_error`
(`smart_battery/listeners.py`), which records a *session abort* to
`smart_error_state` and raises an HA Repair issue (D-029/D-048). D-059 is
for *operational/diagnostic* errors that don't warrant a Repair issue but
must be inspectable when a user reports a problem.

**Context**: GH issue #8 — a `WSServerHandshakeError: 200` during battery
ID discovery — could not be acted on without asking the reporter for
facts the integration already knew (cloud region/host, `ws_mode`, the
exception type, whether it worked during an active session). A
classification audit (`docs/superpowers/audit/2026-06-02-broad-excepts.md`)
found 50 broad `except` sites logging with `_LOGGER.warning("X: %s", exc)`
— no exception-type discrimination, no actionable hint.

**Rationale**: One capture point feeding both the log line and the
diagnostics download avoids drift between the two surfaces. The buffer is
always-on (independent of the opt-in debug-log sensor) so the facts are
present in a diagnostics download without asking the user to reproduce
with debug logging enabled. Recording is purely additive: each migrated
site narrows its `except` to the meaningful exception type(s), keeps a
catch-all `unexpected`/`error` arm so genuine bugs still surface, and
preserves the function's existing control flow (re-raise / fallback
unchanged).

**Priority served**: P-005 (operational transparency).
**Trades against**: none.
**Classification**: other (observability infrastructure; not an invariant
enforcement and not pacing).
**Alternatives considered**:
- Stable error-*code* taxonomy up front: deferred — premature before the
  audit showed the real distribution of error types; categories
  (greppable strings) suffice for now and codes can graduate later.
- Expanding HA Repair issues to every error: rejected — Repair issues are
  the right surface for session aborts / actionable user-fixable states
  (D-029/D-048), not transient/diagnostic detail.
- Two independent tracks (separate log fixes + diagnostics buffer):
  rejected — overlapping info captured by different code drifts.

**Rollout**: brand-agnostic helper + buffer + diagnostics landed first;
adoption is audit-driven and incremental — the issue-#8 site
(`async_discover_battery_id`) plus a high-value subset (BMS-temp fetch,
entity-mode power/schedule/export writes, REST poll) migrated; the
remaining lower-value sites migrate opportunistically against the audit
table. C-039 preserved throughout: the helper takes the buffer as a
parameter, so `smart_battery/` does not import the brand layer.

**Traces**: C-026 (proactive error surfacing), C-039 (dependency
inversion — buffer-as-parameter); P-005;
`smart_battery/logging.py::record_operational_error`,
`smart_battery/domain_data.py::SmartBatteryDomainData.recent_errors`,
`custom_components/foxess_control/diagnostics.py`;
`tests/test_error_recording.py`, `tests/test_diagnostics.py`.

## Key Behaviours

- The diagnostics download must never raise — the version helper and
  buffer reads are defensive (broad except / `getattr` fallbacks),
  because a raising diagnostics handler would deny the user the very
  file they need to report a problem.
- The raw `battery_compound_id` (which embeds a battery serial) is never
  emitted; only its discovery *status* (`discovered` / `missing`) appears
  in `environment`, and the id is in `REDACT_KEYS` as defence-in-depth.
- Migrated `except` sites keep a catch-all `unexpected`/`error` arm so
  narrowing to specific types cannot silently swallow an unanticipated
  exception (swallow-by-omission).
