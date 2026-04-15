"""Thin wrapper around the Home Assistant REST API for E2E tests."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

_log = logging.getLogger("e2e.timing")

# States from which an active session is unreachable.
# "idle" is excluded: there's a brief window after call_service where the
# sensor hasn't updated yet, so the first poll sees "idle" legitimately.
FATAL_FOR_ACTIVE = frozenset({"error"})


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
        t0 = time.monotonic()
        r = self._session.post(
            f"{self.base_url}/api/services/{domain}/{service}",
            json=data or {},
            timeout=30,
        )
        _log.warning(
            "call_service %s/%s: %.1fs (%d)",
            domain,
            service,
            time.monotonic() - t0,
            r.status_code,
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
        fatal_states: frozenset[str] | None = None,
    ) -> str:
        """Poll until entity reaches the expected state or timeout.

        If *fatal_states* is provided and the entity enters one of
        those states, raise immediately instead of polling until
        timeout — the expected state is unreachable.
        """
        t0 = time.monotonic()
        deadline = t0 + timeout_s
        while time.monotonic() < deadline:
            state = self.get_state(entity_id)
            if state == expected:
                _log.warning(
                    "wait_for_state %s=%s: %.1fs",
                    entity_id,
                    expected,
                    time.monotonic() - t0,
                )
                return state
            if fatal_states and state in fatal_states:
                raise RuntimeError(
                    f"{entity_id} reached fatal state '{state}' "
                    f"while waiting for '{expected}' "
                    f"(after {time.monotonic() - t0:.1f}s)"
                )
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
        t0 = time.monotonic()
        deadline = t0 + timeout_s
        last = None
        while time.monotonic() < deadline:
            attrs = self.get_attributes(entity_id)
            last = attrs.get(attr)
            if last == expected:
                _log.warning(
                    "wait_for_attribute %s.%s=%s: %.1fs",
                    entity_id,
                    attr,
                    expected,
                    time.monotonic() - t0,
                )
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
