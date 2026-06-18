"""Startup schedule-reconcile: remove orphaned managed schedule groups.

Issue #11 leftover-group defence.  A FoxESS schedule group recurs daily
(no date), so a managed work-mode group (ForceCharge/ForceDischarge/
Feedin) left enabled in the inverter — by a teardown that did not
complete (HA restart mid-session, a write the inverter did not apply, or
a safety-check abort) — re-fires every day until removed.  On startup,
after session recovery, remove any such group that no recovered session
covers.  Cloud backend only; startup only.  See
docs/superpowers/specs/2026-06-18-startup-schedule-reconcile-design.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .foxess import WorkMode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .foxess.inverter import Inverter

_LOGGER = logging.getLogger(__name__)

# Managed override modes (the managed set minus SelfUse).  An enabled
# group in one of these modes that no session covers is an orphan.
_MANAGED_OVERRIDE_MODES = (
    WorkMode.FORCE_CHARGE.value,
    WorkMode.FORCE_DISCHARGE.value,
    WorkMode.FEEDIN.value,
)


def find_orphan_modes(
    groups: list[dict[str, Any]],
    covered_modes: set[str],
) -> list[str]:
    """Return managed-override work modes present as orphaned groups.

    An orphan is an *enabled* group whose ``workMode`` is a managed
    override mode (ForceCharge/ForceDischarge/Feedin) and is NOT in
    *covered_modes* (the modes a recovered/active session legitimately
    owns).  SelfUse, disabled groups, and unmanaged modes (e.g. Backup)
    are never orphans.  Order-preserving, de-duplicated.
    """
    orphans: list[str] = []
    for group in groups:
        if group.get("enable") != 1:
            continue
        mode = group.get("workMode", "")
        if mode not in _MANAGED_OVERRIDE_MODES:
            continue
        if mode in covered_modes:
            continue
        if mode not in orphans:
            orphans.append(mode)
    return orphans


async def reconcile_schedule(hass: HomeAssistant, inverter: Inverter | None) -> None:
    """Remove orphaned managed schedule groups left in the inverter.

    Runs once on startup, AFTER session recovery, so the live session
    state (``smart_charge_state`` / ``smart_discharge_state``) reflects
    any resumed session.  Cloud backend only.  Never raises — must not
    break integration setup.
    """
    from homeassistant.util import dt as dt_util

    from ._helpers import _cfg, _dd
    from .foxess_adapter import (
        _MANAGED_WORK_MODES,
        _is_placeholder,
        _recent_errors,
        _remove_mode_from_schedule,
    )
    from .smart_battery.logging import record_operational_error

    try:
        if inverter is None or _cfg(hass).entity_mode:
            return
        dd = _dd(hass)

        # 1. Fetch the live schedule (blocking → executor).
        try:
            schedule = await hass.async_add_executor_job(inverter.get_schedule)
        except Exception as err:  # noqa: BLE001 — must not break setup
            dd.last_schedule_reconcile = {
                "action": "fetch_failed",
                "orphans": [],
                "detail": str(err),
            }
            _LOGGER.warning("Startup schedule reconcile: get_schedule failed: %s", err)
            return

        groups = [g for g in schedule.get("groups", []) if not _is_placeholder(g)]
        # 2. Cache the snapshot regardless of outcome (feature B).
        dd.last_schedule_snapshot = [dict(g) for g in groups]
        dd.last_schedule_snapshot_at = dt_util.utcnow().isoformat()

        # 3. Which managed-override modes does a recovered session cover?
        covered: set[str] = set()
        cs = dd.smart_charge_state
        if cs is not None and _covers(cs, groups, WorkMode.FORCE_CHARGE.value):
            covered.add(WorkMode.FORCE_CHARGE.value)
        ds = dd.smart_discharge_state
        if ds is not None:
            for m in (WorkMode.FORCE_DISCHARGE.value, WorkMode.FEEDIN.value):
                if _covers(ds, groups, m):
                    covered.add(m)

        orphans = find_orphan_modes(groups, covered)
        if not orphans:
            dd.last_schedule_reconcile = {
                "action": "none",
                "orphans": [],
                "detail": "no orphaned managed group",
            }
            return

        # 4. C-018 guard: any UNmanaged group present → do not write.
        has_unmanaged = any(
            g.get("workMode") and g.get("workMode") not in _MANAGED_WORK_MODES
            for g in groups
        )
        if has_unmanaged:
            dd.last_schedule_reconcile = {
                "action": "blocked_unmanaged",
                "orphans": orphans,
                "detail": "unmanaged work-mode group present; schedule not modified",
            }
            _LOGGER.warning(
                "Startup schedule reconcile: found orphaned %s but an unmanaged "
                "mode is present — not modifying the schedule (C-018)",
                orphans,
            )
            record_operational_error(
                _LOGGER,
                _recent_errors(hass),
                category="orphaned_schedule_blocked",
                attempted="startup schedule reconcile",
                exc=_OrphanedSchedule(
                    f"orphaned {orphans} not removed: unmanaged mode present"
                ),
                hint=(
                    "a recurring managed schedule group was left in the inverter "
                    "but an unmanaged mode (e.g. Backup) is also present, so it was "
                    "not removed automatically — remove it via the FoxESS app"
                ),
                context={"orphans": orphans},
            )
            return

        # 5. Remove each orphan via the existing primitive.
        min_soc = _cfg(hass).min_soc_on_grid
        removed: list[str] = []
        for mode_str in orphans:
            try:
                await hass.async_add_executor_job(
                    _remove_mode_from_schedule, inverter, WorkMode(mode_str), min_soc
                )
                removed.append(mode_str)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Startup schedule reconcile: failed to remove %s: %s",
                    mode_str,
                    err,
                )

        dd.last_schedule_reconcile = {
            "action": "removed" if removed else "remove_failed",
            "orphans": orphans,
            "detail": f"removed {removed}",
        }
        if removed:
            _LOGGER.warning(
                "Startup schedule reconcile: removed orphaned %s group(s) left "
                "from a prior session — no active session covers them",
                removed,
            )
            record_operational_error(
                _LOGGER,
                _recent_errors(hass),
                category="orphaned_schedule_removed",
                attempted="startup schedule reconcile",
                exc=_OrphanedSchedule(f"removed orphaned managed group(s): {removed}"),
                hint=(
                    "a recurring managed schedule group was left in the inverter "
                    "with no active session — likely a teardown that did not "
                    "complete (HA restart mid-session, or a write the inverter did "
                    "not apply); it has been removed"
                ),
                context={"removed": removed},
            )
    except Exception:  # noqa: BLE001 — reconcile must never break setup
        _LOGGER.debug("Startup schedule reconcile failed (non-critical)", exc_info=True)


def _covers(session: dict[str, Any], groups: list[dict[str, Any]], mode: str) -> bool:
    """True if *session* legitimately owns a group of *mode* in *groups*.

    A session covers a group when the session's start/end window matches an
    enabled group of that mode (hour+minute).  The session dict carries
    ``start``/``end`` datetimes (set during recovery).
    """
    start = session.get("start")
    end = session.get("end")
    if start is None or end is None:
        # A session exists but without a window — conservatively treat its
        # mode as covered so we never remove a group for an active session.
        return True
    for g in groups:
        if g.get("enable") != 1 or g.get("workMode") != mode:
            continue
        if (
            g.get("startHour") == start.hour
            and g.get("startMinute") == start.minute
            and g.get("endHour") == end.hour
            and g.get("endMinute") == end.minute
        ):
            return True
    return False


class _OrphanedSchedule(Exception):
    """Constructed (not raised into control flow) to give record_operational_error
    a BaseException with a useful message."""
