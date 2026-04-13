"""Verify vendored smart_battery copies are identical to the canonical source.

The canonical source lives at ``smart_battery/`` in the repo root.
Each brand integration vendors its own copy under
``custom_components/<brand>/smart_battery/``.  This test fails if any
copy has drifted, telling you exactly which files differ.

To fix: copy the canonical source into the integration directory, e.g.
    rm -rf custom_components/foxess_control/smart_battery
    cp -r smart_battery custom_components/foxess_control/smart_battery
"""

from __future__ import annotations

import filecmp
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = REPO_ROOT / "smart_battery"

# Every integration that vendors smart_battery
VENDORED_COPIES = [
    REPO_ROOT / "custom_components" / "foxess_control" / "smart_battery",
    REPO_ROOT / "custom_components" / "goodwe_battery_control" / "smart_battery",
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
