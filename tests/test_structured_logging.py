"""Tests for smart_battery.logging — structured session context."""

from __future__ import annotations

import collections
import logging
from typing import Any

from smart_battery.logging import (
    SessionContextFilter,
    install_session_filter,
    remove_session_filter,
)


def _make_charge_state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": "charge-uuid-1234",
        "target_soc": 80.0,
        "max_power_w": 3000,
        "last_power_w": 1500,
        "charging_started": True,
        "soc_unavailable_count": 0,
    }
    base.update(overrides)
    return base


def _make_discharge_state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": "discharge-uuid-5678",
        "min_soc": 10.0,
        "max_power_w": 5000,
        "last_power_w": 2500,
        "discharging_started": True,
        "suspended": False,
        "consumption_peak_kw": 1.2,
        "soc_unavailable_count": 0,
    }
    base.update(overrides)
    return base


class TestSessionContextFilter:
    def test_injects_charge_session_context(self) -> None:
        charge = _make_charge_state()
        f = SessionContextFilter(lambda: (charge, None))
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        assert f.filter(record) is True
        assert record.session["session_type"] == "charge"  # type: ignore[attr-defined]
        assert record.session["session_id"] == "charge-uuid-1234"  # type: ignore[attr-defined]
        assert record.session["target_soc"] == 80.0  # type: ignore[attr-defined]

    def test_injects_discharge_session_context(self) -> None:
        discharge = _make_discharge_state()
        f = SessionContextFilter(lambda: (None, discharge))
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        assert f.filter(record) is True
        assert record.session["session_type"] == "discharge"  # type: ignore[attr-defined]
        assert record.session["session_id"] == "discharge-uuid-5678"  # type: ignore[attr-defined]
        assert record.session["min_soc"] == 10.0  # type: ignore[attr-defined]
        assert record.session["consumption_peak_kw"] == 1.2  # type: ignore[attr-defined]

    def test_empty_dict_when_no_session_active(self) -> None:
        f = SessionContextFilter(lambda: (None, None))
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        assert f.filter(record) is True
        assert record.session == {}  # type: ignore[attr-defined]

    def test_never_suppresses_records(self) -> None:
        for getter in [
            lambda: (None, None),
            lambda: (_make_charge_state(), None),
            lambda: (None, _make_discharge_state()),
            lambda: (_make_charge_state(), _make_discharge_state()),
        ]:
            f = SessionContextFilter(getter)
            record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
            assert f.filter(record) is True

    def test_discharge_wins_session_type_when_both_active(self) -> None:
        f = SessionContextFilter(
            lambda: (_make_charge_state(), _make_discharge_state())
        )
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        f.filter(record)
        assert record.session["session_type"] == "discharge"  # type: ignore[attr-defined]
        assert record.session["session_id"] == "discharge-uuid-5678"  # type: ignore[attr-defined]

    def test_survives_getter_exception(self) -> None:
        def bad_getter() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            raise RuntimeError("boom")

        f = SessionContextFilter(bad_getter)
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        assert f.filter(record) is True
        assert record.session == {}  # type: ignore[attr-defined]

    def test_missing_fields_are_skipped(self) -> None:
        sparse = {"session_id": "sparse-uuid"}
        f = SessionContextFilter(lambda: (sparse, None))
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        f.filter(record)
        assert record.session["session_id"] == "sparse-uuid"  # type: ignore[attr-defined]
        assert "target_soc" not in record.session  # type: ignore[attr-defined]


class TestInstallRemove:
    def test_roundtrip(self) -> None:
        logger = logging.getLogger("test.structured.roundtrip")
        getter = lambda: (None, None)  # noqa: E731
        f = install_session_filter(logger, getter)
        assert f in logger.filters
        remove_session_filter(logger, f)
        assert f not in logger.filters

    def test_filter_enriches_records_through_logger(self) -> None:
        logger = logging.getLogger("test.structured.enrich")
        logger.setLevel(logging.DEBUG)
        charge = _make_charge_state()
        f = install_session_filter(logger, lambda: (charge, None))

        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            logger.info("test enrichment")
            assert len(captured) == 1
            assert captured[0].session["session_id"] == "charge-uuid-1234"  # type: ignore[attr-defined]
        finally:
            logger.removeHandler(handler)
            remove_session_filter(logger, f)


class TestDebugLogHandlerWithSession:
    def test_handler_includes_session_in_buffer(self) -> None:
        from custom_components.foxess_control.sensor import _DebugLogHandler

        buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        handler = _DebugLogHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "session test", (), None
        )
        record.session = {"session_type": "charge", "session_id": "abc"}
        handler.emit(record)

        assert len(buf) == 1
        entry = buf[0]
        assert entry["session"]["session_type"] == "charge"
        assert entry["session"]["session_id"] == "abc"

    def test_handler_omits_session_when_empty(self) -> None:
        from custom_components.foxess_control.sensor import _DebugLogHandler

        buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        handler = _DebugLogHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord("test", logging.INFO, "", 0, "no session", (), None)
        handler.emit(record)

        assert len(buf) == 1
        assert "session" not in buf[0]

    def test_handler_omits_session_when_attr_is_empty_dict(self) -> None:
        from custom_components.foxess_control.sensor import _DebugLogHandler

        buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        handler = _DebugLogHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "empty session", (), None
        )
        record.session = {}
        handler.emit(record)

        assert len(buf) == 1
        assert "session" not in buf[0]
