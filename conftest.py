"""Root conftest: pytest-xdist auto worker count based on CPU and RAM.

Worker budget: 1 core and 6 GB RAM per worker for unit tests.
E2E tests (which start Podman containers + Playwright browsers) are
capped at 2 workers to avoid overwhelming the machine.
"""

from __future__ import annotations

import os
from typing import Any


def _get_memory_gb() -> float:
    """Return total physical memory in GiB (POSIX)."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (pages * page_size) / (1024**3)
    except (ValueError, OSError):
        return 8.0  # conservative fallback


_E2E_MAX_WORKERS = 2


def pytest_xdist_auto_num_workers(config: Any) -> int:
    """Select worker count: 1 core and 6 GB RAM per worker.

    E2E tests (collecting from ``e2e/``) are capped at 2 workers
    because each worker starts a Podman container, simulator process,
    and Chromium browser — far heavier than unit test workers.
    """
    cpus = os.cpu_count() or 1
    mem_gb = _get_memory_gb()
    by_memory = int(mem_gb / 6)
    workers = max(1, min(cpus, by_memory))

    # Detect E2E runs by checking invocation args for e2e paths
    args = config.invocation_params.args
    if any("e2e" in str(a) for a in args):
        workers = min(workers, _E2E_MAX_WORKERS)

    return workers
