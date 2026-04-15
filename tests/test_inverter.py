"""Tests for Inverter high-level control.

Uses the FoxESS simulator for realistic HTTP interactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter

if TYPE_CHECKING:
    from .conftest import SimulatorHandle


@pytest.fixture(autouse=True)
def _disable_throttle() -> None:
    """Disable request throttling in tests."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


def _make_inv(sim: SimulatorHandle) -> Inverter:
    client = FoxESSClient("test-api-key", base_url=sim.url)
    return Inverter(client, "SIM0001")


def test_auto_detect(foxess_sim: SimulatorHandle) -> None:
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    inv = Inverter.auto_detect(client)
    assert inv.sn == "SIM0001"


def test_get_soc(foxess_sim: SimulatorHandle) -> None:
    foxess_sim.set(soc=75.5)
    inv = _make_inv(foxess_sim)
    # Simulator returns integer SoC (like real API)
    assert inv.get_soc() == 75.0


def test_self_use(foxess_sim: SimulatorHandle) -> None:
    inv = _make_inv(foxess_sim)
    inv.self_use()
    state = foxess_sim.state()
    assert state["schedule_enabled"]
    assert state["work_mode"] == "SelfUse"


def test_force_charge(foxess_sim: SimulatorHandle) -> None:
    inv = _make_inv(foxess_sim)
    inv.force_charge(target_soc=80)
    state = foxess_sim.state()
    assert state["work_mode"] == "ForceCharge"


def test_force_discharge(foxess_sim: SimulatorHandle) -> None:
    inv = _make_inv(foxess_sim)
    inv.force_discharge(min_soc=20, power=3000)
    state = foxess_sim.state()
    assert state["work_mode"] == "ForceDischarge"
    # Verify the schedule group has correct fdPwr
    groups = [g for g in state["schedule_groups"] if g["workMode"] == "ForceDischarge"]
    assert groups[0]["fdPwr"] == 3000


def test_max_power_cached(foxess_sim: SimulatorHandle) -> None:
    """max_power_w queries device detail once and caches."""
    inv = _make_inv(foxess_sim)
    pw1 = inv.max_power_w
    pw2 = inv.max_power_w
    assert pw1 == pw2
    assert pw1 > 0


def test_get_battery_status(foxess_sim: SimulatorHandle) -> None:
    foxess_sim.set(soc=65, bat_charge_kw=1.2)
    # Tick to update derived values
    foxess_sim.tick(0)
    inv = _make_inv(foxess_sim)
    status = inv.get_battery_status()
    assert status["SoC"] == 65


def test_get_current_mode_force_charge(foxess_sim: SimulatorHandle) -> None:
    inv = _make_inv(foxess_sim)
    inv.force_charge(target_soc=100)
    mode = inv.get_current_mode()
    assert mode == "ForceCharge"


def test_get_schedule_empty(foxess_sim: SimulatorHandle) -> None:
    """No schedule set — returns 8 groups (padded with placeholders)."""
    foxess_sim.reset()
    inv = _make_inv(foxess_sim)
    schedule = inv.get_schedule()
    assert len(schedule["groups"]) == 8


def test_get_current_mode_no_schedule(foxess_sim: SimulatorHandle) -> None:
    """No active schedule groups — returns None."""
    foxess_sim.reset()
    inv = _make_inv(foxess_sim)
    mode = inv.get_current_mode()
    assert mode is None
