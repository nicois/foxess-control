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


@pytest.fixture
def fake_adapter() -> Any:
    """Brand-agnostic InverterAdapter stub that records every Protocol call.

    Import :class:`smart_battery.testing.FakeAdapter` directly for tests
    that want to customise ``max_power_w`` or ``export_limit_w``; use
    this fixture for the default 10 kW / no-export-limit case.

    Intended for tests of ``smart_battery/`` code that want to prove
    the code is brand-agnostic. A test that passes with FakeAdapter
    will pass with any correct :class:`InverterAdapter` implementation —
    it cannot silently depend on FoxESS response shapes.
    """
    from smart_battery.testing import FakeAdapter

    return FakeAdapter()


# === PLAYWRIGHT ISOLATION OVERRIDE START ===
# Narrow the pytest-playwright fixture chain from session- to
# function-scope so each unit test that uses ``page`` gets a fresh
# ``sync_playwright`` context AND releases it on teardown.
#
# Why this exists
# ---------------
# ``pytest-playwright``'s ``playwright`` fixture is
# ``scope="session"`` and calls
# ``playwright.sync_api.sync_playwright().start()`` lazily on first
# use.  Playwright's sync API is a greenlet shim over an asyncio
# event loop running on the main thread — that loop remains
# ``running`` from pytest-playwright's perspective until ``.stop()``
# is called at end-of-session.  While it runs, every
# ``@pytest.mark.asyncio`` test scheduled after a ``page``-using
# test on the same xdist worker fails with either
# ``RuntimeError: Runner.run() cannot be called from a running
# event loop`` (entry path) or ``RuntimeError: Cannot run the event
# loop while another loop is running`` (teardown path during
# ``loop.shutdown_asyncgens``).
#
# The flake is intermittent because ``pytest-randomly`` only
# occasionally schedules ``page``-using tests (currently
# ``tests/test_card_entity_resolution.py``) before asyncio tests
# on the same worker.
#
# Narrowing to function scope costs a single Chromium cold-start
# per ``page``-using test (~0.5s each).  With only five unit
# tests using ``page`` that is negligible.  The E2E suite
# (``tests/e2e/``) keeps its own ``browser_context`` session
# fixture — it isn't a leak there because E2E tests are the
# only tests on that worker and the leak manifests only when
# asyncio tests run after a ``page`` test.
#
# The override is kept as a marker-delimited block so
# ``tests/test_playwright_fixture_isolation.py`` can lift it into
# an isolated sandbox subprocess without dragging in the rest of
# ``tests/conftest.py`` (which imports simulator / HA machinery
# that would fail in the sandbox).
#
# Refs C-031 (no flaky tests — root cause, not skip/xfail).
# Refs C-040 (brand-agnostic tests unaffected — fake_adapter above).

if TYPE_CHECKING:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        BrowserType,
        Page,
        Playwright,
    )


@pytest.fixture
def playwright() -> Generator[Playwright, None, None]:
    """Function-scoped override of pytest-playwright's session-scoped
    ``playwright`` fixture.

    Starting and stopping ``sync_playwright`` per test releases the
    greenlet-backed asyncio loop before any subsequent asyncio test
    runs on the same xdist worker.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        pw.stop()


@pytest.fixture
def browser_type(playwright: Playwright, browser_name: str) -> BrowserType:
    """Function-scoped override — see ``playwright`` fixture above.

    Must be overridden alongside ``playwright`` because pytest
    forbids a session-scoped fixture from depending on a
    function-scoped one; the two scopes must agree down the chain.
    """
    return getattr(playwright, browser_name)


@pytest.fixture
def browser(browser_type: BrowserType) -> Generator[Browser, None, None]:
    """Function-scoped override — see ``playwright`` fixture above.

    Each test gets a fresh Chromium process; the ``sync_playwright``
    teardown at the end of the test closes it and releases the loop.
    """
    browser = browser_type.launch(headless=True)
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def context(browser: Browser) -> Generator[BrowserContext, None, None]:
    """Function-scoped override — see ``playwright`` fixture above.

    pytest-playwright's default ``context`` fixture is already
    function-scoped, but it depends on the session-scoped
    ``new_context`` callback which pulls in artifact-recording
    infrastructure we do not need.  A direct ``browser.new_context()``
    is simpler and keeps the chain fully function-scoped.
    """
    ctx = browser.new_context()
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def page(context: BrowserContext) -> Generator[Page, None, None]:
    """Function-scoped override — see ``playwright`` fixture above.

    Returns a fresh tab; the surrounding context/browser/playwright
    fixtures close everything on teardown.
    """
    pg = context.new_page()
    try:
        yield pg
    finally:
        pg.close()


# === PLAYWRIGHT ISOLATION OVERRIDE END ===
