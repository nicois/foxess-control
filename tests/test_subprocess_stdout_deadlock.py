"""Test that subprocess stdout pipe handling avoids deadlocks.

Reproduces the CI flake where the HA container blocks on stdout writes
because nobody is draining the pipe.  The default Linux pipe buffer is
64 KiB; if a subprocess fills it without a reader, the process blocks
and never finishes starting.

The fix: use ``subprocess.DEVNULL`` for stdout/stderr when the output
is not needed inline (container logs are captured via ``podman logs``
in teardown).

Root cause:  ``conftest.py::ha_e2e`` started the container with
``stdout=subprocess.PIPE`` but never read from the pipe during the
``wait_ready`` phase.  Under CI load (12 xdist workers), the HA
container produced enough startup logs to fill the 64 KiB pipe buffer,
blocking the container process and preventing HA from ever listening
on its HTTP port.  ``wait_ready`` timed out after 120 s because it
was waiting for a process that could never finish starting.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path

import pytest

_E2E_CONFTEST = Path(__file__).resolve().parent / "e2e" / "conftest.py"


class TestPipeDeadlockMechanism:
    """Demonstrate the pipe buffer deadlock that caused the CI flake."""

    def test_pipe_blocks_when_buffer_full(self) -> None:
        """A subprocess that outputs >64 KiB to a PIPE blocks if unread.

        This demonstrates the root cause: the HA container writes
        startup logs to stdout, the 64 KiB pipe buffer fills, and the
        container blocks — preventing HA from starting within the
        120 s ``wait_ready`` timeout.
        """
        # Produce 128 KiB of output — well above the 64 KiB pipe buffer.
        script = (
            "import sys; sys.stdout.write('x' * 131072); sys.stdout.flush(); "
            "import time; time.sleep(0)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # Give the process time to fill the buffer and block.
        # If the pipe blocks, poll() returns None (still running).
        time.sleep(2)
        assert proc.poll() is None, (
            "Expected process to be blocked on pipe write, but it already exited"
        )
        # Clean up — read the pipe to unblock, then wait.
        if proc.stdout:
            proc.stdout.read()
        proc.wait(timeout=5)

    def test_devnull_never_blocks(self) -> None:
        """A subprocess with DEVNULL stdout completes immediately."""
        script = "import sys; sys.stdout.write('x' * 131072); sys.stdout.flush()"
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=5)
        assert proc.returncode == 0


class TestConftestStdoutHandling:
    """Verify the E2E conftest does not use undrained PIPE for containers.

    A static check: parse conftest.py and verify that subprocess.Popen
    calls in long-running fixture functions do not use ``stdout=PIPE``
    without a corresponding drain thread.  This catches regressions if
    someone re-introduces PIPE without draining.
    """

    @pytest.mark.parametrize("func_name", ["ha_e2e", "foxess_sim"])
    def test_fixture_popen_does_not_use_pipe(self, func_name: str) -> None:
        """Popen in E2E fixtures must not use stdout=PIPE.

        Using PIPE without a drain thread causes a deadlock when the
        subprocess fills the 64 KiB pipe buffer.  Container logs are
        captured via ``podman logs`` in teardown; simulator output is
        not needed.
        """
        source = _E2E_CONFTEST.read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name != func_name:
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                is_popen = (
                    isinstance(func, ast.Attribute) and func.attr == "Popen"
                ) or (isinstance(func, ast.Name) and func.id == "Popen")
                if not is_popen:
                    continue
                for kw in child.keywords:
                    if kw.arg != "stdout":
                        continue
                    if isinstance(kw.value, ast.Attribute):
                        assert kw.value.attr != "PIPE", (
                            f"{func_name} uses stdout=subprocess.PIPE "
                            "which causes a pipe buffer deadlock under "
                            "CI load. Use subprocess.DEVNULL instead."
                        )
                    elif isinstance(kw.value, ast.Name):
                        assert kw.value.id != "PIPE", (
                            f"{func_name} uses stdout=PIPE which causes "
                            "a pipe buffer deadlock under CI load."
                        )
