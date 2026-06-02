"""Tests for wait_for_condition + DOM failure capture (stub page, no container)."""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError

from tests.e2e import conftest as cf


class _StubPage:
    def __init__(
        self, *, content: str = "<html>stub</html>", screenshot_raises: bool = False
    ) -> None:
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


def test_capture_failure_writes_html_and_png(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    page = _StubPage(content="<html><body>captured</body></html>")
    summary = cf._capture_failure(page, "stage-3 timeout")
    htmls = list(tmp_path.glob("*.html"))
    pngs = list(tmp_path.glob("*.png"))
    assert htmls and "captured" in htmls[0].read_text()
    assert pngs and pngs[0].read_bytes() == b"PNG-STUB"
    assert isinstance(summary, str) and summary


def test_capture_failure_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    page = _StubPage(screenshot_raises=True)
    summary = cf._capture_failure(page, "stage-3 timeout")
    assert isinstance(summary, str)
    assert list(tmp_path.glob("*.html"))


class _PollStubPage(_StubPage):
    def __init__(
        self,
        pass_results: list[Any],
        fail_results: list[Any] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._pass = list(pass_results)
        self._fail = list(fail_results or [])

    def evaluate(self, expr: str, *args: Any) -> Any:
        if "filter(t => present" in expr:  # the DOM summary JS
            return {"tags": ["home-assistant"], "error_text": None}
        if "FAILCHECK" in expr:
            return self._fail.pop(0) if self._fail else False
        return self._pass.pop(0) if self._pass else False

    def wait_for_load_state(self, *a: Any, **k: Any) -> None:
        return None


def test_pass_check_returns_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    out = cf.wait_for_condition(
        _PollStubPage(pass_results=[True]),
        "PASS true",
        timeout_ms=5000,
        description="t",
    )
    assert out is True
    assert not list(tmp_path.glob("*"))


def test_fail_check_aborts_before_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    page = _PollStubPage(pass_results=[False, False, False], fail_results=[True])
    start = _time.monotonic()
    with pytest.raises(cf.E2EConditionFailed) as ei:
        cf.wait_for_condition(
            page,
            "PASS x",
            fail_check="FAILCHECK y",
            timeout_ms=10000,
            description="panel",
            poll_ms=10,
        )
    assert _time.monotonic() - start < 5
    assert "capture" in str(ei.value)
    assert list(tmp_path.glob("*.html"))


def test_safe_evaluate_captures_on_final_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.e2e import test_ui

    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)

    class _AlwaysDestroyed(_StubPage):
        def evaluate(self, expr: str, *a: Any) -> Any:
            raise PlaywrightError("Execution context was destroyed")

        def wait_for_load_state(self, *a: Any, **k: Any) -> None:
            return None

    with pytest.raises(PlaywrightError):
        test_ui._safe_evaluate(
            _AlwaysDestroyed(), "() => 1", retries=1, settle_timeout_ms=1
        )
    assert list(tmp_path.glob("*.html"))


def test_timeout_captures_dom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    page = _PollStubPage(pass_results=[False] * 50)
    with pytest.raises(cf.E2EConditionTimeout) as ei:
        cf.wait_for_condition(
            page, "PASS x", timeout_ms=300, description="stage3", poll_ms=10
        )
    assert "capture" in str(ei.value)
    assert list(tmp_path.glob("*.html"))


def test_form_input_wait_for_form_captures_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Class-scoped _wait_for_form genuine timeout leaves a DOM capture."""
    from playwright.sync_api import TimeoutError as PwTimeoutError

    from tests.e2e import test_ui

    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    inst = test_ui.TestFormInputPersistence()

    class _TimeoutPage(_StubPage):
        def wait_for_function(self, *a: Any, **k: Any) -> Any:
            raise PwTimeoutError("Page.wait_for_function: Timeout 10000ms exceeded.")

        def wait_for_load_state(self, *a: Any, **k: Any) -> None:
            return None

    with pytest.raises(PwTimeoutError):
        inst._wait_for_form(_TimeoutPage())
    assert list(tmp_path.glob("*.html"))


def test_form_input_safe_evaluate_captures_on_final_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Class-scoped _safe_evaluate final-failure path leaves a DOM capture."""
    from tests.e2e import test_ui

    monkeypatch.setattr(cf, "_failure_capture_dir", lambda: tmp_path)
    inst = test_ui.TestFormInputPersistence()

    class _AlwaysDestroyed(_StubPage):
        def evaluate(self, expr: str, *a: Any) -> Any:
            raise PlaywrightError("Execution context was destroyed")

        def wait_for_load_state(self, *a: Any, **k: Any) -> None:
            return None

    # Stub the recovery helpers so only _safe_evaluate's own retry loop
    # runs (avoids _find_card's real ~30s wait_for_function budget). Each
    # destroyed-context retry calls _recover_form + _wait_for_form; both
    # become no-ops here, so the loop exhausts retries=2 fast and hits the
    # final raise — the capture-before-raise under test.
    monkeypatch.setattr(inst, "_recover_form", lambda *a, **k: None)
    monkeypatch.setattr(inst, "_wait_for_form", lambda *a, **k: None)
    with pytest.raises(PlaywrightError):
        inst._safe_evaluate(_AlwaysDestroyed(), "() => 1")
    assert list(tmp_path.glob("*.html"))
