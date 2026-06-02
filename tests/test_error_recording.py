"""Tests for structured operational-error recording."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import pytest

from smart_battery.domain_data import SmartBatteryDomainData
from smart_battery.logging import record_operational_error


def test_domain_data_has_bounded_recent_errors_buffer() -> None:
    dd = SmartBatteryDomainData()
    assert isinstance(dd.recent_errors, deque)
    assert dd.recent_errors.maxlen == 30
    dd2 = SmartBatteryDomainData()
    dd.recent_errors.append({"x": 1})
    assert len(dd2.recent_errors) == 0  # no shared mutable default


def test_records_self_sufficient_log_line(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.roe.line")
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    with caplog.at_level(logging.WARNING, logger="test.roe.line"):
        record_operational_error(
            logger,
            buf,
            category="ws_discovery",
            attempted="battery ID discovery via wsmaitian WS",
            exc=ValueError("200, message='Invalid response status'"),
            hint=(
                "server returned HTTP 200 not 101 — possible regional endpoint mismatch"
            ),
        )
    text = caplog.text
    assert "ws_discovery" in text
    assert "battery ID discovery via wsmaitian WS" in text
    assert "ValueError" in text
    assert "Invalid response status" in text
    assert "regional endpoint mismatch" in text


def test_appends_structured_record() -> None:
    logger = logging.getLogger("test.roe.buf")
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    record_operational_error(
        logger,
        buf,
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
    assert isinstance(rec["t"], str) and rec["t"]


def test_respects_buffer_maxlen() -> None:
    logger = logging.getLogger("test.roe.cap")
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    for i in range(35):
        record_operational_error(
            logger, buf, category="c", attempted=f"a{i}", exc=ValueError(str(i))
        )
    assert len(buf) == 30
    assert buf[-1]["exc_str"] == "34"
    assert buf[0]["exc_str"] == "5"


def test_buffer_none_logs_without_crashing(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.roe.none")
    with caplog.at_level(logging.ERROR, logger="test.roe.none"):
        record_operational_error(
            logger,
            None,
            category="unexpected",
            attempted="x",
            exc=RuntimeError("y"),
            severity="error",
        )
    assert "RuntimeError" in caplog.text


def test_severity_maps_to_log_level(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.roe.sev")
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    with caplog.at_level(logging.DEBUG, logger="test.roe.sev"):
        record_operational_error(
            logger,
            buf,
            category="c",
            attempted="a",
            exc=ValueError("z"),
            severity="error",
        )
    assert caplog.records[-1].levelno == logging.ERROR


class _FakeWSConnectSession:
    """Minimal aiohttp.ClientSession stand-in whose ws_connect raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.closed = False

    def ws_connect(self, *args: Any, **kwargs: Any) -> Any:
        raise self._exc


@pytest.mark.asyncio
async def test_battery_id_discovery_records_ws_handshake_error() -> None:
    """A WSServerHandshakeError during discovery records a typed,
    region-hinted error and still returns None (issue #8)."""
    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    from custom_components.foxess_control.foxess.web_session import (
        FoxESSWebSession,
    )

    request_info = aiohttp.RequestInfo(
        URL("wss://eu.foxesscloud.com/dew/v0/wsmaitian"),
        "GET",
        CIMultiDictProxy(CIMultiDict()),
    )
    exc = aiohttp.WSServerHandshakeError(
        request_info=request_info,
        history=(),
        status=200,
        message="Invalid response status",
    )

    buf: deque[dict[str, Any]] = deque(maxlen=30)
    session = _FakeWSConnectSession(exc)
    ws = FoxESSWebSession(
        "testuser",
        "d41d8cd98f00b204e9800998ecf8427e",
        base_url="https://eu.foxesscloud.com",
        session=session,  # type: ignore[arg-type]
    )
    # Avoid a real login; discovery only needs a token string.
    ws._token = "tok"  # noqa: SLF001
    ws._last_login = float("inf")  # noqa: SLF001

    result = await ws.async_discover_battery_id("PLANT123", recent_errors=buf)

    assert result is None
    assert len(buf) == 1
    rec = buf[0]
    assert rec["category"] == "ws_discovery"
    assert rec["exc_type"] == "WSServerHandshakeError"
    assert "regional" in (rec["hint"] or "")
    assert rec["context"]["host"] == "https://eu.foxesscloud.com"
    assert rec["context"]["plant_id"] == "PLANT123"


class _FakeGetCtx:
    """Async-context-manager returned by a fake session's .get()."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> Any:
        raise self._exc

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeGetSession:
    """Minimal aiohttp.ClientSession stand-in whose GET raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.closed = False

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return _FakeGetCtx(self._exc)


def _request_info(url: str) -> Any:
    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    return aiohttp.RequestInfo(URL(url), "GET", CIMultiDictProxy(CIMultiDict()))


@pytest.mark.asyncio
async def test_bms_temp_fetch_records_typed_error_on_client_error() -> None:
    """An aiohttp client error during BMS-temp fetch records a typed,
    bms_temp_fetch error with host context and a hint, and still
    returns None (preserving the existing fallback contract)."""
    import aiohttp

    from custom_components.foxess_control.foxess.web_session import (
        FoxESSWebSession,
    )

    exc = aiohttp.ClientConnectionError("cannot connect to host")

    buf: deque[dict[str, Any]] = deque(maxlen=30)
    session = _FakeGetSession(exc)
    ws = FoxESSWebSession(
        "testuser",
        "d41d8cd98f00b204e9800998ecf8427e",
        base_url="https://eu.foxesscloud.com",
        session=session,  # type: ignore[arg-type]
    )
    ws._token = "tok"  # noqa: SLF001
    ws._last_login = float("inf")  # noqa: SLF001

    result = await ws.async_get_battery_temperature(
        battery_compound_id="BID@SERIAL123",
        recent_errors=buf,
    )

    assert result is None
    assert len(buf) == 1
    rec = buf[0]
    assert rec["category"] == "bms_temp_fetch"
    assert rec["exc_type"] == "ClientConnectionError"
    assert rec["hint"]
    assert rec["context"]["host"] == "https://eu.foxesscloud.com"
    # The raw compound id embeds a serial — must NOT be recorded verbatim.
    assert "SERIAL123" not in str(rec["context"])
