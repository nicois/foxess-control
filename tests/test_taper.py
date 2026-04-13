"""Tests for smart_battery.taper — adaptive BMS taper model."""

from __future__ import annotations

import pytest

from smart_battery.taper import (
    EMA_ALPHA,
    MAX_RATIO,
    MIN_RATIO,
    MIN_REQUESTED_W,
    MIN_TRUST_COUNT,
    TaperBin,
    TaperProfile,
)


class TestTaperBinBasics:
    def test_dataclass_fields(self) -> None:
        b = TaperBin(ratio=0.85, count=5)
        assert b.ratio == 0.85
        assert b.count == 5


class TestRecordCharge:
    def test_first_observation_seeds_directly(self) -> None:
        tp = TaperProfile()
        tp.record_charge(90.0, 10000, 8500.0)
        assert tp.charge[90].ratio == 0.85
        assert tp.charge[90].count == 1

    def test_second_observation_uses_ema(self) -> None:
        tp = TaperProfile()
        tp.record_charge(90.0, 10000, 8000.0)  # ratio = 0.8
        tp.record_charge(90.0, 10000, 6000.0)  # ratio = 0.6
        expected = EMA_ALPHA * 0.6 + (1 - EMA_ALPHA) * 0.8
        assert tp.charge[90].ratio == pytest.approx(expected)
        assert tp.charge[90].count == 2

    def test_ratio_clamped_above(self) -> None:
        tp = TaperProfile()
        # actual > requested (e.g. solar contributing)
        tp.record_charge(50.0, 5000, 6000.0)
        assert tp.charge[50].ratio == MAX_RATIO

    def test_ratio_clamped_below(self) -> None:
        tp = TaperProfile()
        tp.record_charge(99.0, 10000, 10.0)  # ratio ~0.001
        assert tp.charge[99].ratio == MIN_RATIO

    def test_ignores_low_requested_power(self) -> None:
        tp = TaperProfile()
        tp.record_charge(50.0, MIN_REQUESTED_W - 1, 200.0)
        assert 50 not in tp.charge

    def test_soc_bucket_clamped(self) -> None:
        tp = TaperProfile()
        tp.record_charge(105.0, 5000, 4000.0)  # soc > 100
        assert 100 in tp.charge
        tp.record_charge(-5.0, 5000, 4000.0)  # soc < 0
        assert 0 in tp.charge

    def test_multiple_observations_converge(self) -> None:
        tp = TaperProfile()
        # Simulate steady 40% acceptance at 95% SoC
        for _ in range(20):
            tp.record_charge(95.0, 10000, 4000.0)
        # Should converge near 0.4
        assert tp.charge[95].ratio == pytest.approx(0.4, abs=0.02)


class TestRecordDischarge:
    def test_records_in_discharge_dict(self) -> None:
        tp = TaperProfile()
        tp.record_discharge(10.0, 8000, 5600.0)
        assert tp.discharge[10].ratio == 0.7
        assert tp.discharge[10].count == 1
        assert 10 not in tp.charge


class TestChargeRatio:
    def test_returns_1_with_no_data(self) -> None:
        tp = TaperProfile()
        assert tp.charge_ratio(90.0) == 1.0

    def test_returns_ratio_when_trusted(self) -> None:
        tp = TaperProfile()
        tp.charge[90] = TaperBin(ratio=0.7, count=MIN_TRUST_COUNT)
        assert tp.charge_ratio(90.0) == 0.7

    def test_ignores_untrusted_bin(self) -> None:
        tp = TaperProfile()
        tp.charge[90] = TaperBin(ratio=0.7, count=MIN_TRUST_COUNT - 1)
        assert tp.charge_ratio(90.0) == 1.0  # falls back to no data

    def test_nearest_neighbor_interpolation(self) -> None:
        tp = TaperProfile()
        tp.charge[88] = TaperBin(ratio=0.85, count=5)
        # No data at 90, should find 88 (within ±5 range)
        assert tp.charge_ratio(90.0) == 0.85

    def test_nearest_neighbor_range_limit_with_edge_extrapolation(self) -> None:
        tp = TaperProfile()
        tp.charge[80] = TaperBin(ratio=0.85, count=5)
        # 90 is 10 away from 80, outside ±5 nearest-neighbor range,
        # but 90 > max(trusted)=80 → edge extrapolation uses 80's ratio
        assert tp.charge_ratio(90.0) == 0.85

    def test_no_edge_extrapolation_for_middle_gap(self) -> None:
        tp = TaperProfile()
        tp.charge[70] = TaperBin(ratio=0.95, count=5)
        tp.charge[90] = TaperBin(ratio=0.60, count=5)
        # 80 is in a gap between 70 and 90, outside ±5 of both
        # Not at an edge → falls back to 1.0
        assert tp.charge_ratio(80.0) == 1.0

    def test_edge_extrapolation_above_max(self) -> None:
        tp = TaperProfile()
        tp.charge[92] = TaperBin(ratio=0.55, count=5)
        # 98 is well above max trusted bin (92), outside ±5 range
        # Edge extrapolation uses the highest bin's ratio
        assert tp.charge_ratio(98.0) == 0.55

    def test_edge_extrapolation_below_min_discharge(self) -> None:
        tp = TaperProfile()
        tp.discharge[15] = TaperBin(ratio=0.70, count=3)
        # 5 is well below min trusted bin (15)
        # Edge extrapolation uses the lowest bin's ratio
        assert tp.discharge_ratio(5.0) == 0.70

    def test_nearest_neighbor_prefers_closer(self) -> None:
        tp = TaperProfile()
        tp.charge[87] = TaperBin(ratio=0.9, count=5)
        tp.charge[92] = TaperBin(ratio=0.6, count=5)
        # 90 is 3 away from 87, 2 away from 92 — finds 92 first
        # (offset 2: checks 88 and 92, 92 has data)
        assert tp.charge_ratio(90.0) == 0.6

    def test_nearest_neighbor_skips_untrusted(self) -> None:
        tp = TaperProfile()
        tp.charge[91] = TaperBin(ratio=0.5, count=1)  # untrusted
        tp.charge[88] = TaperBin(ratio=0.8, count=5)  # trusted
        assert tp.charge_ratio(90.0) == 0.8


class TestDischargeRatio:
    def test_returns_ratio_from_discharge_dict(self) -> None:
        tp = TaperProfile()
        tp.discharge[10] = TaperBin(ratio=0.65, count=3)
        assert tp.discharge_ratio(10.0) == 0.65
        # Charge dict is separate
        assert tp.charge_ratio(10.0) == 1.0


class TestEstimateChargeHours:
    def test_flat_profile_matches_linear(self) -> None:
        """With no taper data (ratio=1.0 everywhere), estimate should
        match linear: energy / power."""
        tp = TaperProfile()
        # 50kWh battery, charge from 50% to 100% at 10kW
        hours = tp.estimate_charge_hours(50.0, 100, 50.0, 10000)
        # Linear: 25kWh / 10kW = 2.5h
        assert hours == pytest.approx(2.5, abs=0.01)

    def test_tapered_profile_longer_than_linear(self) -> None:
        tp = TaperProfile()
        # Add taper at high SoC (interpolation range is ±5%)
        for soc in range(90, 100):
            tp.charge[soc] = TaperBin(ratio=0.5, count=5)

        # Charge from 80% to 100% at 10kW, 50kWh battery
        hours = tp.estimate_charge_hours(80.0, 100, 50.0, 10000)
        linear_hours = 10.0 / 10.0  # 1.0h

        # 80-84%: ratio 0.5 (edge extrapolation — above max trusted=90)
        # 85-89%: ratio 0.5 (nearest neighbor within ±5 of 90)
        # 90-99%: ratio 0.5 (direct data)
        # All 20 steps at 0.5 ratio: 20 * 0.5kWh / 5kW = 2.0h
        assert hours == pytest.approx(2.0, abs=0.01)
        assert hours > linear_hours

    def test_fully_tapered_doubles_time(self) -> None:
        tp = TaperProfile()
        for soc in range(0, 101):
            tp.charge[soc] = TaperBin(ratio=0.5, count=5)

        hours = tp.estimate_charge_hours(0.0, 100, 50.0, 10000)
        linear_hours = 50.0 / 10.0  # 5.0h
        # All at 50% ratio → double the time
        assert hours == pytest.approx(linear_hours * 2, abs=0.01)

    def test_zero_range_returns_zero(self) -> None:
        tp = TaperProfile()
        assert tp.estimate_charge_hours(80.0, 80, 50.0, 10000) == 0.0

    def test_zero_power_returns_zero(self) -> None:
        tp = TaperProfile()
        assert tp.estimate_charge_hours(50.0, 100, 50.0, 0) == 0.0

    def test_zero_capacity_returns_zero(self) -> None:
        tp = TaperProfile()
        assert tp.estimate_charge_hours(50.0, 100, 0.0, 10000) == 0.0


class TestEstimateDischargeHours:
    def test_flat_profile_matches_linear(self) -> None:
        tp = TaperProfile()
        # 50kWh battery, discharge from 80% to 30% at 10kW
        hours = tp.estimate_discharge_hours(80.0, 30, 50.0, 10000)
        # Linear: 25kWh / 10kW = 2.5h
        assert hours == pytest.approx(2.5, abs=0.01)

    def test_tapered_low_soc_longer(self) -> None:
        tp = TaperProfile()
        for soc in range(30, 40):
            tp.discharge[soc] = TaperBin(ratio=0.5, count=5)

        hours = tp.estimate_discharge_hours(80.0, 30, 50.0, 10000)
        linear_hours = 25.0 / 10.0  # 2.5h
        assert hours > linear_hours


class TestSerialization:
    def test_round_trip_empty(self) -> None:
        tp = TaperProfile()
        data = tp.to_dict()
        tp2 = TaperProfile.from_dict(data)
        assert tp2.charge == {}
        assert tp2.discharge == {}

    def test_round_trip_with_data(self) -> None:
        tp = TaperProfile()
        tp.charge[90] = TaperBin(ratio=0.7, count=5)
        tp.charge[95] = TaperBin(ratio=0.4, count=12)
        tp.discharge[10] = TaperBin(ratio=0.65, count=3)

        data = tp.to_dict()
        tp2 = TaperProfile.from_dict(data)

        assert tp2.charge[90].ratio == pytest.approx(0.7)
        assert tp2.charge[90].count == 5
        assert tp2.charge[95].ratio == pytest.approx(0.4)
        assert tp2.charge[95].count == 12
        assert tp2.discharge[10].ratio == pytest.approx(0.65)
        assert tp2.discharge[10].count == 3

    def test_from_dict_handles_empty(self) -> None:
        tp = TaperProfile.from_dict({})
        assert tp.charge == {}
        assert tp.discharge == {}

    def test_from_dict_handles_corrupt_entries(self) -> None:
        data = {
            "charge": {
                "90": [0.7, 5],  # valid
                "bad": [0.5, 3],  # invalid key
                "91": "invalid",  # invalid value
                "92": [0.8],  # too short
            },
            "discharge": None,  # null
        }
        tp = TaperProfile.from_dict(data)
        assert 90 in tp.charge
        assert len(tp.charge) == 1
        assert tp.discharge == {}

    def test_from_dict_clamps_ratios(self) -> None:
        data = {"charge": {"50": [1.5, 3]}}
        tp = TaperProfile.from_dict(data)
        assert tp.charge[50].ratio == MAX_RATIO

    def test_serialized_keys_are_strings(self) -> None:
        """HA Store uses JSON which requires string keys."""
        tp = TaperProfile()
        tp.charge[90] = TaperBin(ratio=0.7, count=5)
        data = tp.to_dict()
        assert all(isinstance(k, str) for k in data["charge"])
