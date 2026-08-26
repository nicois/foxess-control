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

import atexit
import contextlib
import hashlib
import itertools
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

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

    from playwright.sync_api import (
        Browser,
        BrowserContext,
        BrowserType,
        Page,
        Playwright,
    )


# ---------------------------------------------------------------------------
# Counter-override of the unit-suite pytest-playwright scope narrowing.
#
# ``tests/conftest.py`` declares ``playwright`` / ``browser_type`` /
# ``browser`` / ``context`` / ``page`` at **function** scope so that
# pytest-playwright's greenlet-backed asyncio loop is torn down at
# the end of each unit test that uses ``page`` (preventing the
# ``Runner.run() cannot be called from a running event loop``
# cross-test flake on xdist workers that happen to run a page-using
# test before an asyncio test).
#
# Pytest's fixture-resolution rule is that a fixture declared in a
# nearer conftest overrides one declared in a farther parent
# conftest.  The E2E suite under ``tests/e2e/`` ships its own
# ``browser_context`` at **session** scope and must therefore have
# session-scoped ``playwright`` (and downstream chain) visible to
# tests collected here — otherwise the session-scoped
# ``browser_context`` hits ``ScopeMismatch`` at setup.  (That is the
# exact failure that broke 18/20 E2E shards on CI run 25023535568 —
# reverted at ``69caf66`` and ``960381e``.)
#
# These fixtures restore the session scope **only for tests under
# ``tests/e2e/``**.  The unit-suite function-scoped overrides remain
# in effect for the rest of ``tests/``.
#
# Refs C-029 (E2E tests for HA-dependent behaviour — must not break).
# Refs C-031 (no flaky tests — root cause).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def playwright() -> Generator[Playwright, None, None]:
    """Session-scoped ``playwright`` for the E2E suite only.

    Re-declaring at session scope here wins over the function-scoped
    override in ``tests/conftest.py`` for tests collected under
    ``tests/e2e/``.  Required because ``browser_context`` below is
    session-scoped and cannot depend on a function-scoped fixture.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        pw.stop()


@pytest.fixture(scope="session")
def browser_type(playwright: Playwright, browser_name: str) -> BrowserType:
    """Session-scoped ``browser_type`` for the E2E suite only.

    Mirrors pytest-playwright's default.  Only needed because the
    unit-suite override in ``tests/conftest.py`` declares it at
    function scope; without re-declaring here, any consumer that
    reaches ``browser_type`` via pytest-playwright's session-scoped
    ``launch_browser`` / ``browser`` fixtures would ScopeMismatch.
    """
    return getattr(playwright, browser_name)


@pytest.fixture(scope="session")
def browser(browser_type: BrowserType) -> Generator[Browser, None, None]:
    """Session-scoped ``browser`` for the E2E suite only.

    Not used directly by E2E tests (``browser_context`` below calls
    ``playwright.chromium.launch`` itself), but re-declared to keep
    the full pytest-playwright fixture chain scope-consistent in
    case any future E2E test wires ``browser`` in.
    """
    b = browser_type.launch(headless=True)
    try:
        yield b
    finally:
        b.close()


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

# Container-name prefixes this repo owns.  Anything not matching one of
# these is another tool's container and is never touched.
CONTAINER_PREFIX = "ha-e2e"
SOAK_CONTAINER_PREFIX = "ha-soak"
MANAGED_PREFIXES = (CONTAINER_PREFIX, SOAK_CONTAINER_PREFIX)


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


# ---------------------------------------------------------------------------
# Host-port allocation (C-043)
#
# Asking the OS for a free port (bind port 0, read it, release it) is
# necessary but not sufficient: ``podman run -p`` binds the port seconds
# after the probe socket closed, and in that window a *concurrent* pytest
# run's probe is free to be handed the very same port.  Measured on a
# 32-core host, six concurrent processes drawing 50 ports each produced
# duplicates in 5 of 6 rounds.
#
# So the OS probe is paired with a host-wide claim registry: a directory
# of ``<port>`` files, each naming its owning pid, mutated only under a
# cross-process file lock.  A port is only returned once it has been both
# (a) confirmed free by the kernel and (b) claimed exclusively, so two
# cooperating runs cannot be handed the same port however their probes
# interleave.  Claims are released at interpreter exit and any claim
# whose owner pid is gone is reaped, so a crashed run does not sterilise
# its ports.
#
# Deliberately NOT a hash of the checkout path: two different paths can
# hash to the same port, and there would be no way to notice.
# ---------------------------------------------------------------------------

_PORT_CLAIM_DIR = Path(tempfile.gettempdir()) / "foxess-e2e-port-claims"
_PORT_CLAIM_LOCK = Path(tempfile.gettempdir()) / "foxess-e2e-port-claims.lock"
_OWN_PORT_CLAIMS: set[int] = set()
_PORT_CLAIMS_REAPED = False


def _pid_is_alive(pid: int) -> bool:
    """True if ``pid`` names a live process on this host.

    ``PermissionError`` means the process exists but belongs to another
    user — alive.  Unknown ``OSError`` is treated as alive so an
    ambiguous answer never authorises destroying someone's resources.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _probe_free_port() -> int:
    """Ask the kernel for a port that is free *right now*."""
    with socket.socket() as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def _claim_owner(port: int) -> int | None:
    try:
        return int((_PORT_CLAIM_DIR / str(port)).read_text().strip())
    except (OSError, ValueError):
        return None


def claim_port(
    port: int,
    *,
    pid_is_alive: Callable[[int], bool] | None = None,
) -> bool:
    """Reserve ``port`` for this process host-wide.

    Returns ``False`` when a live owner already holds the claim — the
    owner may be another run, or this same run's other fixture (the
    simulator and HA ports must differ too).  A claim whose owner is gone
    is taken over, so a crashed run's ports return to circulation.
    """
    alive = pid_is_alive or _pid_is_alive
    owner = _claim_owner(port)
    if owner is not None and alive(owner):
        return False
    _PORT_CLAIM_DIR.mkdir(parents=True, exist_ok=True)
    (_PORT_CLAIM_DIR / str(port)).write_text(f"{os.getpid()}\n")
    _OWN_PORT_CLAIMS.add(port)
    return True


def _reap_stale_port_claims() -> None:
    """Drop claim files whose owning process no longer exists."""
    with contextlib.suppress(OSError):
        for entry in _PORT_CLAIM_DIR.iterdir():
            if not entry.name.isdigit():
                continue
            owner = _claim_owner(int(entry.name))
            if owner is None or not _pid_is_alive(owner):
                with contextlib.suppress(OSError):
                    entry.unlink()


def release_port_claims() -> None:
    """Release this process's claims (registered with ``atexit``)."""
    for port in list(_OWN_PORT_CLAIMS):
        if _claim_owner(port) == os.getpid():
            with contextlib.suppress(OSError):
                (_PORT_CLAIM_DIR / str(port)).unlink()
        _OWN_PORT_CLAIMS.discard(port)


atexit.register(release_port_claims)


def allocate_free_port(*, attempts: int = 128) -> int:
    """Return a host port that no other cooperating run can be given.

    Probe and claim happen together under a host-wide file lock, so the
    probe of a concurrent run cannot interleave between our kernel probe
    and our claim.
    """
    global _PORT_CLAIMS_REAPED  # noqa: PLW0603
    import filelock  # noqa: PLC0415

    with filelock.FileLock(str(_PORT_CLAIM_LOCK), timeout=120):
        if not _PORT_CLAIMS_REAPED:
            # Once per process: bounds claim-file growth without paying
            # for a directory scan on every allocation.
            _reap_stale_port_claims()
            _PORT_CLAIMS_REAPED = True
        for _ in range(attempts):
            port = _probe_free_port()
            if claim_port(port):
                return port
    raise RuntimeError(
        f"Could not allocate an unclaimed free host port in {attempts} "
        f"attempts (claims: {_PORT_CLAIM_DIR})"
    )


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


_CAPTURE_COUNTER = itertools.count(1)


# Elements HA renders when a page or a card is terminally broken.
#
# ``hass-error-screen`` is the one that matters and the one that was
# missing: ``hass-router-page.createErrorScreen`` appends it when a panel's
# lazy module import rejects, with ``error = "Error while loading page
# lovelace."``.  That import is a ``Promise.all`` over 51 separate webpack
# chunks and is never re-issued for the life of the document, so once this
# element exists the dashboard can never finish rendering — waiting is
# futile and the only recovery is a fresh navigation
# (``open_lovelace_dashboard``).
#
# ``ha-panel-error`` / ``hui-error-card`` were the original two; they cover
# an unknown panel and a card that failed to render.  Keeping them means a
# broken *card* still fails fast rather than timing out.
_LOVELACE_ERROR_SELECTOR = "hass-error-screen, ha-panel-error, hui-error-card"

# Shared shadow-DOM walkers.  HA nests everything several shadow roots
# deep, so a flat ``document.querySelector`` finds none of it — the reason
# the old ``_DOM_SUMMARY_JS`` could never report an error message even
# when the error element was on screen.
_DEEP_DOM_JS = """
    function deepFind(root, sel) {
        const hit = root.querySelector(sel);
        if (hit) return hit;
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) {
                const found = deepFind(el.shadowRoot, sel);
                if (found) return found;
            }
        }
        return null;
    }
    function deepPresent(root, sel) { return !!deepFind(root, sel); }
"""


def _js(template: str) -> str:
    """Expand the shared JS placeholders in a predicate template.

    ``__DEEP__`` becomes the shadow-DOM walkers and ``__ERRORS__`` the
    terminal-error selector, so every predicate agrees on both.  Plain
    substitution rather than ``%``/``format`` because the templates are
    JavaScript and full of braces.
    """
    return template.replace("__DEEP__", _DEEP_DOM_JS).replace(
        "__ERRORS__", _LOVELACE_ERROR_SELECTOR
    )


_DOM_SUMMARY_JS = _js("""() => {
    __DEEP__
    const tags = ['home-assistant','home-assistant-main','ha-panel-lovelace',
        'hui-root','hass-error-screen','ha-panel-error',
        'hui-error-card'].filter(t => deepPresent(document, t));
    let error_text = null;
    const err = deepFind(document, '__ERRORS__');
    if (err) {
        // ``error`` is a Lit property on hass-error-screen, not a
        // reflected attribute, and the rendered message lives in the
        // element's own shadow root — so textContent alone is empty.
        const shadowText = err.shadowRoot ? err.shadowRoot.textContent : '';
        error_text = String(err.error || shadowText || err.textContent || '')
            .trim().slice(0, 300);
    }
    return {tags, error_text};
}""")


# Browser-side diagnostics recorded per page.
#
# HA logs the *reason* a panel failed to load
# (``console.error("Error loading page", "lovelace", err)``) and Playwright
# raises ``requestfailed`` naming the lost asset and the net error.  Neither
# was recorded, which is why three months of Flaky Test Detection artefacts
# contain the symptom and none of the cause.
_MAX_DIAGNOSTIC_ENTRIES = 500
_PAGE_DIAGNOSTICS: WeakKeyDictionary[Any, deque[str]] = WeakKeyDictionary()


def attach_page_diagnostics(page: Any) -> deque[str]:
    """Record console output, page errors and failed requests for ``page``.

    Returns the (bounded) buffer, which ``_capture_failure`` embeds in the
    failure summary and writes alongside the HTML/PNG capture.  Attach once
    per page, immediately after it is created and *before* navigating, so
    load-time failures are captured.
    """
    entries: deque[str] = deque(maxlen=_MAX_DIAGNOSTIC_ENTRIES)

    def _on_console(message: Any) -> None:
        if message.type in ("error", "warning"):
            entries.append(f"console[{message.type}] {message.text}")

    page.on("console", _on_console)
    page.on("pageerror", lambda exc: entries.append(f"pageerror {exc}"))
    page.on(
        "requestfailed",
        lambda request: entries.append(
            f"requestfailed {request.url} :: {request.failure}"
        ),
    )
    _PAGE_DIAGNOSTICS[page] = entries
    return entries


def page_diagnostics(page: Any) -> list[str]:
    """Diagnostics recorded for ``page``, oldest first (empty if none)."""
    return list(_PAGE_DIAGNOSTICS.get(page, ()))


def _failure_capture_dir() -> Path:
    d = Path(__file__).parent / "screenshots" / _worker_id() / "failures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text)[:60].strip("-") or "wait"


def _capture_failure(page: Any, description: str) -> str:
    """Best-effort DOM capture on a wait failure. Never raises."""
    n = next(_CAPTURE_COUNTER)
    base = _failure_capture_dir() / f"{_slug(description)}-{n}"
    parts = [f"capture #{n} ({description})"]
    with contextlib.suppress(Exception):
        info = page.evaluate(_DOM_SUMMARY_JS)
        if isinstance(info, dict):
            if info.get("tags") is not None:
                parts.append(f"elements present: {info['tags']}")
            if info.get("error_text"):
                parts.append(f"error panel text: {info['error_text']!r}")
    with contextlib.suppress(Exception):
        base.with_suffix(".html").write_text(page.content())
        parts.append(f"html: {base.with_suffix('.html').name}")
    with contextlib.suppress(Exception):
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        parts.append(f"png: {base.with_suffix('.png').name}")
    # Browser-side cause, if a recorder was attached.  The tail goes in the
    # message (so the CI log alone explains the failure) and the whole
    # buffer is written next to the HTML/PNG as an artefact.
    diagnostics = page_diagnostics(page)
    if diagnostics:
        with contextlib.suppress(Exception):
            base.with_suffix(".log").write_text("\n".join(diagnostics))
            parts.append(f"log: {base.with_suffix('.log').name}")
        tail = " ;; ".join(entry[:200] for entry in diagnostics[-6:])
        parts.append(f"browser: {tail}")
    return " | ".join(parts)


class E2EConditionTimeout(AssertionError):
    """A wait_for_condition pass_check did not become true in time."""


class E2EConditionFailed(AssertionError):
    """A wait_for_condition fail_check tripped before the pass_check."""


class E2EPanelLoadError(E2EConditionFailed):
    """HA rendered a terminal panel/card error screen for this document.

    Distinct from ``E2EConditionTimeout`` because the two demand opposite
    responses.  A timeout means "not ready yet" — waiting longer might
    help.  This means "this document will never render": HA's panel module
    import already rejected and is never retried, so the only way forward
    is a fresh navigation.  ``open_lovelace_dashboard`` acts on exactly
    this type and on nothing else.
    """


def wait_for_condition(
    page: Any,
    pass_check: str,
    *,
    timeout_ms: int,
    fail_check: str | None = None,
    description: str = "",
    poll_ms: int = 250,
) -> Any:
    """Poll pass_check until truthy; abort early if fail_check trips.

    Returns pass_check's truthy value. On fail_check tripping
    (E2EConditionFailed) or timeout (E2EConditionTimeout), captures DOM
    via _capture_failure and embeds the summary in the raised exception.
    Predicate evaluations hitting a context-destroyed navigation error are
    retried after a <=3000ms domcontentloaded settle within remaining budget.
    """
    from playwright._impl._errors import Error as _PwError  # noqa: PLC0415

    deadline = time.monotonic() + timeout_ms / 1000
    desc = description or "wait_for_condition"

    def _poll(expr: str) -> Any:
        while True:
            try:
                return page.evaluate(expr)
            except _PwError as exc:
                if not any(s in str(exc) for s in _CONTEXT_DESTROYED_SIGNALS):
                    raise
                settle = int((deadline - time.monotonic()) * 1000)
                if settle <= 0:
                    return None
                with contextlib.suppress(_PwError):
                    page.wait_for_load_state(
                        "domcontentloaded", timeout=min(settle, 3000)
                    )

    while True:
        if fail_check is not None and _poll(fail_check):
            summary = _capture_failure(page, desc)
            raise E2EConditionFailed(f"{desc}: fail_check tripped. {summary}")
        val = _poll(pass_check)
        if val:
            return val
        if time.monotonic() >= deadline:
            summary = _capture_failure(page, desc)
            raise E2EConditionTimeout(
                f"{desc}: pass_check not satisfied within {timeout_ms}ms. {summary}"
            )
        time.sleep(poll_ms / 1000)


# ---------------------------------------------------------------------------
# Container naming and ownership (C-043)
#
# The name must be unique per (checkout, run, worker) because the ``ha_e2e``
# fixture removes leftovers by name at *setup*.  With the old
# ``ha-e2e-{worker}`` scheme two concurrent runs both chose ``ha-e2e-gw0``,
# so whichever reached a test second issued ``podman rm -f`` against the
# other run's live container — the run losing the race then polled a dead
# container until its 120s budget expired.
#
# Three qualifiers, each answering a different collision:
#   checkout token  8 hex of sha256(resolved REPO_ROOT) — two worktrees
#                   of this repo never share a name.
#   run pid         the worker process's own pid — unique among *live*
#                   processes on the host by definition, so two
#                   simultaneous runs of the same checkout differ, and
#                   liveness of the pid tells later runs whether the
#                   owner is still around.
#   worker id       the xdist worker, so sibling workers in one run differ.
#
# Explicitly not a timestamp: a timestamp both collides (two runs started
# in the same tick) and is wrong here, because the name must be
# reproducible for the whole run — setup, ``podman logs`` and teardown all
# recompute it.
# ---------------------------------------------------------------------------

_OWNED_NAME_RE = re.compile(
    r"^(?P<prefix>" + "|".join(MANAGED_PREFIXES) + r")"
    r"-(?P<checkout>[0-9a-f]{8})"
    r"-(?P<pid>\d+)"
    r"-(?P<worker>[A-Za-z0-9_]+)$"
)


def _checkout_token() -> str:
    """Stable short token for this checkout's path."""
    return hashlib.sha256(str(Path(REPO_ROOT).resolve()).encode()).hexdigest()[:8]


def _container_name(prefix: str = CONTAINER_PREFIX) -> str:
    """Container name unique to this (checkout, run, worker).

    Stable for the lifetime of the process, so teardown and log capture
    resolve the same container setup created.
    """
    return f"{prefix}-{_checkout_token()}-{os.getpid()}-{_worker_id()}"


def container_is_reclaimable(
    name: str,
    *,
    pid_is_alive: Callable[[int], bool] | None = None,
) -> bool:
    """True only if removing ``name`` cannot disturb another run.

    Reclaimable means either *ours* — this checkout, this process, this
    worker — or a leftover of a **dead** run of this same checkout, which
    is what a crashed run leaves in ``podman ps -a``.

    Everything else is refused, including:
      * any name from another checkout, dead-looking or not (that
        checkout's own next run reclaims it; a pid this host has recycled
        must never look like permission to delete);
      * a live sibling worker's container in our own run;
      * the legacy unqualified ``ha-e2e-gw0`` / ``ha-soak-gw0`` names,
        which an in-flight run of the previous code could still own;
      * anything that is not one of our prefixes at all.
    """
    match = _OWNED_NAME_RE.match(name or "")
    if match is None or match.group("checkout") != _checkout_token():
        return False
    pid = int(match.group("pid"))
    if pid == os.getpid() and match.group("worker") == _worker_id():
        return True
    alive = pid_is_alive or _pid_is_alive
    return not alive(pid)


def _list_container_names() -> list[str]:
    """All container names podman knows about (empty on any failure)."""
    try:
        result = subprocess.run(
            ["podman", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log.debug("Could not list containers: %s", exc)
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def reclaim_stale_containers(
    *,
    list_names: Callable[[], list[str]] | None = None,
    remove: Callable[[str], None] | None = None,
    pid_is_alive: Callable[[int], bool] | None = None,
) -> list[str]:
    """Remove only the containers this run is entitled to remove.

    Returns the names removed.  ``list_names`` / ``remove`` /
    ``pid_is_alive`` are injectable so the decision can be tested against
    a realistic container list without podman.
    """
    lister = list_names or _list_container_names
    remover = remove or _stop_container
    reclaimed: list[str] = []
    for name in lister():
        if container_is_reclaimable(name, pid_is_alive=pid_is_alive):
            remover(name)
            reclaimed.append(name)
    if reclaimed:
        _log.warning("reclaimed stale containers: %s", ", ".join(reclaimed))
    return reclaimed


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
    """Allocate ports no concurrent run can also be given (C-043)."""
    return {"sim": allocate_free_port(), "ha": allocate_free_port()}


@pytest.fixture(scope="session")
def _container_built() -> None:
    """Ensure the container image is built (once per worker, serialised)."""
    _build_container_once()


@pytest.fixture(scope="session")
def _stale_containers_reclaimed() -> None:
    """Reclaim leftovers from crashed runs of this checkout (C-043).

    Once per worker, not per test: one ``podman ps -a`` is enough, and it
    only ever removes containers whose owning run is provably gone (see
    ``container_is_reclaimable``), so a concurrent run is untouched.
    """
    reclaim_stale_containers()


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
    _stale_containers_reclaimed: None,  # noqa: ARG001
) -> Generator[HAClient, None, None]:
    """Start a FRESH HA container for this test.

    Eliminates all state leaks: each test gets a clean HA instance
    with no residual sessions, WS connections, coordinator state,
    or cached data from prior tests.
    """
    wid = _worker_id()
    t0 = time.monotonic()
    # Unique to this (checkout, run, worker), so the setup-time hygiene
    # removal below can only ever hit a leftover of *this* worker's own
    # previous test — never a concurrent run's live container (C-043).
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


# The Lovelace panel lives three shadow-DOM hops deep:
#   <home-assistant> (document root)
#     #shadow-root
#       <home-assistant-main>
#         #shadow-root
#           <ha-panel-lovelace>
#
# Each stage has its own JS predicate and its own budget.  Staging is a
# slow-shard fix: beta.12 escape (run 24872997253) showed a monolithic
# 30s wait_for_function time out at 40.3s while other tests on the same
# shard ran 90.9s — the container was alive, just slow to boot.
#
# By splitting the wait we:
#   - Bound the worst-case legitimate budget to ~75s (3 stages x 25s),
#     so slow runners don't falsely fail.
#   - Produce observable per-stage failures: a timeout in stage 1 tells
#     us HA's frontend never attached; stage 2 means the main layout
#     didn't render; stage 3 means the dashboard router didn't mount
#     Lovelace.  Stage names appear in log output for diagnosis.
#
# Stages 1 and 2 (early DOM milestones) are fast under any CI load: they
# only check shadow-root attachment, which is done synchronously as soon
# as HA's bundled JS evaluates.  A tight 30s cap on these stages catches
# catastrophic failures (HA's frontend never loaded) without masking
# slow-panel scenarios.  Stage 3 is qualitatively different: it waits
# for HA's dynamic panel-module import to complete AND for the WS
# session to establish AND for partial-panel-resolver to hand the panel
# its hass reference.  Under adversarial CI timing (12 xdist workers
# contending for CPU, HA firing a housekeeping navigation at t=~12s),
# stage 3 legitimately needs the full remaining overall budget on its
# post-navigation retry, not a 30s per-call cap.  See
# test_retry_after_midstage_nav_uses_remaining_overall_budget for the
# concrete reproduction.

_STAGE_HOME_ASSISTANT = """() => {
    const el = document.querySelector('home-assistant');
    return !!(el && el.shadowRoot);
}"""

_STAGE_HOME_ASSISTANT_MAIN = """() => {
    const main = document.querySelector('home-assistant');
    if (!main || !main.shadowRoot) return false;
    const ham = main.shadowRoot.querySelector('home-assistant-main');
    return !!(ham && ham.shadowRoot);
}"""

# The final-stage predicate requires a *settled* panel, not merely an
# attached one.  Diagnosed 2026-04-25 by observing a live HA container:
# HA's frontend fires a full page navigation ~1-15s after the initial
# goto returns (auth refresh / service-worker registration).  If the
# predicate only checked ``!!panel``, the helper could return on a
# transient attachment moments before the navigation detaches the
# panel — causing the test body to hit ``Element is not attached to
# the DOM`` as observed in test_gallery_overview_idle[entity] on
# v1.0.13-beta.1 (run 24921297745).
#
# The predicate settles on TWO synchronous DOM facts:
#   (a) ha-panel-lovelace attached inside the main shadow root.
#   (b) hui-root mounted inside panel.shadowRoot — the DOM-observable
#       proof that the panel has completed at least one full render
#       cycle.  hui-root cannot exist unless panel.hass was assigned
#       AND main.hass.connected was true at render time AND a Lit
#       render cycle ran, so its presence implies both connectivity
#       and wiring — without any of the transient JS-property races.
#
# Evolution of this predicate:
#
# - Initial version: bare ``!!panel`` attach check.  Failed on
#   navigation-detach in test_gallery_overview_idle[entity]
#   (v1.0.13-beta.1).
#
# - 2026-04-26 fix (run 24956110840): replaced ``panel.hass`` with
#   ``hui-root`` as the stability signal.  ``panel.hass`` is a JS
#   property assigned by HA's partial-panel-resolver BETWEEN panel
#   mount and first render; under adversarial cloud-variant CI
#   timing the predicate cycled through retries without ever
#   observing panel.hass simultaneously truthy with the other
#   signals.
#
# - 2026-05-03 fix (test_time_picker_stays_open_during_rerender
#   [entity], 74951ms timeout in Flaky Test Detection): dropped
#   ``main.hass.connected`` from the gate.  main.hass.connected is
#   a live JS property reflecting the CURRENT state of HA's
#   frontend WebSocket.  Under entity-mode configuration (extra
#   input_number / input_select / input_boolean helpers plus the
#   EntityCoordinator reading each mapped entity on first refresh)
#   the state-change burst is heavier than cloud mode's single
#   REST poll, causing the frontend WS to transiently drop
#   connected=true→false→true for long enough to miss multiple
#   Playwright polls.  hui-root is STRICTLY STRONGER: once mounted,
#   it is proof the connection was live at render time, and the
#   runtime transient flip does not retroactively invalidate the
#   render.  Keeping connected in the gate produced a predicate
#   that could never converge when the churn outlasted the 75s
#   budget — the exact symptom observed in CI.
#
# hui-root is a synchronous DOM fact — once mounted inside
# panel.shadowRoot, it survives both the partial-panel-resolver
# wire-up race and the entity-mode state-burst WS flap.  We do NOT
# require panel.hass or main.hass.connected at poll time; hui-root's
# presence is strictly stronger than either (the Lit render that
# produced hui-root required both to be true at render time).
_STAGE_HA_PANEL_LOVELACE = """() => {
    const main = document.querySelector('home-assistant');
    if (!main || !main.shadowRoot) return false;
    const ham = main.shadowRoot.querySelector('home-assistant-main');
    if (!ham || !ham.shadowRoot) return false;
    // (a) ha-panel-lovelace attached.
    const panel = ham.shadowRoot.querySelector('ha-panel-lovelace');
    if (!panel || !panel.shadowRoot) return false;
    // (b) hui-root mounted inside panel.shadowRoot — DOM-observable
    // proof the panel completed a render cycle.  Strictly stronger
    // than checking live JS properties (panel.hass or
    // main.hass.connected): hui-root's existence proves the Lit
    // render occurred, which required those properties to be true
    // at render time.  Using DOM facts instead of JS properties
    // avoids the entity-mode WS-flap race and the cloud-mode
    // partial-panel-resolver wire-up race.  See comment block
    // above for the full evolution.
    const huiRoot = panel.shadowRoot.querySelector('hui-root');
    if (!huiRoot) return false;
    return true;
}"""


# Kept as a module-level constant for tests that still reference it
# (and in case a future consumer wants the monolithic predicate).
_PANEL_READY_JS = _STAGE_HA_PANEL_LOVELACE


_LOVELACE_PANEL_STAGES: tuple[tuple[str, str], ...] = (
    ("home-assistant", _STAGE_HOME_ASSISTANT),
    ("home-assistant-main", _STAGE_HOME_ASSISTANT_MAIN),
    ("ha-panel-lovelace", _STAGE_HA_PANEL_LOVELACE),
)


# Shared fail-fast predicate for lovelace staged waits: trips when HA has
# rendered a terminal error element (``_LOVELACE_ERROR_SELECTOR``) anywhere
# in the shadow DOM, so a wait aborts with a captured DOM instead of
# burning the full overall budget on a dead panel.
#
# ``hass-error-screen`` was missing here until 2026-08-26, and its absence
# *was* the chronic Flaky Test Detection failure: HA uses that element (not
# ``ha-panel-error``) when a panel's lazy module import rejects, so every
# occurrence polled a permanently-broken document for the full 75s and then
# reported "pass_check not satisfied" — a timeout message for a page that
# had failed a second after loading.
_LOVELACE_FAIL_CHECK = _js("""() => {
    __DEEP__
    return deepPresent(document, '__ERRORS__');
}""")


# Per-stage timeout cap in milliseconds.  ``None`` means "use the full
# remaining overall budget" — used for the final ``ha-panel-lovelace``
# stage, where post-navigation retries must be allowed to consume any
# leftover overall budget rather than being re-capped at a per-stage
# limit.  Stages 1 and 2 retain the 30s cap: they only check shadow-
# root attachment (synchronous as soon as HA's JS evaluates), so a
# 30s budget catches catastrophic failures without masking slow-panel
# scenarios that only manifest in stage 3.
_LOVELACE_STAGE_TIMEOUTS_MS: dict[str, int | None] = {
    "home-assistant": 30000,
    "home-assistant-main": 30000,
    "ha-panel-lovelace": None,
}


def _wait_for_stage(
    page: Any,
    stage_name: str,
    predicate: str,
    deadline: float,
    max_stage_ms: int | None = 30000,
) -> None:
    """Wait for one lovelace DOM milestone, failing fast on HA error states.

    Per-call budget is ``min(remaining_overall, max_stage_ms)``, or the
    full remaining overall budget when ``max_stage_ms is None`` (the final
    stage must not be re-capped — its post-navigation retries must be
    allowed to consume any leftover overall budget).  See
    ``test_retry_after_midstage_nav_uses_remaining_overall_budget`` for the
    concrete reproduction.

    Delegates to ``wait_for_condition``, which polls the stage predicate,
    aborts early if the shared HA-error ``fail_check`` trips, retries on
    Playwright context-destroyed navigation churn, and captures the DOM
    (HTML + screenshot + summary) before raising ``E2EConditionTimeout`` /
    ``E2EConditionFailed`` on failure.  The stage name is embedded in the
    description, so both the capture filename and the CI log identify the
    stuck stage.

    A tripped ``fail_check`` is re-raised as ``E2EPanelLoadError`` so the
    caller can tell "this document is terminally broken, re-navigate" apart
    from "not ready yet, keep waiting".
    """
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        raise E2EConditionTimeout(
            f"lovelace stage {stage_name!r}: overall deadline exceeded "
            f"before stage could start"
        )
    stage_ms = remaining_ms if max_stage_ms is None else min(remaining_ms, max_stage_ms)
    try:
        wait_for_condition(
            page,
            predicate,
            timeout_ms=stage_ms,
            fail_check=_LOVELACE_FAIL_CHECK,
            description=f"lovelace-stage:{stage_name}",
        )
    except E2EPanelLoadError:
        raise
    except E2EConditionFailed as exc:
        raise E2EPanelLoadError(str(exc)) from exc


def _wait_for_lovelace_panel(page: Any, timeout_ms: int = 75000) -> None:
    """Wait for the Lovelace panel to render via staged DOM milestones.

    Stages (each bounded by its own wait_for_function):
      1. ``home-assistant`` element exists and has shadowRoot (30s cap).
      2. ``home-assistant-main`` exists inside that shadowRoot and has
         its own shadowRoot (30s cap).
      3. ``ha-panel-lovelace`` is mounted, HA's WS session is connected,
         and ``hui-root`` is mounted inside ``panel.shadowRoot`` (proves
         the panel completed at least one render cycle).  Uses full
         remaining overall budget — post-navigation retries must not
         be re-capped.

    Each stage retries on Playwright "Execution context was destroyed"
    errors (HA navigation churn — WS reconnect, dashboard router
    refresh, sidebar load).  Pattern mirrors ``tests/e2e/test_ui.py
    ::_find_card`` (commit aa25b10).

    **Budget**: default 75s is the upper bound on slow-shard HA-boot
    time, justified by observed test timings (run 24872997253, gw2
    shard 12): setup failed at 40.3s with the old 30s cap, yet other
    tests on the same shard ran 90.9s proving the container was merely
    slow to bootstrap.  A total of 75s allows HA's custom-element
    registry and dashboard router to finish on the slowest runners
    without masking genuine hangs.

    **Stage-3 stability signal**: the ha-panel-lovelace predicate
    settles on two synchronous DOM facts — ``ha-panel-lovelace``
    attached inside the main shadow root, and ``hui-root`` mounted
    inside ``panel.shadowRoot``.  It does NOT gate on live JS
    properties (``panel.hass``, ``main.hass.connected``) because
    those can transiently flip under CI churn:
      - ``panel.hass`` flaps during the partial-panel-resolver wire-up
        race (run 24956110840, cloud variant, 74958ms timeout).
      - ``main.hass.connected`` flaps during entity-mode's heavier
        state-change bursts (test_time_picker_stays_open_during_rerender
        [entity], 74951ms timeout in Flaky Test Detection).
    ``hui-root`` is strictly stronger than either: the Lit render
    that produced it required both properties to be true at render
    time, so its DOM presence is proof of the wired-and-connected
    state without being vulnerable to the runtime flap.  See
    ``TestWaitForLovelacePanelCloudVariantSignalStability`` and
    ``TestWaitForLovelacePanelEntityModeInitRace`` for regression
    tests.

    Raises ``E2EConditionTimeout`` if any stage fails within its bounded
    budget (stage name appears in the error message), or
    ``E2EPanelLoadError`` if HA has rendered a terminal error element —
    in which case waiting cannot help and the caller must re-navigate
    (see ``open_lovelace_dashboard``).  Unrelated Playwright errors
    propagate unchanged.
    """
    deadline = time.monotonic() + timeout_ms / 1000

    for stage_name, predicate in _LOVELACE_PANEL_STAGES:
        stage_start = time.monotonic()
        # Per-stage cap: ``None`` means "use full remaining budget".
        # Default 30s for any stage not explicitly listed (defensive).
        stage_cap_ms = _LOVELACE_STAGE_TIMEOUTS_MS.get(stage_name, 30000)
        _wait_for_stage(
            page,
            stage_name,
            predicate,
            deadline,
            max_stage_ms=stage_cap_ms,
        )
        stage_dur = time.monotonic() - stage_start
        _log.debug(
            "[%s] lovelace panel stage '%s' ready in %.1fs",
            _worker_id(),
            stage_name,
            stage_dur,
        )


# Number of dashboard navigations allowed per page.  Only a
# ``E2EPanelLoadError`` — HA's own "this document is dead" signal — consumes
# one; a slow or hung dashboard still fails on the first attempt.
#
# Why re-navigation rather than a deterministic wait: the panel's module
# import is a ``Promise.all`` over 51 chunk requests.  When one of those
# requests is lost, ``hass-router-page`` catches the rejection, renders
# ``hass-error-screen`` and never re-issues the import.  There is no
# precondition left to wait for — the only thing that will ask for the
# missing chunk again is a new document.  Three is enough for any plausible
# transient (two independent losses in a row is ~1 in 10^4 at the observed
# CI rate) while still failing loudly on a genuinely broken build.
_PANEL_LOAD_ATTEMPTS = 3


def open_lovelace_dashboard(
    page: Any,
    ha_port: int,
    *,
    timeout_ms: int = 75000,
    attempts: int = _PANEL_LOAD_ATTEMPTS,
) -> None:
    """Navigate ``page`` to the dashboard and wait for a rendered panel.

    Recovers from HA's terminal panel-load error (``E2EPanelLoadError``) by
    re-navigating, bounded to ``attempts`` navigations in total.  Any other
    failure — including ``E2EConditionTimeout`` — propagates on the first
    attempt: this is recovery from an identified terminal state, not a
    blind retry.
    """
    for attempt in range(1, attempts + 1):
        page.goto(f"http://localhost:{ha_port}/lovelace/0", timeout=60000)
        page.wait_for_url("**/lovelace/**", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)
        try:
            # Staged DOM milestones, each with its own bounded budget and
            # retry on context-destroyed errors (HA navigation churn under
            # CI load).  The 75s overall cap is justified in the helper
            # docstring: slow GH-runners can spend 60s+ legitimately
            # booting HA's custom-element registry.
            _wait_for_lovelace_panel(page, timeout_ms=timeout_ms)
        except E2EPanelLoadError as exc:
            if attempt == attempts:
                raise
            _log.warning(
                "[%s] HA rendered a terminal panel-load error on attempt "
                "%d/%d — re-navigating. %s",
                _worker_id(),
                attempt,
                attempts,
                exc,
            )
            continue
        return


@pytest.fixture
def page(
    browser_context: BrowserContext,
    ha_port: int,
    ha_e2e: HAClient,  # noqa: ARG001 — ensure container is up
) -> Generator[Page, None, None]:
    """Function-scoped page navigated to HA dashboard."""
    t0 = time.monotonic()
    p = browser_context.new_page()
    # Attach before navigating: the console error and the failed request
    # that explain a panel-load failure both happen during page load.
    attach_page_diagnostics(p)
    open_lovelace_dashboard(p, ha_port)
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
