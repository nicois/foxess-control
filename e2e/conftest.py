"""E2E test fixtures: simulator + HA container + Playwright browser.

Scoping model (xdist-compatible):
- _worker_ports: session — unique sim + HA ports per worker
- foxess_sim: session — one simulator per worker
- ha_e2e: session — one HA container per worker
- browser_context: session — one authenticated browser per worker
- page: function — fresh tab per test
- _e2e_reset: function (autouse) — resets sim + clears HA
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
    subprocess.run(
        ["podman", "build", "-t", CONTAINER_IMAGE, str(REPO_ROOT / "e2e")],
        check=True,
        capture_output=True,
    )


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
def ha_port(_worker_ports: dict[str, int]) -> int:
    """The HA port for this worker."""
    return _worker_ports["ha"]


@pytest.fixture(scope="session")
def ha_e2e(
    foxess_sim: SimulatorHandle,
    ha_port: int,
) -> Generator[HAClient, None, None]:
    """Start a HA container with unique port."""
    _build_container()

    tmpdir = tempfile.mkdtemp(prefix="ha-e2e-")
    shutil.copytree(str(HA_CONFIG_SEED), tmpdir, dirs_exist_ok=True)
    os.chmod(tmpdir, 0o777)
    for root, dirs, _files in os.walk(tmpdir):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o777)

    # Write dynamic configuration.yaml with the worker's HA port
    config_yaml = Path(tmpdir) / "configuration.yaml"
    config_yaml.write_text(f"default_config:\n\nhttp:\n  server_port: {ha_port}\n")

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

    ha = HAClient(f"http://localhost:{ha_port}", E2E_TOKEN)
    try:
        ha.wait_ready(timeout_s=120)
    except TimeoutError:
        if proc.stdout:
            print(proc.stdout.read().decode(errors="replace")[-3000:])
        proc.kill()
        raise

    # Wait for integration entities
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
# Playwright fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context(
    playwright: Playwright,
    ha_port: int,
    ha_e2e: HAClient,  # ensure HA is running
) -> Generator[BrowserContext, None, None]:
    """Session-scoped browser context, authenticated via HA login form.

    Logs in once at session start. The auth state persists across
    all pages in this context (session cookies).
    """
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context()

    # Log in via the HA UI
    login_page = context.new_page()
    login_page.goto(
        f"http://localhost:{ha_port}",
        wait_until="networkidle",
        timeout=60000,
    )
    # HA redirects to /auth/authorize — fill in credentials
    # The login form uses web components but the inputs are accessible
    login_page.wait_for_timeout(3000)  # let frontend JS load
    # HA uses Material Design web components with shadow DOM.
    # Fill inputs and click button via JavaScript evaluation.
    login_page.evaluate("""() => {
        function findInShadows(root, selector) {
            const el = root.querySelector(selector);
            if (el) return el;
            for (const child of root.querySelectorAll('*')) {
                if (child.shadowRoot) {
                    const found = findInShadows(child.shadowRoot, selector);
                    if (found) return found;
                }
            }
            return null;
        }
        const inputs = [];
        function findAllInputs(root) {
            for (const input of root.querySelectorAll('input')) {
                inputs.push(input);
            }
            for (const child of root.querySelectorAll('*')) {
                if (child.shadowRoot) findAllInputs(child.shadowRoot);
            }
        }
        findAllInputs(document);
        // First text input = username, first password input = password
        const username = inputs.find(i => i.type === 'text' || i.type === 'email');
        const password = inputs.find(i => i.type === 'password');
        if (username) {
            username.value = 'e2e-test';
            username.dispatchEvent(new Event('input', {bubbles: true}));
        }
        if (password) {
            password.value = 'e2e-test-password';
            password.dispatchEvent(new Event('input', {bubbles: true}));
        }
    }""")
    login_page.wait_for_timeout(500)
    # Click the Log in button
    login_page.evaluate("""() => {
        function findInShadows(root, text) {
            for (const btn of root.querySelectorAll('button, mwc-button, ha-button')) {
                if (btn.textContent && btn.textContent.trim().includes(text))
                    return btn;
            }
            for (const child of root.querySelectorAll('*')) {
                if (child.shadowRoot) {
                    const found = findInShadows(child.shadowRoot, text);
                    if (found) return found;
                }
            }
            return null;
        }
        const btn = findInShadows(document, 'Log in');
        if (btn) btn.click();
    }""")
    # Debug: screenshot after login attempt
    # Wait for redirect to dashboard after successful login
    login_page.wait_for_url("**/lovelace/**", timeout=30000)
    login_page.close()

    yield context
    context.close()
    browser.close()


@pytest.fixture
def page(
    browser_context: BrowserContext,
    ha_port: int,
) -> Generator[Page, None, None]:
    """Function-scoped page navigated to HA dashboard."""
    p = browser_context.new_page()
    p.goto(
        f"http://localhost:{ha_port}/lovelace/0",
        wait_until="networkidle",
        timeout=30000,
    )
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
