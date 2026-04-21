"""Shared test fixtures."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

import aiohttp.web
import pytest
import requests

if TYPE_CHECKING:
    from unittest.mock import MagicMock

from simulator.server import create_app

if TYPE_CHECKING:
    from collections.abc import Generator


class SimulatorHandle:
    """Wraps the simulator server with a synchronous backchannel client."""

    def __init__(self, base_url: str) -> None:
        self.url = base_url

    def set(self, **kwargs: object) -> None:
        requests.post(f"{self.url}/sim/set", json=kwargs, timeout=5)

    def state(self) -> Any:
        return requests.get(f"{self.url}/sim/state", timeout=5).json()

    def fault(self, fault_type: str, count: int = 0) -> None:
        requests.post(
            f"{self.url}/sim/fault",
            json={"type": fault_type, "count": count},
            timeout=5,
        )

    def clear_fault(self) -> None:
        requests.post(f"{self.url}/sim/clear_fault", json={}, timeout=5)

    def reset(self) -> None:
        requests.post(f"{self.url}/sim/reset", json={}, timeout=5)

    def fast_forward(self, seconds: int, step: int = 5) -> Any:
        return requests.post(
            f"{self.url}/sim/fast_forward",
            json={"seconds": seconds, "step": step},
            timeout=30,
        ).json()

    def tick(self, seconds: int) -> Any:
        return requests.post(
            f"{self.url}/sim/tick",
            json={"seconds": seconds},
            timeout=5,
        ).json()


def _run_server(
    app: aiohttp.web.Application, port_holder: list[int], ready: threading.Event
) -> None:
    """Run the aiohttp server in a background thread."""
    loop = asyncio.new_event_loop()

    async def _start() -> None:
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "localhost", 0)
        await site.start()
        sock = site._server.sockets[0]  # type: ignore[union-attr]
        port_holder.append(sock.getsockname()[1])
        ready.set()
        # Keep running until the loop is stopped
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()

    task = loop.create_task(_start())
    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


def _get_handler(hass: MagicMock, service_name: str) -> Any:
    """Look up a registered service handler by name.

    Searches ``hass.services.async_register.call_args_list`` for the
    registration whose second positional arg matches *service_name*.
    """
    for call in hass.services.async_register.call_args_list:
        if call.args[1] == service_name:
            return call.args[2]
    registered = [c.args[1] for c in hass.services.async_register.call_args_list]
    raise KeyError(f"Service {service_name!r} not found; registered: {registered}")


@pytest.fixture(autouse=True)
def _mock_issue_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent unit tests from hitting the real HA issue registry."""
    from unittest.mock import MagicMock

    noop = MagicMock()
    for mod in (
        "smart_battery.listeners",
        "custom_components.foxess_control.smart_battery.listeners",
    ):
        monkeypatch.setattr(f"{mod}._create_session_issue", noop, raising=False)
        monkeypatch.setattr(f"{mod}._clear_session_issue", noop, raising=False)


@pytest.fixture
def foxess_sim() -> Generator[SimulatorHandle, None, None]:
    """Start a FoxESS simulator in a background thread.

    Returns a SimulatorHandle with synchronous backchannel methods.
    The server is started fresh for each test and reset afterward.
    """
    app = create_app()
    port_holder: list[int] = []
    ready = threading.Event()

    thread = threading.Thread(
        target=_run_server, args=(app, port_holder, ready), daemon=True
    )
    thread.start()
    ready.wait(timeout=5)

    handle = SimulatorHandle(f"http://localhost:{port_holder[0]}")
    yield handle

    handle.reset()
    # Thread is daemon — it will be cleaned up on process exit
