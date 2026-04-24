"""Replay recorded session traces through the pure algorithms.

A trace is a JSONL file where each line is one event captured from a
live session (collected by polling the HA info-log sensor).  The
replay harness walks the trace and, for each ``algo_decision`` event,
re-invokes the named algorithm with the recorded inputs and compares
the result to the recorded output.

Divergence between recorded and replayed outputs indicates either:

- an algorithm change that altered outputs for the same inputs
  (captured regression — investigate), or
- a captured-input shape change (schema drift — bump
  ``EVENT_SCHEMA_VERSION`` and migrate traces).

Only pure algorithms are currently replayable.  Listener orchestration,
service handlers, and API calls require the full HA/simulator stack
and are out of scope for this harness.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import algorithms
from .events import ALGO_DECISION
from .taper import TaperProfile

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Registry of replayable algorithm entry points.  Add new entries as
# more call sites emit algo_decision events.
_REPLAY_FUNCS: dict[str, Callable[..., Any]] = {
    "calculate_discharge_power": algorithms.calculate_discharge_power,
    "calculate_charge_power": algorithms.calculate_charge_power,
    "calculate_deferred_start": algorithms.calculate_deferred_start,
    "calculate_discharge_deferred_start": algorithms.calculate_discharge_deferred_start,
    "should_suspend_discharge": algorithms.should_suspend_discharge,
    "is_charge_target_reachable": algorithms.is_charge_target_reachable,
}


@dataclass
class Divergence:
    """One recorded decision whose replay disagrees with the record."""

    index: int
    algo: str
    inputs: dict[str, Any]
    recorded_output: Any
    replayed_output: Any
    reason: str


@dataclass
class ReplayReport:
    total_events: int
    algo_events: int
    replayed: int
    divergences: list[Divergence]

    @property
    def ok(self) -> bool:
        return not self.divergences


_REHYDRATORS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "datetime": lambda d: _dt.datetime.fromisoformat(d["iso"]),
    "time": lambda d: _dt.time.fromisoformat(d["iso"]),
    "timedelta": lambda d: _dt.timedelta(seconds=d["seconds"]),
    "TaperProfile": lambda d: TaperProfile.from_dict(d["data"]),
}


def denormalise_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Reverse :func:`events._normalise_inputs` on a recorded payload."""
    return {k: _denormalise_value(v) for k, v in inputs.items()}


def _denormalise_value(value: Any) -> Any:
    if isinstance(value, dict) and "__type__" in value:
        type_name = value["__type__"]
        rehydrator = _REHYDRATORS.get(type_name)
        if rehydrator is None:
            # Unknown type — return raw dict; replay will likely fail at call
            # time, surfacing as invalid_inputs in the divergence report.
            return value
        return rehydrator(value)
    return value


def load_trace(path: Path) -> list[dict[str, Any]]:
    """Load a JSONL trace file and return a list of event records."""
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def replay_events(events: list[dict[str, Any]]) -> ReplayReport:
    """Walk *events*, replay algo_decision records, return a report.

    Non-algo events are counted but skipped.  An algo_decision for an
    unknown function name records a divergence with reason
    ``unknown_algo`` rather than raising.
    """
    divergences: list[Divergence] = []
    algo_count = 0
    replayed = 0

    for i, event in enumerate(events):
        if event.get("event") != ALGO_DECISION:
            continue
        algo_count += 1
        payload = event.get("payload") or {}
        algo = payload.get("algo")
        inputs = payload.get("inputs") or {}
        recorded_output = payload.get("output")

        fn = _REPLAY_FUNCS.get(algo or "")
        if fn is None:
            divergences.append(
                Divergence(
                    index=i,
                    algo=str(algo),
                    inputs=inputs,
                    recorded_output=recorded_output,
                    replayed_output=None,
                    reason="unknown_algo",
                )
            )
            continue

        try:
            rehydrated_inputs = denormalise_inputs(inputs)
        except (KeyError, ValueError, TypeError) as exc:
            divergences.append(
                Divergence(
                    index=i,
                    algo=str(algo),
                    inputs=inputs,
                    recorded_output=recorded_output,
                    replayed_output=None,
                    reason=f"input_rehydrate_failed: {exc}",
                )
            )
            continue

        try:
            replayed_output = fn(**rehydrated_inputs)
        except TypeError as exc:
            divergences.append(
                Divergence(
                    index=i,
                    algo=str(algo),
                    inputs=inputs,
                    recorded_output=recorded_output,
                    replayed_output=None,
                    reason=f"invalid_inputs: {exc}",
                )
            )
            continue

        replayed += 1
        # Compare raw recorded_output to the normalised replayed output
        # so datetime objects round-trip correctly.
        from .events import normalise_output

        replayed_serialised = normalise_output(replayed_output)
        if replayed_serialised != recorded_output:
            divergences.append(
                Divergence(
                    index=i,
                    algo=str(algo),
                    inputs=inputs,
                    recorded_output=recorded_output,
                    replayed_output=replayed_serialised,
                    reason="output_mismatch",
                )
            )

    return ReplayReport(
        total_events=len(events),
        algo_events=algo_count,
        replayed=replayed,
        divergences=divergences,
    )


def replay_file(path: Path) -> ReplayReport:
    """Convenience: load a trace file and replay it."""
    return replay_events(load_trace(path))
