"""Tests for FoxESS Control sensor entities."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
)
from custom_components.foxess_control.sensor import (
    POLLED_SENSOR_DESCRIPTIONS,
    BatteryForecastSensor,
    ChargePowerSensor,
    ChargeRemainingSensor,
    ChargeWindowSensor,
    DebugLogSensor,
    DischargePowerSensor,
    DischargeRemainingSensor,
    DischargeWindowSensor,
    FoxESSPolledSensor,
    FoxESSWorkModeSensor,
    InfoLogSensor,
    InitDebugLogSensor,
    InverterOverrideStatusSensor,
    SmartOperationsOverviewSensor,
    _get_soc_value,
    async_setup_entry,
    setup_debug_log,
)
from smart_battery.sensor_base import format_duration
from smart_battery.taper import TaperProfile


def _make_hass(
    smart_charge_state: dict[str, Any] | None = None,
    smart_discharge_state: dict[str, Any] | None = None,
    coordinator_soc: float | None = None,
    coordinator_extra: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock hass with DOMAIN data."""
    hass = MagicMock()
    mock_coordinator = MagicMock()
    coordinator_data: dict[str, Any] | None = None
    if coordinator_soc is not None or coordinator_extra:
        coordinator_data = {}
        if coordinator_soc is not None:
            coordinator_data["SoC"] = coordinator_soc
        if coordinator_extra:
            coordinator_data.update(coordinator_extra)
    mock_coordinator.data = coordinator_data
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData(coordinator=mock_coordinator)
    if smart_charge_state is not None:
        dd.smart_charge_state = smart_charge_state
    if smart_discharge_state is not None:
        dd.smart_discharge_state = smart_discharge_state
    hass.data = {DOMAIN: dd}
    return hass


def _make_entry(entry_id: str = "entry1", web_username: str | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {"web_username": web_username} if web_username else {}
    return entry


def _charge_state(**overrides: Any) -> dict[str, Any]:
    """Build a smart charge state dict with sensible defaults."""
    state: dict[str, Any] = {
        "target_soc": 80,
        "last_power_w": 6000,
        "max_power_w": 10500,
        "start": datetime.datetime(2026, 4, 8, 2, 0, 0),
        "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
        "charging_started": True,
    }
    state.update(overrides)
    return state


def _discharge_state(**overrides: Any) -> dict[str, Any]:
    """Build a smart discharge state dict with sensible defaults."""
    state: dict[str, Any] = {
        "min_soc": 30,
        "last_power_w": 5000,
        "start": datetime.datetime(2026, 4, 8, 17, 0, 0),
        "end": datetime.datetime(2026, 4, 8, 20, 0, 0),
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# InverterOverrideStatusSensor (Android Auto)
# ---------------------------------------------------------------------------


class TestInverterOverrideStatusSensor:
    """Tests for the compact Android Auto sensor."""

    def test_idle(self) -> None:
        hass = _make_hass()
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Idle"
        assert sensor.icon == "mdi:home-battery"
        assert sensor.extra_state_attributes is None

    def test_charging(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Chg 6kW→80%"
        assert sensor.icon == "mdi:battery-charging"

    def test_charging_fractional_power(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state(last_power_w=6500))
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Chg 6.5kW→80%"

    def test_charging_low_power(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state(last_power_w=500))
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Chg 500W→80%"

    def test_deferred(self) -> None:
        hass = _make_hass(
            smart_charge_state=_charge_state(last_power_w=0, charging_started=False)
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Wait→80%"
        assert sensor.icon == "mdi:battery-clock"

    def test_deferred_past_start_shows_charging(self) -> None:
        """When listener's committed deferred-start has passed, show charging."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
                # Listener committed "start at 05:17" on its last tick;
                # at 05:50 the sensor should report charging.  See
                # smart_battery/sensor_base.py::is_effectively_charging.
                deferred_start_committed=datetime.datetime(2026, 4, 8, 5, 17, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        # At 05:50, committed deferred-start (05:17) has passed
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == "Chg 10.5kW→80%"
            assert sensor.icon == "mdi:battery-charging"

    def test_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Dchg 5kW→20:00"
        assert sensor.icon == "mdi:battery-arrow-down"

    def test_discharging_with_feedin_limit(self) -> None:
        hass = _make_hass(
            smart_discharge_state=_discharge_state(feedin_energy_limit_kwh=5.0)
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Dchg 5kW 5.0kWh"

    def test_charge_priority(self) -> None:
        """If both states exist, charge takes priority."""
        hass = _make_hass(
            smart_charge_state=_charge_state(),
            smart_discharge_state=_discharge_state(),
        )
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Chg 6kW→80%"

    def test_unique_id(self) -> None:
        sensor = InverterOverrideStatusSensor(_make_hass(), _make_entry("abc"))
        assert sensor.unique_id == "abc_override_status"

    def test_translation_key(self) -> None:
        sensor = InverterOverrideStatusSensor(_make_hass(), _make_entry())
        assert sensor.translation_key == "override_status"

    def test_attributes_charging(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["mode"] == "smart_charge"
        assert attrs["phase"] == "charging"
        assert attrs["power_w"] == 6000
        assert attrs["max_power_w"] == 10500
        assert attrs["target_soc"] == 80
        assert attrs["end_time"] == "2026-04-08T06:00:00"

    def test_attributes_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["mode"] == "smart_discharge"
        assert attrs["power_w"] == 5000
        assert attrs["min_soc"] == 30
        assert attrs["end_time"] == "2026-04-08T20:00:00"

    def test_domain_data_missing(self) -> None:
        hass = MagicMock()
        hass.data = {}
        sensor = InverterOverrideStatusSensor(hass, _make_entry())
        assert sensor.native_value == "Idle"
        assert sensor.icon == "mdi:home-battery"


# ---------------------------------------------------------------------------
# SmartOperationsOverviewSensor (Dashboard)
# ---------------------------------------------------------------------------


class TestSmartOperationsOverviewSensor:
    """Tests for the dashboard overview sensor."""

    def test_idle(self) -> None:
        hass = _make_hass()
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "idle"
        assert sensor.icon == "mdi:home-battery"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is False
        assert attrs["discharge_active"] is False

    def test_charging(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "charging"
        assert sensor.icon == "mdi:battery-charging"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is True
        assert attrs["charge_phase"] == "charging"
        assert attrs["charge_power_w"] == 6000
        assert attrs["charge_window"] == "02:00 – 06:00"

    def test_deferred(self) -> None:
        hass = _make_hass(
            smart_charge_state=_charge_state(last_power_w=0, charging_started=False)
        )
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "deferred"
        assert sensor.icon == "mdi:battery-clock"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_phase"] == "deferred"
        assert attrs["charge_power_w"] == 0

    def test_deferred_past_start_shows_charging(self) -> None:
        """When listener's committed deferred-start has passed, show charging."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
                deferred_start_committed=datetime.datetime(2026, 4, 8, 5, 17, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == "charging"
            assert sensor.icon == "mdi:battery-charging"
            attrs = sensor.extra_state_attributes
            assert attrs["charge_phase"] == "charging"
            assert attrs["charge_power_w"] == 10500

    def test_charge_phase_scheduled_before_window(self) -> None:
        """Before the window opens (now < start), charge_phase = 'scheduled'.

        The discharge side exposes three phases (scheduled/deferred/
        discharging/suspended); the charge side must do the same so the
        dashboard card can distinguish "not yet started" from "window open
        but pacing algorithm has deferred forced charging to later" —
        these two states look identical today ("Charge Scheduled") which
        violates C-020 (user must determine system state from UI alone).
        """
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
                start=datetime.datetime(2026, 4, 8, 11, 0, 0),
                end=datetime.datetime(2026, 4, 8, 14, 0, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        # At 10:00 — BEFORE the window opens at 11:00
        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 10, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs["charge_phase"] == "scheduled", (
                f"Expected 'scheduled' before window start, got "
                f"{attrs['charge_phase']!r}"
            )

    def test_charge_phase_deferred_within_window(self) -> None:
        """Within the window but not actively charging: charge_phase='deferred'.

        This is the user-reported bug scenario: window opened at 11:00, now
        is 12:15, but the pacing algorithm has pushed forced charging to
        later in the window.  The card must distinguish this from the
        "scheduled" (pre-window) state.
        """
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
                start=datetime.datetime(2026, 4, 8, 11, 0, 0),
                end=datetime.datetime(2026, 4, 8, 14, 0, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        # At 12:15 — window is open (start=11:00) but only 2% SoC headroom
        # needs ~15min of charging to fill, so full algorithm will defer
        # near 13:45; at 12:15 we are well within the deferred window.
        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 12, 15, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs["charge_phase"] == "deferred", (
                f"Expected 'deferred' within open window, got {attrs['charge_phase']!r}"
            )

    def test_charge_time_slack_s_during_deferred(self) -> None:
        """While deferred, charge_time_slack_s exposes (deferred_start - now) secs.

        The listener recomputes deferred_start every tick from the current
        consumption, taper profile and BMS temperature; this attribute
        surfaces the algorithm's internal countdown so users can see that
        the session defers until slack reaches zero.  Solar surplus grows
        the slack; a load spike shrinks it.
        """
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
                start=datetime.datetime(2026, 4, 8, 11, 0, 0),
                end=datetime.datetime(2026, 4, 8, 14, 0, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 12, 15, 0),
        ):
            attrs = sensor.extra_state_attributes
            # Deferred within window → slack must be a non-negative integer.
            slack = attrs.get("charge_time_slack_s")
            assert isinstance(slack, int), (
                f"Expected int seconds, got {type(slack).__name__}: {slack!r}"
            )
            assert slack > 0, f"Expected positive slack while deferred, got {slack}"

    def test_charge_time_slack_s_absent_when_charging(self) -> None:
        """While actively charging, charge_time_slack_s is None (not shown)."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=8000,
                charging_started=True,
                max_power_w=10500,
            )
        )
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 11, 30, 0),
        ):
            attrs = sensor.extra_state_attributes
        assert (
            "charge_time_slack_s" not in attrs or attrs["charge_time_slack_s"] is None
        )

    def test_discharge_time_slack_s_during_deferred(self) -> None:
        """Discharge side exposes the same attribute when deferred."""
        hass = _make_hass(
            smart_discharge_state=_discharge_state(
                last_power_w=0,
                discharging_started=False,
                max_power_w=5000,
                min_soc=30,
                start=datetime.datetime(2026, 4, 8, 17, 0, 0),
                end=datetime.datetime(2026, 4, 8, 20, 0, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 90.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            attrs = sensor.extra_state_attributes
            slack = attrs.get("discharge_time_slack_s")
            assert isinstance(slack, int), (
                f"Expected int seconds, got {type(slack).__name__}: {slack!r}"
            )
            assert slack > 0, f"Expected positive slack while deferred, got {slack}"

    def test_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            assert sensor.native_value == "discharging"
            assert sensor.icon == "mdi:battery-arrow-down"
            attrs = sensor.extra_state_attributes
            assert attrs["discharge_active"] is True
            assert attrs["discharge_power_w"] == 5000
            assert attrs["discharge_min_soc"] == 30
            assert attrs["discharge_window"] == "17:00 – 20:00"

    def test_discharging_with_feedin_limit(self) -> None:
        hass = _make_hass(
            smart_discharge_state=_discharge_state(feedin_energy_limit_kwh=5.0)
        )
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "discharging"

    def test_both_active(self) -> None:
        hass = _make_hass(
            smart_charge_state=_charge_state(),
            smart_discharge_state=_discharge_state(),
        )
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "charge_discharge_active"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is True
        assert attrs["discharge_active"] is True

    def test_remaining_time(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 4, 30, 0),
        ):
            assert sensor.extra_state_attributes["charge_remaining"] == "1h 30m"

    def test_remaining_time_minutes_only(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 45, 0),
        ):
            assert sensor.extra_state_attributes["charge_remaining"] == "15m"

    def test_current_soc_from_coordinator(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state(), coordinator_soc=55.5)
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.extra_state_attributes["charge_current_soc"] == 55.5

    def test_current_soc_none_when_unavailable(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.extra_state_attributes["charge_current_soc"] is None

    def test_unique_id(self) -> None:
        sensor = SmartOperationsOverviewSensor(_make_hass(), _make_entry("abc"))
        assert sensor.unique_id == "abc_smart_operations"

    def test_domain_data_missing(self) -> None:
        hass = MagicMock()
        hass.data = {}
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        assert sensor.native_value == "idle"
        attrs = sensor.extra_state_attributes
        assert attrs["charge_active"] is False
        assert attrs["discharge_active"] is False


# ---------------------------------------------------------------------------
# Regression: every native_value must be in _attr_options
#
# HA's SensorEntity.state performs an `value in options` check when
# device_class == ENUM and raises ValueError otherwise. That ValueError
# propagates up through async_write_ha_state → async_update_listeners and
# skips every listener registered after SmartOperationsSensor, freezing the
# entire coordinator-driven sensor set.
#
# Traces: C-020 (UI shows truth), C-026 (errors must surface), C-038
# (sensor formulas match listener formulas). See 2026-04-25 incident.
# ---------------------------------------------------------------------------


def _native_value_scenarios() -> list[
    tuple[str, dict[str, Any] | None, dict[str, Any] | None, datetime.datetime]
]:
    """Enumerate every (expected_value, charge_state, discharge_state, now).

    The list must cover every string literal that
    ``SmartOperationsSensor.native_value`` can return. Adding a new branch
    to ``native_value`` without adding a case here (and a matching entry in
    ``_attr_options``) is the class of bug this test exists to catch.
    """
    # Reference wall-clock for all scenarios
    t_before = datetime.datetime(2026, 4, 8, 1, 30, 0)  # before charge start
    t_charge = datetime.datetime(2026, 4, 8, 4, 0, 0)  # during charge window
    t_before_disc = datetime.datetime(2026, 4, 8, 16, 30, 0)  # before discharge
    t_disc = datetime.datetime(2026, 4, 8, 18, 0, 0)  # during discharge

    charge = _charge_state  # local alias for readability
    discharge = _discharge_state

    return [
        # idle: no charge, no discharge, no error
        ("idle", None, None, t_charge),
        # scheduled: charge window not yet open
        ("scheduled", charge(), None, t_before),
        # charging: active, making progress
        ("charging", charge(), None, t_charge),
        # deferred: window open but pacing has deferred forced charging
        (
            "deferred",
            charge(last_power_w=0, charging_started=False),
            None,
            t_charge,
        ),
        # target_reached
        ("target_reached", charge(target_reached=True), None, t_charge),
        # charge_discharge_active: both sessions live
        ("charge_discharge_active", charge(), discharge(), t_charge),
        # discharge_scheduled: discharge window not yet open
        ("discharge_scheduled", None, discharge(), t_before_disc),
        # discharge_deferred: window open, not yet discharging
        (
            "discharge_deferred",
            None,
            discharge(discharging_started=False),
            t_disc,
        ),
        # discharge_suspended
        (
            "discharge_suspended",
            None,
            discharge(discharging_started=True, suspended=True),
            t_disc,
        ),
        # discharging
        ("discharging", None, discharge(discharging_started=True), t_disc),
    ]


class TestSmartOperationsSensorOptionsCoverage:
    """Every value native_value returns MUST be in _attr_options.

    Without this, HA's SensorEntity.state raises ValueError during
    ``async_write_ha_state``; that exception is uncaught inside
    ``DataUpdateCoordinator.async_update_listeners`` and skips every
    remaining listener — freezing every FoxESS sensor for the duration
    of the session. See production incident 2026-04-25 10:00 AEST.
    """

    @pytest.mark.parametrize(
        "expected,charge,discharge,now",
        _native_value_scenarios(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_native_value_is_in_options(
        self,
        expected: str,
        charge: dict[str, Any] | None,
        discharge: dict[str, Any] | None,
        now: datetime.datetime,
    ) -> None:
        """native_value(scenario) must be a member of _attr_options.

        This is the neighbourhood test: one parametrised case per string
        native_value can return. The 'scheduled' case is the immediate
        reproducer for the 2026-04-25 incident; the others guard against
        future additions to native_value reintroducing the same bug class.
        """
        hass = _make_hass(
            smart_charge_state=charge,
            smart_discharge_state=discharge,
        )
        # Populate the coordinator with a plausible SoC so the deferred
        # branches evaluate without raising.
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=MagicMock(data={"SoC": 50.0})
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=now,
        ):
            value = sensor.native_value

        assert value == expected, (
            f"Scenario expected native_value={expected!r}, got {value!r}"
        )
        assert sensor._attr_options is not None
        assert value in sensor._attr_options, (
            f"native_value returned {value!r} but it is missing from "
            f"_attr_options={sensor._attr_options!r}; HA will raise ValueError "
            f"on async_write_ha_state and freeze all coordinator listeners."
        )

    def test_scheduled_before_window_is_a_valid_option(self) -> None:
        """Primary reproducer for 2026-04-25 incident.

        A smart_charge session whose start is in the future (pre-window)
        must return ``"scheduled"`` as native_value AND that value must
        be present in the sensor's ``_attr_options`` list — otherwise HA
        refuses the state and aborts the listener fan-out, freezing every
        FoxESS sensor until the session ends.
        """
        # Charge window opens 11:00; it is 10:00 — i.e. pre-window.
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                start=datetime.datetime(2026, 4, 8, 11, 0, 0),
                end=datetime.datetime(2026, 4, 8, 14, 0, 0),
            )
        )
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=MagicMock(data={"SoC": 50.0})
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 10, 0, 0),
        ):
            value = sensor.native_value

        # Production contract 1: native_value returns "scheduled"
        assert value == "scheduled", (
            f"Pre-window charge session should report 'scheduled', got {value!r}"
        )
        # Production contract 2 (the bug): "scheduled" must be in options.
        assert sensor._attr_options is not None
        assert "scheduled" in sensor._attr_options, (
            f"_attr_options={sensor._attr_options!r} is missing 'scheduled'; "
            f"HA's SensorEntity.state will raise ValueError and freeze every "
            f"listener registered after this sensor."
        )

    def test_ha_sensor_state_accepts_scheduled(self) -> None:
        """Belt-and-braces: simulate HA's own SensorEntity.state validation.

        When ``device_class == ENUM``, ``SensorEntity.state`` raises
        ``ValueError`` if ``native_value`` is not in ``self.options``. The
        production stack trace shows exactly this ValueError escaping
        async_update_listeners. This test exercises the same code path.
        """
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                start=datetime.datetime(2026, 4, 8, 11, 0, 0),
                end=datetime.datetime(2026, 4, 8, 14, 0, 0),
            )
        )
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=MagicMock(data={"SoC": 50.0})
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        sensor.entity_id = "sensor.foxess_smart_operations"
        # HA's SensorEntity.state reads unit_of_measurement; the default
        # implementation dereferences platform_data which is only set once
        # the entity is registered to a platform. We stub the translation
        # key to None so the check short-circuits, isolating the test to
        # the enum-validation code path we care about.
        sensor._attr_translation_key = None  # type: ignore[assignment]

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 10, 0, 0),
        ):
            # sensor.state is the property HA calls from async_write_ha_state.
            # It will raise ValueError if native_value is not in options —
            # which IS the production bug we are guarding against.
            try:
                state_value = sensor.state
            except ValueError as exc:
                if "not in the list of options" in str(exc):
                    pytest.fail(
                        f"HA SensorEntity.state raised ValueError — this is "
                        f"the production bug that freezes all sensors during "
                        f"a pre-window charge session: {exc}"
                    )
                raise  # any other ValueError is a test setup issue

        assert state_value == "scheduled"


# ---------------------------------------------------------------------------
# Individual dashboard sensors
# ---------------------------------------------------------------------------


class TestChargePowerSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state(last_power_w=4500))
        sensor = ChargePowerSensor(hass, _make_entry())
        assert sensor.native_value == 4500

    def test_none_when_idle(self) -> None:
        sensor = ChargePowerSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None

    def test_attributes(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = ChargePowerSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["target_soc"] == 80
        assert attrs["max_power_w"] == 10500
        assert attrs["phase"] == "charging"

    def test_transition_shows_max_power(self) -> None:
        """Before callback fires, show max_power_w not 0.

        Represents the tick between the listener committing "it's time"
        (deferred-start in the past) and the listener actually writing the
        new power to hardware.  The sensor should display the upcoming
        max_power_w, not the stale 0 from the deferred phase.
        """
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
                deferred_start_committed=datetime.datetime(2026, 4, 8, 5, 17, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == 10500

    def test_unit(self) -> None:
        sensor = ChargePowerSensor(_make_hass(), _make_entry())
        assert sensor.native_unit_of_measurement == "W"


class TestChargeWindowSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = ChargeWindowSensor(hass, _make_entry())
        assert sensor.native_value == "02:00 – 06:00"

    def test_none_when_idle(self) -> None:
        sensor = ChargeWindowSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


class TestChargeRemainingSensor:
    def test_value_when_active_no_capacity(self) -> None:
        """Without battery capacity, falls back to window remaining."""
        hass = _make_hass(smart_charge_state=_charge_state())
        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 0, 0),
        ):
            assert sensor.native_value == "1h 0m"

    def test_uses_window_remaining_not_soc_estimate(self) -> None:
        """Charge remaining uses window end, not power-based SoC estimate."""
        # Window remaining at 02:30 with end 06:00 = 3h30m
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=5000, target_soc=80, charging_started=True
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 30, 0),
        ):
            assert sensor.native_value == "3h 30m"

    def test_deferred_shows_starts_in(self) -> None:
        """When deferred, show time until charging begins."""
        # 10kWh battery, SoC=20%, target=80%, max_power=10500W
        # Energy = 60% * 10kWh = 6kWh
        # Charge at 80% of 10500W = 8400W = 8.4kW
        # Hours = 6 / 8.4 = 0.714h ≈ 42.9min
        # Deferred start = 06:00 - 42.9min ≈ 05:17
        # At 02:00, wait = ~3h 17m
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 0, 0),
        ):
            result = sensor.native_value
            assert result is not None
            assert result.startswith("starts in 3h")

    def test_deferred_about_to_start(self) -> None:
        """When deferred start time has passed, show window remaining."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        # At 05:50, deferred start (~05:17) has passed but callback
        # hasn't fired yet — show window remaining instead of "starting"
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 5, 50, 0),
        ):
            assert sensor.native_value == "10m"

    def test_deferred_never_shows_zero_minute_wait(self) -> None:
        """When the computed deferred start is within the current minute,
        charge_remaining must NOT display 'starts in 0m'.

        Reproduces the live incident: the listener recomputes deferred
        start every tick, and at the instant before the transition it
        returns a value 0-59 seconds in the future.  ``format_duration``
        rounds sub-minute durations down to ``0m``, so the sensor
        displays ``'starts in 0m'`` which is misleading — the user
        interprets it as "scheduled to start" when the window has
        actually been open for over an hour.  The reasonable display at
        this sub-minute boundary is the window-remaining time (same as
        when deferred start has just passed, ``test_deferred_about_to_start``).
        """
        # Build the state and pick a "now" such that the full algorithm
        # returns a deferred start 30 seconds in the future — i.e. within
        # the current minute.  With a 10kWh battery, SoC=20, target=80,
        # max=10500W, window 02:00-06:00 and taper absent:
        #   energy = 60% * 10kWh = 6kWh
        #   headroom 10% → effective = 10500 * 0.9 = 9.45kW
        #   charge_hours = 6/9.45 = 0.635h ≈ 38m06s
        #   buffered = 0.635/0.9 = 0.706h ≈ 42m22s
        #   deferred_start = 06:00 - 42m22s ≈ 05:17:38
        # So patching now to 05:17:08 puts the computed deferred_start
        # 30s in the future: wait = 0m30s → format_duration returns "0m"
        # → current code shows the misleading "starts in 0m".
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        # Compute the actual deferred start from the full algorithm and
        # pick a "now" 30 seconds before it.  This makes the test robust
        # to future tweaks of the algorithm's exact timing.
        from smart_battery.algorithms import calculate_deferred_start

        cs = hass.data[DOMAIN].smart_charge_state
        deferred = calculate_deferred_start(
            20.0,
            80,
            10.0,
            10500,
            cs["end"],
            start=cs["start"],
            headroom=0.10,
        )
        now = deferred - datetime.timedelta(seconds=30)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=now,
        ):
            result = sensor.native_value
            assert result is not None
            assert result != "starts in 0m", (
                f"'starts in 0m' is a nonsense display — the transition is "
                f"imminent.  Sensor returned {result!r}; should either show "
                f"window-remaining time or 'starting' but not a zero wait."
            )

    def test_deferred_clamps_to_window_start(self) -> None:
        """Deferred start never shows a time before the window opens."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=100,
                # Window 11:00-12:00 — energy calc needs >1h so deferred_start
                # would be before 11:00 without clamping
                start=datetime.datetime(2026, 4, 8, 11, 0, 0),
                end=datetime.datetime(2026, 4, 8, 12, 0, 0),
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 10.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = ChargeRemainingSensor(hass, _make_entry())
        # At 08:00, unclamped deferred start would be before 11:00.
        # Clamped to 11:00, so "starts in 3h 0m".
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 8, 0, 0),
        ):
            result = sensor.native_value
            assert result == "starts in 3h 0m"

    def test_none_when_idle(self) -> None:
        sensor = ChargeRemainingSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


class TestDischargePowerSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            assert sensor.native_value == 5000

    def test_before_start_shows_zero(self) -> None:
        """Before the discharge window opens, power should be 0."""
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 15, 0, 0),
        ):
            assert sensor.native_value == 0

    def test_none_when_idle(self) -> None:
        sensor = DischargePowerSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None

    def test_attributes(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargePowerSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["min_soc"] == 30

    def test_unit(self) -> None:
        sensor = DischargePowerSensor(_make_hass(), _make_entry())
        assert sensor.native_unit_of_measurement == "W"

    def test_bat_discharge_zero_returns_zero(self) -> None:
        """When batDischargePower is 0.0 (solar > load, battery not discharging),
        the sensor should report 0, not the requested/target power.

        Regression: polled_kw == 0.0 was treated the same as None (no data),
        falling back to requested_w and creating false 4075W spikes.
        """
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=4075),
            coordinator_extra={"batDischargePower": 0.0},
        )
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            assert sensor.native_value == 0

    def test_bat_discharge_none_falls_back_to_requested(self) -> None:
        """When batDischargePower is None (no data available),
        the sensor should fall back to the requested power value.
        """
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=4075),
        )
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            assert sensor.native_value == 4075

    def test_bat_discharge_positive_returns_observed(self) -> None:
        """When batDischargePower is positive (battery actively discharging),
        the sensor should return the observed value in watts.
        """
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=4075),
            coordinator_extra={"batDischargePower": 0.5},
        )
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            # 0.5 kW = 500 W, min(500, 4075) = 500
            assert sensor.native_value == 500

    def test_bat_discharge_capped_at_requested(self) -> None:
        """When observed discharge exceeds requested, cap at requested."""
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=3000),
            coordinator_extra={"batDischargePower": 5.0},
        )
        sensor = DischargePowerSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            # 5.0 kW = 5000 W, min(5000, 3000) = 3000
            assert sensor.native_value == 3000


class TestDischargeWindowSensor:
    def test_value_when_active(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargeWindowSensor(hass, _make_entry())
        assert sensor.native_value == "17:00 – 20:00"

    def test_none_when_idle(self) -> None:
        sensor = DischargeWindowSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None


class TestDischargeRemainingSensor:
    def test_value_when_active_no_capacity(self) -> None:
        """Without battery capacity, falls back to window remaining."""
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 19, 15, 0),
        ):
            assert sensor.native_value == "45m"

    def test_uses_window_remaining_not_soc_estimate(self) -> None:
        """Discharge remaining uses window end, not power-based SoC estimate."""
        # Window remaining at 17:30 with end 20:00 = 2h30m
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=5000, min_soc=30)
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 40.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            assert sensor.native_value == "2h 30m"

    def test_before_start_shows_starts_in(self) -> None:
        """Before discharge window opens, show time until start."""
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=5000, min_soc=30)
        )
        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 14, 15, 0),
        ):
            assert sensor.native_value == "starts in 2h 45m"

    def test_window_remaining(self) -> None:
        """During discharge, show window remaining."""
        # Window remaining at 19:30 with end 20:00 = 30m
        # Window (30m) is shorter
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=1000, min_soc=30)
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 80.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 19, 30, 0),
        ):
            assert sensor.native_value == "30m"

    def test_energy_limit_closer_shows_kwh(self) -> None:
        """When energy limit is closer to being hit, show kWh remaining."""
        # 50% of time elapsed (18:30 of 17:00-20:00), 80% of energy used
        hass = _make_hass(
            smart_discharge_state=_discharge_state(
                last_power_w=5000,
                min_soc=10,
                feedin_energy_limit_kwh=5.0,
                feedin_start_kwh=100.0,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 50.0, "feedin": 104.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 30, 0),
        ):
            assert sensor.native_value == "1.0 kWh left"

    def test_time_closer_shows_duration(self) -> None:
        """When time window is closer to ending, show time remaining."""
        # 80% of time elapsed (19:24 of 17:00-20:00), 20% of energy used
        hass = _make_hass(
            smart_discharge_state=_discharge_state(
                last_power_w=5000,
                min_soc=10,
                feedin_energy_limit_kwh=5.0,
                feedin_start_kwh=100.0,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 50.0, "feedin": 101.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 19, 24, 0),
        ):
            assert sensor.native_value == "36m"

    def test_none_when_idle(self) -> None:
        sensor = DischargeRemainingSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None

    def test_deferred_countdown_accounts_for_consumption(self) -> None:
        """Deferred countdown must use consumption, not assume zero house load.

        With a grid export limit of 5kW, a 6kW inverter, peak consumption
        of 4kW, and a 2kWh feedin target:
        - Correct (with consumption): effective_export = min(6-4, 5) = 2kW
          feedin_hours = 2/2 = 1h, buffered = 1/0.8 = 1.25h
          → deferred = end - 1.25h = start of window (clamped)
        - Bug (without consumption): effective_export = min(6, 5) = 5kW
          feedin_hours = 2/5 = 0.4h, buffered = 0.4/0.8 = 0.5h
          → deferred = end - 30min (much later than correct)

        The sensor was omitting net_consumption_kw and consumption_peak_kw,
        causing a different deferred start than the listener computes.
        """
        from smart_battery.algorithms import calculate_discharge_deferred_start

        start = datetime.datetime(2026, 4, 23, 16, 0, 0)
        end = datetime.datetime(2026, 4, 23, 17, 0, 0)
        now = datetime.datetime(2026, 4, 23, 16, 5, 0)  # 5 min into window

        # Discharge state with deferred start, feedin limit, and tracked peak
        ds = _discharge_state(
            start=start,
            end=end,
            min_soc=10,
            max_power_w=6000,
            last_power_w=0,
            discharging_started=False,
            feedin_energy_limit_kwh=2.0,
            consumption_peak_kw=4.0,
            battery_capacity_kwh=10.0,
        )

        # Coordinator data: loads=4kW, pv=0 → net_consumption=4kW
        hass = _make_hass(
            smart_discharge_state=ds,
            coordinator_soc=80.0,
            coordinator_extra={"loadsPower": 4.0, "pvPower": 0.0},
        )
        mock_entry = MagicMock()
        mock_entry.options = {
            "battery_capacity_kwh": 10.0,
            "grid_export_limit": 5000,
            "charge_headroom": 10,
        }
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        # Compute the correct deferred start (as the listener would)
        correct_deferred = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            6000,
            end,
            net_consumption_kw=4.0,
            start=start,
            headroom=0.10,
            feedin_energy_limit_kwh=2.0,
            consumption_peak_kw=4.0,
            grid_export_limit_w=5000,
        )

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=now,
        ):
            value = sensor.native_value

        # The correct deferred start is at or before window start
        # (clamped), so the sensor should NOT show a deferral countdown.
        # With the bug (no consumption), the sensor shows "defers 25m"
        # because it computes a later deferred start.
        assert value is not None
        if correct_deferred <= now:
            # Listener would start discharging — sensor should show
            # remaining window time, NOT a deferral countdown
            assert "defers" not in value, (
                f"Sensor shows '{value}' but listener would start "
                f"immediately (deferred={correct_deferred}, now={now})"
            )
        else:
            # Listener would defer — sensor should match
            wait = (correct_deferred - now).total_seconds() / 60
            correct_wait_min = int(wait)
            assert value == f"defers {correct_wait_min}m", (
                f"Sensor shows '{value}' but expected "
                f"'defers {correct_wait_min}m' deferral"
            )

    def test_deferred_countdown_with_grid_export_limit_and_consumption(self) -> None:
        """Grid export limit + consumption must both affect deferral calc.

        With 5kW grid limit, 6kW inverter, 3kW peak consumption, and
        2kWh feedin target:
        - Correct: effective_export = min(6-3, 5) = 3kW
          feedin_hours = 2/3 = 0.667h, buffered = 0.667/0.8 = 0.833h = 50min
          → deferred = end - 50min = 16:10 (in a 60-min window)
        - Bug (no consumption): effective_export = min(6, 5) = 5kW
          feedin_hours = 2/5 = 0.4h, buffered = 0.4/0.8 = 0.5h = 30min
          → deferred = end - 30min = 16:30

        At 16:05, correct shows "defers 5m", bug shows "defers 25m".
        """
        from smart_battery.algorithms import calculate_discharge_deferred_start

        start = datetime.datetime(2026, 4, 23, 16, 0, 0)
        end = datetime.datetime(2026, 4, 23, 17, 0, 0)
        now = datetime.datetime(2026, 4, 23, 16, 5, 0)

        ds = _discharge_state(
            start=start,
            end=end,
            min_soc=10,
            max_power_w=6000,
            last_power_w=0,
            discharging_started=False,
            feedin_energy_limit_kwh=2.0,
            consumption_peak_kw=3.0,
            battery_capacity_kwh=10.0,
        )

        hass = _make_hass(
            smart_discharge_state=ds,
            coordinator_soc=80.0,
            coordinator_extra={"loadsPower": 2.0, "pvPower": 0.0},
        )
        mock_entry = MagicMock()
        mock_entry.options = {
            "battery_capacity_kwh": 10.0,
            "grid_export_limit": 5000,
            "charge_headroom": 10,
        }
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        # Correct deferred start with all parameters (as listener would)
        correct_deferred = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            6000,
            end,
            net_consumption_kw=2.0,
            start=start,
            headroom=0.10,
            feedin_energy_limit_kwh=2.0,
            consumption_peak_kw=3.0,
            grid_export_limit_w=5000,
        )

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=now,
        ):
            value = sensor.native_value

        # Sensor must show the same countdown as the listener's calculation
        assert correct_deferred > now, (
            "Setup error: correct deferred should be after now"
        )
        correct_wait_min = int((correct_deferred - now).total_seconds() / 60)
        assert value == f"defers {correct_wait_min}m", (
            f"Sensor shows '{value}' but expected 'defers {correct_wait_min}m' "
            f"(correct deferred={correct_deferred})"
        )

    def test_deferred_sensor_matches_listener_calculation(self) -> None:
        """The sensor's deferred countdown must match the listener's calc exactly.

        This is the core bug: the sensor was calling calculate_discharge_deferred_start
        without net_consumption_kw, consumption_peak_kw, taper_profile, bms_temp_c,
        or start — producing a different result than the listener.
        """
        from smart_battery.algorithms import calculate_discharge_deferred_start

        start = datetime.datetime(2026, 4, 23, 16, 0, 0)
        end = datetime.datetime(2026, 4, 23, 16, 44, 0)  # 44-min window
        now = datetime.datetime(2026, 4, 23, 16, 0, 0)  # at window start

        # Moderate consumption that changes the effective export rate
        ds = _discharge_state(
            start=start,
            end=end,
            min_soc=10,
            max_power_w=7000,
            last_power_w=0,
            discharging_started=False,
            feedin_energy_limit_kwh=2.0,
            consumption_peak_kw=3.0,
            battery_capacity_kwh=10.0,
        )

        hass = _make_hass(
            smart_discharge_state=ds,
            coordinator_soc=80.0,
            coordinator_extra={"loadsPower": 2.0, "pvPower": 0.0},
        )
        mock_entry = MagicMock()
        mock_entry.options = {
            "battery_capacity_kwh": 10.0,
            "grid_export_limit": 5000,
            "charge_headroom": 10,
        }
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        # Listener's calculation (correct — all params)
        listener_deferred = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            7000,
            end,
            net_consumption_kw=2.0,
            start=start,
            headroom=0.10,
            feedin_energy_limit_kwh=2.0,
            consumption_peak_kw=3.0,
            grid_export_limit_w=5000,
        )

        # Sensor's calculation (broken — missing params)
        sensor_buggy = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            7000,
            end,
            headroom=0.10,
            feedin_energy_limit_kwh=2.0,
            grid_export_limit_w=5000,
        )

        # Verify the two calculations differ (the bug exists)
        assert listener_deferred != sensor_buggy, (
            "Bug doesn't reproduce: listener and sensor compute the same result. "
            "Adjust test parameters so consumption changes the deadline."
        )

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=now,
        ):
            value = sensor.native_value

        # The sensor must produce the same countdown as the listener
        assert value is not None
        if listener_deferred > now:
            wait = (listener_deferred - now).total_seconds() / 60
            expected_min = int(wait)
            assert value == f"defers {expected_min}m", (
                f"Sensor shows '{value}' but expected "
                f"'defers {expected_min}m' matching listener "
                f"(deferred={listener_deferred})"
            )
        else:
            # Listener says start now — no deferral
            assert "defers" not in value, (
                f"Sensor shows '{value}' but listener would start immediately"
            )

    def test_deferred_countdown_uses_peak_consumption_not_just_instant(
        self,
    ) -> None:
        """Peak consumption (not just the instantaneous sample) must flow
        through to the sensor's deferred calculation.

        Covers the corner of the C-038 parameter-parity bug that the
        other two tests only exercise incidentally: when peak and instant
        consumption differ, the sensor must use peak (as the listener
        does), because the listener pessimistically sizes around the
        tracked peak to avoid grid import during spikes between polls.

        With instant load = 1 kW but tracked peak = 5 kW, an effective
        export rate computed off peak yields a substantially earlier
        deferred start than one computed off the instantaneous value.
        Before the fix, the sensor passed neither, so the deferred
        countdown drifted away from what the listener actually did.
        """
        from smart_battery.algorithms import calculate_discharge_deferred_start

        start = datetime.datetime(2026, 4, 24, 16, 0, 0)
        end = datetime.datetime(2026, 4, 24, 18, 0, 0)
        now = datetime.datetime(2026, 4, 24, 16, 5, 0)

        ds = _discharge_state(
            start=start,
            end=end,
            min_soc=10,
            max_power_w=10500,
            last_power_w=0,
            discharging_started=False,
            feedin_energy_limit_kwh=3.0,
            consumption_peak_kw=5.0,  # tracked peak — much higher than instant
            battery_capacity_kwh=10.0,
        )

        # Instant net_consumption = 1 kW (low); peak = 5 kW (high).
        # The listener uses peak to protect against spikes; the sensor
        # must do the same so its countdown matches.
        hass = _make_hass(
            smart_discharge_state=ds,
            coordinator_soc=80.0,
            coordinator_extra={"loadsPower": 1.0, "pvPower": 0.0},
        )
        mock_entry = MagicMock()
        mock_entry.options = {
            "battery_capacity_kwh": 10.0,
            "grid_export_limit": 0,
            "charge_headroom": 10,
        }
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        correct_deferred = calculate_discharge_deferred_start(
            80.0,
            10,
            10.0,
            10500,
            end,
            net_consumption_kw=1.0,
            start=start,
            headroom=0.10,
            feedin_energy_limit_kwh=3.0,
            consumption_peak_kw=5.0,
        )

        sensor = DischargeRemainingSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=now,
        ):
            value = sensor.native_value

        assert correct_deferred > now
        expected = f"defers {format_duration(correct_deferred - now)}"
        assert value == expected, (
            f"Sensor shows '{value}' but listener would show "
            f"'{expected}' (deferred={correct_deferred})"
        )


# ---------------------------------------------------------------------------
# Battery Forecast sensor
# ---------------------------------------------------------------------------


class TestBatteryForecastSensor:
    def test_idle_returns_empty_forecast(self) -> None:
        sensor = BatteryForecastSensor(_make_hass(), _make_entry())
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {"forecast": []}

    def test_charging_forecast_rises_to_target(self) -> None:
        """Forecast SoC rises from current to target during charge."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=5000,
                target_soc=80,
                charging_started=True,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 40.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 4, 0, 0),
        ):
            assert sensor.native_value == 40.0
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            assert len(forecast) > 0
            # First point is current SoC
            assert forecast[0]["soc"] == 40.0
            # Should reach the target SoC by end of window
            assert forecast[-1]["soc"] == 80.0
            # Should not exceed target
            assert all(p["soc"] <= 80.0 for p in forecast)

    def test_charging_forecast_curves_with_taper(self) -> None:
        """Forecast SoC curve bends when taper shows reduced acceptance."""
        taper = TaperProfile()
        # Record enough observations to trust: full power up to 79%, then tapering
        for soc in range(40, 80):
            for _ in range(3):
                taper.record_charge(float(soc), 10000, 10000.0)  # ratio 1.0
        for soc in range(80, 96):
            for _ in range(3):
                # Simulate tapering: 50% acceptance above 80%
                taper.record_charge(float(soc), 10000, 5000.0)  # ratio 0.5

        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=10000,
                max_power_w=10000,
                target_soc=95,
                charging_started=True,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 50.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        hass.data[DOMAIN].taper_profile = taper
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 0, 0),
        ):
            forecast = sensor.extra_state_attributes["forecast"]
            assert len(forecast) > 4

            # Find the rate of SoC change per step in the low-SoC region (50-70%)
            # vs the high-SoC region (80-90%) — taper should halve the rate
            low_region = [p for p in forecast if 55 <= p["soc"] <= 70]
            high_region = [p for p in forecast if 82 <= p["soc"] <= 92]
            assert len(low_region) >= 2, f"Not enough low-region points: {low_region}"
            assert len(high_region) >= 2, (
                f"Not enough high-region points: {high_region}"
            )

            low_rate = (low_region[-1]["soc"] - low_region[0]["soc"]) / (
                low_region[-1]["time"] - low_region[0]["time"]
            )
            high_rate = (high_region[-1]["soc"] - high_region[0]["soc"]) / (
                high_region[-1]["time"] - high_region[0]["time"]
            )

            # With 50% taper above 80%, the high-region rate should be
            # roughly half the low-region rate (allow some tolerance)
            assert high_rate < low_rate * 0.75, (
                f"Forecast should show taper: low={low_rate}, high={high_rate}"
            )

    def test_discharging_forecast_drops_to_min(self) -> None:
        """Forecast SoC drops from current toward min_soc."""
        hass = _make_hass(
            smart_discharge_state=_discharge_state(last_power_w=5000, min_soc=30)
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 70.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            assert sensor.native_value == 70.0
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            assert len(forecast) > 0
            assert forecast[0]["soc"] == 70.0
            # SoC should drop over time
            assert forecast[-1]["soc"] < forecast[0]["soc"]
            # Should not go below min_soc
            assert all(p["soc"] >= 30.0 for p in forecast)

    def test_deferred_charge_shows_flat_then_rise(self) -> None:
        """Deferred charge: SoC stays flat, then rises after start."""
        hass = _make_hass(
            smart_charge_state=_charge_state(
                last_power_w=0,
                charging_started=False,
                max_power_w=10500,
                target_soc=80,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 20.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 2, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            assert len(forecast) > 2
            # Early points should be flat (deferred)
            assert forecast[0]["soc"] == 20.0
            assert forecast[1]["soc"] == 20.0
            # Should reach or approach the target (80%) by end of window
            assert forecast[-1]["soc"] >= 75.0

    def test_no_forecast_without_capacity(self) -> None:
        """Without battery capacity configured, forecast is empty."""
        hass = _make_hass(smart_charge_state=_charge_state())

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 4, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            assert attrs["forecast"] == []

    def test_discharge_forecast_defers_until_start(self) -> None:
        """Discharge forecast starts at the configured interval, not now."""
        # Discharge starts at 17:00, now is 16:00 — graph should start at 17:00
        hass = _make_hass(smart_discharge_state=_discharge_state(last_power_w=5000))
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 60.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 16, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            assert len(forecast) > 2
            # No points before the configured start time
            start_epoch = int(
                datetime.datetime(2026, 4, 8, 17, 0, 0).timestamp() * 1000
            )
            assert forecast[0]["time"] >= start_epoch
            # First point at current SoC (flat_until holds at start)
            assert forecast[0]["soc"] == 60.0
            # Points after start should decrease
            assert forecast[-1]["soc"] < 60.0

    def test_discharge_forecast_capped_by_feedin_limit(self) -> None:
        """Forecast SoC drop is capped by the feed-in energy limit."""
        # 60% SoC, 10kWh battery, 1kWh feedin limit → max 10% SoC drop
        hass = _make_hass(
            smart_discharge_state=_discharge_state(
                last_power_w=5000,
                min_soc=10,
                feedin_energy_limit_kwh=1.0,
            )
        )
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 60.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            attrs = sensor.extra_state_attributes
            forecast = attrs["forecast"]
            # 1kWh / 10kWh = 10% max drop → floor at 50%
            assert all(p["soc"] >= 50.0 for p in forecast)
            # Should still show some discharge
            assert forecast[-1]["soc"] < 60.0

    def test_forecast_points_have_time_and_soc(self) -> None:
        """Each forecast point has 'time' (epoch ms) and 'soc' keys."""
        hass = _make_hass(smart_discharge_state=_discharge_state(last_power_w=5000))
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"SoC": 60.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(
            inverter=MagicMock(), coordinator=mock_coordinator
        )
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        sensor = BatteryForecastSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
            assert attrs is not None
            forecast = attrs["forecast"]
            for point in forecast:
                assert "time" in point
                assert "soc" in point
                assert isinstance(point["time"], int)
                assert isinstance(point["soc"], int | float)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# FoxESSPolledSensor (coordinator-backed)
# ---------------------------------------------------------------------------


class TestFoxESSPolledSensor:
    """Tests for coordinator-backed polled sensors."""

    def _make_coordinator(self, data: dict[str, Any] | None = None) -> MagicMock:
        coordinator = MagicMock()
        coordinator.data = data
        return coordinator

    def test_native_value_reads_from_coordinator(self) -> None:
        coordinator = self._make_coordinator({"SoC": 75.5, "batChargePower": 1.2})
        entry = _make_entry()
        desc = POLLED_SENSOR_DESCRIPTIONS[0]  # SoC
        sensor = FoxESSPolledSensor(coordinator, entry, desc)
        assert sensor.native_value == 75.5

    def test_native_value_returns_none_when_data_is_none(self) -> None:
        coordinator = self._make_coordinator(None)
        entry = _make_entry()
        desc = POLLED_SENSOR_DESCRIPTIONS[0]
        sensor = FoxESSPolledSensor(coordinator, entry, desc)
        assert sensor.native_value is None

    def test_native_value_returns_none_when_variable_missing(self) -> None:
        coordinator = self._make_coordinator({"batChargePower": 1.2})
        entry = _make_entry()
        desc = POLLED_SENSOR_DESCRIPTIONS[0]  # SoC — not in data
        sensor = FoxESSPolledSensor(coordinator, entry, desc)
        assert sensor.native_value is None

    def test_unique_id(self) -> None:
        coordinator = self._make_coordinator({})
        entry = _make_entry("myentry")
        desc = POLLED_SENSOR_DESCRIPTIONS[0]
        sensor = FoxESSPolledSensor(coordinator, entry, desc)
        assert sensor.unique_id == "myentry_battery_soc"

    def test_data_source_exposed_when_multi_source(self) -> None:
        """Sensor exposes data_source when WS credentials configured."""
        coordinator = self._make_coordinator({"SoC": 75.0, "_data_source": "api"})
        entry = _make_entry(web_username="user@example.com")
        sensor = FoxESSPolledSensor(coordinator, entry, POLLED_SENSOR_DESCRIPTIONS[0])
        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["data_source"] == "api"

    def test_data_source_hidden_when_single_source(self) -> None:
        """No attribute when only API configured (no ambiguity)."""
        coordinator = self._make_coordinator({"SoC": 75.0, "_data_source": "api"})
        sensor = FoxESSPolledSensor(
            coordinator, _make_entry(), POLLED_SENSOR_DESCRIPTIONS[0]
        )
        assert sensor.extra_state_attributes is None

    def test_all_descriptors_create_sensors(self) -> None:
        coordinator = self._make_coordinator(
            {
                "SoC": 50,
                "batChargePower": 1.0,
                "batDischargePower": 0.5,
                "loadsPower": 3.2,
                "pvPower": 4.1,
                "ResidualEnergy": 8.0,
                "batTemperature": 25.0,
                "gridConsumptionPower": 2.1,
                "feedinPower": 0.3,
                "generationPower": 4.5,
                "batVolt": 52.1,
                "batCurrent": 23.0,
                "pv1Power": 2.5,
                "pv2Power": 2.0,
                "ambientTemperation": 18.0,
                "invTemperation": 35.0,
                "feedin": 657.1,
                "gridConsumption": 690.0,
                "generation": 1347.0,
                "chargeEnergyToTal": 1028.4,
                "dischargeEnergyToTal": 1211.4,
                "loads": 923.1,
                "energyThroughput": 2102.96,
                "meterPower": -5.014,
                "RVolt": 246.4,
                "RCurrent": 22.1,
                "RFreq": 50.02,
                "epsPower": 0.158,
                "bmsBatteryTemperature": 15.2,
            }
        )
        entry = _make_entry()
        sensors = [
            FoxESSPolledSensor(coordinator, entry, desc)
            for desc in POLLED_SENSOR_DESCRIPTIONS
        ]
        assert len(sensors) == 28
        # All should have a non-None value
        for s in sensors:
            assert s.native_value is not None


# ---------------------------------------------------------------------------
# FoxESSWorkModeSensor
# ---------------------------------------------------------------------------


class TestFoxESSWorkModeSensor:
    """Tests for the work mode sensor."""

    def _make_coordinator(self, data: dict[str, Any] | None = None) -> MagicMock:
        coordinator = MagicMock()
        coordinator.data = data
        return coordinator

    def test_returns_mode_string(self) -> None:
        coordinator = self._make_coordinator({"_work_mode": "SelfUse"})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.native_value == "SelfUse"

    def test_returns_none_when_data_is_none(self) -> None:
        coordinator = self._make_coordinator(None)
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.native_value is None

    def test_returns_none_when_mode_is_none(self) -> None:
        coordinator = self._make_coordinator({"_work_mode": None})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.native_value is None

    def test_unique_id(self) -> None:
        coordinator = self._make_coordinator({})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry("abc"))
        assert sensor.unique_id == "abc_work_mode"

    def test_translation_key(self) -> None:
        coordinator = self._make_coordinator({})
        sensor = FoxESSWorkModeSensor(coordinator, _make_entry())
        assert sensor.translation_key == "work_mode"


# ---------------------------------------------------------------------------
# _get_soc_value coordinator fallback
# ---------------------------------------------------------------------------


class TestGetSocValue:
    """Tests for _get_soc_value reading from coordinator."""

    def test_returns_coordinator_soc(self) -> None:
        hass = _make_hass()
        coordinator = MagicMock()
        coordinator.data = {"SoC": 72.0}
        hass.data[DOMAIN].entries["entry1"] = FoxESSEntryData(coordinator=coordinator)

        assert _get_soc_value(hass) == 72.0

    def test_returns_none_when_no_data(self) -> None:
        hass = _make_hass()

        assert _get_soc_value(hass) is None

    def test_returns_none_when_no_domain_data(self) -> None:
        hass = MagicMock()
        hass.data = {}

        assert _get_soc_value(hass) is None


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_all_entities(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        added: list[Any] = []

        def mock_add(entities: Any, update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, entry, mock_add)  # type: ignore[arg-type]

        # 10 base + 28 polled + 1 work mode + 1 freshness = 40
        assert len(added) == 40
        assert isinstance(added[0], InverterOverrideStatusSensor)
        assert isinstance(added[1], SmartOperationsOverviewSensor)
        assert isinstance(added[2], ChargePowerSensor)
        assert isinstance(added[3], ChargeWindowSensor)
        assert isinstance(added[4], ChargeRemainingSensor)
        assert isinstance(added[5], DischargePowerSensor)
        assert isinstance(added[6], DischargeWindowSensor)
        assert isinstance(added[7], DischargeRemainingSensor)
        assert isinstance(added[8], BatteryForecastSensor)

    @pytest.mark.asyncio
    async def test_creates_polled_sensors_when_coordinator_present(self) -> None:
        hass = _make_hass()
        entry = _make_entry()

        added: list[Any] = []

        def mock_add(entities: Any, update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, entry, mock_add)  # type: ignore[arg-type]

        assert len(added) == 40  # 10 existing + 28 polled + 1 work mode + 1 freshness
        polled = [e for e in added if isinstance(e, FoxESSPolledSensor)]
        assert len(polled) == 28
        work_mode = [e for e in added if isinstance(e, FoxESSWorkModeSensor)]
        assert len(work_mode) == 1


# ---------------------------------------------------------------------------
# Debug log capture
# ---------------------------------------------------------------------------


class TestDebugLog:
    """Tests for the opt-in debug log handler and sensor."""

    def test_setup_returns_none_when_entity_missing(self) -> None:
        hass = MagicMock()
        hass.states.get.return_value = None
        assert setup_debug_log(hass, _make_entry()) is None

    def test_setup_returns_none_when_entity_off(self) -> None:
        hass = MagicMock()
        state = MagicMock()
        state.state = "off"
        hass.states.get.return_value = state
        assert setup_debug_log(hass, _make_entry()) is None

    def test_setup_returns_sensor_and_handler_when_on(self) -> None:
        hass = MagicMock()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state
        result = setup_debug_log(hass, _make_entry())
        assert result is not None
        sensors, handlers = result
        assert any(isinstance(s, DebugLogSensor) for s in sensors)
        assert any(isinstance(s, InitDebugLogSensor) for s in sensors)
        assert len(handlers) == 3

    def test_handler_captures_log_messages(self) -> None:
        import logging

        hass = MagicMock()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state
        sensors, handlers = setup_debug_log(hass, _make_entry())  # type: ignore[misc]
        sensor = next(s for s in sensors if isinstance(s, DebugLogSensor))

        logger = logging.getLogger("custom_components.foxess_control")
        try:
            logger.info("test message %d", 42)
            assert sensor.native_value >= 1
            entries = sensor.extra_state_attributes["entries"]
            assert any("test message 42" in e["msg"] for e in entries)
            assert entries[-1]["level"] == "INFO"
        finally:
            for h in handlers:
                logger.removeHandler(h)

    def test_info_log_captures_only_info_and_above(self) -> None:
        import logging

        hass = MagicMock()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state
        sensors, handlers = setup_debug_log(hass, _make_entry())  # type: ignore[misc]
        info_sensor = next(s for s in sensors if isinstance(s, InfoLogSensor))
        debug_sensor = next(s for s in sensors if isinstance(s, DebugLogSensor))

        logger = logging.getLogger("custom_components.foxess_control")
        try:
            logger.debug("debug only")
            logger.info("info msg")
            logger.warning("warn msg")

            info_entries = info_sensor.extra_state_attributes["entries"]
            debug_entries = debug_sensor.extra_state_attributes["entries"]

            assert not any("debug only" in e["msg"] for e in info_entries)
            assert any("info msg" in e["msg"] for e in info_entries)
            assert any("warn msg" in e["msg"] for e in info_entries)
            assert any("debug only" in e["msg"] for e in debug_entries)
        finally:
            for h in handlers:
                logger.removeHandler(h)

    def test_buffer_is_bounded(self) -> None:
        import collections
        import logging

        from custom_components.foxess_control.sensor import (
            _DEBUG_LOG_BUFFER_SIZE,
            _DebugLogHandler,
        )

        buf: collections.deque[dict[str, str]] = collections.deque(
            maxlen=_DEBUG_LOG_BUFFER_SIZE
        )
        handler = _DebugLogHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test.bounded")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for i in range(_DEBUG_LOG_BUFFER_SIZE + 50):
                logger.info("msg %d", i)
            assert len(buf) == _DEBUG_LOG_BUFFER_SIZE
            # Oldest messages are evicted
            assert "msg 0" not in buf[0]["msg"]
        finally:
            logger.removeHandler(handler)

    def test_init_buffer_does_not_wrap(self) -> None:
        import logging

        from custom_components.foxess_control.sensor import (
            _DEBUG_LOG_BUFFER_SIZE,
            _InitDebugLogHandler,
        )

        buf: list[dict[str, str]] = []
        handler = _InitDebugLogHandler(buf, maxlen=_DEBUG_LOG_BUFFER_SIZE)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test.init_bounded")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for i in range(_DEBUG_LOG_BUFFER_SIZE + 50):
                logger.info("msg %d", i)
            assert len(buf) == _DEBUG_LOG_BUFFER_SIZE
            # First messages are preserved (not evicted)
            assert "msg 0" in buf[0]["msg"]
            assert "msg 1" in buf[1]["msg"]
        finally:
            logger.removeHandler(handler)

    @pytest.mark.asyncio
    async def test_async_setup_entry_adds_debug_sensor_when_enabled(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state

        added: list[Any] = []

        def mock_add(entities: Any, update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, entry, mock_add)  # type: ignore[arg-type]

        # 40 base + 3 log sensors = 43
        assert len(added) == 43
        debug_sensors = [e for e in added if isinstance(e, DebugLogSensor)]
        assert len(debug_sensors) == 1
        info_sensors = [e for e in added if isinstance(e, InfoLogSensor)]
        assert len(info_sensors) == 1
        init_sensors = [e for e in added if isinstance(e, InitDebugLogSensor)]
        assert len(init_sensors) == 1

    def test_log_sensors_have_resolvable_names(self) -> None:
        """Each log sensor must have a name resolvable in HA.

        A sensor with ``_attr_has_entity_name = True`` and no ``_attr_name``
        must have its ``_attr_translation_key`` present in
        ``translations/en.json``. If it's absent, HA falls back to the
        device name and generates ``sensor.foxess_2`` / ``sensor.foxess_3``
        instead of the expected ``sensor.foxess_info_log`` /
        ``sensor.foxess_init_debug_log``.

        Regression: beta.7 removed the explicit ``_attr_name`` override
        from InfoLogSensor and InitDebugLogSensor with the intention that
        translation-driven naming would take over, but the translation
        keys were never added to ``translations/*.json`` — only to
        ``strings.json``. On the live system this caused both sensors to
        register under auto-generated entity IDs, making them effectively
        invisible to users looking for them by their expected names.
        """
        import json
        import logging
        from pathlib import Path

        hass = MagicMock()
        state = MagicMock()
        state.state = "on"
        hass.states.get.return_value = state
        sensors, handlers = setup_debug_log(hass, _make_entry())  # type: ignore[misc]
        logger = logging.getLogger("custom_components.foxess_control")
        try:
            translations_path = (
                Path(__file__).parent.parent
                / "custom_components"
                / "foxess_control"
                / "translations"
                / "en.json"
            )
            translations = json.loads(translations_path.read_text())
            sensor_translations = translations.get("entity", {}).get("sensor", {})

            missing: list[str] = []
            for sensor in sensors:
                # Entities that set has_entity_name=True have two name paths:
                # (a) explicit _attr_name; or
                # (b) _attr_translation_key resolving in translations/en.json.
                if not getattr(sensor, "_attr_has_entity_name", False):
                    continue
                if getattr(sensor, "_attr_name", None) is not None:
                    continue
                key = getattr(sensor, "_attr_translation_key", None)
                assert key is not None, (
                    f"{type(sensor).__name__} has has_entity_name=True but no "
                    f"_attr_name or _attr_translation_key"
                )
                entry = sensor_translations.get(key)
                if not entry or "name" not in entry:
                    missing.append(f"{type(sensor).__name__}.{key}")

            assert not missing, (
                "Log sensors rely on translation-driven naming but these keys "
                "are missing from translations/en.json: "
                f"{missing}. HA will fall back to the device name and "
                "generate sensor.foxess_2/3 instead of the expected entity IDs."
            )
        finally:
            for h in handlers:
                logger.removeHandler(h)


class TestPacingTransparencyAttributes:
    """UX #4 / #6 / #8: pacing-transparency attributes.

    These three UX items share a single implementation surface —
    attributes on ``sensor.foxess_smart_operations`` that expose
    pacing-algorithm reasoning to the user without requiring log
    inspection (C-020).

    #4 `discharge_deferred_reason` / `charge_deferred_reason`:
       short human-readable sentence explaining why the session is
       still deferred instead of actively discharging / charging.

    #6 `discharge_safety_floor_w` + `discharge_peak_consumption_kw`
       + `discharge_paced_target_w`: surface the C-001 safety floor
       so users can see why paced power may exceed what energy math
       alone suggests.

    #8 `discharge_grid_export_limit_w` + `discharge_clamp_active`:
       surface the hardware export clamp separately from inverter
       output — addresses DNO-compliance anxiety on export-limited
       sites.
    """

    def test_deferred_reason_populated_when_discharge_deferred(self) -> None:
        state = _discharge_state(
            last_power_w=0,
            discharging_started=False,
            start=datetime.datetime(2026, 4, 8, 16, 59, 0),  # in the past
            feedin_energy_limit_kwh=1.0,
            consumption_peak_kw=0.2,
        )
        hass = _make_hass(smart_discharge_state=state)
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 17, 30, 0),
        ):
            attrs = sensor.extra_state_attributes
        assert attrs["discharge_phase"] == "deferred"
        assert "discharge_deferred_reason" in attrs
        reason = attrs["discharge_deferred_reason"]
        assert isinstance(reason, str) and len(reason) > 10
        # Mentions the feedin-limit motivation.
        assert "feed-in" in reason or "1 kWh" in reason or "holding" in reason

    def test_deferred_reason_absent_when_actively_discharging(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs["discharge_phase"] == "discharging"
        assert "discharge_deferred_reason" not in attrs

    def test_charge_deferred_reason_populated(self) -> None:
        state = _charge_state(last_power_w=0, charging_started=False, target_soc=90)
        hass = _make_hass(smart_charge_state=state, coordinator_soc=50.0)
        mock_entry = MagicMock()
        mock_entry.options = {"battery_capacity_kwh": 10.0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        with patch(
            "custom_components.foxess_control.sensor.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 3, 0, 0),
        ):
            attrs = sensor.extra_state_attributes
        assert attrs["charge_phase"] == "deferred"
        assert "charge_deferred_reason" in attrs
        reason = attrs["charge_deferred_reason"]
        assert "40" in reason or "gap" in reason or "SoC" in reason

    def test_safety_floor_exposed_with_peak(self) -> None:
        """discharge_safety_floor_w = peak * 1.5 * 1000."""
        state = _discharge_state(consumption_peak_kw=2.0)
        hass = _make_hass(smart_discharge_state=state)
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs["discharge_peak_consumption_kw"] == 2.0
        assert attrs["discharge_safety_floor_w"] == 3000  # 2.0 * 1.5 * 1000

    def test_safety_floor_zero_when_no_peak(self) -> None:
        state = _discharge_state(consumption_peak_kw=0.0)
        hass = _make_hass(smart_discharge_state=state)
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs["discharge_safety_floor_w"] == 0
        assert attrs["discharge_peak_consumption_kw"] == 0.0

    def test_clamp_attributes_absent_when_no_export_limit_configured(
        self,
    ) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        mock_entry = MagicMock()
        mock_entry.options = {"grid_export_limit": 0}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        # With grid_export_limit=0 the clamp attrs should be absent.
        assert "discharge_grid_export_limit_w" not in attrs
        assert "discharge_clamp_active" not in attrs

    def test_clamp_attributes_present_when_export_limit_configured(self) -> None:
        hass = _make_hass(smart_discharge_state=_discharge_state())
        mock_entry = MagicMock()
        mock_entry.options = {"grid_export_limit": 5000}
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs.get("discharge_grid_export_limit_w") == 5000
        # discharge_clamp_active should be a bool (not absent).
        assert isinstance(attrs.get("discharge_clamp_active"), bool)


class TestTaperProfileAttribute:
    """UX #5: the taper profile is exposed as a chart-friendly
    attribute on sensor.foxess_smart_operations.

    The attribute is _unrecorded (no recorder bloat) and contains
    both charge and discharge histograms — each a list of
    {soc, ratio, count} entries, sorted by SoC bin ascending.
    """

    def test_taper_profile_present_when_observations_recorded(self) -> None:
        from smart_battery.taper import TaperProfile

        profile = TaperProfile()
        profile.record_charge(soc=50.0, requested_w=5000, actual_w=5000)
        profile.record_charge(soc=90.0, requested_w=5000, actual_w=1000)
        profile.record_discharge(soc=20.0, requested_w=5000, actual_w=2500)

        hass = _make_hass()
        hass.data[DOMAIN].taper_profile = profile
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "taper_profile" in attrs
        summary = attrs["taper_profile"]
        # Both charge and discharge histograms present, even if only
        # one has observations (empty list is allowed).
        assert "charge" in summary and isinstance(summary["charge"], list)
        assert "discharge" in summary and isinstance(summary["discharge"], list)
        # Charge observations captured.
        assert len(summary["charge"]) == 2
        # Sorted by SoC ascending.
        socs = [entry["soc"] for entry in summary["charge"]]
        assert socs == sorted(socs)
        # Each entry has the expected shape.
        for entry in summary["charge"]:
            assert set(entry.keys()) == {"soc", "ratio", "count"}
            assert 0.0 <= entry["ratio"] <= 1.0
            assert entry["count"] >= 1

    def test_taper_profile_present_even_with_no_observations(self) -> None:
        from smart_battery.taper import TaperProfile

        hass = _make_hass()
        hass.data[DOMAIN].taper_profile = TaperProfile()
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        # Empty profile still surfaces (empty-list histograms), so
        # consumer charts can render "no data yet" gracefully.
        assert attrs["taper_profile"] == {"charge": [], "discharge": []}

    def test_taper_profile_absent_when_no_profile_attached(self) -> None:
        hass = _make_hass()
        # hass.data[DOMAIN].taper_profile stays None (default).
        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        attrs = sensor.extra_state_attributes
        # When no taper profile is attached to domain data, the key
        # is absent (not empty). Distinguishes "not yet initialised"
        # from "initialised but empty".
        assert "taper_profile" not in attrs
