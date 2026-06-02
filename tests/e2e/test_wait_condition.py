"""Tests for wait_for_condition + DOM failure capture (stub page, no container)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.e2e import conftest as cf

if TYPE_CHECKING:
    import pytest


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
