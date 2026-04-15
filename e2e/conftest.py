"""E2E test fixtures: simulator + HA container.
import contextlib

Scoping model (xdist-compatible):
- foxess_sim: session scope — one simulator per worker, fixed port
- ha_e2e: session scope — one HA container per worker
- _e2e_reset: function scope (autouse) — resets sim + clears HA sessions
"""

from __future__ import annotations

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


# Generate a valid HA JWT token matching the pre-seeded .storage/auth file
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


def _find_free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("", 0))
        port: int = s.getsockname()[1]
        return port


def _build_container() -> None:
    subprocess.run(
        ["podman", "build", "-t", CONTAINER_IMAGE, str(REPO_ROOT / "e2e")],
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Session-scoped fixtures (one per xdist worker)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def foxess_sim() -> Generator[SimulatorHandle, None, None]:
    """Start the FoxESS simulator — one per xdist worker."""
    port = _find_free_port()
    proc = subprocess.Popen(
        ["python", "-m", "simulator", "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Wait for ready
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
        proc.kill()
        raise RuntimeError("Simulator did not start")

    yield SimulatorHandle(f"http://localhost:{port}")

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="session")
def ha_e2e(foxess_sim: SimulatorHandle) -> Generator[HAClient, None, None]:
    """Start a HA container — one per xdist worker."""
    _build_container()

    tmpdir = tempfile.mkdtemp(prefix="ha-e2e-")
    shutil.copytree(str(HA_CONFIG_SEED), tmpdir, dirs_exist_ok=True)
    os.chmod(tmpdir, 0o777)
    for root, dirs, _files in os.walk(tmpdir):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o777)

    proc = subprocess.Popen(
        [
            "podman",
            "run",
            "--rm",
            "--network=host",
            "-v",
            f"{REPO_ROOT}/custom_components/foxess_control"
            f":/config/custom_components/foxess_control:ro,Z",
            "-v",
            f"{tmpdir}:/config:Z",
            "-e",
            f"FOXESS_SIMULATOR_URL={foxess_sim.url}",
            CONTAINER_IMAGE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    ha = HAClient("http://localhost:8123", E2E_TOKEN)
    try:
        ha.wait_ready(timeout_s=120)
    except TimeoutError:  # noqa: SIM105
        if proc.stdout:
            print(proc.stdout.read().decode(errors="replace")[-3000:])
        proc.kill()
        raise

    # Wait for integration to fully load (entity exists = integration ready)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            ha.get_state("sensor.foxess_battery_soc")
            break
        except Exception:
            time.sleep(2)
    else:
        raise TimeoutError("Integration entities not created within 60s")

    yield ha

    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Function-scoped autouse fixture (resets state between tests)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _e2e_reset(
    foxess_sim: SimulatorHandle, ha_e2e: HAClient
) -> Generator[None, None, None]:
    """Reset simulator and clear HA sessions before each test.

    This ensures no state leaks between tests. The simulator is
    reset to defaults, and any active HA smart sessions are cancelled.
    The poll cycle (10s) picks up the fresh state.
    """
    foxess_sim.reset()
    with contextlib.suppress(Exception):
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
    # Poll until HA shows idle (confirms reset propagated)
    with contextlib.suppress(TimeoutError):
        ha_e2e.wait_for_state("sensor.foxess_smart_operations", "idle", timeout_s=30)

    yield

    with contextlib.suppress(Exception):
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
