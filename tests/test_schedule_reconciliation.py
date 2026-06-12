"""Integration test: poll-time reconciliation surfaces a schedule that the
inverter ACKs but does not apply (issue #11).

Exercises the brand wiring (reconcile_work_mode) against a real HA issue
registry and real domain data — not mocks.  The simulator silent-drop
seam (Part A) is exercised end-to-end by the E2E test in a later task;
here we drive reconcile_work_mode directly with controlled commanded
intent + reported mode + grace.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.foxess_control._helpers import _dd
from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.domain_data import FoxESSControlData
from custom_components.foxess_control.foxess_adapter import (
    _SCHEDULE_NOT_APPLIED_ISSUE,
    reconcile_work_mode,
)

if TYPE_CHECKING:
    from homeassistant.helpers.issue_registry import IssueEntry


@pytest_asyncio.fixture  # type: ignore[untyped-decorator]
async def reconcile_hass() -> HomeAssistant:
    # HomeAssistant() captures the running event loop in __init__, so it
    # must be built inside an async context (mirrors the harness used by
    # tests/test_sensor_listener_safety.py).
    ha = HomeAssistant("/tmp")
    ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
    ha.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    ha.data[DOMAIN] = FoxESSControlData()
    return ha


def _set_commanded(hass: HomeAssistant, mode_value: str, at: datetime.datetime) -> None:
    dd = _dd(hass)
    dd.commanded_work_mode = mode_value
    dd.commanded_work_mode_at = at.isoformat()


def _issues(hass: HomeAssistant) -> list[IssueEntry]:
    registry = ir.async_get(hass)
    return [
        i
        for i in registry.issues.values()
        if i.domain == DOMAIN and i.issue_id == _SCHEDULE_NOT_APPLIED_ISSUE
    ]


class TestReconcileSurfacing:
    @pytest.mark.asyncio
    async def test_conflict_past_grace_raises_repair(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, "ForceCharge", long_ago)

        reconcile_work_mode(hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300))

        assert _issues(hass), "Expected a schedule_not_applied Repair after grace"
        dd = _dd(hass)
        assert any(
            e.get("category") == "schedule_not_applied" for e in dd.recent_errors
        ), "Expected a schedule_not_applied operational error in the diagnostics buffer"

    @pytest.mark.asyncio
    async def test_within_grace_does_not_raise(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        just_now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=30)
        _set_commanded(hass, "ForceCharge", just_now)

        reconcile_work_mode(hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300))

        assert not _issues(hass), "Should not surface within the grace window"

    @pytest.mark.asyncio
    async def test_match_clears_existing_issue(
        self, reconcile_hass: HomeAssistant
    ) -> None:
        hass = reconcile_hass
        long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
        _set_commanded(hass, "ForceCharge", long_ago)
        reconcile_work_mode(hass, DOMAIN, "SelfUse", datetime.timedelta(seconds=300))
        assert _issues(hass), "pre-condition: conflict raised the issue"

        # Inverter now reports the commanded mode → issue clears.
        reconcile_work_mode(
            hass, DOMAIN, "ForceCharge", datetime.timedelta(seconds=300)
        )
        assert not [i for i in _issues(hass) if i.active], (
            "Issue should clear once modes reconcile"
        )
