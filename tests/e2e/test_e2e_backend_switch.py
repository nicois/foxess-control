"""E2E: switching an entry between cloud and local keeps its entities (C-029).

The point of the in-place switch is that HA's entity registry is untouched:
the same config entry means the same ``{entry_id}_{suffix}`` unique ids, so
every entity id, its history, and whatever dashboards and helpers refer to
it survive a switch in either direction. Only a real HA has a real entity
registry to prove that against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest

if TYPE_CHECKING:
    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def connection_mode() -> str:
    return "cloud"


def test_switch_to_local_and_back_keeps_every_entity(
    ha_e2e: HAClient, foxess_sim: SimulatorHandle
) -> None:
    # Refuse the web-portal WebSocket so the cloud path is deterministically
    # the REST poll ("api"), as the data_source fixture does.
    foxess_sim.fault("ws_refuse")
    ha_e2e.wait_for_state("sensor.foxess_data_freshness", "api", timeout_s=60)
    before = ha_e2e.registry_entities()
    assert "sensor.foxess_battery_soc" in before.values()

    port = urlparse(foxess_sim.url).port
    foxess_sim.set(fuzzing=False, soc=64.0, local_gateway_token="fl_switch")
    result = ha_e2e.reconfigure(
        "reconfigure_local",
        {
            "local_url": f"http://host.containers.internal:{port}",
            "local_token": "Bearer fl_switch",
        },
    )
    assert result.get("reason") == "backend_switched", result

    ha_e2e.wait_for_state("sensor.foxess_data_freshness", "local", timeout_s=60)
    assert (
        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "eq", 64.0, timeout_s=30
        )
        == 64.0
    )
    assert ha_e2e.registry_entities() == before

    # Back to the cloud: the API key was kept, so the form's default is it.
    result = ha_e2e.reconfigure("reconfigure_cloud", {"api_key": "e2e-test-api-key"})
    assert result.get("reason") == "backend_switched", result
    ha_e2e.wait_for_state("sensor.foxess_data_freshness", "api", timeout_s=60)
    assert ha_e2e.registry_entities() == before
