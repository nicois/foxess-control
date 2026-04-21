"""Tests for FoxESS Control sensor entities."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
)
from custom_components.foxess_control.sensor import (
    POLLED_SENSOR_DESCRIPTIONS,
    BatteryForecastSensor,
    ChargePowerSensor,
    ChargeRemainingSensor,
    ChargeWindowSensor,
    DebugLogSensor,
    DischargePowerSensor,
    DischargeRemainingSensor,
    DischargeWindowSensor,
    FoxESSPolledSensor,
    FoxESSWorkModeSensor,
    InfoLogSensor,
    InitDebugLogSensor,
    InverterOverrideStatusSensor,
    SmartOperationsOverviewSensor,
    _get_soc_value,
    async_setup_entry,
    setup_debug_log,
)
from smart_battery.taper import TaperProfile


def _make_hass(
    smart_charge_state: dict[str, Any] | None = None,
    smart_discharge_state: dict[str, Any] | None = None,
    coordinator_soc: float | None = None,
    coordinator_extra: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock hass with DOMAIN data."""
    hass = MagicMock()
    mock_coordinator = MagicMock()
    coordinator_data: dict[str, Any] | None = None
    if coordinator_soc is not None or coordinator_extra:
        coordinator_data = {}
        if coordinator_soc is not None:
            coordinator_data["SoC"] = coordinator_soc
        if coordinator_extra:
            coordinator_data.update(coordinator_extra)
    mock_coordinator.data = coordinator_data
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData(coordinator=mock_coordinator)
    if smart_charge_state is not None:
        dd.smart_charge_state = smart_charge_state
    if smart_discharge_state is not None:
        dd.smart_discharge_state = smart_discharge_state
    hass.data = {DOMAIN: dd}
    return hass


def _make_entry(entry_id: str = "entry1", web_username: str | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {"web_username": web_username} if web_username else {}
    return entry


def _charge_state(**overrides: Any) -> dict[str, Any]:
    """Build a smart charge state dict with sensible defaults."""
    state: dict[str, Any] = {
        "target_soc": 80,
        "last_power_w": 6000,
        "max_power_w": 10500,
        "start": datetime.datetime(2026, 4, 8, 2, 0, 0),
        "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
        "charging_started": True,
    }
    state.update(overrides)
    return state


def _discharge_state(**overrides: Any) -> dict[str, Any]:
    """Build a smart discharge state dict with sensible defaults."""
    state: dict[str, Any] = {
        "min_soc": 30,
        "last_power_w": 5000,
        "start": datetime.datetime(2026, 4, 8, 17, 0, 0),
        "end": datetime.datetime(2026, 4, 8, 20, 0, 0),
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# InverterOverrideStatusSensor (Android Auto)
# ---------------------------------------------------------------------------


class TestInverterOverrideStatusSensor:
    """Tests for the compact Android Auto sensor."""

    def test_idle(self) -> None:
        hass = _make_hass()
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Idle"
        assert sensor.icon == "mdi:home-battery"
        assert sensor.extra_state_attributes is None

    def test_charging(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Chg 6kW→80%"
        assert sensor.icon == "mdi:battery-charging"

    def test_charging_fractional_power(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state(last_power_w=6500))
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Chg 6.5kW→80%"

    def test_charging_low_power(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state(last_power_w=500))
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Chg 500W→80%"

    def test_deferred(self) -> None:
        hass = _make_hass(
            smart_charge_state=_charge_state(last_power_w=0, charging_started=False)
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Wait→80%"
        assert sensor.icon == "mdi:battery-clock"

    def test_deferred_past_start_shows_charging(self) -> None:
        """When deferred start has passed, show charging not waiting."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        # At 05:50, deferred start (~05:17) has passed
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == "Chg 10.5kW→80%"
            assert sensor.icon == "mdi:battery-charging"

    def test_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Dchg 5kW→20:00"
        assert sensor.icon == "mdi:battery-arrow-down"

    def test_discharging_with_feedin_limit(self) -> None:
        hass = _make_hass(
            smart_discharge_state=_discharge_state(feedin_energy_limit_kwh=5.0)
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Dchg 5kW 5.0kWh"

    def test_charge_priority(self) -> None:
        """If both states exist, charge takes priority."""
        hass = _make_hass(
            smart_charge_state=_charge_state(),
            smart_discharge_state=_discharge_state(),
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Chg 6kW→80%"

    def test_unique_id(self) -> None:
        sensor = InverterOverrideStatusSensor(_make_hass(), _make_entry("abc"))
        assert sensor.unique_id == "abc_override_status"

    def test_translation_key(self) -> None:
        sensor = InverterOverrideStatusSensor(_make_hass(), _make_entry())
        assert sensor.translation_key == "override_status"

    def test_attributes_charging(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["mode"] == "smart_charge"
        assert attrs["phase"] == "charging"
        assert attrs["power_w"] == 6000
        assert attrs["max_power_w"] == 10500
        assert attrs["target_soc"] == 80
        assert attrs["end_time"] == "2026-04-08T06:00:00"

    def test_attributes_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["mode"] == "smart_discharge"
        assert attrs["power_w"] == 5000
        assert attrs["min_soc"] == 30
        assert attrs["end_time"] == "2026-04-08T20:00:00"

    def test_domain_data_missing(self) -> None:
        hass = MagicMock()
        hass.data = {}
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Idle"
        assert sensor.icon == "mdi:home-battery"


# ---------------------------------------------------------------------------
# SmartOperationsOverviewSensor (Dashboard)
# ---------------------------------------------------------------------------


class TestSmartOperationsOverviewSensor:
    """Tests for the dashboard overview sensor."""

    def test_idle(self) -> None:
        hass = _make_hass()
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "idle"
        assert sensor.icon == "mdi:home-battery"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is False
        assert attrs["discharge_active"] is False

    def test_charging(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "charging"
        assert sensor.icon == "mdi:battery-charging"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is True
        assert attrs["charge_phase"] == "charging"
        assert attrs["charge_power_w"] == 6000
        assert attrs["charge_window"] == "02:00 – 06:00"

    def test_deferred(self) -> None:
        hass = _make_hass(
            smart_charge_state=_charge_state(last_power_w=0, charging_started=False)
        )
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "deferred"
        assert sensor.icon == "mdi:battery-clock"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_phase"] == "deferred"
        assert attrs["charge_power_w"] == 0

    def test_deferred_past_start_shows_charging(self) -> None:
        """When deferred start has passed, show charging state not deferred."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == "charging"
            assert sensor.icon == "mdi:battery-charging"
            attrs = sensor.extra_state_attributes
            assert attrs["charge_phase"] == "charging"
            assert attrs["charge_power_w"] == 10500

    def test_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            assert sensor.native_value == "discharging"
            assert sensor.icon == "mdi:battery-arrow-down"
            attrs = sensor.extra_state_attributes
            assert attrs["discharge_active"] is True
            assert attrs["discharge_power_w"] == 5000
            assert attrs["discharge_min_soc"] == 30
            assert attrs["discharge_window"] == "17:00 – 20:00"

    def test_discharging_with_feedin_limit(self) -> None:
        hass = _make_hass(
            smart_discharge_state=_discharge_state(feedin_energy_limit_kwh=5.0)
        )
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "discharging"

    def test_both_active(self) -> None:
        hass = _make_hass(
            smart_charge_state=_charge_state(),
            smart_discharge_state=_discharge_state(),
        )
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "charge_discharge_active"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is True
        assert attrs["discharge_active"] is True

    def test_remaining_time(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 4, 30, 0),
        ):
            assert sensor.extra_state_attributes["charge_remaining"] == "1h 30m"

    def test_remaining_time_minutes_only(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 45, 0),
        ):
            assert sensor.extra_state_attributes["charge_remaining"] == "15m"

    def test_current_soc_from_coordinator(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state(), coordinator_soc=55.5)
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.extra_state_attributes["charge_current_soc"] == 55.5

    def test_current_soc_none_when_unavailable(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.extra_state_attributes["charge_current_soc"] is None

    def test_unique_id(self) -> None:
        sensor = SmartOperationsOverviewSensor(_make_hass(), _make_entry("abc"))
        assert sensor.unique_id == "abc_smart_operations"

    def test_domain_data_missing(self) -> None:
        hass = MagicMock()
        hass.data = {}
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "idle"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is False
        assert attrs["discharge_active"] is False


# ---------------------------------------------------------------------------
# Individual dashboard sensors
# ---------------------------------------------------------------------------


class TestChargePowerSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state(last_power_w=4500))
        sensor = ChargePowerSensor(hass, _make_entry())
        assert sensor.native_value == 4500

    def test_none_when_idle(self) -> None:
        sensor = ChargePowerSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None

    def test_attributes(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = ChargePowerSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["target_soc"] == 80
        assert attrs["max_power_w"] == 10500
        assert attrs["phase"] == "charging"

    def test_transition_shows_max_power(self) -> None:
        """Before callback fires, show max_power_w not 0."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == 10500

    def test_unit(self) -> None:
        sensor = ChargePowerSensor(_make_hass(), _make_entry())
        assert sensor.native_unit_of_measurement == "W"


class TestChargeWindowSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = ChargeWindowSensor(hass, _make_entry())
        assert sensor.native_value == "02:00 – 06:00"

    def test_none_when_idle(self) -> None:
        sensor = ChargeWindowSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


class TestChargeRemainingSensor:
    def test_value_when_active_no_capacity(self) -> None:
        """Without battery capacity, falls back to window remaining."""
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 0, 0),
        ):
            assert sensor.native_value == "1h 0m"

    def test_uses_window_remaining_not_soc_estimate(self) -> None:
        """Charge remaining uses window end, not power-based SoC estimate."""
        # Window remaining at 02:30 with end 06:00 = 3h30m
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=5000, target_soc=80, charging_started=True
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 30, 0),
        ):
            assert sensor.native_value == "3h 30m"

    def test_deferred_shows_starts_in(self) -> None:
        """When deferred, show time until charging begins."""
        # 10kWh battery, SoC=20%, target=80%, max_power=10500W
        # Energy = 60% * 10kWh = 6kWh
        # Charge at 80% of 10500W = 8400W = 8.4kW
        # Hours = 6 / 8.4 = 0.714h ≈ 42.9min
        # Deferred start = 06:00 - 42.9min ≈ 05:17
        # At 02:00, wait = ~3h 17m
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 0, 0),
        ):
            result = sensor.native_value
            assert result is not None
            assert result.startswith("starts in 3h")

    def test_deferred_about_to_start(self) -> None:
        """When deferred start time has passed, show window remaining."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        # At 05:50, deferred start (~05:17) has passed but callback
        # hasn't fired yet — show window remaining instead of "starting"
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == "10m"

    def test_deferred_clamps_to_window_start(self) -> None:
        """Deferred start never shows a time before the window opens."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=100,
                # Window 11:00-12:00 — energy calc needs >1h so deferred_start
                # would be before 11:00 without clamping
                start=datetime.datetime(2026, 4, 8, 11, 0, 0),
                end=datetime.datetime(2026, 4, 8, 12, 0, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 10.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        # At 08:00, unclamped deferred start would be before 11:00.
        # Clamped to 11:00, so "starts in 3h 0m".
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 8, 0, 0),
        ):
            result = sensor.native_value
            assert result == "starts in 3h 0m"

    def test_none_when_idle(self) -> None:
        sensor = ChargeRemainingSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


class TestDischargePowerSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            assert sensor.native_value == 5000

    def test_before_start_shows_zero(self) -> None:
        """Before the discharge window opens, power should be 0."""
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 15, 0, 0),
        ):
            assert sensor.native_value == 0

    def test_none_when_idle(self) -> None:
        sensor = DischargePowerSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None

    def test_attributes(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargePowerSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["min_soc"] == 30

    def test_unit(self) -> None:
        sensor = DischargePowerSensor(_make_hass(), _make_entry())
        assert sensor.native_unit_of_measurement == "W"

    def test_bat_discharge_zero_returns_zero(self) -> None:
        """When batDischargePower is 0.0 (solar > load, battery not discharging),
        the sensor should report 0, not the requested/target power.

        Regression: polled_kw == 0.0 was treated the same as None (no data),
        falling back to requested_w and creating false 4075W spikes.
        """
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=4075),
            coordinator_extra={"batDischargePower": 0.0},
        )
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            assert sensor.native_value == 0

    def test_bat_discharge_none_falls_back_to_requested(self) -> None:
        """When batDischargePower is None (no data available),
        the sensor should fall back to the requested power value.
        """
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=4075),
        )
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            assert sensor.native_value == 4075

    def test_bat_discharge_positive_returns_observed(self) -> None:
        """When batDischargePower is positive (battery actively discharging),
        the sensor should return the observed value in watts.
        """
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=4075),
            coordinator_extra={"batDischargePower": 0.5},
        )
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            # 0.5 kW = 500 W, min(500, 4075) = 500
            assert sensor.native_value == 500

    def test_bat_discharge_capped_at_requested(self) -> None:
        """When observed discharge exceeds requested, cap at requested."""
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=3000),
            coordinator_extra={"batDischargePower": 5.0},
        )
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            # 5.0 kW = 5000 W, min(5000, 3000) = 3000
            assert sensor.native_value == 3000


class TestDischargeWindowSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargeWindowSensor(hass, _make_entry())
        assert sensor.native_value == "17:00 – 20:00"

    def test_none_when_idle(self) -> None:
        sensor = DischargeWindowSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


class TestDischargeRemainingSensor:
    def test_value_when_active_no_capacity(self) -> None:
        """Without battery capacity, falls back to window remaining."""
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 19, 15, 0),
        ):
            assert sensor.native_value == "45m"

    def test_uses_window_remaining_not_soc_estimate(self) -> None:
        """Discharge remaining uses window end, not power-based SoC estimate."""
        # Window remaining at 17:30 with end 20:00 = 2h30m
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=5000, min_soc=30)
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 40.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            assert sensor.native_value == "2h 30m"

    def test_before_start_shows_starts_in(self) -> None:
        """Before discharge window opens, show time until start."""
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=5000, min_soc=30)
        )
        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 14, 15, 0),
        ):
            assert sensor.native_value == "starts in 2h 45m"

    def test_window_remaining(self) -> None:
        """During discharge, show window remaining."""
        # Window remaining at 19:30 with end 20:00 = 30m
        # Window (30m) is shorter
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=1000, min_soc=30)
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 80.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 19, 30, 0),
        ):
            assert sensor.native_value == "30m"

    def test_energy_limit_closer_shows_kwh(self) -> None:
        """When energy limit is closer to being hit, show kWh remaining."""
        # 50% of time elapsed (18:30 of 17:00-20:00), 80% of energy used
        hass = _make_hass(
            smart_discharge_state=_discharge_state(
                last_power_w=5000,
                min_soc=10,
                feedin_energy_limit_kwh=5.0,
                feedin_start_kwh=100.0,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 50.0, "feedin": 104.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 30, 0),
        ):
            assert sensor.native_value == "1.0 kWh left"

    def test_time_closer_shows_duration(self) -> None:
        """When time window is closer to ending, show time remaining."""
        # 80% of time elapsed (19:24 of 17:00-20:00), 20% of energy used
        hass = _make_hass(
            smart_discharge_state=_discharge_state(
                last_power_w=5000,
                min_soc=10,
                feedin_energy_limit_kwh=5.0,
                feedin_start_kwh=100.0,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 50.0, "feedin": 101.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 19, 24, 0),
        ):
            assert sensor.native_value == "36m"

    def test_none_when_idle(self) -> None:
        sensor = DischargeRemainingSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


# ---------------------------------------------------------------------------
# Battery Forecast sensor
# ---------------------------------------------------------------------------


class TestBatteryForecastSensor:
    def test_idle_returns_empty_forecast(self) -> None:
        sensor = BatteryForecastSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {"forecast": []}

    def test_charging_forecast_rises_to_target(self) -> None:
        """Forecast SoC rises from current to target during charge."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=5000,
                target_soc=80,
                charging_started=True,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 40.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 4, 0, 0),
        ):
            assert sensor.native_value == 40.0
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            assert len(forecast) > 0
            # First point is current SoC
            assert forecast[0]["soc"] == 40.0
            # Should reach the target SoC by end of window
            assert forecast[-1]["soc"] == 80.0
            # Should not exceed target
            assert all(p["soc"] <= 80.0 for p in forecast)

    def test_charging_forecast_curves_with_taper(self) -> None:
        """Forecast SoC curve bends when taper shows reduced acceptance."""
        taper = TaperProfile()
        # Record enough observations to trust: full power up to 79%, then tapering
        for soc in range(40, 80):
            for _ in range(3):
                taper.record_charge(float(soc), 10000, 10000.0)  # ratio 1.0
        for soc in range(80, 96):
            for _ in range(3):
                # Simulate tapering: 50% acceptance above 80%
                taper.record_charge(float(soc), 10000, 5000.0)  # ratio 0.5

        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=10000,
                max_power_w=10000,
                target_soc=95,
                charging_started=True,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 50.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        hass.data[DOMAIN].taper_profile = taper
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 0, 0),
        ):
            forecast = sensor.extra_state_attributes["forecast"]
            assert len(forecast) > 4

            # Find the rate of SoC change per step in the low-SoC region (50-70%)
            # vs the high-SoC region (80-90%) — taper should halve the rate
            low_region = [p for p in forecast if 55 <= p["soc"] <= 70]
            high_region = [p for p in forecast if 82 <= p["soc"] <= 92]
            assert len(low_region) >= 2, f"Not enough low-region points: {low_region}"
            assert len(high_region) >= 2, (
                f"Not enough high-region points: {high_region}"
            )

            low_rate = (low_region[-1]["soc"] - low_region[0]["soc"]) / (
                low_region[-1]["time"] - low_region[0]["time"]
            )
            high_rate = (high_region[-1]["soc"] - high_region[0]["soc"]) / (
                high_region[-1]["time"] - high_region[0]["time"]
            )

            # With 50% taper above 80%, the high-region rate should be
            # roughly half the low-region rate (allow some tolerance)
            assert high_rate < low_rate * 0.75, (
                f"Forecast should show taper: low={low_rate}, high={high_rate}"
            )

    def test_discharging_forecast_drops_to_min(self) -> None:
        """Forecast SoC drops from current toward min_soc."""
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=5000, min_soc=30)
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            assert sensor.native_value == 70.0
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            assert len(forecast) > 0
            assert forecast[0]["soc"] == 70.0
            # SoC should drop over time
            assert forecast[-1]["soc"] < forecast[0]["soc"]
            # Should not go below min_soc
            assert all(p["soc"] >= 30.0 for p in forecast)

    def test_deferred_charge_shows_flat_then_rise(self) -> None:
        """Deferred charge: SoC stays flat, then rises after start."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            assert len(forecast) > 2
            # Early points should be flat (deferred)
            assert forecast[0]["soc"] == 20.0
            assert forecast[1]["soc"] == 20.0
            # Should reach or approach the target (80%) by end of window
            assert forecast[-1]["soc"] >= 75.0

    def test_no_forecast_without_capacity(self) -> None:
        """Without battery capacity configured, forecast is empty."""
        hass = _make_hass(smart_charge_state=_charge_state())

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 4, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            assert attrs["forecast"] == []

    def test_discharge_forecast_defers_until_start(self) -> None:
        """Discharge forecast starts at the configured interval, not now."""
        # Discharge starts at 17:00, now is 16:00 — graph should start at 17:00
        hass = _make_hass(smart_discharge_state=_discharge_state(last_power_w=5000))
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 60.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 16, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            assert len(forecast) > 2
            # No points before the configured start time
            start_epoch = int(
                datetime.datetime(2026, 4, 8, 17, 0, 0).timestamp() * 1000
            )
            assert forecast[0]["time"] >= start_epoch
            # First point at current SoC (flat_until holds at start)
            assert forecast[0]["soc"] == 60.0
            # Points after start should decrease
            assert forecast[-1]["soc"] < 60.0

    def test_discharge_forecast_capped_by_feedin_limit(self) -> None:
        """Forecast SoC drop is capped by the feed-in energy limit."""
        # 60% SoC, 10kWh battery, 1kWh feedin limit → max 10% SoC drop
        hass = _make_hass(
            smart_discharge_state=_discharge_state(
                last_power_w=5000,
                min_soc=10,
                feedin_energy_limit_kwh=1.0,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 60.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            attrs = sensor.extra_state_attributes
            forecast = attrs["forecast"]
            # 1kWh / 10kWh = 10% max drop → floor at 50%
            assert all(p["soc"] >= 50.0 for p in forecast)
            # Should still show some discharge
            assert forecast[-1]["soc"] < 60.0

    def test_forecast_points_have_time_and_soc(self) -> None:
        """Each forecast point has 'time' (epoch ms) and 'soc' keys."""
        hass = _make_hass(smart_discharge_state=_discharge_state(last_power_w=5000))
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 60.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            for point in forecast:
                assert "time" in point
                assert "soc" in point
                assert isinstance(point["time"], int)
                assert isinstance(point["soc"], int | float)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# FoxESSPolledSensor (coordinator-backed)
# ---------------------------------------------------------------------------


class TestFoxESSPolledSensor:
    """Tests for coordinator-backed polled sensors."""

    def _make_coordinator(self, data: dict[str, Any] | None = None) -> MagicMock:
        coordinator = MagicMock()
        coordinator.data = data
        return coordinator

    def test_native_value_reads_from_coordinator(self) -> None:
        coordinator = self._make_coordinator({"SoC": 75.5, "batChargePower": 1.2})
        entry = _make_entry()
        desc = POLLED_SENSOR_DESCRIPTIONS[0]  # SoC
        sensor = FoxESSPolledSensor(coordinator, entry, desc)
        assert sensor.native_value == 75.5

    def test_native_value_returns_none_when_data_is_none(self) -> None:
        coordinator = self._make_coordinator(None)
        entry = _make_entry()
        desc = POLLED_SENSOR_DESCRIPTIONS[0]
        sensor = FoxESSPolledSensor(coordinator, entry, desc)
        assert sensor.native_value is None

    def test_native_value_returns_none_when_variable_missing(self) -> None:
        coordinator = self._make_coordinator({"batChargePower": 1.2})
        entry = _make_entry()
        desc = POLLED_SENSOR_DESCRIPTIONS[0]  # SoC — not in data
        sensor = FoxESSPolledSensor(coordinator, entry, desc)
        assert sensor.native_value is None

    def test_unique_id(self) -> None:
        coordinator = self._make_coordinator({})
        entry = _make_entry("myentry")
        desc = POLLED_SENSOR_DESCRIPTIONS[0]
        sensor = FoxESSPolledSensor(coordinator, entry, desc)
        assert sensor.unique_id == "myentry_battery_soc"

    def test_data_source_exposed_when_multi_source(self) -> None:
        """Sensor exposes data_source when WS credentials configured."""
        coordinator = self._make_coordinator({"SoC": 75.0, "_data_source": "api"})
        entry = _make_entry(web_username="user@example.com")
        sensor = FoxESSPolledSensor(coordinator, entry, POLLED_SENSOR_DESCRIPTIONS[0])
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["data_source"] == "api"

    def test_data_source_hidden_when_single_source(self) -> None:
        """No attribute when only API configured (no ambiguity)."""
        coordinator = self._make_coordinator({"SoC": 75.0, "_data_source": "api"})
        sensor = FoxESSPolledSensor(
            coordinator, _make_entry(), POLLED_SENSOR_DESCRIPTIONS[0]
        )
        assert sensor.extra_state_attributes is None

    def test_all_descriptors_create_sensors(self) -> None:
        coordinator = self._make_coordinator(
            {
                "SoC": 50,
                "batChargePower": 1.0,
                "batDischargePower": 0.5,
                "loadsPower": 3.2,
                "pvPower": 4.1,
                "ResidualEnergy": 8.0,
                "batTemperature": 25.0,
                "gridConsumptionPower": 2.1,
                "feedinPower": 0.3,
                "generationPower": 4.5,
                "batVolt": 52.1,
                "batCurrent": 23.0,
                "pv1Power": 2.5,
                "pv2Power": 2.0,
                "ambientTemperation": 18.0,
                "invTemperation": 35.0,
                "feedin": 657.1,
                "gridConsumption": 690.0,
                "generation": 1347.0,
                "chargeEnergyToTal": 1028.4,
                "dischargeEnergyToTal": 1211.4,
                "loads": 923.1,
                "energyThroughput": 2102.96,
                "meterPower": -5.014,
                "RVolt": 246.4,
                "RCurrent": 22.1,
                "RFreq": 50.02,
                "epsPower": 0.158,
                "bmsBatteryTemperature": 15.2,
            }
        )
        entry = _make_entry()
        sensors = [
            FoxESSPolledSensor(coordinator, entry, desc)
            for desc in POLLED_SENSOR_DESCRIPTIONS
        ]
        assert len(sensors) == 28
        # All should have a non-None value
        for s in sensors:
            assert s.native_value is not None


# ---------------------------------------------------------------------------
# FoxESSWorkModeSensor
# ---------------------------------------------------------------------------


class TestFoxESSWorkModeSensor:
    """Tests for the work mode sensor."""

    def _make_coordinator(self, data: dict[str, Any] | None = None) -> MagicMock:
        coordinator = MagicMock()
        coordinator.data = data
        return coordinator

    def test_returns_mode_string(self) -> None:
        coordinator = self._make_coordinator({"_work_mode": "SelfUse"})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.native_value == "SelfUse"

    def test_returns_none_when_data_is_none(self) -> None:
        coordinator = self._make_coordinator(None)
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.native_value is None

    def test_returns_none_when_mode_is_none(self) -> None:
        coordinator = self._make_coordinator({"_work_mode": None})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.native_value is None

    def test_unique_id(self) -> None:
        coordinator = self._make_coordinator({})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry("abc"))
        assert sensor.unique_id == "abc_work_mode"

    def test_translation_key(self) -> None:
        coordinator = self._make_coordinator({})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.translation_key == "work_mode"


# ---------------------------------------------------------------------------
# _get_soc_value coordinator fallback
# ---------------------------------------------------------------------------


class TestGetSocValue:
    """Tests for _get_soc_value reading from coordinator."""

    def test_returns_coordinator_soc(self) -> None:
        hass = _make_hass()
        coordinator = MagicMock()
        coordinator.data = {"SoC": 72.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(coordinator=coordinator)

        assert _get_soc_value(hass) == 72.0

    def test_returns_none_when_no_data(self) -> None:
        hass = _make_hass()

        assert _get_soc_value(hass) is None

    def test_returns_none_when_no_domain_data(self) -> None:
        hass = MagicMock()
        hass.data = {}

        assert _get_soc_value(hass) is None


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_all_entities(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        added: list[Any] = []

        def mock_add(entities: Any, update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, entry, mock_add)  # type: ignore[arg-type]

        # 9 base + 28 polled + 1 work mode + 1 freshness = 39
        assert len(added) == 39
        assert isinstance(added[0], InverterOverrideStatusSensor)
        assert isinstance(added[1], SmartOperationsOverviewSensor)
        assert isinstance(added[2], ChargePowerSensor)
        assert isinstance(added[3], ChargeWindowSensor)
        assert isinstance(added[4], ChargeRemainingSensor)
        assert isinstance(added[5], DischargePowerSensor)
        assert isinstance(added[6], DischargeWindowSensor)
        assert isinstance(added[7], DischargeRemainingSensor)
        assert isinstance(added[8], BatteryForecastSensor)

    @pytest.mark.asyncio
    async def test_creates_polled_sensors_when_coordinator_present(self) -> None:
        hass = _make_hass()
        entry = _make_entry()

        added: list[Any] = []

        def mock_add(entities: Any, update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, entry, mock_add)  # type: ignore[arg-type]

        assert len(added) == 39  # 9 existing + 28 polled + 1 work mode + 1 freshness
        polled = [e for e in added if isinstance(e, FoxESSPolledSensor)]
        assert len(polled) == 28
        work_mode = [e for e in added if isinstance(e, FoxESSWorkModeSensor)]
        assert len(work_mode) == 1


# ---------------------------------------------------------------------------
# Debug log capture
# ---------------------------------------------------------------------------


class TestDebugLog:
    """Tests for the opt-in debug log handler and sensor."""

    def test_setup_returns_none_when_entity_missing(self) -> None:
        hass = MagicMock()
        hass.states.get.return_value = None
        assert setup_debug_log(hass, _make_entry()) is None

    def test_setup_returns_none_when_entity_off(self) -> None:
        hass = MagicMock()
        state = MagicMock()
        state.state = "off"
        hass.states.get.return_value = state
        assert setup_debug_log(hass, _make_entry()) is None

    def test_setup_returns_sensor_and_handler_when_on(self) -> None:
        hass = MagicMock()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state
        result = setup_debug_log(hass, _make_entry())
        assert result is not None
        sensors, handlers = result
        assert any(isinstance(s, DebugLogSensor) for s in sensors)
        assert any(isinstance(s, InitDebugLogSensor) for s in sensors)
        assert len(handlers) == 3

    def test_handler_captures_log_messages(self) -> None:
        import logging

        hass = MagicMock()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state
        sensors, handlers = setup_debug_log(hass, _make_entry())  # type: ignore[misc]
        sensor = next(s for s in sensors if isinstance(s, DebugLogSensor))

        logger = logging.getLogger("custom_components.foxess_control")
        try:
            logger.info("test message %d", 42)
            assert sensor.native_value >= 1
            entries = sensor.extra_state_attributes["entries"]
            assert any("test message 42" in e["msg"] for e in entries)
            assert entries[-1]["level"] == "INFO"
        finally:
            for h in handlers:
                logger.removeHandler(h)

    def test_info_log_captures_only_info_and_above(self) -> None:
        import logging

        hass = MagicMock()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state
        sensors, handlers = setup_debug_log(hass, _make_entry())  # type: ignore[misc]
        info_sensor = next(s for s in sensors if isinstance(s, InfoLogSensor))
        debug_sensor = next(s for s in sensors if isinstance(s, DebugLogSensor))

        logger = logging.getLogger("custom_components.foxess_control")
        try:
            logger.debug("debug only")
            logger.info("info msg")
            logger.warning("warn msg")

            info_entries = info_sensor.extra_state_attributes["entries"]
            debug_entries = debug_sensor.extra_state_attributes["entries"]

            assert not any("debug only" in e["msg"] for e in info_entries)
            assert any("info msg" in e["msg"] for e in info_entries)
            assert any("warn msg" in e["msg"] for e in info_entries)
            assert any("debug only" in e["msg"] for e in debug_entries)
        finally:
            for h in handlers:
                logger.removeHandler(h)

    def test_buffer_is_bounded(self) -> None:
        import collections
        import logging

        from custom_components.foxess_control.sensor import (
            _DEBUG_LOG_BUFFER_SIZE,
            _DebugLogHandler,
        )

        buf: collections.deque[dict[str, str]] = collections.deque(
            maxlen=_DEBUG_LOG_BUFFER_SIZE
        )
        handler = _DebugLogHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test.bounded")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for i in range(_DEBUG_LOG_BUFFER_SIZE + 50):
                logger.info("msg %d", i)
            assert len(buf) == _DEBUG_LOG_BUFFER_SIZE
            # Oldest messages are evicted
            assert "msg 0" not in buf[0]["msg"]
        finally:
            logger.removeHandler(handler)

    def test_init_buffer_does_not_wrap(self) -> None:
        import logging

        from custom_components.foxess_control.sensor import (
            _DEBUG_LOG_BUFFER_SIZE,
            _InitDebugLogHandler,
        )

        buf: list[dict[str, str]] = []
        handler = _InitDebugLogHandler(buf, maxlen=_DEBUG_LOG_BUFFER_SIZE)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test.init_bounded")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for i in range(_DEBUG_LOG_BUFFER_SIZE + 50):
                logger.info("msg %d", i)
            assert len(buf) == _DEBUG_LOG_BUFFER_SIZE
            # First messages are preserved (not evicted)
            assert "msg 0" in buf[0]["msg"]
            assert "msg 1" in buf[1]["msg"]
        finally:
            logger.removeHandler(handler)

    @pytest.mark.asyncio
    async def test_async_setup_entry_adds_debug_sensor_when_enabled(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state

        added: list[Any] = []

        def mock_add(entities: Any, update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, entry, mock_add)  # type: ignore[arg-type]

        # 39 base + 3 log sensors = 42
        assert len(added) == 42
        debug_sensors = [e for e in added if isinstance(e, DebugLogSensor)]
        assert len(debug_sensors) == 1
        info_sensors = [e for e in added if isinstance(e, InfoLogSensor)]
        assert len(info_sensors) == 1
        init_sensors = [e for e in added if isinstance(e, InitDebugLogSensor)]
        assert len(init_sensors) == 1
