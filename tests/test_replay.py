"""Tests for smart_battery.replay — trace replay harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from smart_battery.events import ALGO_DECISION, EVENT_SCHEMA_VERSION
from smart_battery.replay import Divergence, replay_events, replay_file


def _make_algo_event(algo: str, inputs: dict[str, Any], output: Any) -> dict[str, Any]:
    return {
        "event": ALGO_DECISION,
        "schema_version": EVENT_SCHEMA_VERSION,
        "payload": {"algo": algo, "inputs": inputs, "output": output},
    }


_DISCHARGE_INPUTS: dict[str, Any] = {
    "current_soc": 65.0,
    "min_soc": 10,
    "battery_capacity_kwh": 10.0,
    "remaining_hours": 2.0,
    "max_power_w": 5000,
    "net_consumption_kw": 0.5,
    "headroom": 0.10,
    "consumption_peak_kw": 0.8,
}


class TestReplayEvents:
    def test_matching_trace_has_no_divergences(self) -> None:
        from smart_battery.algorithms import calculate_discharge_power

        expected = calculate_discharge_power(**_DISCHARGE_INPUTS)
        events = [
            _make_algo_event("calculate_discharge_power", _DISCHARGE_INPUTS, expected)
        ]

        report = replay_events(events)

        assert report.ok
        assert report.algo_events == 1
        assert report.replayed == 1
        assert report.divergences == []

    def test_output_mismatch_reported(self) -> None:
        events = [
            _make_algo_event("calculate_discharge_power", _DISCHARGE_INPUTS, 99999)
        ]
        report = replay_events(events)

        assert not report.ok
        assert len(report.divergences) == 1
        d = report.divergences[0]
        assert d.reason == "output_mismatch"
        assert d.recorded_output == 99999
        assert d.replayed_output != 99999

    def test_unknown_algo_reported(self) -> None:
        events = [_make_algo_event("unknown_function", {}, 42)]
        report = replay_events(events)

        assert not report.ok
        assert report.divergences[0].reason == "unknown_algo"
        assert report.replayed == 0

    def test_invalid_inputs_reported(self) -> None:
        bad_inputs = {"not_a_real_param": 1}
        events = [_make_algo_event("calculate_discharge_power", bad_inputs, 0)]
        report = replay_events(events)

        assert not report.ok
        assert report.divergences[0].reason.startswith("invalid_inputs")

    def test_non_algo_events_ignored(self) -> None:
        events = [
            {"event": "tick_snapshot", "payload": {"soc": 50}},
            {"event": "session_transition", "payload": {"state": "started"}},
        ]
        report = replay_events(events)

        assert report.ok
        assert report.total_events == 2
        assert report.algo_events == 0
        assert report.replayed == 0

    def test_mixed_trace_counts_correctly(self) -> None:
        from smart_battery.algorithms import calculate_discharge_power

        expected = calculate_discharge_power(**_DISCHARGE_INPUTS)
        events = [
            {"event": "tick_snapshot", "payload": {"soc": 50}},
            _make_algo_event("calculate_discharge_power", _DISCHARGE_INPUTS, expected),
            {"event": "session_transition", "payload": {"state": "started"}},
            _make_algo_event("calculate_discharge_power", _DISCHARGE_INPUTS, expected),
        ]
        report = replay_events(events)

        assert report.ok
        assert report.total_events == 4
        assert report.algo_events == 2
        assert report.replayed == 2


class TestReplayFile:
    def test_replay_file_roundtrip(self, tmp_path: Path) -> None:
        from smart_battery.algorithms import calculate_discharge_power

        expected = calculate_discharge_power(**_DISCHARGE_INPUTS)
        events = [
            _make_algo_event("calculate_discharge_power", _DISCHARGE_INPUTS, expected),
            _make_algo_event(
                "calculate_discharge_power", _DISCHARGE_INPUTS, expected + 1
            ),
        ]

        trace_file = tmp_path / "trace.jsonl"
        with trace_file.open("w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        report = replay_file(trace_file)

        assert report.algo_events == 2
        assert report.replayed == 2
        assert len(report.divergences) == 1
        assert report.divergences[0].reason == "output_mismatch"

    def test_empty_file_is_ok(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "empty.jsonl"
        trace_file.write_text("")
        report = replay_file(trace_file)
        assert report.ok
        assert report.total_events == 0


class TestReplayRoundTripWithNormalisation:
    """Full capture → serialise → collect → replay cycle with non-primitives."""

    def test_datetime_inputs_survive_round_trip(self, tmp_path: Path) -> None:
        """A calculate_deferred_start event with a datetime ``end`` must
        serialise to JSON, load back, and replay to the same output.
        """
        import datetime as _dt
        import logging

        from smart_battery.algorithms import calculate_deferred_start
        from smart_battery.events import call_algo
        from smart_battery.replay import replay_file

        logger = logging.getLogger("test.roundtrip.datetime")
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        cap = _Capture()
        logger.addHandler(cap)
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
            logger.removeHandler(cap)

        rec = captured[0]
        event = {
            "event": rec.event,  # type: ignore[attr-defined]
            "payload": rec.payload,  # type: ignore[attr-defined]
            "schema_version": rec.schema_version,  # type: ignore[attr-defined]
        }

        trace_file = tmp_path / "trace.jsonl"
        with trace_file.open("w", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

        report = replay_file(trace_file)
        assert report.ok, f"divergences: {report.divergences}"
        assert report.replayed == 1

    def test_taper_profile_input_survives_round_trip(self, tmp_path: Path) -> None:
        import logging

        from smart_battery.algorithms import calculate_charge_power
        from smart_battery.events import call_algo
        from smart_battery.replay import replay_file
        from smart_battery.taper import TaperProfile

        logger = logging.getLogger("test.roundtrip.taper")
        logger.setLevel(logging.DEBUG)
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        cap = _Capture()
        logger.addHandler(cap)
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
            logger.removeHandler(cap)

        rec = captured[0]
        event = {
            "event": rec.event,  # type: ignore[attr-defined]
            "payload": rec.payload,  # type: ignore[attr-defined]
            "schema_version": rec.schema_version,  # type: ignore[attr-defined]
        }

        trace_file = tmp_path / "trace.jsonl"
        with trace_file.open("w", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

        report = replay_file(trace_file)
        assert report.ok, f"divergences: {report.divergences}"
        assert report.replayed == 1


_TRACE_DIR = Path(__file__).parent / "replay_traces"
_TRACE_FILES = sorted(_TRACE_DIR.glob("*.jsonl")) if _TRACE_DIR.exists() else []


@pytest.mark.parametrize("trace_path", _TRACE_FILES, ids=[p.name for p in _TRACE_FILES])
def test_committed_trace_replays_clean(trace_path: Path) -> None:
    """Regression gate: any committed trace must replay to matching outputs.

    Drop a JSONL file into ``tests/replay_traces/`` (collected via
    ``scripts/collect_events.py`` or hand-crafted from a known-good run)
    and this parametrised test will fail if the algorithms ever produce
    different outputs for the recorded inputs — a locked-in protection
    against silent regressions.
    """
    report = replay_file(trace_path)
    assert report.ok, (
        f"{trace_path.name}: {len(report.divergences)} divergences — "
        f"first: {report.divergences[0] if report.divergences else None}"
    )


class TestDivergence:
    def test_divergence_carries_full_context(self) -> None:
        d = Divergence(
            index=3,
            algo="calculate_discharge_power",
            inputs={"x": 1},
            recorded_output=100,
            replayed_output=200,
            reason="output_mismatch",
        )
        assert d.index == 3
        assert d.recorded_output != d.replayed_output
