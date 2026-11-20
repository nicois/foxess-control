"""C-038: the operations sensor's charge reachability and slack must use the
listener's inputs (issue #32).

The reporter saw the card claim the session was waiting because the target
could be reached, while ``charge_target_reachable`` read false and
``charge_time_slack_s`` 0 — the sensor and the listener disagreeing.  The
sensor read battery capacity from the *options* while the listener uses the
value captured in the session, and ``_is_charge_reachable`` also left out net
consumption and the configured headroom.  Every sensor computation now passes
exactly what the listener passes.

Brand-agnostic per C-040.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

from smart_battery.algorithms import (
    calculate_deferred_start,
    is_charge_target_reachable,
)
from smart_battery.sensor_base import (
    SmartOperationsOverviewSensor,
    charge_reachability_slack_minutes,
    charge_time_slack_seconds,
)

from .test_charge_slack_sensor import TEST_DOMAIN, _charge_state_active, _make_hass

_NOW = datetime.datetime(2026, 4, 8, 3, 0, 0)
_SENSOR = "smart_battery.sensor_base"


def _sensor(hass: MagicMock) -> SmartOperationsOverviewSensor:
    entry = MagicMock()
    entry.entry_id = "e1"
    return SmartOperationsOverviewSensor(hass, entry, TEST_DOMAIN, MagicMock())


def _listener_reachable(cs: dict[str, Any], soc: float, net_kw: float) -> bool:
    """What the listener computes for this session (its parameter list)."""
    remaining = (cs["end"] - _NOW).total_seconds() / 3600.0
    return is_charge_target_reachable(
        current_soc=soc,
        target_soc=cs["target_soc"],
        battery_capacity_kwh=cs["battery_capacity_kwh"],
        remaining_hours=remaining,
        max_power_w=cs["max_power_w"],
        net_consumption_kw=net_kw,
        headroom=0.10,
    )


class TestReachabilityParity:
    def test_uses_the_session_capacity_not_the_options(self) -> None:
        """Options say 42 kWh; the session was started with 10 kWh.  The
        listener paces on 10 kWh, so the sensor must report on 10 kWh."""
        # 60 % of 42 kWh needs ~3.1 h of the 3 h left; of 10 kWh, ~45 min.
        hass = _make_hass(coordinator_soc=40.0, battery_capacity_kwh=42.0)
        cs = _charge_state_active(battery_capacity_kwh=10.0)
        with patch(f"{_SENSOR}.dt_util.now", return_value=_NOW):
            sensor_says = _sensor(hass)._is_charge_reachable(cs, 40.0)
        assert _listener_reachable(cs, 40.0, 0.0) is True
        assert sensor_says is True, "sensor judged the options' 42 kWh"

    def test_uses_net_consumption_like_the_listener(self) -> None:
        """A heavy house load eats the charge budget; the listener counts it,
        so the sensor must too."""
        hass = _make_hass(
            coordinator_soc=20.0,
            battery_capacity_kwh=20.0,
            coordinator_extra={"loadsPower": 8.0, "pvPower": 0.0},
        )
        cs = _charge_state_active(battery_capacity_kwh=20.0)
        with patch(f"{_SENSOR}.dt_util.now", return_value=_NOW):
            sensor_says = _sensor(hass)._is_charge_reachable(cs, 20.0)
        listener_says = _listener_reachable(cs, 20.0, 8.0)
        assert listener_says is False
        assert sensor_says is listener_says

    def test_falls_back_to_options_for_a_session_without_capacity(self) -> None:
        """Sessions persisted before capacity was captured still work."""
        hass = _make_hass(coordinator_soc=50.0, battery_capacity_kwh=10.0)
        cs = _charge_state_active()
        cs.pop("battery_capacity_kwh", None)
        with patch(f"{_SENSOR}.dt_util.now", return_value=_NOW):
            assert _sensor(hass)._is_charge_reachable(cs, 50.0) is True


class TestSlackParity:
    def test_reachability_slack_uses_the_session_capacity(self) -> None:
        hass = _make_hass(coordinator_soc=50.0, battery_capacity_kwh=42.0)
        cs = _charge_state_active(battery_capacity_kwh=10.0)
        with patch(f"{_SENSOR}.dt_util.now", return_value=_NOW):
            slack = charge_reachability_slack_minutes(hass, TEST_DOMAIN, cs)
        # 5 kWh at 9 kW effective, /0.9 headroom: ~37 min of 180 → ~143.
        assert slack is not None and slack > 100

    def test_deferral_slack_uses_the_session_capacity(self) -> None:
        hass = _make_hass(coordinator_soc=50.0, battery_capacity_kwh=42.0)
        cs = _charge_state_active(battery_capacity_kwh=10.0, charging_started=False)
        with (
            patch(f"{_SENSOR}.dt_util.now", return_value=_NOW),
            patch(f"{_SENSOR}.is_effectively_charging", return_value=False),
        ):
            slack = charge_time_slack_seconds(hass, TEST_DOMAIN, cs)
        expected = calculate_deferred_start(
            50.0, 100, 10.0, 10000, cs["end"], start=cs["start"], headroom=0.10
        )
        assert slack == max(0, int((expected - _NOW).total_seconds()))


class TestDeferralReason:
    """Issue #32: the old text claimed the "SoC trajectory projects to reach
    target without forced charge", which reads as a solar forecast.  There
    is none: the charge waits because starting later at full power still
    reaches the target in time.  The reason must say that, with the time."""

    def test_names_the_start_time_and_makes_no_forecast_claim(self) -> None:
        from smart_battery.sensor_base import _explain_charge_deferral

        cs = _charge_state_active(
            charging_started=False,
            target_soc=100,
            deferred_start_committed=datetime.datetime(2026, 4, 8, 4, 15, 0),
        )
        reason = _explain_charge_deferral(cs, 60.0)
        assert "04:15" in reason
        assert "trajectory" not in reason
        assert "without forced charge" not in reason
        assert "60.0%" in reason and "40.0% gap" in reason

    def test_without_a_committed_start(self) -> None:
        from smart_battery.sensor_base import _explain_charge_deferral

        cs = _charge_state_active(charging_started=False, target_soc=100)
        reason = _explain_charge_deferral(cs, 60.0)
        assert "trajectory" not in reason
        assert "gap" in reason
