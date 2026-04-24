"""E2E test fixtures: simulator + HA container + Playwright browser.

Scoping model (xdist-compatible):
- connection_mode: session — "cloud" or "entity"
- _worker_ports: session — unique sim + HA ports per worker
- browser_context: session — reused browser (pages reconnect per test)
- foxess_sim: function — fresh simulator per test (cloud only)
- ha_e2e: function — fresh container per test (full isolation)
- event_stream: function — fresh WS subscription per container
- page: function — fresh tab per test

Every function-scoped fixture uses yield for teardown. Each test
gets a clean simulator, HA instance, and event stream with zero
state from prior tests.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import requests

from .ha_client import HAClient, HAEventStream

_log = logging.getLogger("e2e.timing")


_test_durations: dict[str, float] = {}


def pytest_configure(config: Any) -> None:
    """Ensure e2e.timing messages appear in pytest output."""
    logging.getLogger("e2e.timing").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("e2e.timing").addHandler(handler)


# Invalid connection_mode + data_source combinations.
# Deselecting at collection time avoids fixture setup (including the
# expensive page/container fixtures) for combos that would be skipped
# anyway.  Without this, the page fixture's wait_for_function can
# time out under CI load before the data_source fixture's pytest.skip()
# gets a chance to execute — causing spurious ERRORs.
_INVALID_COMBOS = {"entity-api", "entity-ws", "cloud-entity"}


def pytest_collection_modifyitems(
    config: Any,  # noqa: ARG001
    items: list[Any],
) -> None:
    """Deselect tests with invalid connection_mode + data_source combos."""
    keep: list[Any] = []
    for item in items:
        # Node IDs end with e.g. [entity-ws] or [cloud-api].
        # Only filter items that have a two-part parametrisation matching
        # the connection_mode-data_source pattern.
        node_id = item.nodeid
        bracket = node_id.rsplit("[", 1)[-1].rstrip("]") if "[" in node_id else ""
        if bracket in _INVALID_COMBOS:
            continue
        keep.append(item)
    items[:] = keep


def pytest_runtest_logreport(report: Any) -> None:
    """Collect test durations (works on xdist controller)."""
    if report.when == "call":
        _test_durations[report.nodeid] = report.duration
    elif report.when == "setup" and report.duration > 1.0:
        _test_durations[f"{report.nodeid} [setup]"] = report.duration


def pytest_terminal_summary(terminalreporter: Any, config: Any) -> None:
    if not _test_durations:
        return
    terminalreporter.section("E2E timing breakdown")
    for name, dur in sorted(_test_durations.items(), key=lambda x: -x[1]):
        short = name.split("::")[-1]
        terminalreporter.write_line(f"  {dur:6.1f}s  {short}")
    total = sum(d for k, d in _test_durations.items() if "[setup]" not in k)
    terminalreporter.write_line(f"  {'─' * 40}")
    terminalreporter.write_line(
        f"  {total:6.1f}s  total test time (wall < this due to parallelism)"
    )


if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from playwright.sync_api import BrowserContext, Page, Playwright


# ---------------------------------------------------------------------------
# Auth token (matches pre-seeded .storage/auth)
# ---------------------------------------------------------------------------


def _generate_ha_token() -> str:
    import datetime as _dt

    import jwt

    return jwt.encode(
        {
            "iss": "e2e-refresh-001",
            "iat": _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
            "exp": _dt.datetime(2036, 1, 1, tzinfo=_dt.UTC),
        },
        "e2e-jwt-key-not-used-for-long-lived",
        algorithm="HS256",
    )


E2E_TOKEN = _generate_ha_token()
E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent.parent
HA_CONFIG_SEED = E2E_DIR / "ha_config"
CONTAINER_IMAGE = "ha-foxess-e2e"


# ---------------------------------------------------------------------------
# Simulator handle
# ---------------------------------------------------------------------------


class SimulatorHandle:
    """Synchronous backchannel client for the simulator."""

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
            timeout=60,
        ).json()

    def tick(self, seconds: int) -> Any:
        return requests.post(
            f"{self.url}/sim/tick",
            json={"seconds": seconds},
            timeout=5,
        ).json()

    def ws_unit(self, unit: str) -> None:
        requests.post(f"{self.url}/sim/ws_unit", json={"unit": unit}, timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("", 0))
        port: int = s.getsockname()[1]
        return port


def _build_container_once() -> None:
    """Build the HA container image once, serialised across xdist workers."""
    import filelock

    lock = filelock.FileLock(str(REPO_ROOT / ".e2e-build.lock"), timeout=300)
    with lock:
        subprocess.run(
            ["podman", "build", "-t", CONTAINER_IMAGE, str(E2E_DIR)],
            check=True,
            capture_output=True,
        )


def _worker_id() -> str:
    """Return the xdist worker ID, or 'main' for serial runs."""
    return os.environ.get("PYTEST_XDIST_WORKER", "main")


def _container_name() -> str:
    """Deterministic container name for this worker."""
    return f"ha-e2e-{_worker_id()}"


def _stop_container(name: str) -> None:
    """Stop and remove a container by name (idempotent)."""
    with contextlib.suppress(Exception):
        subprocess.run(
            ["podman", "stop", "-t", "5", name],
            capture_output=True,
            timeout=15,
        )
    with contextlib.suppress(Exception):
        subprocess.run(
            ["podman", "rm", "-f", name],
            capture_output=True,
            timeout=15,
        )


def _kill_process(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a subprocess, escalating to kill."""
    with contextlib.suppress(Exception):
        proc.terminate()
        proc.wait(timeout=5)
    if proc.poll() is None:
        with contextlib.suppress(Exception):
            proc.kill()


# ---------------------------------------------------------------------------
# Session-scoped fixtures (one per xdist worker)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", params=["cloud", "entity"])
def connection_mode(request: pytest.FixtureRequest) -> str:
    """Control whether tests run against cloud API or entity mode."""
    return str(request.param)


@pytest.fixture(scope="session")
def _worker_ports() -> dict[str, int]:
    """Allocate unique ports for this xdist worker."""
    return {"sim": _find_free_port(), "ha": _find_free_port()}


@pytest.fixture(scope="session")
def _container_built() -> None:
    """Ensure the container image is built (once per worker, serialised)."""
    _build_container_once()


@pytest.fixture
def foxess_sim(
    _worker_ports: dict[str, int],
    connection_mode: str,
) -> Generator[SimulatorHandle | None, None, None]:
    """Start a fresh simulator per test (cloud mode only)."""
    if connection_mode != "cloud":
        yield None
        return
    port = _worker_ports["sim"]
    proc = subprocess.Popen(
        ["python", "-m", "simulator", "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"http://localhost:{port}/sim/state", timeout=1)
            if r.status_code == 200:
                break
        except requests.ConnectionError:
            pass
        time.sleep(0.2)
    else:
        _kill_process(proc)
        raise RuntimeError("Simulator did not start")

    yield SimulatorHandle(f"http://localhost:{port}")

    _kill_process(proc)


@pytest.fixture(scope="session")
def ha_port(_worker_ports: dict[str, int]) -> int:
    """The HA port for this worker."""
    return _worker_ports["ha"]


@pytest.fixture(scope="session")
def browser_context(
    playwright: Playwright,
) -> Generator[BrowserContext, None, None]:
    """Session-scoped browser — pages reconnect per test."""
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    yield context
    context.close()
    browser.close()


# ---------------------------------------------------------------------------
# Function-scoped fixtures (fresh per test)
# ---------------------------------------------------------------------------


@pytest.fixture
def ha_e2e(
    foxess_sim: SimulatorHandle | None,
    ha_port: int,
    connection_mode: str,
    _container_built: None,  # noqa: ARG001
) -> Generator[HAClient, None, None]:
    """Start a FRESH HA container for this test.

    Eliminates all state leaks: each test gets a clean HA instance
    with no residual sessions, WS connections, coordinator state,
    or cached data from prior tests.
    """
    wid = _worker_id()
    t0 = time.monotonic()
    name = _container_name()
    _stop_container(name)

    tmpdir = tempfile.mkdtemp(prefix="ha-e2e-")
    shutil.copytree(str(HA_CONFIG_SEED), tmpdir, dirs_exist_ok=True)

    if connection_mode == "entity":
        entity_config = HA_CONFIG_SEED / "configuration.entity.yaml"
        entity_entries = HA_CONFIG_SEED / ".storage" / "core.config_entries.entity"
        shutil.copy2(str(entity_config), os.path.join(tmpdir, "configuration.yaml"))
        shutil.copy2(
            str(entity_entries),
            os.path.join(tmpdir, ".storage", "core.config_entries"),
        )

    os.chmod(tmpdir, 0o777)
    for root, dirs, _files in os.walk(tmpdir):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o777)

    sim_port = foxess_sim.url.rsplit(":", 1)[1] if foxess_sim else "0"
    # stdout/stderr → DEVNULL to avoid pipe-buffer deadlock.
    # The default Linux pipe buffer is 64 KiB.  Under CI load (12 xdist
    # workers), HA's startup logs can fill the buffer before wait_ready()
    # succeeds, blocking the container process and preventing HA from
    # ever listening on its HTTP port.  Container logs are captured via
    # "podman logs" in teardown and error paths, so PIPE is unnecessary.
    proc = subprocess.Popen(
        [
            "podman",
            "run",
            "--rm",
            "--name",
            name,
            "-p",
            f"{ha_port}:8123",
            "--add-host=host.containers.internal:host-gateway",
            "-v",
            f"{REPO_ROOT}/custom_components/foxess_control"
            f":/config/custom_components/foxess_control:ro,z",
            "-v",
            f"{tmpdir}:/config:Z",
            "-e",
            f"FOXESS_SIMULATOR_URL=http://host.containers.internal:{sim_port}",
            CONTAINER_IMAGE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        ha = HAClient(f"http://localhost:{ha_port}", E2E_TOKEN)
        ha.wait_ready(timeout_s=120)

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                ha.get_state("sensor.foxess_battery_soc")
                break
            except requests.RequestException:
                time.sleep(2)
        else:
            # Capture container logs for diagnosis (stdout is DEVNULL).
            try:
                logs = subprocess.run(
                    ["podman", "logs", "--tail", "200", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if logs.stdout:
                    print(logs.stdout[-3000:])
            except (subprocess.SubprocessError, OSError):
                pass
            raise TimeoutError("Integration entities not created within 120s")
        _log.warning(
            "[%s] container ready: %.1fs",
            wid,
            time.monotonic() - t0,
        )
    except BaseException:
        _stop_container(name)
        _kill_process(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    yield ha

    try:
        logs = subprocess.run(
            ["podman", "logs", "--tail", "200", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if logs.stdout:
            print(f"\n=== HA container logs (tail) ===\n{logs.stdout[-5000:]}")
    except (subprocess.SubprocessError, OSError) as exc:
        _log.debug("Failed to capture container logs: %s", exc)
    _stop_container(name)
    _kill_process(proc)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def event_stream(
    ha_e2e: HAClient,  # noqa: ARG001 — ensure HA is running
    ha_port: int,
) -> Generator[HAEventStream, None, None]:
    """Function-scoped WebSocket event stream — fresh per container."""
    stream = HAEventStream(f"http://localhost:{ha_port}", E2E_TOKEN)
    try:
        yield stream
    finally:
        stream.close()


# ---------------------------------------------------------------------------
# Page fixture helpers
# ---------------------------------------------------------------------------


# Matches the error messages Playwright surfaces when a navigation
# destroys the JS execution context mid-poll.  Checked as substrings.
_CONTEXT_DESTROYED_SIGNALS = (
    "Execution context was destroyed",
    "navigating",
)


_PANEL_READY_JS = """() => {
    const main = document.querySelector('home-assistant');
    if (!main || !main.shadowRoot) return false;
    const ham = main.shadowRoot.querySelector('home-assistant-main');
    if (!ham || !ham.shadowRoot) return false;
    const panel = ham.shadowRoot.querySelector('ha-panel-lovelace');
    return !!panel;
}"""


def _wait_for_lovelace_panel(page: Any, timeout_ms: int = 30000) -> None:
    """Wait for the Lovelace panel to render in the shadow DOM.

    Retries on Playwright "Execution context was destroyed" errors,
    which occur when HA's frontend triggers a navigation (WS reconnect,
    dashboard router refresh, sidebar load) during the initial page
    load.  Without retry logic, a single navigation burst causes the
    fixture to time out after 30s even though the panel would render
    a few seconds later once navigation settles.

    Mirrors the retry pattern in
    ``tests/e2e/test_ui.py::_find_card`` (commit aa25b10).

    Genuine ``TimeoutError`` (panel never rendered) is propagated.
    Non-context-destruction Playwright errors are also propagated.
    """
    from playwright._impl._errors import Error as PlaywrightError  # noqa: PLC0415
    from playwright._impl._errors import TimeoutError as PwTimeoutError  # noqa: PLC0415

    deadline = time.monotonic() + timeout_ms / 1000
    remaining_ms = timeout_ms

    while True:
        try:
            page.wait_for_function(_PANEL_READY_JS, timeout=remaining_ms)
            return
        except PwTimeoutError:
            # Genuine timeout — panel truly did not render in budget.
            raise
        except PlaywrightError as exc:
            if not any(s in str(exc) for s in _CONTEXT_DESTROYED_SIGNALS):
                # Unrelated playwright failure — propagate.
                raise
            # Navigation destroyed the context.  Settle on networkidle
            # (best effort) and retry with whatever budget remains.
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise
            with contextlib.suppress(PlaywrightError):
                page.wait_for_load_state(
                    "networkidle",
                    timeout=min(remaining_ms, 15000),
                )
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise


@pytest.fixture
def page(
    browser_context: BrowserContext,
    ha_port: int,
    ha_e2e: HAClient,  # noqa: ARG001 — ensure container is up
) -> Generator[Page, None, None]:
    """Function-scoped page navigated to HA dashboard."""
    t0 = time.monotonic()
    p = browser_context.new_page()
    p.goto(f"http://localhost:{ha_port}/lovelace/0", timeout=60000)
    p.wait_for_url("**/lovelace/**", timeout=60000)
    p.wait_for_load_state("networkidle", timeout=30000)
    # Wait for the HA Lovelace panel to render inside the shadow DOM.
    # Uses _wait_for_lovelace_panel which retries on
    # "Execution context was destroyed" errors caused by navigation
    # churn under CI load (mirrors _find_card's pattern, aa25b10).
    _wait_for_lovelace_panel(p, timeout_ms=30000)
    _log.warning("[%s] page ready: %.1fs", _worker_id(), time.monotonic() - t0)
    yield p
    p.close()


# ---------------------------------------------------------------------------
# Helpers for tests
# ---------------------------------------------------------------------------


def set_inverter_state(
    connection_mode: str,
    foxess_sim: SimulatorHandle | None,
    ha_e2e: HAClient,
    event_stream: HAEventStream | None = None,
    **kwargs: float,
) -> None:
    """Set inverter state via simulator (cloud) or input helpers (entity)."""
    if connection_mode == "cloud" and foxess_sim is not None:
        foxess_sim.set(**kwargs)
    else:
        if "soc" in kwargs:
            ha_e2e.set_input_number("input_number.foxess_soc", float(kwargs["soc"]))
        if "solar_kw" in kwargs:
            ha_e2e.set_input_number(
                "input_number.foxess_pv_power", float(kwargs["solar_kw"])
            )
        if "load_kw" in kwargs:
            ha_e2e.set_input_number(
                "input_number.foxess_loads_power", float(kwargs["load_kw"])
            )
        # Wait for the entity coordinator to propagate values.
        if "soc" in kwargs:
            target = str(float(kwargs["soc"]))
            if event_stream is not None:
                event_stream.wait_for_state(
                    "sensor.foxess_battery_soc",
                    target,
                    timeout_s=30,
                )
            else:
                ha_e2e.wait_for_numeric_state(
                    "sensor.foxess_battery_soc",
                    "ge",
                    float(kwargs["soc"]) - 1,
                    timeout_s=30,
                    poll_interval=1.0,
                )
        elif kwargs:
            if "solar_kw" in kwargs:
                ha_e2e.wait_for_numeric_state(
                    "sensor.foxess_solar_power",
                    "ge",
                    float(kwargs["solar_kw"]) - 0.1,
                    timeout_s=30,
                    poll_interval=1.0,
                )
            elif "load_kw" in kwargs:
                ha_e2e.wait_for_numeric_state(
                    "sensor.foxess_house_load",
                    "ge",
                    float(kwargs["load_kw"]) - 0.1,
                    timeout_s=30,
                    poll_interval=1.0,
                )
            else:
                time.sleep(2)


@pytest.fixture(params=["api", "ws", "entity"])
def data_source(
    request: pytest.FixtureRequest,
    foxess_sim: SimulatorHandle | None,
    ha_e2e: HAClient,
    connection_mode: str,
) -> Generator[str, None, None]:
    """Control the active data source for the test.

    Valid combinations: cloud → [api, ws], entity → [entity].
    Invalid cross-products are deselected at collection time by
    pytest_collection_modifyitems (see _INVALID_COMBOS).  The runtime
    guards below are kept as a safety net.
    """
    mode: str = request.param
    if connection_mode == "entity" and mode != "entity":
        pytest.skip(f"{mode} not valid for entity mode")
    if connection_mode == "cloud" and mode == "entity":
        pytest.skip("entity not valid for cloud mode")
    if mode == "api" and foxess_sim is not None:
        foxess_sim.fault("ws_refuse")
    if mode == "ws":
        ha_e2e.set_options(ws_mode="smart_sessions")
    yield mode


@pytest.fixture
def structured_logs(
    ha_e2e: HAClient,
) -> Callable[[], list[dict[str, Any]]]:
    """Return a callable that fetches debug log entries with session context."""

    def _get() -> list[dict[str, Any]]:
        attrs = ha_e2e.get_attributes("sensor.foxess_control_debug_log")
        return [e for e in attrs.get("entries", []) if e.get("session")]

    return _get
