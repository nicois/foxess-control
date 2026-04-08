"""Tests for FoxESS Control sensor entities."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.sensor import (
    ChargePowerSensor,
    ChargeRemainingSensor,
    ChargeWindowSensor,
    DischargePowerSensor,
    DischargeRemainingSensor,
    DischargeWindowSensor,
    InverterOverrideStatusSensor,
    SmartOperationsOverviewSensor,
    async_setup_entry,
)


def _make_hass(
    smart_charge_state: dict[str, Any] | None = None,
    smart_discharge_state: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock hass with DOMAIN data."""
    hass = MagicMock()
    domain_data: dict[str, Any] = {
        "_smart_charge_unsubs": [],
        "_smart_discharge_unsubs": [],
    }
    if smart_charge_state is not None:
        domain_data["_smart_charge_state"] = smart_charge_state
    if smart_discharge_state is not None:
        domain_data["_smart_discharge_state"] = smart_discharge_state
    hass.data = {DOMAIN: domain_data}
    hass.states.get = MagicMock(return_value=None)
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
        "soc_entity": "sensor.battery_soc",
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
        "soc_entity": "sensor.battery_soc",
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

    def test_current_soc_from_entity(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        soc_state = MagicMock()
        soc_state.state = "55.5"
        hass.states.get = MagicMock(return_value=soc_state)
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
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 0, 0),
        ):
            assert sensor.native_value == "1h 0m"

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
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 19, 15, 0),
        ):
            assert sensor.native_value == "45m"

    def test_none_when_idle(self) -> None:
        sensor = DischargeRemainingSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


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

        assert len(added) == 8
        assert isinstance(added[0], InverterOverrideStatusSensor)
        assert isinstance(added[1], SmartOperationsOverviewSensor)
        assert isinstance(added[2], ChargePowerSensor)
        assert isinstance(added[3], ChargeWindowSensor)
        assert isinstance(added[4], ChargeRemainingSensor)
        assert isinstance(added[5], DischargePowerSensor)
        assert isinstance(added[6], DischargeWindowSensor)
        assert isinstance(added[7], DischargeRemainingSensor)
