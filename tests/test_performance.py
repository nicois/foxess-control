"""Performance regression tests.

Guards against accidental complexity in hot-path algorithms and ensures
no synchronous I/O calls appear in async functions.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from smart_battery.algorithms import calculate_charge_power, calculate_discharge_power
from smart_battery.taper import TaperProfile


class TestAlgorithmPerformance:
    """Core algorithm calculations must stay well under 10ms."""

    @pytest.mark.parametrize(
        "soc,target,remaining_h",
        [
            (20, 80, 4.0),
            (10, 100, 2.0),
            (50, 80, 0.5),
            (95, 100, 6.0),
        ],
    )
    def test_charge_power_calculation_time(
        self, soc: float, target: int, remaining_h: float
    ) -> None:
        iterations = 1000
        start = time.perf_counter()
        for _ in range(iterations):
            calculate_charge_power(
                current_soc=soc,
                target_soc=target,
                battery_capacity_kwh=60.0,
                remaining_hours=remaining_h,
                max_power_w=10500,
                net_consumption_kw=1.5,
                headroom=0.10,
            )
        elapsed_ms = (time.perf_counter() - start) * 1000
        avg_ms = elapsed_ms / iterations
        assert avg_ms < 10, (
            f"calculate_charge_power averaged {avg_ms:.3f}ms (limit 10ms)"
        )

    @pytest.mark.parametrize(
        "soc,min_soc,remaining_h,feedin",
        [
            (80, 30, 3.0, None),
            (80, 30, 3.0, 2.5),
            (50, 10, 1.0, None),
            (35, 30, 0.25, 0.5),
        ],
    )
    def test_discharge_power_calculation_time(
        self, soc: float, min_soc: int, remaining_h: float, feedin: float | None
    ) -> None:
        iterations = 1000
        start = time.perf_counter()
        for _ in range(iterations):
            calculate_discharge_power(
                current_soc=soc,
                min_soc=min_soc,
                battery_capacity_kwh=60.0,
                remaining_hours=remaining_h,
                max_power_w=10500,
                net_consumption_kw=1.5,
                headroom=0.10,
                feedin_remaining_kwh=feedin,
                consumption_peak_kw=2.0,
            )
        elapsed_ms = (time.perf_counter() - start) * 1000
        avg_ms = elapsed_ms / iterations
        assert avg_ms < 10, (
            f"calculate_discharge_power averaged {avg_ms:.3f}ms (limit 10ms)"
        )


class TestTaperProfilePerformance:
    """1000 taper observations must complete in under 100ms."""

    def test_record_and_query_performance(self) -> None:
        tp = TaperProfile()
        iterations = 1000

        start = time.perf_counter()
        for i in range(iterations):
            soc = 20 + (i % 80)
            tp.record_charge(soc, 5000, 4500.0 - (soc * 10))
            tp.record_discharge(soc, 5000, 4800.0 - (soc * 5))
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 100, (
            f"1000 taper observations took {elapsed_ms:.1f}ms (limit 100ms)"
        )

        start = time.perf_counter()
        for soc in range(0, 101):
            tp.charge_ratio(float(soc))
            tp.discharge_ratio(float(soc))
        query_ms = (time.perf_counter() - start) * 1000
        assert query_ms < 50, f"202 ratio queries took {query_ms:.1f}ms (limit 50ms)"


_SYNC_IO_CALLS = frozenset(
    {
        "open",
        "read_bytes",
        "read_text",
        "write_bytes",
        "write_text",
    }
)

_SCAN_DIRS = [
    Path("smart_battery"),
    Path("custom_components/foxess_control"),
]


class TestNoSyncIOInAsync:
    """AST scan: async functions must not contain synchronous I/O calls."""

    def _collect_violations(self) -> list[str]:
        violations: list[str] = []
        for scan_dir in _SCAN_DIRS:
            if not scan_dir.exists():
                continue
            for py_file in scan_dir.rglob("*.py"):
                try:
                    tree = ast.parse(py_file.read_text(), filename=str(py_file))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.AsyncFunctionDef):
                        continue
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            func = child.func
                            name = None
                            if isinstance(func, ast.Name):
                                name = func.id
                            elif isinstance(func, ast.Attribute):
                                name = func.attr
                            if name in _SYNC_IO_CALLS:
                                violations.append(
                                    f"{py_file}:{child.lineno}: "
                                    f"sync I/O '{name}()' in async function "
                                    f"'{node.name}'"
                                )
        return violations

    def test_no_sync_io_in_async_paths(self) -> None:
        violations = self._collect_violations()
        assert not violations, "Synchronous I/O in async functions:\n" + "\n".join(
            violations
        )
