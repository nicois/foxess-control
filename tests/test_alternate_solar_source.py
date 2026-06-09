"""Alternate solar source (AC-coupled): additive extra PV variable."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from custom_components.foxess_control import coordinator as coordinator_mod
from custom_components.foxess_control.const import (
    CONF_ADDITIONAL_PV_POWER_VARIABLE,
)
from custom_components.foxess_control.coordinator import FoxESSDataCoordinator
from custom_components.foxess_control.domain_data import build_config
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter

if TYPE_CHECKING:
    import pytest

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
