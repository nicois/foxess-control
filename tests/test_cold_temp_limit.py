"""Tests for cold-temperature BMS charge curtailment."""

from __future__ import annotations

from unittest.mock import MagicMock

from smart_battery.listeners import (
    COLD_TEMP_CURRENT_LIMIT_A,
    COLD_TEMP_THRESHOLD_C,
    _apply_cold_temp_limit,
)

DOMAIN = "foxess_control"


def _make_hass(
    bms_temp: float | None = None,
    bat_volt: float | None = None,
) -> MagicMock:
    hass = MagicMock()
    mock_coordinator = MagicMock()
    data: dict[str, float] = {}
    if bms_temp is not None:
        data["bmsBatteryTemperature"] = bms_temp
    if bat_volt is not None:
        data["batVolt"] = bat_volt
    mock_coordinator.data = data if data else None
    hass.data = {DOMAIN: {"entry1": {"coordinator": mock_coordinator}}}
    return hass


class TestColdTempLimit:
    def test_no_limit_above_threshold(self) -> None:
        hass = _make_hass(bms_temp=20.0, bat_volt=51.0)
        assert _apply_cold_temp_limit(12000, hass, DOMAIN) == 12000

    def test_limit_applied_below_threshold(self) -> None:
        hass = _make_hass(bms_temp=10.0, bat_volt=50.0)
        result = _apply_cold_temp_limit(12000, hass, DOMAIN)
        expected = int(COLD_TEMP_CURRENT_LIMIT_A * 50.0)
        assert result == expected

    def test_limit_at_exact_threshold_no_limit(self) -> None:
        hass = _make_hass(bms_temp=COLD_TEMP_THRESHOLD_C, bat_volt=50.0)
        assert _apply_cold_temp_limit(12000, hass, DOMAIN) == 12000

    def test_limit_just_below_threshold(self) -> None:
        hass = _make_hass(bms_temp=15.9, bat_volt=50.0)
        result = _apply_cold_temp_limit(12000, hass, DOMAIN)
        assert result == int(COLD_TEMP_CURRENT_LIMIT_A * 50.0)

    def test_no_limit_when_temp_unavailable(self) -> None:
        hass = _make_hass(bms_temp=None, bat_volt=50.0)
        assert _apply_cold_temp_limit(12000, hass, DOMAIN) == 12000

    def test_no_limit_when_voltage_unavailable(self) -> None:
        hass = _make_hass(bms_temp=5.0, bat_volt=None)
        assert _apply_cold_temp_limit(12000, hass, DOMAIN) == 12000

    def test_no_limit_when_coordinator_has_no_data(self) -> None:
        hass = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.data = None
        hass.data = {DOMAIN: {"entry1": {"coordinator": mock_coordinator}}}
        assert _apply_cold_temp_limit(12000, hass, DOMAIN) == 12000

    def test_cold_limit_higher_than_max_returns_max(self) -> None:
        """If the cold limit (80A × 50V = 4000W) exceeds max_power_w, no change."""
        hass = _make_hass(bms_temp=5.0, bat_volt=50.0)
        assert _apply_cold_temp_limit(3000, hass, DOMAIN) == 3000

    def test_cold_limit_uses_live_voltage(self) -> None:
        hass = _make_hass(bms_temp=5.0, bat_volt=48.0)
        result = _apply_cold_temp_limit(12000, hass, DOMAIN)
        assert result == int(COLD_TEMP_CURRENT_LIMIT_A * 48.0)

    def test_zero_voltage_returns_max(self) -> None:
        hass = _make_hass(bms_temp=5.0, bat_volt=0.0)
        assert _apply_cold_temp_limit(12000, hass, DOMAIN) == 12000
