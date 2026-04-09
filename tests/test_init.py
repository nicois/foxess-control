"""Tests for FoxESS Control integration setup and service handlers."""

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.foxess_control import (
    _build_override_group,
    _calculate_charge_power,
    _calculate_deferred_start,
    _get_current_soc,
    _get_net_consumption,
    _groups_overlap,
    _is_expired,
    _is_placeholder,
    _merge_with_existing,
    _remove_mode_from_schedule,
    _resolve_start_end,
    _resolve_start_end_explicit,
    _sanitize_group,
)
from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.foxess.inverter import (
    Inverter,
    ScheduleGroup,
    WorkMode,
)


class TestResolveStartEnd:
    """Tests for _resolve_start_end."""

    def test_defaults_to_now(self) -> None:
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            start, end = _resolve_start_end(datetime.timedelta(hours=1))
            assert start == datetime.datetime(2026, 4, 7, 10, 0, 0)
            assert end == datetime.datetime(2026, 4, 7, 11, 0, 0)

    def test_explicit_start_time(self) -> None:
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            start, end = _resolve_start_end(
                datetime.timedelta(hours=2), datetime.time(14, 30)
            )
            assert start == datetime.datetime(2026, 4, 7, 14, 30, 0)
            assert end == datetime.datetime(2026, 4, 7, 16, 30, 0)

    def test_zero_duration_rejected(self) -> None:
        with pytest.raises(ServiceValidationError, match="positive"):
            _resolve_start_end(datetime.timedelta(0))

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ServiceValidationError, match="positive"):
            _resolve_start_end(datetime.timedelta(hours=-1))

    def test_exceeds_max_hours(self) -> None:
        with pytest.raises(ServiceValidationError, match="4 hours"):
            _resolve_start_end(datetime.timedelta(hours=5))

    def test_exactly_max_hours(self) -> None:
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            start, end = _resolve_start_end(datetime.timedelta(hours=4))
            assert end == datetime.datetime(2026, 4, 7, 14, 0, 0)

    def test_crosses_midnight_rejected(self) -> None:
        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 23, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="midnight"),
        ):
            _resolve_start_end(datetime.timedelta(hours=2))

    def test_explicit_start_crosses_midnight_rejected(self) -> None:
        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="midnight"),
        ):
            _resolve_start_end(datetime.timedelta(hours=2), datetime.time(23, 0))

    def test_just_before_midnight_ok(self) -> None:
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 23, 0, 0),
        ):
            _, end = _resolve_start_end(datetime.timedelta(minutes=59))
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

    def test_fd_soc_clamped_to_minimum(self) -> None:
        """fdSoc below default api_min_soc (11) is clamped."""
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

        now = datetime.datetime(2026, 4, 7, 18, 0, 0)
        end = datetime.datetime(2026, 4, 7, 20, 0, 0)

        group = _build_override_group(
            now, end, WorkMode.FORCE_DISCHARGE, inverter, min_soc_on_grid=15, fd_soc=10
        )

        assert group["fdSoc"] == 11
        assert group["minSocOnGrid"] == 11  # clamped to <= fdSoc

    def test_fd_soc_clamped_to_custom_api_min_soc(self) -> None:
        """fdSoc is clamped to the custom api_min_soc value."""
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

        now = datetime.datetime(2026, 4, 7, 18, 0, 0)
        end = datetime.datetime(2026, 4, 7, 20, 0, 0)

        group = _build_override_group(
            now,
            end,
            WorkMode.FORCE_DISCHARGE,
            inverter,
            min_soc_on_grid=15,
            fd_soc=5,
            api_min_soc=8,
        )

        assert group["fdSoc"] == 8
        assert group["minSocOnGrid"] == 8  # clamped to <= fdSoc

    def test_min_soc_on_grid_clamped_to_fd_soc(self) -> None:
        """minSocOnGrid exceeding fdSoc is clamped down."""
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

        now = datetime.datetime(2026, 4, 7, 18, 0, 0)
        end = datetime.datetime(2026, 4, 7, 20, 0, 0)

        group = _build_override_group(
            now, end, WorkMode.FORCE_DISCHARGE, inverter, min_soc_on_grid=30, fd_soc=20
        )

        assert group["fdSoc"] == 20
        assert group["minSocOnGrid"] == 20

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


class TestGroupsOverlap:
    """Tests for _groups_overlap."""

    @staticmethod
    def _g(sh: int, sm: int, eh: int, em: int) -> ScheduleGroup:
        return {
            "enable": 1,
            "startHour": sh,
            "startMinute": sm,
            "endHour": eh,
            "endMinute": em,
            "workMode": "SelfUse",
            "minSocOnGrid": 10,
            "fdSoc": 10,
            "fdPwr": 10500,
        }

    def test_no_overlap(self) -> None:
        assert not _groups_overlap(self._g(10, 0, 11, 0), self._g(12, 0, 13, 0))

    def test_adjacent_no_overlap(self) -> None:
        assert not _groups_overlap(self._g(10, 0, 11, 0), self._g(11, 0, 12, 0))

    def test_overlap(self) -> None:
        assert _groups_overlap(self._g(10, 0, 12, 0), self._g(11, 0, 13, 0))

    def test_contained(self) -> None:
        assert _groups_overlap(self._g(10, 0, 14, 0), self._g(11, 0, 12, 0))


class TestMergeWithExisting:
    """Tests for _merge_with_existing."""

    def _make_group(
        self,
        mode: str,
        start_h: int,
        start_m: int,
        end_h: int,
        end_m: int,
        enable: int = 1,
    ) -> ScheduleGroup:
        return {
            "enable": enable,
            "startHour": start_h,
            "startMinute": start_m,
            "endHour": end_h,
            "endMinute": end_m,
            "workMode": mode,
            "minSocOnGrid": 10,
            "fdSoc": 100,
            "fdPwr": 10500,
        }

    def test_removes_same_mode_keeps_other(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                self._make_group("ForceCharge", 8, 0, 10, 0),
                self._make_group("ForceDischarge", 17, 0, 20, 0),
            ],
        }

        new_group = self._make_group("ForceCharge", 12, 0, 14, 0)
        result = _merge_with_existing(
            inverter,
            new_group,
            WorkMode.FORCE_CHARGE,
        )

        modes = [g["workMode"] for g in result]
        assert modes == ["ForceDischarge", "ForceCharge"]
        assert result[-1]["startHour"] == 12

    def test_conflict_with_different_mode_aborts(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                self._make_group("ForceDischarge", 13, 0, 15, 0),
            ],
        }

        new_group = self._make_group("ForceCharge", 14, 0, 16, 0)
        with pytest.raises(ServiceValidationError, match="conflicts"):
            _merge_with_existing(
                inverter,
                new_group,
                WorkMode.FORCE_CHARGE,
            )

    def test_force_removes_conflicting_group(self) -> None:
        """With force=True, overlapping groups are removed instead of raising."""
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                self._make_group("ForceDischarge", 13, 0, 15, 0),
            ],
        }

        new_group = self._make_group("ForceCharge", 14, 0, 16, 0)
        result = _merge_with_existing(
            inverter,
            new_group,
            WorkMode.FORCE_CHARGE,
            force=True,
        )

        assert len(result) == 1
        assert result[0]["workMode"] == "ForceCharge"

    def test_placeholder_groups_ignored(self) -> None:
        """API placeholder groups (workMode 'Invalid') are dropped."""
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                self._make_group("Invalid", 0, 0, 0, 0, enable=0),
            ],
        }

        new_group = self._make_group("ForceCharge", 14, 0, 16, 0)
        result = _merge_with_existing(
            inverter,
            new_group,
            WorkMode.FORCE_CHARGE,
        )

        assert len(result) == 1
        assert result[0]["workMode"] == "ForceCharge"

    def test_auto_disabled_group_re_enabled(self) -> None:
        """Groups disabled by the API after their window are re-enabled."""
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                self._make_group("ForceCharge", 11, 0, 14, 0, enable=0),
            ],
        }

        new_group = self._make_group("ForceDischarge", 17, 0, 20, 0)
        result = _merge_with_existing(
            inverter,
            new_group,
            WorkMode.FORCE_DISCHARGE,
        )

        assert len(result) == 2
        fc = [g for g in result if g["workMode"] == "ForceCharge"][0]
        assert fc["enable"] == 1

    def test_empty_schedule(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {"enable": 0, "groups": []}

        new_group = self._make_group("ForceCharge", 10, 0, 12, 0)
        result = _merge_with_existing(
            inverter,
            new_group,
            WorkMode.FORCE_CHARGE,
        )

        assert len(result) == 1

    def test_non_overlapping_different_mode_retained(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                self._make_group("ForceDischarge", 17, 0, 20, 0),
            ],
        }

        new_group = self._make_group("ForceCharge", 10, 0, 12, 0)
        result = _merge_with_existing(
            inverter,
            new_group,
            WorkMode.FORCE_CHARGE,
        )

        assert len(result) == 2
        modes = [g["workMode"] for g in result]
        assert "ForceDischarge" in modes
        assert "ForceCharge" in modes

    def test_past_groups_retained(self) -> None:
        """Past groups may be recurring daily schedules and must be kept."""
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                self._make_group("ForceCharge", 8, 0, 10, 0),
            ],
        }

        new_group = self._make_group("ForceDischarge", 14, 0, 16, 0)
        result = _merge_with_existing(
            inverter,
            new_group,
            WorkMode.FORCE_DISCHARGE,
        )

        assert len(result) == 2
        modes = [g["workMode"] for g in result]
        assert "ForceCharge" in modes
        assert "ForceDischarge" in modes

    def test_extra_fields_stripped(self) -> None:
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    **self._make_group("ForceDischarge", 17, 0, 20, 0),
                    "extraField": "unexpected",
                },
            ],
        }

        new_group = self._make_group("ForceCharge", 10, 0, 12, 0)
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            result = _merge_with_existing(
                inverter,
                new_group,
                WorkMode.FORCE_CHARGE,
            )

        retained = result[0]
        assert "extraField" not in retained

    def test_self_use_baseline_dropped(self) -> None:
        """A full-day SelfUse group should not block force actions."""
        inverter = MagicMock(spec=Inverter)
        inverter.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                self._make_group("SelfUse", 0, 0, 23, 59),
            ],
        }

        new_group = self._make_group("ForceCharge", 14, 0, 16, 0)
        result = _merge_with_existing(
            inverter,
            new_group,
            WorkMode.FORCE_CHARGE,
        )

        assert len(result) == 1
        assert result[0]["workMode"] == "ForceCharge"


class TestSanitizeGroup:
    """Tests for _sanitize_group."""

    def test_strips_unknown_keys(self) -> None:
        raw: dict[str, Any] = {
            "enable": 1,
            "startHour": 10,
            "startMinute": 0,
            "endHour": 12,
            "endMinute": 0,
            "workMode": "ForceCharge",
            "minSocOnGrid": 15,
            "fdSoc": 100,
            "fdPwr": 10500,
            "id": 12345,
            "properties": {},
        }
        result = _sanitize_group(raw)
        assert "id" not in result
        assert "properties" not in result
        assert result["workMode"] == "ForceCharge"


class TestIsPlaceholder:
    """Tests for _is_placeholder."""

    def test_invalid_mode_is_placeholder(self) -> None:
        assert _is_placeholder({"workMode": "Invalid", "enable": 0})

    def test_empty_mode_is_placeholder(self) -> None:
        assert _is_placeholder({"workMode": "", "enable": 0})

    def test_missing_mode_is_placeholder(self) -> None:
        assert _is_placeholder({"enable": 0})

    def test_real_mode_is_not_placeholder(self) -> None:
        assert not _is_placeholder({"workMode": "ForceCharge", "enable": 1})

    def test_disabled_real_mode_is_not_placeholder(self) -> None:
        assert not _is_placeholder({"workMode": "ForceCharge", "enable": 0})


class TestIsExpired:
    """Tests for _is_expired."""

    def test_expired(self) -> None:
        group: ScheduleGroup = {
            "enable": 1,
            "startHour": 8,
            "startMinute": 0,
            "endHour": 10,
            "endMinute": 0,
            "workMode": "ForceCharge",
            "minSocOnGrid": 10,
            "fdSoc": 100,
            "fdPwr": 10500,
        }
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            assert _is_expired(group)

    def test_not_expired(self) -> None:
        group: ScheduleGroup = {
            "enable": 1,
            "startHour": 14,
            "startMinute": 0,
            "endHour": 16,
            "endMinute": 0,
            "workMode": "ForceCharge",
            "minSocOnGrid": 10,
            "fdSoc": 100,
            "fdPwr": 10500,
        }
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            assert not _is_expired(group)


class TestCalculateChargePower:
    """Tests for _calculate_charge_power."""

    def test_basic_calculation(self) -> None:
        # 50% -> 100% of 10 kWh battery in 2 hours = 5 kWh / 2h = 2500W
        result = _calculate_charge_power(50.0, 100, 10.0, 2.0, 10000)
        assert result == 2500

    def test_result_is_int(self) -> None:
        result = _calculate_charge_power(50.0, 100, 10.0, 3.0, 10000)
        assert isinstance(result, int)

    def test_clamped_to_min(self) -> None:
        # Very small energy needed → clamped to 100W
        result = _calculate_charge_power(99.0, 100, 10.0, 4.0, 10000)
        assert result == 100

    def test_clamped_to_max(self) -> None:
        # Large energy in short time → clamped to max_power_w
        result = _calculate_charge_power(0.0, 100, 20.0, 0.5, 5000)
        assert result == 5000

    def test_zero_remaining_hours(self) -> None:
        result = _calculate_charge_power(50.0, 100, 10.0, 0.0, 8000)
        assert result == 8000

    def test_negative_remaining_hours(self) -> None:
        result = _calculate_charge_power(50.0, 100, 10.0, -1.0, 8000)
        assert result == 8000

    def test_soc_at_target(self) -> None:
        # energy_needed <= 0, returns 100
        result = _calculate_charge_power(80.0, 80, 10.0, 2.0, 10000)
        assert result == 100

    def test_soc_above_target(self) -> None:
        result = _calculate_charge_power(90.0, 80, 10.0, 2.0, 10000)
        assert result == 100

    def test_consumption_increases_power(self) -> None:
        # 5kWh / 2h = 2500W battery + 1500W consumption = 4000W
        result = _calculate_charge_power(
            50.0, 100, 10.0, 2.0, 10000, net_consumption_kw=1.5
        )
        assert result == 4000

    def test_consumption_clamped_to_max(self) -> None:
        # 5kWh / 2h = 2500W + 8000W consumption = 10500W → clamped to 10000
        result = _calculate_charge_power(
            50.0, 100, 10.0, 2.0, 10000, net_consumption_kw=8.0
        )
        assert result == 10000

    def test_negative_consumption_ignored(self) -> None:
        # Solar surplus → net negative; should not reduce charge power
        base = _calculate_charge_power(50.0, 100, 10.0, 2.0, 10000)
        with_solar = _calculate_charge_power(
            50.0, 100, 10.0, 2.0, 10000, net_consumption_kw=-3.0
        )
        assert with_solar == base


class TestGetNetConsumption:
    """Tests for _get_net_consumption."""

    def test_returns_loads_minus_pv(self) -> None:
        hass = MagicMock()
        coordinator = MagicMock()
        coordinator.data = {"loadsPower": 3.5, "pvPower": 1.2}
        hass.data = {DOMAIN: {"entry1": {"coordinator": coordinator}}}
        result = _get_net_consumption(hass)
        assert result == pytest.approx(2.3)

    def test_returns_negative_when_solar_exceeds(self) -> None:
        hass = MagicMock()
        coordinator = MagicMock()
        coordinator.data = {"loadsPower": 1.0, "pvPower": 4.0}
        hass.data = {DOMAIN: {"entry1": {"coordinator": coordinator}}}
        result = _get_net_consumption(hass)
        assert result == pytest.approx(-3.0)

    def test_returns_zero_when_no_domain_data(self) -> None:
        hass = MagicMock()
        hass.data = {}
        assert _get_net_consumption(hass) == 0.0

    def test_returns_zero_when_coordinator_data_none(self) -> None:
        hass = MagicMock()
        coordinator = MagicMock()
        coordinator.data = None
        hass.data = {DOMAIN: {"entry1": {"coordinator": coordinator}}}
        assert _get_net_consumption(hass) == 0.0

    def test_returns_zero_when_values_missing(self) -> None:
        hass = MagicMock()
        coordinator = MagicMock()
        coordinator.data = {"SoC": 50}
        hass.data = {DOMAIN: {"entry1": {"coordinator": coordinator}}}
        # loadsPower/pvPower missing → default to 0
        assert _get_net_consumption(hass) == 0.0

    def test_skips_underscore_keys(self) -> None:
        hass = MagicMock()
        coordinator = MagicMock()
        coordinator.data = {"loadsPower": 5.0, "pvPower": 1.0}
        hass.data = {
            DOMAIN: {
                "_state": {"some": "data"},
                "entry1": {"coordinator": coordinator},
            }
        }
        assert _get_net_consumption(hass) == pytest.approx(4.0)


class TestCalculateDeferredStart:
    """Tests for _calculate_deferred_start."""

    def test_basic_deferral(self) -> None:
        # 10kWh * 60% = 6kWh; 10.5kW - 10% headroom = 9.45kW; 6/9.45 = 0.635h ≈ 38min
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(20.0, 80, 10.0, 10500, end)
        # deferred = 06:00 - 38.1min ≈ 05:21
        assert result.hour == 5
        assert result.minute == 21

    def test_soc_at_target_returns_end(self) -> None:
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(80.0, 80, 10.0, 10500, end)
        assert result == end

    def test_soc_above_target_returns_end(self) -> None:
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(90.0, 80, 10.0, 10500, end)
        assert result == end

    def test_large_battery_defers_less(self) -> None:
        # 60kWh * 60% = 36kWh; 9.45kW effective; 36/9.45 = 3.81h
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(20.0, 80, 60.0, 10500, end)
        # 06:00 - 3.81h = 02:11
        assert result.hour == 2
        assert result.minute == 11

    def test_small_charge_needed_defers_more(self) -> None:
        # 10kWh * 10% = 1kWh; 9.45kW effective; 1/9.45 = 0.106h ≈ 6min
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(70.0, 80, 10.0, 10500, end)
        assert result.hour == 5
        assert result.minute == 53

    def test_consumption_brings_start_earlier(self) -> None:
        # 6kWh needed; 10.5kW - 3kW consumption = 7.5kW effective; 6/7.5 = 0.8h = 48min
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(
            20.0, 80, 10.0, 10500, end, net_consumption_kw=3.0
        )
        assert result.hour == 5
        assert result.minute == 12

    def test_high_consumption_uses_remaining_capacity(self) -> None:
        # Consumption nearly equals max power — effective charge is tiny
        # 6kWh needed; 10.5kW - 10kW = 0.5kW; 6/0.5 = 12h
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(
            20.0, 80, 10.0, 10500, end, net_consumption_kw=10.0
        )
        # 06:00 - 12h = 18:00 previous day
        assert result.hour == 18

    def test_negative_consumption_ignored(self) -> None:
        # Solar exceeding load → net negative; treated as 0 → min headroom applies
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        no_consumption = _calculate_deferred_start(20.0, 80, 10.0, 10500, end)
        with_solar = _calculate_deferred_start(
            20.0, 80, 10.0, 10500, end, net_consumption_kw=-2.0
        )
        assert with_solar == no_consumption


class TestResolveStartEndExplicit:
    """Tests for _resolve_start_end_explicit."""

    def test_valid_window(self) -> None:
        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            start, end = _resolve_start_end_explicit(
                datetime.time(17, 0), datetime.time(20, 0)
            )
            assert start == datetime.datetime(2026, 4, 7, 17, 0, 0)
            assert end == datetime.datetime(2026, 4, 7, 20, 0, 0)

    def test_end_before_start_rejected(self) -> None:
        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="after start"),
        ):
            _resolve_start_end_explicit(datetime.time(20, 0), datetime.time(17, 0))

    def test_equal_times_rejected(self) -> None:
        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="after start"),
        ):
            _resolve_start_end_explicit(datetime.time(17, 0), datetime.time(17, 0))

    def test_exceeds_max_hours(self) -> None:
        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="4 hours"),
        ):
            _resolve_start_end_explicit(datetime.time(10, 0), datetime.time(15, 0))


class TestRemoveModeFromSchedule:
    """Tests for _remove_mode_from_schedule."""

    @staticmethod
    def _make_group(mode: str, sh: int, eh: int) -> dict[str, Any]:
        return {
            "enable": 1,
            "startHour": sh,
            "startMinute": 0,
            "endHour": eh,
            "endMinute": 0,
            "workMode": mode,
            "minSocOnGrid": 15,
            "fdSoc": 100,
            "fdPwr": 10500,
        }

    def test_removes_target_mode_keeps_others(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {
            "groups": [
                self._make_group("ForceCharge", 2, 6),
                self._make_group("ForceDischarge", 17, 20),
            ]
        }

        _remove_mode_from_schedule(inv, WorkMode.FORCE_DISCHARGE, 15)

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceCharge"

    def test_falls_back_to_self_use_when_empty(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {
            "groups": [self._make_group("ForceDischarge", 17, 20)]
        }

        _remove_mode_from_schedule(inv, WorkMode.FORCE_DISCHARGE, 15)

        inv.self_use.assert_called_once_with(15)
        inv.set_schedule.assert_not_called()

    def test_skips_placeholders(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {
            "groups": [
                {"workMode": "Invalid", "enable": 0},
                self._make_group("ForceCharge", 2, 6),
            ]
        }

        _remove_mode_from_schedule(inv, WorkMode.FORCE_DISCHARGE, 15)

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceCharge"

    def test_re_enables_disabled_groups(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {
            "groups": [
                {**self._make_group("ForceCharge", 2, 6), "enable": 0},
            ]
        }

        _remove_mode_from_schedule(inv, WorkMode.FORCE_DISCHARGE, 15)

        groups = inv.set_schedule.call_args.args[0]
        assert groups[0]["enable"] == 1


class TestGetCurrentSoc:
    """Tests for _get_current_soc helper."""

    def _make_hass(
        self,
        coordinator_soc: float | None = None,
    ) -> MagicMock:
        hass = MagicMock()

        # Coordinator
        mock_coordinator = MagicMock()
        if coordinator_soc is not None:
            mock_coordinator.data = {"SoC": coordinator_soc}
        else:
            mock_coordinator.data = None

        hass.data = {
            DOMAIN: {
                "entry1": {
                    "inverter": MagicMock(),
                    "coordinator": mock_coordinator,
                },
            }
        }
        return hass

    def test_returns_coordinator_soc(self) -> None:
        hass = self._make_hass(coordinator_soc=60.0)
        assert _get_current_soc(hass) == 60.0

    def test_returns_none_when_unavailable(self) -> None:
        hass = self._make_hass(coordinator_soc=None)
        assert _get_current_soc(hass) is None


class TestGetNetConsumptionParseErrors:
    """Tests for _get_net_consumption when coordinator returns bad values."""

    def test_string_value_returns_zero_and_logs(self) -> None:
        hass = MagicMock()
        coordinator = MagicMock()
        coordinator.data = {"loadsPower": "12.5kW", "pvPower": 1.0}
        hass.data = {DOMAIN: {"entry1": {"coordinator": coordinator}}}
        result = _get_net_consumption(hass)
        assert result == 0.0

    def test_none_values_returns_zero(self) -> None:
        hass = MagicMock()
        coordinator = MagicMock()
        coordinator.data = {"loadsPower": None, "pvPower": None}
        hass.data = {DOMAIN: {"entry1": {"coordinator": coordinator}}}
        result = _get_net_consumption(hass)
        assert result == 0.0

    def test_empty_coordinator_data_returns_zero(self) -> None:
        """loadsPower/pvPower absent → defaults to 0, so net = 0."""
        hass = MagicMock()
        coordinator = MagicMock()
        coordinator.data = {}
        hass.data = {DOMAIN: {"entry1": {"coordinator": coordinator}}}
        assert _get_net_consumption(hass) == 0.0


class TestCalculateChargePowerEdgeCases:
    """Edge case tests for _calculate_charge_power with consumption."""

    def test_consumption_exceeds_max_power(self) -> None:
        # 5kWh/2h = 2500W + 20000W consumption → clamped to 10000
        result = _calculate_charge_power(
            50.0, 100, 10.0, 2.0, 10000, net_consumption_kw=20.0
        )
        assert result == 10000

    def test_zero_battery_capacity(self) -> None:
        # 0 capacity → 0 energy needed → min power 100
        result = _calculate_charge_power(
            50.0, 100, 0.0, 2.0, 10000, net_consumption_kw=3.0
        )
        assert result == 100

    def test_very_small_remaining_with_consumption(self) -> None:
        # Tiny remaining time → max power regardless of consumption
        result = _calculate_charge_power(
            50.0, 100, 10.0, 0.001, 10000, net_consumption_kw=2.0
        )
        assert result == 10000


class TestCalculateDeferredStartEdgeCases:
    """Edge case tests for _calculate_deferred_start with consumption."""

    def test_consumption_equals_max_power(self) -> None:
        # Net consumption = max power → fallback to 10% headroom
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(
            20.0, 80, 10.0, 10500, end, net_consumption_kw=10.5
        )
        # effective = 10% of 10.5 = 1.05kW; 6kWh / 1.05 = 5.71h
        # Start = 06:00 - 5.71h = 00:17
        assert result.hour == 0
        assert result.minute == 17

    def test_consumption_exceeds_max_power(self) -> None:
        # Net consumption > max power → same fallback to 10%
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(
            20.0, 80, 10.0, 10500, end, net_consumption_kw=15.0
        )
        # Same as above: effective = 1.05kW
        assert result.hour == 0
        assert result.minute == 17

    def test_zero_battery_capacity(self) -> None:
        end = datetime.datetime(2026, 4, 7, 6, 0, 0)
        result = _calculate_deferred_start(
            50.0, 100, 0.0, 10500, end, net_consumption_kw=3.0
        )
        # 0 energy needed → returns end
        assert result == end
