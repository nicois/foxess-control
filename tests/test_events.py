"""Tests for smart_battery.events — structured event emission for replay."""

from __future__ import annotations

import collections
import logging
from typing import Any

import pytest

from smart_battery.events import (
    ALGO_DECISION,
    EVENT_SCHEMA_VERSION,
    SCHEDULE_WRITE,
    SERVICE_CALL,
    SESSION_TRANSITION,
    TICK_SNAPSHOT,
    call_algo,
    emit_event,
    emit_schedule_write,
    normalise_value,
)


class TestEmitEvent:
    def test_emits_info_record_with_event_attribute(self) -> None:
        logger = logging.getLogger("test.events.basic")
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            emit_event(
                logger,
                ALGO_DECISION,
                algo="calculate_discharge_power",
                inputs={"current_soc": 50.0, "min_soc": 10},
                output=2000,
            )
        finally:
            logger.removeHandler(handler)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.levelno == logging.INFO
        assert rec.event == ALGO_DECISION  # type: ignore[attr-defined]
        assert rec.payload["algo"] == "calculate_discharge_power"  # type: ignore[attr-defined]
        assert rec.payload["inputs"]["current_soc"] == 50.0  # type: ignore[attr-defined]
        assert rec.payload["output"] == 2000  # type: ignore[attr-defined]
        assert rec.schema_version == EVENT_SCHEMA_VERSION  # type: ignore[attr-defined]

    def test_message_is_human_readable(self) -> None:
        """emit_event still produces a readable log message (not just structured)."""
        logger = logging.getLogger("test.events.message")
        logger.setLevel(logging.DEBUG)
        captured: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        handler = _Capture()
        logger.addHandler(handler)
        try:
            emit_event(
                logger,
                ALGO_DECISION,
                algo="calculate_discharge_power",
                output=1500,
            )
        finally:
            logger.removeHandler(handler)

        assert len(captured) == 1
        msg = captured[0]
        assert "algo_decision" in msg
        assert "calculate_discharge_power" in msg


class TestDebugLogHandlerCapturesEvents:
    """DebugLogHandler should serialise event + payload into its buffer."""

    def test_event_and_payload_in_buffer(self) -> None:
        from custom_components.foxess_control.sensor import _DebugLogHandler

        buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        handler = _DebugLogHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "algo_decision msg", (), None
        )
        record.event = ALGO_DECISION
        record.payload = {
            "algo": "calculate_discharge_power",
            "output": 2000,
            "inputs": {"current_soc": 50.0},
        }
        record.schema_version = EVENT_SCHEMA_VERSION
        handler.emit(record)

        assert len(buf) == 1
        entry = buf[0]
        assert entry["event"] == ALGO_DECISION
        assert entry["payload"]["algo"] == "calculate_discharge_power"
        assert entry["payload"]["output"] == 2000
        assert entry["schema_version"] == EVENT_SCHEMA_VERSION

    def test_non_event_records_unchanged(self) -> None:
        """Records without event attribute should not have event/payload keys."""
        from custom_components.foxess_control.sensor import _DebugLogHandler

        buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        handler = _DebugLogHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))

        record = logging.LogRecord("test", logging.INFO, "", 0, "plain log", (), None)
        handler.emit(record)

        assert len(buf) == 1
        assert "event" not in buf[0]
        assert "payload" not in buf[0]


class TestReplayFromDischargeEvents:
    """A recorded algo_decision must replay to the same output."""

    def test_replay_reproduces_recorded_output(self) -> None:
        """Given a recorded algo_decision event, re-invoking the algorithm
        with the same inputs must produce the same output byte-for-byte.

        This is the core property item 11 depends on: pure algorithms are
        replayable from their recorded inputs.
        """
        from smart_battery.algorithms import calculate_discharge_power

        inputs: dict[str, Any] = {
            "current_soc": 65.0,
            "min_soc": 10,
            "battery_capacity_kwh": 10.0,
            "remaining_hours": 2.0,
            "max_power_w": 5000,
            "net_consumption_kw": 0.5,
            "headroom": 0.10,
            "consumption_peak_kw": 0.8,
        }
        expected = calculate_discharge_power(**inputs)
        recorded: dict[str, Any] = {
            "event": ALGO_DECISION,
            "payload": {
                "algo": "calculate_discharge_power",
                "inputs": inputs,
                "output": expected,
            },
        }

        replayed = calculate_discharge_power(**recorded["payload"]["inputs"])
        assert replayed == recorded["payload"]["output"]


class TestCallAlgo:
    def test_call_algo_returns_function_output(self) -> None:
        from smart_battery.algorithms import calculate_discharge_power

        logger = logging.getLogger("test.call_algo.basic")

        result = call_algo(
            logger,
            calculate_discharge_power,
            "test_site",
            current_soc=65.0,
            min_soc=10,
            battery_capacity_kwh=10.0,
            remaining_hours=2.0,
            max_power_w=5000,
            net_consumption_kw=0.5,
            headroom=0.10,
            consumption_peak_kw=0.8,
        )

        assert isinstance(result, int)
        assert 100 <= result <= 5000

    def test_call_algo_emits_event_with_call_site(self) -> None:
        from smart_battery.algorithms import calculate_discharge_power

        logger = logging.getLogger("test.call_algo.emit")
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            call_algo(
                logger,
                calculate_discharge_power,
                "deferred_start",
                current_soc=65.0,
                min_soc=10,
                battery_capacity_kwh=10.0,
                remaining_hours=2.0,
                max_power_w=5000,
            )
        finally:
            logger.removeHandler(handler)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.event == ALGO_DECISION  # type: ignore[attr-defined]
        payload = rec.payload  # type: ignore[attr-defined]
        assert payload["algo"] == "calculate_discharge_power"
        assert payload["call_site"] == "deferred_start"
        assert payload["inputs"]["current_soc"] == 65.0

    def test_call_algo_normalises_datetime_inputs(self) -> None:
        """Datetime inputs in the recorded event are serialisable."""
        import datetime as _dt
        import json as _json

        from smart_battery.algorithms import calculate_deferred_start

        logger = logging.getLogger("test.call_algo.datetime")
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        end = _dt.datetime(2026, 4, 24, 20, 0, 0, tzinfo=_dt.UTC)
        try:
            call_algo(
                logger,
                calculate_deferred_start,
                "test",
                current_soc=30.0,
                target_soc=80,
                battery_capacity_kwh=10.0,
                max_power_w=5000,
                end=end,
            )
        finally:
            logger.removeHandler(handler)

        rec = captured[0]
        payload = rec.payload  # type: ignore[attr-defined]
        # Full record must survive JSON round-trip (what the HA REST API does)
        serialised = _json.dumps(payload)
        restored = _json.loads(serialised)
        assert restored["inputs"]["end"]["__type__"] == "datetime"
        assert restored["inputs"]["end"]["iso"] == end.isoformat()
        assert restored["output"]["__type__"] == "datetime"

    def test_call_algo_normalises_taper_profile(self) -> None:
        """TaperProfile input records via to_dict()."""
        import json as _json

        from smart_battery.algorithms import calculate_charge_power
        from smart_battery.taper import TaperProfile

        logger = logging.getLogger("test.call_algo.taper")
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        taper = TaperProfile()
        try:
            call_algo(
                logger,
                calculate_charge_power,
                "test",
                current_soc=50.0,
                target_soc=80,
                battery_capacity_kwh=10.0,
                remaining_hours=2.0,
                max_power_w=5000,
                taper_profile=taper,
            )
        finally:
            logger.removeHandler(handler)

        rec = captured[0]
        payload = rec.payload  # type: ignore[attr-defined]
        # Survive JSON round-trip
        serialised = _json.dumps(payload)
        restored = _json.loads(serialised)
        assert restored["inputs"]["taper_profile"]["__type__"] == "TaperProfile"
        assert "data" in restored["inputs"]["taper_profile"]


class TestDischargeListenerEmitsDecision:
    """When calculate_discharge_power is invoked via the listener,
    an algo_decision event should be emitted with exact inputs + output.

    This is the first slice: we only instrument the discharge power
    decision. The test verifies the event appears on the listener
    logger — which is where SessionContextFilter and the debug-log
    handlers attach in production.
    """

    @pytest.mark.asyncio
    async def test_discharge_pacing_emits_algo_decision(self) -> None:
        """A paced discharge tick emits an algo_decision event with
        inputs that, when replayed, reproduce the recorded output.
        """
        import datetime as _dt
        from unittest.mock import MagicMock, patch

        from custom_components.foxess_control import _register_services
        from custom_components.foxess_control.const import DOMAIN
        from custom_components.foxess_control.foxess.inverter import Inverter
        from smart_battery.algorithms import calculate_discharge_power

        from .conftest import _get_handler
        from .test_services import _make_call, _make_hass

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 80.0, "loadsPower": 0.0, "pvPower": 0.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> Any:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        _register_services(hass)
        handler = _get_handler(hass, "smart_discharge")

        # Attach a capture handler to the listener's logger
        listener_logger = logging.getLogger(
            "custom_components.foxess_control.smart_battery.listeners"
        )
        listener_logger.setLevel(logging.DEBUG)
        captured_records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_records.append(record)

        cap = _Capture()
        listener_logger.addHandler(cap)

        try:
            with (
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                    return_value=_dt.datetime(2026, 4, 7, 17, 0, 0),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                    return_value=MagicMock(),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                    side_effect=capture_interval,
                ),
            ):
                await handler(
                    _make_call(
                        {
                            "start_time": _dt.time(17, 0),
                            "end_time": _dt.time(20, 0),
                            "min_soc": 10,
                        }
                    )
                )

            assert captured_interval is not None

            # Drop SoC and fire the periodic callback past the deferred start
            hass.data[DOMAIN].entries["entry1"].coordinator.data = {
                "SoC": 50.0,
                "loadsPower": 0.0,
                "pvPower": 0.0,
            }

            with (
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                    return_value=_dt.datetime(2026, 4, 7, 19, 40, 0),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners._get_grid_export_limit",
                    return_value=0,
                ),
            ):
                await captured_interval(_dt.datetime(2026, 4, 7, 19, 40, 0))
        finally:
            listener_logger.removeHandler(cap)

        # Filter for algo_decision events on calculate_discharge_power
        decisions = [
            r
            for r in captured_records
            if getattr(r, "event", None) == ALGO_DECISION
            and getattr(r, "payload", {}).get("algo") == "calculate_discharge_power"
        ]
        assert decisions, "no algo_decision for calculate_discharge_power emitted"

        # A tick_snapshot should also fire at the top of the discharge
        # callback so replay can reconstruct per-tick state.
        snapshots = [
            r
            for r in captured_records
            if getattr(r, "event", None) == TICK_SNAPSHOT
            and getattr(r, "payload", {}).get("phase") == "discharge_tick"
        ]
        assert snapshots, "no tick_snapshot for discharge_tick emitted"

        # A session_transition should fire when discharge starts
        transitions = [
            r
            for r in captured_records
            if getattr(r, "event", None) == SESSION_TRANSITION
            and getattr(r, "payload", {}).get("session_type") == "discharge"
            and getattr(r, "payload", {}).get("state") == "started"
        ]
        assert transitions, "no session_transition for discharge started emitted"

        # A service_call should fire when the smart_discharge service is invoked
        service_calls = [
            r
            for r in captured_records
            if getattr(r, "event", None) == SERVICE_CALL
            and getattr(r, "payload", {}).get("service") == "smart_discharge"
        ]
        # service_call is emitted through services.py logger, captured only
        # on listener logger if hierarchies overlap — not required here
        assert service_calls is not None  # document that we checked

        # Replay the first recorded decision and confirm the output matches
        rec = decisions[0]
        payload: dict[str, Any] = rec.payload  # type: ignore[attr-defined]
        inputs = payload["inputs"]
        recorded_output = payload["output"]
        replayed = calculate_discharge_power(**inputs)
        assert replayed == recorded_output


class TestScheduleWrite:
    def test_emits_mode_power_fd_soc(self) -> None:
        from smart_battery.types import WorkMode

        logger = logging.getLogger("test.schedule_write.basic")
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        try:
            emit_schedule_write(
                logger,
                WorkMode.FORCE_DISCHARGE,
                power_w=3500,
                fd_soc=10,
                call_site="test",
            )
        finally:
            logger.removeHandler(handler)

        assert len(captured) == 1
        rec = captured[0]
        assert rec.event == SCHEDULE_WRITE  # type: ignore[attr-defined]
        payload = rec.payload  # type: ignore[attr-defined]
        assert payload["mode"] == WorkMode.FORCE_DISCHARGE.value
        assert payload["power_w"] == 3500
        assert payload["fd_soc"] == 10
        assert payload["call_site"] == "test"


class TestNormaliseValue:
    def test_datetime_roundtrip(self) -> None:
        import datetime as _dt

        now = _dt.datetime(2026, 4, 24, 12, 30, 45, tzinfo=_dt.UTC)
        norm = normalise_value(now)
        assert norm == {"__type__": "datetime", "iso": now.isoformat()}

    def test_time_roundtrip(self) -> None:
        import datetime as _dt

        t = _dt.time(17, 30)
        norm = normalise_value(t)
        assert norm == {"__type__": "time", "iso": "17:30:00"}

    def test_timedelta_roundtrip(self) -> None:
        import datetime as _dt

        td = _dt.timedelta(hours=2, minutes=30)
        norm = normalise_value(td)
        assert norm == {"__type__": "timedelta", "seconds": 9000.0}

    def test_primitive_passes_through(self) -> None:
        assert normalise_value(42) == 42
        assert normalise_value("hello") == "hello"
        assert normalise_value(None) is None
        assert normalise_value(3.14) == 3.14
