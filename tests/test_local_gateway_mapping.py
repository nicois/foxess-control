"""Local gateway backend: mapping gateway payloads onto coordinator data.

The coordinator's consumers (sensors, smart-session listeners, the
grid-direction logic) all read the cloud-shaped ``dict[str, float]``:
directional power variables in **kW**, ``SoC`` in %, cumulative
counters in kWh, plus the private ``_work_mode`` / ``_data_source`` /
``_data_last_update`` keys. The local gateway reports signed **watts**
in a different shape. These tests pin the translation, using a real
``GET /api/v1/state`` captured from a KH10 (serial pseudonymised).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.foxess_control.foxess.local_client import (
    LocalGatewayError,
    SSEParser,
    map_snapshot_event,
    map_state,
    problem_to_error,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "local_gateway" / "state_kh10.json"


@pytest.fixture
def state() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_FIXTURE.read_text())
    return copy.deepcopy(data)


class TestMapStatePower:
    """Signed gateway watts become the cloud's directional kW variables."""

    def test_exporting_and_discharging(self, state: dict[str, Any]) -> None:
        state["power"] = {
            "pv_w": 0,
            "grid_w": -1520,
            "load_w": 1800,
            "ac_port_w": 3320,
            "battery_w": -3320,
            "eps_w": 200,
        }
        data = map_state(state)
        assert data["feedinPower"] == pytest.approx(1.52)
        assert data["gridConsumptionPower"] == 0.0
        assert data["batDischargePower"] == pytest.approx(3.32)
        assert data["batChargePower"] == 0.0
        assert data["loadsPower"] == pytest.approx(1.8)
        assert data["pvPower"] == 0.0
        assert data["generationPower"] == pytest.approx(3.32)
        assert data["epsPower"] == pytest.approx(0.2)

    def test_importing_and_charging(self, state: dict[str, Any]) -> None:
        state["power"] = {
            "pv_w": 500,
            "grid_w": 6000,
            "load_w": 700,
            "ac_port_w": -5800,
            "battery_w": 6300,
            "eps_w": 0,
        }
        data = map_state(state)
        assert data["gridConsumptionPower"] == pytest.approx(6.0)
        assert data["feedinPower"] == 0.0
        assert data["batChargePower"] == pytest.approx(6.3)
        assert data["batDischargePower"] == 0.0
        assert data["pvPower"] == pytest.approx(0.5)
        # The cloud's generationPower is the inverter's AC *output*,
        # floored at zero while it is a net consumer.
        assert data["generationPower"] == 0.0

    def test_eps_absent_reads_zero(self, state: dict[str, Any]) -> None:
        del state["power"]["eps_w"]
        assert map_state(state)["epsPower"] == 0.0


class TestMapStateBatteryAndTemperatures:
    def test_soc_voltage_and_temperatures(self, state: dict[str, Any]) -> None:
        state["battery"].update(soc_pct=68, pack_voltage_v=531.48)
        state["temperatures"] = {
            "battery_c": 27.5,
            "inverter_c": 26.1,
            "ambient_c": 33.3,
            "cell_min_c": 20,
            "cell_max_c": 24.7,
        }
        data = map_state(state)
        assert data["SoC"] == 68
        assert data["batVolt"] == pytest.approx(531.48)
        assert data["batTemperature"] == pytest.approx(27.5)
        assert data["invTemperation"] == pytest.approx(26.1)
        assert data["ambientTemperation"] == pytest.approx(33.3)
        # The coldest cell is the FoxESS portal's "BMS battery temperature".
        assert data["bmsBatteryTemperature"] == pytest.approx(20)

    def test_unreported_values_are_absent_not_zero(self, state: dict[str, Any]) -> None:
        """A ``null`` must read *unavailable* in HA, never a false 0 (C-020)."""
        state["temperatures"]["cell_min_c"] = None
        state["temperatures"]["battery_c"] = None
        state["battery"]["pack_voltage_v"] = None
        data = map_state(state)
        assert "bmsBatteryTemperature" not in data
        assert "batTemperature" not in data
        assert "batVolt" not in data

    def test_missing_temperatures_section(self, state: dict[str, Any]) -> None:
        del state["temperatures"]
        data = map_state(state)
        assert "batTemperature" not in data
        assert data["SoC"] == 68


class TestMapStateWorkMode:
    @pytest.mark.parametrize(
        ("gateway_mode", "expected"),
        [
            ("self-use", "SelfUse"),
            ("feed-in", "Feedin"),
            ("backup", "Backup"),
            ("force-charge", "ForceCharge"),
            ("force-discharge", "ForceDischarge"),
        ],
    )
    def test_active_mode_maps_to_work_mode(
        self, state: dict[str, Any], gateway_mode: str, expected: str
    ) -> None:
        state["active_mode"]["mode"] = gateway_mode
        assert map_state(state)["_work_mode"] == expected

    def test_mode_the_integration_does_not_model_is_none(
        self, state: dict[str, Any]
    ) -> None:
        state["active_mode"]["mode"] = "peak-shaving"
        assert map_state(state)["_work_mode"] is None

    def test_missing_active_mode_is_none(self, state: dict[str, Any]) -> None:
        del state["active_mode"]
        assert map_state(state)["_work_mode"] is None


class TestMapStateEnergyCounters:
    """Lifetime counters, per docs/api/foxess-local-gateway-requirements.md §1."""

    def test_counters_map_to_cloud_names(self, state: dict[str, Any]) -> None:
        state["energy"] = {
            "as_of": "2026-11-19T00:10:00+11:00",
            "grid_export_kwh": 1234.5,
            "grid_import_kwh": 2345.6,
            "pv_kwh": 3456.7,
            "battery_charge_kwh": 456.7,
            "battery_discharge_kwh": 567.8,
            "load_kwh": 6789.0,
            "output_kwh": 7890.1,
        }
        data = map_state(state)
        assert data["feedin"] == pytest.approx(1234.5)
        assert data["gridConsumption"] == pytest.approx(2345.6)
        assert data["PVEnergyTotal"] == pytest.approx(3456.7)
        assert data["chargeEnergyToTal"] == pytest.approx(456.7)
        assert data["dischargeEnergyToTal"] == pytest.approx(567.8)
        assert data["loads"] == pytest.approx(6789.0)
        assert data["generation"] == pytest.approx(7890.1)

    def test_gateway_without_counters_omits_them(self, state: dict[str, Any]) -> None:
        """Today's gateway has no ``energy``: the sensors read unavailable."""
        state.pop("energy", None)
        data = map_state(state)
        for key in ("feedin", "gridConsumption", "PVEnergyTotal", "loads"):
            assert key not in data

    def test_null_counter_is_absent(self, state: dict[str, Any]) -> None:
        state["energy"] = {"grid_export_kwh": 10.0, "pv_kwh": None}
        data = map_state(state)
        assert data["feedin"] == pytest.approx(10.0)
        assert "PVEnergyTotal" not in data


class TestMapStateFreshness:
    def test_source_and_timestamp_come_from_the_inverter_frame(
        self, state: dict[str, Any]
    ) -> None:
        """``_data_last_update`` is the frame time, not "now" — a gateway
        replaying an old snapshot must look old in the UI (C-020)."""
        state["as_of"] = "2026-11-19T00:10:45.158680688+11:00"
        data = map_state(state)
        assert data["_data_source"] == "local"
        assert data["_data_last_update"] == "2026-11-18T13:10:45.158680+00:00"
        assert data["_gateway_online"] is True

    def test_offline_gateway_is_flagged(self, state: dict[str, Any]) -> None:
        state["online"] = False
        assert map_state(state)["_gateway_online"] is False

    def test_never_seen_inverter_has_no_timestamp(self, state: dict[str, Any]) -> None:
        state["as_of"] = None
        state["online"] = False
        data = map_state(state)
        assert "_data_last_update" not in data
        assert data["_gateway_online"] is False


class TestMapSnapshotEvent:
    def test_snapshot_carries_power_and_soc_only(self) -> None:
        event = {
            "ts": "2026-11-19T00:11:00+11:00",
            "power": {
                "pv_w": 2000,
                "grid_w": -500,
                "load_w": 1500,
                "ac_port_w": 1500,
                "battery_w": 0,
            },
            "soc_pct": 71,
            "battery_state": "idle",
        }
        data = map_snapshot_event(event)
        assert data["SoC"] == 71
        assert data["pvPower"] == pytest.approx(2.0)
        assert data["feedinPower"] == pytest.approx(0.5)
        assert data["gridConsumptionPower"] == 0.0
        assert data["_data_source"] == "local"
        assert data["_data_last_update"] == "2026-11-18T13:11:00+00:00"
        assert data["_gateway_online"] is True
        # Values a snapshot does not carry must not be clobbered.
        assert "batTemperature" not in data
        assert "_work_mode" not in data


class TestMapSnapshotEnergy:
    """Snapshots carry ``energy`` once the counters have been read, so the
    feed-in counter the discharge target paces from stays 5 s fresh."""

    def test_snapshot_energy_maps_to_cloud_counters(self) -> None:
        event = {
            "ts": "2026-11-19T00:11:00+11:00",
            "power": {
                "pv_w": 0,
                "grid_w": 0,
                "load_w": 0,
                "ac_port_w": 0,
                "battery_w": 0,
            },
            "soc_pct": 50,
            "battery_state": "idle",
            "energy": {"grid_export_kwh": 2484.8, "pv_kwh": None},
        }
        data = map_snapshot_event(event)
        assert data["feedin"] == pytest.approx(2484.8)
        assert "PVEnergyTotal" not in data

    def test_snapshot_before_first_counter_read_has_no_counters(self) -> None:
        event = {
            "ts": "2026-11-19T00:11:00+11:00",
            "power": {
                "pv_w": 0,
                "grid_w": 0,
                "load_w": 0,
                "ac_port_w": 0,
                "battery_w": 0,
            },
            "soc_pct": 50,
            "battery_state": "idle",
        }
        assert "feedin" not in map_snapshot_event(event)


class TestSSEParser:
    def test_single_event(self) -> None:
        parser = SSEParser()
        events = parser.feed('id: 7\nevent: snapshot\ndata: {"a": 1}\n\n')
        assert events == [(7, "snapshot", {"a": 1})]

    def test_event_split_across_chunks(self) -> None:
        parser = SSEParser()
        assert parser.feed("id: 8\nevent: conn") == []
        assert parser.feed('ection\ndata: {"state": "of') == []
        assert parser.feed('fline"}\n\n') == [(8, "connection", {"state": "offline"})]

    def test_crlf_line_endings(self) -> None:
        parser = SSEParser()
        events = parser.feed('id: 1\r\nevent: gap\r\ndata: {"missed": 3}\r\n\r\n')
        assert events == [(1, "gap", {"missed": 3})]

    def test_multi_line_data_is_joined(self) -> None:
        parser = SSEParser()
        events = parser.feed(
            'event: config\ndata: {"changed":\ndata: ["schedule"]}\n\n'
        )
        assert events == [(None, "config", {"changed": ["schedule"]})]

    def test_comments_and_retry_are_ignored(self) -> None:
        parser = SSEParser()
        events = parser.feed(": keep-alive\nretry: 3000\n\nevent: resync\ndata: {}\n\n")
        assert events == [(None, "resync", {})]

    def test_unparseable_data_is_skipped(self) -> None:
        parser = SSEParser()
        events = parser.feed(
            "event: snapshot\ndata: not json\n\nevent: gap\ndata: {}\n\n"
        )
        assert events == [(None, "gap", {})]

    def test_last_event_id_tracks_the_newest_id(self) -> None:
        parser = SSEParser()
        parser.feed("id: 41\nevent: gap\ndata: {}\n\nid: 42\nevent: gap\ndata: {}\n\n")
        assert parser.last_event_id == 42


class TestProblemDocuments:
    def test_problem_becomes_typed_error(self) -> None:
        err = problem_to_error(
            403,
            {
                "type": "about:blank",
                "title": "Forbidden",
                "status": 403,
                "code": "insufficient_role",
                "detail": "needs standard",
                "required_role": "standard",
                "token_role": "read-only",
            },
        )
        assert isinstance(err, LocalGatewayError)
        assert err.status == 403
        assert err.code == "insufficient_role"
        assert "needs standard" in str(err)
        assert err.problem["required_role"] == "standard"

    def test_non_problem_body_still_yields_status(self) -> None:
        err = problem_to_error(502, None)
        assert err.status == 502
        assert err.code is None
