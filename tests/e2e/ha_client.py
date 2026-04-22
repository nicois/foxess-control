"""Thin wrapper around the Home Assistant REST + WebSocket API for E2E tests."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import requests
import websocket

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

    def set_options(self, **overrides: Any) -> None:
        """Update integration options via the HA options flow and reload.

        Opens the options flow for the foxess_control config entry,
        merges *overrides* into the schema defaults, submits, and
        reloads the entry so the new values take effect.
        """
        entries = self._session.get(
            f"{self.base_url}/api/config/config_entries/entry", timeout=10
        ).json()
        foxess_entry = next(e for e in entries if e["domain"] == "foxess_control")
        entry_id = foxess_entry["entry_id"]

        r = self._session.post(
            f"{self.base_url}/api/config/config_entries/options/flow",
            json={"handler": entry_id},
            timeout=10,
        )
        r.raise_for_status()
        flow = r.json()
        flow_id = flow["flow_id"]
        submit_data: dict[str, Any] = {}
        for field in flow.get("data_schema", []):
            submit_data[field["name"]] = field.get("default")
        submit_data.update(overrides)

        r = self._session.post(
            f"{self.base_url}/api/config/config_entries/options/flow/{flow_id}",
            json=submit_data,
            timeout=10,
        )
        if not r.ok:
            raise RuntimeError(f"Options flow failed: {r.status_code} {r.text[:500]}")

        r = self._session.post(
            f"{self.base_url}/api/config/config_entries/entry/{entry_id}/reload",
            timeout=10,
        )
        if not r.ok:
            raise RuntimeError(f"Reload failed: {r.status_code} {r.text[:500]}")
        self._wait_for_integration_ready()

    def set_input_number(self, entity_id: str, value: float) -> None:
        """Set an input_number helper value."""
        self.call_service(
            "input_number",
            "set_value",
            {"entity_id": entity_id, "value": value},
        )

    def set_input_select(self, entity_id: str, option: str) -> None:
        """Set an input_select helper option."""
        self.call_service(
            "input_select",
            "select_option",
            {"entity_id": entity_id, "option": option},
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

    def wait_for_numeric_attribute(
        self,
        entity_id: str,
        attr: str,
        condition: str,
        value: float,
        timeout_s: float = 30,
        poll_interval: float = 2.0,
    ) -> float:
        """Poll until a numeric entity attribute satisfies *condition*.

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
        t0 = time.monotonic()
        deadline = t0 + timeout_s
        last = None
        while time.monotonic() < deadline:
            attrs = self.get_attributes(entity_id)
            raw = attrs.get(attr)
            if raw is not None:
                try:
                    last = float(raw)
                    if op(last, value):
                        _log.warning(
                            "wait_for_numeric_attribute %s.%s %s %s: %.1fs",
                            entity_id,
                            attr,
                            condition,
                            value,
                            time.monotonic() - t0,
                        )
                        return last
                except (ValueError, TypeError):
                    pass
            time.sleep(poll_interval)
        raise TimeoutError(
            f"{entity_id}.{attr} did not satisfy {condition} {value} "
            f"within {timeout_s}s (last: {last})"
        )

    def enable_entity(self, entity_id: str) -> None:
        """Enable a disabled-by-default entity via the WS entity registry API."""
        import websocket as _ws

        ws_url = self.base_url.replace("http://", "ws://") + "/api/websocket"
        ws = _ws.create_connection(ws_url, timeout=10)
        try:
            msg = json.loads(ws.recv())
            if msg["type"] != "auth_required":
                raise RuntimeError(f"Expected auth_required, got {msg['type']}")
            token = str(self._session.headers["Authorization"]).split(" ", 1)[1]
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            msg = json.loads(ws.recv())
            if msg["type"] != "auth_ok":
                raise RuntimeError(f"Auth failed: {msg}")
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": "config/entity_registry/update",
                        "entity_id": entity_id,
                        "disabled_by": None,
                    }
                )
            )
            result = json.loads(ws.recv())
            if not result.get("success"):
                raise RuntimeError(f"Failed to enable {entity_id}: {result}")
        finally:
            ws.close()

    def _wait_for_integration_ready(self, timeout_s: float = 15) -> None:
        """Poll until the integration's entities are available after reload."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                state = self.get_state("sensor.foxess_battery_soc")
                if state != "unavailable":
                    return
            except requests.RequestException:
                pass
            time.sleep(1)
        _log.warning("Integration not ready after %.0fs, continuing", timeout_s)

    def reload_integration(self, domain: str = "foxess_control") -> None:
        """Reload a config entry so entity registry changes take effect."""
        entries = self._session.get(
            f"{self.base_url}/api/config/config_entries/entry", timeout=10
        ).json()
        entry = next(e for e in entries if e["domain"] == domain)
        r = self._session.post(
            f"{self.base_url}/api/config/config_entries/entry/"
            f"{entry['entry_id']}/reload",
            timeout=10,
        )
        if not r.ok:
            raise RuntimeError(f"Reload failed: {r.status_code} {r.text[:200]}")
        self._wait_for_integration_ready()

    def is_ready(self) -> bool:
        """Check if HA is responding.

        Catches ``ConnectionError`` (container not listening yet) and
        ``OSError`` (socket torn down mid-connect — "Bad file descriptor")
        so that ``wait_ready`` keeps retrying instead of aborting.
        """
        try:
            r = self._session.get(f"{self.base_url}/api/", timeout=5)
            return r.status_code == 200
        except (requests.ConnectionError, OSError):
            return False

    def wait_ready(self, timeout_s: float = 120) -> None:
        """Wait for HA to be ready."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.is_ready():
                return
            time.sleep(2)
        raise TimeoutError(f"HA did not become ready within {timeout_s}s")


class HAEventStream:
    """WebSocket subscription for instant HA state change notifications.

    Connects to ``/api/websocket``, authenticates, and subscribes to
    ``state_changed`` events.  Provides blocking ``wait_for_state``
    and ``wait_for_attribute`` that resolve as soon as HA pushes the
    matching event — no polling needed.
    """

    def __init__(self, base_url: str, token: str) -> None:
        ws_url = base_url.replace("http://", "ws://") + "/api/websocket"
        self._ws = websocket.create_connection(ws_url, timeout=10)
        self._token = token
        self._msg_id = 1
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

        # Authenticate
        msg = json.loads(self._ws.recv())
        if msg["type"] != "auth_required":
            raise RuntimeError(f"Expected auth_required, got {msg['type']}")
        self._ws.send(json.dumps({"type": "auth", "access_token": token}))
        msg = json.loads(self._ws.recv())
        if msg["type"] != "auth_ok":
            raise RuntimeError(f"Auth failed: {msg}")

        # Subscribe to state_changed
        self._ws.send(
            json.dumps(
                {
                    "id": self._msg_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )
        )
        self._msg_id += 1
        result = json.loads(self._ws.recv())
        if not result.get("success"):
            raise RuntimeError(f"Subscribe failed: {result}")

        # Background thread to accumulate events
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def _recv_loop(self) -> None:
        self._ws.settimeout(1.0)
        while not self._stop.is_set():
            try:
                raw = self._ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "event":
                    with self._lock:
                        self._events.append(msg["event"]["data"])
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketException, json.JSONDecodeError) as exc:
                _log.debug("WS recv loop ending: %s", exc)
                break

    def wait_for_state(
        self,
        entity_id: str,
        expected: str,
        timeout_s: float = 30,
        fatal_states: frozenset[str] | None = None,
        rest_client: HAClient | None = None,
    ) -> str:
        """Block until entity reaches expected state via WS event.

        Drains stale events first, then checks the current REST state
        as a baseline.  This eliminates the race between triggering a
        state change and starting to listen — if the state already
        matches, returns immediately.
        """
        t0 = time.monotonic()
        deadline = t0 + timeout_s

        # Drain stale events so we only see fresh ones
        self.drain()

        # Check current state via REST — the change may have already
        # happened before we started listening.
        if rest_client is not None:
            try:
                current = rest_client.get_state(entity_id)
                if current == expected:
                    _log.warning(
                        "ws_wait_for_state %s=%s: %.1fs (already)",
                        entity_id,
                        expected,
                        time.monotonic() - t0,
                    )
                    return current
                if fatal_states and current in fatal_states:
                    raise RuntimeError(
                        f"{entity_id} already in fatal state "
                        f"'{current}' while waiting for '{expected}'"
                    )
            except requests.RequestException as exc:
                _log.debug("REST baseline check unavailable: %s", exc)

        while time.monotonic() < deadline:
            with self._lock:
                while self._events:
                    ev = self._events.pop(0)
                    if ev.get("entity_id") != entity_id:
                        continue
                    new_state = ev.get("new_state", {}).get("state")
                    if new_state == expected:
                        _log.warning(
                            "ws_wait_for_state %s=%s: %.1fs",
                            entity_id,
                            expected,
                            time.monotonic() - t0,
                        )
                        return str(new_state)
                    if fatal_states and new_state in fatal_states:
                        raise RuntimeError(
                            f"{entity_id} reached fatal state "
                            f"'{new_state}' while waiting for "
                            f"'{expected}' (after "
                            f"{time.monotonic() - t0:.1f}s)"
                        )
            time.sleep(0.1)
        raise TimeoutError(
            f"{entity_id} did not reach '{expected}' within "
            f"{timeout_s}s (via WebSocket)"
        )

    def wait_for_attribute(
        self,
        entity_id: str,
        attr: str,
        expected: str,
        timeout_s: float = 30,
    ) -> str:
        """Block until entity attribute matches via WS event."""
        t0 = time.monotonic()
        deadline = t0 + timeout_s
        last = None
        while time.monotonic() < deadline:
            with self._lock:
                while self._events:
                    ev = self._events.pop(0)
                    if ev.get("entity_id") != entity_id:
                        continue
                    attrs = ev.get("new_state", {}).get("attributes", {})
                    last = attrs.get(attr)
                    if last == expected:
                        _log.warning(
                            "ws_wait_for_attribute %s.%s=%s: %.1fs",
                            entity_id,
                            attr,
                            expected,
                            time.monotonic() - t0,
                        )
                        return str(last)
            time.sleep(0.1)
        raise TimeoutError(
            f"{entity_id}.{attr} did not reach '{expected}' within "
            f"{timeout_s}s (last: '{last}', via WebSocket)"
        )

    def drain(self) -> None:
        """Clear all accumulated events."""
        with self._lock:
            self._events.clear()

    def close(self) -> None:
        self._stop.set()
        self._ws.close()
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            _log.warning("HAEventStream recv thread did not stop within 3s")
