"""FoxESS surfaces the scheduler-handback feature is built on.

Two halves, both prerequisites for the handback itself (issues #16, #4):

1. **The Mode Scheduler master switch** — schedule writes must survive a
   switch that is off, because handback turns it off.
2. **The direct device settings** — ``/op/v0/device/setting/{get,set}``,
   which is how the idle state is set once the scheduler is out of the
   way.

Schedule writes must survive a Mode Scheduler master switch that is off.

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
import requests

from custom_components.foxess_control.foxess.client import (
    FoxESSApiError,
    FoxESSClient,
)
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


class TestSchedulerSupportIsHonestAboutFailure:
    """A read that failed must not masquerade as "no scheduler support".

    ``get_scheduler_flag`` used to degrade an unexpected response shape to
    all-False, so one malformed reply became "this inverter reports no Mode
    Scheduler support".  The *decision* was fail-safe either way — handback
    declines — but the reason logged was a confident lie about the user's
    hardware, which is worse than admitting ignorance (C-020, P-005): it
    sends someone looking for a firmware limitation that does not exist.

    So there is exactly one signal for "unknown": an exception.  A failed
    request and a malformed response are indistinguishable in the only
    respect that matters — we do not know the answer — and collapsing them
    onto one mechanism makes it impossible to treat unknown as False by
    forgetting to check a sentinel.  :meth:`Inverter.probe_scheduler_support`
    turns that into the tri-state the policy layer consumes.
    """

    def test_malformed_response_raises_rather_than_reporting_unsupported(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """``result: null`` is a shape this API really does return."""
        foxess_sim.set(scheduler_flag_null=True)
        inv = _make_inv(foxess_sim)

        with pytest.raises(ValueError, match="Mode Scheduler"):
            inv.get_scheduler_flag()

    def test_probe_reports_support(self, foxess_sim: SimulatorHandle) -> None:
        inv = _make_inv(foxess_sim)
        assert inv.probe_scheduler_support() is True

    def test_probe_reports_a_device_that_says_unsupported(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """The device answered, and the answer was no."""
        foxess_sim.set(scheduler_supported=False)
        inv = _make_inv(foxess_sim)

        assert inv.probe_scheduler_support() is False

    def test_probe_reports_unknown_on_a_malformed_response(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(scheduler_flag_null=True)
        inv = _make_inv(foxess_sim)

        assert inv.probe_scheduler_support() is None, (
            "a malformed reply was reported as a fact about the hardware"
        )

    def test_probe_reports_unknown_when_the_request_fails(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        inv = _make_inv(foxess_sim)
        foxess_sim.fault("api_500")

        assert inv.probe_scheduler_support() is None

    def test_probe_distinguishes_unknown_from_unsupported(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """The whole point: these two must not be the same value.

        ``None is False`` would be a lie the policy layer cannot detect.
        """
        inv = _make_inv(foxess_sim)
        foxess_sim.set(scheduler_supported=False)
        answered = inv.probe_scheduler_support()

        foxess_sim.set(scheduler_flag_null=True)
        unknown = inv.probe_scheduler_support()

        assert answered is False
        assert unknown is None
        assert answered is not unknown

    def test_probe_does_not_write_to_the_device(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """A capability probe is a read.  Nothing about it may change state."""
        _pin_midday(foxess_sim)
        inv = _make_inv(foxess_sim)
        before = foxess_sim.state()

        inv.probe_scheduler_support()

        after = foxess_sim.state()
        assert after["scheduler_enabled"] == before["scheduler_enabled"]
        assert after["schedule_groups"] == before["schedule_groups"]
        assert after["work_mode_direct"] == before["work_mode_direct"]


class TestTheMasterSwitchWriteEndpointIsRemembered:
    """Issue #17: an inverter whose ``scheduler/set`` answers HTTP 404.

    Reported on an **H3-12.0-E** running 1.0.22-beta.6.  That endpoint is
    the only way to turn the Mode Scheduler master switch *off*, so handback
    cannot work on that hardware — and until the integration remembers the
    fact, it re-discovers it at every single session boundary and declines
    without ever being able to say why.

    Two properties bound how much is inferred from one response, because
    over-inferring would permanently disable a working feature:

    1. **Only a 404 counts.**  "This endpoint does not exist" is a durable
       property of a firmware/region; a 500, a timeout or a dropped
       connection is a bad afternoon at FoxESS and says nothing at all
       about whether the endpoint is there.
    2. **Nothing is persisted, and a success clears it.**  The memory lives
       on the ``Inverter`` instance, so a reload or a restart re-tests from
       scratch, and any later master-switch write that succeeds erases it
       immediately.  ``_ensure_scheduler_enabled`` attempts the write on
       *every* schedule write, so a transient 404 self-heals at the next
       session rather than needing a restart.
    """

    def test_a_fresh_inverter_makes_no_claim(self, foxess_sim: SimulatorHandle) -> None:
        """Nothing has been tried, so nothing is asserted."""
        inv = _make_inv(foxess_sim)

        assert inv.scheduler_set_unavailable is False, (
            "an inverter that has never attempted the write claims the "
            "endpoint is missing — that would decline handback on every "
            "install until something proved otherwise"
        )

    def test_a_404_on_the_write_is_remembered(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(scheduler_set_supported=False)
        inv = _make_inv(foxess_sim)

        with pytest.raises(requests.HTTPError, match="404"):
            inv.set_scheduler_enabled(False)

        assert inv.scheduler_set_unavailable is True, (
            "the 404 was forgotten, so the handback will re-discover it at "
            "every session boundary and never be able to explain itself"
        )

    def test_a_schedule_write_discovers_it(self, foxess_sim: SimulatorHandle) -> None:
        """No dedicated probe needed: every session write already asks.

        ``_ensure_scheduler_enabled`` runs before every schedule write, so by
        the time a session's teardown reaches the handback the answer is
        already known — without costing a single extra request.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20, power=3000)

        assert foxess_sim.state()["work_mode"] == "ForceDischarge", (
            "the session write itself failed, so this test is not about "
            "what it claims to be about"
        )
        assert inv.scheduler_set_unavailable is True

    def test_a_transient_server_error_is_not_remembered(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """A 500 must not permanently disable the feature.

        The fault is injected for exactly the number of requests the client
        spends on the master-switch write (its transient retries plus the
        attempt that gives up), so nothing else meets a broken simulator.
        """
        inv = _make_inv(foxess_sim)
        assert inv.max_power_w == 10500  # warm the detail/properties caches

        foxess_sim.fault("api_500", count=FoxESSClient.TRANSIENT_RETRIES + 1)
        with pytest.raises(requests.HTTPError, match="500"):
            inv.set_scheduler_enabled(False)

        assert inv.scheduler_set_unavailable is False, (
            "a transient 500 was recorded as 'this firmware has no such "
            "endpoint', which would decline handback for the rest of the "
            "session even though the endpoint is there"
        )

    def test_a_rejection_by_the_device_is_not_remembered(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """errno 40257 is the device refusing, not the endpoint missing.

        A device reporting ``support: false`` serves the endpoint and
        rejects the write.  That is already reported honestly by
        ``scheduler_supported``, and recording it here as well would
        attribute the refusal to the wrong cause.
        """
        foxess_sim.set(scheduler_supported=False)
        inv = _make_inv(foxess_sim)

        with pytest.raises(FoxESSApiError, match="40257"):
            inv.set_scheduler_enabled(False)

        assert inv.scheduler_set_unavailable is False

    def test_a_later_success_clears_it(self, foxess_sim: SimulatorHandle) -> None:
        """Self-healing: one 404 does not disable the feature for good."""
        foxess_sim.set(scheduler_set_supported=False)
        inv = _make_inv(foxess_sim)
        with pytest.raises(requests.HTTPError, match="404"):
            inv.set_scheduler_enabled(False)
        assert inv.scheduler_set_unavailable is True

        foxess_sim.set(scheduler_set_supported=True)
        inv.set_scheduler_enabled(False)

        assert inv.scheduler_set_unavailable is False, (
            "the endpoint answered, and the integration still believes it is missing"
        )
        assert foxess_sim.state()["scheduler_enabled"] is False

    def test_a_transient_error_does_not_erase_what_was_learned(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Only a *success* is evidence the endpoint is there."""
        foxess_sim.set(scheduler_set_supported=False)
        inv = _make_inv(foxess_sim)
        with pytest.raises(requests.HTTPError, match="404"):
            inv.set_scheduler_enabled(False)

        foxess_sim.set(scheduler_set_supported=True)
        foxess_sim.fault("api_500", count=FoxESSClient.TRANSIENT_RETRIES + 1)
        with pytest.raises(requests.HTTPError, match="500"):
            inv.set_scheduler_enabled(False)

        assert inv.scheduler_set_unavailable is True

    def test_the_flag_snapshot_still_reports_no_switch_write(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """A rejected write must not be recorded as a switch position.

        Pre-existing behaviour, re-pinned here because the 404 bookkeeping
        sits in the same method: a diagnostics download that asserted the
        switch was off after a 404 would contradict the device it describes.
        """
        foxess_sim.set(scheduler_set_supported=False)
        inv = _make_inv(foxess_sim)

        with pytest.raises(requests.HTTPError, match="404"):
            inv.set_scheduler_enabled(False)

        assert inv.scheduler_flag_snapshot is None


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


class TestDirectWorkModeEnumeration:
    """What the device declares it will accept *off* the scheduler.

    Load-bearing for the whole handback design: because the direct
    enumeration has no forced modes, sessions must keep using the
    scheduler and handback can only ever govern the **idle** state.  If
    FoxESS ever adds ForceCharge/ForceDischarge here, that constraint
    disappears and the design should be revisited — so it is asserted
    rather than assumed, and a failure here is news, not a bug.
    """

    def test_reads_work_mode_and_its_enumeration(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        inv = _make_inv(foxess_sim)

        setting = inv.get_setting("WorkMode")

        assert setting["value"] == "SelfUse"
        assert "SelfUse" in setting["enumList"]

    def test_direct_enumeration_has_no_forced_modes(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """The premise of "sessions keep using the scheduler"."""
        inv = _make_inv(foxess_sim)

        declared = inv.get_setting("WorkMode")["enumList"]

        forced = {"ForceCharge", "ForceDischarge"} & set(declared)
        assert not forced, (
            f"the direct work-mode enumeration now offers {sorted(forced)}; "
            "sessions may no longer need the scheduler at all, so revisit "
            "the handback design rather than deleting this assertion"
        )

    def test_scheduler_enumeration_does_have_forced_modes(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Guards the premise: the two enumerations really do differ.

        Without this, the assertion above could "pass" against a
        simulator that simply declares nothing anywhere.
        """
        inv = _make_inv(foxess_sim)

        assert {"ForceCharge", "ForceDischarge"} <= inv.declared_work_modes


class TestDirectMinSocOnGrid:
    """``MinSocOnGrid`` accepts 0 off-scheduler but not inside a group.

    This asymmetry *is* issue #4: the 10 % floor users cannot get below is
    a restriction of the Mode Scheduler, not of the hardware.  Handing the
    inverter back is therefore the only way a lower floor can hold.
    """

    def test_setting_range_allows_zero(self, foxess_sim: SimulatorHandle) -> None:
        inv = _make_inv(foxess_sim)

        setting = inv.get_setting("MinSocOnGrid")

        assert setting["range"]["min"] == 0.0, (
            "the direct MinSocOnGrid setting no longer accepts 0; issue #4 "
            "depends on the 10% floor being a scheduler restriction"
        )
        assert setting["unit"] == "%"

    def test_schedule_range_forbids_zero(self, foxess_sim: SimulatorHandle) -> None:
        """The contrasting half of the asymmetry (C-042)."""
        inv = _make_inv(foxess_sim)

        declared = inv.scheduler_properties["minsocongrid"]["range"]

        assert declared["min"] == 10.0, (
            "the scheduler no longer imposes a 10% minSocOnGrid floor — if "
            "so, issue #4 no longer needs a handback to be fixed"
        )

    def test_round_trips_a_value(self, foxess_sim: SimulatorHandle) -> None:
        inv = _make_inv(foxess_sim)

        inv.set_setting("MinSocOnGrid", "15")

        assert inv.get_setting("MinSocOnGrid")["value"] == "15"
        assert foxess_sim.state()["min_soc_on_grid"] == 15

    def test_round_trips_zero(self, foxess_sim: SimulatorHandle) -> None:
        """0 is the value the schedule path cannot express — pin it works."""
        inv = _make_inv(foxess_sim)

        inv.set_setting("MinSocOnGrid", "0")

        assert inv.get_setting("MinSocOnGrid")["value"] == "0"
        assert foxess_sim.state()["min_soc_on_grid"] == 0

    def test_setting_and_battery_soc_endpoints_are_one_value(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """``setting/get`` and ``battery/soc/get`` read the same device value.

        Two API surfaces onto one register.  If the simulator modelled them
        as independent fields, the capture-and-restore of a user's Min SoC
        could be broken on real hardware while every test passed (P-002).
        """
        inv = _make_inv(foxess_sim)

        inv.set_setting("MinSocOnGrid", "7")
        assert inv.get_min_soc()["minSocOnGrid"] == 7

        inv.set_min_soc(min_soc=5, min_soc_on_grid=23)
        assert inv.get_setting("MinSocOnGrid")["value"] == "23"


class TestSetWorkModeDirect:
    """Writing the work mode as a device setting, not a schedule group."""

    def test_changes_the_device_setting(self, foxess_sim: SimulatorHandle) -> None:
        foxess_sim.set(work_mode_direct="Feedin")
        inv = _make_inv(foxess_sim)

        inv.set_work_mode_direct("SelfUse")

        assert foxess_sim.state()["work_mode_direct"] == "SelfUse"
        assert inv.get_setting("WorkMode")["value"] == "SelfUse"

    def test_governs_the_inverter_once_the_scheduler_is_off(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Guards the premise: the setting is not cosmetic.

        With the master switch off the groups are inert, so whatever the
        direct setting says is what the inverter actually does — which is
        the entire mechanism handback relies on.  A test that passed
        because the simulator stored the value and ignored it would prove
        nothing.
        """
        _pin_midday(foxess_sim)
        inv = _make_inv(foxess_sim)
        inv.set_scheduler_enabled(False)

        inv.set_work_mode_direct("Feedin")

        assert foxess_sim.state()["work_mode"] == "Feedin"

    def test_refuses_a_mode_outside_the_declared_enumeration(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Refuse locally rather than collect an opaque 40257.

        Deliberately stricter than the *schedule* path, which warns and
        writes anyway: there, refusing could break installs that work
        today, whereas nothing depends on this surface yet.
        """
        inv = _make_inv(foxess_sim)

        with pytest.raises(ValueError, match="ForceDischarge") as excinfo:
            inv.set_work_mode_direct("ForceDischarge")

        assert "SelfUse" in str(excinfo.value), (
            "the error must name the declared enumeration, or the user "
            "cannot tell what would have been accepted (C-020)"
        )

    def test_refusing_writes_nothing(self, foxess_sim: SimulatorHandle) -> None:
        """Raising is not enough — the device must be untouched."""
        foxess_sim.set(work_mode_direct="Feedin")
        inv = _make_inv(foxess_sim)

        with pytest.raises(ValueError, match="ForceDischarge"):
            inv.set_work_mode_direct("ForceDischarge")

        assert foxess_sim.state()["work_mode_direct"] == "Feedin", (
            "the refused write reached the device anyway"
        )

    def test_proceeds_when_the_device_declares_no_enumeration(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Older firmware declares no ``enumList`` — do not block on it.

        With no declaration there is no basis to refuse, and refusing
        would make the feature unavailable on hardware that may well
        accept the write.
        """
        foxess_sim.set(setting_work_modes=[])
        inv = _make_inv(foxess_sim)

        assert "enumList" not in inv.get_setting("WorkMode"), (
            "precondition: this device must declare no enumeration"
        )

        inv.set_work_mode_direct("SelfUse")

        assert foxess_sim.state()["work_mode_direct"] == "SelfUse"


class TestDirectSettingsAndScheduleAreIndependent:
    """The two control paths must not leak into each other.

    Confusing :meth:`Inverter.set_work_mode` (schedule group) with
    :meth:`Inverter.set_work_mode_direct` (device setting) would silently
    move control between the scheduler and the direct settings — which is
    the exact subject matter of this feature, so it is pinned.
    """

    def test_direct_write_creates_no_schedule_group(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        inv = _make_inv(foxess_sim)

        inv.set_work_mode_direct("SelfUse")

        assert foxess_sim.state()["schedule_groups"] == [], (
            "set_work_mode_direct wrote a schedule group — it must not "
            "touch the scheduler at all"
        )

    def test_direct_write_leaves_existing_groups_alone(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        _pin_midday(foxess_sim)
        inv = _make_inv(foxess_sim)
        inv.force_discharge(min_soc=20, power=3000)
        before = foxess_sim.state()["schedule_groups"]

        inv.set_work_mode_direct("SelfUse")

        assert foxess_sim.state()["schedule_groups"] == before
        assert foxess_sim.state()["work_mode"] == "ForceDischarge", (
            "the active group must still win while the master switch is on"
        )

    def test_schedule_write_leaves_the_direct_setting_alone(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        _pin_midday(foxess_sim)
        foxess_sim.set(work_mode_direct="Feedin")
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20, power=3000)

        assert foxess_sim.state()["work_mode_direct"] == "Feedin", (
            "a session schedule write overwrote the user's direct work "
            "mode; handback would restore the wrong idle state"
        )

    def test_schedule_write_leaves_min_soc_on_grid_setting_alone(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """C-042 clamps the *group* value, never the device setting."""
        inv = _make_inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "5")

        inv.force_discharge(min_soc=20, power=3000)

        assert inv.get_setting("MinSocOnGrid")["value"] == "5", (
            "the schedule write moved the persistent Min SoC floor"
        )
