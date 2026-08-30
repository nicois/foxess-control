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

_LOGGER = logging.getLogger(__name__)

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

# Attribute name under which a brand layer may annotate an exception with
# non-secret facts about what it was attempting when the exception was
# raised.  The C-039 seam for observability: the brand layer knows the
# payload it sent, the brand-agnostic code that catches the exception does
# not, and must not import a brand module to find out.  So the brand
# annotates and re-raises; the catcher reads the annotation back with
# :func:`diagnostic_context` and hands it to
# :func:`record_operational_error` as *context*.
#
# Why an exception annotation rather than a Protocol method: the value is
# per-*failure*, not per-adapter, and it has to survive being re-raised
# through layers (executor job -> adapter -> listener) that have no
# reference to the object that built it.  A Protocol accessor would also
# have to be plumbed through the circuit-breaker wrapper, which is
# deliberately adapter-agnostic.
DIAGNOSTIC_CONTEXT_ATTR = "diagnostic_context"


def attach_diagnostic_context(exc: BaseException, context: dict[str, Any]) -> None:
    """Annotate *exc* with non-secret facts about the failed attempt.

    Stores a *copy*, so a caller that reuses or mutates its dict afterwards
    cannot rewrite history.  Best-effort and never raises: an exception that
    refuses attribute assignment must still propagate as itself, because the
    original failure matters more than its annotation — and because
    replacing it with an ``AttributeError`` would change which exception the
    circuit breaker sees (C-024).

    Put only non-secret facts here.  The diagnostics exporter redacts known
    sensitive *keys*, which does not help if a token is embedded in a value.
    """
    try:
        setattr(exc, DIAGNOSTIC_CONTEXT_ATTR, dict(context))
    except Exception:  # noqa: BLE001 — annotation must never mask the failure
        _LOGGER.debug(
            "Could not annotate %s with diagnostic context",
            type(exc).__name__,
            exc_info=True,
        )


def diagnostic_context(exc: BaseException) -> dict[str, Any]:
    """Return the context attached by :func:`attach_diagnostic_context`.

    ``{}`` when nothing was attached, so callers can merge unconditionally.
    """
    ctx = getattr(exc, DIAGNOSTIC_CONTEXT_ATTR, None)
    return dict(ctx) if isinstance(ctx, dict) else {}


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
    dedupe_key: str | None = None,
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

    *dedupe_key* opts into collapsing: when the buffer already holds an entry
    recorded under the same key, that entry's ``repeat`` count and ``last_t``
    are updated in place instead of a second entry being appended.  Opt-in
    because most call sites fire once per poll and want every occurrence;
    the listener retry path fires every tick against the *same* failure, and
    without collapsing one error class evicts all 30 slots and destroys the
    diagnostic value of everything else in the download.  Collapsing bounds
    the buffer, never the log: every occurrence is still logged.
    """
    exc_type = type(exc).__name__
    message = f"[{category}] {attempted}: {exc_type}: {exc}"
    if hint:
        message += f" — {hint}"
    logger.log(_SEVERITY_LEVELS.get(severity, logging.WARNING), "%s", message)
    if buffer is None:
        return
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    if dedupe_key is not None:
        # Scan the whole buffer, not just its last entry: escalation records
        # (e.g. the circuit breaker opening) legitimately land between
        # repeats of the underlying failure, and keying on the previous
        # entry alone would let the failure re-append once per session.
        for entry in reversed(buffer):
            if entry.get("dedupe_key") == dedupe_key:
                entry["repeat"] = int(entry.get("repeat", 1)) + 1
                entry["last_t"] = now
                return
    record: dict[str, Any] = {
        "t": now,
        "category": category,
        "attempted": attempted,
        "exc_type": exc_type,
        "exc_str": str(exc),
        "hint": hint,
        "context": dict(context) if context else {},
        "severity": severity,
    }
    if dedupe_key is not None:
        record["dedupe_key"] = dedupe_key
        record["repeat"] = 1
        record["last_t"] = now
    buffer.append(record)
