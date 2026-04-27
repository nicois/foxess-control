#!/usr/bin/env python3
"""Knowledge-tree inventory and audit — mechanizes /project-overview check.

Enumerates every ``### P-NNN``, ``### C-NNN``, ``### D-NNN`` header
across ``docs/knowledge/`` and reports:

- Inventory counts (total entries, unique IDs, retired).
- ID collisions — IDs appearing in more than one file.
- Sequence gaps — missing numbers in the ID sequence.
- Priority-integrity violations — C-NNN without ``Priority enforced``,
  D-NNN without ``Priority served`` / ``Classification`` /
  ``Trades against``, trade-off inversions (D-NNN sacrificing a
  higher priority for a lower one), and ``safety``-classified D-NNN
  with no C-NNN trace.
- Trace gaps — C-NNN with no D-NNN pointing at them (upward gap),
  D-NNN with no C-NNN in their Traces (UNJUSTIFIED), P-NNN with no
  enforcing C-NNN (UNCONSTRAINED / aspirational).

Does NOT:
- Decide whether a gap is GAP vs ACCEPTED (judgement).
- Classify prose staleness.
- Write new tree entries.
- Propagate constraint changes through the tree.

Usage::

    python scripts/knowledge_audit.py              # human-readable
    python scripts/knowledge_audit.py --json       # machine-readable
    python scripts/knowledge_audit.py --strict     # exit 1 on violations

``--strict`` returns non-zero only for definite integrity violations
(missing priority / classification / trades-against, trade-off
inversions, safety-without-C-trace). ID collisions and sequence gaps
are reported but not fatal by default — this project has three
known collisions documented in META.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TREE_DIR = REPO_ROOT / "docs" / "knowledge"


# --- parsers -----------------------------------------------------------------
# Entry header: "### C-013: title", with body up to next ### header or EOF.
_HEADER_RE = re.compile(
    r"^### ([PCD]-\d+):\s*(.+?)(?=\n### [PCD]-\d+:|\Z)",
    re.M | re.S,
)
_PRIORITY_ENFORCED_RE = re.compile(r"\*\*Priority enforced\*\*:\s*(P-\d+)")
_PRIORITY_SERVED_RE = re.compile(r"\*\*Priority served\*\*:\s*(P-\d+)")
_TRADES_RE = re.compile(r"\*\*Trades against\*\*:\s*(P-\d+|none)", re.I)
_CLS_RE = re.compile(r"\*\*Classification\*\*:\s*`?(\w+)`?")
# Traces block: everything between "**Traces**:" and the next "**Header**:"
# or EOF.
_TRACES_BLOCK_RE = re.compile(r"\*\*Traces\*\*:(.*?)(?=\n\*\*[A-Z]|\Z)", re.S)
_C_REF_RE = re.compile(r"C-\d+")


@dataclass
class Entry:
    id: str
    kind: str  # "P" | "C" | "D"
    file: str  # relative to repo root
    title: str
    retired: bool = False
    priority: str | None = None  # C: enforced; D: served
    trades: str | None = None  # D only
    classification: str | None = None  # D only
    c_refs: list[str] = field(default_factory=list)


def _pn(pid: str) -> int:
    return int(pid.split("-")[1])


def _parse_block(kind: str, eid: str, body: str, file: Path) -> Entry:
    first_nl = body.find("\n")
    title = body[:first_nl].strip() if first_nl >= 0 else body.strip()
    entry = Entry(
        id=eid,
        kind=kind,
        file=str(file.relative_to(REPO_ROOT)),
        title=title,
        retired="[RETIRED]" in title,
    )
    if kind == "C":
        if m := _PRIORITY_ENFORCED_RE.search(body):
            entry.priority = m.group(1)
    elif kind == "D":
        if m := _PRIORITY_SERVED_RE.search(body):
            entry.priority = m.group(1)
        if m := _TRADES_RE.search(body):
            entry.trades = m.group(1)
        if m := _CLS_RE.search(body):
            entry.classification = m.group(1)
        if m := _TRACES_BLOCK_RE.search(body):
            entry.c_refs = sorted(set(_C_REF_RE.findall(m.group(1))))
    return entry


def collect_entries() -> list[Entry]:
    """Enumerate every P/C/D-NNN header across the knowledge tree."""
    entries: list[Entry] = []
    files_by_kind: dict[str, list[Path]] = {
        "P": [TREE_DIR / "01-vision.md"],
        "C": [TREE_DIR / "02-constraints.md"],
        "D": sorted((TREE_DIR / "04-design").glob("*.md"))
        if (TREE_DIR / "04-design").is_dir()
        else [],
    }
    for kind, paths in files_by_kind.items():
        for path in paths:
            if not path.exists():
                continue
            for m in _HEADER_RE.finditer(path.read_text()):
                eid = m.group(1)
                if not eid.startswith(f"{kind}-"):
                    # P-NNN header inside 02-constraints.md etc. — ignore.
                    continue
                # body is the match minus the "### ID: " prefix
                body = m.group(0).split(":", 1)[1]
                entries.append(_parse_block(kind, eid, body, path))
    return entries


# --- audit steps -------------------------------------------------------------


def find_collisions(entries: list[Entry]) -> dict[str, list[Entry]]:
    by_id: dict[str, list[Entry]] = {}
    for e in entries:
        by_id.setdefault(e.id, []).append(e)
    return {i: es for i, es in by_id.items() if len(es) > 1}


def find_sequence_gaps(entries: list[Entry]) -> dict[str, list[str]]:
    gaps: dict[str, list[str]] = {"P": [], "C": [], "D": []}
    for kind in ("P", "C", "D"):
        seen = {_pn(e.id) for e in entries if e.kind == kind}
        if not seen:
            continue
        hi = max(seen)
        missing = [n for n in range(1, hi + 1) if n not in seen]
        gaps[kind] = [f"{kind}-{n:03d}" for n in missing]
    return gaps


def check_priority_integrity(entries: list[Entry]) -> dict[str, list[Any]]:
    v: dict[str, list[Any]] = {
        "c_missing_priority": [],
        "d_missing_priority": [],
        "d_missing_classification": [],
        "d_missing_trades": [],
        "trade_off_inversions": [],
        "safety_without_c_trace": [],
    }
    for e in entries:
        if e.retired:
            continue
        if e.kind == "C" and not e.priority:
            v["c_missing_priority"].append(e.id)
        elif e.kind == "D":
            if not e.priority:
                v["d_missing_priority"].append(f"{e.id} ({e.file})")
            if not e.classification:
                v["d_missing_classification"].append(f"{e.id} ({e.file})")
            if not e.trades:
                v["d_missing_trades"].append(f"{e.id} ({e.file})")
            if (
                e.trades
                and e.trades.lower() != "none"
                and e.priority
                and _pn(e.priority) >= _pn(e.trades)
            ):
                v["trade_off_inversions"].append(
                    {
                        "id": e.id,
                        "file": e.file,
                        "serves": e.priority,
                        "trades": e.trades,
                    }
                )
            if e.classification == "safety" and not e.c_refs:
                v["safety_without_c_trace"].append(f"{e.id} ({e.file})")
    return v


def build_trace_map(entries: list[Entry]) -> dict[str, Any]:
    p_by_c: dict[str, list[str]] = {}
    p_by_d: dict[str, list[str]] = {}
    d_by_c: dict[str, list[str]] = {}
    for e in entries:
        if e.retired:
            continue
        if e.kind == "C" and e.priority:
            p_by_c.setdefault(e.priority, []).append(e.id)
        elif e.kind == "D":
            if e.priority:
                p_by_d.setdefault(e.priority, []).append(e.id)
            for cref in e.c_refs:
                d_by_c.setdefault(cref, []).append(e.id)
    unjustified = sorted(
        f"{e.id} ({e.file})"
        for e in entries
        if e.kind == "D" and not e.retired and not e.c_refs
    )
    active_c = {e.id for e in entries if e.kind == "C" and not e.retired}
    c_no_d = sorted(c for c in active_c if c not in d_by_c)
    active_p = {e.id for e in entries if e.kind == "P" and not e.retired}
    p_no_c = sorted(p for p in active_p if p not in p_by_c)
    return {
        "p_enforced_by_c": {k: sorted(v) for k, v in p_by_c.items()},
        "p_served_by_d": {k: sorted(v) for k, v in p_by_d.items()},
        "d_traces_c": {k: sorted(v) for k, v in d_by_c.items()},
        "unjustified_d": unjustified,
        "c_no_d_upward": c_no_d,
        "p_no_c_enforcing": p_no_c,
    }


def counts(entries: list[Entry]) -> dict[str, int]:
    active = [e for e in entries if not e.retired]
    return {
        "p_total": sum(1 for e in active if e.kind == "P"),
        "c_total": sum(1 for e in active if e.kind == "C"),
        "d_entries_total": sum(1 for e in active if e.kind == "D"),
        "d_unique_ids": len({e.id for e in active if e.kind == "D"}),
        "entries_retired_marker": sum(1 for e in entries if e.retired),
    }


def audit() -> dict[str, Any]:
    entries = collect_entries()
    return {
        "counts": counts(entries),
        "collisions": {
            cid: [{"id": e.id, "file": e.file, "title": e.title} for e in es]
            for cid, es in sorted(find_collisions(entries).items())
        },
        "sequence_gaps": find_sequence_gaps(entries),
        "priority_integrity": check_priority_integrity(entries),
        "trace_map": build_trace_map(entries),
    }


# --- reporting ---------------------------------------------------------------


def _fmt_list(items: list[Any], empty: str = "none") -> str:
    if not items:
        return empty
    return ", ".join(str(i) for i in items)


def render(data: dict[str, Any]) -> str:
    out: list[str] = []
    c = data["counts"]
    out.append("Knowledge-tree audit")
    out.append("")
    out.append("Inventory:")
    out.append(f"  P-NNN (active):    {c['p_total']}")
    out.append(f"  C-NNN (active):    {c['c_total']}")
    out.append(
        f"  D-NNN entries:     {c['d_entries_total']} "
        f"({c['d_unique_ids']} unique IDs, "
        f"{c['d_entries_total'] - c['d_unique_ids']} collisions)"
    )
    if c["entries_retired_marker"]:
        out.append(f"  retired marker:    {c['entries_retired_marker']}")
    out.append("")

    collisions = data["collisions"]
    out.append(f"ID collisions: {len(collisions)}")
    for cid, es in collisions.items():
        out.append(f"  {cid}:")
        for e in es:
            out.append(f"    - {e['file']}: {e['title']}")
    out.append("")

    gaps = data["sequence_gaps"]
    any_gaps = any(gaps.values())
    out.append(f"Sequence gaps: {'none' if not any_gaps else ''}")
    for kind in ("P", "C", "D"):
        if gaps[kind]:
            out.append(f"  {kind}: {_fmt_list(gaps[kind])}")
    out.append("")

    pi = data["priority_integrity"]
    out.append("Priority integrity:")
    out.append(f"  C-NNN missing priority:       {_fmt_list(pi['c_missing_priority'])}")
    out.append(f"  D-NNN missing priority:       {_fmt_list(pi['d_missing_priority'])}")
    out.append(
        f"  D-NNN missing classification: {_fmt_list(pi['d_missing_classification'])}"
    )
    out.append(f"  D-NNN missing trades-against: {_fmt_list(pi['d_missing_trades'])}")
    out.append(
        f"  Safety w/o C-NNN trace:       {_fmt_list(pi['safety_without_c_trace'])}"
    )
    inv = pi["trade_off_inversions"]
    out.append(f"  Trade-off inversions:         {len(inv) if inv else 'none'}")
    for row in inv:
        out.append(
            f"    {row['id']} ({row['file']}): "
            f"serves {row['serves']} but trades {row['trades']}"
        )
    out.append("")

    tm = data["trace_map"]
    out.append(
        f"P-NNN with no C-NNN enforcing (aspirational / gap): "
        f"{len(tm['p_no_c_enforcing'])}"
    )
    for pid in tm["p_no_c_enforcing"]:
        out.append(f"  {pid}")
    out.append("")
    out.append(
        f"C-NNN with no D-NNN pointing at them (upward gap): {len(tm['c_no_d_upward'])}"
    )
    for cid in tm["c_no_d_upward"]:
        out.append(f"  {cid}")
    out.append("")
    out.append(f"D-NNN UNJUSTIFIED (no C-NNN trace): {len(tm['unjustified_d'])}")
    for did in tm["unjustified_d"]:
        out.append(f"  {did}")
    return "\n".join(out)


def has_strict_violations(data: dict[str, Any]) -> bool:
    """Definite integrity violations that `--strict` treats as fatal.

    Excludes ID collisions and sequence gaps (project-specific tolerated
    conditions documented in META.md).
    """
    pi = data["priority_integrity"]
    return bool(
        pi["c_missing_priority"]
        or pi["d_missing_priority"]
        or pi["d_missing_classification"]
        or pi["d_missing_trades"]
        or pi["trade_off_inversions"]
        or pi["safety_without_c_trace"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Knowledge-tree inventory and audit.")
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of text."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any priority-integrity violation is present "
        "(collisions and sequence gaps are always reported but never "
        "fatal — they may be tolerated by the project).",
    )
    args = parser.parse_args(argv)

    data = audit()
    print(json.dumps(data, indent=2, sort_keys=True) if args.json else render(data))

    if args.strict and has_strict_violations(data):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
