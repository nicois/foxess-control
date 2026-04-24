#!/usr/bin/env python3
"""Collect structured session events from a live HA instance.

Polls the FoxESS Control info-log sensor via the HA REST API, extracts
entries that carry an ``event`` field, deduplicates them, and writes
JSONL traces to ``test-artifacts/traces/``.

Usage::

    HA_URL=https://ha.example HA_TOKEN=... python scripts/collect_events.py \
        --sensor sensor.foxess_info_log \
        --out test-artifacts/traces

One file is written per session_id.  Events without a session_id are
written to ``no_session.jsonl``.  Run continuously or on-demand — the
collector polls at *interval* seconds until stopped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - optional dep
    print("requests is required: pip install requests", file=sys.stderr)
    sys.exit(1)


def _event_fingerprint(entry: dict[str, Any]) -> str:
    """Stable hash of (t, event, payload) for deduplication."""
    key = json.dumps(
        {
            "t": entry.get("t"),
            "event": entry.get("event"),
            "payload": entry.get("payload"),
            "session": entry.get("session"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(key.encode()).hexdigest()


def _session_id(entry: dict[str, Any]) -> str:
    sess = entry.get("session") or {}
    return sess.get("session_id") or "no_session"


def fetch_entries(url: str, token: str, sensor: str) -> list[dict[str, Any]]:
    """Fetch the current ``entries`` attribute from the sensor."""
    r = requests.get(
        f"{url.rstrip('/')}/api/states/{sensor}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    state = r.json()
    entries = (state.get("attributes") or {}).get("entries") or []
    return [e for e in entries if isinstance(e, dict)]


def write_events(
    entries: list[dict[str, Any]],
    out_dir: Path,
    seen: set[str],
) -> int:
    """Append new event records to per-session JSONL files.

    Returns the number of new records written.
    """
    new_count = 0
    by_session: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if "event" not in entry:
            continue
        fp = _event_fingerprint(entry)
        if fp in seen:
            continue
        seen.add(fp)
        sid = _session_id(entry)
        by_session.setdefault(sid, []).append(entry)
        new_count += 1

    for sid, events in by_session.items():
        path = out_dir / f"{sid}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    return new_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("HA_URL"),
        help="HA base URL (or HA_URL env)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HA_TOKEN") or os.environ.get("HA_CLAUDE_TOKEN"),
        help="HA long-lived token (or HA_TOKEN / HA_CLAUDE_TOKEN env)",
    )
    parser.add_argument(
        "--sensor",
        default="sensor.foxess_control_info_log",
        help="Sensor entity_id to poll (info_log has longer INFO-level retention)",
    )
    parser.add_argument(
        "--out",
        default="test-artifacts/traces",
        type=Path,
        help="Output directory for JSONL files",
    )
    parser.add_argument(
        "--interval",
        default=30.0,
        type=float,
        help="Poll interval in seconds",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit (useful for ad-hoc collection)",
    )
    args = parser.parse_args()

    if not args.url or not args.token:
        print("--url and --token (or HA_URL / HA_TOKEN env) required", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()

    while True:
        try:
            entries = fetch_entries(args.url, args.token, args.sensor)
            new = write_events(entries, args.out, seen)
            if new:
                print(f"wrote {new} new events (total seen: {len(seen)})")
        except requests.RequestException as exc:
            print(f"fetch failed: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
