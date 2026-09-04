"""Alternate solar source (AC-coupled): additive extra PV variable."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.foxess_control import coordinator as coordinator_mod
from custom_components.foxess_control.const import (
    CONF_ADDITIONAL_PV_POWER_VARIABLE,
)
from custom_components.foxess_control.coordinator import FoxESSDataCoordinator
from custom_components.foxess_control.domain_data import build_config
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter

if TYPE_CHECKING:
    from .conftest import SimulatorHandle


def test_additional_pv_variable_defaults_to_none() -> None:
    cfg = build_config({})
    assert cfg.additional_pv_power_variable is None


def test_additional_pv_variable_read_from_options() -> None:
    cfg = build_config({CONF_ADDITIONAL_PV_POWER_VARIABLE: "meterPower2"})
    assert cfg.additional_pv_power_variable == "meterPower2"


def test_additional_pv_variable_blank_is_none() -> None:
    # An empty string in options must normalise to None (no extra poll).
    cfg = build_config({CONF_ADDITIONAL_PV_POWER_VARIABLE: ""})
    assert cfg.additional_pv_power_variable is None


def _make_coordinator(hass: Any, inverter: Any) -> FoxESSDataCoordinator:
    # The DataUpdateCoordinator base calls frame.report_usage() at
    # construction, which needs HA's frame helper initialised. Patch it out
    # so the real __init__ runs (matching the pattern in tests/test_coordinator.py).
    with patch("homeassistant.helpers.frame.report_usage"):
        return FoxESSDataCoordinator(hass, inverter, update_interval_seconds=300)


def test_fetch_all_sums_extra_variable_into_pvpower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    inverter.get_real_time.return_value = {"pvPower": 2.0, "meterPower2": 3.0}
    inverter.get_current_mode.return_value = None

    coord = _make_coordinator(hass, inverter)
    data = coord._fetch_all()

    assert data["pvPower"] == 5.0
    assert coord._additional_pv_kw == 3.0
    requested = inverter.get_real_time.call_args.args[0]
    assert "meterPower2" in requested


def test_fetch_all_unset_does_not_request_or_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    inverter.get_real_time.return_value = {"pvPower": 2.0}
    inverter.get_current_mode.return_value = None

    coord = _make_coordinator(hass, inverter)
    data = coord._fetch_all()

    assert data["pvPower"] == 2.0
    assert coord._additional_pv_kw == 0.0
    requested = inverter.get_real_time.call_args.args[0]
    assert "meterPower2" not in requested


def test_fetch_all_garbage_extra_value_adds_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    inverter.get_real_time.return_value = {"pvPower": 2.0}  # meterPower2 missing
    inverter.get_current_mode.return_value = None

    coord = _make_coordinator(hass, inverter)
    data = coord._fetch_all()
    assert data["pvPower"] == 2.0
    assert coord._additional_pv_kw == 0.0


def test_fetch_all_negative_extra_added_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    inverter.get_real_time.return_value = {"pvPower": 2.0, "meterPower2": -1.0}
    inverter.get_current_mode.return_value = None

    coord = _make_coordinator(hass, inverter)
    data = coord._fetch_all()
    assert data["pvPower"] == 1.0  # raw add, no clamp


def test_fetch_all_persistently_missing_var_surfaces_config_error_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured variable that is never returned must surface ONE config
    error after _ADDITIONAL_PV_MISSING_LIMIT consecutive absences (C-020/C-026),
    not silently contribute 0 forever and not spam on every poll."""
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    inverter.get_real_time.return_value = {"pvPower": 2.0}  # meterPower2 missing
    inverter.get_current_mode.return_value = None

    spy = MagicMock()
    monkeypatch.setattr(coordinator_mod, "record_operational_error", spy)

    coord = _make_coordinator(hass, inverter)
    limit = coordinator_mod._ADDITIONAL_PV_MISSING_LIMIT
    assert limit == 3

    # First two absences: still no error (transient blip tolerated).
    coord._fetch_all()
    assert spy.call_count == 0
    coord._fetch_all()
    assert spy.call_count == 0
    # Third consecutive absence: surfaces exactly once.
    coord._fetch_all()
    assert spy.call_count == 1
    # Subsequent absences must NOT re-fire (no spam).
    coord._fetch_all()
    coord._fetch_all()
    assert spy.call_count == 1

    # Verify the call shape: config category, names the variable.
    _logger_arg, _buffer_arg = spy.call_args.args
    kwargs = spy.call_args.kwargs
    assert kwargs["category"] == "config"
    assert "meterPower2" in kwargs["attempted"]
    assert isinstance(kwargs["exc"], BaseException)


def test_fetch_all_present_value_resets_missing_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present value resets the consecutive-absence counter: absent twice,
    present once, absent twice more must NOT fire (only the 3rd *consecutive*
    absence fires)."""
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    inverter.get_current_mode.return_value = None

    spy = MagicMock()
    monkeypatch.setattr(coordinator_mod, "record_operational_error", spy)

    coord = _make_coordinator(hass, inverter)

    absent = {"pvPower": 2.0}
    present = {"pvPower": 2.0, "meterPower2": 3.0}

    inverter.get_real_time.return_value = absent
    coord._fetch_all()
    coord._fetch_all()
    assert spy.call_count == 0

    inverter.get_real_time.return_value = present
    coord._fetch_all()  # resets counter
    assert spy.call_count == 0
    assert coord._additional_pv_missing_count == 0

    inverter.get_real_time.return_value = absent
    coord._fetch_all()
    coord._fetch_all()
    # Only 2 consecutive since reset — still must not fire.
    assert spy.call_count == 0
    # Third consecutive does fire.
    coord._fetch_all()
    assert spy.call_count == 1


def test_ws_inject_adds_held_additional_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    coord = _make_coordinator(hass, inverter)
    coord.data = {"pvPower": 0.0}
    coord._additional_pv_kw = 3.0  # as if a prior REST poll cached it

    coord.inject_realtime_data({"pvPower": 1.5})

    assert coord.data["pvPower"] == 4.5


def test_ws_inject_zero_held_value_no_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    inverter = MagicMock()
    coord = _make_coordinator(hass, inverter)
    coord.data = {"pvPower": 0.0}

    coord.inject_realtime_data({"pvPower": 1.5})

    assert coord.data["pvPower"] == 1.5


# === Simulator-backed end-to-end coverage ===========================
# The mock-based tests above prove the coordinator's sum/inject logic.
# This test closes the remaining gap: it drives the REAL FoxESS REST
# path (FoxESSClient.post -> /op/v0/device/real/query -> Inverter
# ._parse_real_time) against the project simulator, proving that an
# arbitrary configured variable such as ``meterPower2`` (an AC-coupled
# solar inverter on a second meter channel) is served by real/query and
# surfaces in get_real_time's parsed dict — exactly the dict
# ``FoxESSDataCoordinator._fetch_all`` sums into pvPower.  Per CLAUDE.md
# C-028 ("simulator over mocks") this is the canonical integration
# layer; the in-process ``foxess_sim`` fixture runs a real aiohttp app,
# so no container/E2E harness is needed.


def test_simulator_serves_meter_power2_through_real_query(
    foxess_sim: SimulatorHandle,
) -> None:
    """Real Inverter.get_real_time() over the simulator returns both
    pvPower and the configured extra variable meterPower2 with the
    seeded values (fuzzing disabled for determinism)."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
    # Disable jitter so the seeded values are returned verbatim.
    foxess_sim.set(fuzzing=False, solar_kw=2.0, meter_power2_kw=3.0)

    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    inv = Inverter(client, "SIM0001")

    result = inv.get_real_time(["pvPower", "meterPower2"])

    assert result["pvPower"] == 2.0
    assert result["meterPower2"] == 3.0
    # Summing the two (what _fetch_all does) yields the AC-coupled total.
    assert result["pvPower"] + result["meterPower2"] == 5.0


def test_simulator_meter_power2_defaults_to_zero(
    foxess_sim: SimulatorHandle,
) -> None:
    """meterPower2 is served (value 0.0) even when never set, so a
    coordinator configured for it adds zero rather than treating it as
    a persistently-missing variable."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
    foxess_sim.set(fuzzing=False, solar_kw=1.0)

    client = FoxESSClient("test-api-key", base_url=foxess_sim.url)
    inv = Inverter(client, "SIM0001")

    result = inv.get_real_time(["pvPower", "meterPower2"])

    assert result["pvPower"] == 1.0
    assert result["meterPower2"] == 0.0


# === The WebSocket ``aux`` term (issue #18) ==========================
# The AC-coupled generation is `meterPower2` over REST but arrives as
# `aux` on the WebSocket.  Before the fix the WS path never read it, so
# during a smart session the C-006 grid-direction balance was blind to
# AC-coupled generation and reported a site the native app showed
# *exporting* as *importing*.


# julianjwong's readings from issue #18 (H3 5kW Gen 2, AC-coupled):
#   native PV strings 0 kW, house load 1.80 kW, AC-coupled Gen Load
#   3.32 kW, battery 100% at 0 W → native app shows EXPORTING 1.52 kW.
_JULIAN_LOAD_KW = 1.80
_JULIAN_AUX_KW = 3.32
_JULIAN_EXPORT_KW = 1.52


def _julian_ws_frame(aux: object | None = None) -> dict[str, Any]:
    """A wsmaitian frame for julianjwong's site at the reported moment."""
    node: dict[str, Any] = {
        "solar": {"power": {"value": "0", "unit": "W"}},
        "load": {"power": {"value": "1800", "unit": "W"}},
        "bat": {"power": {"value": "0", "unit": "W"}, "soc": 100, "charge": 0},
        "grid": {"power": {"value": "1520", "unit": "W"}, "gridStatus": 2},
    }
    if aux is not None:
        node["aux"] = aux
    return {"errno": 0, "result": {"node": node, "timeDiff": 5}}


def test_ws_inject_does_not_double_add_when_mapper_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The term is added exactly once.

    When the mapper has already folded the AC-coupled term into
    ``pvPower`` it marks the frame; the coordinator must then not add its
    own REST-held copy on top, and the marker must not leak into
    coordinator data.
    """
    from custom_components.foxess_control import domain_data as dd_mod

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    coord = _make_coordinator(hass, MagicMock())
    coord.data = {"pvPower": 0.0}
    coord._additional_pv_kw = 3.0  # stale REST value

    # Mapper already applied a live 3.32 kW aux reading.
    coord.inject_realtime_data({"pvPower": 3.32, "_additional_pv_kw": 3.32})

    assert coord.data is not None
    assert coord.data["pvPower"] == pytest.approx(3.32)
    assert "_additional_pv_kw" not in coord.data


def test_ws_frame_with_aux_reports_export_not_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #18 end to end: battery full, AC-coupled excess → export.

    Drives the real mapper and the real coordinator inject path with
    julianjwong's numbers and asserts the user-visible outcome: Grid
    Feed-in Energy advances, Grid Consumption Energy does not.
    """
    from custom_components.foxess_control import domain_data as dd_mod
    from custom_components.foxess_control.foxess.realtime_ws import (
        map_ws_to_coordinator,
    )

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    coord = _make_coordinator(hass, MagicMock())
    coord.data = {
        "pvPower": 0.0,
        "feedin": 100.0,
        "gridConsumption": 200.0,
        "SoC": 100.0,
    }
    # A configured user always has the last REST poll cached, so this also
    # pins that the term is counted once, not twice.
    coord._additional_pv_kw = _JULIAN_AUX_KW

    frame = _julian_ws_frame({"power": {"value": "3320", "unit": "W"}})
    mapped = map_ws_to_coordinator(frame, additional_pv_enabled=True)

    # The sign must come out as export, matching the native FoxESS app.
    assert mapped["feedinPower"] == pytest.approx(_JULIAN_EXPORT_KW)
    assert mapped["gridConsumptionPower"] == 0.0

    coord.inject_realtime_data(dict(mapped))
    # Simulate 5 s between frames (same technique as tests/test_coordinator.py)
    coord._ws_last_time = time.monotonic() - 5.0
    coord.inject_realtime_data(dict(mapped))

    assert coord.data is not None
    assert coord.data["feedinPower"] == pytest.approx(_JULIAN_EXPORT_KW)
    assert coord.data["gridConsumptionPower"] == 0.0
    # Total generation seen by the algorithm is the AC-coupled output.
    assert coord.data["pvPower"] == pytest.approx(_JULIAN_AUX_KW)
    # Grid Feed-in Energy advances...
    assert coord.data["feedin"] > 100.0
    assert coord.data["feedin"] == pytest.approx(
        100.0 + _JULIAN_EXPORT_KW * 5.0 / 3600.0, rel=0.01
    )
    # ...and Grid Consumption Energy does not.
    assert coord.data["gridConsumption"] == 200.0


def test_ws_frame_without_aux_shows_the_reported_symptom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-fix behaviour, pinned: consumption climbs, feed-in never does.

    Proves the fix's effect comes from the AC-coupled term reaching the
    C-006 balance and not from some unrelated change: with no term
    available the same frame still yields import.
    """
    from custom_components.foxess_control import domain_data as dd_mod
    from custom_components.foxess_control.foxess.realtime_ws import (
        map_ws_to_coordinator,
    )

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    coord = _make_coordinator(hass, MagicMock())
    coord.data = {"pvPower": 0.0, "feedin": 100.0, "gridConsumption": 200.0}

    mapped = map_ws_to_coordinator(_julian_ws_frame(), additional_pv_enabled=True)
    coord.inject_realtime_data(dict(mapped))
    coord._ws_last_time = time.monotonic() - 5.0
    coord.inject_realtime_data(dict(mapped))

    assert coord.data is not None
    assert coord.data["gridConsumptionPower"] == pytest.approx(_JULIAN_EXPORT_KW)
    assert coord.data["feedinPower"] == 0.0
    assert coord.data["feedin"] == 100.0  # never increments — the complaint


def test_stale_rest_term_alone_never_reached_the_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard on the actual defect.

    The coordinator's REST-held term is added to ``pvPower`` *after*
    ``map_ws_to_coordinator`` has already split the grid reading into
    consumption/feed-in, so on its own it can never correct the sign —
    however fresh the REST value is.  Feeding the balance is what fixes
    issue #18.
    """
    from custom_components.foxess_control import domain_data as dd_mod
    from custom_components.foxess_control.foxess.realtime_ws import (
        map_ws_to_coordinator,
    )

    cfg = dd_mod.build_config({"additional_pv_power_variable": "meterPower2"})
    hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.foxess_control.coordinator._cfg", lambda _h: cfg
    )
    coord = _make_coordinator(hass, MagicMock())
    coord.data = {"pvPower": 0.0}
    coord._additional_pv_kw = _JULIAN_AUX_KW  # perfectly fresh REST value

    # Mapper run WITHOUT the term (the pre-fix WS path).
    mapped = map_ws_to_coordinator(_julian_ws_frame())
    coord.inject_realtime_data(dict(mapped))

    assert coord.data is not None
    # pvPower is corrected by the coordinator...
    assert coord.data["pvPower"] == pytest.approx(_JULIAN_AUX_KW)
    # ...but the grid direction was already decided, and stays wrong.
    assert coord.data["gridConsumptionPower"] == pytest.approx(_JULIAN_EXPORT_KW)
    assert coord.data["feedinPower"] == 0.0


def test_simulator_ws_frame_carries_aux(foxess_sim: SimulatorHandle) -> None:
    """The simulator models julianjwong's AC-coupled site over the real WS.

    Connects to the simulator's ``/dew/v0/wsmaitian`` endpoint, so the
    frame under test comes from the simulated inverter rather than a
    hand-written literal (C-028).
    """
    foxess_sim.set(
        fuzzing=False,
        ws_emit_aux=True,
        solar_kw=0.0,
        load_kw=_JULIAN_LOAD_KW,
        meter_power2_kw=_JULIAN_AUX_KW,
        soc=100.0,
    )

    state = foxess_sim.state()
    # The simulated site exports the AC-coupled excess, battery idle at 100%.
    assert state["grid_export_kw"] == pytest.approx(_JULIAN_EXPORT_KW)
    assert state["grid_import_kw"] == 0.0
    assert state["bat_charge_kw"] == 0.0
    assert state["bat_discharge_kw"] == 0.0

    frame = _fetch_ws_frame(foxess_sim.url)
    node = frame["result"]["node"]

    # The FoxESS PV strings read zero — the generation is all AC-coupled.
    assert float(node["solar"]["power"]["value"]) == 0.0
    assert "aux" in node, f"simulator frame has no aux node: {sorted(node)}"
    # C-004: watts, as a string.
    assert float(node["aux"]["power"]["value"]) == pytest.approx(3320.0)
    assert node["aux"]["power"]["unit"] == "W"


def test_simulator_ws_frame_maps_to_export_with_aux(
    foxess_sim: SimulatorHandle,
) -> None:
    """A simulator frame from an AC-coupled site maps to export, not import."""
    from custom_components.foxess_control.foxess.realtime_ws import (
        map_ws_to_coordinator,
    )

    foxess_sim.set(
        fuzzing=False,
        ws_emit_aux=True,
        solar_kw=0.0,
        load_kw=_JULIAN_LOAD_KW,
        meter_power2_kw=_JULIAN_AUX_KW,
        soc=100.0,
    )
    frame = _fetch_ws_frame(foxess_sim.url)

    # The WS reports whole watts as strings (C-004) and the simulator
    # truncates, so a value derived from chained float subtraction
    # (3.32 - 1.80 = 1.5199...) arrives as 1519 W.  Allow 2 W; the
    # direction of the flow is what this test is about.
    watts = 0.002

    configured = map_ws_to_coordinator(frame, additional_pv_enabled=True)
    assert configured["pvPower"] == pytest.approx(_JULIAN_AUX_KW, abs=watts)
    assert configured["feedinPower"] == pytest.approx(_JULIAN_EXPORT_KW, abs=watts)
    assert configured["gridConsumptionPower"] == 0.0

    # Same frame, feature not configured: unchanged from today's behaviour,
    # which is what makes this the reported bug.
    unconfigured = map_ws_to_coordinator(frame)
    assert unconfigured["pvPower"] == 0.0
    assert unconfigured["gridConsumptionPower"] == pytest.approx(
        _JULIAN_EXPORT_KW, abs=watts
    )
    assert unconfigured["feedinPower"] == 0.0


def test_simulator_ws_frame_omits_aux_by_default(
    foxess_sim: SimulatorHandle,
) -> None:
    """A DC-coupled plant's frame has no ``aux`` node.

    Matches the real captured frame in ``tests/test_realtime_ws.py``
    (``test_real_world_sample``), whose node keys are solar/grid/bat/
    load/device/charger/heatpump.
    """
    foxess_sim.set(fuzzing=False, solar_kw=2.0, load_kw=1.0)
    frame = _fetch_ws_frame(foxess_sim.url)
    assert "aux" not in frame["result"]["node"]


def _fetch_ws_frame(base_url: str) -> dict[str, Any]:
    """Open the simulator's wsmaitian endpoint and read one frame."""
    import asyncio

    import aiohttp

    async def _go() -> dict[str, Any]:
        ws_url = base_url.replace("http://", "ws://") + "/dew/v0/wsmaitian"
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(ws_url) as ws,
        ):
            await ws.send_str("getdata")
            msg = await asyncio.wait_for(ws.receive(), timeout=10)
            payload: dict[str, Any] = msg.json()
            return payload

    return asyncio.run(_go())
