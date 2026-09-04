"""Scheduler-handback policy: the decision, separated from the doing.

``plan_handback`` answers two questions about the *idle* state — should the
integration hand the inverter back to its own settings, and what exactly
should it put back — and answers them as a pure function, so every guard
is testable without HTTP, without a simulator and without Home Assistant.
This is the one module in the handback feature where mocking nothing and
driving nothing is the correct choice (C-028's simulator rule is about
I/O, and there is none here).

The guards are not tidiness.  Each one is a way the feature could damage a
working install:

* **Not enabled** — the option is opt-in and defaults off.  Hundreds of
  installs must behave exactly as they do today after an upgrade.
* **Entity mode** — there is no cloud Mode Scheduler to hand back.
* **A session is active** — disabling the scheduler mid-session strands
  the active override: the group stops driving the inverter while the
  integration still believes it is charging or discharging.  That is a
  safety divergence, not an aesthetic one (C-025).
* **Unmanaged modes present** — C-018.  The user put that Backup group
  there deliberately; we do not touch a schedule we do not own.
* **Scheduler unsupported** — nothing to hand back.
* **The master switch cannot be written** — issue #17: an H3-12.0-E whose
  ``/op/v0/device/scheduler/set`` answers HTTP 404.  The device *has* a Mode
  Scheduler; there is simply no way to turn it off, so handback is
  impossible on that hardware and must say so rather than declining
  silently at every session boundary forever.

And the single most important behaviour, which is why
``restore_min_soc_on_grid`` is ``int | None`` rather than ``int``: when
nothing was captured, **nothing is restored**.  A default is never
substituted.  This project has already shipped a defect where an adapter
wrote a session value into the persistent Min SoC floor and never put the
user's value back, so the inverter imported from the grid after the
session ended.  Choosing a floor is the user's business (P-002); the most
this feature may ever do is put back what it found.
"""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.foxess_control.handback import HandbackPlan, plan_handback

# The situation in which handback SHOULD act: enabled, cloud backend, no
# session, no unmanaged modes, scheduler present, a captured floor to put
# back.  Each test overrides only the field it is about, so a test never
# accidentally passes because two guards were tripped at once.
_ACTS = {
    "enabled": True,
    "entity_mode": False,
    "session_active": False,
    "unmanaged_modes": [],
    "scheduler_supported": True,
    "scheduler_set_unavailable": False,
    "captured_min_soc_on_grid": 20,
}


def _plan(**overrides: object) -> HandbackPlan:
    """Plan for the acting situation with *overrides* applied."""
    return plan_handback(**{**_ACTS, **overrides})  # type: ignore[arg-type]


class TestActs:
    """The whole point: with nothing in the way, hand back."""

    def test_acts(self) -> None:
        assert _plan().act is True

    def test_disables_the_scheduler(self) -> None:
        # Issue #16: the master switch left on is what blocks the user's
        # own local Modbus control, so turning it off IS the feature.
        assert _plan().disable_scheduler is True

    def test_sets_self_use_directly(self) -> None:
        # The direct work-mode enumeration has no forced modes, so SelfUse
        # is the only sane idle state to write off the scheduler.
        assert _plan().work_mode == "SelfUse"

    def test_restores_the_captured_floor(self) -> None:
        assert _plan(captured_min_soc_on_grid=20).restore_min_soc_on_grid == 20


class TestNeverInventsAMinSoc:
    """Nothing captured means nothing restored — never a default."""

    def test_never_invents_a_min_soc(self) -> None:
        plan = _plan(captured_min_soc_on_grid=None)
        assert plan.restore_min_soc_on_grid is None

    def test_still_hands_back_without_a_captured_floor(self) -> None:
        # Not having captured a floor is no reason to leave the scheduler
        # on: the handback still happens, it just restores nothing.
        plan = _plan(captured_min_soc_on_grid=None)
        assert plan.act is True
        assert plan.disable_scheduler is True
        assert plan.work_mode == "SelfUse"

    @pytest.mark.parametrize("captured", [0, 1, 7, 10, 11, 99, 100])
    def test_restores_verbatim(self, captured: int) -> None:
        # Verbatim means verbatim: no clamping to the scheduler's 10%
        # minimum, no rounding, no "sensible" substitution.  0 is the
        # whole of issue #4 — reachable as a device setting but not as a
        # schedule group — so a clamp here would silently defeat it.
        assert (
            _plan(captured_min_soc_on_grid=captured).restore_min_soc_on_grid == captured
        )


class TestNotEnabled:
    """Opt-in, default off: the upgrade-safety guarantee."""

    def test_does_not_act(self) -> None:
        assert _plan(enabled=False).act is False

    def test_touches_nothing(self) -> None:
        plan = _plan(enabled=False)
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None

    def test_reason_points_at_the_option(self) -> None:
        reason = _plan(enabled=False).reason.lower()
        assert reason
        assert "enabl" in reason


class TestEntityMode:
    """No cloud scheduler exists in entity mode."""

    def test_does_not_act(self) -> None:
        assert _plan(entity_mode=True).act is False

    def test_touches_nothing(self) -> None:
        plan = _plan(entity_mode=True)
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None

    def test_reason_names_entity_mode(self) -> None:
        reason = _plan(entity_mode=True).reason.lower()
        assert "entity mode" in reason


class TestSessionActive:
    """C-025: never strand a live override."""

    def test_does_not_act(self) -> None:
        assert _plan(session_active=True).act is False

    def test_touches_nothing(self) -> None:
        plan = _plan(session_active=True)
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None

    def test_reason_says_a_session_is_active(self) -> None:
        reason = _plan(session_active=True).reason.lower()
        assert "session" in reason


class TestUnmanagedModes:
    """C-018: refuse to touch a schedule containing modes we do not own."""

    def test_does_not_act(self) -> None:
        assert _plan(unmanaged_modes=["Backup (00:00-06:00)"]).act is False

    def test_touches_nothing(self) -> None:
        plan = _plan(unmanaged_modes=["Backup (00:00-06:00)"])
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None

    def test_reason_names_the_mode(self) -> None:
        # A log line saying "unmanaged mode present" sends the user
        # hunting through the FoxESS app; one naming the group does not.
        reason = _plan(unmanaged_modes=["Backup (00:00-06:00)"]).reason
        assert "Backup (00:00-06:00)" in reason

    def test_reason_names_every_mode(self) -> None:
        modes = ["Backup (00:00-06:00)", "PeakShaving (17:00-19:00)"]
        reason = _plan(unmanaged_modes=modes).reason
        for mode in modes:
            assert mode in reason


class TestSchedulerUnsupported:
    """Nothing to hand back on a device with no Mode Scheduler."""

    def test_does_not_act(self) -> None:
        assert _plan(scheduler_supported=False).act is False

    def test_touches_nothing(self) -> None:
        plan = _plan(scheduler_supported=False)
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None

    def test_reason_mentions_the_scheduler(self) -> None:
        reason = _plan(scheduler_supported=False).reason.lower()
        assert "scheduler" in reason


class TestSchedulerSupportUnknown:
    """``None`` means the probe failed — say so, do not invent a diagnosis.

    ``scheduler_supported`` is a tri-state because "the device says it has
    no Mode Scheduler" and "we could not find out" are different facts with
    the same consequence.  Collapsing them (as an all-False degrade on a
    malformed reply did) makes one transient bad reply produce a confident
    claim about the user's hardware, sending them to look for a firmware
    limitation that does not exist.  A log that lies is worse than one that
    says "unknown" (C-020, P-005).

    The *decision* is identical — decline, touch nothing — so widening the
    parameter cannot make handback less safe; only the reason changes.
    """

    def test_does_not_act(self) -> None:
        assert _plan(scheduler_supported=None).act is False

    def test_touches_nothing(self) -> None:
        plan = _plan(scheduler_supported=None)
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None

    def test_reason_admits_it_could_not_be_determined(self) -> None:
        reason = _plan(scheduler_supported=None).reason.lower()
        assert "could not determine" in reason

    def test_reason_does_not_claim_the_hardware_lacks_a_scheduler(self) -> None:
        reason = _plan(scheduler_supported=None).reason.lower()
        assert "reports no mode scheduler support" not in reason

    def test_unknown_and_unsupported_read_differently(self) -> None:
        assert (
            _plan(scheduler_supported=None).reason
            != _plan(scheduler_supported=False).reason
        )


class TestSchedulerSetUnavailable:
    """Issue #17: the device has a Mode Scheduler, but no way to switch it off.

    Reported on an **H3-12.0-E** running 1.0.22-beta.6::

        Could not turn the Mode Scheduler master switch on via
          /op/v0/device/scheduler/set on inverter H3-12.0-E;
          writing the schedule anyway.
        requests.exceptions.HTTPError: 404 Client Error: Not Found

    That endpoint is the *only* way to turn the switch **off**, so handback
    cannot work on that hardware at all.  Sessions are unaffected — the same
    user separately observed Mode Scheduler being enabled implicitly by a
    schedule write — which is exactly why this is a **separate input** from
    ``scheduler_supported`` rather than a fourth value of it:

    * ``scheduler_supported`` answers "does this device have a Mode
      Scheduler?", from ``/op/v1/device/scheduler/get/flag``.  On the H3 the
      honest answer is **yes**.
    * ``scheduler_set_unavailable`` answers "can its master switch be
      written?", from a 404 on ``/op/v0/device/scheduler/set``.

    Folding the second into ``scheduler_supported=False`` would make the
    integration state a falsehood about the user's hardware — precisely the
    mistake ``get_scheduler_flag`` was changed to stop making (C-020,
    P-005) — and would send them hunting for a scheduler they demonstrably
    have.  Folding it into ``None`` would be worse still: ``None`` already
    means "the flag read failed and will be retried", and a missing write
    endpoint is neither a read nor transient.

    The decision is the same either way (decline, touch nothing), so
    widening the signature cannot make handback less safe; only the reason
    changes, and the reason is the whole point.
    """

    def test_does_not_act(self) -> None:
        assert _plan(scheduler_set_unavailable=True).act is False

    def test_touches_nothing(self) -> None:
        plan = _plan(scheduler_set_unavailable=True)
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None

    def test_declines_even_though_the_scheduler_is_supported(self) -> None:
        # The H3 case exactly: the flag says the device HAS a Mode
        # Scheduler, and the write endpoint still 404s.  An implementation
        # that only consulted ``scheduler_supported`` would act here, fail
        # the master-switch write, and leave the user's inverter
        # scheduler-controlled while reporting a handback.
        plan = _plan(scheduler_supported=True, scheduler_set_unavailable=True)
        assert plan.act is False

    def test_reason_says_the_switch_cannot_be_turned_off(self) -> None:
        reason = _plan(scheduler_set_unavailable=True).reason.lower()
        assert "master switch" in reason, (
            f"the reason does not name the master switch: {reason}"
        )
        assert "404" in reason, (
            f"the reason does not say what the inverter actually did: {reason}"
        )

    def test_reason_does_not_claim_the_hardware_lacks_a_scheduler(self) -> None:
        # The lie worth guarding against: this device HAS a Mode Scheduler.
        reason = _plan(scheduler_set_unavailable=True).reason.lower()
        assert "reports no mode scheduler support" not in reason
        assert "could not determine" not in reason

    def test_it_reads_differently_from_every_other_scheduler_reason(self) -> None:
        # Three distinct facts, three distinct reasons: "no scheduler",
        # "could not find out", "scheduler present but not switchable".
        # Two of them sharing a reason would send triage to the wrong place.
        reasons = {
            _plan(scheduler_supported=False).reason,
            _plan(scheduler_supported=None).reason,
            _plan(scheduler_set_unavailable=True).reason,
        }
        assert len(reasons) == 3, f"reasons collapsed onto each other: {reasons}"


class TestGuardPrecedence:
    """Pin the reason attribution so it cannot drift.

    Precedence runs from most permanent to most transient:
    ``enabled`` → ``entity_mode`` → ``scheduler_supported`` →
    ``scheduler_set_unavailable`` → ``unmanaged_modes`` →
    ``session_active``.  The reason a caller logs should name the condition
    that will still be true tomorrow, not the one that clears itself in an
    hour.  Precedence only ever selects *which* reason is reported — every
    guard yields ``act=False``, so no ordering can make the decision less
    safe.
    """

    def test_disabled_beats_session_active(self) -> None:
        # The named case: with the option off, the session is irrelevant —
        # handback would not happen once it ended either, and "not
        # enabled" is the thing the user would have to change.
        reason = _plan(enabled=False, session_active=True).reason.lower()
        assert "enabl" in reason
        assert "session" not in reason

    def test_disabled_beats_everything(self) -> None:
        reason = _plan(
            enabled=False,
            entity_mode=True,
            session_active=True,
            unmanaged_modes=["Backup (00:00-06:00)"],
            scheduler_supported=False,
        ).reason.lower()
        assert "enabl" in reason

    def test_entity_mode_beats_session_active(self) -> None:
        reason = _plan(entity_mode=True, session_active=True).reason.lower()
        assert "entity mode" in reason
        assert "session" not in reason

    def test_unsupported_beats_unmanaged_modes(self) -> None:
        reason = _plan(
            scheduler_supported=False, unmanaged_modes=["Backup (00:00-06:00)"]
        ).reason
        assert "scheduler" in reason.lower()
        assert "Backup" not in reason

    def test_unknown_support_beats_unmanaged_modes(self) -> None:
        # Same slot in the precedence order as an outright "unsupported":
        # not knowing whether there is a scheduler is at least as permanent
        # as a Backup group the user can delete.
        reason = _plan(
            scheduler_supported=None, unmanaged_modes=["Backup (00:00-06:00)"]
        ).reason
        assert "could not determine" in reason.lower()
        assert "Backup" not in reason

    def test_unsupported_beats_an_unavailable_switch_write(self) -> None:
        # A device with no Mode Scheduler at all also has no working
        # master-switch endpoint (the simulator models exactly that: it
        # answers errno 40257 there).  The absence of the scheduler is the
        # cause; the unwritable switch is the symptom, and reporting a
        # symptom over its cause sends the user to the wrong place.
        reason = _plan(
            scheduler_supported=False, scheduler_set_unavailable=True
        ).reason.lower()
        assert "reports no mode scheduler support" in reason
        assert "404" not in reason

    def test_unknown_support_beats_an_unavailable_switch_write(self) -> None:
        # Fresh evidence about the device outranks a memory of an earlier
        # attempt: a flag read that just failed says less is known now than
        # a 404 recorded some time ago implied.
        reason = _plan(
            scheduler_supported=None, scheduler_set_unavailable=True
        ).reason.lower()
        assert "could not determine" in reason
        assert "404" not in reason

    def test_an_unavailable_switch_write_beats_unmanaged_modes(self) -> None:
        # A Backup group the user can delete in the FoxESS app is not the
        # thing standing in their way; an endpoint their firmware does not
        # serve is, and telling them to delete the group would waste their
        # time on a fix that cannot work.
        reason = _plan(
            scheduler_set_unavailable=True, unmanaged_modes=["Backup (00:00-06:00)"]
        ).reason
        assert "404" in reason
        assert "Backup" not in reason

    def test_an_unavailable_switch_write_beats_session_active(self) -> None:
        reason = _plan(scheduler_set_unavailable=True, session_active=True).reason
        assert "404" in reason
        assert "session" not in reason.lower()

    def test_disabled_beats_an_unavailable_switch_write(self) -> None:
        reason = _plan(enabled=False, scheduler_set_unavailable=True).reason.lower()
        assert "enabl" in reason
        assert "404" not in reason

    def test_entity_mode_beats_an_unavailable_switch_write(self) -> None:
        reason = _plan(entity_mode=True, scheduler_set_unavailable=True).reason.lower()
        assert "entity mode" in reason
        assert "404" not in reason

    def test_unmanaged_modes_beat_session_active(self) -> None:
        # A Backup group blocks handback permanently and needs the user in
        # the FoxESS app; the session ends on its own.  Report the former.
        reason = _plan(
            unmanaged_modes=["Backup (00:00-06:00)"], session_active=True
        ).reason
        assert "Backup (00:00-06:00)" in reason
        assert "session" not in reason.lower()


class TestPlanIsImmutable:
    """A plan callers can edit between decision and execution defeats the
    reason for separating them."""

    def test_is_frozen(self) -> None:
        assert dataclasses.is_dataclass(HandbackPlan)
        plan = _plan()
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.act = False  # type: ignore[misc]

    def test_min_soc_cannot_be_filled_in_later(self) -> None:
        # The specific mutation this guards against: a caller "helpfully"
        # supplying a default floor the user never chose.
        plan = _plan(captured_min_soc_on_grid=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.restore_min_soc_on_grid = 10  # type: ignore[misc]


class TestNoDeclinedPlanCarriesActions:
    """Whatever the reason, a declined plan is inert."""

    @pytest.mark.parametrize(
        "override",
        [
            {"enabled": False},
            {"entity_mode": True},
            {"session_active": True},
            {"unmanaged_modes": ["Backup (00:00-06:00)"]},
            {"scheduler_supported": False},
            {"scheduler_supported": None},
            {"scheduler_set_unavailable": True},
        ],
    )
    def test_declined_plan_is_inert(self, override: dict[str, object]) -> None:
        plan = _plan(**override)
        assert plan.act is False
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None
        assert plan.reason, "a declined plan must say why, for the log (P-005)"
