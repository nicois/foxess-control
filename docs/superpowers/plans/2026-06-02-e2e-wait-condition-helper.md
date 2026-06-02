# E2E `wait_for_condition` Helper + DOM Capture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the E2E suite a poll primitive that fails fast on known-bad DOM states and captures DOM (HTML + screenshot + summary) on any non-success exit, so the next panel/card-wait failure is diagnosable as a CI artifact instead of a bare 75s timeout.

**Architecture:** A new `_capture_failure(page, description)` writes capture files under `tests/e2e/screenshots/<worker>/failures/` (the path the GH workflows already upload with `if: always()`). A new `wait_for_condition(page, pass_check, *, timeout_ms, fail_check=None, description, poll_ms)` polls the pass predicate, aborts early if a fail predicate trips, captures DOM on any failure, and preserves the existing context-destroyed retry idiom. The existing *wait* helpers are rebuilt on it while preserving their public signatures so the ~36 call sites are untouched. `_safe_evaluate` stays a separate one-shot primitive but gains capture-on-final-failure.

**Tech Stack:** Python 3.14, pytest (+ pytest-xdist, pytest-randomly), Playwright sync API, containerised Home Assistant. Test-infrastructure only — no product code changes.

**Spec:** `docs/superpowers/specs/2026-06-02-e2e-wait-condition-helper-design.md`

**Key facts (verified against current code):**
- Context-destroyed signals: `("Execution context was destroyed", "navigating")` (`conftest.py:545`).
- GH upload: both `.github/workflows/e2e.yml:122-129` and `flaky-tests.yml:72-79` upload `tests/e2e/screenshots/` with `if: always()`, 14-day retention. Writing under that tree = automatic CI artifact, no workflow change.
- `_worker_id()` exists in `conftest.py:289`; `test_ui.py:31` has `_WORKER`; `SCREENSHOT_DIR = Path(__file__).parent / "screenshots" / _WORKER`.
- `test_ui.py` imports from conftest via `from .conftest import ...` (line 19); `PlaywrightError` imported (line 17), `contextlib` imported (line 9).
- **Contracts differ and MUST be preserved:** `_find_card`→`bool` (timeout→`False`, never raises); `_safe_wait_for_function`→`JSHandle`, re-raises; `_wait_for_card_hass`→`None`, raises; `_wait_for_lovelace_panel`→`None`, raises.

The full per-task detail follows in the task sections below.

---

## File Structure

- `tests/e2e/conftest.py` (modify) — add `_capture_failure`, `_failure_capture_dir`, `E2EConditionFailed`/`E2EConditionTimeout`, `wait_for_condition`; rebuild `_wait_for_stage`/`_wait_for_lovelace_panel` on it + a lovelace `fail_check`.
- `tests/e2e/test_ui.py` (modify) — rebuild `_find_card`, `_wait_for_card_hass`, `_safe_wait_for_function` on `wait_for_condition`; add capture to `_safe_evaluate` final-failure path.
- `tests/e2e/test_wait_condition.py` (create) — unit-ish tests for the primitive + capture, using a stub page (no container).

Capture artifacts: `tests/e2e/screenshots/<worker>/failures/<slug>-<n>.{html,png}`.

---

## Task 1: `_capture_failure` routine + capture dir

**Files:** Modify `tests/e2e/conftest.py`; Create `tests/e2e/test_wait_condition.py`.

- [ ] **Step 1: Write the failing test** — create `tests/e2e/test_wait_condition.py`:

```python
"""Tests for wait_for_condition + DOM failure capture (stub page, no container)."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import pytest
from tests.e2e import conftest as cf


class _StubPage:
    def __init__(self, *, content: str = "<html>stub</html>", screenshot_raises: bool = False) -> None:
        self._content = content
        self._screenshot_raises = screenshot_raises
    def content(self) -> str:
        return self._content
    def screenshot(self, **kwargs: Any) -> bytes:
        if self._screenshot_raises:
            raise RuntimeError("cannot screenshot")
        Path(kwargs["path"]).write_bytes(b"PNG-STUB")
        return b"PNG-STUB"
    def evaluate(self, expr: str, *args: Any) -> Any:
        return {"tags": ["home-assistant"], "error_text": None}


def test_capture_failure_writes_html_and_png(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    page = _StubPage(content="<html><body>captured</body></html>")
    summary = cf._capture_failure(page, "stage-3 timeout")
    htmls = list(tmp_path.glob("*.html"))
    pngs = list(tmp_path.glob("*.png"))
    assert htmls and "captured" in htmls[0].read_text()
    assert pngs and pngs[0].read_bytes() == b"PNG-STUB"
    assert isinstance(summary, str) and summary


def test_capture_failure_never_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    page = _StubPage(screenshot_raises=True)
    summary = cf._capture_failure(page, "stage-3 timeout")
    assert isinstance(summary, str)
    assert list(tmp_path.glob("*.html"))
```

- [ ] **Step 2: Run to verify FAIL** — `pytest tests/e2e/test_wait_condition.py -v` → `AttributeError: ... '_capture_failure'`.

- [ ] **Step 3: Implement** in `tests/e2e/conftest.py` (after `_worker_id`). Add `import itertools` if absent (`contextlib`, `Path` likely already imported — add if not):

```python
import contextlib
import itertools
from pathlib import Path

_CAPTURE_COUNTER = itertools.count(1)

_DOM_SUMMARY_JS = """() => {
    function present(root, tag) {
        if (root.querySelector(tag)) return true;
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot && present(el.shadowRoot, tag)) return true;
        }
        return false;
    }
    const tags = ['home-assistant','home-assistant-main','ha-panel-lovelace',
        'hui-root','ha-panel-error','hui-error-card'].filter(t => present(document, t));
    let error_text = null;
    const err = document.querySelector('ha-panel-error, hui-error-card');
    if (err) error_text = (err.textContent || '').trim().slice(0, 300);
    return {tags, error_text};
}"""


def _failure_capture_dir() -> Path:
    d = Path(__file__).parent / "screenshots" / _worker_id() / "failures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text)[:60].strip("-") or "wait"


def _capture_failure(page: Any, description: str) -> str:
    """Best-effort DOM capture on a wait failure. Never raises."""
    n = next(_CAPTURE_COUNTER)
    base = _failure_capture_dir() / f"{_slug(description)}-{n}"
    parts = [f"capture #{n} ({description})"]
    with contextlib.suppress(Exception):
        info = page.evaluate(_DOM_SUMMARY_JS)
        if isinstance(info, dict):
            if info.get("tags") is not None:
                parts.append(f"elements present: {info['tags']}")
            if info.get("error_text"):
                parts.append(f"error panel text: {info['error_text']!r}")
    with contextlib.suppress(Exception):
        base.with_suffix(".html").write_text(page.content())
        parts.append(f"html: {base.with_suffix('.html').name}")
    with contextlib.suppress(Exception):
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        parts.append(f"png: {base.with_suffix('.png').name}")
    return " | ".join(parts)
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/e2e/test_wait_condition.py -v` → both PASS.

- [ ] **Step 5: Commit** — `git add tests/e2e/conftest.py tests/e2e/test_wait_condition.py && git commit -m "feat(e2e): _capture_failure writes DOM html+png+summary under screenshot tree"`

---

## Task 2: `wait_for_condition` primitive

**Files:** Modify `tests/e2e/conftest.py`; extend `tests/e2e/test_wait_condition.py`.

- [ ] **Step 1: Write failing tests** — append to `tests/e2e/test_wait_condition.py`:

```python
import time as _time
from playwright.sync_api import Error as PlaywrightError


class _PollStubPage(_StubPage):
    def __init__(self, pass_results: list, fail_results: list | None = None, **kw) -> None:
        super().__init__(**kw)
        self._pass = list(pass_results)
        self._fail = list(fail_results or [])
    def evaluate(self, expr: str, *args: Any) -> Any:
        if "filter(t => present" in expr:        # the DOM summary JS
            return {"tags": ["home-assistant"], "error_text": None}
        if "FAILCHECK" in expr:
            return self._fail.pop(0) if self._fail else False
        return self._pass.pop(0) if self._pass else False
    def wait_for_load_state(self, *a, **k):
        return None


def test_pass_check_returns_immediately(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    out = cf.wait_for_condition(_PollStubPage(pass_results=[True]), "PASS true",
                                timeout_ms=5000, description="t")
    assert out is True
    assert not list(tmp_path.glob("*"))


def test_fail_check_aborts_before_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    page = _PollStubPage(pass_results=[False, False, False], fail_results=[True])
    start = _time.monotonic()
    with pytest.raises(cf.E2EConditionFailed) as ei:
        cf.wait_for_condition(page, "PASS x", fail_check="FAILCHECK y",
                              timeout_ms=10000, description="panel", poll_ms=10)
    assert _time.monotonic() - start < 5
    assert "capture" in str(ei.value)
    assert list(tmp_path.glob("*.html"))


def test_timeout_captures_dom(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    page = _PollStubPage(pass_results=[False] * 50)
    with pytest.raises(cf.E2EConditionTimeout) as ei:
        cf.wait_for_condition(page, "PASS x", timeout_ms=300,
                              description="stage3", poll_ms=10)
    assert "capture" in str(ei.value)
    assert list(tmp_path.glob("*.html"))
```

- [ ] **Step 2: Run to verify FAIL** — `pytest tests/e2e/test_wait_condition.py -v` → `AttributeError: ... 'wait_for_condition'`.

- [ ] **Step 3: Implement** in `tests/e2e/conftest.py` (after `_capture_failure`). Add `import time` if absent. The poll uses `page.evaluate(predicate)` in a loop so pass + fail predicates share one context-destroyed retry path. DO NOT redefine `_CONTEXT_DESTROYED_SIGNALS` — it already exists at ~line 545; reference it.

```python
import time


class E2EConditionTimeout(AssertionError):
    """A wait_for_condition pass_check did not become true in time."""


class E2EConditionFailed(AssertionError):
    """A wait_for_condition fail_check tripped before the pass_check."""


def wait_for_condition(page: Any, pass_check: str, *, timeout_ms: int,
                       fail_check: str | None = None, description: str = "",
                       poll_ms: int = 250) -> Any:
    """Poll pass_check until truthy; abort early if fail_check trips.

    Returns pass_check's truthy value. On fail_check tripping
    (E2EConditionFailed) or timeout (E2EConditionTimeout), captures DOM
    via _capture_failure and embeds the summary in the raised exception.
    Predicate evaluations hitting a context-destroyed navigation error are
    retried after a <=3000ms domcontentloaded settle within remaining budget.
    """
    from playwright._impl._errors import Error as _PwError  # noqa: PLC0415
    deadline = time.monotonic() + timeout_ms / 1000
    desc = description or "wait_for_condition"

    def _eval(expr: str) -> Any:
        while True:
            try:
                return page.evaluate(expr)
            except _PwError as exc:
                if not any(s in str(exc) for s in _CONTEXT_DESTROYED_SIGNALS):
                    raise
                settle = int((deadline - time.monotonic()) * 1000)
                if settle <= 0:
                    return None
                with contextlib.suppress(_PwError):
                    page.wait_for_load_state("domcontentloaded", timeout=min(settle, 3000))

    while True:
        if fail_check is not None and _eval(fail_check):
            summary = _capture_failure(page, desc)
            raise E2EConditionFailed(f"{desc}: fail_check tripped. {summary}")
        val = _eval(pass_check)
        if val:
            return val
        if time.monotonic() >= deadline:
            summary = _capture_failure(page, desc)
            raise E2EConditionTimeout(
                f"{desc}: pass_check not satisfied within {timeout_ms}ms. {summary}")
        time.sleep(poll_ms / 1000)
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/e2e/test_wait_condition.py -v` → 5 PASS.

- [ ] **Step 5: Commit** — `git add tests/e2e/conftest.py tests/e2e/test_wait_condition.py && git commit -m "feat(e2e): wait_for_condition — pass/fail poll with DOM capture on failure"`

---

## Task 3: Rebuild `_wait_for_lovelace_panel` on the primitive + HA-error fail_check

**Files:** Modify `tests/e2e/conftest.py` (`_wait_for_stage`).

This is the flaking wait. Each stage becomes a `wait_for_condition` call with the stage predicate as `pass_check` and a shared HA-error `fail_check`. Preserves the staged budgets and overall 75s cap. `_wait_for_lovelace_panel` itself is unchanged (it loops `_LOVELACE_PANEL_STAGES` calling `_wait_for_stage(page, name, pred, deadline, max_stage_ms=cap)`; confirm at ~line 837-848).

- [ ] **Step 1: Add the HA-error fail_check constant** near `_LOVELACE_PANEL_STAGES`:

```python
_LOVELACE_FAIL_CHECK = """() => {
    function present(root, sel) {
        if (root.querySelector(sel)) return true;
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot && present(el.shadowRoot, sel)) return true;
        }
        return false;
    }
    return present(document, 'ha-panel-error, hui-error-card');
}"""
```

- [ ] **Step 2: Rewrite `_wait_for_stage`** to delegate to `wait_for_condition`:

```python
def _wait_for_stage(page: Any, stage_name: str, predicate: str, deadline: float,
                    max_stage_ms: int | None = 30000) -> None:
    """Wait for one lovelace DOM milestone, failing fast on HA error states.

    Per-call budget is min(remaining_overall, max_stage_ms), or full
    remaining budget when max_stage_ms is None (final stage must not be
    re-capped). Delegates to wait_for_condition, which captures DOM and
    raises E2EConditionTimeout/E2EConditionFailed on failure (stage name in
    the description -> capture filename + CI log identify the stuck stage).
    """
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        raise E2EConditionTimeout(
            f"lovelace stage {stage_name!r}: overall deadline exceeded before stage could start")
    stage_ms = remaining_ms if max_stage_ms is None else min(remaining_ms, max_stage_ms)
    wait_for_condition(page, predicate, timeout_ms=stage_ms,
                       fail_check=_LOVELACE_FAIL_CHECK,
                       description=f"lovelace-stage:{stage_name}")
```

- [ ] **Step 3: Verify** — `pytest tests/e2e/test_wait_condition.py -v` PASS; `python -c "import tests.e2e.conftest"` succeeds. (The container-backed `TestWaitForLovelacePanel*` regression tests run in Task 6's slow suite.)

- [ ] **Step 4: Commit** — `git add tests/e2e/conftest.py && git commit -m "feat(e2e): lovelace panel wait fails fast on HA error states + captures DOM"`

---

## Task 4: Rebuild `_find_card`, `_wait_for_card_hass`, `_safe_wait_for_function`

**Files:** Modify `tests/e2e/test_ui.py`. Preserve EXACT signatures + return contracts so the ~36 call sites are untouched.

- [ ] **Step 1: Extend the conftest import** (line 19):

```python
from .conftest import (
    E2EConditionFailed,
    E2EConditionTimeout,
    set_inverter_state,
    wait_for_condition,
)
```

- [ ] **Step 2: Rewrite `_find_card`** (returns bool, never raises):

```python
def _find_card(page: Page, tag: str, timeout: int = 30000) -> bool:
    """Return True if a custom card element exists anywhere in the page DOM.

    Pierces shadow roots. Delegates to wait_for_condition (DOM capture on
    failure); preserves the original contract of returning False on timeout.
    """
    js = f"""() => {{
        function findInShadows(root, tag) {{
            if (root.querySelector(tag)) return true;
            for (const el of root.querySelectorAll('*')) {{
                if (el.shadowRoot && findInShadows(el.shadowRoot, tag)) return true;
            }}
            return false;
        }}
        return findInShadows(document, '{tag}');
    }}"""
    try:
        return bool(wait_for_condition(page, js, timeout_ms=timeout,
                                       description=f"find-card:{tag}"))
    except E2EConditionTimeout:
        return False
```

- [ ] **Step 3: Rewrite `_wait_for_card_hass`** (void, raises). FIRST read the current body (~line 219) and match its predicate semantics exactly (the `_hass` truthy + render-cycle check). Use the current predicate if it differs from below; do not weaken it:

```python
def _wait_for_card_hass(page: Page, tag: str, timeout_ms: int = 30000) -> None:
    """Wait until <tag>._hass is truthy and the card has rendered.
    Raises E2EConditionTimeout (with DOM capture) on failure.
    """
    js = f"""() => {{
        function find(root, tag) {{
            const el = root.querySelector(tag);
            if (el) return el;
            for (const e of root.querySelectorAll('*')) {{
                if (e.shadowRoot) {{ const f = find(e.shadowRoot, tag); if (f) return f; }}
            }}
            return null;
        }}
        const card = find(document, '{tag}');
        return !!(card && card._hass && card.shadowRoot && card.shadowRoot.childElementCount > 0);
    }}"""
    wait_for_condition(page, js, timeout_ms=timeout_ms, description=f"card-hass:{tag}")
```

- [ ] **Step 4: Rewrite `_safe_wait_for_function`** — keep `page.wait_for_function` (returns JSHandle) but capture DOM on both failure exits. `wait_for_condition` is NOT used here because it returns a value not a handle, and 13 callers do `.json_value()`:

```python
def _safe_wait_for_function(page: Page, expression: str, *, timeout: int = 10000,
                            retries: int = 3, settle_timeout_ms: int = 3000) -> JSHandle:
    """page.wait_for_function with context-destroyed retry, returning the JSHandle.
    Captures DOM on final failure before re-raising.
    """
    import time as _time
    from .conftest import _capture_failure
    deadline = _time.monotonic() + (timeout / 1000) * (retries + 1)
    last_exc: PlaywrightError | None = None
    for attempt in range(retries + 1):
        remaining = int((deadline - _time.monotonic()) * 1000)
        if remaining <= 0:
            break
        try:
            return page.wait_for_function(expression, timeout=min(remaining, timeout))
        except PlaywrightError as exc:
            msg = str(exc)
            if "Execution context was destroyed" not in msg and "navigating" not in msg:
                _capture_failure(page, "safe_wait_for_function")
                raise
            last_exc = exc
            if attempt == retries:
                break
            with contextlib.suppress(PlaywrightError):
                page.wait_for_load_state("domcontentloaded", timeout=settle_timeout_ms)
    _capture_failure(page, "safe_wait_for_function")
    assert last_exc is not None  # noqa: S101
    raise last_exc
```

- [ ] **Step 5: Verify** — `pytest tests/e2e/test_wait_condition.py -v` PASS; `python -c "import tests.e2e.test_ui"` succeeds (compile-checks the rewrites).

- [ ] **Step 6: Commit** — `git add tests/e2e/test_ui.py && git commit -m "refactor(e2e): _find_card/_wait_for_card_hass route through wait_for_condition; capture on failure"`

---

## Task 5: `_safe_evaluate` capture-on-final-failure

**Files:** Modify `tests/e2e/test_ui.py` (`_safe_evaluate`, ~line 84-148). Stays a separate one-shot primitive (24 call sites unchanged); only add DOM capture before the final re-raise.

- [ ] **Step 1: Write failing test** — append to `tests/e2e/test_wait_condition.py`:

```python
def test_safe_evaluate_captures_on_final_failure(tmp_path, monkeypatch) -> None:
    from tests.e2e import test_ui
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    class _AlwaysDestroyed(_StubPage):
        def evaluate(self, expr, *a):
            raise PlaywrightError("Execution context was destroyed")
        def wait_for_load_state(self, *a, **k):
            return None
    with pytest.raises(PlaywrightError):
        test_ui._safe_evaluate(_AlwaysDestroyed(), "() => 1", retries=1, settle_timeout_ms=1)
    assert list(tmp_path.glob("*.html"))
```

- [ ] **Step 2: Run to verify FAIL** — no `.html` written yet.

- [ ] **Step 3: Add capture before the final re-raise** in `_safe_evaluate` (replace the tail `assert last_exc is not None` / `raise last_exc`):

```python
    from .conftest import _capture_failure
    _capture_failure(page, "safe_evaluate")
    assert last_exc is not None  # noqa: S101
    raise last_exc
```

- [ ] **Step 4: Run to verify PASS** — `pytest tests/e2e/test_wait_condition.py::test_safe_evaluate_captures_on_final_failure -v`.

- [ ] **Step 5: Commit** — `git add tests/e2e/test_ui.py tests/e2e/test_wait_condition.py && git commit -m "feat(e2e): _safe_evaluate captures DOM on final failure before re-raise"`

---

## Task 6: Full E2E suite verification (the real proof)

**Files:** none (verification only).

- [ ] **Step 1:** `pytest tests/ -m "not slow" --tb=short` → all pass (includes test_wait_condition.py; no regressions).
- [ ] **Step 2:** `pytest tests/e2e/ -m slow -n auto --tb=short 2>&1 | tail -40` → green. If a wait now FAILS, it must raise E2EConditionTimeout/E2EConditionFailed with a DOM summary AND a capture file must exist under `tests/e2e/screenshots/<worker>/failures/` — inspect it (feature working as intended; investigate what it reveals).
- [ ] **Step 3:** `pre-commit run --all-files` → clean on touched files (pre-existing unrelated mypy error in `tests/test_sensor_listener_safety.py:284` is not introduced here).
- [ ] **Step 4:** Report (no commit): unit + E2E results; whether any wait exercised the capture path and what it showed; confirm the ~36 call sites were unmodified (only helper bodies).

---

## Self-Review

- **Spec coverage:** capture routine + dir → T1. wait_for_condition (pass/fail/timeout + capture + context-destroyed retry) → T2. Lovelace migration + HA-error fail_check → T3. _find_card/_wait_for_card_hass/_safe_wait_for_function migration preserving contracts → T4. _safe_evaluate capture (kept separate) → T5. CI-artifact path → T1 _failure_capture_dir (verified vs both workflows). Full-suite regression proof → T6.
- **Placeholders:** every code step shows real code; predicates concrete; no TBD. The "read current body before rewriting" (T4 S3) is a correctness safeguard with explicit fallback, not a placeholder.
- **Contract/type consistency:** wait_for_condition signature identical T2-T4; E2EConditionTimeout/Failed defined T2, imported T4. Returns preserved: _find_card→bool (catches E2EConditionTimeout→False), _wait_for_card_hass→None/raises, _safe_wait_for_function→JSHandle/raises (deliberately NOT via wait_for_condition — returns handle not value), _safe_evaluate→value/raises (separate, T5). _capture_failure/_failure_capture_dir consistent T1-T5.
- **Scope:** single subsystem, test-only, no product code.
