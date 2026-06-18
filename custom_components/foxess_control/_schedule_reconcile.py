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
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Managed override modes (the managed set minus SelfUse).  An enabled
# group in one of these modes that no session covers is an orphan.
_MANAGED_OVERRIDE_MODES = ("ForceCharge", "ForceDischarge", "Feedin")


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
