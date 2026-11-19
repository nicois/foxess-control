"""E2E: the local gateway backend in a real Home Assistant (C-029).

A containerised HA loads a local-gateway config entry pointing at the
simulator's foxess-local ``/api/v1``. Phase 1 is read-only: entities are
populated from the gateway, live values arrive over the event stream
(well inside the 60 s ``/state`` poll), and control services refuse with
an explanation.

``connection_mode`` is pinned to ``"local"`` for this module only; the
shared cloud/entity suites are not re-run in a mode that cannot control
the inverter yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def connection_mode() -> str:
    return "local"


class TestLocalSensors:
    def test_entities_come_from_the_gateway(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        ha_e2e.wait_for_state("sensor.foxess_data_freshness", "local", timeout_s=30)
        # Pinned rather than read back from the free-running model, whose
        # SoC drifts with the simulated load and could cross a rounding
        # boundary between the two reads (C-031).
        foxess_sim.set(fuzzing=False, soc=61.0)
        soc = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "eq", 61.0, timeout_s=30
        )
        assert soc == 61.0

    def test_live_values_arrive_over_the_event_stream(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        """30 s is half the /state poll: only the stream can deliver it."""
        foxess_sim.set(soc=73.0)
        soc = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "eq", 73.0, timeout_s=30
        )
        assert soc == 73.0

    def test_lifetime_feed_in_counter(
        self, ha_e2e: HAClient, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(feedin_total_kwh=1234.5)
        value = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_grid_feed_in_energy", "eq", 1234.5, timeout_s=30
        )
        assert value == 1234.5


class TestLocalReadOnly:
    def test_control_service_refuses_with_an_explanation(
        self, ha_e2e: HAClient
    ) -> None:
        error = ha_e2e.refused_service(
            "foxess_control", "force_charge", {"duration": "00:30:00"}
        )
        assert error.get("translation_key") == "local_control_unavailable", error
