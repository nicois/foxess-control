"""Tests for FoxESS Control binary sensors."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.foxess_control.binary_sensor import (
    SmartChargeActiveSensor,
    SmartDischargeActiveSensor,
    async_setup_entry,
)
from custom_components.foxess_control.const import DOMAIN


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


class TestSmartChargeActiveSensor:
    """Tests for SmartChargeActiveSensor."""

    def test_off_when_no_state(self) -> None:
        hass = _make_hass()
        sensor = SmartChargeActiveSensor(hass, _make_entry())
        assert sensor.is_on is False

    def test_on_when_state_present(self) -> None:
        hass = _make_hass(
            smart_charge_state={
                "target_soc": 80,
                "last_power_w": 1500,
                "max_power_w": 10500,
                "end": datetime.datetime(2026, 4, 7, 6, 0, 0),
                "soc_entity": "sensor.battery_soc",
            }
        )
        sensor = SmartChargeActiveSensor(hass, _make_entry())
        assert sensor.is_on is True

    def test_attributes_when_active(self) -> None:
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        hass = _make_hass(
            smart_charge_state={
                "target_soc": 80,
                "last_power_w": 1500,
                "max_power_w": 10500,
                "end": end,
                "soc_entity": "sensor.battery_soc",
            }
        )
        sensor = SmartChargeActiveSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["target_soc"] == 80
        assert attrs["current_power_w"] == 1500
        assert attrs["max_power_w"] == 10500
        assert attrs["end_time"] == end.isoformat()
        assert attrs["soc_entity"] == "sensor.battery_soc"

    def test_attributes_none_when_off(self) -> None:
        hass = _make_hass()
        sensor = SmartChargeActiveSensor(hass, _make_entry())
        assert sensor.extra_state_attributes is None

    def test_unique_id(self) -> None:
        sensor = SmartChargeActiveSensor(_make_hass(), _make_entry("abc123"))
        assert sensor.unique_id == "abc123_smart_charge_active"

    def test_off_when_domain_data_missing(self) -> None:
        hass = MagicMock()
        hass.data = {}
        sensor = SmartChargeActiveSensor(hass, _make_entry())
        assert sensor.is_on is False
        assert sensor.extra_state_attributes is None


class TestSmartDischargeActiveSensor:
    """Tests for SmartDischargeActiveSensor."""

    def test_off_when_no_state(self) -> None:
        hass = _make_hass()
        sensor = SmartDischargeActiveSensor(hass, _make_entry())
        assert sensor.is_on is False

    def test_on_when_state_present(self) -> None:
        hass = _make_hass(
            smart_discharge_state={
                "min_soc": 30,
                "last_power_w": 5000,
                "end": datetime.datetime(2026, 4, 7, 20, 0, 0),
                "soc_entity": "sensor.battery_soc",
            }
        )
        sensor = SmartDischargeActiveSensor(hass, _make_entry())
        assert sensor.is_on is True

    def test_attributes_when_active(self) -> None:
        end = datetime.datetime(2026, 4, 7, 20, 0, 0)
        hass = _make_hass(
            smart_discharge_state={
                "min_soc": 30,
                "last_power_w": 5000,
                "end": end,
                "soc_entity": "sensor.battery_soc",
            }
        )
        sensor = SmartDischargeActiveSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["min_soc"] == 30
        assert attrs["last_power_w"] == 5000
        assert attrs["end_time"] == end.isoformat()
        assert attrs["soc_entity"] == "sensor.battery_soc"

    def test_attributes_none_when_off(self) -> None:
        hass = _make_hass()
        sensor = SmartDischargeActiveSensor(hass, _make_entry())
        assert sensor.extra_state_attributes is None

    def test_unique_id(self) -> None:
        sensor = SmartDischargeActiveSensor(_make_hass(), _make_entry("abc123"))
        assert sensor.unique_id == "abc123_smart_discharge_active"

    def test_off_when_domain_data_missing(self) -> None:
        hass = MagicMock()
        hass.data = {}
        sensor = SmartDischargeActiveSensor(hass, _make_entry())
        assert sensor.is_on is False


class TestAsyncSetupEntry:
    """Tests for binary sensor platform setup."""

    @pytest.mark.asyncio
    async def test_creates_two_entities(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        added: list[Any] = []

        def mock_add(entities: Any, update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, entry, mock_add)  # type: ignore[arg-type]

        assert len(added) == 2
        assert isinstance(added[0], SmartChargeActiveSensor)
        assert isinstance(added[1], SmartDischargeActiveSensor)
