"""Tests for E2E HAClient health-check resilience.

Verifies that ``wait_ready`` and ``is_ready`` handle transient OS-level
errors (e.g. ``OSError: [Errno 9] Bad file descriptor``) by retrying
instead of propagating the exception.  This reproduces a CI flake where
the HA container's health-check socket was torn down during startup,
causing ``OSError`` to escape the retry loop and fail fixture setup.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from tests.e2e.ha_client import HAClient


class TestIsReady:
    """HAClient.is_ready must tolerate transient OS errors."""

    def test_returns_false_on_os_error(self) -> None:
        """OSError (bad file descriptor) should return False, not propagate."""
        client = HAClient("http://localhost:9999", "fake-token")
        with patch.object(
            client._session, "get", side_effect=OSError(9, "Bad file descriptor")
        ):
            result = client.is_ready()

        assert result is False

    def test_returns_false_on_connection_error(self) -> None:
        """ConnectionError should still return False (existing behaviour)."""
        client = HAClient("http://localhost:9999", "fake-token")
        with patch.object(
            client._session, "get", side_effect=requests.ConnectionError("refused")
        ):
            result = client.is_ready()

        assert result is False

    def test_returns_true_on_200(self) -> None:
        """200 response means ready."""
        client = HAClient("http://localhost:9999", "fake-token")
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch.object(client._session, "get", return_value=mock_response):
            result = client.is_ready()

        assert result is True

    def test_returns_false_on_non_200(self) -> None:
        """Non-200 response means not ready."""
        client = HAClient("http://localhost:9999", "fake-token")
        mock_response = MagicMock()
        mock_response.status_code = 503
        with patch.object(client._session, "get", return_value=mock_response):
            result = client.is_ready()

        assert result is False


class TestWaitReady:
    """HAClient.wait_ready must survive transient errors and eventually succeed."""

    def test_os_error_then_success(self) -> None:
        """OSError on first attempts, then 200 — wait_ready must not raise."""
        client = HAClient("http://localhost:9999", "fake-token")
        ok_response = MagicMock()
        ok_response.status_code = 200

        with patch.object(
            client._session,
            "get",
            side_effect=[
                OSError(9, "Bad file descriptor"),
                OSError(9, "Bad file descriptor"),
                ok_response,
            ],
        ):
            # Should NOT raise — OSError is retried
            client.wait_ready(timeout_s=30)

    def test_connection_error_then_success(self) -> None:
        """ConnectionError followed by 200 — existing behaviour preserved."""
        client = HAClient("http://localhost:9999", "fake-token")
        ok_response = MagicMock()
        ok_response.status_code = 200

        with patch.object(
            client._session,
            "get",
            side_effect=[
                requests.ConnectionError("refused"),
                ok_response,
            ],
        ):
            client.wait_ready(timeout_s=30)

    def test_permanent_failure_raises_timeout(self) -> None:
        """If the server never comes up, wait_ready raises TimeoutError."""
        client = HAClient("http://localhost:9999", "fake-token")
        with (
            patch.object(
                client._session,
                "get",
                side_effect=OSError(9, "Bad file descriptor"),
            ),
            pytest.raises(TimeoutError, match="HA did not become ready"),
        ):
            client.wait_ready(timeout_s=0.1)
