"""Unit tests for soak inflection-point detection."""

from __future__ import annotations

from tests.soak.conftest import SoakSample
from tests.soak.results_db import detect_inflections


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
