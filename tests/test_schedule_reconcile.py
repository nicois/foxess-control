"""Tests for the startup schedule-reconcile (issue-#11 leftover-group defence)."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from custom_components.foxess_control._helpers import _dd
from custom_components.foxess_control._schedule_reconcile import (
    find_orphan_modes,
    reconcile_schedule,
)
from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    IntegrationConfig,
)
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter, ScheduleGroup


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


# --- Integration tests: the async reconcile_schedule orchestrator ---------


@pytest.fixture(autouse=True)
def _fast_client() -> None:
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


@pytest_asyncio.fixture  # type: ignore[untyped-decorator]
async def reconcile_hass() -> Any:
    # HomeAssistant() captures the running event loop in __init__, so it must
    # be built inside an async context (mirrors tests/test_schedule_reconciliation.py).
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import issue_registry as ir

    ha = HomeAssistant("/tmp")
    ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
    ha.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    dd = FoxESSControlData()
    dd.config = IntegrationConfig(
        min_soc_on_grid=11,
        api_min_soc=11,
        battery_capacity_kwh=10.0,
        min_power_change=100,
        max_power_w=10000,
        grid_export_limit_w=5000,
        smart_headroom=0.10,
        bms_polling_interval=300.0,
        ws_mode="auto",
        entity_mode=False,
    )
    ha.data[DOMAIN] = dd
    return ha


def _inv(sim: Any) -> Inverter:
    return Inverter(FoxESSClient("k", base_url=sim.url), "SIM0001")


def _force_charge_group() -> ScheduleGroup:
    return {
        "enable": 1,
        "startHour": 11,
        "startMinute": 0,
        "endHour": 13,
        "endMinute": 59,
        "workMode": "ForceCharge",
        "minSocOnGrid": 11,
        "fdSoc": 100,
        "fdPwr": 10000,
    }


def _backup_group() -> ScheduleGroup:
    # Window must NOT overlap _force_charge_group (11:00-13:59); the FoxESS API
    # rejects overlapping schedule windows (errno 42023 "Time overlap").
    return {
        "enable": 1,
        "startHour": 14,
        "startMinute": 0,
        "endHour": 23,
        "endMinute": 59,
        "workMode": "Backup",
        "minSocOnGrid": 20,
        "fdSoc": 20,
        "fdPwr": 0,
    }


def _enabled_managed(inv: Inverter) -> list[str]:
    sched = inv.get_schedule()
    return [
        g["workMode"]
        for g in sched.get("groups", [])
        if g.get("enable") == 1
        and g.get("workMode") in ("ForceCharge", "ForceDischarge", "Feedin")
    ]


class TestReconcileOrchestrator:
    @pytest.mark.asyncio
    async def test_orphan_force_charge_removed(
        self, foxess_sim: Any, reconcile_hass: Any
    ) -> None:
        inv = _inv(foxess_sim)
        inv.set_schedule([_force_charge_group()])
        await reconcile_schedule(reconcile_hass, inv)
        assert _enabled_managed(inv) == []
        dd = _dd(reconcile_hass)
        assert any(
            e.get("category") == "orphaned_schedule_removed" for e in dd.recent_errors
        )
        assert dd.last_schedule_reconcile is not None
        assert dd.last_schedule_reconcile["action"] == "removed"

    @pytest.mark.asyncio
    async def test_covered_group_kept(
        self, foxess_sim: Any, reconcile_hass: Any
    ) -> None:
        inv = _inv(foxess_sim)
        inv.set_schedule([_force_charge_group()])
        now = datetime.datetime.now(datetime.UTC)
        # window must MATCH the group (start 11:00, end 13:59) for coverage
        start = now.replace(hour=11, minute=0, second=0, microsecond=0)
        end = now.replace(hour=13, minute=59, second=0, microsecond=0)
        _dd(reconcile_hass).smart_charge_state = {
            "start": start,
            "end": end,
            "target_soc": 100,
        }
        await reconcile_schedule(reconcile_hass, inv)
        assert _enabled_managed(inv) == ["ForceCharge"]
        reconcile = _dd(reconcile_hass).last_schedule_reconcile
        assert reconcile is not None
        assert reconcile["action"] == "none"

    @pytest.mark.asyncio
    async def test_resumed_discharge_with_safe_horizon_group_is_kept(
        self, foxess_sim: Any, reconcile_hass: Any
    ) -> None:
        # A resumed discharge session whose live group has a C-027 safe-horizon
        # end (earlier than the session's full-window end) and a write-time
        # start — windows DIFFER from the session. Must be KEPT (work-mode-only
        # coverage), not removed.
        inv = _inv(foxess_sim)
        inv.set_schedule(
            [
                {
                    "enable": 1,
                    "startHour": 18,
                    "startMinute": 30,
                    "endHour": 19,
                    "endMinute": 30,
                    "workMode": "ForceDischarge",
                    "minSocOnGrid": 11,
                    "fdSoc": 20,
                    "fdPwr": 5000,
                }
            ]
        )
        now = datetime.datetime.now(datetime.UTC)
        # Session's FULL window is 17:00-21:00 — deliberately different from the
        # live group's 18:30-19:30 safe-horizon window.
        _dd(reconcile_hass).smart_discharge_state = {
            "start": now.replace(hour=17, minute=0, second=0, microsecond=0),
            "end": now.replace(hour=21, minute=0, second=0, microsecond=0),
            "min_soc": 11,
        }
        await reconcile_schedule(reconcile_hass, inv)
        assert "ForceDischarge" in _enabled_managed(inv), (
            "a resumed discharge session's safe-horizon group must NOT be removed"
        )
        reconcile = _dd(reconcile_hass).last_schedule_reconcile
        assert reconcile is not None
        assert reconcile["action"] == "none"

    @pytest.mark.asyncio
    async def test_resumed_discharge_covers_feedin_group(
        self, foxess_sim: Any, reconcile_hass: Any
    ) -> None:
        # A discharge session also covers a Feedin-family group.
        inv = _inv(foxess_sim)
        inv.set_schedule(
            [
                {
                    "enable": 1,
                    "startHour": 9,
                    "startMinute": 0,
                    "endHour": 11,
                    "endMinute": 0,
                    "workMode": "Feedin",
                    "minSocOnGrid": 11,
                    "fdSoc": 20,
                    "fdPwr": 5000,
                }
            ]
        )
        now = datetime.datetime.now(datetime.UTC)
        _dd(reconcile_hass).smart_discharge_state = {
            "start": now.replace(hour=8, minute=0, second=0, microsecond=0),
            "end": now.replace(hour=12, minute=0, second=0, microsecond=0),
            "min_soc": 11,
        }
        await reconcile_schedule(reconcile_hass, inv)
        assert "Feedin" in _enabled_managed(inv)
        reconcile = _dd(reconcile_hass).last_schedule_reconcile
        assert reconcile is not None
        assert reconcile["action"] == "none"

    @pytest.mark.asyncio
    async def test_unmanaged_mode_blocks_removal(
        self, foxess_sim: Any, reconcile_hass: Any
    ) -> None:
        inv = _inv(foxess_sim)
        inv.set_schedule([_force_charge_group(), _backup_group()])
        await reconcile_schedule(reconcile_hass, inv)
        assert "ForceCharge" in _enabled_managed(inv)
        reconcile = _dd(reconcile_hass).last_schedule_reconcile
        assert reconcile is not None
        assert reconcile["action"] == "blocked_unmanaged"

    @pytest.mark.asyncio
    async def test_no_orphan_no_write(
        self, foxess_sim: Any, reconcile_hass: Any
    ) -> None:
        inv = _inv(foxess_sim)
        self_use: ScheduleGroup = {
            "enable": 1,
            "startHour": 0,
            "startMinute": 0,
            "endHour": 23,
            "endMinute": 59,
            "workMode": "SelfUse",
            "minSocOnGrid": 11,
            "fdSoc": 11,
            "fdPwr": 10000,
        }
        inv.set_schedule([self_use])
        await reconcile_schedule(reconcile_hass, inv)
        reconcile = _dd(reconcile_hass).last_schedule_reconcile
        assert reconcile is not None
        assert reconcile["action"] == "none"
        assert _dd(reconcile_hass).last_schedule_snapshot is not None
