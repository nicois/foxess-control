"""Root conftest: pytest-xdist auto worker count based on CPU and RAM.

Worker budget: 1 core and 6 GB RAM per worker.
"""

from __future__ import annotations

import os


def _get_memory_gb() -> float:
    """Return total physical memory in GiB (POSIX)."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (pages * page_size) / (1024**3)
    except (ValueError, OSError):
        return 8.0  # conservative fallback


def pytest_xdist_auto_num_workers(config: object) -> int:
    """Select worker count: 1 core and 6 GB RAM per worker."""
    cpus = os.cpu_count() or 1
    mem_gb = _get_memory_gb()
    by_memory = int(mem_gb / 4)
    return max(1, min(cpus, by_memory))
