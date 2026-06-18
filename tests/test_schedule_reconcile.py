"""Tests for the startup schedule-reconcile (issue-#11 leftover-group defence)."""

from __future__ import annotations

from typing import Any

from custom_components.foxess_control._schedule_reconcile import find_orphan_modes


def _g(mode: str, sh: int, eh: int, enable: int = 1) -> dict[str, Any]:
    return {
        "enable": enable,
        "startHour": sh,
        "startMinute": 0,
        "endHour": eh,
        "endMinute": 0,
        "workMode": mode,
        "minSocOnGrid": 11,
        "fdSoc": 100,
        "fdPwr": 10000,
    }


class TestFindOrphanModes:
    def test_enabled_managed_group_with_no_cover_is_orphan(self) -> None:
        groups = [_g("ForceCharge", 11, 14)]
        assert find_orphan_modes(groups, covered_modes=set()) == ["ForceCharge"]

    def test_covered_group_is_not_orphan(self) -> None:
        groups = [_g("ForceCharge", 11, 14)]
        assert find_orphan_modes(groups, covered_modes={"ForceCharge"}) == []

    def test_self_use_is_never_orphan(self) -> None:
        groups = [_g("SelfUse", 0, 23)]
        assert find_orphan_modes(groups, covered_modes=set()) == []

    def test_disabled_group_is_not_orphan(self) -> None:
        groups = [_g("ForceCharge", 11, 14, enable=0)]
        assert find_orphan_modes(groups, covered_modes=set()) == []

    def test_discharge_and_feedin_orphans_detected(self) -> None:
        groups = [_g("ForceDischarge", 17, 20), _g("Feedin", 9, 11)]
        assert sorted(find_orphan_modes(groups, covered_modes=set())) == [
            "Feedin",
            "ForceDischarge",
        ]

    def test_unmanaged_group_is_not_reported_as_orphan(self) -> None:
        # Backup is unmanaged — it is not an orphan we would remove; the
        # C-018 block is handled separately by the orchestrator.
        groups = [_g("Backup", 0, 23)]
        assert find_orphan_modes(groups, covered_modes=set()) == []

    def test_mixed_covered_and_orphan(self) -> None:
        groups = [_g("ForceCharge", 11, 14), _g("ForceDischarge", 17, 20)]
        assert find_orphan_modes(groups, covered_modes={"ForceCharge"}) == [
            "ForceDischarge"
        ]
