"""Cross-test isolation guard: pytest-playwright must not leak a running
asyncio loop into subsequent ``@pytest.mark.asyncio`` tests on the same
xdist worker.

Background
----------
``pytest-playwright``'s ``page`` fixture ultimately calls
``playwright.sync_api.sync_playwright().start()``.  Playwright's sync API
is a greenlet-based shim over the async API: the underlying asyncio event
loop **remains ``running``** on the main thread for the entire lifetime
of the sync ``Playwright`` handle, only stopping when ``.stop()`` is
called.

``pytest-playwright`` declares its ``playwright`` fixture with
``scope="session"``, so ``.stop()`` is deferred to end-of-session.  Every
``@pytest.mark.asyncio`` test that runs AFTER a ``page``-using test on
the same xdist worker therefore hits
``asyncio.Runner.run()``'s ``_check_running()`` guard and fails with::

    RuntimeError: Runner.run() cannot be called from a running event loop

(or, on the teardown path during shutdown of asyncgens::

    RuntimeError: Cannot run the event loop while another loop is running)

The symptom is intermittent because ``pytest-randomly`` + ``pytest-xdist``
only place a ``page``-using test before an asyncio test on the same
worker in roughly half of random orderings.  Tests fail only when a
worker happens to be unlucky.

The fix overrides the ``playwright`` fixture chain at
``tests/conftest.py`` with ``scope="function"`` so each unit test that
uses ``page`` starts a fresh ``sync_playwright`` and stops it on
teardown — releasing the loop before any other test runs on the
worker.  A counter-override in ``tests/e2e/conftest.py`` re-declares
the same fixtures at session scope, so the E2E ``browser_context``
(session-scoped) fixture chain is unaffected.

This test runs the minimal two-test reproduction in a subprocess under
``-p no:randomly`` so the run order is deterministic:

1. A test that requests the ``page`` fixture (uses pytest-playwright).
2. A ``@pytest.mark.asyncio`` test that would fail if a loop were
   left running.

Before the fix: step 2 fails with ``RuntimeError``.
After the fix: step 2 passes.

Constraint: C-031 (no flaky tests — fix the root cause, do not
skip / xfail / tune params).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_OVERRIDE_MARKER_START = "# === PLAYWRIGHT ISOLATION OVERRIDE START ==="
_OVERRIDE_MARKER_END = "# === PLAYWRIGHT ISOLATION OVERRIDE END ==="


def _extract_override_block() -> str | None:
    """Return the playwright-override snippet from the project conftest,
    or ``None`` if no such block exists (pre-fix state).

    The snippet is kept as a marker-delimited block inside
    ``tests/conftest.py`` precisely so this test can lift it into an
    isolated sandbox without also dragging in the rest of
    ``tests/conftest.py`` (which imports the simulator and HA stubs
    that we do not want running inside the repro subprocess).
    """
    conftest = Path(__file__).resolve().parent / "conftest.py"
    src = conftest.read_text(encoding="utf-8")
    if _OVERRIDE_MARKER_START not in src or _OVERRIDE_MARKER_END not in src:
        return None
    return src.split(_OVERRIDE_MARKER_START, 1)[1].split(_OVERRIDE_MARKER_END, 1)[0]


def test_pytest_playwright_does_not_leak_running_loop(tmp_path: Path) -> None:
    """Deterministic reproduction of the cross-test isolation flake.

    Runs two tests in a subprocess, ordered alphabetically so that
    ``test_a_uses_page`` runs first and ``test_b_is_asyncio`` runs
    second.  With the session-scoped ``playwright`` fixture intact
    (no override in ``tests/conftest.py``) the subprocess exits with
    the exact production symptom:

        RuntimeError: Runner.run() cannot be called from a running event loop

    With the function-scoped override in ``tests/conftest.py``,
    pytest-playwright's loop is stopped at teardown of
    ``test_a_uses_page`` and ``test_b_is_asyncio`` passes cleanly.
    """
    # Create an isolated sandbox with no pyproject.toml so the
    # project's pytest config (which pins ``-n auto`` for
    # pytest-xdist) is NOT inherited.  Determinism requires the two
    # tests to run serially on the same process.
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "pytest.ini").write_text(
        textwrap.dedent(
            """
            [pytest]
            asyncio_mode = strict
            """
        ).strip()
        + "\n"
    )

    # Copy the override block (if present in tests/conftest.py) into
    # the sandbox conftest.  If the marker block is absent, the
    # subprocess runs against stock pytest-playwright and the repro
    # MUST fail — which is what we assert-not after the fix.
    override = _extract_override_block()
    conftest_preamble = textwrap.dedent(
        """
        from __future__ import annotations

        from collections.abc import Generator
        from typing import TYPE_CHECKING, Any

        import pytest
        """
    ).lstrip()
    if override is not None:
        (sandbox / "conftest.py").write_text(conftest_preamble + override)
    else:
        (sandbox / "conftest.py").write_text("")

    (sandbox / "test_repro.py").write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import pytest


            def test_a_uses_page(page) -> None:
                # Any trivial use of the page fixture is enough to
                # trigger sync_playwright().start() and leave the
                # greenlet event loop running on the main thread.
                page.set_content("<p>hello</p>")


            @pytest.mark.asyncio
            async def test_b_is_asyncio() -> None:
                # This test must succeed.  It will fail with
                # "Runner.run() cannot be called from a running event loop"
                # if the previous test's sync_playwright context has not
                # been torn down.
                assert True
            """
        ).strip()
        + "\n"
    )

    # Ensure the sandbox is not under any parent directory that has a
    # pyproject.toml — ``tmp_path`` is under /tmp so we're safe, but
    # defend against a surprising future layout with an explicit check.
    assert not any((p / "pyproject.toml").exists() for p in sandbox.parents), (
        "Sandbox must not inherit a parent pyproject.toml that pins "
        "pytest-xdist flags — repro would not be deterministic."
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_repro.py",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-v",
            "--tb=short",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=sandbox,
        timeout=120,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        "test_b_is_asyncio failed — pytest-playwright's session-scoped "
        "`playwright` fixture leaked a running event loop into the "
        "subsequent asyncio test.\n"
        f"\n=== stdout ===\n{result.stdout}\n=== stderr ===\n{result.stderr}"
    )
    # Belt-and-braces: the exact production error strings must NOT
    # appear in a successful-run output.
    assert "cannot be called from a running event loop" not in combined, (
        "Symptom string leaked into subprocess output — fix is not in place.\n"
        f"\n=== combined ===\n{combined}"
    )
    assert "Cannot run the event loop while another loop is running" not in combined, (
        "Second-flavour symptom leaked into subprocess output.\n"
        f"\n=== combined ===\n{combined}"
    )
