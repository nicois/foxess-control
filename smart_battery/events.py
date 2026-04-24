"""Structured event emission for session replay.

Events are log records with an ``event`` attribute (a short type name)
and a ``payload`` dict.  They flow through the standard logging
pipeline, so the existing :class:`SessionContextFilter` enriches them
with session context, and the debug-log sensor buffers them for
external collection.

The shape is intentionally minimal: event type, payload, schema
version.  Callers pass raw Python primitives in the payload so the
recorded traces can be re-fed to the pure algorithm functions
verbatim.

Event types (kept as constants to avoid typos at call sites):

- ``ALGO_DECISION`` — one emission per invocation of a pacing
  algorithm.  Payload includes the function name, its inputs as a
  dict, and its output.  Replaying the algorithm with the recorded
  inputs must reproduce the recorded output.

- ``TICK_SNAPSHOT`` — coordinator state at a tick boundary.  Payload
  captures SoC, powers, work mode, data source freshness.

- ``SCHEDULE_WRITE`` — every inverter schedule write.  Payload is the
  groups list plus the API response.

- ``TAPER_UPDATE`` — taper observation recorded.  Payload includes
  the observation inputs and the resulting profile snapshot.

- ``SERVICE_CALL`` — service invocation (smart_charge, smart_discharge,
  force_*).  Payload has the sanitised service data.

- ``SESSION_TRANSITION`` — lifecycle transitions (started, deferred,
  suspended, resumed, cancelled, ended).  Payload names the new state
  and the reason.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

EVENT_SCHEMA_VERSION = 1

ALGO_DECISION = "algo_decision"
TICK_SNAPSHOT = "tick_snapshot"
SCHEDULE_WRITE = "schedule_write"
TAPER_UPDATE = "taper_update"
SERVICE_CALL = "service_call"
SESSION_TRANSITION = "session_transition"


def emit_event(
    logger: logging.Logger,
    event_type: str,
    **payload: Any,
) -> None:
    """Emit a structured event through *logger* at INFO level.

    The record carries ``event``, ``payload``, and ``schema_version``
    attributes in addition to a human-readable message.  Handlers that
    understand the event protocol (the debug-log handler) serialise the
    structured fields; other handlers just see a normal log line.
    """
    message = f"{event_type}: {_format_summary(event_type, payload)}"
    logger.info(
        message,
        extra={
            "event": event_type,
            "payload": payload,
            "schema_version": EVENT_SCHEMA_VERSION,
        },
    )


def emit_schedule_write(
    logger: logging.Logger,
    mode: Any,
    *,
    power_w: int | None = None,
    fd_soc: int | None = None,
    call_site: str = "",
) -> None:
    """Emit a schedule_write event describing an inverter mode change.

    Fired just before :meth:`adapter.apply_mode` or :meth:`remove_override`
    so replay can reconstruct the exact sequence of mode transitions
    that the listener drove.
    """
    mode_value = getattr(mode, "value", str(mode))
    emit_event(
        logger,
        SCHEDULE_WRITE,
        mode=mode_value,
        power_w=power_w,
        fd_soc=fd_soc,
        call_site=call_site,
    )


def call_algo(
    logger: logging.Logger,
    fn: Callable[..., Any],
    call_site: str,
    **inputs: Any,
) -> Any:
    """Invoke *fn* with *inputs* and emit the decision as an event.

    Returns the function's output.  Inputs are normalised to
    JSON-serialisable primitives on the emitted record so the event
    survives a round-trip through the HA REST API.
    """
    output = fn(**inputs)
    emit_event(
        logger,
        ALGO_DECISION,
        algo=fn.__name__,
        call_site=call_site,
        inputs=normalise_inputs(inputs),
        output=normalise_output(output),
    )
    return output


def normalise_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Convert inputs to a JSON-serialisable form for recording.

    Known non-primitive types (datetime/time/timedelta, objects with
    ``to_dict``) are converted; everything else is passed through.
    Replay reverses the conversion in
    :func:`smart_battery.replay.denormalise_inputs`.
    """
    return {k: normalise_value(v) for k, v in inputs.items()}


def normalise_value(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        return {"__type__": "datetime", "iso": value.isoformat()}
    if isinstance(value, _dt.time):
        return {"__type__": "time", "iso": value.isoformat()}
    if isinstance(value, _dt.timedelta):
        return {"__type__": "timedelta", "seconds": value.total_seconds()}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return {"__type__": type(value).__name__, "data": value.to_dict()}
    return value


def normalise_output(output: Any) -> Any:
    """Outputs are either primitives or datetimes — same normaliser."""
    return normalise_value(output)


def _format_summary(event_type: str, payload: dict[str, Any]) -> str:
    """Build a short human-readable summary for the log line."""
    if event_type == ALGO_DECISION:
        algo = payload.get("algo", "?")
        output = payload.get("output")
        return f"{algo} -> {output}"
    if event_type == SESSION_TRANSITION:
        return f"{payload.get('state', '?')} ({payload.get('reason', '')})".strip()
    return ", ".join(f"{k}={v}" for k, v in payload.items() if k != "inputs")
