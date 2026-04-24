"""Tests for smart_battery.algorithms — pure pacing functions.

These tests verify that the extracted shared algorithms produce
identical results to the original foxess_control implementations.
"""

from __future__ import annotations

import datetime
from typing import Any

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

    def _live_2026_04_24_taper(self) -> TaperProfile:
        """Live taper profile captured during the 2026-04-24 02:53 UTC event.

        Contains several outlier observations (81:0.05/1, 83:0.41/3, 85:0.16/2,
        90:0.21/7) surrounded by 0.87-1.0 neighbours, which skew the
        taper-integrated charge-hours estimate upward.
        """
        data: dict[str, Any] = {
            "charge": {
                "33": [0.1991118124436429, 2],
                "34": [0.8112584791103095, 3],
                "35": [0.9194285714285715, 1],
                "36": [0.8118390501953712, 3],
                "37": [0.8111457168620378, 2],
                "38": [0.8118390501953712, 3],
                "39": [0.8116123835287046, 2],
                "40": [0.8038057142857141, 3],
                "41": [0.8036190476190476, 2],
                "42": [0.9249619047619047, 2],
                "43": [0.8037523809523809, 2],
                "44": [0.6243393893466402, 4],
                "45": [0.8170122076515414, 3],
                "46": [0.9258599999999999, 3],
                "47": [0.9445139723801788, 2],
                "48": [0.9262857142857142, 3],
                "49": [0.9265657142857141, 3],
                "50": [0.9269580952380951, 3],
                "51": [0.9269238095238095, 3],
                "52": [0.9487333333333332, 2],
                "53": [0.9283158095238093, 4],
                "54": [0.9438857142857142, 2],
                "55": [0.9289761904761904, 3],
                "56": [0.9448190476190476, 2],
                "57": [0.9451485714285713, 3],
                "58": [0.9942857142857142, 2],
                "59": [0.979542857142857, 2],
                "60": [0.9357523809523809, 2],
                "61": [0.9430285714285713, 2],
                "62": [1.0, 1],
                "63": [0.8106666666666666, 1],
                "64": [1.0, 1],
                "65": [0.9763140817650875, 2],
                "66": [0.9762491888384166, 2],
                "67": [0.9809523809523809, 1],
                "68": [0.976346528228423, 2],
                "69": [0.9809523809523809, 1],
                "70": [0.976541207008436, 2],
                "71": [1.0, 2],
                "72": [0.9218040233614536, 1],
                "73": [0.9836015574302399, 4],
                "75": [0.9885687865022712, 5],
                "77": [0.96068, 4],
                "79": [0.9606599999999998, 3],
                "80": [0.8747999999999998, 3],
                "81": [0.05, 1],
                "82": [0.8747999999999998, 4],
                "83": [0.4093, 3],
                "84": [0.8747999999999998, 4],
                "85": [0.15642857142857142, 2],
                "86": [0.875, 4],
                "87": [0.5833333333333333, 2],
                "88": [1.0, 3],
                "90": [0.20963817927242218, 7],
                "91": [0.4051333333333333, 3],
                "92": [0.4058, 3],
                "93": [0.4063809523809524, 1],
                "94": [0.40723809523809523, 1],
                "95": [0.4080952380952381, 1],
                "96": [0.41019999999999995, 2],
            }
        }
        return TaperProfile.from_dict(data)

    def test_outlier_taper_does_not_falsely_fail_live_2026_04_24(self) -> None:
        """Outlier taper observations must not flip a feasible target to unreachable.

        Reproduces the 2026-04-24 02:53 UTC live event: during a smart
        charge with solar surplus, the taper-integrated estimate summed
        several isolated outlier observations (bins 81, 83, 85, 87, 90
        with ratios 0.05-0.58) that were surrounded by 0.87-1.0
        neighbours.  The integration produced 1.04h of taper-weighted
        charge hours, which after the 10% headroom buffer exceeded the
        remaining 1.09h window — so the algorithm returned False despite
        the inverter empirically charging at ~10.2 kW and needing only
        6.3 kWh in 65 minutes.

        The feasibility check (C-022) must remain a plausibility bound
        rather than a pessimistic point estimate: isolated outlier bins
        should not cause a spurious HA Repair issue when a typical
        taper-aware scenario comfortably reaches the target.
        """
        tp = self._live_2026_04_24_taper()
        # Exact live inputs from the algo_decision event.
        assert (
            is_charge_target_reachable(
                current_soc=75.0,
                target_soc=90,
                battery_capacity_kwh=42.0,
                remaining_hours=1.0939019616666668,
                max_power_w=10500,
                net_consumption_kw=-1.09,  # solar surplus
                headroom=0.1,
                taper_profile=tp,
                bms_temp_c=18.9,
            )
            is True
        )

    def test_live_2026_04_24_taper_with_house_load_still_reachable(self) -> None:
        """Same live inputs but with modest house load — still reachable.

        Solar surplus was incidental to the bug: the root cause was the
        outlier-dominated taper integration, not the consumption sign.
        Switching net_consumption_kw to +1.0 kW must still return True —
        median-smoothed estimate: 6.3 kWh / (9.45 kW * 0.87) = 0.77h,
        buffered = 0.85h, comfortably under 1.09h.
        """
        tp = self._live_2026_04_24_taper()
        assert (
            is_charge_target_reachable(
                current_soc=75.0,
                target_soc=90,
                battery_capacity_kwh=42.0,
                remaining_hours=1.0939019616666668,
                max_power_w=10500,
                net_consumption_kw=1.0,  # modest house load
                headroom=0.1,
                taper_profile=tp,
                bms_temp_c=18.9,
            )
            is True
        )

    def test_live_2026_04_24_taper_with_short_window_still_unreachable(self) -> None:
        """Same live inputs but half the remaining time — genuinely unreachable.

        Guard against over-fix that would make everything reachable.
        With only 33 minutes remaining (0.547h), even the linear estimate
        with the median ratio (0.77h) exceeds the window, so the fix
        must still return False.
        """
        tp = self._live_2026_04_24_taper()
        assert (
            is_charge_target_reachable(
                current_soc=75.0,
                target_soc=90,
                battery_capacity_kwh=42.0,
                remaining_hours=0.5469509808333334,  # half of the live value
                max_power_w=10500,
                net_consumption_kw=-1.09,
                headroom=0.1,
                taper_profile=tp,
                bms_temp_c=18.9,
            )
            is False
        )

    def test_live_2026_04_24_inputs_with_fresh_taper_trivially_reachable(self) -> None:
        """Same live inputs but empty TaperProfile — trivially reachable.

        Sanity check: without any taper data, the linear calculation
        says 6.3 kWh / 9.45 kW = 0.67h needed, buffered 0.74h, well
        within 1.09h.
        """
        tp = TaperProfile()
        assert (
            is_charge_target_reachable(
                current_soc=75.0,
                target_soc=90,
                battery_capacity_kwh=42.0,
                remaining_hours=1.0939019616666668,
                max_power_w=10500,
                net_consumption_kw=-1.09,
                headroom=0.1,
                taper_profile=tp,
                bms_temp_c=18.9,
            )
            is True
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

    def test_tight_window_small_battery_starts_immediately(self) -> None:
        """Tight window with small battery and feedin must start at window start.

        30-min window, 10.5 kW max, 80%->30% SoC, 10 kWh battery (5 kWh
        to drain at 10 kW effective = 30 min buffered = 33 min, longer
        than the window).  Feedin limit (6 kWh) exceeds the SoC energy,
        so SoC drain is the binding constraint — starts immediately.
        """
        start = datetime.datetime(2026, 4, 22, 0, 3, 0)
        end = datetime.datetime(2026, 4, 22, 0, 33, 0)
        now = datetime.datetime(2026, 4, 22, 0, 5, 0)

        deferred = calculate_discharge_deferred_start(
            80.0,
            30,
            10.0,
            10500,
            end,
            net_consumption_kw=0.5,
            start=start,
            feedin_energy_limit_kwh=6.0,
        )
        assert deferred <= now, (
            f"Deferred start {deferred} is after now {now}: "
            f"tight window should start immediately"
        )

    def test_large_battery_feedin_defers_by_feedin_deadline(self) -> None:
        """Large battery with small feedin limit should defer based on feedin.

        Reproduces production bug: 42 kWh battery, SoC 98%->21%, 51 min
        window, 1 kWh feedin limit.  The SoC deadline (draining 32 kWh)
        falls hours before the window start, but the feedin target only
        needs ~7 min of discharge.  The session should defer until ~7 min
        before end, not start immediately at low paced power for the full
        51 minutes (which creates sustained C-001 import risk).
        """
        start = datetime.datetime(2026, 4, 22, 16, 9, 0)
        end = datetime.datetime(2026, 4, 22, 17, 0, 0)

        deferred = calculate_discharge_deferred_start(
            98.0,
            21,
            42.0,
            10500,
            end,
            net_consumption_kw=0.2,
            start=start,
            feedin_energy_limit_kwh=1.0,
        )
        # Feedin deadline: 1 kWh at ~10.3 kW effective export = ~5.8 min,
        # with 20% headroom = ~7.3 min.  Deferred start should be ~16:52,
        # NOT at the window start (16:09).
        minutes_before_end = (end - deferred).total_seconds() / 60
        assert minutes_before_end < 15, (
            f"Deferred start {deferred} is {minutes_before_end:.0f} min "
            f"before end — should be <15 min for 1 kWh feedin, not "
            f"the full {(end - start).total_seconds() / 60:.0f} min window"
        )

    def test_small_feedin_defers_later_than_full_soc(self) -> None:
        """Small feedin target with large SoC headroom should defer much later.

        Scenario: 1 kWh export target, SoC 60%→10% = 5 kWh available,
        5 kW max power.  Without the cap the SoC deadline dominates
        and forced discharge starts far too early.
        """
        end = self._end()
        start = self._start()
        # Without feedin limit — needs time to drain 5 kWh
        soc_only = calculate_discharge_deferred_start(
            60.0, 10, 10.0, 5000, end, start=start
        )
        # With 1 kWh feedin limit — only needs ~12 min of forced discharge
        with_feedin = calculate_discharge_deferred_start(
            60.0,
            10,
            10.0,
            5000,
            end,
            start=start,
            feedin_energy_limit_kwh=1.0,
        )
        # The feedin-limited session should defer significantly later
        assert with_feedin > soc_only
        # Should be close to end (1 kWh at 5 kW = 0.2h, buffered ~0.22h)
        delta = (end - with_feedin).total_seconds() / 60
        assert delta < 20  # well under 20 minutes, not the ~67 min for full SoC


class TestGridExportLimitDeferral:
    """Tests for grid_export_limit_w effect on discharge deferral (C-037).

    When a hardware export limit is configured, both the SoC deadline and
    the feed-in energy deadline must cap the effective export rate at the
    limit value, producing an earlier (more conservative) deferred start.
    """

    def _start(self) -> datetime.datetime:
        return datetime.datetime(2026, 4, 24, 16, 0, 0)

    def _end(self) -> datetime.datetime:
        return datetime.datetime(2026, 4, 24, 18, 0, 0)

    def test_soc_deadline_capped_by_export_limit(self) -> None:
        """Export limit caps effective discharge rate → earlier SoC deadline."""
        uncapped = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=2.0,
            start=self._start(),
            feedin_energy_limit_kwh=3.0,
        )
        capped = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            self._end(),
            net_consumption_kw=2.0,
            start=self._start(),
            feedin_energy_limit_kwh=3.0,
            grid_export_limit_w=3000,
        )
        assert capped <= uncapped, (
            f"Export limit should produce earlier or equal start: "
            f"capped={capped}, uncapped={uncapped}"
        )

    def test_feedin_deadline_capped_by_export_limit(self) -> None:
        """Export limit caps effective export rate → earlier feedin deadline."""
        uncapped = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            feedin_energy_limit_kwh=5.0,
        )
        capped = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            feedin_energy_limit_kwh=5.0,
            grid_export_limit_w=3000,
        )
        assert capped < uncapped, (
            f"Feedin export limit should force earlier start: "
            f"capped={capped}, uncapped={uncapped}"
        )

    def test_zero_limit_has_no_effect(self) -> None:
        """grid_export_limit_w=0 means no limit — same result as omitted."""
        without = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            feedin_energy_limit_kwh=3.0,
        )
        with_zero = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            feedin_energy_limit_kwh=3.0,
            grid_export_limit_w=0,
        )
        assert with_zero == without

    def test_limit_higher_than_max_power_has_no_effect(self) -> None:
        """Export limit above max_power_w does not change the deadline."""
        without = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            feedin_energy_limit_kwh=3.0,
        )
        with_high = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            self._end(),
            start=self._start(),
            feedin_energy_limit_kwh=3.0,
            grid_export_limit_w=20000,
        )
        assert with_high == without
