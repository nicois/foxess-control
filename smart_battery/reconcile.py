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


class CommandKind(enum.Enum):
    """What the integration did to produce the watched mode.

    - ``APPLY``: an override was *applied* (e.g. ForceCharge).  We expect
      the inverter to report that mode; a mismatch means it didn't apply.
    - ``REMOVE``: an override was *removed*.  We expect the inverter to
      report anything *except* the removed mode; still reporting it means
      the removal didn't take.  Reporting SelfUse, or an unrelated managed
      mode (e.g. a standalone Feedin group), is fine.
    """

    APPLY = "apply"
    REMOVE = "remove"


def _norm(mode: str | None) -> str:
    """Normalise a reported/commanded mode; None or empty string reports as SelfUse.

    ``get_current_mode`` returns None when no enabled group covers now —
    the inverter is in self-use.  Treat that as SelfUse so a commanded
    removal (expecting *not* the removed mode) reconciles cleanly.
    """
    return mode if mode else SELF_USE


def reconcile_commanded_mode(
    kind: CommandKind,
    watched_mode: str | None,
    commanded_at: datetime.datetime,
    reported_mode: str | None,
    now: datetime.datetime,
    grace: datetime.timedelta,
) -> ReconcileVerdict:
    """Return the reconciliation verdict for a commanded (kind, watched_mode).

    - ``watched_mode is None`` → OK (nothing has been commanded yet).
    - No mismatch → OK.
    - Mismatch but ``now - commanded_at <= grace`` → WITHIN_GRACE
      (tolerate write-propagation lag).
    - Mismatch and ``now - commanded_at > grace`` → CONFLICT.

    Mismatch is defined by ``kind``:

    - ``APPLY``: mismatch when the inverter does *not* report the watched
      mode (the override we commanded didn't apply).
    - ``REMOVE``: mismatch when the inverter *still* reports the watched
      mode (the removed override is still active).  Any other reported
      mode — SelfUse or an unrelated managed group — is OK.
    """
    if watched_mode is None:
        return ReconcileVerdict.OK

    if kind is CommandKind.REMOVE:
        mismatch = _norm(reported_mode) == _norm(watched_mode)
    else:  # APPLY
        mismatch = _norm(reported_mode) != _norm(watched_mode)

    if not mismatch:
        return ReconcileVerdict.OK
    if now - commanded_at > grace:
        return ReconcileVerdict.CONFLICT
    return ReconcileVerdict.WITHIN_GRACE
