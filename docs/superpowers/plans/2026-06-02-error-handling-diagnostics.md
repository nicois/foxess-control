# Structured Error Capture for Self-Sufficient Diagnostics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a user hits an error, the log line and the HA diagnostics download already contain what a maintainer would otherwise ask for (exception type, what was attempted, likely-cause hint, resolved cloud host/region, ws_mode, WS state) — reducing back-and-forth.

**Architecture:** A single brand-agnostic `record_error(...)` helper both logs a consistent, self-sufficient line AND appends a structured record to an always-on ring buffer on `SmartBatteryDomainData`. `diagnostics.py` exports that buffer plus a new `environment` section. Adoption is audit-driven: classify all 65 broad excepts first, then migrate the config/environment and genuine-bug sites.

**Tech Stack:** Python 3.14, Home Assistant integration, pytest (+ pytest-xdist, pytest-randomly), aiohttp. Canonical code in `smart_battery/`; the vendored copy under `custom_components/foxess_control/smart_battery/` is synced by the pre-commit hook (C-015) — never hand-edit it.

**Key constraints:** C-039 (smart_battery/ must not import brand modules — the helper takes the buffer as a parameter), C-040 (brand-agnostic tests use no brand adapter), C-015 (vendored sync via pre-commit), C-026 (proactive error surfacing), P-005 (transparency). Redaction of the outward-facing diagnostics file is safety-critical.

**Spec:** `docs/superpowers/specs/2026-06-02-error-handling-diagnostics-design.md`

---

## File Structure

- `smart_battery/logging.py` (modify) — add `record_error(...)` + level map + timestamp helper. Brand-agnostic; no HA import.
- `smart_battery/domain_data.py` (modify) — add `recent_errors: deque` field to `SmartBatteryDomainData`.
- `custom_components/foxess_control/diagnostics.py` (modify) — add `recent_errors` + `environment` sections; extend `REDACT_KEYS`.
- `custom_components/foxess_control/foxess/web_session.py` (modify, Phase 2) — narrow the issue-#8 except and call `record_error`.
- `tests/test_error_recording.py` (create) — `record_error` + ring-buffer tests.
- `tests/test_diagnostics.py` (create or extend) — diagnostics shape + redaction tests.
- `docs/superpowers/audit/2026-06-02-broad-excepts.md` (create, Task 1) — the audit table.

The vendored copies under `custom_components/foxess_control/smart_battery/{logging,domain_data}.py` update automatically via pre-commit — do not edit them directly.

---

## Task 1: Audit the 65 broad excepts

**Files:**
- Create: `docs/superpowers/audit/2026-06-02-broad-excepts.md`

This task is an investigation, not code — it produces the table that scopes Phase 2 (Tasks 5+). No tests.

- [ ] **Step 1: Enumerate every broad except in production code**

Run:
```bash
cd /home/claude-aiven-2/git/fox
grep -rn "except Exception\|except BaseException\|except:" \
  custom_components/foxess_control/ smart_battery/ --include="*.py" \
  | grep -v "tests/\|/test_" \
  | grep -v "custom_components/foxess_control/smart_battery/"   # skip vendored dupes
```
Expected: ~33 unique production sites (the 65 count double-counts the vendored `smart_battery/` copy; exclude the vendored path so each logical site appears once).

- [ ] **Step 2: Classify each site into one of four buckets**

For each `file:line`, read the surrounding try-body and the except handler, and assign exactly one bucket:
- `transient-retryable` — single API timeout / brief network blip; self-heals next tick.
- `config-or-environment` — wrong region/host, expired/rejected token, missing entity, unreachable/changed endpoint (issue #8 is here).
- `genuine-bug` — shouldn't happen; indicates our defect.
- `intentional-suppression` — best-effort cleanup where failure genuinely doesn't matter.

Write the table to the audit file with columns: `file:line | current handler summary | bucket | recommended action`. Recommended actions:
- transient-retryable → keep broad-ish; drop log level to debug/info; no buffer.
- config-or-environment → narrow the except to the meaningful type(s); call `record_error` with hint + context; buffer.
- genuine-bug → `record_error` at `severity="error"`, `category="unexpected"`; buffer; never silently swallow.
- intentional-suppression → add a one-line comment explaining why; leave as-is.

- [ ] **Step 3: Add a header to the audit file**

The file must begin with:
```markdown
# Broad-except audit (2026-06-02)

Drives Phase 2 of the error-handling/diagnostics plan
(docs/superpowers/plans/2026-06-02-error-handling-diagnostics.md).
Vendored copies under custom_components/foxess_control/smart_battery/
are excluded (synced from canonical smart_battery/).

| file:line | current handler | bucket | recommended action |
|---|---|---|---|
```
Then the rows. End with a count summary per bucket.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/audit/2026-06-02-broad-excepts.md
git commit -m "Audit: classify broad except clauses for error-handling rollout"
```

---

## Task 2: Ring buffer field on SmartBatteryDomainData

**Files:**
- Modify: `smart_battery/domain_data.py` (the `SmartBatteryDomainData` dataclass)
- Test: `tests/test_error_recording.py` (created here, extended in Task 3)

- [ ] **Step 1: Write the failing test**

Create `tests/test_error_recording.py`:
```python
"""Tests for structured error recording (record_error + ring buffer)."""

from __future__ import annotations

from collections import deque

from smart_battery.domain_data import SmartBatteryDomainData


def test_domain_data_has_bounded_recent_errors_buffer() -> None:
    dd = SmartBatteryDomainData()
    assert isinstance(dd.recent_errors, deque)
    # Always-on, bounded so it can't grow without limit.
    assert dd.recent_errors.maxlen == 30
    # Independent instances get independent buffers (no shared mutable default).
    dd2 = SmartBatteryDomainData()
    dd.recent_errors.append({"x": 1})
    assert len(dd2.recent_errors) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_error_recording.py -v`
Expected: FAIL — `AttributeError: 'SmartBatteryDomainData' object has no attribute 'recent_errors'`

- [ ] **Step 3: Add the field**

In `smart_battery/domain_data.py`, add `deque` to the imports at the top:
```python
from collections import deque
```
Then add this field to the `SmartBatteryDomainData` dataclass (after `pending_override_cleanup`):
```python
    # Always-on bounded ring buffer of recent structured errors,
    # exported by the brand diagnostics platform.  Independent of the
    # opt-in debug-log sensor.  See smart_battery/logging.py::record_error.
    recent_errors: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=30)
    )
```
(`Any` and `field` are already imported in this file.)

- [ ] **Step 4: Sync vendored copy + run test**

Run:
```bash
pre-commit run sync-vendored-smart-battery --all-files || true
pytest tests/test_error_recording.py -v
```
Expected: the sync hook updates `custom_components/foxess_control/smart_battery/domain_data.py`; test PASSES.

- [ ] **Step 5: Commit**

```bash
git add smart_battery/domain_data.py custom_components/foxess_control/smart_battery/domain_data.py tests/test_error_recording.py
git commit -m "feat: always-on recent_errors ring buffer on SmartBatteryDomainData"
```

---

## Task 3: The `record_error` helper

**Files:**
- Modify: `smart_battery/logging.py`
- Test: `tests/test_error_recording.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_error_recording.py`:
```python
import logging
from collections import deque

import pytest

from smart_battery.logging import record_error


def test_record_error_logs_self_sufficient_line(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.record_error.line")
    buf: deque[dict] = deque(maxlen=30)
    with caplog.at_level(logging.WARNING, logger="test.record_error.line"):
        record_error(
            logger, buf,
            category="ws_discovery",
            attempted="battery ID discovery via wsmaitian WS",
            exc=ValueError("200, message='Invalid response status'"),
            hint="server returned HTTP 200 not 101 — possible regional endpoint mismatch",
        )
    text = caplog.text
    # The pasted log line alone must name: category, attempt, exc type, exc str, hint.
    assert "ws_discovery" in text
    assert "battery ID discovery via wsmaitian WS" in text
    assert "ValueError" in text
    assert "Invalid response status" in text
    assert "regional endpoint mismatch" in text


def test_record_error_appends_structured_record() -> None:
    logger = logging.getLogger("test.record_error.buf")
    buf: deque[dict] = deque(maxlen=30)
    record_error(
        logger, buf,
        category="ws_discovery",
        attempted="discover battery id",
        exc=ValueError("boom"),
        hint="check region",
        context={"host": "www.foxesscloud.com", "plant_id": "abc"},
        severity="warning",
    )
    assert len(buf) == 1
    rec = buf[0]
    assert rec["category"] == "ws_discovery"
    assert rec["attempted"] == "discover battery id"
    assert rec["exc_type"] == "ValueError"
    assert rec["exc_str"] == "boom"
    assert rec["hint"] == "check region"
    assert rec["context"] == {"host": "www.foxesscloud.com", "plant_id": "abc"}
    assert rec["severity"] == "warning"
    assert isinstance(rec["t"], str) and rec["t"]  # ISO timestamp present


def test_record_error_respects_buffer_maxlen() -> None:
    logger = logging.getLogger("test.record_error.cap")
    buf: deque[dict] = deque(maxlen=30)
    for i in range(35):
        record_error(logger, buf, category="c", attempted=f"a{i}",
                     exc=ValueError(str(i)))
    assert len(buf) == 30
    # Oldest evicted; newest retained.
    assert buf[-1]["exc_str"] == "34"
    assert buf[0]["exc_str"] == "5"


def test_record_error_buffer_none_logs_without_crashing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.record_error.none")
    with caplog.at_level(logging.ERROR, logger="test.record_error.none"):
        record_error(logger, None, category="unexpected",
                     attempted="x", exc=RuntimeError("y"), severity="error")
    assert "RuntimeError" in caplog.text


def test_record_error_severity_maps_to_log_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.record_error.sev")
    buf: deque[dict] = deque(maxlen=30)
    with caplog.at_level(logging.DEBUG, logger="test.record_error.sev"):
        record_error(logger, buf, category="c", attempted="a",
                     exc=ValueError("z"), severity="error")
    assert caplog.records[-1].levelno == logging.ERROR
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_error_recording.py -v`
Expected: FAIL — `ImportError: cannot import name 'record_error' from 'smart_battery.logging'`

- [ ] **Step 3: Implement `record_error`**

In `smart_battery/logging.py`, add `datetime` and `deque` imports at the top alongside the existing imports:
```python
import datetime
from collections import deque
```
Then add at module scope (after the imports, before or after `SessionContextFilter` — anywhere top-level):
```python
_SEVERITY_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def record_error(
    logger: logging.Logger,
    buffer: "deque[dict[str, Any]] | None",
    *,
    category: str,
    attempted: str,
    exc: BaseException,
    hint: str | None = None,
    context: dict[str, Any] | None = None,
    severity: str = "warning",
) -> None:
    """Log a self-sufficient error line and record it to a ring buffer.

    Brand-agnostic (C-039): takes the ring buffer as a parameter rather
    than importing brand domain data.  Pass ``None`` for *buffer* to log
    only.  The log line names the category, what was attempted, the
    exception type and string, and an optional likely-cause *hint*, so a
    single pasted line is self-sufficient.  The buffer record is the
    structured form exported by the diagnostics platform.

    *context* should contain only non-secret facts; the diagnostics
    exporter redacts known-sensitive keys, but do not put raw tokens or
    passwords here.
    """
    exc_type = type(exc).__name__
    message = f"[{category}] {attempted}: {exc_type}: {exc}"
    if hint:
        message += f" — {hint}"
    logger.log(_SEVERITY_LEVELS.get(severity, logging.WARNING), "%s", message)
    if buffer is not None:
        buffer.append(
            {
                "t": datetime.datetime.now(datetime.UTC).isoformat(
                    timespec="seconds"
                ),
                "category": category,
                "attempted": attempted,
                "exc_type": exc_type,
                "exc_str": str(exc),
                "hint": hint,
                "context": dict(context) if context else {},
                "severity": severity,
            }
        )
```

- [ ] **Step 4: Sync vendored copy + run tests**

Run:
```bash
pre-commit run sync-vendored-smart-battery --all-files || true
pytest tests/test_error_recording.py -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add smart_battery/logging.py custom_components/foxess_control/smart_battery/logging.py tests/test_error_recording.py
git commit -m "feat: record_error helper — self-sufficient log line + ring-buffer record"
```

---

## Task 4: Diagnostics — recent_errors + environment sections

**Files:**
- Modify: `custom_components/foxess_control/diagnostics.py`
- Test: `tests/test_diagnostics.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_diagnostics.py`:
```python
"""Tests for the FoxESS Control diagnostics platform."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.foxess_control.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.foxess_control.const import DOMAIN


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_hass_and_entry(domain_data) -> tuple[MagicMock, MagicMock]:
    hass = MagicMock()
    hass.data = {DOMAIN: domain_data}
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {"api_key": "SECRET", "device_serial": "SN123"}
    entry.options = {"ws_mode": "auto"}
    return hass, entry


def test_diagnostics_includes_recent_errors() -> None:
    buf = deque(maxlen=30)
    buf.append({
        "t": "2026-06-02T00:00:00+00:00", "category": "ws_discovery",
        "attempted": "discover battery id", "exc_type": "WSServerHandshakeError",
        "exc_str": "200, message='Invalid response status'",
        "hint": "regional endpoint mismatch", "context": {"host": "www.foxesscloud.com"},
        "severity": "warning",
    })
    dd = SimpleNamespace(
        entries={}, smart_charge_state=None, smart_discharge_state=None,
        smart_error_state=None, realtime_ws=None, taper_profile=None,
        ws_mode="auto", recent_errors=buf, web_session=None, plant_id="p1",
        battery_compound_id=None,
    )
    hass, entry = _make_hass_and_entry(dd)
    result = _run(async_get_config_entry_diagnostics(hass, entry))
    assert "recent_errors" in result
    assert result["recent_errors"][0]["exc_type"] == "WSServerHandshakeError"


def test_diagnostics_environment_reports_host_and_ws_mode() -> None:
    dd = SimpleNamespace(
        entries={}, smart_charge_state=None, smart_discharge_state=None,
        smart_error_state=None, realtime_ws=None, taper_profile=None,
        ws_mode="auto", recent_errors=deque(maxlen=30),
        web_session=SimpleNamespace(BASE_URL="https://www.foxesscloud.com"),
        plant_id="p1", battery_compound_id=None,
    )
    hass, entry = _make_hass_and_entry(dd)
    result = _run(async_get_config_entry_diagnostics(hass, entry))
    env = result["environment"]
    assert env["cloud_base_url"] == "https://www.foxesscloud.com"
    assert env["ws_mode"] == "auto"
    assert env["ws_connected"] is False
    assert env["plant_id_present"] is True
    assert env["battery_compound_id_status"] == "missing"


def test_diagnostics_redacts_secrets_everywhere() -> None:
    # A token accidentally placed in an error context must not leak.
    buf = deque(maxlen=30)
    buf.append({
        "t": "2026-06-02T00:00:00+00:00", "category": "login",
        "attempted": "web login", "exc_type": "X", "exc_str": "y",
        "hint": None, "context": {"api_key": "LEAKED", "host": "h"},
        "severity": "warning",
    })
    dd = SimpleNamespace(
        entries={}, smart_charge_state=None, smart_discharge_state=None,
        smart_error_state=None, realtime_ws=None, taper_profile=None,
        ws_mode="auto", recent_errors=buf,
        web_session=SimpleNamespace(BASE_URL="https://www.foxesscloud.com"),
        plant_id="p1", battery_compound_id="uuid@SN999",
    )
    hass, entry = _make_hass_and_entry(dd)
    result = _run(async_get_config_entry_diagnostics(hass, entry))
    flat = str(result)
    assert "SECRET" not in flat       # entry.data api_key
    assert "SN123" not in flat        # entry.data device_serial
    assert "LEAKED" not in flat       # api_key inside an error context
    assert "SN999" not in flat        # serial inside the compound id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_diagnostics.py -v`
Expected: FAIL — `KeyError: 'recent_errors'` / `KeyError: 'environment'` (and the redaction test fails because `LEAKED`/`SN999` currently leak).

- [ ] **Step 3: Implement the new sections**

In `custom_components/foxess_control/diagnostics.py`:

(a) Extend `REDACT_KEYS`:
```python
REDACT_KEYS = {
    "api_key", "web_password", "web_username", "device_serial",
    "token", "batSn", "battery_compound_id",
}
```

(b) In `async_get_config_entry_diagnostics`, before the `return async_redact_data(...)`, build the two new sections:
```python
    ws = domain_data.realtime_ws
    ws_connected = bool(ws is not None and getattr(ws, "is_connected", False))

    web_session = getattr(domain_data, "web_session", None)
    cloud_base_url = getattr(web_session, "BASE_URL", None)

    compound = getattr(domain_data, "battery_compound_id", None)
    if compound:
        compound_status = "discovered"
    else:
        compound_status = "missing"

    environment = {
        "integration_version": _integration_version(),
        "cloud_base_url": cloud_base_url,
        "ws_mode": getattr(domain_data, "ws_mode", None),
        "ws_connected": ws_connected,
        "battery_compound_id_status": compound_status,
        "plant_id_present": bool(getattr(domain_data, "plant_id", None)),
        "inverter_model": getattr(inverter, "model", None) if inverter else None,
        "max_power_w": inverter.max_power_w if inverter else None,
        "data_source": (
            coordinator_data.get("data_source") if coordinator_data else None
        ),
    }

    recent_errors = list(getattr(domain_data, "recent_errors", []))
```
Add `recent_errors` and `environment` to the dict passed to `async_redact_data`:
```python
            "recent_errors": recent_errors,
            "environment": environment,
```

(c) Add the version helper at module scope:
```python
def _integration_version() -> str | None:
    """Read the integration version from manifest.json."""
    import json
    from pathlib import Path

    try:
        manifest = Path(__file__).parent / "manifest.json"
        return json.loads(manifest.read_text()).get("version")
    except Exception:  # diagnostics must never raise
        return None
```
Note: `compound_status` exposes only presence, and `battery_compound_id` is in `REDACT_KEYS` so any place the raw id appears is redacted. The `environment` block intentionally does NOT include the raw compound id.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_diagnostics.py -v`
Expected: all PASS, including `test_diagnostics_redacts_secrets_everywhere`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/diagnostics.py tests/test_diagnostics.py
git commit -m "feat: diagnostics exports recent_errors + environment (host/ws_mode/compound status)"
```

---

## Task 5: Adopt record_error at the issue-#8 site (exemplar for Phase 2)

**Files:**
- Modify: `custom_components/foxess_control/foxess/web_session.py` (the `async_discover_battery_id` except, ~line 201)
- Test: `tests/test_error_recording.py` (extend) OR a focused test in `tests/test_web_session.py` if one exists

This task fixes the concrete config-or-environment site from the audit. Every other config-or-environment / genuine-bug row in the audit table follows this same shape — repeat per row in follow-up commits.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_error_recording.py` (adjust import path if `web_session` needs an event loop / aiohttp — keep the test at the unit level by driving the except path directly if a full WS mock is heavy; otherwise assert on the recorded buffer entry):
```python
def test_battery_discovery_records_handshake_error_with_context() -> None:
    """A WSServerHandshakeError in discovery is recorded with a region hint."""
    import aiohttp
    from collections import deque
    import logging
    from smart_battery.logging import record_error

    buf: deque[dict] = deque(maxlen=30)
    logger = logging.getLogger("test.web_session")
    # Simulate the narrowed except body the implementation will run:
    exc = aiohttp.WSServerHandshakeError(
        request_info=None, history=(), status=200,
        message="Invalid response status",
    )
    record_error(
        logger, buf,
        category="ws_discovery",
        attempted="battery ID discovery via wsmaitian WS",
        exc=exc,
        hint=("server returned HTTP 200 not 101 — possible regional "
              "endpoint mismatch (configured host: https://www.foxesscloud.com) "
              "or rejected web-session token"),
        context={"host": "https://www.foxesscloud.com", "plant_id": "p1"},
    )
    rec = buf[0]
    assert rec["category"] == "ws_discovery"
    assert rec["exc_type"] == "WSServerHandshakeError"
    assert "regional" in rec["hint"]
    assert rec["context"]["host"] == "https://www.foxesscloud.com"
```
(This test pins the contract for the recorded entry. If the project has a `web_session` test harness that can drive `async_discover_battery_id` against a fake aiohttp session returning a 200 handshake, prefer that — it exercises the real call site. Use whichever the existing test infrastructure supports; do not mock away the `WSServerHandshakeError` type.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_error_recording.py::test_battery_discovery_records_handshake_error_with_context -v`
Expected: FAIL until the import/signature lines are in place (they are, from Task 3) — if it passes immediately it's only asserting the helper, so ALSO add the call-site change in Step 3 and verify the real path. (See note below.)

- [ ] **Step 3: Narrow the except and call record_error at the real site**

In `custom_components/foxess_control/foxess/web_session.py`, replace the broad handler in `async_discover_battery_id`:
```python
        except Exception as exc:
            _LOGGER.warning("Battery ID discovery via WebSocket failed: %s", exc)
        return None
```
with a narrowed, recorded version (add `import aiohttp` if not already imported, and import `record_error` from the smart_battery logging module + obtain the domain-data buffer via the brand accessor `_dd(hass)` — pass `None` if the helper has no hass handle here; check how `async_discover_battery_id` is called and thread the buffer or `domain_data` in if available, otherwise log-only with `buffer=None`):
```python
        except aiohttp.WSServerHandshakeError as exc:
            record_error(
                _LOGGER, buffer,
                category="ws_discovery",
                attempted="battery ID discovery via wsmaitian WS",
                exc=exc,
                hint=(
                    "server returned HTTP %s not 101 — possible regional "
                    "endpoint mismatch (configured host: %s) or rejected "
                    "web-session token" % (exc.status, self.BASE_URL)
                ),
                context={"host": self.BASE_URL, "plant_id": plant_id},
            )
        except Exception as exc:
            record_error(
                _LOGGER, buffer,
                category="unexpected",
                attempted="battery ID discovery via wsmaitian WS",
                exc=exc,
                severity="error",
            )
        return None
```
Threading the buffer: `async_discover_battery_id` is called from `coordinator._rediscover_battery_compound_id` (and the startup task). Add a `buffer: "deque[dict[str, Any]] | None" = None` parameter to `async_discover_battery_id`, and pass `domain_data.recent_errors` from each caller. If threading the buffer proves invasive in this task, pass `buffer=None` (log-only) and record a follow-up to thread it — but the narrowed except + improved log line must land regardless.

- [ ] **Step 4: Run tests + full suite**

Run:
```bash
pytest tests/test_error_recording.py -v
pytest tests/ -m "not slow" --tb=short
```
Expected: target test PASSES; full unit suite green (no regressions).

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --all-files
git add custom_components/foxess_control/foxess/web_session.py tests/test_error_recording.py
git add custom_components/foxess_control/coordinator.py  # if a caller was threaded
git commit -m "feat: battery-ID discovery records typed error + region hint (issue #8 diagnosability)"
```

---

## Task 6: Migrate remaining config/environment + genuine-bug sites

**Files:** per the Task 1 audit table — REST poll (`coordinator.py`), schedule writes (`foxess/inverter.py` / `foxess_adapter.py`), token/login (`web_session.py`), and any other `config-or-environment` / `genuine-bug` rows.

For EACH such row, repeat the Task 5 shape:

- [ ] **Step 1:** Write a test asserting the recorded entry's `category`, `exc_type`, and that a `hint`/`context` is present (or, where a harness exists, drive the real call site).
- [ ] **Step 2:** Run it — confirm it fails / pins the new contract.
- [ ] **Step 3:** Narrow the except to the meaningful exception type(s); call `record_error` with `category`, `attempted`, `exc`, `hint`, `context`, and `severity` (`error` for genuine-bug, `warning` for config/environment). Thread `domain_data.recent_errors` as `buffer`.
- [ ] **Step 4:** `pytest tests/ -m "not slow"` — green.
- [ ] **Step 5:** `pre-commit run --all-files`; commit per logical site (e.g. `feat: REST poll records typed error + context`).

`transient-retryable` sites: only adjust log level (warning→debug/info) and add a brief comment — no `record_error`, no test change. `intentional-suppression` sites: add a one-line comment explaining why the failure is safe to swallow — no code change.

Do NOT attempt all sites in one commit. One logical site per commit keeps review tractable and bisectable.

---

## Task 7: Knowledge-tree update

**Files:** `docs/knowledge/` via the project-overview update workflow.

- [ ] **Step 1:** Add design decision **D-059** (structured error capture feeding logs + diagnostics) to an appropriate `04-design/*.md` (likely a new `04-design/observability.md` or append to an existing observability/diagnostics doc), citing C-026 / P-005, classification `other`, traces to `tests/test_error_recording.py` + `tests/test_diagnostics.py`.
- [ ] **Step 2:** Update `02-constraints.md` C-026 `Traces` to include the new helper + diagnostics sections.
- [ ] **Step 3:** Add the new test files to `06-tests.md`; update matrices/counts in `05-coverage.md`.
- [ ] **Step 4:** Run `python scripts/knowledge_audit.py` — confirm no new priority/classification gaps or ID collisions.
- [ ] **Step 5:** Bump `last_verified` on edited docs + META `workflow_state`; commit `Docs: D-059 structured error capture`.

---

## Self-Review

- **Spec coverage:** Phase 0 audit → Task 1. `record_error` helper → Tasks 2–3. Diagnostics `recent_errors` + `environment` → Task 4. Redaction (safety-critical) → Task 4 Step 1/3. Audit-driven Phase 2 adoption → Tasks 5–6. Issue-#8 site + region hint → Task 5. Knowledge tree (D-059, C-026) → Task 7. All spec sections covered.
- **Placeholder scan:** No TBD/TODO; every code step shows real code. The one genuinely deferred decision (whether to thread the buffer into `async_discover_battery_id` in Task 5 vs follow-up) is explicit with a concrete fallback (`buffer=None`, log-only), not a vague "handle later".
- **Type consistency:** `record_error(logger, buffer, *, category, attempted, exc, hint, context, severity)` signature is identical across Tasks 3, 5, 6. `recent_errors: deque[dict[str, Any]]` (maxlen 30) consistent across Tasks 2, 4, 5. Record shape `{t, category, attempted, exc_type, exc_str, hint, context, severity}` consistent across Tasks 3 and 4's test fixtures.
- **Scope:** Single subsystem (error capture + diagnostics). Focused. No decomposition needed.
