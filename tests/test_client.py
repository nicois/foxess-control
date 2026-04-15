"""Tests for FoxESS API client authentication and request handling.

Uses the FoxESS simulator for realistic HTTP interactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import requests

from custom_components.foxess_control.foxess.client import FoxESSApiError, FoxESSClient

if TYPE_CHECKING:
    from .conftest import SimulatorHandle


@pytest.fixture(autouse=True)
def _disable_throttle() -> None:
    """Disable request throttling and retry delays in tests."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
    FoxESSClient.RATE_LIMIT_MAX_DELAY = 0.0


def test_get_request(foxess_sim: SimulatorHandle) -> None:
    """GET request returns simulator data."""
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    result = client.get("/op/v0/device/detail", {"sn": "SIM0001"})
    assert "capacity" in result


def test_post_request(foxess_sim: SimulatorHandle) -> None:
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    result = client.post("/op/v0/device/list", {"currentPage": 1, "pageSize": 10})
    assert result["data"][0]["deviceSN"] == "SIM0001"


def test_real_query(foxess_sim: SimulatorHandle) -> None:
    """Real-time query returns power variables from model."""
    foxess_sim.set(soc=75, solar_kw=2.0, load_kw=0.5)
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    result = client.post(
        "/op/v0/device/real/query",
        {"sn": "SIM0001", "variables": ["SoC", "pvPower", "loadsPower"]},
    )
    datas = {d["variable"]: d["value"] for d in result[0]["datas"]}
    # SoC is integer (no fuzz), power values have ±2% jitter
    assert datas["SoC"] == 75.0
    assert datas["pvPower"] == pytest.approx(2.0, rel=0.05)
    assert datas["loadsPower"] == pytest.approx(0.5, rel=0.05)


def test_rate_limit_retry_succeeds(foxess_sim: SimulatorHandle) -> None:
    """A rate-limited request should retry and succeed."""
    foxess_sim.fault("rate_limit", count=1)
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    result = client.get("/op/v0/device/detail", {"sn": "SIM0001"})
    assert "capacity" in result


def test_transient_503_retries_and_succeeds(
    foxess_sim: SimulatorHandle,
) -> None:
    """A 503 response should be retried and succeed on next attempt."""
    foxess_sim.fault("api_down", count=1)
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    result = client.get("/op/v0/device/detail", {"sn": "SIM0001"})
    assert "capacity" in result


def test_transient_post_retries_and_succeeds(
    foxess_sim: SimulatorHandle,
) -> None:
    """A 503 POST response should be retried."""
    foxess_sim.fault("api_down", count=1)
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    result = client.post("/op/v0/device/list", {"currentPage": 1, "pageSize": 10})
    assert result["data"][0]["deviceSN"] == "SIM0001"


def test_transient_retries_exhaust_raises(
    foxess_sim: SimulatorHandle,
) -> None:
    """After TRANSIENT_RETRIES, the error is raised."""
    foxess_sim.fault("api_500")  # permanent
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    with pytest.raises(requests.HTTPError):
        client.get("/op/v0/device/detail", {"sn": "SIM0001"})
    foxess_sim.clear_fault()


def test_non_retryable_status_raises_immediately(
    foxess_sim: SimulatorHandle,
) -> None:
    """A 400 error should not be retried."""
    foxess_sim.fault("api_400")  # permanent
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    with pytest.raises(requests.HTTPError):
        client.get("/op/v0/device/detail", {"sn": "SIM0001"})
    foxess_sim.clear_fault()


def test_rate_limit_exhausts_retries(foxess_sim: SimulatorHandle) -> None:
    """After all retries are exhausted, the rate-limit error is raised."""
    foxess_sim.fault("rate_limit")  # permanent
    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    with pytest.raises(FoxESSApiError, match="40400"):
        client.post("/op/v0/device/list", {"currentPage": 1, "pageSize": 10})
    foxess_sim.clear_fault()
