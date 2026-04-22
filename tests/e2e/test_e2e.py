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

    Avoids midnight crossings (C-009): clamps end to 23:59 and
    ensures start >= 00:00.  When ``now`` is near midnight the
    window shifts so the current minute always falls inside [start, end).
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    now_min = now.hour * 60 + now.minute
    start_min = max(0, now_min - 2)
    end_min = start_min + minutes
    if end_min > 23 * 60 + 59:
        end_min = 23 * 60 + 59
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
