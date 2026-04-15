"""End-to-end tests: real HA container + FoxESS simulator.

These tests are slow (container startup ~60s) and require podman.
Run with: pytest e2e/ -m slow
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow


class TestSmartDischarge:
    """Smart discharge E2E tests."""

    def test_discharge_starts_and_paces(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """Service call → pacing → schedule write → entity update."""
        foxess_sim.reset()
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {
                "start_time": "00:00:00",
                "end_time": "23:59:00",
                "min_soc": 30,
            },
        )

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations", "discharging", timeout_s=60
        )

        # Simulator should have received a schedule write
        state = foxess_sim.state()
        assert state["schedule_enabled"]
        assert state["work_mode"] == "ForceDischarge"

    def test_discharge_drains_battery(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """Fast-forward time and verify SoC decreases."""
        foxess_sim.reset()
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {
                "start_time": "00:00:00",
                "end_time": "23:59:00",
                "min_soc": 30,
            },
        )

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations", "discharging", timeout_s=60
        )

        # Fast-forward 10 minutes
        foxess_sim.fast_forward(600, step=5)
        time.sleep(3)  # let HA process WS burst + REST poll

        soc = float(ha_e2e.get_state("sensor.foxess_battery_soc"))
        assert soc < 80, f"SoC should have decreased, got {soc}"


class TestSmartCharge:
    """Smart charge E2E tests."""

    def test_charge_starts(self, ha_e2e: HAClient, foxess_sim: SimulatorHandle) -> None:
        """Service call starts a charge session."""
        foxess_sim.reset()
        foxess_sim.set(soc=20, solar_kw=0, load_kw=0.3)

        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {
                "start_time": "00:00:00",
                "end_time": "23:59:00",
                "target_soc": 80,
            },
        )

        # Should transition to charging or deferred
        time.sleep(10)
        state = ha_e2e.get_state("sensor.foxess_smart_operations")
        assert state in ("charging", "deferred"), f"Unexpected state: {state}"


class TestFaultInjection:
    """Verify integration handles simulator-injected faults."""

    def test_ws_unit_mismatch_handled(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """WS sends kW instead of W — integration should detect and handle."""
        foxess_sim.reset()
        foxess_sim.set(soc=50, solar_kw=0, load_kw=0.5)
        foxess_sim.ws_unit("kW")

        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {
                "start_time": "00:00:00",
                "end_time": "23:59:00",
                "min_soc": 20,
            },
        )

        foxess_sim.fast_forward(60, step=5)
        time.sleep(3)

        # SoC should reflect actual discharge, not 1000x error
        soc = float(ha_e2e.get_state("sensor.foxess_battery_soc"))
        assert 40 < soc < 55, f"SoC looks wrong (unit mismatch?): {soc}"

        # Reset WS unit for other tests
        foxess_sim.ws_unit("W")


class TestDataSource:
    """Verify data source badge behaviour."""

    def test_api_source_when_idle(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """When idle, data source should be API."""
        foxess_sim.reset()
        time.sleep(5)

        attrs = ha_e2e.get_attributes("sensor.foxess_battery_soc")
        # data_source attribute exists because web credentials are configured
        assert attrs.get("data_source") in ("api", None)
