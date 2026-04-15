"""End-to-end tests: real HA container + FoxESS simulator.

Run with: pytest e2e/ -m slow
Requires: podman, PyJWT

Fixture scoping:
- foxess_sim + ha_e2e: session scope (one per xdist worker)
- _e2e_reset: autouse function scope (resets sim + clears HA)
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow


def _tight_window(minutes: int = 30) -> tuple[str, str]:
    """Return a tight window starting ~now (UTC).

    Short windows force the algorithm to start immediately (no deferral),
    since the entire energy budget must be delivered within the window.
    Uses UTC because the HA container defaults to UTC timezone.
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    start = now - datetime.timedelta(minutes=2)
    end = start + datetime.timedelta(minutes=minutes)
    return (
        f"{start.hour:02d}:{start.minute:02d}:00",
        f"{end.hour:02d}:{end.minute:02d}:00",
    )


class TestSmartDischarge:
    def test_discharge_starts(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """Service call → schedule written → state transitions."""
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )

        state = ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
        )
        assert state == "discharging"

        sim_state = foxess_sim.state()
        assert sim_state["schedule_enabled"]

    def test_discharge_drains_battery(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """Fast-forward and verify SoC decreases."""
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
        )

        foxess_sim.fast_forward(600, step=5)

        soc = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "lt", 80.0, timeout_s=60
        )
        assert soc < 80


class TestSmartCharge:
    def test_charge_starts(self, ha_e2e: HAClient, foxess_sim: SimulatorHandle) -> None:
        """Service call starts a charge session."""
        foxess_sim.set(soc=20, solar_kw=0, load_kw=0.3)

        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {"start_time": start, "end_time": end, "target_soc": 80},
        )

        state = ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "charging",
            timeout_s=120,
        )
        assert state == "charging"


class TestFaultInjection:
    def test_ws_unit_mismatch_handled(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """WS sends kW instead of W — integration handles it."""
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 20},
        )

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
        )

        # Switch to kW units AFTER discharge starts
        foxess_sim.ws_unit("kW")
        foxess_sim.fast_forward(60, step=5)

        # SoC should still reflect actual discharge (unit detection works)
        soc = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "lt", 80.0, timeout_s=60
        )
        assert soc < 80

        foxess_sim.ws_unit("W")


class TestDataSource:
    def test_api_source_when_idle(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """When idle, data source should be API."""
        ha_e2e.wait_for_state("sensor.foxess_smart_operations", "idle", timeout_s=30)
        attrs = ha_e2e.get_attributes("sensor.foxess_battery_soc")
        assert attrs.get("data_source") in ("api", None)
