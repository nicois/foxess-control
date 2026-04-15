"""E2E test fixtures: simulator + HA container."""

from __future__ import annotations

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
            "iss": "e2e-refresh-001",  # matches refresh_tokens[0].id in auth
            "iat": _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
            "exp": _dt.datetime(2036, 1, 1, tzinfo=_dt.UTC),
        },
        "e2e-jwt-key-not-used-for-long-lived",  # matches jwt_key in auth
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


def _start_simulator() -> tuple[subprocess.Popen[bytes], int]:
    """Start the simulator in a subprocess, return (process, port)."""
    import socket

    # Find a free port
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    proc = subprocess.Popen(
        ["python", "-m", "simulator", "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Wait for it to be ready
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"http://localhost:{port}/sim/state", timeout=1)
            if r.status_code == 200:
                return proc, port
        except requests.ConnectionError:
            pass
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("Simulator did not start")


def _build_container() -> None:
    """Build the HA container image (cached by podman)."""
    subprocess.run(
        ["podman", "build", "-t", CONTAINER_IMAGE, str(REPO_ROOT / "e2e")],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def foxess_sim() -> Generator[SimulatorHandle, None, None]:
    """Start the FoxESS simulator for E2E tests."""
    proc, port = _start_simulator()
    handle = SimulatorHandle(f"http://localhost:{port}")
    yield handle
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def ha_e2e(
    foxess_sim: SimulatorHandle,
) -> Generator[HAClient, None, None]:
    """Start a HA container pointed at the simulator."""
    _build_container()

    # Copy ha_config to a temp dir (HA needs write access to .storage and deps)
    tmpdir = tempfile.mkdtemp(prefix="ha-e2e-")
    shutil.copytree(str(HA_CONFIG_SEED), tmpdir, dirs_exist_ok=True)
    # Ensure the container user (root in HA image) can write
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
    except TimeoutError:
        # Dump container logs for debugging
        if proc.stdout:
            print(proc.stdout.read().decode(errors="replace")[-3000:])
        proc.kill()
        raise

    # Extra wait for integration to fully load
    time.sleep(10)

    yield ha

    proc.terminate()
    proc.wait(timeout=30)
    shutil.rmtree(tmpdir, ignore_errors=True)
