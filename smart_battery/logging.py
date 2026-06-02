"""Structured session context for smart battery logging.

Provides a logging.Filter that enriches log records with session
context fields read from the active session state.  Attached to the
logger hierarchy so existing _LOGGER calls gain structured fields
with zero changes to call sites.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections import deque
    from collections.abc import Callable

_CHARGE_FIELDS = (
    "session_id",
    "target_soc",
    "max_power_w",
    "last_power_w",
    "charging_started",
    "soc_unavailable_count",
)

_DISCHARGE_FIELDS = (
    "session_id",
    "min_soc",
    "max_power_w",
    "last_power_w",
    "discharging_started",
    "suspended",
    "consumption_peak_kw",
    "soc_unavailable_count",
)


class SessionContextFilter(logging.Filter):
    """Inject active session context into log records.

    The *context_getter* returns ``(charge_state, discharge_state)``
    dicts (or ``None`` when no session is active).  Fields are set on
    the ``LogRecord`` under a ``session`` key so they don't collide
    with standard attributes.
    """

    def __init__(
        self,
        context_getter: Callable[
            [], tuple[dict[str, Any] | None, dict[str, Any] | None]
        ],
        name: str = "",
    ) -> None:
        super().__init__(name)
        self._get_context = context_getter

    def filter(self, record: logging.LogRecord) -> bool:
        ctx: dict[str, Any] = {}
        try:
            charge, discharge = self._get_context()
        except Exception:  # noqa: BLE001
            record.session = ctx
            return True

        if charge is not None:
            ctx["session_type"] = "charge"
            for field in _CHARGE_FIELDS:
                if field in charge:
                    ctx[field] = charge[field]

        if discharge is not None:
            ctx["session_type"] = "discharge"
            for field in _DISCHARGE_FIELDS:
                if field in discharge:
                    ctx[field] = discharge[field]

        record.session = ctx
        return True


def install_session_filter(
    logger: logging.Logger,
    context_getter: Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]],
) -> SessionContextFilter:
    """Attach a `SessionContextFilter` to *logger* and return it."""
    f = SessionContextFilter(context_getter)
    logger.addFilter(f)
    return f


def remove_session_filter(
    logger: logging.Logger,
    f: SessionContextFilter,
) -> None:
    """Remove a previously installed `SessionContextFilter`."""
    logger.removeFilter(f)


_SEVERITY_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def record_operational_error(
    logger: logging.Logger,
    buffer: deque[dict[str, Any]] | None,
    *,
    category: str,
    attempted: str,
    exc: BaseException,
    hint: str | None = None,
    context: dict[str, Any] | None = None,
    severity: str = "warning",
) -> None:
    """Log a self-sufficient operational-error line and record it to a ring buffer.

    Brand-agnostic (C-039): takes the ring buffer as a parameter rather than
    importing brand domain data.  Pass ``None`` for *buffer* to log only.  The
    log line names the category, what was attempted, the exception type and
    string, and an optional likely-cause *hint*, so a single pasted line is
    self-sufficient.  The buffer record is the structured form exported by the
    diagnostics platform.

    This is distinct from the listener's ``_record_error`` (which records a
    session abort to ``smart_error_state`` and raises a Repair issue).  This
    helper is for operational/diagnostic errors surfaced via the diagnostics
    download, not session aborts.

    *context* should contain only non-secret facts; the diagnostics exporter
    redacts known-sensitive keys, but do not put raw tokens or passwords here.
    """
    exc_type = type(exc).__name__
    message = f"[{category}] {attempted}: {exc_type}: {exc}"
    if hint:
        message += f" — {hint}"
    logger.log(_SEVERITY_LEVELS.get(severity, logging.WARNING), "%s", message)
    if buffer is not None:
        buffer.append(
            {
                "t": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                "category": category,
                "attempted": attempted,
                "exc_type": exc_type,
                "exc_str": str(exc),
                "hint": hint,
                "context": dict(context) if context else {},
                "severity": severity,
            }
        )
