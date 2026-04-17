"""Structural tests for smart_battery/.

Covers vendored copy sync, import boundary, and sync invariants.

The canonical source lives at ``smart_battery/`` in the repo root.
Each brand integration vendors its own copy under
``custom_components/<brand>/smart_battery/``.  This test fails if any
copy has drifted, telling you exactly which files differ.

To fix: copy the canonical source into the integration directory, e.g.
    rm -rf custom_components/foxess_control/smart_battery
    cp -r smart_battery custom_components/foxess_control/smart_battery
"""

from __future__ import annotations

import ast
import filecmp
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = REPO_ROOT / "smart_battery"

# Every integration that vendors smart_battery
VENDORED_COPIES = [
    REPO_ROOT / "custom_components" / "foxess_control" / "smart_battery",
]


def _py_files(directory: Path) -> set[str]:
    """Return relative .py file paths within *directory*."""
    return {
        str(p.relative_to(directory))
        for p in directory.rglob("*.py")
        if "__pycache__" not in p.parts
    }


@pytest.mark.parametrize(
    "vendored",
    VENDORED_COPIES,
    ids=[str(p.relative_to(REPO_ROOT)) for p in VENDORED_COPIES],
)
def test_vendored_copy_matches_canonical(vendored: Path) -> None:
    assert CANONICAL.is_dir(), f"Canonical source missing: {CANONICAL}"
    assert vendored.is_dir(), f"Vendored copy missing: {vendored}"

    canonical_files = _py_files(CANONICAL)
    vendored_files = _py_files(vendored)

    only_in_canonical = canonical_files - vendored_files
    only_in_vendored = vendored_files - canonical_files
    assert not only_in_canonical, (
        f"Files in canonical but missing from {vendored.relative_to(REPO_ROOT)}: "
        f"{sorted(only_in_canonical)}"
    )
    assert not only_in_vendored, (
        f"Files in {vendored.relative_to(REPO_ROOT)} but missing from canonical: "
        f"{sorted(only_in_vendored)}"
    )

    mismatched: list[str] = []
    for rel in sorted(canonical_files):
        if not filecmp.cmp(str(CANONICAL / rel), str(vendored / rel), shallow=False):
            mismatched.append(rel)

    assert not mismatched, (
        f"Files differ between canonical and "
        f"{vendored.relative_to(REPO_ROOT)}:\n"
        + "\n".join(f"  {f}" for f in mismatched)
        + "\n\nTo fix: rm -rf "
        + str(vendored.relative_to(REPO_ROOT))
        + " && cp -r smart_battery "
        + str(vendored.relative_to(REPO_ROOT))
    )


# ---------------------------------------------------------------------------
# Brand packages that smart_battery/ must never import from (C-021)
# ---------------------------------------------------------------------------
BRAND_PACKAGES = {"foxess", "foxess_control"}


def test_smart_battery_has_no_brand_imports() -> None:
    """C-021: smart_battery/ must not import from any brand-specific package."""
    violations: list[str] = []

    for py_file in sorted(CANONICAL.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))

        rel = py_file.relative_to(CANONICAL)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in BRAND_PACKAGES:
                        violations.append(f"{rel}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue  # relative import with no module (e.g. from . import x)
                top = node.module.split(".")[0]
                if top in BRAND_PACKAGES:
                    violations.append(
                        f"{rel}:{node.lineno} from {node.module} import ..."
                    )

    assert not violations, (
        "smart_battery/ imports brand-specific packages:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Sync cancel invariant (C-016)
# ---------------------------------------------------------------------------

_SESSION_PY = CANONICAL / "session.py"
_LISTENERS_PY = CANONICAL / "listeners.py"


def _find_funcdef(
    tree: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the top-level function/method definition with *name*, or None."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ):
            return node
    return None


def test_cancel_smart_session_is_synchronous() -> None:
    """C-016: cancel_smart_session must be sync.

    No awaits allowed between unsub and state clear.
    """
    source = _SESSION_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SESSION_PY))

    func = _find_funcdef(tree, "cancel_smart_session")
    assert func is not None, "cancel_smart_session not found in session.py"

    # Must be a plain def, not async def
    assert isinstance(func, ast.FunctionDef), (
        f"cancel_smart_session is {type(func).__name__} — expected FunctionDef (sync)"
    )

    # A sync function cannot contain Await nodes, but verify explicitly
    awaits = [node for node in ast.walk(func) if isinstance(node, ast.Await)]
    assert not awaits, (
        f"cancel_smart_session contains {len(awaits)} Await node(s) — "
        "must be fully synchronous to prevent races between unsub and state clear"
    )

    # Also verify the callers in listeners.py are sync
    listeners_source = _LISTENERS_PY.read_text(encoding="utf-8")
    listeners_tree = ast.parse(listeners_source, filename=str(_LISTENERS_PY))

    for caller_name in ("cancel_smart_charge", "cancel_smart_discharge"):
        caller = _find_funcdef(listeners_tree, caller_name)
        assert caller is not None, f"{caller_name} not found in listeners.py"
        assert isinstance(caller, ast.FunctionDef), (
            f"{caller_name} is {type(caller).__name__} — expected FunctionDef (sync)"
        )
