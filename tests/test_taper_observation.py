"""Tests for taper observation recording in the listener layer.

Verifies that _record_taper_observation correctly records the BMS taper
ratio as actual_power / max_power (not actual_power / paced_power).

Bug: when charging is paced below max, the denominator was last_power_w
(the paced request) instead of max_power_w (inverter maximum). This
causes actual/paced > 1.0, clamped to 1.0, hiding the taper entirely.

Related: C-014 (taper plausibility), D-011 (SoC-indexed taper),
D-012 (quality gates), D-014 (temperature correction).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smart_battery.taper import TaperProfile


def _make_hass_and_state(
    *,
    coordinator_actual_kw: float | None,
    last_power_w: int,
    max_power_w: int,
    bms_temp: float | None = None,
    taper_deficit_streak: int = 0,
) -> tuple[MagicMock, dict[str, Any]]:
    """Create minimal mocks for _record_taper_observation.

    Returns (hass_mock, cur_state_dict).
    """
    hass = MagicMock()
    hass.async_create_task = MagicMock()

    cur_state: dict[str, Any] = {
        "last_power_w": last_power_w,
        "max_power_w": max_power_w,
        "taper_deficit_streak": taper_deficit_streak,
    }
    return hass, cur_state


def _call_record(
    hass: MagicMock,
    cur_state: dict[str, Any],
    taper: TaperProfile,
    soc: float,
    coordinator_actual_kw: float | None,
    bms_temp: float | None = None,
    record_fn_name: str = "record_charge",
    coordinator_var: str = "batChargePower",
    interval_seconds: int = 300,
) -> None:
    """Call _record_taper_observation with mocked coordinator lookups."""
    from smart_battery.listeners import _record_taper_observation

    with (
        patch(
            "smart_battery.listeners._get_coordinator_value",
            return_value=coordinator_actual_kw,
        ),
        patch(
            "smart_battery.listeners._get_bms_temperature",
            return_value=bms_temp,
        ),
    ):
        _record_taper_observation(
            hass,
            "test_domain",
            taper,
            cur_state,
            soc,
            coordinator_var,
            record_fn_name,
            save_every=100,
            interval_seconds=interval_seconds,
        )


class TestTaperObservationDenominator:
    """The recorded taper ratio must use max_power_w as denominator,
    not last_power_w (the paced request)."""

    def test_paced_charge_records_correct_taper_ratio(self) -> None:
        """Production scenario: SoC 81%, max 10500W, paced to 4552W,
        actual 6380W from BMS. Expected ratio = 6380/10500 = 0.607.

        Bug: used 6380/4552 = 1.40, clamped to 1.0."""
        taper = TaperProfile()
        hass, state = _make_hass_and_state(
            coordinator_actual_kw=6.38,
            last_power_w=4552,
            max_power_w=10500,
        )
        _call_record(hass, state, taper, soc=81.0, coordinator_actual_kw=6.38)

        assert 81 in taper.charge
        expected_ratio = 6380 / 10500  # ~0.607
        assert taper.charge[81].ratio == pytest.approx(expected_ratio, abs=0.01)

    def test_full_power_charge_records_correct_ratio(self) -> None:
        """When not pacing (last_power_w == max_power_w), ratio should
        be the same regardless of which denominator is used."""
        taper = TaperProfile()
        hass, state = _make_hass_and_state(
            coordinator_actual_kw=10.4,
            last_power_w=10500,
            max_power_w=10500,
        )
        _call_record(hass, state, taper, soc=69.0, coordinator_actual_kw=10.4)

        assert 69 in taper.charge
        expected_ratio = 10400 / 10500  # ~0.990
        assert taper.charge[69].ratio == pytest.approx(expected_ratio, abs=0.01)

    def test_paced_discharge_records_correct_taper_ratio(self) -> None:
        """Same bug on the discharge side: paced discharge hides BMS taper."""
        taper = TaperProfile()
        hass, state = _make_hass_and_state(
            coordinator_actual_kw=3.5,
            last_power_w=2000,
            max_power_w=10500,
        )
        _call_record(
            hass,
            state,
            taper,
            soc=12.0,
            coordinator_actual_kw=3.5,
            record_fn_name="record_discharge",
            coordinator_var="batDischargePower",
            interval_seconds=60,
        )

        assert 12 in taper.discharge
        expected_ratio = 3500 / 10500  # ~0.333
        assert taper.discharge[12].ratio == pytest.approx(expected_ratio, abs=0.01)

    def test_actual_below_min_actual_w_still_skipped(self) -> None:
        """Observations with actual < 50W should still be rejected (D-012)."""
        taper = TaperProfile()
        hass, state = _make_hass_and_state(
            coordinator_actual_kw=0.03,  # 30W
            last_power_w=5000,
            max_power_w=10500,
        )
        _call_record(hass, state, taper, soc=50.0, coordinator_actual_kw=0.03)

        assert 50 not in taper.charge

    def test_low_last_power_skips_observation(self) -> None:
        """When last_power_w < 500, observation is skipped entirely
        (the pre-filter on line 341 should still use last_power_w)."""
        taper = TaperProfile()
        hass, state = _make_hass_and_state(
            coordinator_actual_kw=5.0,
            last_power_w=400,
            max_power_w=10500,
        )
        _call_record(hass, state, taper, soc=50.0, coordinator_actual_kw=5.0)

        assert 50 not in taper.charge


class TestTaperObservationTemperatureDenominator:
    """Temperature recording also needs max_power_w as denominator."""

    def test_temp_recording_uses_max_power_denominator(self) -> None:
        """Temperature taper recording should also use max_power_w,
        not last_power_w, as the denominator for the raw ratio."""
        taper = TaperProfile()
        hass, state = _make_hass_and_state(
            coordinator_actual_kw=6.38,
            last_power_w=4552,
            max_power_w=10500,
            # Need sustained deficit streak to trigger temp recording
            taper_deficit_streak=100,  # already above threshold
        )

        _call_record(
            hass,
            state,
            taper,
            soc=81.0,
            coordinator_actual_kw=6.38,
            bms_temp=19.0,
            interval_seconds=300,
        )

        # Temperature recording should have fired (streak sufficient).
        # The temp factor should reflect max_power_w denominator.
        # raw_ratio = 6380/10500 = 0.607, soc_ratio = 1.0 (no prior SoC data)
        # temp_factor = 0.607/1.0 = 0.607
        assert 19 in taper.charge_temp
        expected_temp_factor = 6380 / 10500  # ~0.607
        assert taper.charge_temp[19].ratio == pytest.approx(
            expected_temp_factor, abs=0.01
        )
