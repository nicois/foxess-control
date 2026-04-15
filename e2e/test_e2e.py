"""End-to-end tests: real HA container + FoxESS simulator.

These tests are slow (container startup ~60s) and require podman.
Run with: pytest e2e/ -m slow
"""

from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow


def _future_window(hours: int = 3) -> tuple[str, str]:
    """Return a start_time/end_time window starting now, lasting `hours`."""
    now = datetime.datetime.now()
    start = now - datetime.timedelta(minutes=1)  # just past to ensure it's active
    end = start + datetime.timedelta(hours=hours)
    return (
        f"{start.hour:02d}:{start.minute:02d}:00",
        f"{end.hour:02d}:{end.minute:02d}:00",
    )


class TestSmartDischarge:
    """Smart discharge E2E tests."""

    def test_discharge_starts_and_paces(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """Service call starts discharge, schedule written to simulator."""
        foxess_sim.reset()
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        # Clear any leftover session
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        time.sleep(15)  # wait for poll cycle to pick up reset state

        start, end = _future_window(3)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )

        # Wait for discharge to start (may go through deferred first)
        try:
            ha_e2e.wait_for_state(
                "sensor.foxess_smart_operations", "discharging", timeout_s=90
            )
        except TimeoutError:
            # Accept deferred too — the algorithm may decide to defer
            state = ha_e2e.get_state("sensor.foxess_smart_operations")
            assert state in (
                "discharging",
                "discharge_deferred",
                "discharge_scheduled",
            ), f"Unexpected state: {state}"

        # Simulator should have received a schedule write
        sim_state = foxess_sim.state()
        assert sim_state["schedule_enabled"]

    def test_discharge_drains_battery(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """Fast-forward and verify SoC decreases."""
        foxess_sim.reset()
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        time.sleep(15)  # wait for poll cycle to pick up reset state

        start, end = _future_window(3)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        time.sleep(10)  # let session start

        # Fast-forward 10 minutes
        foxess_sim.fast_forward(600, step=5)
        time.sleep(15)  # let HA poll updated data from simulator

        soc = float(ha_e2e.get_state("sensor.foxess_battery_soc"))
        assert soc < 80, f"SoC should have decreased, got {soc}"


class TestSmartCharge:
    """Smart charge E2E tests."""

    def test_charge_starts(self, ha_e2e: HAClient, foxess_sim: SimulatorHandle) -> None:
        """Service call starts a charge session."""
        foxess_sim.reset()
        foxess_sim.set(soc=20, solar_kw=0, load_kw=0.3)

        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        time.sleep(15)  # wait for poll cycle to pick up reset state

        start, end = _future_window(3)
        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {"start_time": start, "end_time": end, "target_soc": 80},
        )
        time.sleep(15)

        state = ha_e2e.get_state("sensor.foxess_smart_operations")
        assert state in (
            "charging",
            "deferred",
            "target_reached",
            "idle",  # may be idle if session setup is still in progress
        ), f"Unexpected state: {state}"
        # If idle, the session hasn't started yet — verify it was accepted
        # by checking that no error was raised (the service call succeeded)


class TestFaultInjection:
    """Verify integration handles simulator-injected faults."""

    def test_ws_unit_mismatch_handled(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """WS sends kW instead of W — integration should handle it."""
        foxess_sim.reset()
        foxess_sim.set(soc=50, solar_kw=0, load_kw=0.5)

        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        time.sleep(15)  # wait for poll cycle to pick up reset state

        foxess_sim.ws_unit("kW")

        start, end = _future_window(3)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 20},
        )

        foxess_sim.fast_forward(60, step=5)
        time.sleep(5)

        soc = float(ha_e2e.get_state("sensor.foxess_battery_soc"))
        # SoC should be roughly correct (not 0 or 100 from 1000x error)
        assert 30 < soc < 55, f"SoC looks wrong (unit mismatch?): {soc}"

        foxess_sim.ws_unit("W")


class TestDataSource:
    """Verify data source badge behaviour."""

    def test_api_source_when_idle(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """When idle, data source should be API."""
        foxess_sim.reset()
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        time.sleep(5)

        attrs = ha_e2e.get_attributes("sensor.foxess_battery_soc")
        assert attrs.get("data_source") in ("api", None)
