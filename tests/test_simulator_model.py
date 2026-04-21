"""Tests for InverterModel physics — fdSoc enforcement and efficiency."""

from __future__ import annotations

import pytest

from simulator.model import InverterModel, ScheduleGroup


def _model_with_schedule(
    mode: str,
    fd_soc: int = 80,
    soc: float = 50.0,
    solar_kw: float = 0.0,
    load_kw: float = 0.5,
) -> InverterModel:
    m = InverterModel(fuzzing=False, soc=soc, solar_kw=solar_kw, load_kw=load_kw)
    m.schedule_groups = [
        ScheduleGroup(
            enable=1,
            startHour=0,
            startMinute=0,
            endHour=23,
            endMinute=59,
            workMode=mode,
            fdSoc=fd_soc,
            fdPwr=5000,
        )
    ]
    m.schedule_enabled = True
    return m


class TestFdSocEnforcement:
    """Verify the simulator stops charging/discharging at fdSoc."""

    def test_force_charge_stops_at_fdsoc(self) -> None:
        m = _model_with_schedule("ForceCharge", fd_soc=80, soc=80.0)
        m.tick(60)
        assert m.bat_charge_kw == 0.0

    def test_force_charge_continues_below_fdsoc(self) -> None:
        m = _model_with_schedule("ForceCharge", fd_soc=80, soc=70.0)
        m.tick(60)
        assert m.bat_charge_kw > 0.0

    def test_force_charge_stops_above_fdsoc(self) -> None:
        m = _model_with_schedule("ForceCharge", fd_soc=80, soc=85.0)
        m.tick(60)
        assert m.bat_charge_kw == 0.0

    def test_force_discharge_stops_at_fdsoc(self) -> None:
        m = _model_with_schedule("ForceDischarge", fd_soc=20, soc=20.0)
        m.tick(60)
        assert m.bat_discharge_kw == 0.0

    def test_force_discharge_continues_above_fdsoc(self) -> None:
        m = _model_with_schedule("ForceDischarge", fd_soc=20, soc=50.0)
        m.tick(60)
        assert m.bat_discharge_kw > 0.0

    def test_force_discharge_stops_below_fdsoc(self) -> None:
        m = _model_with_schedule("ForceDischarge", fd_soc=20, soc=15.0)
        m.tick(60)
        assert m.bat_discharge_kw == 0.0

    def test_feedin_stops_at_fdsoc(self) -> None:
        m = _model_with_schedule("Feedin", fd_soc=30, soc=30.0)
        m.tick(60)
        assert m.bat_discharge_kw == 0.0

    def test_feedin_continues_above_fdsoc(self) -> None:
        m = _model_with_schedule("Feedin", fd_soc=30, soc=50.0)
        m.tick(60)
        assert m.bat_discharge_kw > 0.0

    def test_self_use_not_affected_by_fdsoc(self) -> None:
        """SelfUse doesn't use fdSoc — battery discharges to meet load."""
        m = _model_with_schedule("SelfUse", fd_soc=80, soc=50.0, load_kw=2.0)
        m.tick(60)
        assert m.bat_discharge_kw > 0.0

    def test_force_charge_grid_recalculated_at_fdsoc(self) -> None:
        """When charge stops at fdSoc, grid should only supply load."""
        m = _model_with_schedule(
            "ForceCharge", fd_soc=80, soc=80.0, solar_kw=1.0, load_kw=0.5
        )
        m.tick(60)
        assert m.bat_charge_kw == 0.0
        assert m.grid_export_kw == pytest.approx(0.5, abs=0.01)
        assert m.grid_import_kw == 0.0

    def test_force_discharge_grid_recalculated_at_fdsoc(self) -> None:
        """When discharge stops at fdSoc, grid must supply any remaining load."""
        m = _model_with_schedule(
            "ForceDischarge", fd_soc=20, soc=20.0, solar_kw=0.0, load_kw=1.0
        )
        m.tick(60)
        assert m.bat_discharge_kw == 0.0
        assert m.grid_import_kw == pytest.approx(1.0, abs=0.01)

    def test_soc_does_not_change_when_clamped_at_fdsoc(self) -> None:
        """SoC should remain stable when fdSoc clamp is active."""
        m = _model_with_schedule("ForceCharge", fd_soc=80, soc=80.0)
        m.tick(300)
        assert m.soc == pytest.approx(80.0, abs=0.01)


class TestBatteryEfficiency:
    """Verify that the efficiency factor affects energy stored/drawn."""

    def test_default_efficiency_is_lossless(self) -> None:
        m = InverterModel(fuzzing=False)
        assert m.efficiency == 1.0

    def test_charging_stores_less_energy(self) -> None:
        """With 90% efficiency, charging stores 90% of input energy."""
        lossless = _model_with_schedule("ForceCharge", fd_soc=100, soc=50.0)
        lossy = _model_with_schedule("ForceCharge", fd_soc=100, soc=50.0)
        lossy.efficiency = 0.90

        lossless.tick(3600)
        lossy.tick(3600)

        assert lossy.soc < lossless.soc
        soc_ratio = (lossy.soc - 50.0) / (lossless.soc - 50.0)
        assert soc_ratio == pytest.approx(0.90, abs=0.01)

    def test_discharging_draws_more_energy(self) -> None:
        """With 90% efficiency, discharging consumes more battery than delivered."""
        lossless = _model_with_schedule(
            "ForceDischarge", fd_soc=10, soc=80.0, load_kw=0.0
        )
        lossy = _model_with_schedule("ForceDischarge", fd_soc=10, soc=80.0, load_kw=0.0)
        lossy.efficiency = 0.90

        lossless.tick(3600)
        lossy.tick(3600)

        assert lossy.soc < lossless.soc

    def test_efficiency_one_matches_original_behavior(self) -> None:
        """Efficiency=1.0 should produce identical results to pre-efficiency code."""
        m = _model_with_schedule("ForceCharge", fd_soc=100, soc=50.0)
        m.efficiency = 1.0
        m.tick(60)
        fd_pwr_kw = m.schedule_groups[0].fdPwr / 1000.0
        dt_hours = 60.0 / 3600.0
        expected_delta_pct = fd_pwr_kw * dt_hours / m.battery_capacity_kwh * 100.0
        assert m.soc == pytest.approx(50.0 + expected_delta_pct, abs=0.1)


class TestTemperatureTaper:
    """Verify temperature-based charge taper simulation."""

    def test_temp_charge_taper_factor_warm(self) -> None:
        """At 25C (default), temperature factor should be 1.0."""
        m = InverterModel(fuzzing=False, battery_temperature=25.0)
        assert m._temp_charge_taper_factor() == 1.0

    def test_temp_charge_taper_factor_cold(self) -> None:
        """At 0C, temperature factor should be 0.5."""
        m = InverterModel(fuzzing=False, battery_temperature=0.0)
        assert m._temp_charge_taper_factor() == 0.5

    def test_temp_charge_taper_factor_midpoint(self) -> None:
        """At 7.5C (half of 15C threshold), factor should be 0.75."""
        m = InverterModel(fuzzing=False, battery_temperature=7.5)
        expected = 0.5 + 0.5 * (7.5 / 15.0)
        assert m._temp_charge_taper_factor() == pytest.approx(expected, abs=0.01)
        assert m._temp_charge_taper_factor() == pytest.approx(0.75, abs=0.01)

    def test_temp_charge_taper_factor_at_threshold(self) -> None:
        """At exactly 15C, temperature factor should be 1.0."""
        m = InverterModel(fuzzing=False, battery_temperature=15.0)
        assert m._temp_charge_taper_factor() == 1.0

    def test_cold_temp_reduces_charge_in_tick(self) -> None:
        """Set battery_temperature=0, verify bat_charge_kw reduced vs 25C."""
        warm = _model_with_schedule("ForceCharge", fd_soc=100, soc=50.0)
        warm.battery_temperature = 25.0
        warm.tick(60)
        warm_charge = warm.bat_charge_kw

        cold = _model_with_schedule("ForceCharge", fd_soc=100, soc=50.0)
        cold.battery_temperature = 0.0
        cold.tick(60)
        cold_charge = cold.bat_charge_kw

        assert cold_charge < warm_charge
        assert cold_charge == pytest.approx(warm_charge * 0.5, abs=0.01)

    def test_temp_taper_combines_with_soc_taper(self) -> None:
        """Set soc=95 (above charge_taper_soc=90) AND battery_temperature=0."""
        m = _model_with_schedule("ForceCharge", fd_soc=100, soc=95.0)
        m.battery_temperature = 0.0
        m.tick(60)

        # Compute expected tapers
        soc_taper = (100.0 - 95.0) / (100.0 - 90.0)  # 0.5
        temp_taper = 0.5  # at 0C
        combined_taper = soc_taper * temp_taper  # 0.25

        fd_pwr_kw = m.schedule_groups[0].fdPwr / 1000.0
        expected_charge = fd_pwr_kw * combined_taper

        assert m.bat_charge_kw == pytest.approx(expected_charge, abs=0.01)
