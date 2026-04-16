"""End-to-end tests: real HA container + FoxESS simulator / input helpers.

Run with: pytest e2e/ -m slow
Requires: podman, PyJWT

Fixture scoping:
- connection_mode: session — "cloud" or "entity"
- foxess_sim + ha_e2e: session scope (one per xdist worker)
- _e2e_reset: autouse function scope (resets sim/entities + clears HA)
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest

from .conftest import set_inverter_state
from .ha_client import FATAL_FOR_ACTIVE, HAEventStream

if TYPE_CHECKING:
    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow


def _tight_window(minutes: int = 30) -> tuple[str, str]:
    """Return a tight window starting ~now (UTC)."""
    now = datetime.datetime.now(tz=datetime.UTC)
    start = now - datetime.timedelta(minutes=2)
    end = start + datetime.timedelta(minutes=minutes)
    return (
        f"{start.hour:02d}:{start.minute:02d}:00",
        f"{end.hour:02d}:{end.minute:02d}:00",
    )


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
    ) -> None:
        """Fast-forward and verify SoC decreases (cloud only)."""
        if connection_mode != "cloud":
            pytest.skip("requires simulator fast_forward")
        assert foxess_sim is not None
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

        foxess_sim.fast_forward(600, step=5)

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
        import time as _time

        _time.sleep(5)  # let the steal take effect

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

    def test_ws_all_sessions_persists_via_options_flow(
        self,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """ws_all_sessions set via options flow must be persisted and effective.

        Reproduces the production bug: user checks ws_all_sessions in
        the UI, but the option isn't saved to entry.options, so WS
        never activates at max power.

        Steps:
        1. Start with ws_all_sessions absent from options
        2. Set it via the options flow REST API
        3. Verify it appears in the config entry
        4. Start a discharge and verify WS activates
        """
        if connection_mode != "cloud":
            pytest.skip("WS is cloud-specific")
        assert foxess_sim is not None
        import requests as _requests

        session = _requests.Session()
        session.headers.update(
            {
                "Authorization": ha_e2e._session.headers["Authorization"],
                "Content-Type": "application/json",
            }
        )

        # Get current config entry
        entries = session.get(
            f"{ha_e2e.base_url}/api/config/config_entries/entry"
        ).json()
        foxess_entry = next(e for e in entries if e["domain"] == "foxess_control")
        entry_id = foxess_entry["entry_id"]

        # Start options flow
        r = session.post(
            f"{ha_e2e.base_url}/api/config/config_entries/options/flow",
            json={"handler": entry_id},
        )
        assert r.ok, f"Options flow start failed: {r.status_code}"
        flow = r.json()
        assert flow.get("type") == "form", f"Expected form, got {flow}"
        flow_id = flow["flow_id"]

        # Build submission from schema defaults + ws_all_sessions=True
        schema = flow.get("data_schema", [])
        submit_data = {}
        for field in schema:
            name = field.get("name", "")
            submit_data[name] = field.get("default")
        submit_data["ws_all_sessions"] = True

        # Submit the options
        r = session.post(
            f"{ha_e2e.base_url}/api/config/config_entries/options/flow/{flow_id}",
            json=submit_data,
        )
        assert r.ok, f"Options flow submit failed: {r.status_code} {r.json()}"

        # Verify the option was persisted by reloading and checking
        # that WS activates during a discharge
        r = session.post(
            f"{ha_e2e.base_url}/api/config/config_entries/entry/{entry_id}/reload"
        )
        assert r.ok, f"Reload failed: {r.status_code}"

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

        # If ws_all_sessions was persisted, WS should connect
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

        # Reload the integration — simulates HA restart.
        # Triggers async_unload_entry + async_setup_entry, which
        # recovers the session from persistent storage.
        import requests as _requests

        session = _requests.Session()
        session.headers.update(
            {
                "Authorization": ha_e2e._session.headers["Authorization"],
                "Content-Type": "application/json",
            }
        )
        entries = session.get(
            f"{ha_e2e.base_url}/api/config/config_entries/entry"
        ).json()
        foxess_entry = next(e for e in entries if e["domain"] == "foxess_control")
        r = session.post(
            f"{ha_e2e.base_url}/api/config/config_entries/entry/"
            f"{foxess_entry['entry_id']}/reload"
        )
        assert r.ok, f"Reload failed: {r.status_code}"

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
