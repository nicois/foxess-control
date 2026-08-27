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
        assert _plan(captured_min_soc_on_grid=captured).restore_min_soc_on_grid == captured


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


class TestGuardPrecedence:
    """Pin the reason attribution so it cannot drift.

    Precedence runs from most permanent to most transient:
    ``enabled`` → ``entity_mode`` → ``scheduler_supported`` →
    ``unmanaged_modes`` → ``session_active``.  The reason a caller logs
    should name the condition that will still be true tomorrow, not the
    one that clears itself in an hour.  Precedence only ever selects
    *which* reason is reported — every guard yields ``act=False``, so no
    ordering can make the decision less safe.
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
        ],
    )
    def test_declined_plan_is_inert(self, override: dict[str, object]) -> None:
        plan = _plan(**override)
        assert plan.act is False
        assert plan.disable_scheduler is False
        assert plan.work_mode is None
        assert plan.restore_min_soc_on_grid is None
        assert plan.reason, "a declined plan must say why, for the log (P-005)"
