"""Tests for FoxESS Control integration setup and service handlers."""

import datetime
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.foxess_control import (
    _build_override_group,
    _validate_duration,
)
from custom_components.foxess_control.foxess.inverter import Inverter, WorkMode


class TestValidateDuration:
    """Tests for _validate_duration."""

    def test_positive_duration(self) -> None:
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            end = _validate_duration(datetime.timedelta(hours=1))
            assert end == datetime.datetime(2026, 4, 7, 11, 0, 0)

    def test_zero_duration_rejected(self) -> None:
        with pytest.raises(ServiceValidationError, match="positive"):
            _validate_duration(datetime.timedelta(0))

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ServiceValidationError, match="positive"):
            _validate_duration(datetime.timedelta(hours=-1))

    def test_exceeds_max_hours(self) -> None:
        with pytest.raises(ServiceValidationError, match="4 hours"):
            _validate_duration(datetime.timedelta(hours=5))

    def test_exactly_max_hours(self) -> None:
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            end = _validate_duration(datetime.timedelta(hours=4))
            assert end == datetime.datetime(2026, 4, 7, 14, 0, 0)

    def test_crosses_midnight_rejected(self) -> None:
        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 23, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="midnight"),
        ):
            _validate_duration(datetime.timedelta(hours=2))

    def test_just_before_midnight_ok(self) -> None:
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 23, 0, 0),
        ):
            end = _validate_duration(datetime.timedelta(minutes=59))
            assert end.date() == datetime.date(2026, 4, 7)


class TestBuildOverrideGroup:
    """Tests for _build_override_group."""

    def test_force_charge_group(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

        now = datetime.datetime(2026, 4, 7, 14, 30, 0)
        end = datetime.datetime(2026, 4, 7, 15, 30, 0)

        group = _build_override_group(
            now, end, WorkMode.FORCE_CHARGE, inverter, min_soc_on_grid=15, fd_soc=100
        )

        assert group == {
            "enable": 1,
            "startHour": 14,
            "startMinute": 30,
            "endHour": 15,
            "endMinute": 30,
            "workMode": "ForceCharge",
            "minSocOnGrid": 15,
            "fdSoc": 100,
            "fdPwr": 10500,
        }

    def test_force_discharge_group(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 8400

        now = datetime.datetime(2026, 4, 7, 18, 0, 0)
        end = datetime.datetime(2026, 4, 7, 20, 0, 0)

        group = _build_override_group(
            now, end, WorkMode.FORCE_DISCHARGE, inverter, min_soc_on_grid=10, fd_soc=20
        )

        assert group["workMode"] == "ForceDischarge"
        assert group["fdSoc"] == 20
        assert group["fdPwr"] == 8400
        assert group["minSocOnGrid"] == 10

    def test_custom_power(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

        now = datetime.datetime(2026, 4, 7, 14, 0, 0)
        end = datetime.datetime(2026, 4, 7, 15, 0, 0)

        group = _build_override_group(
            now,
            end,
            WorkMode.FORCE_CHARGE,
            inverter,
            min_soc_on_grid=15,
            fd_soc=100,
            fd_pwr=6000,
        )

        assert group["fdPwr"] == 6000

    def test_default_power_uses_inverter_max(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

        now = datetime.datetime(2026, 4, 7, 14, 0, 0)
        end = datetime.datetime(2026, 4, 7, 15, 0, 0)

        group = _build_override_group(
            now,
            end,
            WorkMode.FORCE_CHARGE,
            inverter,
            min_soc_on_grid=15,
            fd_soc=100,
        )

        assert group["fdPwr"] == 10500
