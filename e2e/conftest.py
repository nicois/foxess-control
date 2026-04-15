"""E2E test fixtures: simulator + HA container + Playwright browser.

Scoping model (xdist-compatible):
- _worker_ports: session — unique sim + HA ports per worker
- foxess_sim: session — one simulator per worker
- ha_e2e: session — one HA container per worker
- browser_context: session — one authenticated browser per worker
- page: function — fresh tab per test
- _e2e_reset: function (autouse) — resets sim + clears HA

Resource lifecycle:
- Named containers (ha-e2e-{worker_id}) enable deterministic cleanup
- atexit handlers catch abnormal exits (SIGTERM, unhandled exceptions)
- Setup failures trigger immediate cleanup before re-raising
- Per-worker self-cleanup removes own stale container from prior runs
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import requests

from .ha_client import HAClient

if TYPE_CHECKING:
    from collections.abc import Generator

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
REPO_ROOT = Path(__file__).resolve().parent.parent
HA_CONFIG_SEED = REPO_ROOT / "e2e" / "ha_config"
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


def _build_container() -> None:
    """Build the HA container image, serialised across xdist workers."""
    import filelock

    lock = filelock.FileLock(str(REPO_ROOT / ".e2e-build.lock"), timeout=300)
    with lock:
        subprocess.run(
            ["podman", "build", "-t", CONTAINER_IMAGE, str(REPO_ROOT / "e2e")],
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


@pytest.fixture(scope="session")
def _worker_ports() -> dict[str, int]:
    """Allocate unique ports for this xdist worker."""
    return {"sim": _find_free_port(), "ha": _find_free_port()}


@pytest.fixture(scope="session")
def foxess_sim(
    _worker_ports: dict[str, int],
) -> Generator[SimulatorHandle, None, None]:
    """Start the FoxESS simulator."""
    port = _worker_ports["sim"]
    proc = subprocess.Popen(
        ["python", "-m", "simulator", "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _cleanup() -> None:
        _kill_process(proc)

    atexit.register(_cleanup)

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
        _cleanup()
        atexit.unregister(_cleanup)
        raise RuntimeError("Simulator did not start")

    yield SimulatorHandle(f"http://localhost:{port}")

    _cleanup()
    atexit.unregister(_cleanup)


@pytest.fixture(scope="session")
def ha_port(_worker_ports: dict[str, int]) -> int:
    """The HA port for this worker."""
    return _worker_ports["ha"]


@pytest.fixture(scope="session")
def ha_e2e(
    foxess_sim: SimulatorHandle,
    ha_port: int,
) -> Generator[HAClient, None, None]:
    """Start a HA container with unique port.

    The container is named ``ha-e2e-{worker_id}`` so it can be
    found and cleaned up deterministically.  An atexit handler
    ensures the container is stopped even if the fixture teardown
    is skipped (setup failure, worker crash, SIGTERM).
    """
    _build_container()

    name = _container_name()
    # Stop any stale container with our name from a prior failed run.
    _stop_container(name)

    tmpdir = tempfile.mkdtemp(prefix="ha-e2e-")
    shutil.copytree(str(HA_CONFIG_SEED), tmpdir, dirs_exist_ok=True)
    os.chmod(tmpdir, 0o777)
    for root, dirs, _files in os.walk(tmpdir):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o777)

    sim_port = foxess_sim.url.rsplit(":", 1)[1]
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
            f":/config/custom_components/foxess_control:ro,Z",
            "-v",
            f"{tmpdir}:/config:Z",
            "-e",
            f"FOXESS_SIMULATOR_URL=http://host.containers.internal:{sim_port}",
            CONTAINER_IMAGE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _cleanup() -> None:
        _stop_container(name)
        _kill_process(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)

    atexit.register(_cleanup)

    try:
        ha = HAClient(f"http://localhost:{ha_port}", E2E_TOKEN)
        ha.wait_ready(timeout_s=120)

        # Wait for integration entities
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                ha.get_state("sensor.foxess_battery_soc")
                break
            except Exception:
                time.sleep(2)
        else:
            if proc.stdout:
                print(proc.stdout.read().decode(errors="replace")[-3000:])
            raise TimeoutError("Integration entities not created within 120s")
    except:
        _cleanup()
        atexit.unregister(_cleanup)
        raise

    yield ha

    _cleanup()
    atexit.unregister(_cleanup)


# ---------------------------------------------------------------------------
# Playwright fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context(
    playwright: Playwright,
    ha_e2e: HAClient,  # ensure HA is running
) -> Generator[BrowserContext, None, None]:
    """Session-scoped browser context.

    Auth is bypassed via pre-seeded http.auth content_user
    (same approach as home-assistant-query-selector E2E tests).
    No login form interaction needed.
    """
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()
    yield context
    context.close()
    browser.close()


@pytest.fixture
def page(
    browser_context: BrowserContext,
    ha_port: int,
) -> Generator[Page, None, None]:
    """Function-scoped page navigated to HA dashboard.

    HA's trusted_networks with allow_bypass_login auto-completes the
    OAuth flow: browser → /auth/authorize → auto-approve → redirect
    back to dashboard. We wait for this chain to complete.
    """
    p = browser_context.new_page()
    p.goto(f"http://localhost:{ha_port}/lovelace/0", timeout=60000)
    # Wait for the auth redirect chain to complete and dashboard to load
    p.wait_for_url("**/lovelace/**", timeout=60000)
    p.wait_for_load_state("networkidle", timeout=30000)
    p.wait_for_timeout(5000)  # let custom cards load and render
    yield p
    p.close()


# ---------------------------------------------------------------------------
# Function-scoped autouse reset
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _e2e_reset(
    foxess_sim: SimulatorHandle,
    ha_e2e: HAClient,
) -> Generator[None, None, None]:
    """Reset simulator and clear HA sessions before each test."""
    foxess_sim.reset()
    with contextlib.suppress(Exception):
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
    with contextlib.suppress(TimeoutError):
        ha_e2e.wait_for_state("sensor.foxess_smart_operations", "idle", timeout_s=30)

    yield

    with contextlib.suppress(Exception):
        ha_e2e.call_service("foxess_control", "clear_overrides", {})


@pytest.fixture(params=["api", "ws"])
def data_source(
    request: pytest.FixtureRequest,
    foxess_sim: SimulatorHandle,
    _e2e_reset: None,
) -> Generator[str, None, None]:
    """Control the active data source for the test.

    Depends on _e2e_reset so the fault is injected AFTER reset clears
    all faults.  Tests that use this fixture run twice — once per mode.

    Extensible: add "modbus" to params when a Modbus simulator exists.
    """
    mode: str = request.param
    if mode == "api":
        foxess_sim.fault("ws_refuse")
    yield mode
