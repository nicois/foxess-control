"""Execute the scheduler-handback plan: at teardown, or when asked.

``handback.plan_handback`` decides; this module does.  It is the only place
in the integration that turns the Mode Scheduler master switch **off**, and
at a session boundary it is deliberately the last thing to happen.

Two entry points, one executor:

* :func:`async_handback_after_teardown` — automatic, gated on the opt-in
  option, and **never raises** (see below).
* :func:`async_handback_on_request` — the ``disable_scheduler`` action
  (issue #16 asked for it by name).  It does **not** consult the option,
  and it **raises** ``ServiceValidationError`` on a refusal.

Both differences are deliberate and are argued at
:func:`async_handback_on_request`.  Everything else — every guard, the
order, the per-step failure handling, the outcome record — is shared, so
"the action applies the same policy" is a property of there being one
implementation rather than of two of them agreeing.

**The order is load-bearing.**

    remove the managed group → master switch off → work mode → Min SoC

Removing the group first means a failure part-way through can never leave a
*forced discharge or charge* running behind a disabled scheduler.  With the
switch off, schedule groups are inert; with the switch off *and* a forced
group still on the device, the integration has lost the ability to steer an
inverter that is still discharging — no longer a tidiness problem but a
P-001 one.  So the group removal is the caller's job and has already
succeeded by the time anything here runs: every hook calls this *after* its
own removal, never before, and never in the same try block.

Correspondingly, **nothing on the teardown path may raise**.  The caller's
removal has already happened; the teardown paths in
``smart_battery/listeners.py`` treat an exception out of ``remove_override``
as "the override is still on" and queue a retry via
``pending_override_cleanup``.  A handback failure that propagated would
therefore be reported to the user as a failed override removal, which is
both wrong and alarming.  C-025 outranks this whole feature (P-002 over
P-005): the override coming off must never be made conditional on the
inverter being handed back.

:func:`async_handback_on_request` is the one exception, and only because it
is not on that path: no override has just come off, nothing is waiting to
be retried, and a user is holding a service call open.  It raises on a
refusal for the same reason the teardown must not — whoever is listening
needs the truth (C-020).  A device that refuses a *write* still does not
raise, on either path.

Three step failures are independent on purpose.  If the master switch
write fails, the work mode and the Min SoC restore are still attempted:
putting the user's own floor back is worth doing whether or not the
scheduler could be released (P-002), and the direct work-mode setting only
governs while no group is in force, so writing it early is harmless.  Each
failure is recorded — a handback that fails silently is worse than one that
never runs, because the user sees the option switched on and believes their
inverter was released (C-020, C-026, D-059).

Brand-specific (the FoxESS Mode Scheduler is a FoxESS concept), so it lives
here rather than in ``smart_battery/`` (C-021, C-039).
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .handback import plan_handback

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .domain_data import FoxESSControlData
    from .foxess.inverter import Inverter
    from .handback import HandbackPlan

_LOGGER = logging.getLogger(__name__)

# The ``category`` every operational error from this module carries, and the
# prefix of its ``dedupe_key``.  Part of the contract the diagnostics
# download reads, so it is one constant rather than four string literals.
_CATEGORY = "scheduler_handback"

# The device setting that is the same register as ``minSocOnGrid`` in
# ``battery/soc/{get,set}``.  Written through the settings surface, never
# through ``set_min_soc`` — see ``_min_soc_capture``.
_SETTING_MIN_SOC_ON_GRID = "MinSocOnGrid"

_STEP_DISABLE = "disable_scheduler"
_STEP_WORK_MODE = "work_mode"
_STEP_MIN_SOC = "min_soc_on_grid"

_OK = "ok"
_FAILED = "failed"
_SKIPPED = "skipped"
_REVERTED = "reverted"
_REVERT_FAILED = "revert_failed"


async def async_handback_after_teardown(
    hass: HomeAssistant, inverter: Inverter | None
) -> None:
    """Hand the inverter back to its own settings, if that is warranted.

    Call this **after** a managed override group has been successfully
    removed, from outside the try block that guards the removal.  Never
    raises, and on the shipped default (handback off) makes no API call at
    all — not merely no writes.
    """
    try:
        await _handback(hass, inverter)
    except Exception as err:  # noqa: BLE001 — the teardown must not fail here
        # Everything below already handles its own failures, so reaching
        # this is a bug in this module rather than an inverter problem.  Both
        # channels still have to carry it: silence would make the feature
        # look like it had run (C-026), and the outcome record would
        # otherwise keep the *previous* session's answer — a download
        # reading "acted: true, every step ok" for a teardown that crashed.
        # A stale record is a subtler lie than a missing one, so it is the
        # one worth clearing.
        _record_error(
            hass,
            attempted="scheduler handback at session teardown",
            exc=err,
            hint=(
                "the scheduler handback failed unexpectedly; the session's "
                "override was still removed, and the inverter has been left "
                "under Mode Scheduler control"
            ),
            dedupe_key=f"{_CATEGORY}:unexpected",
        )
        # Best-effort, and second: ``_record_outcome`` is itself a plausible
        # cause of getting here, in which case this cannot work and the ring
        # buffer above is the only surviving channel.  Whatever happens, the
        # record is replaced wholesale or not at all — never half-written,
        # which would have the download describing a handback in a shape
        # that never occurred.
        _record_outcome_best_effort(
            hass,
            _declined(
                "the scheduler handback failed unexpectedly, so the inverter "
                f"has been left under Mode Scheduler control ({err!r}) — the "
                "session's override was still removed"
            ),
        )


async def _handback(hass: HomeAssistant, inverter: Inverter | None) -> None:
    from ._helpers import _cfg, _dd

    dd = _dd(hass)
    plan = await _decide(hass, dd, inverter, enabled=_cfg(hass).scheduler_handback)
    if not plan.act or inverter is None:
        _record_outcome(dd, plan, steps={})
        _LOGGER.debug("Scheduler handback declined: %s", plan.reason)
        return

    await _execute(hass, dd, inverter, plan)


async def async_handback_on_request(
    hass: HomeAssistant, inverter: Inverter | None
) -> None:
    """Release the inverter now, because the user asked (issue #16).

    Backs the ``foxess_control.disable_scheduler`` action.  Two deliberate
    differences from :func:`async_handback_after_teardown`, and no others:

    **1. The opt-in option is not consulted.**  ``enabled=True`` is passed
    to the policy unconditionally, because the service call *is* the
    consent.  The option exists so that an upgrade does not start writing
    to hundreds of inverters unasked; it governs the *automatic* behaviour
    at a session boundary.  Making the action honour it as well would leave
    the person in issue #16 — who wants their inverter released but not
    after every session — with an action that refuses until they enable the
    thing they were avoiding, i.e. with no action at all.

    This is not a licence to invent a Min SoC.  The floor still comes from
    ``dd.captured_min_soc_on_grid`` and nowhere else, and ``None`` still
    means restore nothing: capture is ungated (only the *write* half of
    ``_min_soc_capture`` checks the option), so an install that never opted
    in already knows the user's own floor and can put exactly that back.
    P-002 is not something an explicit call can waive.

    **2. A refusal raises.**  The teardown hook must never raise — the
    listener reads an exception out of ``remove_override`` as "the override
    is still on" and queues a retry — but here there is a user waiting on a
    service call, and an action that silently does nothing is worse than no
    action, because they will conclude their inverter was released (C-020).
    Every ``act=False`` plan therefore becomes a
    ``ServiceValidationError`` naming the reason, including the
    unmanaged-mode refusal, which names the offending mode so "remove it in
    the FoxESS app" is actionable advice (C-018).

    A **step** failure does not raise, and that asymmetry is the point: a
    refusal happens before anything is attempted, so "nothing happened,
    here is why" is the whole truth.  A step failure happens after all
    three independent steps have been attempted and some may have taken
    effect, so an exception would misreport a partial handback as a total
    one.  Those are surfaced where partial truths belong — the outcome
    record and the recent-errors buffer (C-026, D-059).

    Deliberately **not** a session teardown: no schedule group is added or
    removed.  ``clear_overrides`` already does that, and a user asking for
    local control back should not silently lose a group they built.
    """
    from ._helpers import _dd

    dd = _dd(hass)
    plan = await _decide(hass, dd, inverter, enabled=True)
    if not plan.act or inverter is None:
        _record_outcome(dd, plan, steps={})
        _LOGGER.info("Refused to release the inverter: %s", plan.reason)
        raise _refused(plan.reason)

    # A race with a session that started mid-flight is a refusal too — the
    # executor has already put the master switch back and abandoned the
    # rest, so the caller must not be told the inverter was released.
    refusal = await _execute(hass, dd, inverter, plan)
    if refusal is not None:
        raise _refused(refusal)


async def _decide(
    hass: HomeAssistant,
    dd: FoxESSControlData,
    inverter: Inverter | None,
    *,
    enabled: bool,
) -> HandbackPlan:
    """Build the plan, paying for inputs only when they can change it.

    Shared by both entry points, so "the action applies the same guards" is
    a property of there being one implementation rather than of two of them
    happening to agree.  *enabled* is the only input that differs: the
    teardown hook passes the option, the action passes ``True``.
    """
    from ._helpers import _cfg

    cfg = _cfg(hass)

    # The session snapshot is taken FIRST, before any I/O, so that the
    # window between it and the first write is exactly the window
    # ``_execute``'s re-checks cover.  Gathering it late would shrink the
    # window while making it untestable, which is the worse trade.
    session_active = _session_active(dd)

    if not enabled or cfg.entity_mode or inverter is None:
        # Decline without touching the network.  ``plan_handback`` would
        # decline on any of these three (``scheduler_supported=None`` is
        # the "could not determine" decline, which is the honest answer
        # when there is no inverter to ask), so this is not a second
        # decision — it is a decision made without paying for inputs that
        # cannot change it.  That distinction is what makes "a default
        # install issues no new API calls" true rather than aspirational:
        # probing scheduler support on every session boundary of every
        # install that never opted in would be a real cost.
        return plan_handback(
            enabled=enabled,
            entity_mode=cfg.entity_mode,
            session_active=session_active,
            unmanaged_modes=[],
            scheduler_supported=None,
            captured_min_soc_on_grid=None,
        )

    unmanaged = await _unmanaged_modes(hass, inverter)
    if unmanaged is None:
        # A failed schedule read is not a policy decision: with the group
        # list unknown, whether C-018 applies is unknown too, and the safe
        # answer to an unknown is to leave the inverter alone.  Reported as
        # its own reason rather than squeezed into one of plan_handback's,
        # because "could not find out" and "found out and declined" send a
        # user to look in completely different places (C-020).
        return _declined(
            "could not read the inverter schedule, so whether it contains "
            "work modes this integration does not manage is unknown — the "
            "inverter was left exactly as it is"
        )

    return plan_handback(
        enabled=True,
        entity_mode=False,
        session_active=session_active,
        unmanaged_modes=unmanaged,
        scheduler_supported=await hass.async_add_executor_job(
            inverter.probe_scheduler_support
        ),
        captured_min_soc_on_grid=dd.captured_min_soc_on_grid,
    )


def _refused(reason: str) -> ServiceValidationError:
    """The exception a refused ``disable_scheduler`` call raises.

    The reason is passed both positionally (so logs and tests see it
    without a translation cache) and as a placeholder (so the HA UI renders
    the localised wrapper).  ``ServiceValidationError`` rather than
    ``HomeAssistantError`` because nothing went wrong — the request was
    valid and the answer is no.
    """
    return ServiceValidationError(
        f"The inverter was not released from Mode Scheduler control: {reason}",
        translation_domain=DOMAIN,
        translation_key="handback_refused",
        translation_placeholders={"reason": reason},
    )


async def _execute(
    hass: HomeAssistant,
    dd: FoxESSControlData,
    inverter: Inverter,
    plan: HandbackPlan,
) -> str | None:
    """Carry out *plan*, one independently-failing step at a time.

    Returns ``None`` when the plan was carried out (whether or not its
    individual steps succeeded — those are reported through the outcome
    record and the error buffer), or the refusal reason when a race with a
    starting session made it abandon.  The teardown hook ignores the
    return; the ``disable_scheduler`` action turns it into the exception the
    caller sees, because the one thing worse than the action refusing is
    the action refusing quietly (C-020).
    """
    steps: dict[str, str] = {}

    # --- Race guard, part 1: re-check immediately before the first write.
    #
    # The ``session_active`` the plan was made from was a snapshot taken
    # before two awaits' worth of I/O, and a smart-session service call can
    # be served on the event loop in between.  Acting on it stale would
    # turn the master switch off under a session that has just written its
    # schedule group, leaving the group inert: the integration would
    # believe it is discharging while the inverter does nothing (C-025).
    # Cheap, so it is not an optimisation to skip.
    if _session_active(dd):
        _record_outcome(dd, _declined(_RACE_REASON), steps=steps)
        _LOGGER.info("Scheduler handback abandoned: %s", _RACE_REASON)
        return _RACE_REASON

    # --- Step 1: the Mode Scheduler master switch.
    if plan.disable_scheduler:
        steps[_STEP_DISABLE] = await _step(
            hass,
            _STEP_DISABLE,
            lambda: inverter.set_scheduler_enabled(False),
            attempted="turn the Mode Scheduler master switch off",
            hint=(
                "the inverter would not release Mode Scheduler control, so it "
                "still ignores its own work-mode setting and its Min SoC "
                "cannot go below the scheduler's 10% floor; some firmware and "
                "regions do not serve this endpoint at all"
            ),
        )

        # --- Race guard, part 2: no pre-check can close the window, so it
        # self-heals.  A session that started while the switch was going
        # off has already written (or is about to write) a group that
        # cannot drive the inverter, so the switch goes straight back on
        # and the rest of the handback is abandoned.  Task 1's
        # ``_ensure_scheduler_enabled`` makes the worst case survivable,
        # but a session whose group was written *before* the switch went
        # off would not be re-enabled by anything else.
        if _session_active(dd):
            await _reenable_for_session(hass, inverter, steps)
            _record_outcome(dd, _declined(_RACE_REASON), steps=steps)
            _LOGGER.warning("Scheduler handback reverted: %s", _RACE_REASON)
            return _RACE_REASON

    # --- Step 2: the device's own work-mode setting.
    #
    # Only meaningful once the switch is off — while a group is in force
    # this setting is invisible — but attempted even if step 1 failed,
    # because it is harmless then and correct the moment the switch does
    # come off.
    work_mode = plan.work_mode
    if work_mode is None:
        steps[_STEP_WORK_MODE] = _SKIPPED
    else:
        steps[_STEP_WORK_MODE] = await _step(
            hass,
            _STEP_WORK_MODE,
            lambda: inverter.set_work_mode_direct(work_mode),
            attempted=f"set the inverter's own work mode to {work_mode}",
            hint=(
                "with Mode Scheduler off, this setting is what the inverter "
                "actually does — if it could not be written the inverter may "
                "be idling in some other mode"
            ),
        )

    # --- Step 3: the user's own persistent Min SoC floor.
    #
    # ``None`` means nothing was ever captured, which means restore
    # nothing.  A default is never substituted: choosing a floor is the
    # user's business (P-002), and the most this may do is put back what it
    # found — verbatim, 0 included (issue #4).
    floor = plan.restore_min_soc_on_grid
    if floor is None:
        steps[_STEP_MIN_SOC] = _SKIPPED
        _LOGGER.debug(
            "Scheduler handback: no captured Min SoC on grid, so the "
            "inverter's own floor is left untouched"
        )
    else:
        steps[_STEP_MIN_SOC] = await _step(
            hass,
            _STEP_MIN_SOC,
            lambda: inverter.set_setting(_SETTING_MIN_SOC_ON_GRID, str(floor)),
            attempted=f"restore your own Min SoC on grid ({floor}%)",
            hint=(
                "the inverter may still be holding a Min SoC floor from the "
                "session, which makes it import from the grid to maintain a "
                "level you did not choose"
            ),
        )

    _record_outcome(dd, plan, steps=steps)
    if all(state == _OK for state in steps.values() if state != _SKIPPED):
        _LOGGER.info(
            "Scheduler handback: Mode Scheduler off, work mode %s, Min SoC on "
            "grid %s — the inverter is back under its own settings",
            plan.work_mode,
            "left as it was" if floor is None else f"{floor}%",
        )
    return None


_RACE_REASON = (
    "a smart session started while the handback was in progress, so the "
    "Mode Scheduler was left on: disabling it now would stop the new "
    "session's schedule group driving the inverter (C-025)"
)


async def _step(
    hass: HomeAssistant,
    step: str,
    action: Callable[[], None],
    *,
    attempted: str,
    hint: str,
) -> str:
    """Run one blocking handback write in the executor, absorbing failure.

    Returns ``"ok"`` or ``"failed"``.  Steps are independent: one that
    fails must not stop the others, because each does something useful on
    its own.
    """
    try:
        await hass.async_add_executor_job(action)
    except Exception as err:  # noqa: BLE001 — a step failure is not fatal
        _record_error(
            hass,
            attempted=f"scheduler handback: {attempted}",
            exc=err,
            hint=hint,
            dedupe_key=f"{_CATEGORY}:{step}",
        )
        return _FAILED
    return _OK


async def _reenable_for_session(
    hass: HomeAssistant, inverter: Inverter, steps: dict[str, str]
) -> None:
    """Put the master switch back on after losing the race to a session."""
    try:
        await hass.async_add_executor_job(inverter.set_scheduler_enabled, True)
        steps[_STEP_DISABLE] = _REVERTED
    except Exception as err:  # noqa: BLE001 — already the unhappy path
        steps[_STEP_DISABLE] = _REVERT_FAILED
        _record_error(
            hass,
            attempted=(
                "turn the Mode Scheduler master switch back on after a smart "
                "session started mid-handback"
            ),
            exc=err,
            hint=(
                "the new session's schedule group may not drive the inverter "
                "until the next schedule write re-enables the switch; check "
                "that Mode Scheduler is enabled in the FoxESS app if the "
                "session appears to do nothing"
            ),
            dedupe_key=f"{_CATEGORY}:revert",
        )


def _session_active(dd: FoxESSControlData) -> bool:
    """Is any smart session live right now?

    Either family counts.  A charge session outliving a discharge teardown
    still owns the inverter, and handback governs the idle state only.
    """
    return dd.smart_charge_state is not None or dd.smart_discharge_state is not None


async def _unmanaged_modes(hass: HomeAssistant, inverter: Inverter) -> list[str] | None:
    """Descriptions of unmanaged schedule groups, or None if unreadable.

    ``[]`` means "read it, nothing unmanaged"; ``None`` means "could not
    find out".  Collapsing the two would turn an API blip into a confident
    claim that the user's schedule is ours to rewrite (C-018).
    """
    from .foxess_adapter import _is_placeholder, check_schedule_conflicts

    try:
        schedule = await hass.async_add_executor_job(inverter.get_schedule)
    except Exception as err:  # noqa: BLE001 — unknown is a valid answer
        _record_error(
            hass,
            attempted="scheduler handback: read the inverter schedule",
            exc=err,
            hint=(
                "the schedule could not be read, so the handback could not "
                "check for work modes this integration does not manage and "
                "left the inverter alone"
            ),
            dedupe_key=f"{_CATEGORY}:schedule_read",
        )
        return None
    groups = [g for g in schedule.get("groups", []) if not _is_placeholder(g)]
    return check_schedule_conflicts(groups)


def _declined(reason: str) -> HandbackPlan:
    """A decline this module reached itself, in ``plan_handback``'s shape.

    Used only where the policy layer has no input to express the situation
    — an unreadable schedule, a session that appeared after the plan was
    made, or a bug in this module.  Every action field stays at its inert
    default, so a caller that ignored ``act`` still could not do anything
    with it.
    """
    from .handback import HandbackPlan

    return HandbackPlan(act=False, reason=reason)


def _record_outcome_best_effort(hass: HomeAssistant, plan: HandbackPlan) -> None:
    """Record *plan* as the outcome, swallowing anything that goes wrong.

    For the top-level guard only.  Two of the things it needs — the domain
    data, and :func:`_record_outcome` itself — are plausible causes of
    getting there, so this must be able to fail without re-entering the
    guard it was called from.  When it does fail the record simply keeps its
    previous contents, which the ring buffer's entry then contradicts; that
    is the acceptable outcome, and a partially-written record is not.
    """
    try:
        from ._helpers import _dd

        _record_outcome(_dd(hass), plan, steps={})
    except Exception:  # noqa: BLE001 — the last-resort path cannot itself fail
        _LOGGER.debug(
            "Could not record the scheduler-handback outcome; the recent-errors "
            "buffer is the only remaining account of this teardown",
            exc_info=True,
        )


def _record_outcome(
    dd: FoxESSControlData, plan: HandbackPlan, *, steps: dict[str, str]
) -> None:
    """Leave a last-outcome record for the diagnostics download.

    Recorded on *every* path, including the declines, because "nothing
    happened, and here is why" is the answer a user needs when their
    inverter is still scheduler-controlled — and the reason a decline gives
    is the actionable half (C-020, P-005).  Same shape as
    ``last_schedule_reconcile``: a plain dict, so the diagnostics exporter
    needs no knowledge of this module.

    ``restored_min_soc_on_grid`` reports what actually reached the device,
    not what was intended.  Reporting the plan's value after the write
    failed would be the record asserting the user's floor was put back when
    it was not — precisely the lie this whole feature has to avoid (P-002).
    """
    restored = plan.restore_min_soc_on_grid if steps.get(_STEP_MIN_SOC) == _OK else None
    dd.last_handback = {
        "t": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "acted": bool(plan.act),
        "reason": plan.reason,
        "steps": dict(steps),
        "restored_min_soc_on_grid": restored,
    }


def _record_error(
    hass: HomeAssistant,
    *,
    attempted: str,
    exc: BaseException,
    hint: str,
    dedupe_key: str,
) -> None:
    """Log and record a handback failure, best-effort.

    *dedupe_key* is always supplied: a teardown that fails does so at every
    session boundary against the same cause, and without collapsing, one
    broken install would fill all 30 slots of the ring buffer and destroy
    the diagnostic value of everything else in the download.  Collapsing
    bounds the buffer, never the log.
    """
    from .foxess_adapter import _recent_errors
    from .smart_battery.logging import record_operational_error

    try:
        record_operational_error(
            _LOGGER,
            _recent_errors(hass),
            category=_CATEGORY,
            attempted=attempted,
            exc=exc,
            hint=hint,
            dedupe_key=dedupe_key,
        )
    except Exception:  # noqa: BLE001 — recording must never mask the teardown
        _LOGGER.debug("Could not record a scheduler-handback failure", exc_info=True)
