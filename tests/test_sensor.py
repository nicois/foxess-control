"""Tests for FoxESS Control override status sensor."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.sensor import (
    InverterOverrideStatusSensor,
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
    return hass


def _make_entry(entry_id: str = "entry1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


class TestInverterOverrideStatusSensor:
    """Tests for InverterOverrideStatusSensor."""

    def test_idle_when_no_state(self) -> None:
        hass = _make_hass()
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Idle"
        assert sensor.icon == "mdi:home-battery"
        assert sensor.extra_state_attributes is None

    def test_charging_state(self) -> None:
        hass = _make_hass(
            smart_charge_state={
                "target_soc": 80,
                "last_power_w": 6000,
                "max_power_w": 10500,
                "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
                "soc_entity": "sensor.battery_soc",
                "charging_started": True,
            }
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Charging 6.0kW"
        assert sensor.icon == "mdi:battery-charging"
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["mode"] == "smart_charge"
        assert attrs["phase"] == "charging"
        assert attrs["power_w"] == 6000
        assert attrs["target_soc"] == 80

    def test_deferred_state(self) -> None:
        hass = _make_hass(
            smart_charge_state={
                "target_soc": 80,
                "last_power_w": 0,
                "max_power_w": 10500,
                "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
                "soc_entity": "sensor.battery_soc",
                "charging_started": False,
            }
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Deferred"
        assert sensor.icon == "mdi:battery-clock"
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["phase"] == "deferred"

    def test_discharging_state(self) -> None:
        hass = _make_hass(
            smart_discharge_state={
                "min_soc": 30,
                "last_power_w": 5000,
                "end": datetime.datetime(2026, 4, 8, 20, 0, 0),
                "soc_entity": "sensor.battery_soc",
            }
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Discharging 5.0kW"
        assert sensor.icon == "mdi:battery-arrow-down"
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["mode"] == "smart_discharge"
        assert attrs["min_soc"] == 30

    def test_charging_low_power_shows_watts(self) -> None:
        hass = _make_hass(
            smart_charge_state={
                "target_soc": 80,
                "last_power_w": 500,
                "max_power_w": 10500,
                "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
                "soc_entity": "sensor.battery_soc",
                "charging_started": True,
            }
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Charging 500W"

    def test_charge_takes_priority_over_discharge(self) -> None:
        """If both states somehow exist, charge takes priority."""
        hass = _make_hass(
            smart_charge_state={
                "target_soc": 80,
                "last_power_w": 6000,
                "max_power_w": 10500,
                "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
                "soc_entity": "sensor.battery_soc",
                "charging_started": True,
            },
            smart_discharge_state={
                "min_soc": 30,
                "last_power_w": 5000,
                "end": datetime.datetime(2026, 4, 8, 20, 0, 0),
                "soc_entity": "sensor.battery_soc",
            },
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Charging 6.0kW"

    def test_unique_id(self) -> None:
        sensor = InverterOverrideStatusSensor(_make_hass(), _make_entry("abc123"))
        assert sensor.unique_id == "abc123_override_status"

    def test_idle_when_domain_data_missing(self) -> None:
        hass = MagicMock()
        hass.data = {}
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Idle"
        assert sensor.icon == "mdi:home-battery"
        assert sensor.extra_state_attributes is None


class TestAsyncSetupEntry:
    """Tests for sensor platform setup."""

    @pytest.mark.asyncio
    async def test_creates_one_entity(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        added: list[Any] = []

        def mock_add(entities: Any, update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, entry, mock_add)  # type: ignore[arg-type]

        assert len(added) == 1
        assert isinstance(added[0], InverterOverrideStatusSensor)
