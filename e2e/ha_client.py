"""Thin wrapper around the Home Assistant REST API for E2E tests."""

from __future__ import annotations

import time
from typing import Any

import requests


class HAClient:
    """Synchronous client for the HA REST API."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    def get_state(self, entity_id: str) -> str:
        """Return the state value of an entity."""
        r = self._session.get(f"{self.base_url}/api/states/{entity_id}", timeout=10)
        r.raise_for_status()
        return str(r.json()["state"])

    def get_attributes(self, entity_id: str) -> dict[str, Any]:
        """Return the attributes of an entity."""
        r = self._session.get(f"{self.base_url}/api/states/{entity_id}", timeout=10)
        r.raise_for_status()
        return dict(r.json()["attributes"])

    def call_service(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> None:
        """Call a HA service."""
        r = self._session.post(
            f"{self.base_url}/api/services/{domain}/{service}",
            json=data or {},
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(
                f"Service call {domain}/{service} failed: "
                f"{r.status_code} {r.text[:200]}"
            )

    def wait_for_state(
        self,
        entity_id: str,
        expected: str,
        timeout_s: float = 30,
        poll_interval: float = 1.0,
    ) -> str:
        """Poll until entity reaches the expected state or timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = self.get_state(entity_id)
            if state == expected:
                return state
            time.sleep(poll_interval)
        raise TimeoutError(
            f"{entity_id} did not reach '{expected}' within {timeout_s}s "
            f"(last: '{self.get_state(entity_id)}')"
        )

    def wait_for_numeric_state(
        self,
        entity_id: str,
        condition: str,
        value: float,
        timeout_s: float = 60,
        poll_interval: float = 2.0,
    ) -> float:
        """Poll until entity's numeric state satisfies the condition.

        condition: "lt", "gt", "le", "ge", "eq", "ne"
        """
        import operator

        ops = {
            "lt": operator.lt,
            "gt": operator.gt,
            "le": operator.le,
            "ge": operator.ge,
            "eq": operator.eq,
            "ne": operator.ne,
        }
        op = ops[condition]
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            raw = self.get_state(entity_id)
            try:
                last = float(raw)
                if op(last, value):
                    return last
            except (ValueError, TypeError):
                pass
            time.sleep(poll_interval)
        raise TimeoutError(
            f"{entity_id} did not satisfy {condition} {value} "
            f"within {timeout_s}s (last: {last})"
        )

    def wait_for_attribute(
        self,
        entity_id: str,
        attr: str,
        expected: str,
        timeout_s: float = 30,
        poll_interval: float = 2.0,
    ) -> str:
        """Poll until an entity attribute reaches the expected value."""
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            attrs = self.get_attributes(entity_id)
            last = attrs.get(attr)
            if last == expected:
                return str(last)
            time.sleep(poll_interval)
        raise TimeoutError(
            f"{entity_id}.{attr} did not reach '{expected}' "
            f"within {timeout_s}s (last: '{last}')"
        )

    def is_ready(self) -> bool:
        """Check if HA is responding."""
        try:
            r = self._session.get(f"{self.base_url}/api/", timeout=5)
            return r.status_code == 200
        except requests.ConnectionError:
            return False

    def wait_ready(self, timeout_s: float = 120) -> None:
        """Wait for HA to be ready."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.is_ready():
                # Give integration time to load
                time.sleep(5)
                return
            time.sleep(2)
        raise TimeoutError(f"HA did not become ready within {timeout_s}s")
