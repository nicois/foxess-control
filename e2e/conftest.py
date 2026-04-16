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

from .ha_client import HAClient

_log = logging.getLogger("e2e.timing")


_test_durations: dict[str, float] = {}


def pytest_configure(config: Any) -> None:
    """Ensure e2e.timing messages appear in pytest output."""
    logging.getLogger("e2e.timing").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("e2e.timing").addHandler(handler)


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


@pytest.fixture(scope="session", params=["cloud", "entity"])
def connection_mode(request: pytest.FixtureRequest) -> str:
    """Control whether tests run against cloud API or entity mode."""
    return str(request.param)


@pytest.fixture(scope="session")
def _worker_ports() -> dict[str, int]:
    """Allocate unique ports for this xdist worker."""
    return {"sim": _find_free_port(), "ha": _find_free_port()}


@pytest.fixture(scope="session")
def foxess_sim(
    _worker_ports: dict[str, int],
    connection_mode: str,
) -> Generator[SimulatorHandle | None, None, None]:
    """Start the FoxESS simulator (cloud mode only)."""
    if connection_mode != "cloud":
        yield None
        return
    wid = _worker_id()
    t0 = time.monotonic()
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

    _log.warning("[%s] simulator ready in %.1fs", wid, time.monotonic() - t0)
    yield SimulatorHandle(f"http://localhost:{port}")

    _cleanup()
    atexit.unregister(_cleanup)


@pytest.fixture(scope="session")
def ha_port(_worker_ports: dict[str, int]) -> int:
    """The HA port for this worker."""
    return _worker_ports["ha"]


@pytest.fixture(scope="session")
def ha_e2e(
    foxess_sim: SimulatorHandle | None,
    ha_port: int,
    connection_mode: str,
) -> Generator[HAClient, None, None]:
    """Start a HA container with unique port.

    The container is named ``ha-e2e-{worker_id}`` so it can be
    found and cleaned up deterministically.  An atexit handler
    ensures the container is stopped even if the fixture teardown
    is skipped (setup failure, worker crash, SIGTERM).

    In entity mode, the config files are swapped for variants that
    define input helpers and an entity-mode config entry.
    """
    wid = _worker_id()
    t0 = time.monotonic()
    _build_container()
    _log.warning("[%s] container build: %.1fs", wid, time.monotonic() - t0)

    name = _container_name()
    _stop_container(name)

    tmpdir = tempfile.mkdtemp(prefix="ha-e2e-")
    shutil.copytree(str(HA_CONFIG_SEED), tmpdir, dirs_exist_ok=True)

    if connection_mode == "entity":
        # Overwrite with entity-mode config variants
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
        t1 = time.monotonic()
        ha.wait_ready(timeout_s=120)
        _log.warning("[%s] HA ready: %.1fs", wid, time.monotonic() - t1)

        # Wait for integration entities
        t2 = time.monotonic()
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
        _log.warning(
            "[%s] entities ready: %.1fs (total: %.1fs)",
            wid,
            time.monotonic() - t2,
            time.monotonic() - t0,
        )
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
    t0 = time.monotonic()
    p = browser_context.new_page()
    p.goto(f"http://localhost:{ha_port}/lovelace/0", timeout=60000)
    p.wait_for_url("**/lovelace/**", timeout=60000)
    p.wait_for_load_state("networkidle", timeout=30000)
    p.wait_for_timeout(2000)  # let custom cards render
    _log.warning("[%s] page ready: %.1fs", _worker_id(), time.monotonic() - t0)
    yield p
    p.close()


# ---------------------------------------------------------------------------
# Function-scoped autouse reset
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _e2e_reset(
    foxess_sim: SimulatorHandle | None,
    ha_e2e: HAClient,
    connection_mode: str,
) -> Generator[None, None, None]:
    """Reset simulator/entities and clear HA sessions before each test."""
    t0 = time.monotonic()
    if connection_mode == "cloud" and foxess_sim is not None:
        foxess_sim.reset()
    elif connection_mode == "entity":
        with contextlib.suppress(Exception):
            ha_e2e.set_input_select("input_select.foxess_work_mode", "Self Use")
            ha_e2e.set_input_number("input_number.foxess_soc", 50)
            ha_e2e.set_input_number("input_number.foxess_charge_power", 0)
            ha_e2e.set_input_number("input_number.foxess_discharge_power", 0)
            ha_e2e.set_input_number("input_number.foxess_min_soc", 10)
            ha_e2e.set_input_number("input_number.foxess_loads_power", 0)
            ha_e2e.set_input_number("input_number.foxess_pv_power", 0)
    with contextlib.suppress(Exception):
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
    with contextlib.suppress(TimeoutError):
        ha_e2e.wait_for_state("sensor.foxess_smart_operations", "idle", timeout_s=10)
    _log.warning("[%s] reset: %.1fs", _worker_id(), time.monotonic() - t0)

    yield

    with contextlib.suppress(Exception):
        ha_e2e.call_service("foxess_control", "clear_overrides", {})


def set_inverter_state(
    connection_mode: str,
    foxess_sim: SimulatorHandle | None,
    ha_e2e: HAClient,
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
                "input_number.foxess_pv_power", float(kwargs["solar_kw"]) * 1000
            )
        if "load_kw" in kwargs:
            ha_e2e.set_input_number(
                "input_number.foxess_loads_power", float(kwargs["load_kw"]) * 1000
            )
        # Wait for the entity coordinator to pick up the new values
        if "soc" in kwargs:
            ha_e2e.wait_for_numeric_state(
                "sensor.foxess_battery_soc",
                "ge",
                float(kwargs["soc"]) - 1,
                timeout_s=15,
                poll_interval=1.0,
            )


@pytest.fixture(params=["api", "ws", "entity"])
def data_source(
    request: pytest.FixtureRequest,
    foxess_sim: SimulatorHandle | None,
    connection_mode: str,
    _e2e_reset: None,
) -> Generator[str, None, None]:
    """Control the active data source for the test.

    Skips inapplicable combinations:
    - cloud + "entity" → skip
    - entity + "api"/"ws" → skip
    """
    mode: str = request.param
    if connection_mode == "entity" and mode != "entity":
        pytest.skip(f"{mode} only applies to cloud mode")
    if connection_mode == "cloud" and mode == "entity":
        pytest.skip("entity only applies to entity mode")
    if mode == "api" and foxess_sim is not None:
        foxess_sim.fault("ws_refuse")
    yield mode
