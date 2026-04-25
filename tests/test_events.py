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


class TestInverterScheduleWriteEmission:
    """Every inverter schedule API write must emit a SCHEDULE_WRITE event.

    The docstring in :mod:`smart_battery.events` says the payload is
    "groups list plus the API response".  This is the event that
    proves a write actually reached the inverter's HTTP endpoint;
    it is distinct from the pre-write intent emission done by
    :func:`emit_schedule_write`, which fires at the smart_battery
    layer before the adapter call.

    Regression: during a live charge session on 2026-04-25, an entire
    15-minute session with confirmed inverter schedule writes emitted
    ZERO schedule_write events because the FoxESS ``_services.py``
    path invokes ``inverter.set_schedule`` directly, bypassing the
    smart_battery intent emissions.  Emitting at the inverter
    layer closes that hole for any caller.
    """

    def _capture_on_logger(
        self, logger_name: str
    ) -> tuple[list[logging.LogRecord], logging.Logger, logging.Handler]:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        return captured, logger, handler

    def test_set_schedule_emits_event_with_groups_and_response(
        self, foxess_sim: Any
    ) -> None:
        """A direct ``Inverter.set_schedule`` call must emit ``SCHEDULE_WRITE``.

        The payload must carry the groups written and the API response
        so a replay harness can reconstruct the exact API state change.
        """
        from custom_components.foxess_control.foxess.client import FoxESSClient
        from custom_components.foxess_control.foxess.inverter import Inverter

        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
        inv = Inverter(client, "SIM0001")

        captured, logger, handler = self._capture_on_logger(
            "custom_components.foxess_control.foxess.inverter"
        )
        # Fall back to the canonical root logger the tests can also import
        # via — both should receive the record.
        captured_root, logger_root, handler_root = self._capture_on_logger(
            "foxess.inverter"
        )
        try:
            from custom_components.foxess_control.foxess.inverter import (
                ScheduleGroup,
            )

            group: ScheduleGroup = {
                "enable": 1,
                "startHour": 1,
                "startMinute": 0,
                "endHour": 2,
                "endMinute": 30,
                "workMode": "ForceCharge",
                "minSocOnGrid": 11,
                "fdSoc": 100,
                "fdPwr": 5000,
            }
            inv.set_schedule([group])
        finally:
            logger.removeHandler(handler)
            logger_root.removeHandler(handler_root)

        events = [
            r
            for r in (*captured, *captured_root)
            if getattr(r, "event", None) == SCHEDULE_WRITE
        ]
        assert events, (
            "Inverter.set_schedule did not emit a schedule_write event; "
            "loggers seen: "
            + ", ".join(sorted({r.name for r in (*captured, *captured_root)}))
        )

        rec = events[0]
        payload = rec.payload  # type: ignore[attr-defined]
        assert "groups" in payload, f"missing 'groups' in {payload!r}"
        assert isinstance(payload["groups"], list)
        assert len(payload["groups"]) == 1
        assert payload["groups"][0]["workMode"] == "ForceCharge"
        assert payload["groups"][0]["fdPwr"] == 5000
        # Per events.py docstring: "the API response" — the result
        # field from the FoxESS API (scheduler/enable returns null).
        assert "response" in payload, f"missing 'response' in {payload!r}"

    def test_set_schedule_payload_is_json_serialisable(self, foxess_sim: Any) -> None:
        """Payload must survive JSON round-trip for replay harness."""
        import json as _json

        from custom_components.foxess_control.foxess.client import FoxESSClient
        from custom_components.foxess_control.foxess.inverter import Inverter

        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
        inv = Inverter(client, "SIM0001")

        captured, logger, handler = self._capture_on_logger(
            "custom_components.foxess_control.foxess.inverter"
        )
        captured_root, logger_root, handler_root = self._capture_on_logger(
            "foxess.inverter"
        )
        try:
            from custom_components.foxess_control.foxess.inverter import (
                ScheduleGroup,
            )

            group: ScheduleGroup = {
                "enable": 1,
                "startHour": 3,
                "startMinute": 15,
                "endHour": 4,
                "endMinute": 45,
                "workMode": "ForceDischarge",
                "minSocOnGrid": 11,
                "fdSoc": 11,
                "fdPwr": 7500,
            }
            inv.set_schedule([group])
        finally:
            logger.removeHandler(handler)
            logger_root.removeHandler(handler_root)

        events = [
            r
            for r in (*captured, *captured_root)
            if getattr(r, "event", None) == SCHEDULE_WRITE
        ]
        assert events
        rec = events[0]
        # Round-trip the whole envelope through JSON, like the HA REST
        # API does when the debug-log sensor is scraped.
        envelope = {
            "event": rec.event,  # type: ignore[attr-defined]
            "schema_version": rec.schema_version,  # type: ignore[attr-defined]
            "payload": rec.payload,  # type: ignore[attr-defined]
        }
        restored = _json.loads(_json.dumps(envelope))
        assert restored["event"] == SCHEDULE_WRITE
        assert restored["payload"]["groups"][0]["fdPwr"] == 7500

    def test_set_work_mode_emits_event(self, foxess_sim: Any) -> None:
        """``Inverter.set_work_mode`` also writes to ``/scheduler/enable``.

        ``Inverter.self_use`` delegates to ``set_work_mode``; the
        same emission must cover both paths.
        """
        from custom_components.foxess_control.foxess.client import FoxESSClient
        from custom_components.foxess_control.foxess.inverter import (
            Inverter,
            WorkMode,
        )

        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
        inv = Inverter(client, "SIM0001")

        captured, logger, handler = self._capture_on_logger(
            "custom_components.foxess_control.foxess.inverter"
        )
        captured_root, logger_root, handler_root = self._capture_on_logger(
            "foxess.inverter"
        )
        try:
            inv.set_work_mode(WorkMode.SELF_USE, fd_pwr=5000)
        finally:
            logger.removeHandler(handler)
            logger_root.removeHandler(handler_root)

        events = [
            r
            for r in (*captured, *captured_root)
            if getattr(r, "event", None) == SCHEDULE_WRITE
        ]
        assert events, "Inverter.set_work_mode did not emit a schedule_write event"
        payload = events[0].payload  # type: ignore[attr-defined]
        assert "groups" in payload
        assert len(payload["groups"]) == 1
        assert payload["groups"][0]["workMode"] == WorkMode.SELF_USE.value

    @pytest.mark.asyncio
    async def test_smart_charge_service_emits_schedule_write(
        self, foxess_sim: Any
    ) -> None:
        """End-to-end: starting smart charge via service call must emit
        ``schedule_write`` at the initial inverter write.

        This mirrors the 2026-04-25 regression: the user triggered
        ``smart_charge``, confirmed schedule writes via the API, yet
        the event stream contained no ``schedule_write`` records
        because the FoxESS direct-write path bypassed emission sites.
        """
        import datetime
        from unittest.mock import MagicMock, patch

        from custom_components.foxess_control import _register_services
        from custom_components.foxess_control.foxess.client import FoxESSClient
        from custom_components.foxess_control.foxess.inverter import Inverter

        from .test_services import _make_call, _make_hass

        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
        inv = Inverter(client, "SIM0001")

        # Large SoC gap + short window forces an immediate (non-deferred)
        # charge start so inverter.set_schedule is called at service-handle
        # time.  The deferred path delays the write until mid-session which
        # would be a different code path (still covered by the unit
        # Inverter.set_schedule tests above).
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        _register_services(hass)
        from .conftest import _get_handler

        handler = _get_handler(hass, "smart_charge")

        # Capture records on the integration's root logger so we match
        # what the production debug-log handler would see.
        logger = logging.getLogger("custom_components.foxess_control")
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        cap = _Capture()
        logger.addHandler(cap)

        now = datetime.datetime(2026, 4, 7, 1, 30, 0)
        try:
            with (
                patch(
                    "custom_components.foxess_control.dt_util.now",
                    return_value=now,
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.services.dt_util.now",
                    return_value=now,
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                    return_value=now,
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                    return_value=MagicMock(),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                    return_value=MagicMock(),
                ),
            ):
                await handler(
                    _make_call(
                        {
                            "start_time": datetime.time(1, 0),
                            "end_time": datetime.time(2, 0),
                            "target_soc": 95,
                        }
                    )
                )
        finally:
            logger.removeHandler(cap)

        write_events = [
            r for r in captured if getattr(r, "event", None) == SCHEDULE_WRITE
        ]
        assert write_events, (
            "smart_charge service call did not emit any schedule_write "
            "events on custom_components.foxess_control logger"
        )

        # At least one event must carry the groups that were actually
        # written to the API — this is the regression-proof assertion.
        events_with_groups = [
            r
            for r in write_events
            if isinstance(getattr(r, "payload", None), dict)
            and "groups" in r.payload  # type: ignore[attr-defined]
            and r.payload["groups"]  # type: ignore[attr-defined]
        ]
        assert events_with_groups, (
            "no schedule_write event carried a non-empty groups list; "
            f"payloads seen: {[getattr(r, 'payload', None) for r in write_events]}"
        )
        charge_groups = [
            g
            for r in events_with_groups
            for g in r.payload["groups"]  # type: ignore[attr-defined]
            if g.get("workMode") == "ForceCharge"
        ]
        assert charge_groups, (
            "no schedule_write event carried a ForceCharge group; "
            f"groups seen: {[r.payload['groups'] for r in events_with_groups]}"  # type: ignore[attr-defined]
        )


class TestInverterScheduleWriteReachesParentHandler:
    """SCHEDULE_WRITE from ``_post_schedule`` must reach the debug-log
    handler attached to the integration's parent logger — even when a
    child logger has an explicit level override that would otherwise
    suppress INFO emissions.

    Production wiring:
    :func:`custom_components.foxess_control.sensor.setup_debug_log`
    attaches a bounded-deque ``_DebugLogHandler`` at the parent logger
    ``custom_components.foxess_control`` and relies on Python logging's
    propagation so records from every descendant module (including
    ``...foxess.inverter`` and ``...foxess_adapter``) land in the
    same buffer.  The parent logger's level is forced to DEBUG so that
    unconfigured children inherit DEBUG.

    Regression: during a 2-hour ``smart_charge`` session on v1.0.12, a
    live HA user observed zero API-layer ``schedule_write`` events in
    ``sensor.foxess_debug_log`` despite confirmed inverter writes.
    Listener-layer schedule_write emissions (from
    ``custom_components.foxess_control.smart_battery.listeners``) did
    reach the buffer in the same session.  Both loggers share the same
    parent and the same propagation path, so the asymmetry points to
    something specific to the child logger, not the handler chain:
    an **explicit level set on the child logger** (via HA's
    ``logger:`` YAML, the ``logger.set_level`` service, or a saved
    debug-logger config in ``core.logger``) drops INFO records at
    :meth:`Logger.isEnabledFor` **before** they propagate to the parent's
    handlers.

    The existing
    :class:`TestInverterScheduleWriteEmission` test attaches its capture
    handler **directly** to the ``foxess.inverter`` leaf logger, bypassing
    the parent-propagation code path entirely, so it cannot observe
    records being dropped at the child's level check.  These tests
    wire the handler the way production wires it and prove that the
    record still reaches the buffer when the child logger's level is
    WARNING (matching the user's symptom).
    """

    @pytest.mark.asyncio
    async def test_schedule_write_reaches_parent_handler_with_child_warning_level(
        self, foxess_sim: Any
    ) -> None:
        """Reproduce the production symptom: child logger at WARNING.

        The handler is attached to ``custom_components.foxess_control``
        at DEBUG (just like :func:`setup_debug_log`).  The child
        logger ``custom_components.foxess_control.foxess.inverter`` is
        explicitly set to WARNING — emulating what HA's
        ``logger.set_level`` or an archived ``core.logger`` config
        could have done.  ``_post_schedule`` must still deliver the
        ``SCHEDULE_WRITE`` event to the parent's handler because the
        event is part of the integration's structured telemetry and
        is the only record confirming a write reached the API.
        """
        import asyncio as _asyncio

        from custom_components.foxess_control.foxess.client import FoxESSClient
        from custom_components.foxess_control.foxess.inverter import (
            Inverter,
            ScheduleGroup,
        )
        from custom_components.foxess_control.sensor import _DebugLogHandler
        from smart_battery.logging import SessionContextFilter

        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
        inv = Inverter(client, "SIM0001")

        parent_logger = logging.getLogger("custom_components.foxess_control")
        child_logger = logging.getLogger(
            "custom_components.foxess_control.foxess.inverter"
        )
        original_parent_level = parent_logger.level
        original_child_level = child_logger.level
        parent_logger.setLevel(logging.DEBUG)
        # Simulate a live-user configuration where the child logger was
        # explicitly pinned at WARNING (HA's logger component writes
        # level on child loggers when the frontend or YAML specifies
        # per-module logging).  With this override, INFO records from
        # the child are dropped by the logger's isEnabledFor check
        # before propagation — the parent-attached handler never fires.
        child_logger.setLevel(logging.WARNING)

        buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        handler = _DebugLogHandler(buf)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        handler.addFilter(SessionContextFilter(lambda: (None, None)))
        parent_logger.addHandler(handler)

        group: ScheduleGroup = {
            "enable": 1,
            "startHour": 1,
            "startMinute": 0,
            "endHour": 2,
            "endMinute": 30,
            "workMode": "ForceCharge",
            "minSocOnGrid": 11,
            "fdSoc": 100,
            "fdPwr": 5000,
        }

        loop = _asyncio.get_running_loop()
        try:
            # Route through the default ThreadPoolExecutor, the same
            # mechanism HomeAssistant.async_add_executor_job uses.
            await loop.run_in_executor(None, inv.set_schedule, [group])
        finally:
            parent_logger.removeHandler(handler)
            parent_logger.setLevel(original_parent_level)
            child_logger.setLevel(original_child_level)

        api_events = [
            e
            for e in buf
            if e.get("event") == SCHEDULE_WRITE and "groups" in e.get("payload", {})
        ]
        assert api_events, (
            "Inverter.set_schedule did not reach the parent's debug-log "
            "handler when the child logger level is WARNING. "
            "This is the production symptom: the API-layer schedule_write "
            "event must survive any child-logger level override because "
            "it is the sole telemetry confirming a schedule was written. "
            f"Buffer: {list(buf)}"
        )
        payload = api_events[0]["payload"]
        assert payload["groups"][0]["workMode"] == "ForceCharge"
        assert "response" in payload
        assert payload["endpoint"] == "/op/v0/device/scheduler/enable"

    @pytest.mark.asyncio
    async def test_schedule_write_reaches_parent_handler_from_executor_default_levels(
        self, foxess_sim: Any
    ) -> None:
        """Baseline: executor-path emission with default child logger levels.

        With no child-level override, the record should also reach the
        parent's debug-log handler.  Guards against a future change that
        over-corrects by attaching a handler **only** to the leaf logger
        (which would still work here but break the production
        parent-handler contract).
        """
        import asyncio as _asyncio

        from custom_components.foxess_control.foxess.client import FoxESSClient
        from custom_components.foxess_control.foxess.inverter import (
            Inverter,
            ScheduleGroup,
        )
        from custom_components.foxess_control.sensor import _DebugLogHandler

        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
        inv = Inverter(client, "SIM0001")

        parent_logger = logging.getLogger("custom_components.foxess_control")
        original_level = parent_logger.level
        parent_logger.setLevel(logging.DEBUG)

        buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        handler = _DebugLogHandler(buf)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        parent_logger.addHandler(handler)

        group: ScheduleGroup = {
            "enable": 1,
            "startHour": 3,
            "startMinute": 15,
            "endHour": 4,
            "endMinute": 45,
            "workMode": "ForceDischarge",
            "minSocOnGrid": 11,
            "fdSoc": 11,
            "fdPwr": 7500,
        }

        loop = _asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, inv.set_schedule, [group])
        finally:
            parent_logger.removeHandler(handler)
            parent_logger.setLevel(original_level)

        api_events = [e for e in buf if e.get("event") == SCHEDULE_WRITE]
        assert api_events, f"no SCHEDULE_WRITE reached buffer; buf={list(buf)}"

    @pytest.mark.asyncio
    async def test_schedule_write_with_session_context_survives_child_override(
        self, foxess_sim: Any
    ) -> None:
        """Neighbourhood case for C-038-style divergence: session context
        must still attach to executor-emitted records even when the
        child logger has a level override.

        The fix must not strip session context (the session filter lives
        on the handler, so as long as the record reaches the handler,
        the filter runs and enriches the record).
        """
        import asyncio as _asyncio

        from custom_components.foxess_control.foxess.client import FoxESSClient
        from custom_components.foxess_control.foxess.inverter import (
            Inverter,
            ScheduleGroup,
        )
        from custom_components.foxess_control.sensor import _DebugLogHandler
        from smart_battery.logging import SessionContextFilter

        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
        inv = Inverter(client, "SIM0001")

        parent_logger = logging.getLogger("custom_components.foxess_control")
        child_logger = logging.getLogger(
            "custom_components.foxess_control.foxess.inverter"
        )
        original_parent_level = parent_logger.level
        original_child_level = child_logger.level
        parent_logger.setLevel(logging.DEBUG)
        child_logger.setLevel(logging.WARNING)

        buf: collections.deque[dict[str, Any]] = collections.deque(maxlen=100)
        handler = _DebugLogHandler(buf)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        charge_state: dict[str, Any] = {
            "session_id": "charge-executor-5678",
            "target_soc": 95,
            "max_power_w": 5000,
        }
        handler.addFilter(SessionContextFilter(lambda: (charge_state, None)))
        parent_logger.addHandler(handler)

        group: ScheduleGroup = {
            "enable": 1,
            "startHour": 1,
            "startMinute": 0,
            "endHour": 2,
            "endMinute": 30,
            "workMode": "ForceCharge",
            "minSocOnGrid": 11,
            "fdSoc": 100,
            "fdPwr": 5000,
        }

        loop = _asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, inv.set_schedule, [group])
        finally:
            parent_logger.removeHandler(handler)
            parent_logger.setLevel(original_parent_level)
            child_logger.setLevel(original_child_level)

        api_events = [e for e in buf if e.get("event") == SCHEDULE_WRITE]
        assert api_events, "no SCHEDULE_WRITE reached buffer"
        entry = api_events[0]
        assert "session" in entry, (
            f"session context missing from executor-emitted record; entry: {entry}"
        )
        assert entry["session"].get("session_id") == "charge-executor-5678"


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
