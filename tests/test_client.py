"""Tests for FoxESS API client authentication and request handling."""

import hashlib

import pytest
import responses

from custom_components.foxess_control.foxess.client import FoxESSApiError, FoxESSClient


@pytest.fixture(autouse=True)
def _disable_throttle() -> None:
    """Disable request throttling in tests."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


@responses.activate
def test_get_request_signs_correctly() -> None:
    """Verify the signature is md5(path + CRLF + token + CRLF + ts)."""
    client = FoxESSClient("test-api-key")

    responses.add(
        responses.GET,
        "https://www.foxesscloud.com/op/v0/device/detail",
        json={"errno": 0, "result": {"deviceSN": "ABC123"}},
    )

    result = client.get("/op/v0/device/detail", {"sn": "ABC123"})

    assert result == {"deviceSN": "ABC123"}
    req = responses.calls[0].request
    assert req.headers["token"] == "test-api-key"
    ts = req.headers["timestamp"]
    expected_sig = hashlib.md5(
        rf"/op/v0/device/detail\r\ntest-api-key\r\n{ts}".encode()
    ).hexdigest()
    assert req.headers["signature"] == expected_sig


@responses.activate
def test_post_request() -> None:
    client = FoxESSClient("test-api-key")

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/list",
        json={
            "errno": 0,
            "result": {"data": [{"deviceSN": "ABC123"}], "total": 1},
        },
    )

    result = client.post("/op/v0/device/list", {"currentPage": 1, "pageSize": 10})
    assert result["data"][0]["deviceSN"] == "ABC123"


@responses.activate
def test_api_error_raises() -> None:
    client = FoxESSClient("test-api-key")

    responses.add(
        responses.GET,
        "https://www.foxesscloud.com/op/v0/device/detail",
        json={"errno": 40257, "msg": "invalid request body parameters"},
    )

    with pytest.raises(FoxESSApiError, match="40257"):
        client.get("/op/v0/device/detail", {"sn": "INVALID"})


@responses.activate
def test_rate_limit_retry_succeeds() -> None:
    """A rate-limited request should retry and succeed."""
    client = FoxESSClient("test-api-key")

    # First call: rate limited
    responses.add(
        responses.GET,
        "https://www.foxesscloud.com/op/v0/device/detail",
        json={"errno": 40400, "msg": "request frequency too high"},
    )
    # Second call: success
    responses.add(
        responses.GET,
        "https://www.foxesscloud.com/op/v0/device/detail",
        json={"errno": 0, "result": {"deviceSN": "ABC123"}},
    )

    result = client.get("/op/v0/device/detail", {"sn": "ABC123"})
    assert result == {"deviceSN": "ABC123"}
    assert len(responses.calls) == 2


@responses.activate
def test_rate_limit_exhausts_retries() -> None:
    """After all retries are exhausted, the rate-limit error is raised."""
    client = FoxESSClient("test-api-key")

    for _ in range(FoxESSClient.RATE_LIMIT_RETRIES + 1):
        responses.add(
            responses.POST,
            "https://www.foxesscloud.com/op/v0/device/list",
            json={"errno": 40400, "msg": "request frequency too high"},
        )

    with pytest.raises(FoxESSApiError, match="40400"):
        client.post("/op/v0/device/list", {"currentPage": 1, "pageSize": 10})

    assert len(responses.calls) == FoxESSClient.RATE_LIMIT_RETRIES + 1
