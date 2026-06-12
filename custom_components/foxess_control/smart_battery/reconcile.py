"""Brand-agnostic reconciliation of commanded vs reported work mode.

Pure decision logic (C-039): given what the integration last commanded
and what the inverter currently reports, decide whether they diverge —
tolerating a grace window for write propagation.  No I/O, no HA imports.
See docs/superpowers/specs/2026-06-12-schedule-write-verification-design.md.
"""

from __future__ import annotations

import datetime  # noqa: TC003 — used at runtime for timedelta comparison
import enum

# The mode the inverter reports when no managed group is active.
SELF_USE = "SelfUse"


class ReconcileVerdict(enum.Enum):
    """Outcome of comparing commanded vs reported work mode."""

    OK = "ok"
    WITHIN_GRACE = "within_grace"
    CONFLICT = "conflict"


def _norm(mode: str | None) -> str:
    """Normalise a reported/commanded mode; None or empty string reports as SelfUse.

    ``get_current_mode`` returns None when no enabled group covers now —
    the inverter is in self-use.  Treat that as SelfUse so a commanded
    removal (expecting SelfUse) reconciles cleanly.
    """
    return mode if mode else SELF_USE


def reconcile_commanded_mode(
    commanded_mode: str | None,
    commanded_at: datetime.datetime,
    reported_mode: str | None,
    now: datetime.datetime,
    grace: datetime.timedelta,
) -> ReconcileVerdict:
    """Return the reconciliation verdict.

    - ``commanded_mode is None`` → OK (nothing has been commanded yet).
    - reported matches commanded → OK.
    - mismatch but ``now - commanded_at <= grace`` → WITHIN_GRACE
      (tolerate write-propagation lag).
    - mismatch and ``now - commanded_at > grace`` → CONFLICT.

    A commanded *removal* is represented by ``commanded_mode == "SelfUse"``,
    so both conflict directions (override-not-applied and
    override-not-removed) are the same comparison.
    """
    if commanded_mode is None:
        return ReconcileVerdict.OK
    if _norm(reported_mode) == _norm(commanded_mode):
        return ReconcileVerdict.OK
    if now - commanded_at > grace:
        return ReconcileVerdict.CONFLICT
    return ReconcileVerdict.WITHIN_GRACE
