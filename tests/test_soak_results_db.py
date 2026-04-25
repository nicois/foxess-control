"""Unit tests for soak inflection-point detection and run persistence."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from tests.soak.conftest import InvariantViolation, SoakRecorder, SoakSample
from tests.soak.results_db import detect_inflections, save_run

if TYPE_CHECKING:
    from pathlib import Path


def _sample(
    elapsed_s: float = 0,
    soc: float = 50,
    state: str = "charging",
    power_w: float = 5000,
) -> SoakSample:
    return SoakSample(
        elapsed_s=elapsed_s,
        wall_time="00:00:00",
        soc=soc,
        state=state,
        power_w=power_w,
        grid_import_kw=0,
        grid_export_kw=0,
        solar_kw=0,
        load_kw=0,
        bat_charge_kw=0,
        bat_discharge_kw=0,
    )


class TestDetectInflections:
    def test_empty_and_single(self) -> None:
        assert detect_inflections([]) == []
        assert detect_inflections([_sample()]) == []

    def test_state_change(self) -> None:
        samples = [
            _sample(0, state="deferred"),
            _sample(10, state="deferred"),
            _sample(20, state="charging"),
            _sample(30, state="charging"),
        ]
        events = detect_inflections(samples)
        state_events = [e for e in events if e.event_type == "state_change"]
        assert len(state_events) == 1
        assert state_events[0].detail == "deferred→charging"
        assert state_events[0].elapsed_s == 20

    def test_soc_direction_change_with_deadband(self) -> None:
        samples = [
            _sample(0, soc=50),
            _sample(10, soc=51),
            _sample(20, soc=53),
            _sample(30, soc=52),
            _sample(40, soc=51),
            _sample(50, soc=49),
        ]
        events = detect_inflections(samples)
        soc_events = [e for e in events if e.event_type == "soc_direction"]
        assert len(soc_events) == 1
        assert "falling" in soc_events[0].detail

    def test_soc_jitter_ignored(self) -> None:
        samples = [
            _sample(0, soc=50),
            _sample(10, soc=50.5),
            _sample(20, soc=49.5),
            _sample(30, soc=50.2),
        ]
        events = detect_inflections(samples)
        soc_events = [e for e in events if e.event_type == "soc_direction"]
        assert len(soc_events) == 0

    def test_power_step(self) -> None:
        samples = [
            _sample(0, power_w=0),
            _sample(10, power_w=100),
            _sample(20, power_w=8500),
            _sample(30, power_w=8400),
        ]
        events = detect_inflections(samples)
        power_events = [e for e in events if e.event_type == "power_step"]
        assert len(power_events) == 1
        assert "0W→100W" not in power_events[0].detail
        assert "8500W" in power_events[0].detail

    def test_multiple_event_types(self) -> None:
        samples = [
            _sample(0, soc=50, state="deferred", power_w=0),
            _sample(10, soc=53, state="charging", power_w=8500),
            _sample(20, soc=56, state="charging", power_w=8500),
        ]
        events = detect_inflections(samples)
        types = {e.event_type for e in events}
        assert "state_change" in types
        assert "power_step" in types


class TestSaveRunViolationPersistence:
    """``save_run`` must persist every ``InvariantViolation`` recorded on
    the ``SoakRecorder`` as an event row.

    Regression: a live soak run (2026-04-23, v1.0.12
    ``test_charge_solar_then_spike``) recorded ``violations=3`` on the
    ``runs`` row but zero ``event_type='violation'`` rows in the events
    table.  When a future run fails we only have the counter, not the
    diagnostic detail — violating C-020 (operational transparency).
    """

    def test_violations_persisted_as_events(self, tmp_path: Path) -> None:
        """One event row per violation, with type='violation' and the
        rule + detail preserved in the ``detail`` column."""
        recorder = SoakRecorder(test_name="tests/soak/test_scenarios.py::bogus")
        recorder.samples = [
            _sample(0, soc=80, state="discharging", power_w=3000),
            _sample(60, soc=79, state="discharging", power_w=3000),
        ]
        recorder.violations = [
            InvariantViolation(
                elapsed_s=30.0,
                rule="no_grid_import_during_discharge",
                detail="grid_import_kw=0.42 at soc=79.5",
            ),
            InvariantViolation(
                elapsed_s=45.0,
                rule="soc_monotonic",
                detail="soc jumped 79.0→82.0 unexpectedly",
            ),
        ]
        db_path = tmp_path / "soak_results.db"
        run_id = save_run(
            db_path,
            recorder,
            scenario="bogus",
            started_at="2026-04-26T00:00:00+00:00",
            passed=False,
        )
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT elapsed_s, event_type, detail "
                "FROM events WHERE run_id=? AND event_type='violation' "
                "ORDER BY elapsed_s",
                (run_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 2, (
            f"expected 2 violation event rows, got {len(rows)}: {rows}"
        )
        assert rows[0][0] == 30.0
        assert "no_grid_import_during_discharge" in rows[0][2]
        assert "grid_import_kw=0.42" in rows[0][2]
        assert rows[1][0] == 45.0
        assert "soc_monotonic" in rows[1][2]

    def test_no_violation_events_when_clean(self, tmp_path: Path) -> None:
        """A run with zero violations produces zero violation event rows."""
        recorder = SoakRecorder(test_name="tests/soak/test_scenarios.py::clean")
        recorder.samples = [
            _sample(0, soc=80, state="discharging", power_w=3000),
            _sample(60, soc=75, state="discharging", power_w=3000),
        ]
        db_path = tmp_path / "soak_results.db"
        run_id = save_run(
            db_path,
            recorder,
            scenario="clean",
            started_at="2026-04-26T00:00:00+00:00",
            passed=True,
        )
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id=? AND event_type='violation'",
                (run_id,),
            )
            (count,) = cur.fetchone()
        assert count == 0, "clean run should have no violation events"
