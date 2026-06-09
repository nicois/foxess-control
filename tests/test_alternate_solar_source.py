"""Alternate solar source (AC-coupled): additive extra PV variable."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from custom_components.foxess_control.const import (
    CONF_ADDITIONAL_PV_POWER_VARIABLE,
)
from custom_components.foxess_control.coordinator import FoxESSDataCoordinator
from custom_components.foxess_control.domain_data import build_config

if TYPE_CHECKING:
    import pytest


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
