#!/usr/bin/env python3
"""Collect a merged timeline of FoxESS Control events + exogenous state from HA.

Purpose: produce a single JSONL per session that captures both what the
integration *decided* (algorithm decisions, schedule writes, session
transitions, tick snapshots) **and** what the real inverter + house
were *doing* at the same moments (SoC, house load, PV, grid import /
export, BMS temperature, work mode). This pairing is what the
simulator needs to be validated against a real session — feed the
exogenous observations into the simulator, re-run the algorithms at
each tick, and assert the simulator's resulting state agrees with the
HA observation within tolerance.

Two modes:

  **live**   Poll the integration's log sensor for structured events
             plus a set of exogenous sensors at a tight interval and
             append new records to per-session JSONL files. Run for
             the duration of a session.

  **history** Pull recorded state changes from HA's
             ``/api/history/period/`` for a past time range and
             interleave them with any still-retained events into a
             single timeline. Useful for reconstructing sessions that
             have already ended.

Output format: one JSONL per session (default ``test-artifacts/ha-sessions/``).
Each line is one of:

  {"t": <iso>, "kind": "event",       "event": "algo_decision",
   "payload": {...}, "session": {...}}
  {"t": <iso>, "kind": "observation", "entity_id": "sensor.foxess_battery_soc",
   "state": "92.0", "attributes": {...}}

The ``kind`` field makes the two streams trivially separable. Events
carry the same shape as ``scripts/collect_events.py`` produces, so
traces collected here can be replayed through
``smart_battery.replay`` directly (filter for ``kind == "event"``).

Usage (live, polling every 5 s)::

    HA_URL=https://ha.example HA_TOKEN=... \\
        python scripts/collect_ha_session.py live \\
          --out test-artifacts/ha-sessions \\
          --interval 5

Usage (history, reconstruct a past session)::

    python scripts/collect_ha_session.py history \\
        --start 2026-04-24T16:04:00+10:00 \\
        --end   2026-04-24T16:45:00+10:00 \\
        --out   test-artifacts/ha-sessions/2026-04-24_discharge.jsonl

Environment: ``HA_URL`` + one of ``HA_TOKEN`` / ``HA_CLAUDE_TOKEN``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover
    print("requests is required: pip install requests", file=sys.stderr)
    sys.exit(1)


# Default sensors worth pairing with events. Override with --sensor.
# Chosen to cover: SoC, house load, solar (split + total), grid flows
# (power + cumulative energy), BMS + inverter temperatures, charge /
# discharge power, work mode, and the integration's own exposed
# smart-ops attributes. Missing entities on a given deployment are
# tolerated — ``fetch_state`` returns None for 404s.
DEFAULT_SENSORS = [
    "sensor.foxess_battery_soc",
    "sensor.foxess_house_load",
    "sensor.foxess_generation",
    "sensor.foxess_pv1_power",
    "sensor.foxess_pv2_power",
    "sensor.foxess_grid_consumption",
    "sensor.foxess_grid_feed_in",
    "sensor.foxess_grid_meter_power",
    "sensor.foxess_grid_feed_in_energy",
    "sensor.foxess_grid_consumption_energy",
    "sensor.foxess_charge_rate",
    "sensor.foxess_discharge_rate",
    "sensor.foxess_battery_temperature",
    "sensor.foxess_bms_battery_temperature",
    "sensor.foxess_work_mode",
    "sensor.foxess_smart_operations",
    "sensor.foxess_status",
    "binary_sensor.foxess_smart_charge_active",
    "binary_sensor.foxess_smart_discharge_active",
]

# Log sensors that carry structured events. The integration
# historically named these differently across versions; we probe each
# in order and use whichever exists.
EVENT_SENSOR_CANDIDATES = [
    "sensor.foxess_info_log",
    "sensor.foxess_debug_log",
    "sensor.foxess_control_info_log",
    "sensor.foxess_control_debug_log",
]

# ---------- HTTP helpers ----------


class HAClient:
    def __init__(self, url: str, token: str, timeout: float = 10.0) -> None:
        self.base = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(self.headers)

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        r = self._session.get(
            f"{self.base}/api/states/{entity_id}", timeout=self.timeout
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return cast("dict[str, Any]", r.json())

    def get_history(
        self,
        start: datetime,
        end: datetime,
        entity_ids: list[str],
    ) -> list[list[dict[str, Any]]]:
        start_iso = quote(start.isoformat(), safe="")
        end_iso = quote(end.isoformat(), safe="")
        filter_arg = quote(",".join(entity_ids), safe="")
        url = (
            f"{self.base}/api/history/period/{start_iso}"
            f"?filter_entity_id={filter_arg}&end_time={end_iso}"
        )
        r = self._session.get(url, timeout=max(self.timeout, 30))
        r.raise_for_status()
        return cast("list[list[dict[str, Any]]]", r.json())


# ---------- Event parsing ----------


def _event_fingerprint(entry: dict[str, Any]) -> str:
    """Stable hash of (t, event, payload, session) for deduplication."""
    key = json.dumps(
        {
            "t": entry.get("t"),
            "event": entry.get("event"),
            "payload": entry.get("payload"),
            "session": entry.get("session"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(key.encode()).hexdigest()


def _obs_fingerprint(entity_id: str, obs: dict[str, Any]) -> str:
    """Stable hash for observations — changes only when the state does."""
    return hashlib.sha256(
        f"{entity_id}|{obs.get('last_changed')}|{obs.get('state')}".encode()
    ).hexdigest()


def _session_id(entry: dict[str, Any]) -> str:
    sess = entry.get("session") or {}
    return sess.get("session_id") or "no_session"


def _pick_event_sensor(client: HAClient, override: str | None) -> str | None:
    if override:
        return override if client.get_state(override) else None
    for candidate in EVENT_SENSOR_CANDIDATES:
        if client.get_state(candidate):
            return candidate
    return None


def _event_entries(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not state:
        return []
    entries = (state.get("attributes") or {}).get("entries") or []
    return [e for e in entries if isinstance(e, dict) and "event" in e]


# ---------- Writers ----------


class TimelineWriter:
    """Writes event + observation records to per-session JSONL files.

    Observations without a current session context (e.g. idle periods)
    go to ``no_session.jsonl`` so the exogenous timeline is never lost.
    """

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._current_session = "no_session"
        self._seen: set[str] = set()

    def write_event(self, entry: dict[str, Any]) -> bool:
        fp = _event_fingerprint(entry)
        if fp in self._seen:
            return False
        self._seen.add(fp)
        sid = _session_id(entry)
        # Track the latest active session so observations are routed
        # into the same file.
        if sid != "no_session":
            self._current_session = sid
        record = {
            "t": entry.get("t"),
            "kind": "event",
            "event": entry.get("event"),
            "schema_version": entry.get("schema_version"),
            "payload": entry.get("payload"),
            "session": entry.get("session"),
        }
        self._append(sid, record)
        return True

    def write_observation(self, entity_id: str, state: dict[str, Any]) -> bool:
        fp = _obs_fingerprint(entity_id, state)
        if fp in self._seen:
            return False
        self._seen.add(fp)
        record = {
            "t": state.get("last_changed") or state.get("last_updated"),
            "kind": "observation",
            "entity_id": entity_id,
            "state": state.get("state"),
            "attributes": self._trim_attrs(state.get("attributes") or {}),
        }
        self._append(self._current_session, record)
        return True

    def _append(self, session_id: str, record: dict[str, Any]) -> None:
        path = self.out_dir / f"{session_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    @staticmethod
    def _trim_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
        """Drop UI-only cruft from attribute blobs to keep files small."""
        drop = {
            "friendly_name",
            "icon",
            "device_class",
            "state_class",
            "unit_of_measurement",
            "options",
        }
        return {k: v for k, v in attrs.items() if k not in drop}


# ---------- Live mode ----------


def run_live(args: argparse.Namespace, client: HAClient) -> int:
    try:
        event_sensor = _pick_event_sensor(client, args.event_sensor)
    except requests.RequestException as exc:
        # Startup can't even reach HA — exit immediately so the
        # supervisor applies its backoff. The process will try again
        # on the next schedule.
        print(f"startup failed: {exc}", file=sys.stderr)
        return 1
    if event_sensor:
        print(f"using event sensor: {event_sensor}", file=sys.stderr)
    else:
        print(
            "no event sensor available — will still record exogenous observations",
            file=sys.stderr,
        )

    writer = TimelineWriter(args.out)
    stop = False

    def _sig_handler(_signum: int, _frame: Any) -> None:  # noqa: ANN401
        nonlocal stop
        stop = True
        print("\ncaught signal — finishing current poll and exiting", file=sys.stderr)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    # Consecutive HA-connection failures. When this crosses the
    # threshold we exit non-zero so the process supervisor (systemd,
    # etc.) can apply its own backoff — far more appropriate than
    # in-process busy-waiting at the poll interval.
    consecutive_failures = 0

    while not stop:
        try:
            # Events first — advances the active session pointer so
            # same-tick observations land in the right file.
            if event_sensor:
                state = client.get_state(event_sensor)
                new_events = 0
                for entry in _event_entries(state):
                    if writer.write_event(entry):
                        new_events += 1
            else:
                new_events = 0

            new_obs = 0
            for sensor in args.sensor or DEFAULT_SENSORS:
                obs = client.get_state(sensor)
                if obs is None:
                    continue
                if writer.write_observation(sensor, obs):
                    new_obs += 1

            if new_events or new_obs:
                print(
                    f"events={new_events} obs={new_obs} "
                    f"session={writer._current_session}",
                    file=sys.stderr,
                )
            # Reset the failure counter on any successful poll.
            consecutive_failures = 0
        except requests.RequestException as exc:
            consecutive_failures += 1
            print(
                f"poll failed ({consecutive_failures}/"
                f"{args.max_consecutive_failures}): {exc}",
                file=sys.stderr,
            )
            if consecutive_failures >= args.max_consecutive_failures:
                print(
                    "too many consecutive failures — exiting so the "
                    "service supervisor can apply its backoff",
                    file=sys.stderr,
                )
                return 1

        if args.once:
            return 0
        # Sleep in short chunks so Ctrl-C is responsive.
        for _ in range(max(1, int(args.interval * 2))):
            if stop:
                break
            time.sleep(0.5)
    return 0


# ---------- History mode ----------


def run_history(args: argparse.Namespace, client: HAClient) -> int:
    start = _parse_ts(args.start)
    end = _parse_ts(args.end)
    sensors = args.sensor or DEFAULT_SENSORS
    event_sensor = _pick_event_sensor(client, args.event_sensor)

    # Single merged file for a known-bounded history run.
    out_path: Path = args.out
    if out_path.is_dir():
        stamp = start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_path / f"history_{stamp}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"history range {start.isoformat()} → {end.isoformat()} "
        f"({len(sensors)} sensors{'' if not event_sensor else ' + events'})",
        file=sys.stderr,
    )

    series = client.get_history(start, end, sensors)

    # Flatten observations into one timeline, sorted by timestamp.
    obs_timeline: list[tuple[str, str, dict[str, Any]]] = []
    for one_series in series:
        if not one_series:
            continue
        eid = one_series[0].get("entity_id", "")
        for point in one_series:
            ts = point.get("last_changed") or point.get("last_updated")
            if ts is None:
                continue
            obs_timeline.append((ts, eid, point))

    # Events currently retained by the log sensor (bounded buffer of
    # the most recent ~75 entries — ``history`` mode is best-effort
    # for events; use ``live`` mode during the session to capture them
    # exhaustively).
    events: list[dict[str, Any]] = []
    if event_sensor:
        state = client.get_state(event_sensor)
        for entry in _event_entries(state):
            ts = entry.get("t")
            try:
                if ts and start <= _parse_ts(ts) <= end:
                    events.append(entry)
            except ValueError:
                continue

    # Interleave: observations keyed by ts string (ISO sorts correctly),
    # events keyed by their 't' field.
    merged: list[tuple[str, dict[str, Any]]] = []
    for ts, entity_id, point in obs_timeline:
        merged.append(
            (
                ts,
                {
                    "t": ts,
                    "kind": "observation",
                    "entity_id": entity_id,
                    "state": point.get("state"),
                    "attributes": TimelineWriter._trim_attrs(
                        point.get("attributes") or {}
                    ),
                },
            )
        )
    for entry in events:
        ts = entry.get("t") or ""
        merged.append(
            (
                ts,
                {
                    "t": ts,
                    "kind": "event",
                    "event": entry.get("event"),
                    "schema_version": entry.get("schema_version"),
                    "payload": entry.get("payload"),
                    "session": entry.get("session"),
                },
            )
        )
    merged.sort(key=lambda pair: pair[0])

    with out_path.open("w", encoding="utf-8") as f:
        for _, record in merged:
            f.write(json.dumps(record, default=str) + "\n")

    event_count = sum(1 for _, r in merged if r.get("kind") == "event")
    obs_count = len(merged) - event_count
    print(
        f"wrote {len(merged)} records ({event_count} events, "
        f"{obs_count} observations) → {out_path}",
        file=sys.stderr,
    )
    return 0


def _parse_ts(value: str) -> datetime:
    # Accept both "Z" and "+HH:MM" offsets.
    cleaned = value.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)


# ---------- Entry point ----------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        action="append",
        help=(
            "Sensor entity_id to record (may be repeated). "
            "Default: the built-in FoxESS observation set."
        ),
    )
    parser.add_argument(
        "--event-sensor",
        help=(
            "Override the log-sensor entity_id that carries structured "
            "events. Default: auto-detect from a known list."
        ),
    )

    sub = parser.add_subparsers(dest="mode", required=True)

    p_live = sub.add_parser("live", help="Stream events + observations in real time")
    p_live.add_argument(
        "--out",
        default=Path("test-artifacts/ha-sessions"),
        type=Path,
        help="Output directory (one JSONL per session_id)",
    )
    p_live.add_argument(
        "--interval",
        default=5.0,
        type=float,
        help="Poll interval in seconds (default: 5)",
    )
    p_live.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit (useful for ad-hoc capture)",
    )
    p_live.add_argument(
        "--max-consecutive-failures",
        default=3,
        type=int,
        help=(
            "Exit non-zero after this many consecutive poll failures "
            "so the service supervisor applies its own backoff "
            "(default: 3 — with the default 5 s interval, the process "
            "exits after ~15 s of unreachable HA)."
        ),
    )

    p_hist = sub.add_parser(
        "history", help="Reconstruct a past session from HA history"
    )
    p_hist.add_argument("--start", required=True, help="ISO8601 start timestamp")
    p_hist.add_argument("--end", required=True, help="ISO8601 end timestamp")
    p_hist.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output file path or directory",
    )

    args = parser.parse_args()

    if not args.url or not args.token:
        print(
            "--url and --token (or HA_URL / HA_TOKEN env) required",
            file=sys.stderr,
        )
        return 2

    client = HAClient(args.url, args.token)

    if args.mode == "live":
        return run_live(args, client)
    if args.mode == "history":
        return run_history(args, client)
    parser.error(f"unknown mode {args.mode!r}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
