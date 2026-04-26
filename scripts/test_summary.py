#!/usr/bin/env python3
"""Authoritative test-count summary, generated on demand.

Replaces the four inline copies (``06-tests.md`` header + end-of-E2E
sentence, ``05-coverage.md`` C-029 row + summary totals, any ad-hoc
commentary) that used to drift between knowledge-tree updates.

Usage::

    python scripts/test_summary.py              # current tree, human-readable
    python scripts/test_summary.py --json       # current tree, JSON
    python scripts/test_summary.py --history    # walk tags, print tsv
    python scripts/test_summary.py --history --since v1.0.11

The historical view uses ``git``: for each annotated ``v*`` tag, the
script checks out a detached worktree (no working-tree side effects)
and runs ``pytest --co -q`` against the tag.  Counts at each tag are
cached at ``.test-count-cache.json`` at the repo root (git-ignored)
so repeat runs are fast.

All counts come from ``pytest --co -q`` — never from static globbing
— so they match the count that CI would see on the same commit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = REPO_ROOT / ".test-count-cache.json"


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout + result.stderr


def _count_collected(output: str) -> int:
    """Count actual test items printed by ``pytest --co -q``.

    The summary footer ("<N> tests collected" / "<N>/<M> collected
    (<D> deselected)") can be misleading under xdist or after marker
    filtering — the N shown there doesn't always match the number of
    lines actually enumerated.  Counting item lines is authoritative
    and matches the number of tests that would run.
    """
    return sum(1 for line in output.splitlines() if line.startswith("tests/"))


def collect_counts(repo: Path = REPO_ROOT) -> dict[str, int]:
    """Return ``{unit, e2e, soak, total}`` for the given repo checkout.

    All counts come from a single ``pytest tests/ --co -q`` invocation
    and are derived by partitioning the test-item list by path prefix.
    Running separate collections per subdir can give different numbers
    because root-level conftests (that deselect invalid parametrize
    combos) only engage when the session rootdir is ``tests/``.
    """
    out = _run(["pytest", "tests/", "--co", "-q"], cwd=repo)
    items = [line for line in out.splitlines() if line.startswith("tests/")]
    e2e = sum(1 for it in items if it.startswith("tests/e2e/"))
    soak = sum(1 for it in items if it.startswith("tests/soak/"))
    total = len(items)
    unit = total - e2e - soak
    return {"unit": unit, "e2e": e2e, "soak": soak, "total": total}


def _load_cache() -> dict[str, dict[str, int]]:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_cache(cache: dict[str, dict[str, int]]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _list_tags(since: str | None) -> list[str]:
    """Return ``v*`` tags in chronological order.

    If ``since`` is given, start from that tag (inclusive).
    """
    out = _run(["git", "tag", "-l", "v*", "--sort=creatordate"], cwd=REPO_ROOT)
    tags = [t for t in out.splitlines() if t.strip()]
    if since:
        if since not in tags:
            msg = f"--since tag '{since}' not found"
            raise SystemExit(msg)
        tags = tags[tags.index(since) :]
    return tags


def _collect_at_tag(tag: str) -> dict[str, int] | None:
    """Check out a detached worktree at ``tag`` and collect counts.

    Returns ``None`` if the tag can't be checked out (e.g. pre-dates
    the test layout).  Worktree is cleaned up unconditionally.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="test-summary-") as tmp:
        try:
            _run(["git", "worktree", "add", "--detach", tmp, tag], cwd=REPO_ROOT)
            counts = collect_counts(Path(tmp))
            return counts if counts["total"] > 0 else None
        finally:
            _run(["git", "worktree", "remove", "--force", tmp], cwd=REPO_ROOT)


def historical(since: str | None) -> list[tuple[str, dict[str, int]]]:
    """Return ``[(tag, counts)]`` for every ``v*`` tag, cached."""
    cache = _load_cache()
    results: list[tuple[str, dict[str, int]]] = []
    for tag in _list_tags(since):
        if tag in cache:
            counts = cache[tag]
        else:
            print(f"collecting {tag}...", file=sys.stderr)
            counts = _collect_at_tag(tag) or {}
            cache[tag] = counts
            _save_cache(cache)
        if counts:
            results.append((tag, counts))
    return results


def _format_counts(counts: dict[str, int]) -> str:
    return (
        f"{counts['unit']} unit + {counts['e2e']} E2E + "
        f"{counts['soak']} soak = {counts['total']} total"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human-readable text."
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Walk git tags and report counts at each one (cached).",
    )
    parser.add_argument(
        "--since",
        metavar="TAG",
        help="Start the history walk at this tag (inclusive). Requires --history.",
    )
    args = parser.parse_args(argv)

    if args.since and not args.history:
        parser.error("--since requires --history")

    if args.history:
        data = historical(args.since)
        if args.json:
            print(json.dumps({tag: counts for tag, counts in data}, indent=2))
        else:
            widths = {"tag": max(len("tag"), *(len(t) for t, _ in data))}
            hdr = f"{'tag':<{widths['tag']}}  unit  e2e  soak  total"
            print(hdr)
            print("-" * len(hdr))
            for tag, counts in data:
                print(
                    f"{tag:<{widths['tag']}}  "
                    f"{counts['unit']:>4}  {counts['e2e']:>3}  "
                    f"{counts['soak']:>4}  {counts['total']:>5}"
                )
        return 0

    counts = collect_counts()
    if args.json:
        print(json.dumps(counts, indent=2))
    else:
        print(_format_counts(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
