"""End-to-end tests: real HA container + FoxESS simulator / input helpers.

Run with: pytest tests/e2e/ -m slow
Requires: podman, PyJWT

Fixture scoping:
- connection_mode: session — "cloud" or "entity"
- foxess_sim + ha_e2e: session scope (one per xdist worker)
- _e2e_reset: autouse function scope (resets sim/entities + clears HA)
"""

from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING

import pytest

from .conftest import set_inverter_state
from .ha_client import FATAL_FOR_ACTIVE, HAEventStream

if TYPE_CHECKING:
    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow


def _tight_window(minutes: int = 30) -> tuple[str, str]:
    """Return a tight window starting ~2 min before now (UTC).

    Avoids midnight crossings (C-009): start and end are minute-of-day
    strings, so the inverter schedule must end on the same calendar day
    it starts.  C-031 also requires that the returned window has enough
    remaining duration *after* ``now`` for the test's longest
    ``wait_for_state`` call to succeed before the schedule's ``end``
    fires ``_on_timer_expire`` and cancels the session — otherwise the
    sensor reverts to 'idle' mid-test.

    When ``now`` is too close to midnight to fit ``minutes`` of window
    AND have at least ``minutes - 2`` minutes remaining after ``now``,
    we sleep until 00:00:05 of the next UTC day before computing the
    window.  The sleep is bounded by 60 seconds (the worst case: ``now``
    is at 23:59:00).  This is the only realistic way to satisfy both
    invariants simultaneously — the FoxESS API rejects midnight-crossing
    schedules so we cannot return tomorrow's date for ``end``.

    Reproduces CI run 26006442121 (2026-05-17 23:58 UTC, 16/20 shards
    failed) — the previous implementation clamped ``end_min`` to 23:59
    without checking remaining time, so calling ``_tight_window(10)``
    at ``now=23:58:58`` returned ``("23:49:00", "23:59:00")`` and the
    schedule's ``_on_timer_expire`` fired ~2 seconds after the service
    call, cancelling every session before any wait_for_state could see
    the discharging/charging transition.
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    now_min = now.hour * 60 + now.minute

    # Required remaining minutes after ``now``.  The helper documents a
    # 2-minute backshift on ``start`` so up to 2 minutes of the
    # requested ``minutes`` is consumed by the backshift; the rest must
    # remain *after* ``now`` for the test's wait_for_state to succeed
    # before _on_timer_expire fires.  With clamping at 23:59 the
    # remaining minutes after ``now`` could fall to zero — the bug we
    # are fixing.
    min_remaining = max(1, minutes - 2)

    # If clamping at 23:59 would leave us with less than ``min_remaining``
    # minutes after ``now``, sleep into the new UTC day and recompute.
    # The check ``now_min + min_remaining > 23*60 + 59`` is equivalent
    # to "less than min_remaining minutes left until 23:59"; we add
    # the seconds part so the bound is inclusive of the second-precision
    # remainder of the current minute.
    last_min = 23 * 60 + 59
    if now_min + min_remaining > last_min:
        seconds_until_midnight = ((23 - now.hour) * 3600 + (59 - now.minute) * 60) + (
            60 - now.second
        )
        # Add a 5-second safety margin so the post-sleep ``now`` is
        # firmly inside the new day even if the system clock is
        # adjusted slightly during the sleep.
        time.sleep(seconds_until_midnight + 5)
        now = datetime.datetime.now(tz=datetime.UTC)
        now_min = now.hour * 60 + now.minute

    start_min = max(0, now_min - 2)
    end_min = start_min + minutes
    if end_min > last_min:
        # Defence-in-depth: shouldn't trigger after the sleep above,
        # but keep the original clamp for any minutes value that would
        # exceed 23:59 even from a 00:00 start (e.g. minutes=24*60+1).
        end_min = last_min
        start_min = max(0, end_min - minutes)
    return (
        f"{start_min // 60:02d}:{start_min % 60:02d}:00",
        f"{end_min // 60:02d}:{end_min % 60:02d}:00",
    )


def _wait_for_positive_attr(
    ha: HAClient,
    entity_id: str,
    attr: str,
    timeout_s: float = 30,
) -> float:
    """Poll until an entity attribute is numeric and > 0."""
    return ha.wait_for_numeric_attribute(entity_id, attr, "gt", 0, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Smart discharge (both modes)
# ---------------------------------------------------------------------------


class TestSmartDischarge:
    def test_discharge_starts(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Service call → state transitions to discharging."""
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=80,
            load_kw=0.5,
        )

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )

        state = ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        assert state == "discharging"

    def test_discharge_drains_battery(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """SoC decreases during discharge (both modes)."""
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=80,
            load_kw=0.5,
        )

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        if connection_mode == "cloud":
            assert foxess_sim is not None
            foxess_sim.fast_forward(600, step=5)
        else:
            ha_e2e.set_input_number("input_number.foxess_soc", 70.0)

        soc = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "lt", 80.0, timeout_s=60
        )
        assert soc < 80

    def test_discharge_on_device_with_lower_declared_fd_pwr(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Discharge works on a model whose declared fdPwr ceiling is lower.

        Issues #12/#14/#17, C-042.  ``fdPwr`` was derived as
        ``capacity_kw * 1050`` — correct for a KH10, whose declared ceiling
        is exactly that, but 5 % above the ceiling declared by H3 / EVO
        models.  The API rejected the whole write with errno 40257, so
        Force Discharge failed immediately and the operations sensor never
        left idle.

        The simulator is shaped like an H3-12.0-M (``capacity`` 12 kW →
        12600 W by the old heuristic, declared ceiling 12000 W).  The
        integration is reloaded after the change so it re-reads device
        detail and the declared ranges, then the discharge must actually
        start (C-020: determinable from the UI alone).
        """
        if connection_mode != "cloud":
            pytest.skip("declared scheduler ranges are a cloud-API concept")
        assert foxess_sim is not None

        foxess_sim.set(
            soc=80,
            load_kw=0.5,
            device_type="H3-12.0-M",
            max_power_w=12600,
            fd_pwr_max_w=12000,
            max_grid_export_limit_w=12000,
        )
        # Reload so a fresh Inverter re-reads device detail + declared ranges.
        ha_e2e.reload_integration()

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )

        state = ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        assert state == "discharging"

        # The override is genuinely in force on the inverter, within the
        # ceiling the device declares it accepts.
        groups = [
            g
            for g in foxess_sim.state()["schedule_groups"]
            if g["workMode"] == "ForceDischarge"
        ]
        assert groups, "no ForceDischarge group in force on the inverter"
        assert groups[0]["fdPwr"] <= 12000


# ---------------------------------------------------------------------------
# Smart charge (both modes)
# ---------------------------------------------------------------------------


class TestSmartCharge:
    def test_charge_starts(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Service call starts a charge session."""
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=20,
            load_kw=0.3,
        )

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {"start_time": start, "end_time": end, "target_soc": 80},
        )

        state = ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "charging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        assert state == "charging"


# ---------------------------------------------------------------------------
# Cloud-only tests (simulator required)
# ---------------------------------------------------------------------------


class TestScheduleReconciliation:
    def test_schedule_not_applied_surfaces_repair(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """A firmware that ACKs but drops schedule writes should surface a Repair.

        Issue #11.  The simulator's silent-drop seam makes
        ``/scheduler/enable`` return success (errno 0) WITHOUT applying the
        schedule.  The intent: the client records the commanded ForceCharge
        (the write "succeeded" from its view), the inverter keeps reporting
        SelfUse, and once the grace window (poll_interval + 60s) elapses the
        coordinator's reconciler raises a ``schedule_not_applied`` Repair
        issue in the ``foxess_control`` domain.

        Previously two production gaps made this inert in cloud mode and the
        test was skipped: (1) cloud session-start paths in ``_services.py``
        wrote the schedule via ``inverter.set_schedule(...)`` directly and
        never recorded the commanded mode, so the reconciler had no intent
        until the ~300s ``apply_mode`` tick; and (2) ``_record_commanded_mode``
        reset the grace clock on every identical re-issue, so a persistent
        conflict could never cross the grace window.  Both are fixed (issue
        #11): session-start writes now call ``_record_commanded_mode`` and the
        clock resets only on a genuine mode change.
        """
        if connection_mode != "cloud":
            pytest.skip("requires simulator silent-drop seam")
        assert foxess_sim is not None

        # Shrink the poll cadence so the reconciler runs often and the grace
        # window (poll_interval + 60s) is small.  At the default 300s poll the
        # grace is 360s and the reconciler only evaluates every 5 min, so the
        # Repair could not appear within a practical test timeout.  60s is the
        # minimum the options flow accepts (config_flow_base: min=60) → grace
        # ~120s, reconciler re-evaluates every 60s, so the Repair surfaces by
        # ~120-180s.
        ha_e2e.set_options(polling_interval=60)

        # Inverter silently drops schedule writes: /scheduler/enable returns
        # success but the schedule is never applied.
        foxess_sim.set(soc=20, load_kw=0.3, silent_drop_schedule=True)

        # SoC 20% → target 95% in a 10-min window needs ~immediate full-rate
        # charging, so the session does NOT defer: it writes FORCE_CHARGE at
        # session start and records the commanded mode right away.  (A
        # deferred start would delay intent recording past the window.)
        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {"start_time": start, "end_time": end, "target_soc": 95},
        )

        # The commanded ForceCharge is never applied (silent drop), so once the
        # ~120s grace elapses the reconciler raises the Repair issue on a
        # subsequent 60s poll.  240s gives headroom over the worst case
        # (grace + a full poll interval + reload settling).
        ha_e2e.wait_for_repair_issue(
            "foxess_control", "schedule_not_applied", timeout_s=240
        )


class TestFeedinPacing:
    def test_feedin_power_adjusts_over_time(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Feed-in budget pacing caps discharge power below inverter max.

        With D-005, feedin-limited sessions defer until the feedin
        deadline, then discharge at sub-max power.  Feedin drain times
        that exceed the remaining window produce no deferral and the
        feedin limit simply caps paced power.

        Uses a 30-min window with 3 kWh feedin.  Feedin drain = 18 min
        at 10 kW effective.  SoC drain = 30 min (5 kWh / 10 kW).
        With now ≈ start+2, remaining ≈ 28 min.  Feedin deadline
        (buffered 20%) = 22.5 min before end → deferral ≈ 5.5 min.
        After deferral, paced power ≈ 3/(22.5/60) ≈ 8 kW < 10.5 kW.
        """
        if connection_mode != "cloud":
            pytest.skip("requires simulator fast_forward")
        assert foxess_sim is not None

        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)
        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc",
            "ge",
            79,
            timeout_s=90,
        )
        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {
                "start_time": start,
                "end_time": end,
                "min_soc": 30,
                "feedin_energy_limit_kwh": 3.0,
            },
        )

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=600,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        initial_power = ha_e2e.wait_for_numeric_attribute(
            "sensor.foxess_smart_operations",
            "discharge_power_w",
            "gt",
            0,
            timeout_s=30,
        )
        max_power = 10500
        assert initial_power < max_power, (
            f"Feed-in pacing should limit power below inverter max, "
            f"but got {initial_power}W (max={max_power}W)"
        )


class TestSchedulerHandback:
    """Opt-in scheduler handback, through a real HA options flow (#16, #4).

    The unit suite drives the same code against the same simulator, but
    only this can prove the option is actually reachable end to end: that
    turning it on in the options flow re-captures the user's floor, that a
    real service-call teardown releases the inverter, and — the regression
    that would matter most — that the *next* session still takes effect
    once the Mode Scheduler master switch has been off.
    """

    def test_handback_releases_the_inverter_and_the_next_session_works(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        if connection_mode != "cloud":
            pytest.skip("the Mode Scheduler master switch is a cloud concept")
        assert foxess_sim is not None
        # A 4% floor: below the Mode Scheduler's declared 10% minimum, so
        # it is only expressible through the direct settings — issue #4.
        foxess_sim.set(min_soc_on_grid=4, soc=80, solar_kw=0, load_kw=0.5)
        # Opting in is the one moment a re-capture is safe: whatever the
        # register holds now is by definition the user's own value, because
        # the integration has not been writing to it.
        ha_e2e.set_options(scheduler_handback=True)

        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        assert foxess_sim.state()["scheduler_enabled"] is True, (
            "a session must run with the Mode Scheduler on, or its group "
            "cannot drive the inverter"
        )

        # Stand in for the defect this integration has already shipped:
        # while the session owns the device, the persistent floor and the
        # direct work mode drift away from what the user chose.  Without
        # this the assertions below would be vacuously true.
        foxess_sim.set(min_soc_on_grid=55, work_mode_direct="Feedin")

        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        ha_e2e.wait_for_state("sensor.foxess_smart_operations", "idle", timeout_s=120)

        # ``clear_overrides`` awaits the handback inline and
        # ``call_service`` blocks, so device state is final here — no sleep
        # and no polling needed.
        state = foxess_sim.state()
        assert state["scheduler_enabled"] is False, (
            "the Mode Scheduler master switch is still on — the inverter is "
            "still scheduler-controlled and local Modbus control stays "
            "blocked (issue #16)"
        )
        assert state["work_mode_direct"] == "SelfUse", (
            f"the inverter's own work mode is {state['work_mode_direct']!r}; "
            "with the scheduler off this is what it actually does"
        )
        assert state["min_soc_on_grid"] == 4, (
            "the user's own 4% floor was not restored — the inverter will "
            "import from the grid to hold a level they never chose"
        )

        # The regression Task 1 built _ensure_scheduler_enabled for: a
        # session written after the switch went off must still fire.
        start2, end2 = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start2, "end_time": end2, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        assert foxess_sim.state()["scheduler_enabled"] is True, (
            "the session after a handback wrote its schedule behind a "
            "disabled master switch: errno 0, nothing surfaced to the user, "
            "and no discharge"
        )

    def test_default_install_leaves_the_scheduler_alone(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """The upgrade-safety guarantee, in a real HA with real defaults.

        Nothing is opted into and nothing is configured: exactly what
        hundreds of existing installs look like the moment they upgrade.
        A full session and teardown must leave the master switch on, the
        direct work-mode setting untouched, and the persistent Min SoC
        register untouched.
        """
        if connection_mode != "cloud":
            pytest.skip("the Mode Scheduler master switch is a cloud concept")
        assert foxess_sim is not None
        foxess_sim.set(
            min_soc_on_grid=17, work_mode_direct="Feedin", soc=80, load_kw=0.5
        )

        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        ha_e2e.wait_for_state("sensor.foxess_smart_operations", "idle", timeout_s=120)

        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True
        assert state["work_mode_direct"] == "Feedin"
        assert state["min_soc_on_grid"] == 17


class TestFaultInjection:
    def test_ws_unit_mismatch_handled(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """WS sends kW instead of W — integration handles it."""
        if connection_mode != "cloud":
            pytest.skip("WS fault injection requires cloud mode")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 20},
        )

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        foxess_sim.ws_unit("kW")
        foxess_sim.fast_forward(60, step=5)

        soc = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "lt", 80.0, timeout_s=60
        )
        assert soc < 80
        foxess_sim.ws_unit("W")


class TestDataSource:
    def test_api_source_when_idle(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """When idle with WS blocked, data source should be API."""
        if connection_mode != "cloud":
            pytest.skip("data_source attribute is cloud-specific")
        # Block WS to ensure data_source deterministically reverts to
        # "api".  ws_refuse also disconnects existing WS clients.
        if foxess_sim is not None:
            foxess_sim.fault("ws_refuse")
        ha_e2e.wait_for_state("sensor.foxess_smart_operations", "idle", timeout_s=30)
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "api",
            timeout_s=60,
        )
        if foxess_sim is not None:
            foxess_sim.clear_fault()

    def test_ws_always_connects_without_session(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """ws_mode=always connects WS at startup without a session."""
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        foxess_sim.set(soc=50, solar_kw=1.0, load_kw=0.3)
        ha_e2e.set_options(ws_mode="always")

        ha_e2e.wait_for_state("sensor.foxess_smart_operations", "idle", timeout_s=60)
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

    def test_ws_recovers_after_stream_stolen(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """WS must recover when another client steals the data stream.

        Simulates the user opening the FoxESS app while a smart
        discharge is running.  The app opens a new WS connection to
        the cloud, which starves the integration's existing WS (no
        more data messages, but the TCP connection stays alive via
        heartbeats).  The integration must detect the stale stream
        and reconnect.
        """
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")

        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)
        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # Confirm WS is active
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

        # Steal the stream: open a competing WS connection to the
        # simulator.  The push loop only sends to the newest client,
        # so the integration's connection goes silent.
        import websocket as _websocket

        sim_url = foxess_sim.url.replace("http://", "ws://")
        thief = _websocket.create_connection(
            f"{sim_url}/dew/v0/wsmaitian",
            timeout=5,
        )
        thief.send("getdata")
        thief.recv()  # consume initial message

        # data_source should revert to "api" once the integration
        # detects stale WS (30s timeout) and the reconnect kicks the
        # thief off.  Then data_source should flip back to "ws".
        #
        # Wait for data_source to return to "ws" — proving the
        # integration recovered from the stolen stream.
        # Verify data_source drops to "api" (stream is dead)
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "api",
            timeout_s=45,
        )

        # Now wait for recovery — WS should reconnect and become
        # the newest client, stealing back the stream.
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

        thief.close()

    def test_ws_connects_on_second_session(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """WS must connect on a second discharge session after the first ends.

        Reproduces production bug: WS works in session 1, session ends
        (WS disconnects via linger), session 2 starts, WS never
        connects. No restart involved — just two back-to-back sessions
        in the same HA instance.
        """
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")

        # --- Session 1 ---
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)
        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

        # End session 1 — cancel via clear_overrides (not natural end)
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=60,
        )

        # Do NOT wait for WS linger — start session 2 immediately,
        # matching the production scenario where the user cancelled
        # and started a new session.
        foxess_sim.set(soc=75, solar_kw=0, load_kw=0.5)
        start2, end2 = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start2, "end_time": end2, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # WS must connect again in session 2
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

    def test_ws_connects_after_deferred_start(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """WS must connect when a deferred discharge transitions to active.

        Reproduces production bug: session starts deferred (SoC headroom
        means forced discharge isn't needed yet).  The initial
        _maybe_start_realtime_ws call is skipped for deferred sessions.
        When the deferred phase ends and forced discharge begins, the
        periodic timer callback must trigger WS — but the timer uses
        the unwrapped callback that doesn't call _maybe_start_realtime_ws.

        Setup: low energy to discharge (SoC 25%, min_soc 20%) with a
        10-min window.  At 10.5 kW max power the ~0.5 kWh discharge
        only needs ~3.3 min (with headroom), so the algorithm defers
        for ~5 min before starting forced discharge.

        The SoC must propagate to the coordinator BEFORE the service
        call, otherwise the coordinator's default 50% SoC makes the
        energy estimate too large and skips deferral entirely.
        """
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=25, solar_kw=0, load_kw=0.3)

        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc",
            "le",
            26,
            timeout_s=90,
        )

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 20},
        )

        # Session should start in deferred phase
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharge_deferred",
            timeout_s=60,
        )

        # Wait for deferred→active transition (up to ~7 min)
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=420,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # WS must connect after the deferred→active transition
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

    def test_ws_mode_persists_via_options_flow(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """ws_mode set via options flow must be persisted and effective.

        Steps:
        1. Set ws_mode to smart_sessions via the options flow REST API
        2. Start a discharge and verify WS activates
        """
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")

        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)
        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # If ws_mode was persisted, WS should connect
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

    def test_ws_reconnects_after_reload_at_max_power(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """WS must reconnect after integration reload during discharge.

        Reproduces the production bug: WS is active during discharge,
        HA restarts (simulated via config entry reload), session resumes,
        but WS fails to reconnect because the WS lifecycle isn't
        re-established after session recovery.
        """
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # Confirm WS is active before reload
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

        ha_e2e.reload_integration()

        # Wait for session to resume after reload
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # WS must reconnect after reload
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

    def test_ws_linger_captures_post_discharge_data(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """After session end, WS linger must capture self-use data, not stale discharge.

        Reproduces D-009 regression: the linger task starts before the
        override removal API call completes, so it captures a WS push
        that still shows forced-discharge values.  After the linger
        disconnects, the coordinator is left with stale discharge data
        and data_source incorrectly set to "api" without having seen
        the real post-session state.

        The correct behaviour: after session end and override removal,
        the discharge rate entity should show 0 (self-use) and
        data_source should revert to "api".
        """
        if connection_mode != "cloud":
            pytest.skip("WS linger is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

        # Confirm discharge rate is non-zero while discharging
        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_discharge_rate",
            "ge",
            0.1,
            timeout_s=30,
        )

        # Zero out load so self-use produces no discharge — this lets us
        # distinguish "linger captured post-session data" from "linger
        # captured stale forced-discharge data".
        foxess_sim.set(load_kw=0)

        # End session via clear_overrides
        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=60,
        )

        # After linger completes (~30s max), data_source should revert
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "api",
            timeout_s=60,
        )

        # The discharge rate should reflect self-use with no load (0),
        # not the stale forced-discharge value captured during linger.
        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_discharge_rate",
            "le",
            0.05,
            timeout_s=60,
        )

    def test_ws_disconnects_after_natural_session_end_and_stays_disconnected(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """After natural session-end the WS must disconnect and stay disconnected.

        Reproduces the 2026-05-31 leak: when a smart-discharge session
        ends via its end-time timer (``_on_timer_expire``), the
        WebSocket goes through ``_stop_realtime_ws`` and its 30s
        linger phase, then ``async_disconnect`` is awaited.  In
        production this leaves the WS in a state where the listen
        loop's reconnect path (``_try_reconnect``) re-establishes
        the connection ~13s later and keeps pumping data
        indefinitely — even though no smart session is running.

        Contract under test (the test asserts only on observable
        behaviour, not internals): after the session goes ``idle``
        via natural timer expiry, ``sensor.foxess_battery_soc``'s
        ``data_source`` attribute must remain ``"api"`` for at
        least 60 seconds.  The first ``"ws"`` reading fails the
        test — there is no path on which the WS should re-emerge
        without a new session.

        Contrast with ``test_ws_linger_captures_post_discharge_data``
        which exercises ``clear_overrides`` (explicit cancel) — that
        path does not trigger the leak because the override-removal
        sequence completes synchronously before the linger.
        """
        if connection_mode != "cloud":
            pytest.skip("WS lifecycle is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        # Use a deliberately short window so the natural timer fires
        # within the test's lifetime.  ~3 minutes from now: long
        # enough to reach ``discharging`` + WS-up, short enough that
        # the timer fires while we're watching.
        now = datetime.datetime.now(tz=datetime.UTC)
        # Backshift start by 1 minute so the schedule is "live" the
        # moment we write it; end ~3 minutes from now.  Floor seconds
        # so the time strings are minute-precise (HH:MM:00 form).
        start_dt = (now - datetime.timedelta(minutes=1)).replace(
            second=0, microsecond=0
        )
        end_dt = (now + datetime.timedelta(minutes=3)).replace(second=0, microsecond=0)
        # Avoid midnight crossing (C-009): if start is on day N and
        # end would land on day N+1 we cannot represent it as a
        # single-day window.  In that case skip the test rather than
        # introduce timing flakiness — the next CI run should miss
        # the boundary.
        if end_dt.date() != start_dt.date():
            pytest.skip("midnight boundary — would require cross-day schedule (C-009)")
        start = start_dt.strftime("%H:%M:%S")
        end = end_dt.strftime("%H:%M:%S")

        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

        # Wait for the natural timer to fire — do NOT call
        # clear_overrides.  ``_on_timer_expire`` runs at ``end_dt``
        # and transitions the session to idle via
        # ``cancel_smart_discharge`` + the deferred ``_stop_realtime_ws``
        # coroutine.  Allow a generous timeout (window plus slack)
        # because the underlying ``async_track_point_in_time`` may
        # fire a few seconds late under container load.
        timer_remaining_s = (
            end_dt - datetime.datetime.now(tz=datetime.UTC)
        ).total_seconds()
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=max(120.0, timer_remaining_s + 60.0),
        )

        # Now poll continuously for 60s asserting data_source stays
        # "api".  Allow a brief grace period (linger is up to 30s,
        # plus the post-linger ``async_disconnect`` settle) before
        # the first assertion to give ``_stop_realtime_ws`` time to
        # complete normally — the bug is about RECONNECT, not about
        # the linger itself, so we measure 60s AFTER the linger
        # would have completed under any reasonable timing.
        deadline = time.monotonic() + 90.0  # 30s linger window + 60s observation
        first_observed_at: float | None = None
        observation_start = time.monotonic()
        observed_values: list[tuple[float, str | None]] = []
        while time.monotonic() < deadline:
            attrs = ha_e2e.get_attributes("sensor.foxess_battery_soc")
            ds = attrs.get("data_source")
            elapsed = time.monotonic() - observation_start
            observed_values.append((elapsed, ds))
            # During the first 30s, allow data_source to be either
            # "ws" (linger still running, captured a post-session
            # frame) or "api" (linger completed / session reverted).
            # After 30s, the WS must be fully torn down — any "ws"
            # reading is the bug.
            if elapsed >= 30.0:
                if first_observed_at is None:
                    first_observed_at = elapsed
                if ds == "ws":
                    sample_summary = ", ".join(
                        f"t={t:.1f}s:{v!r}" for t, v in observed_values[-10:]
                    )
                    raise AssertionError(
                        "data_source returned to 'ws' "
                        f"{elapsed:.1f}s after session-end "
                        f"(first observation at {first_observed_at:.1f}s; "
                        f"recent samples: {sample_summary}). "
                        "Expected 'api' continuously — natural session-end "
                        "must leave the WebSocket disconnected."
                    )
            time.sleep(5.0)

        # Sanity: confirm we observed at least one post-30s sample
        assert first_observed_at is not None, (
            f"observation loop completed without any post-30s samples "
            f"(captured {len(observed_values)} samples)"
        )


class TestEnergyDashboardSolarSource:
    """The entity HA presents as solar energy must be PV-only.

    Production report (KH10, 2026-08-26): users wired the sensor named
    "Solar Generation Energy" (German "Solarenergie",
    ``sensor.foxess_solarenergie``) into the HA Energy dashboard as their
    *solar* source.  It is fed by the FoxESS ``generation`` variable — the
    inverter's cumulative AC **output** energy — so it climbed all night
    while the battery discharged, double-counting the battery (one night's
    home consumption came out as 26.6 kWh against a real 7.2 kWh load).

    ``sensor.foxess_solar_pv_energy`` (``PVEnergyTotal``) is the PV-only
    counter and the correct Energy-dashboard solar source;
    ``sensor.foxess_inverter_output_energy`` is the relabelled output
    counter.  Both are asserted here because the fix is the *pair*: a new
    PV sensor is useless if the mislabelled one still reads as solar.
    """

    def test_pv_energy_flat_overnight_while_output_energy_rises(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        if connection_mode != "cloud":
            pytest.skip("cloud cumulative energy counters are cloud-mode only")
        assert foxess_sim is not None

        pv_entity = "sensor.foxess_solar_pv_energy"
        out_entity = "sensor.foxess_inverter_output_energy"

        # Night: no sun, house load carried entirely by the battery.
        foxess_sim.set(fuzzing=False, soc=90, solar_kw=0.0, load_kw=1.5)
        # Gate on the renamed *output power* sensor reaching the new load.
        # It is not one of the median-filtered WS power channels
        # (_WS_MEDIAN_FILTERED_VARIABLES), so it tracks the very first poll
        # that sees the new simulator state — unlike sensor.foxess_solar_power
        # or _house_load, which need three samples to clear the filter.
        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_inverter_output", "ge", 1.4, timeout_s=180
        )
        # Both energy entities must exist and carry a numeric reading (proves
        # the new variable is polled and the rename kept a working entity).
        pv_before = ha_e2e.wait_for_numeric_state(pv_entity, "ge", 0.0, timeout_s=120)
        out_before = ha_e2e.wait_for_numeric_state(out_entity, "ge", 0.0, timeout_s=120)

        # 4 h of night.
        foxess_sim.fast_forward(4 * 3600, step=900)

        # The next poll must show the inverter having put ~6 kWh out...
        out_after = ha_e2e.wait_for_numeric_state(
            out_entity, "gt", out_before + 1.0, timeout_s=300
        )
        # ...and the PV counter must not have budged.
        pv_after = float(ha_e2e.get_state(pv_entity))
        assert float(ha_e2e.get_state("sensor.foxess_solar_power")) == 0.0
        assert pv_after == pytest.approx(pv_before, abs=0.01), (
            f"{pv_entity} rose {pv_after - pv_before:.3f} kWh overnight with "
            f"zero solar power while inverter output rose "
            f"{out_after - out_before:.3f} kWh — the Energy dashboard's solar "
            f"source is counting battery discharge as solar yield"
        )

    def test_pv3_and_pv4_report_per_string_power(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Issue #15: multi-MPPT inverters expose PV3/PV4 in Home Assistant.

        Disabled by default, following the PV1/PV2 pattern, so the test
        enables them and reloads.  The observable contract is that a
        four-string inverter's per-string readings reach HA as distinct
        values — not that they merely exist.
        """
        if connection_mode != "cloud":
            pytest.skip("per-string PV variables are cloud-mode only")
        assert foxess_sim is not None

        foxess_sim.set(
            fuzzing=False,
            soc=70,
            solar_kw=4.0,
            load_kw=1.0,
            pv3_kw=1.25,
            pv4_kw=0.75,
        )
        for eid in ("sensor.foxess_pv3_power", "sensor.foxess_pv4_power"):
            ha_e2e.enable_entity(eid)
        ha_e2e.reload_integration()

        # Distinct per-string values, so a single shared reading would fail.
        pv3 = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_pv3_power", "ge", 1.2, timeout_s=180
        )
        pv4 = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_pv4_power", "ge", 0.7, timeout_s=180
        )
        assert pv3 > pv4, (
            f"PV3 ({pv3}) and PV4 ({pv4}) must carry their own readings; "
            f"the simulator was set to 1.25 and 0.75 kW respectively"
        )

    def test_pv_energy_rises_with_sun_and_stays_below_output(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Daytime with the battery also discharging: PV yield must rise,
        but by strictly less than inverter output."""
        if connection_mode != "cloud":
            pytest.skip("cloud cumulative energy counters are cloud-mode only")
        assert foxess_sim is not None

        pv_entity = "sensor.foxess_solar_pv_energy"
        out_entity = "sensor.foxess_inverter_output_energy"

        foxess_sim.set(fuzzing=False, soc=80, solar_kw=3.0, load_kw=5.0)
        # Output power == load here (no export, no import).  Gated on the
        # unfiltered output sensor rather than sensor.foxess_solar_power,
        # which is median-filtered and needs three polls to settle.
        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_inverter_output", "ge", 4.9, timeout_s=180
        )
        pv_before = ha_e2e.wait_for_numeric_state(pv_entity, "ge", 0.0, timeout_s=120)
        out_before = ha_e2e.wait_for_numeric_state(out_entity, "ge", 0.0, timeout_s=120)

        foxess_sim.fast_forward(2 * 3600, step=900)

        pv_after = ha_e2e.wait_for_numeric_state(
            pv_entity, "gt", pv_before + 1.0, timeout_s=300
        )
        out_after = float(ha_e2e.get_state(out_entity))
        pv_delta = pv_after - pv_before
        out_delta = out_after - out_before
        assert pv_delta < out_delta, (
            f"PV energy rose {pv_delta:.3f} kWh but inverter output rose only "
            f"{out_delta:.3f} kWh — with 3 kW PV, 5 kW load and the battery "
            f"covering the shortfall, output must exceed PV yield"
        )


# ---------------------------------------------------------------------------
# Entity-mode-only tests
# ---------------------------------------------------------------------------


class TestEntityMode:
    def test_work_mode_entity_updated(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Discharge sets work_mode entity to Force Discharge."""
        if connection_mode != "entity":
            pytest.skip("entity-mode only")
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=80,
            load_kw=0.5,
        )

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        mode = event_stream.wait_for_state(
            "input_select.foxess_work_mode",
            "Force Discharge",
            timeout_s=90,
            rest_client=ha_e2e,
        )
        assert mode == "Force Discharge"

    def test_power_entity_written(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Discharge writes a power value to the discharge_power entity."""
        if connection_mode != "entity":
            pytest.skip("entity-mode only")
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=80,
            load_kw=0.5,
        )

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # Wait for the entity adapter to write power — use REST polling
        # with a long timeout since the initial apply_mode happens on
        # the first listener tick after deferred start completes.
        power = ha_e2e.wait_for_numeric_state(
            "input_number.foxess_discharge_power",
            "gt",
            0,
            timeout_s=120,
            poll_interval=2.0,
        )
        assert power > 0, "Discharge power entity should be set"

    def test_export_limit_entity_written_at_start(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """When CONF_EXPORT_LIMIT_ENTITY is configured, the entity is
        written to the hardware max on session start, and modulated
        on subsequent ticks as the listener tapers discharge."""
        pytest.skip(
            "Needs foxess_modbus installed in the E2E container + "
            "multi-step set_options helper; CONF_EXPORT_LIMIT_ENTITY lives "
            "in the options-flow modbus sub-step which only appears when "
            "foxess_modbus is present. Unit tests cover seed/taper/revert."
        )
        if connection_mode != "entity":
            pytest.skip("entity-mode only")
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=80,
            load_kw=0.5,
        )

        # Enable the export-limit actuator via options flow.
        ha_e2e.set_options(
            export_limit_entity="input_number.foxess_max_grid_export_limit",
            grid_export_limit=5000,
        )

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # Listener seeds the actuator at grid_export_limit=5000 W.
        value = ha_e2e.wait_for_numeric_state(
            "input_number.foxess_max_grid_export_limit",
            "eq",
            5000,
            timeout_s=60,
            poll_interval=2.0,
        )
        assert value == 5000

    def test_self_use_on_clear(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """clear_overrides reverts work mode to Self Use."""
        if connection_mode != "entity":
            pytest.skip("entity-mode only")
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=80,
            load_kw=0.5,
        )

        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        ha_e2e.call_service("foxess_control", "clear_overrides", {})
        event_stream.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=30,
            rest_client=ha_e2e,
        )

        mode = ha_e2e.get_state("input_select.foxess_work_mode")
        assert mode == "Self Use"

    def test_entity_mode_charge_lifecycle(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Entity-mode charge: start → SoC rises → session completes.

        When SoC reaches target the charge listener stops charging but
        keeps monitoring until the window expires.  We use a short (5 min)
        window so the session reaches idle promptly after target is hit.
        """
        if connection_mode != "entity":
            pytest.skip("entity-mode only")
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=20,
            load_kw=0.3,
        )

        start, end = _tight_window(5)
        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {"start_time": start, "end_time": end, "target_soc": 50},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "charging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        mode = ha_e2e.get_state("input_select.foxess_work_mode")
        assert mode == "Force Charge"

        ha_e2e.set_input_number("input_number.foxess_soc", 50.0)

        # After SoC reaches target the session monitors until the window
        # expires.  With a 5 min window (starting 2 min before now),
        # expiry is ~3 min away.  Add buffer for charge tick intervals.
        event_stream.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=660,
            rest_client=ha_e2e,
        )

        mode = ha_e2e.get_state("input_select.foxess_work_mode")
        assert mode == "Self Use"

    def test_entity_mode_discharge_ends_at_min_soc(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Entity-mode discharge ends session when SoC reaches min_soc.

        When SoC drops to min_soc the discharge listener confirms
        over two ticks then removes the override → session ends (idle).
        """
        if connection_mode != "entity":
            pytest.skip("entity-mode only")
        set_inverter_state(
            connection_mode,
            foxess_sim,
            ha_e2e,
            event_stream=event_stream,
            soc=80,
            load_kw=0.5,
        )

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        ha_e2e.set_input_number("input_number.foxess_soc", 30.0)

        # SoC at min_soc ends the session after two confirmation ticks
        # (each 60s).  Session transitions to idle, not suspended.
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=180,
        )

        mode = ha_e2e.get_state("input_select.foxess_work_mode")
        assert mode == "Self Use"


# ---------------------------------------------------------------------------
# Integration reload / HA restart recovery
# ---------------------------------------------------------------------------


class TestReloadRecovery:
    """Session recovery after integration reload (simulated HA restart)."""

    def test_discharge_resumes_after_reload(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Active discharge session resumes after reload with power > 0."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=80, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        _wait_for_positive_attr(
            ha_e2e, "sensor.foxess_smart_operations", "discharge_target_power_w"
        )

        ha_e2e.reload_integration()

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        _wait_for_positive_attr(
            ha_e2e, "sensor.foxess_smart_operations", "discharge_target_power_w"
        )

    def test_charge_resumes_after_reload(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Active charge session resumes after reload."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=30, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {"start_time": start, "end_time": end, "target_soc": 80},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "charging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        ha_e2e.reload_integration()

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "charging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

    def test_ws_reconnects_after_discharge_reload(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """WS data source recovers after reload during paced discharge."""
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

        ha_e2e.reload_integration()

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

    def test_ws_reconnects_after_charge_reload(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """WS data source recovers after reload during charge."""
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=30, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_charge",
            {"start_time": start, "end_time": end, "target_soc": 80},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "charging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

        ha_e2e.reload_integration()

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "charging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=90,
        )

    def test_idle_after_reload_with_no_session(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Reload with no active session stays idle."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=80, load_kw=0.5)

        state = ha_e2e.get_state("sensor.foxess_smart_operations")
        assert state == "idle"

        ha_e2e.reload_integration()

        state = ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=60,
        )
        assert state == "idle"

    def test_session_clears_after_window_expires_during_reload(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """No phantom session after reload when window already expired."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=80, load_kw=0.5)

        start, end_str = _tight_window(4)

        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end_str, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=300,
        )

        ha_e2e.reload_integration()

        state = ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "idle",
            timeout_s=60,
        )
        assert state == "idle"

    def test_bms_temperature_recovers_after_reload(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """BMS battery temperature sensor recovers after reload.

        Requires cloud mode: the simulator serves the WS compound ID
        (for discovery) and /dew/v0/device/detail (for temperature).
        """
        if connection_mode != "cloud":
            pytest.skip("BMS temperature requires cloud mode (web session)")
        assert foxess_sim is not None
        foxess_sim.set(soc=50, battery_temperature=32.5)

        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_bms_battery_temperature",
            "ge",
            20.0,
            timeout_s=120,
        )

        temp_before = float(ha_e2e.get_state("sensor.foxess_bms_battery_temperature"))
        assert 25.0 <= temp_before <= 40.0, (
            f"Expected temperature near 32.5°C, got {temp_before}"
        )

        foxess_sim.set(battery_temperature=28.0)
        ha_e2e.reload_integration()

        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_bms_battery_temperature",
            "ge",
            20.0,
            timeout_s=120,
        )

        temp_after = float(ha_e2e.get_state("sensor.foxess_bms_battery_temperature"))
        assert 20.0 <= temp_after <= 35.0, (
            f"Expected temperature near 28.0°C after reload, got {temp_after}"
        )

    def test_entity_mode_discharge_resumes_after_reload(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Entity-mode discharge resumes after reload (no schedule group check)."""
        if connection_mode != "entity":
            pytest.skip("Entity-mode-specific test")
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=80, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        ha_e2e.reload_integration()

        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        mode = ha_e2e.get_state("input_select.foxess_work_mode")
        assert mode == "Force Discharge"


# ---------------------------------------------------------------------------
# Fault recovery (cloud only) — circuit breaker + transient fault survival
# ---------------------------------------------------------------------------


class TestFaultRecovery:
    def test_api_down_during_discharge_opens_circuit_breaker(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """api_down → circuit breaker opens → session holds position."""
        if connection_mode != "cloud":
            pytest.skip("Fault injection requires cloud mode")
        assert foxess_sim is not None
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=180,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        foxess_sim.fault("api_down")

        # Discharge ticks every 60s — after 3 consecutive errors the
        # circuit breaker opens.  The FoxESS client retries 503 errors
        # internally (TRANSIENT_RETRIES=3 with exponential backoff),
        # so each failed tick may take ~30-45s of retry time on top of
        # the 60s interval.  Budget 600s to cover worst case.
        deadline = time.monotonic() + 600
        breaker_active = False
        while time.monotonic() < deadline:
            attrs = ha_e2e.get_attributes("sensor.foxess_smart_operations")
            if attrs.get("circuit_breaker_active") is True:
                breaker_active = True
                break
            time.sleep(5)
        assert breaker_active, (
            "Circuit breaker should activate after consecutive errors"
        )

        state = ha_e2e.get_state("sensor.foxess_smart_operations")
        assert state == "discharging", (
            "Session must hold position while breaker is open"
        )

        foxess_sim.clear_fault()

    def test_rate_limit_transient_discharge_survives(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Transient rate_limit (count=2) does not abort discharge."""
        if connection_mode != "cloud":
            pytest.skip("Fault injection requires cloud mode")
        assert foxess_sim is not None
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        foxess_sim.fault("rate_limit", count=2)

        # Advance simulator time so SoC drops. If the session survived
        # the transient faults, the schedule is still active and SoC
        # will decrease. Discharge ticks every 60s; the 2 rate-limit
        # errors are consumed by the first 2 ticks, then subsequent
        # ticks succeed normally.
        foxess_sim.fast_forward(300, step=5)

        soc = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "lt", 80.0, timeout_s=120
        )
        assert soc < 80, "SoC should drop, proving session survived rate-limit"

        state = ha_e2e.get_state("sensor.foxess_smart_operations")
        assert state == "discharging", (
            "Session should survive transient rate-limit errors"
        )

    def test_ws_refuse_falls_back_to_api_during_session(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """WS refused during active session → data_source falls back to api."""
        if connection_mode != "cloud":
            pytest.skip("WS fault injection requires cloud mode")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        foxess_sim.fault("ws_refuse")

        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "api",
            timeout_s=60,
        )

        state = ha_e2e.get_state("sensor.foxess_smart_operations")
        assert state == "discharging", "Session must continue on API fallback"

        foxess_sim.clear_fault()

    def test_ws_disconnect_recovers(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """WS disconnect → data_source drops → clear fault → WS recovers."""
        if connection_mode != "cloud":
            pytest.skip("WS fault injection requires cloud mode")
        assert foxess_sim is not None
        ha_e2e.set_options(ws_mode="smart_sessions")
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=60,
        )

        foxess_sim.fault("ws_disconnect")

        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "api",
            timeout_s=60,
        )

        foxess_sim.clear_fault()

        ha_e2e.wait_for_attribute(
            "sensor.foxess_battery_soc",
            "data_source",
            "ws",
            timeout_s=120,
        )

        state = ha_e2e.get_state("sensor.foxess_smart_operations")
        assert state == "discharging", "Session must survive WS reconnection"

    def test_api_500_transient_recovery(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        event_stream: HAEventStream,
    ) -> None:
        """Transient API 500 (count=2) does not kill the discharge session."""
        if connection_mode != "cloud":
            pytest.skip("Fault injection requires cloud mode")
        assert foxess_sim is not None
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)

        start, end = _tight_window(15)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        foxess_sim.fault("api_500", count=2)

        # Advance simulator time so SoC drops. If the session survived,
        # the schedule is still active and energy flows.
        foxess_sim.fast_forward(300, step=5)

        soc = ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "lt", 80.0, timeout_s=120
        )
        assert soc < 80, "SoC should drop, proving session survived API 500"

        state = ha_e2e.get_state("sensor.foxess_smart_operations")
        assert state == "discharging", "Session should survive transient API 500 errors"
