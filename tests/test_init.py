"""Tests for FoxESS Control integration setup and service handlers."""

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.foxess_control import (
    _build_override_group,
    _groups_overlap,
    _is_expired,
    _is_placeholder,
    _merge_with_existing,
    _resolve_start_end,
    _sanitize_group,
)
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
        """fdSoc below 11 is clamped to 11 (API minimum)."""
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

        now = datetime.datetime(2026, 4, 7, 18, 0, 0)
        end = datetime.datetime(2026, 4, 7, 20, 0, 0)

        group = _build_override_group(
            now, end, WorkMode.FORCE_DISCHARGE, inverter, min_soc_on_grid=15, fd_soc=10
        )

        assert group["fdSoc"] == 11
        assert group["minSocOnGrid"] == 11  # clamped to <= fdSoc

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
