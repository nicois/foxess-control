"""Two concurrent pytest runs on one host must not destroy each other.

Symptom
-------
Two simultaneous E2E runs on the same machine (two agents, two shells,
or a soak run alongside an E2E run) turn an isolated-green suite into a
storm of unattributable failures.  The signature failure is at *fixture
setup*::

    ERROR at setup of TestDataSource.test_api_source_when_idle[cloud]
    E   TimeoutError: HA did not become ready within 120s

Reproduced directly on 2026-08-26 by running the same two-test E2E
subset from two working directories at once (run B started 5s after run
A): run A returned ``1 passed, 1 skipped, 2 errors`` with both errors
``TimeoutError: HA did not become ready within 120s`` and a 121.9s
setup, while run B returned ``2 passed, 2 skipped``.

Root cause
----------
Two independent host-level resources are derived without any
per-checkout or per-run qualifier:

1. **Container name.**  ``tests/e2e/conftest.py::_container_name()``
   returned ``ha-e2e-{PYTEST_XDIST_WORKER}``, which is identical in
   every concurrent run — ``ha-e2e-gw0`` in both.  Because the
   ``ha_e2e`` fixture calls ``_stop_container(name)`` at *setup* as a
   hygiene step, the second run to start a test issues ``podman rm -f``
   against the *first* run's live container.  The first run's HAClient
   then polls a dead container until its 120s budget expires.

2. **Host port.**  ``_find_free_port()`` binds port 0, reads the
   assigned port and *closes the socket* before ``podman run -p`` gets
   a chance to bind it.  Between the probe and the publish, a
   concurrent run's probe can be handed the very same port.  Measured
   on this host: 6 concurrent processes drawing 50 ports each produced
   duplicate ports in 5 of 6 rounds.

Invariants encoded here
-----------------------
* Names differ across checkouts, across xdist workers, and across two
  *simultaneous* runs of the same checkout — while staying stable
  within a single run (setup and teardown must agree on the name).
* Leftover-cleanup removes this run's own containers and the leftovers
  of *crashed* runs of this same checkout, and never a container a live
  run may still own.
* Port allocation never returns a port that is bound, and never hands
  the same port to two concurrent processes.

Refs C-031 (no flaky tests — this infrastructure manufactured flakes
that looked like product failures), C-029 (E2E must stay usable),
C-043 (per-checkout, per-run isolation of shared host resources).

These are deliberately *fast* tests: the real proof is the two
concurrent pytest runs described above, which is far too slow to run on
every commit, so the invariants it demonstrates are encoded here at
subprocess speed instead.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Podman's own identifier grammar for --name.
_PODMAN_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _conftest() -> Any:
    from tests.e2e import conftest  # noqa: PLC0415

    return conftest


def _require(attr: str) -> Any:
    """Fetch a conftest helper, failing with a diagnosable message."""
    fn = getattr(_conftest(), attr, None)
    if fn is None:
        pytest.fail(
            f"tests.e2e.conftest.{attr} is not defined. Concurrent pytest "
            f"runs on one host cannot be isolated without it: container "
            f"cleanup has no way to tell its own containers from another "
            f"live run's, so the setup-time hygiene step removes the "
            f"other run's HA container."
        )
    return fn


# ---------------------------------------------------------------------------
# Subprocess snippets — two *separate processes* are the only honest way
# to test "two simultaneous runs", so the concurrency invariants are
# driven through real interpreters rather than a mocked pid.
# ---------------------------------------------------------------------------

_NAME_SNIPPET = (
    "import sys; sys.path.insert(0, {root!r});"
    "from tests.e2e import conftest;"
    "print(conftest._container_name())"
)

# Allocates ports, reports them, then blocks on stdin so the run is still
# *live* — and therefore still holding whatever reservation the allocator
# gives it — while the parent compares every run's ports.  A run that has
# exited has legitimately released its ports, so only concurrently-live
# runs can be asserted against each other.
_HOLD_PORTS_SNIPPET = (
    "import sys, json; sys.path.insert(0, {root!r});"
    "from tests.e2e import conftest;"
    "alloc = getattr(conftest, 'allocate_free_port', None) "
    "or conftest._find_free_port;"
    "print(json.dumps([alloc() for _ in range(50)]), flush=True);"
    "sys.stdin.readline()"
)


def _spawn(snippet: str, *, worker: str | None) -> subprocess.Popen[str]:
    import os  # noqa: PLC0415

    env = dict(os.environ)
    if worker is None:
        env.pop("PYTEST_XDIST_WORKER", None)
    else:
        env["PYTEST_XDIST_WORKER"] = worker
    return subprocess.Popen(
        [sys.executable, "-c", snippet.format(root=str(REPO_ROOT))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _collect(proc: subprocess.Popen[str]) -> str:
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, f"helper subprocess failed:\n{err}"
    return out.strip()


class TestContainerNamePerCheckoutAndRun:
    """``_container_name()`` must be unique per (checkout, run, worker)."""

    def test_two_simultaneous_runs_of_one_checkout_differ(self) -> None:
        """THE bug: two concurrent runs picked the identical name.

        Same checkout, same xdist worker id, two live processes — exactly
        the situation two agents sharing this host produce.  Before the
        fix both processes print ``ha-e2e-gw0``, so whichever starts a
        test second removes the other's container.
        """
        procs = [_spawn(_NAME_SNIPPET, worker="gw0") for _ in range(2)]
        names = [_collect(p) for p in procs]
        assert names[0] != names[1], (
            f"two simultaneous runs of the same checkout both chose "
            f"{names[0]!r} — the second run's setup-time _stop_container() "
            f"will destroy the first run's live HA container"
        )

    def test_many_simultaneous_runs_all_differ(self) -> None:
        """Eight concurrent runs on one worker id → eight distinct names."""
        procs = [_spawn(_NAME_SNIPPET, worker="gw3") for _ in range(8)]
        names = [_collect(p) for p in procs]
        dupes = {n for n, c in Counter(names).items() if c > 1}
        assert not dupes, f"duplicate container names across runs: {dupes}"

    def test_different_checkouts_differ(self, monkeypatch: Any) -> None:
        """Two worktree paths → two different names.

        The name must be a function of the checkout, so a run in
        ``/repo`` and a run in ``/repo/.claude/worktrees/agent-x`` never
        collide even if their pids were somehow equal.
        """
        conftest = _conftest()
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        monkeypatch.setattr(conftest, "REPO_ROOT", Path("/checkout/alpha"))
        first = conftest._container_name()
        monkeypatch.setattr(conftest, "REPO_ROOT", Path("/checkout/beta"))
        second = conftest._container_name()
        assert first != second, (
            f"both checkouts chose {first!r} — a run in each would fight "
            f"over the same container"
        )

    def test_different_xdist_workers_differ(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        first = _conftest()._container_name()
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
        second = _conftest()._container_name()
        assert first != second

    def test_many_workers_in_one_run_all_differ(self, monkeypatch: Any) -> None:
        """A 32-worker run needs 32 distinct names."""
        names = []
        for i in range(32):
            monkeypatch.setenv("PYTEST_XDIST_WORKER", f"gw{i}")
            names.append(_conftest()._container_name())
        assert len(set(names)) == 32, f"collisions among worker names: {names}"

    def test_serial_run_without_xdist_worker_is_valid(self, monkeypatch: Any) -> None:
        """``PYTEST_XDIST_WORKER`` absent (serial run) still yields a name."""
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        name = _conftest()._container_name()
        assert _PODMAN_NAME_RE.match(name), f"invalid podman name: {name!r}"

    def test_single_worker_run_is_valid(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        name = _conftest()._container_name()
        assert _PODMAN_NAME_RE.match(name), f"invalid podman name: {name!r}"
        assert len(name) <= 63, f"container name too long for podman: {name!r}"

    def test_name_is_stable_within_a_run(self, monkeypatch: Any) -> None:
        """Repeated calls in one process must agree.

        ``ha_e2e`` computes the name at setup and uses it again for
        ``podman logs`` and teardown, so a wall-clock or counter
        component would leak containers.  This is also why a
        timestamp-based scheme is not merely collision-prone but wrong:
        the name has to be reproducible for the whole run.
        """
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        conftest = _conftest()
        assert conftest._container_name() == conftest._container_name()


def _foreign_checkout_name(worker: str = "gw0") -> str:
    """A container name owned by a *live* run of a different checkout.

    Same (live) pid as this process, different checkout path — so the
    only thing that can exclude it from cleanup is the checkout
    qualifier.
    """
    conftest = _conftest()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PYTEST_XDIST_WORKER", worker)
        mp.setattr(conftest, "REPO_ROOT", Path("/checkout/other"))
        return str(conftest._container_name())


def _crashed_run_name(worker: str = "gw0") -> str:
    """A name left behind by a *dead* run of this same checkout.

    Derived by asking a real subprocess for the name it would use and
    then letting it exit, so the test never has to know the name format
    and the owner really is gone — which is exactly the state a crashed
    run leaves ``podman ps -a`` in.
    """
    return _collect(_spawn(_NAME_SNIPPET, worker=worker))


class TestLeftoverCleanupOwnership:
    """Cleanup must reclaim own/crashed leftovers, never a live run's."""

    def test_own_name_is_reclaimable(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        name = _conftest()._container_name()
        assert _require("container_is_reclaimable")(name) is True

    def test_live_run_of_another_checkout_is_never_reclaimable(
        self, monkeypatch: Any
    ) -> None:
        """The invariant whose absence caused the outage."""
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        foreign = _foreign_checkout_name()
        assert _require("container_is_reclaimable")(foreign) is False, (
            f"{foreign!r} belongs to another checkout's run and must not be removed"
        )

    def test_live_sibling_worker_of_same_run_is_not_reclaimable(
        self, monkeypatch: Any
    ) -> None:
        """gw0 must not reclaim gw1's container in its own run.

        Both names carry this (live) process's run identity, so the
        sibling is protected by the liveness check alone.
        """
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
        sibling = _conftest()._container_name()
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        assert _require("container_is_reclaimable")(sibling) is False

    def test_crashed_run_of_this_checkout_is_reclaimable(
        self, monkeypatch: Any
    ) -> None:
        """A leftover from a previous *crashed* run must still be cleaned.

        Otherwise the collision bug is merely traded for a leak: every
        crashed run would strand a container and its published port.
        """
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        leftover = _crashed_run_name(worker="gw7")
        assert _require("container_is_reclaimable")(leftover) is True, (
            f"{leftover!r} was left by a crashed run of this checkout and "
            f"must still be reclaimed"
        )

    def test_crashed_run_of_another_checkout_is_left_alone(
        self, monkeypatch: Any
    ) -> None:
        """Another checkout reclaims its own leftovers; we don't guess.

        A dead-looking pid in another checkout's name may equally be a
        pid this host has recycled, so the safe answer is to leave it.
        """
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        foreign = _foreign_checkout_name()
        reclaimable = _require("container_is_reclaimable")
        assert reclaimable(foreign, pid_is_alive=lambda _pid: False) is False

    @pytest.mark.parametrize(
        "name",
        [
            "aiven-core-agent-crabredir-C1",
            "ha-e2e",
            "ha-e2e-gw0",
            "ha-soak-gw0",
            "ha-e2e-deadbeef-notapid-gw0",
            "",
        ],
    )
    def test_unrecognised_names_are_never_reclaimable(
        self, name: str, monkeypatch: Any
    ) -> None:
        """Unrelated or legacy names must be left strictly alone.

        ``ha-e2e-gw0`` is the *old* unqualified name: an in-flight run of
        the previous code could own it, so a new run must not touch it.
        """
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        assert _require("container_is_reclaimable")(name) is False

    def test_reclaim_removes_exactly_the_owned_and_dead(self, monkeypatch: Any) -> None:
        """End-to-end cleanup decision over a realistic container list.

        Every name here is produced the way the real code produces it —
        no hand-written name formats — and liveness is the real one, so
        this asserts the actual decision a run makes against the
        ``podman ps -a`` output of a busy host.
        """
        conftest = _conftest()
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
        sibling = str(conftest._container_name())
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        own = str(conftest._container_name())
        crashed = _crashed_run_name(worker="gw9")
        foreign = _foreign_checkout_name()

        listed = [
            own,
            sibling,
            crashed,
            foreign,
            "aiven-core-agent-crabredir-C1",
            "ha-e2e-gw0",
        ]
        removed: list[str] = []
        _require("reclaim_stale_containers")(
            list_names=lambda: listed,
            remove=removed.append,
        )
        assert sorted(removed) == sorted([own, crashed]), (
            f"reclaim removed {removed!r}; expected only this run's own "
            f"container ({own!r}) and the crashed run's leftover "
            f"({crashed!r})"
        )


class TestSoakAndE2ECoexist:
    """``tests/soak/`` names containers on the same pattern.

    A soak run and an E2E run on one host must be able to overlap, which
    is how the ``121 passed, 34 skipped, 3 errors`` report was produced.
    """

    def test_soak_and_e2e_names_differ(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        conftest = _conftest()
        e2e = conftest._container_name()
        soak = conftest._container_name(prefix="ha-soak")
        assert e2e != soak

    def test_soak_names_carry_the_same_isolation(self, monkeypatch: Any) -> None:
        """Checkout + run + worker qualifiers, exactly as for E2E."""
        conftest = _conftest()
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(conftest, "REPO_ROOT", Path("/checkout/alpha"))
            alpha = conftest._container_name(prefix="ha-soak")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(conftest, "REPO_ROOT", Path("/checkout/beta"))
            beta = conftest._container_name(prefix="ha-soak")
        assert alpha != beta
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
        assert conftest._container_name(prefix="ha-soak") != alpha

    def test_soak_conftest_uses_the_shared_helper(self) -> None:
        """No second, unqualified naming scheme may exist in soak."""
        source = (REPO_ROOT / "tests" / "soak" / "conftest.py").read_text()
        assert 'f"ha-soak-{' not in source, (
            "tests/soak/conftest.py still builds its container name "
            "inline; it must use the shared _container_name() helper so "
            "the isolation rules apply there too"
        )

    def test_reclaim_covers_the_soak_prefix(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        conftest = _conftest()
        own_soak = str(conftest._container_name(prefix="ha-soak"))
        removed: list[str] = []
        _require("reclaim_stale_containers")(
            list_names=lambda: [own_soak],
            remove=removed.append,
        )
        assert removed == [own_soak]


class TestPortAllocationCollisionProof:
    """Published host ports must not be handed to two runs at once."""

    def test_never_returns_a_bound_port(self) -> None:
        """Exercise the real allocator against a really-bound port."""
        alloc = _require("allocate_free_port")
        with socket.socket() as held:
            held.bind(("", 0))
            held.listen(1)
            taken = held.getsockname()[1]
            got = [alloc() for _ in range(20)]
        assert taken not in got, f"allocator returned bound port {taken}"

    def test_repeated_allocation_is_distinct(self) -> None:
        alloc = _require("allocate_free_port")
        got = [alloc() for _ in range(20)]
        assert len(set(got)) == len(got), f"allocator repeated a port: {got}"

    def test_claim_is_exclusive_while_owner_lives(self) -> None:
        """A claimed-but-not-yet-bound port must not be re-issued.

        This is the window the old probe-and-release allocator left open:
        ``podman run -p`` binds seconds after the probe closed its
        socket, so a concurrent probe is free to pick the same port.
        """
        claim = _require("claim_port")
        alloc = _require("allocate_free_port")
        port = alloc()
        assert claim(port) is False, (
            f"port {port} is already claimed by this run but claim_port "
            f"handed it out again"
        )

    def test_claim_reaps_a_dead_owners_claim(self) -> None:
        """A crashed run's claim must not sterilise the port forever."""
        claim = _require("claim_port")
        alloc = _require("allocate_free_port")
        port = alloc()
        assert claim(port, pid_is_alive=lambda _pid: False) is True

    def test_concurrent_live_runs_never_share_a_port(self) -> None:
        """Six simultaneous runs drawing 50 ports each, four times over.

        All six stay alive — and keep holding their ports — until every
        run has reported, so this asserts the property that matters: no
        two runs that overlap in time are given the same host port.

        Pre-fix measurement on this host: duplicates appeared in 5 of 6
        rounds at this volume, so a clean sweep of four rounds is a
        strong signal.  Post-fix it is not a probability at all — the
        host-wide claim registry makes a duplicate impossible.
        """
        for rnd in range(4):
            procs = [_spawn(_HOLD_PORTS_SNIPPET, worker=f"gw{i}") for i in range(6)]
            try:
                ports: list[int] = []
                for p in procs:
                    assert p.stdout is not None
                    line = p.stdout.readline()
                    assert line, f"helper produced no output: {p.stderr}"
                    ports.extend(json.loads(line))
                dupes = {p for p, c in Counter(ports).items() if c > 1}
                assert not dupes, (
                    f"round {rnd}: {len(ports)} ports held simultaneously by "
                    f"6 concurrent runs, but {sorted(dupes)} were handed out "
                    f"more than once — two runs would publish HA on the same "
                    f"host port"
                )
            finally:
                for p in procs:
                    with contextlib.suppress(OSError, ValueError):
                        assert p.stdin is not None
                        p.stdin.write("go\n")
                        p.stdin.close()
                for p in procs:
                    p.wait(timeout=60)
