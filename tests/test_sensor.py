"""Tests for FoxESS Control sensor entities."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.sensor import (
    POLLED_SENSOR_DESCRIPTIONS,
    BatteryForecastSensor,
    ChargePowerSensor,
    ChargeRemainingSensor,
    ChargeWindowSensor,
    DischargePowerSensor,
    DischargeRemainingSensor,
    DischargeWindowSensor,
    FoxESSPolledSensor,
    FoxESSWorkModeSensor,
    InverterOverrideStatusSensor,
    SmartOperationsOverviewSensor,
    _get_soc_value,
    async_setup_entry,
)


def _make_hass(
    smart_charge_state: dict[str, Any] | None = None,
    smart_discharge_state: dict[str, Any] | None = None,
    coordinator_soc: float | None = None,
) -> MagicMock:
    """Create a mock hass with DOMAIN data."""
    hass = MagicMock()
    mock_coordinator = MagicMock()
    mock_coordinator.data = (
        {"SoC": coordinator_soc} if coordinator_soc is not None else None
    )
    domain_data: dict[str, Any] = {
        "_smart_charge_unsubs": [],
        "_smart_discharge_unsubs": [],
        "entry1": {"coordinator": mock_coordinator},
    }
    if smart_charge_state is not None:
        domain_data["_smart_charge_state"] = smart_charge_state
    if smart_discharge_state is not None:
        domain_data["_smart_discharge_state"] = smart_discharge_state
    hass.data = {DOMAIN: domain_data}
    return hass


def _make_entry(entry_id: str = "entry1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
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

    def test_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Dchg 5kW→30%"
        assert sensor.icon == "mdi:battery-arrow-down"

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

    def test_name(self) -> None:
        sensor = InverterOverrideStatusSensor(_make_hass(), _make_entry())
        assert sensor.name == "FoxESS Status"

    def test_attributes_charging(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["mode"] == "smart_charge"
        assert attrs["phase"] == "charging"
        assert attrs["target_soc"] == 80

    def test_attributes_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["mode"] == "smart_discharge"
        assert attrs["min_soc"] == 30

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
        assert sensor.native_value == "Idle"
        assert sensor.icon == "mdi:home-battery"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is False
        assert attrs["discharge_active"] is False

    def test_charging(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "Charging to 80%"
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
        assert sensor.native_value == "Deferred charge to 80%"
        assert sensor.icon == "mdi:battery-clock"
        assert sensor.extra_state_attributes["charge_phase"] == "deferred"

    def test_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "Discharging to 30%"
        assert sensor.icon == "mdi:battery-arrow-down"
        attrs = sensor.extra_state_attributes
        assert attrs["discharge_active"] is True
        assert attrs["discharge_window"] == "17:00 – 20:00"

    def test_both_active(self) -> None:
        hass = _make_hass(
            smart_charge_state=_charge_state(),
            smart_discharge_state=_discharge_state(),
        )
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "Charge + Discharge active"
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
        assert sensor.native_value == "Idle"
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
        assert attrs["phase"] == "charging"

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

    def test_soc_estimate_shorter_than_window(self) -> None:
        """When target SoC will be reached before window ends, show that."""
        # 10kWh battery, SoC=70%, target=80%, power=5000W
        # Energy = 10% * 10kWh = 1kWh; time = 1kWh / 5kW = 0.2h = 12min
        # Window remaining at 02:30 with end 06:00 = 3h30m
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=5000, target_soc=80, charging_started=True
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 30, 0),
        ):
            assert sensor.native_value == "12m"

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
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
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
        """When deferred start time has passed, show 'starting'."""
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
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        # At 05:50, deferred start (~05:17) has passed
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == "starting"

    def test_none_when_idle(self) -> None:
        sensor = ChargeRemainingSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


class TestDischargePowerSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargePowerSensor(hass, _make_entry())
        assert sensor.native_value == 5000

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

    def test_soc_estimate_shorter_than_window(self) -> None:
        """When SoC will reach min_soc before window ends, show that."""
        # 10kWh battery, SoC=40%, min_soc=30%, power=5000W
        # Energy = 10% * 10kWh = 1kWh; time = 1kWh / 5kW = 0.2h = 12min
        # Window remaining at 17:30 with end 20:00 = 2h30m
        # So SoC estimate (12m) is shorter
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=5000, min_soc=30)
        )
        # Add a real entry_id so _get_battery_capacity_kwh finds it
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 40.0}
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            assert sensor.native_value == "12m"

    def test_window_shorter_than_soc_estimate(self) -> None:
        """When window ends before min_soc would be reached, show window."""
        # 10kWh battery, SoC=80%, min_soc=30%, power=1000W
        # Energy = 50% * 10kWh = 5kWh; time = 5kWh / 1kW = 5h
        # Window remaining at 19:30 with end 20:00 = 30m
        # Window (30m) is shorter
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=1000, min_soc=30)
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 80.0}
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 19, 30, 0),
        ):
            assert sensor.native_value == "30m"

    def test_none_when_idle(self) -> None:
        sensor = DischargeRemainingSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


# ---------------------------------------------------------------------------
# Battery Forecast sensor
# ---------------------------------------------------------------------------


class TestBatteryForecastSensor:
    def test_idle_returns_none(self) -> None:
        sensor = BatteryForecastSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None
        assert sensor.extra_state_attributes is None

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
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
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
            # SoC should rise over time
            assert forecast[-1]["soc"] > forecast[0]["soc"]
            # Should not exceed target
            assert all(p["soc"] <= 80.0 for p in forecast)

    def test_discharging_forecast_drops_to_min(self) -> None:
        """Forecast SoC drops from current toward min_soc."""
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=5000, min_soc=30)
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
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
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
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
            # Last point should be higher
            assert forecast[-1]["soc"] > 20.0

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

    def test_forecast_points_have_time_and_soc(self) -> None:
        """Each forecast point has 'time' (epoch ms) and 'soc' keys."""
        hass = _make_hass(smart_discharge_state=_discharge_state(last_power_w=5000))
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 60.0}
        hass.data[DOMAIN]["entry1"] = {
            "inverter": MagicMock(),
            "coordinator": mock_coordinator,
        }
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
            }
        )
        entry = _make_entry()
        sensors = [
            FoxESSPolledSensor(coordinator, entry, desc)
            for desc in POLLED_SENSOR_DESCRIPTIONS
        ]
        assert len(sensors) == 16
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

    def test_name(self) -> None:
        coordinator = self._make_coordinator({})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.name == "FoxESS Work Mode"


# ---------------------------------------------------------------------------
# _get_soc_value coordinator fallback
# ---------------------------------------------------------------------------


class TestGetSocValue:
    """Tests for _get_soc_value reading from coordinator."""

    def test_returns_coordinator_soc(self) -> None:
        hass = _make_hass()
        coordinator = MagicMock()
        coordinator.data = {"SoC": 72.0}
        hass.data[DOMAIN]["entry1"] = {"coordinator": coordinator}

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

        # 9 base + 16 polled + 1 work mode = 26
        assert len(added) == 26
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

        assert len(added) == 26  # 9 existing + 16 polled + 1 work mode
        polled = [e for e in added if isinstance(e, FoxESSPolledSensor)]
        assert len(polled) == 16
        work_mode = [e for e in added if isinstance(e, FoxESSWorkModeSensor)]
        assert len(work_mode) == 1
