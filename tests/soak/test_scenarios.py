"""Soak test scenarios: full charge/discharge sessions with realistic profiles.

Each test runs a complete smart charge or discharge session in real time
through the real HA integration + simulator, verifying invariants throughout.

Run all:       pytest tests/soak/ -m soak --tb=short
Single:        pytest tests/soak/test_scenarios.py::test_charge_basic -m soak
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .conftest import (
    LoadProfile,
    ScenarioConfig,
    SoakRecorder,
    SolarProfile,
    check_charge_invariants,
    check_discharge_invariants,
    check_grid_import_during_discharge,
    run_scenario,
)

if TYPE_CHECKING:
    from tests.e2e.conftest import SimulatorHandle
    from tests.e2e.ha_client import HAClient

pytestmark = [pytest.mark.soak, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Charge scenarios
# ---------------------------------------------------------------------------


def test_charge_basic(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Basic charge: 20% -> 80%, 4h window, flat load, no solar."""
    config = ScenarioConfig(
        name="charge_basic",
        session_type="charge",
        window_minutes=240,
        initial_soc=20.0,
        target_soc=80,
        load=LoadProfile(base_kw=0.5),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_with_solar(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Charge with significant solar: exercises D-043 re-deferral.

    Solar peaks at 4kW, which should supplement grid charging and
    push SoC ahead of schedule. The listener should re-defer when ahead.
    """
    config = ScenarioConfig(
        name="charge_with_solar",
        session_type="charge",
        window_minutes=240,
        initial_soc=30.0,
        target_soc=80,
        load=LoadProfile(base_kw=0.5),
        solar=SolarProfile(peak_kw=4.0, sunrise_hour=5.0, sunset_hour=18.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_spiky_load(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Charge with intermittent high load (hot water heater, EV, etc.)."""
    config = ScenarioConfig(
        name="charge_spiky_load",
        session_type="charge",
        window_minutes=240,
        initial_soc=20.0,
        target_soc=80,
        load=LoadProfile(
            base_kw=0.5,
            spikes=[
                (30, 5.0, 15),  # 5kW spike at 30min for 15min
                (90, 3.0, 20),  # 3kW spike at 90min for 20min
                (180, 7.0, 10),  # 7kW spike at 180min for 10min
            ],
        ),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_high_soc_taper(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Charge from 70% to 100%: heavy BMS taper above 90% SoC."""
    config = ScenarioConfig(
        name="charge_high_soc_taper",
        session_type="charge",
        window_minutes=240,
        initial_soc=70.0,
        target_soc=100,
        charge_taper_soc=85.0,
        load=LoadProfile(base_kw=0.3),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_cold_battery(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Charge with cold battery (5C): BMS current limiting."""
    config = ScenarioConfig(
        name="charge_cold_battery",
        session_type="charge",
        window_minutes=240,
        initial_soc=20.0,
        target_soc=80,
        battery_temperature=5.0,
        load=LoadProfile(base_kw=0.5),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_large_battery(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Charge a 42kWh battery: long deferred start, extended charging."""
    config = ScenarioConfig(
        name="charge_large_battery",
        session_type="charge",
        window_minutes=240,
        initial_soc=20.0,
        target_soc=80,
        battery_capacity_kwh=42.0,
        load=LoadProfile(base_kw=1.0),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_solar_exceeds_target(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Solar alone pushes SoC past target during deferral.

    Start at 60% with 6kW solar and target 80%. Solar adds ~5.5kW net
    (after 0.5kW load), reaching 80% in ~22min without grid charging.
    The session should detect target is met and go idle.
    """
    config = ScenarioConfig(
        name="charge_solar_exceeds_target",
        session_type="charge",
        window_minutes=240,
        initial_soc=60.0,
        target_soc=80,
        load=LoadProfile(base_kw=0.5),
        solar=SolarProfile(peak_kw=6.0, sunrise_hour=0.0, sunset_hour=23.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_solar_then_spike(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Solar pushes SoC past target, then a load spike drains it back below.

    Start at 65% with 5kW solar and target 80%. Solar reaches target in
    ~18min. At 60min a 9kW spike lasting 30min drains ~2.7kWh, dropping
    SoC back below 80%. The session should detect SoC fell below target
    and resume charging (or re-defer).
    """
    config = ScenarioConfig(
        name="charge_solar_then_spike",
        session_type="charge",
        window_minutes=240,
        initial_soc=65.0,
        target_soc=80,
        load=LoadProfile(
            base_kw=0.5,
            spikes=[
                (60, 9.0, 30),  # 9kW spike at 60min for 30min
            ],
        ),
        solar=SolarProfile(peak_kw=5.0, sunrise_hour=0.0, sunset_hour=23.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_heavy_load_during_deferral(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """High load drains SoC during deferral, stressing deferred-start recalc.

    Start at 30% with 3kW load. During the deferral phase SoC drops
    significantly, forcing the listener to recalculate an earlier start.
    """
    config = ScenarioConfig(
        name="charge_heavy_load_deferral",
        session_type="charge",
        window_minutes=240,
        initial_soc=30.0,
        target_soc=80,
        load=LoadProfile(base_kw=3.0),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_charge_tight_window(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Window barely fits the charge: tests C-022 unreachable target surfacing.

    20% -> 80% (6kWh) at 10.5kW takes ~34min. A 45-min window leaves
    almost no headroom. The deferred start should be very close to the
    window start, and the target should still be reachable.
    """
    config = ScenarioConfig(
        name="charge_tight_window",
        session_type="charge",
        window_minutes=45,
        initial_soc=20.0,
        target_soc=80,
        load=LoadProfile(base_kw=0.5),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_charge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


# ---------------------------------------------------------------------------
# Discharge scenarios
# ---------------------------------------------------------------------------


def test_discharge_basic(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Basic discharge: 80% -> 20%, 4h window, flat load."""
    config = ScenarioConfig(
        name="discharge_basic",
        session_type="discharge",
        window_minutes=240,
        initial_soc=80.0,
        target_soc=20,
        load=LoadProfile(base_kw=1.5),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_discharge_invariants(soak_recorder, config)
    check_grid_import_during_discharge(soak_recorder)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_discharge_with_solar(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Discharge with solar: solar covers some load, extending battery."""
    config = ScenarioConfig(
        name="discharge_with_solar",
        session_type="discharge",
        window_minutes=240,
        initial_soc=80.0,
        target_soc=20,
        load=LoadProfile(base_kw=2.0),
        solar=SolarProfile(peak_kw=3.0, sunrise_hour=6.0, sunset_hour=18.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_discharge_invariants(soak_recorder, config)
    check_grid_import_during_discharge(soak_recorder)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_discharge_solar_exceeds_load(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Solar exceeds load during discharge: net generation pushes SoC up.

    With 5kW solar and 1.5kW load, net generation is ~3.5kW during peak.
    The discharge controller must handle SoC rising instead of falling
    while still maintaining the session correctly.
    """
    config = ScenarioConfig(
        name="discharge_solar_exceeds_load",
        session_type="discharge",
        window_minutes=240,
        initial_soc=60.0,
        target_soc=20,
        load=LoadProfile(base_kw=1.5),
        solar=SolarProfile(peak_kw=5.0, sunrise_hour=0.0, sunset_hour=23.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_discharge_invariants(soak_recorder, config)
    check_grid_import_during_discharge(soak_recorder)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_discharge_spiky_load(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Discharge with load spikes exceeding max battery power."""
    config = ScenarioConfig(
        name="discharge_spiky_load",
        session_type="discharge",
        window_minutes=240,
        initial_soc=90.0,
        target_soc=15,
        load=LoadProfile(
            base_kw=1.0,
            spikes=[
                (20, 8.0, 10),  # 8kW spike (kitchen appliances)
                (60, 12.0, 5),  # 12kW spike (exceeds inverter max)
                (120, 6.0, 20),  # 6kW sustained
                (200, 4.0, 15),
            ],
        ),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_discharge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_discharge_near_min_soc(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Discharge starting near min_soc: tests end-of-discharge guard (C-017)."""
    config = ScenarioConfig(
        name="discharge_near_min_soc",
        session_type="discharge",
        window_minutes=120,
        initial_soc=25.0,
        target_soc=20,
        load=LoadProfile(base_kw=2.0),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_discharge_invariants(soak_recorder, config)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


def test_discharge_large_battery(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Discharge a 42kWh battery over an extended window."""
    config = ScenarioConfig(
        name="discharge_large_battery",
        session_type="discharge",
        window_minutes=240,
        initial_soc=90.0,
        target_soc=15,
        battery_capacity_kwh=42.0,
        load=LoadProfile(base_kw=2.0),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, config, soak_recorder)
    check_discharge_invariants(soak_recorder, config)
    check_grid_import_during_discharge(soak_recorder)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )


# ---------------------------------------------------------------------------
# Combined scenarios
# ---------------------------------------------------------------------------


def test_charge_then_discharge(
    ha_e2e: HAClient,
    foxess_sim: SimulatorHandle,
    soak_recorder: SoakRecorder,
) -> None:
    """Charge to 90%, then discharge to 20%: full cycle test."""
    charge_config = ScenarioConfig(
        name="cycle_charge",
        session_type="charge",
        window_minutes=180,
        initial_soc=20.0,
        target_soc=90,
        load=LoadProfile(base_kw=0.5),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, charge_config, soak_recorder)
    check_charge_invariants(soak_recorder, charge_config)

    discharge_config = ScenarioConfig(
        name="cycle_discharge",
        session_type="discharge",
        window_minutes=180,
        initial_soc=float(foxess_sim.state()["soc"]),
        target_soc=20,
        load=LoadProfile(base_kw=2.0),
        solar=SolarProfile(peak_kw=0.0),
    )
    run_scenario(ha_e2e, foxess_sim, discharge_config, soak_recorder)
    check_discharge_invariants(soak_recorder, discharge_config)
    check_grid_import_during_discharge(soak_recorder)
    assert not soak_recorder.violations, (
        f"{len(soak_recorder.violations)} violations: "
        + "; ".join(v.detail for v in soak_recorder.violations)
    )
