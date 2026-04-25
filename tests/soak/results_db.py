"""SQLite store for soak test inflection points and run metadata.

Records state transitions, SoC direction changes, and power step changes
as discrete events. Dense CSV data is kept separately for single-run
debugging; this DB is the cross-run comparison layer.

Schema:
    runs    — one row per (tag, scenario) execution
    events  — inflection points within a run
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import SoakRecorder, SoakSample

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tag         TEXT NOT NULL,
    scenario    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    duration_s  REAL,
    passed      INTEGER,
    violations  INTEGER DEFAULT 0,
    soc_start   REAL,
    soc_end     REAL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    elapsed_s   REAL NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT NOT NULL,
    soc         REAL,
    power_w     REAL,
    state       TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_run
    ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_tag_scenario
    ON runs(tag, scenario);
"""

SOC_DEADBAND = 2.0
POWER_STEP_THRESHOLD_W = 500.0


@dataclass
class InflectionEvent:
    elapsed_s: float
    event_type: str
    detail: str
    soc: float
    power_w: float
    state: str


def _get_tag() -> str:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def detect_inflections(
    samples: list[SoakSample],
) -> list[InflectionEvent]:
    if len(samples) < 2:
        return []

    events: list[InflectionEvent] = []
    prev = samples[0]

    # Track SoC direction: +1 rising, -1 falling, 0 unknown
    soc_direction = 0
    soc_anchor = prev.soc

    for cur in samples[1:]:
        if cur.state != prev.state:
            events.append(
                InflectionEvent(
                    elapsed_s=cur.elapsed_s,
                    event_type="state_change",
                    detail=f"{prev.state}→{cur.state}",
                    soc=cur.soc,
                    power_w=cur.power_w,
                    state=cur.state,
                )
            )

        delta = cur.soc - soc_anchor
        if abs(delta) >= SOC_DEADBAND:
            new_dir = 1 if delta > 0 else -1
            if soc_direction != 0 and new_dir != soc_direction:
                label = "rising" if new_dir > 0 else "falling"
                events.append(
                    InflectionEvent(
                        elapsed_s=cur.elapsed_s,
                        event_type="soc_direction",
                        detail=(
                            f"{'falling' if soc_direction == -1 else 'rising'}"
                            f"→{label} at {cur.soc:.1f}%"
                        ),
                        soc=cur.soc,
                        power_w=cur.power_w,
                        state=cur.state,
                    )
                )
            soc_direction = new_dir
            soc_anchor = cur.soc

        power_delta = abs(cur.power_w - prev.power_w)
        if power_delta >= POWER_STEP_THRESHOLD_W:
            events.append(
                InflectionEvent(
                    elapsed_s=cur.elapsed_s,
                    event_type="power_step",
                    detail=(f"{prev.power_w:.0f}W→{cur.power_w:.0f}W"),
                    soc=cur.soc,
                    power_w=cur.power_w,
                    state=cur.state,
                )
            )

        prev = cur

    return events


def _open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    return conn


def save_run(
    db_path: Path,
    recorder: SoakRecorder,
    scenario: str,
    started_at: str,
    passed: bool,
) -> int:
    events = detect_inflections(recorder.samples)
    # Persist every recorded invariant violation as its own event row.
    # Without this the runs row carries only a violations *count*; the
    # rule and detail are dropped and post-mortem diagnosis is
    # impossible (C-020).
    sample_by_elapsed = {s.elapsed_s: s for s in recorder.samples}
    for v in recorder.violations:
        nearest = (
            min(
                recorder.samples,
                key=lambda s: abs(s.elapsed_s - v.elapsed_s),
                default=None,
            )
            if recorder.samples
            else None
        )
        snap = sample_by_elapsed.get(v.elapsed_s) or nearest
        events.append(
            InflectionEvent(
                elapsed_s=v.elapsed_s,
                event_type="violation",
                detail=f"{v.rule}: {v.detail}",
                soc=snap.soc if snap else 0.0,
                power_w=snap.power_w if snap else 0.0,
                state=snap.state if snap else "",
            )
        )
    tag = _get_tag()

    conn = _open_db(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO runs "
            "(tag, scenario, started_at, duration_s, passed, "
            "violations, soc_start, soc_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tag,
                scenario,
                started_at,
                recorder.samples[-1].elapsed_s if recorder.samples else 0,
                int(passed),
                len(recorder.violations),
                recorder.samples[0].soc if recorder.samples else None,
                recorder.samples[-1].soc if recorder.samples else None,
            ),
        )
        run_id = cur.lastrowid
        assert run_id is not None

        conn.executemany(
            "INSERT INTO events "
            "(run_id, elapsed_s, event_type, detail, soc, power_w, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    e.elapsed_s,
                    e.event_type,
                    e.detail,
                    e.soc,
                    e.power_w,
                    e.state,
                )
                for e in events
            ],
        )
        conn.commit()
        return run_id
    finally:
        conn.close()
