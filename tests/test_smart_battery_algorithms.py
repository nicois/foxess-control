"""Tests for smart_battery.algorithms — pure pacing functions.

These tests verify that the extracted shared algorithms produce
identical results to the original foxess_control implementations.
"""

from __future__ import annotations

import datetime

from custom_components.foxess_control.smart_battery.algorithms import (
    calculate_charge_power,
    calculate_deferred_start,
    calculate_discharge_deferred_start,
    calculate_discharge_power,
    is_charge_target_reachable,
    should_suspend_discharge,
    soc_energy_kwh,
)
from custom_components.foxess_control.smart_battery.taper import TaperBin, TaperProfile


class TestSocEnergyKwh:
    def test_full_charge(self) -> None:
        assert soc_energy_kwh(100.0, 10.0) == 10.0

    def test_half_charge(self) -> None:
        assert soc_energy_kwh(50.0, 10.0) == 5.0

    def test_zero(self) -> None:
        assert soc_energy_kwh(0.0, 10.0) == 0.0


class TestCalculateChargePower:
    def test_basic_calculation(self) -> None:
        result = calculate_charge_power(50.0, 100, 10.0, 2.0, 10000)
        assert result == 3055

    def test_result_is_int(self) -> None:
        result = calculate_charge_power(50.0, 100, 10.0, 3.0, 10000)
        assert isinstance(result, int)

    def test_clamped_to_min(self) -> None:
        result = calculate_charge_power(99.0, 100, 10.0, 4.0, 10000)
        assert result == 100

    def test_clamped_to_max(self) -> None:
        result = calculate_charge_power(0.0, 100, 20.0, 0.5, 5000)
        assert result == 5000

    def test_zero_remaining_hours(self) -> None:
        result = calculate_charge_power(50.0, 100, 10.0, 0.0, 8000)
        assert result == 8000

    def test_negative_remaining_hours(self) -> None:
        result = calculate_charge_power(50.0, 100, 10.0, -1.0, 8000)
        assert result == 8000

    def test_soc_at_target(self) -> None:
        result = calculate_charge_power(80.0, 80, 10.0, 2.0, 10000)
        assert result == 100

    def test_soc_above_target(self) -> None:
        result = calculate_charge_power(90.0, 80, 10.0, 2.0, 10000)
        assert result == 100

    def test_consumption_increases_power(self) -> None:
        result = calculate_charge_power(
            50.0, 100, 10.0, 2.0, 10000, net_consumption_kw=1.5
        )
        assert result == 4705

    def test_consumption_clamped_to_max(self) -> None:
        result = calculate_charge_power(
            50.0, 100, 10.0, 2.0, 10000, net_consumption_kw=8.0
        )
        assert result == 10000

    def test_negative_consumption_ignored(self) -> None:
        base = calculate_charge_power(50.0, 100, 10.0, 2.0, 10000)
        with_solar = calculate_charge_power(
            50.0, 100, 10.0, 2.0, 10000, net_consumption_kw=-3.0
        )
        assert with_solar == base


class TestCalculateChargePowerTrajectory:
    """Tests for trajectory tracking when charging_started_energy_kwh is provided."""

    def test_behind_schedule_returns_max(self) -> None:
        # Started at 5kWh (50%), target 10kWh (100%), 10kWh capacity
        # Elapsed 0.5h of 1.8h effective window → progress ~28%
        # Ideal: 5 + 0.28 * 5 = 6.39 kWh. Actual at 50% = 5kWh → behind
        result = calculate_charge_power(
            50.0,
            100,
            10.0,
            1.5,
            10000,
            charging_started_energy_kwh=5.0,
            elapsed_since_charge_started=0.5,
            effective_charge_window=2.0,
            min_power_change_w=500,
        )
        assert result == 10000

    def test_ahead_of_schedule_paces_normally(self) -> None:
        # Progressed well — actual energy is above ideal trajectory
        result = calculate_charge_power(
            85.0,
            100,
            10.0,
            1.5,
            10000,
            charging_started_energy_kwh=5.0,
            elapsed_since_charge_started=0.5,
            effective_charge_window=2.0,
            min_power_change_w=500,
        )
        assert result < 10000

    def test_no_trajectory_without_started_energy(self) -> None:
        # No trajectory tracking — should pace normally
        result = calculate_charge_power(50.0, 100, 10.0, 2.0, 10000)
        assert result < 10000


class TestCalculateDeferredStart:
    def test_basic_deferral(self) -> None:
        end = datetime.datetime(2025, 1, 1, 6, 0)
        result = calculate_deferred_start(50.0, 100, 10.0, 5000, end)
        assert result < end

    def test_no_charging_needed(self) -> None:
        end = datetime.datetime(2025, 1, 1, 6, 0)
        result = calculate_deferred_start(100.0, 100, 10.0, 5000, end)
        assert result == end

    def test_soc_above_target(self) -> None:
        end = datetime.datetime(2025, 1, 1, 6, 0)
        result = calculate_deferred_start(90.0, 80, 10.0, 5000, end)
        assert result == end

    def test_clamped_to_start(self) -> None:
        start = datetime.datetime(2025, 1, 1, 1, 0)
        end = datetime.datetime(2025, 1, 1, 2, 0)
        # Very large energy need in short window → deferred would be before start
        result = calculate_deferred_start(0.0, 100, 100.0, 5000, end, start=start)
        assert result == start

    def test_consumption_affects_deferral(self) -> None:
        end = datetime.datetime(2025, 1, 1, 6, 0)
        no_load = calculate_deferred_start(50.0, 100, 10.0, 5000, end)
        with_load = calculate_deferred_start(
            50.0, 100, 10.0, 5000, end, net_consumption_kw=2.0
        )
        # With load, effective charge rate is lower → needs to start earlier
        assert with_load < no_load

    def test_taper_consumption_affects_deferral(self) -> None:
        """Taper path must also account for consumption headroom (D-007)."""
        tp = TaperProfile(charge={i: TaperBin(ratio=0.8, count=5) for i in range(101)})
        end = datetime.datetime(2025, 1, 1, 6, 0)
        no_load = calculate_deferred_start(50.0, 100, 10.0, 5000, end, taper_profile=tp)
        with_load = calculate_deferred_start(
            50.0, 100, 10.0, 5000, end, net_consumption_kw=2.0, taper_profile=tp
        )
        # With load, effective charge rate is lower → needs to start earlier
        assert with_load < no_load

    def test_taper_starts_earlier_than_linear(self) -> None:
        """Taper ratios < 1 should produce an earlier start than linear."""
        tp = TaperProfile(charge={i: TaperBin(ratio=0.6, count=5) for i in range(101)})
        end = datetime.datetime(2025, 1, 1, 6, 0)
        linear = calculate_deferred_start(50.0, 100, 10.0, 5000, end)
        tapered = calculate_deferred_start(50.0, 100, 10.0, 5000, end, taper_profile=tp)
        # 60% taper → longer charge time → earlier start
        assert tapered < linear

    def test_deferred_start_with_cold_temp(self) -> None:
        """Cold temperature should require earlier start time."""
        # Create taper profile with 0.7 ratio at 10°C (trusted)
        tp = TaperProfile(charge={i: TaperBin(ratio=1.0, count=10) for i in range(101)})
        tp.charge_temp = {10: TaperBin(ratio=0.7, count=10)}
        end = datetime.datetime(2025, 1, 1, 6, 0)
        no_temp = calculate_deferred_start(50.0, 100, 10.0, 5000, end, taper_profile=tp)
        with_cold_temp = calculate_deferred_start(
            50.0, 100, 10.0, 5000, end, taper_profile=tp, bms_temp_c=10.0
        )
        # Cold temp (0.7 factor) → longer charge time → earlier start
        assert with_cold_temp < no_temp

    def test_deferred_start_no_temp_unchanged(self) -> None:
        """Passing None for temperature should produce same result."""
        tp = TaperProfile(charge={i: TaperBin(ratio=0.8, count=5) for i in range(101)})
        tp.charge_temp = {10: TaperBin(ratio=0.7, count=10)}
        end = datetime.datetime(2025, 1, 1, 6, 0)
        without_param = calculate_deferred_start(
            50.0, 100, 10.0, 5000, end, taper_profile=tp
        )
        with_none = calculate_deferred_start(
            50.0, 100, 10.0, 5000, end, taper_profile=tp, bms_temp_c=None
        )
        # None should be same as omitting the parameter
        assert without_param == with_none


class TestIsChargeTargetReachable:
    """Tests for is_charge_target_reachable (C-022)."""

    def test_already_at_target(self) -> None:
        assert is_charge_target_reachable(80.0, 80, 10.0, 1.0, 5000) is True

    def test_above_target(self) -> None:
        assert is_charge_target_reachable(90.0, 80, 10.0, 1.0, 5000) is True

    def test_plenty_of_time(self) -> None:
        # 30% of 10kWh = 3kWh, 5kW max → ~0.67h needed, 4h available
        assert is_charge_target_reachable(50.0, 80, 10.0, 4.0, 5000) is True

    def test_insufficient_time(self) -> None:
        # 50% of 10kWh = 5kWh, 5kW max → ~1.1h needed (with headroom), 0.5h available
        assert is_charge_target_reachable(50.0, 100, 10.0, 0.5, 5000) is False

    def test_zero_remaining(self) -> None:
        assert is_charge_target_reachable(50.0, 100, 10.0, 0.0, 5000) is False

    def test_high_consumption_reduces_reachability(self) -> None:
        # Without consumption: reachable
        assert is_charge_target_reachable(50.0, 80, 10.0, 2.0, 5000) is True
        # With 4kW consumption eating into 5kW max: unreachable
        assert (
            is_charge_target_reachable(
                50.0, 80, 10.0, 2.0, 5000, net_consumption_kw=4.0
            )
            is False
        )

    def test_taper_reduces_reachability(self) -> None:
        tp = TaperProfile(charge={i: TaperBin(ratio=0.3, count=5) for i in range(101)})
        # Without taper: reachable (3kWh at 5kW = ~0.67h, 2h available)
        assert is_charge_target_reachable(50.0, 80, 10.0, 2.0, 5000) is True
        # With 30% taper: effective ~1.5kW → ~2.2h needed, 2h not enough
        assert (
            is_charge_target_reachable(50.0, 80, 10.0, 2.0, 5000, taper_profile=tp)
            is False
        )

    def test_charge_target_reachable_with_temp(self) -> None:
        """Cold temperature should reduce reachability."""
        # Create taper profile with reasonable ratios
        tp = TaperProfile(charge={i: TaperBin(ratio=0.9, count=10) for i in range(101)})
        tp.charge_temp = {5: TaperBin(ratio=0.3, count=10)}  # Very cold, trusted
        # Without temp: should be reachable
        # (3kWh at 4.5kW effective ~0.75h, 1.5h available)
        assert (
            is_charge_target_reachable(50.0, 80, 10.0, 1.5, 5000, taper_profile=tp)
            is True
        )
        # With very cold temp (0.3 factor): should be unreachable (needs ~2.5h)
        assert (
            is_charge_target_reachable(
                50.0, 80, 10.0, 1.5, 5000, taper_profile=tp, bms_temp_c=5.0
            )
            is False
        )


class TestCalculateDischargePower:
    def test_basic_calculation(self) -> None:
        result = calculate_discharge_power(80.0, 20, 10.0, 3.0, 5000)
        assert 100 < result < 5000

    def test_soc_at_min(self) -> None:
        result = calculate_discharge_power(20.0, 20, 10.0, 3.0, 5000)
        assert result == 100

    def test_soc_below_min(self) -> None:
        result = calculate_discharge_power(15.0, 20, 10.0, 3.0, 5000)
        assert result == 100

    def test_zero_remaining_hours(self) -> None:
        result = calculate_discharge_power(80.0, 20, 10.0, 0.0, 5000)
        assert result == 5000

    def test_consumption_reduces_power(self) -> None:
        base = calculate_discharge_power(80.0, 20, 10.0, 3.0, 5000)
        with_load = calculate_discharge_power(
            80.0, 20, 10.0, 3.0, 5000, net_consumption_kw=1.0
        )
        # House load assists discharge → lower inverter power needed
        assert with_load < base

    def test_high_consumption_returns_min(self) -> None:
        # House load exceeds inverter capacity — can't cover it, return min
        result = calculate_discharge_power(
            80.0, 20, 10.0, 3.0, 5000, net_consumption_kw=10.0
        )
        assert result == 100

    def test_consumption_floor_prevents_grid_import(self) -> None:
        # Paced power would be below 3kW house load → floor at 3kW
        result = calculate_discharge_power(
            35.0, 20, 10.0, 3.0, 5000, net_consumption_kw=3.0
        )
        assert result >= 3000

    def test_consumption_floor_not_applied_when_exceeds_max(self) -> None:
        # 6kW consumption > 5kW max power — floor can't help, return min
        result = calculate_discharge_power(
            25.0, 20, 10.0, 3.0, 5000, net_consumption_kw=6.0
        )
        assert result == 100


class TestDischargePowerPeakSafetyFloor:
    """Tests for peak-consumption-based safety floor (P1 priority)."""

    def test_peak_raises_floor_above_current(self) -> None:
        # Current 0.5kW, peak 7kW → floor at 7*1.5=10.5kW
        result = calculate_discharge_power(
            80.0,
            20,
            10.0,
            2.0,
            15000,
            net_consumption_kw=0.5,
            consumption_peak_kw=7.0,
        )
        assert result >= 10500  # 7 * 1.5 * 1000

    def test_peak_floor_clamped_to_max_power(self) -> None:
        # Peak 12kW → floor at 18kW but max is 15kW.
        # Safety floor > max_power → floor skipped (grid import unavoidable).
        result = calculate_discharge_power(
            90.0,
            30,
            20.0,
            2.0,
            15000,
            net_consumption_kw=0.5,
            consumption_peak_kw=12.0,
        )
        assert result <= 15000

    def test_no_peak_uses_current_only(self) -> None:
        # No peak → floor at current * 1.5 = 0.75kW, pacing dominates
        result = calculate_discharge_power(
            80.0,
            20,
            10.0,
            3.0,
            5000,
            net_consumption_kw=0.5,
        )
        assert result < 5000  # pacing, not max

    def test_peak_decayed_still_provides_margin(self) -> None:
        # Peak decayed to 4kW → floor at 6kW
        result = calculate_discharge_power(
            50.0,
            20,
            10.0,
            2.0,
            10000,
            net_consumption_kw=0.5,
            consumption_peak_kw=4.0,
        )
        assert result >= 6000

    def test_peak_zero_same_as_no_peak(self) -> None:
        no_peak = calculate_discharge_power(
            80.0,
            20,
            10.0,
            3.0,
            5000,
            net_consumption_kw=1.0,
        )
        zero_peak = calculate_discharge_power(
            80.0,
            20,
            10.0,
            3.0,
            5000,
            net_consumption_kw=1.0,
            consumption_peak_kw=0.0,
        )
        assert no_peak == zero_peak


class TestDischargePowerFeedinConstraint:
    def test_feedin_caps_discharge(self) -> None:
        # Without limit: full discharge pacing
        unlimited = calculate_discharge_power(80.0, 20, 10.0, 3.0, 5000)
        # With tight limit: should cap power
        limited = calculate_discharge_power(
            80.0, 20, 10.0, 3.0, 5000, feedin_remaining_kwh=0.5
        )
        assert limited < unlimited

    def test_feedin_exhausted_returns_min(self) -> None:
        result = calculate_discharge_power(
            80.0, 20, 10.0, 3.0, 5000, feedin_remaining_kwh=0.0
        )
        assert result == 100


class TestShouldSuspendDischarge:
    def test_no_suspension_when_safe(self) -> None:
        # 60% - 20% = 40% of 10kWh = 4kWh. At 0.5kW consumption,
        # 8h to drain. Window is 2h → safe.
        assert not should_suspend_discharge(60.0, 20, 10.0, 2.0, 0.5)

    def test_suspends_when_consumption_threatens(self) -> None:
        # 25% - 20% = 5% of 10kWh = 0.5kWh. At 2kW consumption,
        # 0.25h to drain. Window is 2h → suspend.
        assert should_suspend_discharge(25.0, 20, 10.0, 2.0, 2.0)

    def test_suspends_at_min_soc(self) -> None:
        assert should_suspend_discharge(20.0, 20, 10.0, 2.0, 1.0)

    def test_no_suspension_without_consumption(self) -> None:
        assert not should_suspend_discharge(25.0, 20, 10.0, 2.0, 0.0)

    def test_no_suspension_with_solar_surplus(self) -> None:
        assert not should_suspend_discharge(25.0, 20, 10.0, 2.0, -2.0)

    def test_zero_remaining_hours(self) -> None:
        assert not should_suspend_discharge(50.0, 20, 10.0, 0.0, 2.0)

    def test_zero_capacity(self) -> None:
        assert not should_suspend_discharge(50.0, 20, 0.0, 2.0, 2.0)

    def test_peak_triggers_suspension(self) -> None:
        # 50% - 20% = 30% of 10kWh = 3kWh. Current 0.5kW → 6h drain > 2h*1.1 → safe.
        # But peak 3kW → 1h drain < 2h * 1.1 → suspend.
        assert not should_suspend_discharge(50.0, 20, 10.0, 2.0, 0.5)
        assert should_suspend_discharge(
            50.0, 20, 10.0, 2.0, 0.5, consumption_peak_kw=3.0
        )

    def test_peak_below_current_uses_current(self) -> None:
        # Peak lower than current — current dominates.
        result_no_peak = should_suspend_discharge(25.0, 20, 10.0, 2.0, 2.0)
        result_low_peak = should_suspend_discharge(
            25.0, 20, 10.0, 2.0, 2.0, consumption_peak_kw=1.0
        )
        assert result_no_peak == result_low_peak

    def test_end_guard_suspends_low_energy(self) -> None:
        # 21% - 20% = 1% of 10kWh = 0.1kWh.
        # At 5kW consumption, floor = 5 * 1.5 = 7.5kW.
        # Guard = 7.5 * 10/60 = 1.25 kWh.  0.1 < 1.25 → suspend.
        assert should_suspend_discharge(21.0, 20, 10.0, 0.05, 5.0)

    def test_end_guard_does_not_trigger_with_plenty_of_energy(self) -> None:
        # 60% - 20% = 40% of 10kWh = 4kWh.
        # Floor = 5 * 1.5 = 7.5kW.  Guard = 1.25 kWh.  4 > 1.25 → no guard.
        # Use short window so P2 doesn't trigger: 4/5 = 0.8h > 0.5*1.1.
        assert not should_suspend_discharge(60.0, 20, 10.0, 0.5, 5.0)

    def test_end_guard_scales_with_consumption(self) -> None:
        # 22% - 20% = 2% of 10kWh = 0.2kWh.
        # At 0.5kW consumption, floor = 0.75kW, guard = 0.125kWh.
        # 0.2 > 0.125 → no guard.  Short window so P2 doesn't fire:
        # 0.2/0.5 = 0.4h > 0.3*1.1 = 0.33.
        assert not should_suspend_discharge(22.0, 20, 10.0, 0.3, 0.5)
        # At 2kW consumption, floor = 3kW, guard = 3*10/60 = 0.5kWh.
        # 0.2 < 0.5 → suspend via guard.
        assert should_suspend_discharge(22.0, 20, 10.0, 0.05, 2.0)


class TestCalculateDischargeDeferredStart:
    """Tests for calculate_discharge_deferred_start."""

    def _end(self, hours_from_now: float = 2.0) -> datetime.datetime:
        return datetime.datetime(2026, 4, 13, 20, 0, 0)

    def _start(self) -> datetime.datetime:
        return datetime.datetime(2026, 4, 13, 18, 0, 0)

    def test_no_discharge_needed_returns_end(self) -> None:
        """SoC already at min — no forced discharge needed."""
        result = calculate_discharge_deferred_start(30.0, 30, 10.0, 10500, self._end())
        assert result == self._end()

    def test_soc_below_min_returns_end(self) -> None:
        result = calculate_discharge_deferred_start(25.0, 30, 10.0, 10500, self._end())
        assert result == self._end()

    def test_defers_with_long_window(self) -> None:
        """Plenty of time — should defer past start."""
        result = calculate_discharge_deferred_start(
            80.0, 30, 10.0, 10500, self._end(), start=self._start()
        )
        # 5kWh to discharge at 10.5kW = ~0.48h → buffered ~0.53h
        # End - 0.53h = ~19:28, which is after 18:00 start
        assert result > self._start()
        assert result < self._end()

    def test_tight_window_returns_start(self) -> None:
        """Window too short — should return start time (discharge immediately)."""
        short_end = datetime.datetime(2026, 4, 13, 18, 20, 0)
        result = calculate_discharge_deferred_start(
            90.0, 10, 20.0, 10500, short_end, start=self._start()
        )
        assert result == self._start()

    def test_house_consumption_reduces_effective_rate(self) -> None:
        """House load reduces effective discharge rate → earlier start."""
        no_load = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=0.0,
            start=self._start(),
        )
        with_load = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=2.0,
            start=self._start(),
        )
        # House load reduces effective forced discharge rate → need earlier start
        assert with_load < no_load

    def test_feedin_uses_doubled_headroom(self) -> None:
        """Feed-in deadline uses 2x headroom → starts earlier."""
        soc_only = calculate_discharge_deferred_start(
            80.0, 30, 10.0, 10500, self._end(), start=self._start()
        )
        with_feedin = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            feedin_energy_limit_kwh=5.0,
        )
        # Feed-in requires earlier start due to doubled headroom
        assert with_feedin <= soc_only

    def test_feedin_deadline_accounts_for_consumption(self) -> None:
        """House load reduces effective export rate → earlier start."""
        no_load = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=0.0,
            feedin_energy_limit_kwh=5.0,
            start=self._start(),
        )
        with_load = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=3.0,
            feedin_energy_limit_kwh=5.0,
            start=self._start(),
        )
        # House load reduces export rate → must start earlier for feedin
        assert with_load < no_load

    def test_zero_max_power_returns_end(self) -> None:
        result = calculate_discharge_deferred_start(80.0, 30, 10.0, 0, self._end())
        assert result == self._end()

    def test_large_feedin_dominates(self) -> None:
        """Large feedin limit forces earlier start than SoC alone."""
        result = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            feedin_energy_limit_kwh=15.0,
        )
        # 15kWh export at ~10.5kW effective = ~1.43h → buffered ~1.79h
        # That should push start before the SoC-only deadline
        assert result >= self._start()
        assert result < self._end()

    def test_peak_consumption_makes_feedin_earlier(self) -> None:
        """Peak consumption reduces effective export rate → earlier feedin deadline."""
        no_peak = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=1.0,
            feedin_energy_limit_kwh=5.0,
            start=self._start(),
        )
        with_peak = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=1.0,
            feedin_energy_limit_kwh=5.0,
            consumption_peak_kw=5.0,
            start=self._start(),
        )
        # Peak 5kW reduces effective export from 10.5-1=9.5kW to 10.5-5=5.5kW
        assert with_peak < no_peak

    def test_taper_consumption_affects_soc_deadline(self) -> None:
        """Taper path must also account for consumption (D-007)."""
        tp = TaperProfile(
            discharge={i: TaperBin(ratio=0.8, count=5) for i in range(101)}
        )
        no_load = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=0.0,
            start=self._start(),
            taper_profile=tp,
        )
        with_load = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=2.0,
            start=self._start(),
            taper_profile=tp,
        )
        # Consumption reduces effective discharge rate → earlier start
        assert with_load < no_load

    def test_taper_starts_earlier_than_linear(self) -> None:
        """Taper ratios < 1 → longer discharge time → earlier start."""
        tp = TaperProfile(
            discharge={i: TaperBin(ratio=0.6, count=5) for i in range(101)}
        )
        linear = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            start=self._start(),
        )
        tapered = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            taper_profile=tp,
        )
        assert tapered < linear

    def test_peak_without_feedin_no_effect(self) -> None:
        """Peak only affects feedin deadline — SoC deadline unaffected."""
        no_peak = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            start=self._start(),
        )
        with_peak = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            self._end(),
            consumption_peak_kw=5.0,
            start=self._start(),
        )
        assert no_peak == with_peak
