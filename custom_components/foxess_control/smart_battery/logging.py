"""Structured session context for smart battery logging.

Provides a logging.Filter that enriches log records with session
context fields read from the active session state.  Attached to the
logger hierarchy so existing _LOGGER calls gain structured fields
with zero changes to call sites.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
