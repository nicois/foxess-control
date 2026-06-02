"""Tests for structured operational-error recording."""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from smart_battery.domain_data import SmartBatteryDomainData
from smart_battery.logging import record_operational_error

if TYPE_CHECKING:
    import pytest


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
