"""Schedule writes must survive a Mode Scheduler master switch that is off.

Prerequisite for the scheduler-handback feature (issues #16, #4): once
anything turns the inverter's Mode Scheduler master switch **off**, every
subsequent smart session writes its schedule groups via
``POST /op/v0/device/scheduler/enable`` — and groups only drive the
inverter while the master switch is on.

Two facts frame this:

* Removing every group does **not** turn the switch off.  Confirmed from
  issue #16: FoxCloud still showed the inverter as scheduler-controlled
  with no groups left.
* Whether ``scheduler/enable`` turns the switch back **on** from off is
  **unverified**.  Establishing it would mean writing to the owner's
  production home battery, so it has not been done.

If the API does not imply the enable and the integration does not do it
explicitly, a session after a handback writes a schedule that silently
never fires: errno 0, no error surfaced, no mode change, no discharge —
the worst available failure mode (P-003, P-005, C-020).

The fix is to not depend on the answer, so these tests pin **both**
possible API behaviours via the simulator's
``scheduler_enable_implies_on`` knob and assert the same observable
outcome either way: the switch is on and the group is actually in force.

Everything here drives the real ``FoxESSClient`` / ``Inverter`` over HTTP
against a fresh simulator instance (C-028) — no mocks in between — and
asserts on observable device state, never on which method was called.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter, WorkMode

if TYPE_CHECKING:
    from .conftest import SimulatorHandle


@pytest.fixture(autouse=True)
def _disable_throttle() -> None:
    """Disable request throttling in tests."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


def _make_inv(sim: SimulatorHandle) -> Inverter:
    client = FoxESSClient("test-api-key", base_url=sim.url)
    return Inverter(client, "SIM0001")


def _pin_midday(sim: SimulatorHandle) -> None:
    """Pin simulated wall-clock time inside every full-day window.

    ``set_work_mode`` writes a 00:00-23:59 group, and the simulator matches
    windows with ``start <= now < end`` — so at 23:59 exactly no group is
    active and the active mode reads SelfUse.  Pinning the clock removes a
    one-minute-per-day flake from the "the group is in force" assertions
    (C-031).
    """
    sim.set(sim_time="2026-01-15T12:00:00+00:00")


def _written_groups(sim: SimulatorHandle, mode: WorkMode) -> list[dict[str, Any]]:
    state = sim.state()
    return [g for g in state["schedule_groups"] if g["workMode"] == mode.value]


def _assert_in_force(sim: SimulatorHandle, mode: WorkMode) -> None:
    """Assert *mode* is both written and actually driving the inverter."""
    state = sim.state()
    assert state["scheduler_enabled"] is True, (
        "the Mode Scheduler master switch is still off — the schedule was "
        "written behind a disabled switch and will never fire"
    )
    assert _written_groups(sim, mode), f"no {mode.value} group is on the inverter"
    assert state["work_mode"] == mode.value, (
        f"inverter is in {state['work_mode']}, not {mode.value} — the group "
        "was accepted but is not in force"
    )


class TestSchedulerMasterSwitch:
    """Writing groups must work whether or not the write flips the switch."""

    def test_session_write_works_when_enable_is_implicit(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """If ``scheduler/enable`` does imply the switch, sessions work."""
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_enabled=False, scheduler_enable_implies_on=True)
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20, power=3000)

        _assert_in_force(foxess_sim, WorkMode.FORCE_DISCHARGE)

    def test_session_write_works_when_enable_is_not_implicit(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """The dangerous case: groups written behind a disabled switch.

        The integration must have enabled the switch explicitly, because
        nothing else is going to.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_enabled=False, scheduler_enable_implies_on=False)
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20, power=3000)

        _assert_in_force(foxess_sim, WorkMode.FORCE_DISCHARGE)

    def test_force_charge_works_behind_a_disabled_switch(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Charge sessions share the choke point, so they benefit too."""
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_enabled=False, scheduler_enable_implies_on=False)
        inv = _make_inv(foxess_sim)

        inv.force_charge(target_soc=80)

        _assert_in_force(foxess_sim, WorkMode.FORCE_CHARGE)

    def test_feedin_works_behind_a_disabled_switch(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """``foxess_control.feedin`` must not silently no-op either."""
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_enabled=False, scheduler_enable_implies_on=False)
        inv = _make_inv(foxess_sim)

        inv.set_work_mode(WorkMode.FEEDIN)

        _assert_in_force(foxess_sim, WorkMode.FEEDIN)

    def test_self_use_teardown_works_behind_a_disabled_switch(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """C-025: the SelfUse baseline written on teardown must land.

        A teardown that silently fails leaves whatever the inverter was
        last told to do in place — the P-001/P-002 hazard.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_enabled=False, scheduler_enable_implies_on=False)
        inv = _make_inv(foxess_sim)

        inv.self_use(min_soc_on_grid=15)

        _assert_in_force(foxess_sim, WorkMode.SELF_USE)


class TestNoRegressionWhenSwitchAlreadyOn:
    """Today's installs — switch on, implies-on true — must be untouched."""

    def test_switch_already_on_still_works(self, foxess_sim: SimulatorHandle) -> None:
        _pin_midday(foxess_sim)
        assert foxess_sim.state()["scheduler_enabled"] is True

        inv = _make_inv(foxess_sim)
        inv.force_discharge(min_soc=20, power=3000)

        _assert_in_force(foxess_sim, WorkMode.FORCE_DISCHARGE)

    def test_payload_written_is_unchanged(self, foxess_sim: SimulatorHandle) -> None:
        """The groups on the wire must be byte-for-byte what they were.

        Enabling the master switch is a separate request to a separate
        endpoint; it must not perturb the schedule payload itself.
        """
        _pin_midday(foxess_sim)
        inv = _make_inv(foxess_sim)

        inv.self_use(min_soc_on_grid=11)

        assert foxess_sim.state()["schedule_groups"] == [
            {
                "enable": 1,
                "startHour": 0,
                "startMinute": 0,
                "endHour": 23,
                "endMinute": 59,
                "workMode": "SelfUse",
                "minSocOnGrid": 11,
                "fdSoc": 11,
                "fdPwr": 10500,
            }
        ]


class TestDeviceWithoutScheduler:
    """``support: false`` must not be made worse than it is today.

    Chosen behaviour: **attempt the enable and tolerate the rejection.**
    Probing ``support`` first would cost an extra request on every write and
    would itself have to tolerate failure, and refusing to write because the
    flag said unsupported would break installs that work today (hundreds of
    users — prefer the safe thing over forcing reconfiguration).  Crashing
    is not acceptable; a rejected master-switch write is.
    """

    def test_unsupported_scheduler_does_not_abort_the_write(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_supported=False)
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20, power=3000)

        assert _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE), (
            "a device reporting support: false lost its schedule write"
        )
        assert foxess_sim.state()["work_mode"] == "ForceDischarge"

    def test_flag_reports_lack_of_support(self, foxess_sim: SimulatorHandle) -> None:
        foxess_sim.set(scheduler_supported=False)
        inv = _make_inv(foxess_sim)

        assert inv.get_scheduler_flag() == {"enable": True, "support": False}


class TestEnableFailureIsTolerated:
    """A failed enable must never abort a write that would have worked."""

    def test_missing_set_endpoint_does_not_abort_the_write(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Older firmware / other regions may not serve ``scheduler/set``.

        HTTP 404 on the master-switch write must degrade to today's
        behaviour, not to a failed session.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20, power=3000)

        assert foxess_sim.state()["work_mode"] == "ForceDischarge"

    def test_server_error_on_enable_does_not_abort_the_write(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Same for a transient 500 that outlasts the client's retries.

        The fault is injected for exactly the number of requests the client
        will spend on the master-switch write, so the schedule write that
        follows meets a healthy simulator.  Caches are warmed first so no
        capability probe is in the way.
        """
        _pin_midday(foxess_sim)
        inv = _make_inv(foxess_sim)
        assert inv.max_power_w == 10500  # warms detail + properties caches

        foxess_sim.fault("api_500", count=FoxESSClient.TRANSIENT_RETRIES + 1)
        inv.force_discharge(min_soc=20, power=3000)

        state = foxess_sim.state()
        assert state["active_fault"] is None, "fault outlived the master-switch write"
        assert state["work_mode"] == "ForceDischarge"

    def test_enable_failure_is_diagnosable_without_log_spam(
        self, foxess_sim: SimulatorHandle, caplog: pytest.LogCaptureFixture
    ) -> None:
        """C-020: warn once, then stay quiet.

        Discharge pacing writes the schedule every 60 s; a warning per write
        would bury the log on every install whose firmware lacks the
        endpoint, but total silence would make the handback failure mode
        undiagnosable.
        """
        foxess_sim.set(scheduler_set_supported=False)
        inv = _make_inv(foxess_sim)

        with caplog.at_level(logging.DEBUG):
            inv.self_use(min_soc_on_grid=11)
            inv.self_use(min_soc_on_grid=11)

        warnings = [
            rec.getMessage()
            for rec in caplog.records
            if rec.levelno >= logging.WARNING and "Mode Scheduler" in rec.getMessage()
        ]
        assert len(warnings) == 1, (
            f"expected exactly one Mode Scheduler warning, got {warnings}"
        )


class TestSchedulerFlagSurface:
    """``get_scheduler_flag`` / ``set_scheduler_enabled`` round-trip."""

    def test_flag_reports_the_switch_state(self, foxess_sim: SimulatorHandle) -> None:
        inv = _make_inv(foxess_sim)
        assert inv.get_scheduler_flag() == {"enable": True, "support": True}

        foxess_sim.set(scheduler_enabled=False)
        assert inv.get_scheduler_flag() == {"enable": False, "support": True}

    def test_set_scheduler_enabled_round_trips(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        inv = _make_inv(foxess_sim)

        inv.set_scheduler_enabled(False)
        assert foxess_sim.state()["scheduler_enabled"] is False
        assert inv.get_scheduler_flag()["enable"] is False

        inv.set_scheduler_enabled(True)
        assert foxess_sim.state()["scheduler_enabled"] is True

    def test_switching_off_stops_groups_taking_effect(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Guards the premise: the simulator's switch actually gates modes.

        Without this, a test that "passes" because the master switch is
        cosmetic would prove nothing.
        """
        _pin_midday(foxess_sim)
        inv = _make_inv(foxess_sim)
        inv.force_discharge(min_soc=20, power=3000)
        assert foxess_sim.state()["work_mode"] == "ForceDischarge"

        inv.set_scheduler_enabled(False)

        state = foxess_sim.state()
        assert state["work_mode"] == "SelfUse"
        assert _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE), (
            "issue #16: turning the switch off must not delete the groups"
        )


class TestThroughCloudAdapter:
    """Production routes session writes through FoxESSCloudAdapter."""

    @pytest.mark.asyncio
    async def test_apply_mode_enables_the_switch(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        from custom_components.foxess_control.foxess_adapter import FoxESSCloudAdapter

        foxess_sim.set(scheduler_enabled=False, scheduler_enable_implies_on=False)
        inv = _make_inv(foxess_sim)

        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

        now = datetime.datetime.now()
        adapter = FoxESSCloudAdapter(
            hass,
            inv,
            min_soc_on_grid=15,
            api_min_soc=11,
            start=now,
            end=now + datetime.timedelta(hours=1),
        )

        await adapter.apply_mode(hass, WorkMode.FORCE_DISCHARGE, fd_soc=20)

        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True, (
            "a real session write left the master switch off"
        )
        assert _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)
