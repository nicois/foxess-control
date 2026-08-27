"""Scheduler-handback policy: should we hand the inverter back, and with what?

When no smart session is running, the integration can turn the inverter's
**Mode Scheduler master switch off** and set the idle state through the
device's own settings instead — which is what closes issue #16 (the switch
left on blocks the user's local Modbus control) and issue #4 (a Min SoC of
0% is only reachable outside the scheduler, because the scheduler declares
``minsocongrid.range.min = 10`` and rejects less with errno 40257).

This module is the *decision* only.  It performs no I/O, imports nothing
from Home Assistant, and returns a value describing what should happen;
the caller does it.  Splitting it this way is what makes every guard
below — each of which is a way this feature could break a working
install — testable exhaustively without HTTP or a running HA.  See
``tests/test_handback_policy.py``.

Brand-specific by nature (the FoxESS Mode Scheduler is a FoxESS concept),
so it lives here rather than in ``smart_battery/`` (C-021, C-039).

Sessions themselves are unaffected however this is configured: the direct
work-mode enumeration contains no forced modes, so a forced charge or
discharge can *only* be expressed as a schedule group.  Handback governs
the **idle** state and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

from .foxess import WorkMode


@dataclass(frozen=True)
class HandbackPlan:
    """What to do about the idle state, decided but not yet done.

    Frozen deliberately: a plan a caller can edit between the decision and
    the execution defeats the reason for separating them, and the specific
    edit worth making impossible is filling in a ``restore_min_soc_on_grid``
    the user never chose.

    ``restore_min_soc_on_grid`` is ``int | None`` rather than ``int``
    because "restore nothing" is a real and common answer — see
    :func:`plan_handback`.  When ``act`` is False every action field is
    inert, so a caller that ignores ``act`` still does no harm.
    """

    act: bool
    reason: str = ""
    disable_scheduler: bool = False
    work_mode: str | None = None
    restore_min_soc_on_grid: int | None = None


def plan_handback(
    *,
    enabled: bool,
    entity_mode: bool,
    session_active: bool,
    unmanaged_modes: list[str],
    scheduler_supported: bool,
    captured_min_soc_on_grid: int | None,
) -> HandbackPlan:
    """Decide whether to hand the inverter back to its own settings.

    *unmanaged_modes* is the descriptive list produced by
    ``check_schedule_conflicts`` (e.g. ``["Backup (00:00-06:00)"]``);
    *captured_min_soc_on_grid* is the user's own persistent Min SoC as it
    was read **before** the integration last touched it, or ``None`` if it
    was never captured.

    **When nothing was captured, nothing is restored.**  A default is
    never substituted, and a captured value is passed through verbatim —
    no clamping to the scheduler's 10% minimum, no rounding.  This
    integration has already shipped a defect where an adapter wrote a
    session value into the persistent Min SoC floor and never put the
    user's value back, so the inverter imported from the grid once the
    session ended.  Choosing a floor is the user's business (P-002); the
    most this may ever do is put back what it found.  A handback with
    nothing to restore still happens — it just leaves the floor alone.

    Guards are evaluated ``enabled`` → ``entity_mode`` →
    ``scheduler_supported`` → ``unmanaged_modes`` → ``session_active``:
    most permanent cause first, so the reason a caller logs names the
    condition that will still be true tomorrow.  A Backup group needs the
    user in the FoxESS app; an active session clears itself within hours,
    and reporting the transient cause over the permanent one would tell a
    user to wait for something that was never going to happen.  Precedence
    only ever selects *which* reason is reported — every guard yields
    ``act=False`` — so no ordering can make the decision less safe.

    In order:

    1. **Not enabled** — opt-in, default off.  Existing installs must
       behave exactly as they did before the upgrade.
    2. **Entity mode** — there is no cloud Mode Scheduler to hand back.
    3. **Scheduler unsupported** — likewise nothing to hand back.
    4. **Unmanaged modes present** — C-018: the schedule contains modes
       this integration does not manage, which the user put there
       deliberately.  Named in the reason so the log is actionable.
    5. **A session is active** — C-025.  Turning the master switch off
       mid-session strands the live override: the group stops driving the
       inverter while the integration still believes it is charging or
       discharging.  A safety divergence, not untidiness.
    """
    if not enabled:
        return HandbackPlan(
            act=False,
            reason=(
                "scheduler handback is not enabled for this inverter "
                "(opt-in integration option, off by default)"
            ),
        )

    if entity_mode:
        return HandbackPlan(
            act=False,
            reason=(
                "entity mode: control goes through Home Assistant entities, "
                "so there is no cloud Mode Scheduler to hand back"
            ),
        )

    if not scheduler_supported:
        return HandbackPlan(
            act=False,
            reason=(
                "this inverter reports no Mode Scheduler support, "
                "so there is nothing to hand back"
            ),
        )

    if unmanaged_modes:
        return HandbackPlan(
            act=False,
            reason=(
                "the inverter schedule contains work modes this integration does "
                "not manage, so it will not be modified (C-018): "
                + ", ".join(unmanaged_modes)
                + " — remove them via the FoxESS app to allow handback"
            ),
        )

    if session_active:
        return HandbackPlan(
            act=False,
            reason=(
                "a smart session is active; handback governs the idle state only, "
                "and disabling the Mode Scheduler now would stop the session's "
                "schedule group driving the inverter (C-025)"
            ),
        )

    return HandbackPlan(
        act=True,
        reason=(
            "no smart session active: returning the inverter to its own "
            "work-mode setting with the Mode Scheduler off"
        ),
        disable_scheduler=True,
        work_mode=WorkMode.SELF_USE.value,
        # Verbatim, including None ("restore nothing") and 0 (issue #4).
        restore_min_soc_on_grid=captured_min_soc_on_grid,
    )
