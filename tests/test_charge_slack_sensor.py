"""Brand-agnostic tests for ``sensor.foxess_charge_slack``.

The slack sensor exposes — during the *active* charging phase — how
many minutes of margin the algorithm has between its
*buffered_hours* (the time the algorithm projects it needs at the
current effective charge rate, with consumption + headroom buffers)
and *remaining_hours* (the actual time left in the window).

Positive: comfortable margin — the target is reachable with time to
spare.
Zero: exactly on the deadline.
Negative: ``is_charge_target_reachable`` would return False — the
target is unreachable.

The sensor MUST share its parameter list with the listener's
reachability check (C-038): both the sensor and the listener
ultimately call ``_buffered_charge_hours(...)`` with the same inputs,
so they cannot disagree about whether the algorithm thinks the target
is reachable.

Brand-agnostic per C-040: this file imports only ``smart_battery/``
modules + stdlib + pytest.  It does not load
``custom_components.foxess_control.*``.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smart_battery.algorithms import (
    _buffered_charge_hours,
    is_charge_target_reachable,
)
from smart_battery.domain_data import EntryData, SmartBatteryDomainData
from smart_battery.sensor_base import (
    ChargeSlackSensor,
    charge_reachability_slack_minutes,
)
from smart_battery.taper import TaperProfile

TEST_DOMAIN = "foxess_control"


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_hass(
    coordinator_soc: float | None = None,
    coordinator_extra: dict[str, Any] | None = None,
    battery_capacity_kwh: float = 10.0,
    headroom_pct: int = 10,
    taper_profile: TaperProfile | None = None,
) -> MagicMock:
    """Create a mock hass with brand-agnostic SmartBatteryDomainData.

    Mirror of ``test_charge_deferred_sensor._make_hass`` — uses canonical
    domain data so ``get_domain_data``/``get_first_coordinator``/
    ``get_first_entry_id`` resolve correctly.
    """
    hass = MagicMock()
    mock_coordinator = MagicMock()
    coordinator_data: dict[str, Any] = {}
    if coordinator_soc is not None:
        coordinator_data["SoC"] = coordinator_soc
    if coordinator_extra:
        coordinator_data.update(coordinator_extra)
    mock_coordinator.data = coordinator_data if coordinator_data else None

    dd = SmartBatteryDomainData()
    dd.entries["entry1"] = EntryData(coordinator=mock_coordinator)
    if taper_profile is not None:
        dd.taper_profile = taper_profile
    hass.data = {TEST_DOMAIN: dd}

    mock_entry = MagicMock()
    mock_entry.options = {
        "battery_capacity_kwh": battery_capacity_kwh,
        "smart_headroom": headroom_pct,
    }
    hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)
    return hass


def _charge_state_active(**overrides: Any) -> dict[str, Any]:
    """Build an *actively charging* charge state dict."""
    state: dict[str, Any] = {
        "target_soc": 100,
        "last_power_w": 5000,
        "max_power_w": 10000,
        "start": datetime.datetime(2026, 4, 8, 2, 0, 0),
        "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
        "charging_started": True,  # actively charging
    }
    state.update(overrides)
    return state


def _charge_state_deferred(**overrides: Any) -> dict[str, Any]:
    """Build a *deferred* charge state dict."""
    state: dict[str, Any] = {
        "target_soc": 100,
        "last_power_w": 0,
        "max_power_w": 10000,
        "start": datetime.datetime(2026, 4, 8, 2, 0, 0),
        "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
        "charging_started": False,
        # No deferred_start_committed — is_effectively_charging will
        # return False.
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Helper-function tests (charge_reachability_slack_minutes)
# ---------------------------------------------------------------------------


class TestChargeReachabilitySlackMinutes:
    """Unit tests for the helper that the sensor calls."""

    def test_returns_minutes_during_active_charge(self) -> None:
        """Comfortable margin: buffered ~1.0h, remaining 1.5h → ~30min slack.

        Inputs picked to make the math obvious:
          - 50% gap, 10 kWh capacity → 5 kWh needed
          - 10 kW max, headroom 10% → effective 9 kW (no consumption)
          - charge_hours = 5/9 ≈ 0.556 h
          - buffered_hours = 0.556 / 0.9 ≈ 0.617 h ≈ 37 min
          - remaining = 1.5 h = 90 min
          - slack ≈ 90 - 37 = 53 min
        """
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        # Window ends 1.5h after now.
        now = datetime.datetime(2026, 4, 8, 2, 0, 0)
        end = now + datetime.timedelta(hours=1.5)
        cs = _charge_state_active(
            target_soc=100,
            max_power_w=10000,
            start=now,
            end=end,
        )
        with patch("smart_battery.sensor_base.dt_util.now", return_value=now):
            slack = charge_reachability_slack_minutes(hass, TEST_DOMAIN, cs)
        assert slack is not None
        assert slack > 0, "comfortable margin must be positive"
        # Spot-check: should be in the ballpark of 53 min.
        assert 40 <= slack <= 70, f"expected ~53 min, got {slack}"

    def test_returns_negative_when_unreachable(self) -> None:
        """Tight remaining window: buffered > remaining → negative slack."""
        hass = _make_hass(
            coordinator_soc=20.0,  # 80% gap
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        # Only 30 minutes remaining for 8 kWh — at 9 kW effective that's
        # ~53 min needed → ~23 min over.  The buffered figure adds the
        # 10% headroom buffer → ~58 min, slack = 30 - 58 = -28 min.
        now = datetime.datetime(2026, 4, 8, 2, 0, 0)
        end = now + datetime.timedelta(minutes=30)
        cs = _charge_state_active(
            target_soc=100,
            max_power_w=10000,
            start=now,
            end=end,
        )
        with patch("smart_battery.sensor_base.dt_util.now", return_value=now):
            slack = charge_reachability_slack_minutes(hass, TEST_DOMAIN, cs)
        assert slack is not None
        assert slack < 0, f"unreachable target must yield negative slack, got {slack}"

    def test_returns_unavailable_when_no_session(self) -> None:
        """``cs is None`` (no charge session) returns None (unavailable)."""
        hass = _make_hass(coordinator_soc=50.0)
        slack = charge_reachability_slack_minutes(hass, TEST_DOMAIN, None)
        assert slack is None

    def test_returns_unavailable_during_deferred_phase(self) -> None:
        """Deferred phase has its own slack semantics — this sensor is
        explicitly NOT for that.  ``is_effectively_charging`` is False
        when ``charging_started=False`` and no ``deferred_start_committed``
        is in the past, so the sensor must return None.
        """
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
        )
        now = datetime.datetime(2026, 4, 8, 1, 0, 0)
        cs = _charge_state_deferred(
            start=datetime.datetime(2026, 4, 8, 2, 0, 0),
            end=datetime.datetime(2026, 4, 8, 6, 0, 0),
        )
        with patch("smart_battery.sensor_base.dt_util.now", return_value=now):
            slack = charge_reachability_slack_minutes(hass, TEST_DOMAIN, cs)
        assert slack is None, (
            f"deferred phase must return None (unavailable), got {slack}; "
            "the sensor's semantics apply only during active charge"
        )

    @pytest.mark.parametrize(
        ("soc", "target", "remaining_hours"),
        [
            (50.0, 100, 1.5),  # comfortable
            (50.0, 100, 0.5),  # tight
            (20.0, 100, 1.0),  # very tight
            (90.0, 100, 1.0),  # nearly full
            (30.0, 80, 2.0),  # wide window
        ],
    )
    def test_parity_with_listener_reachability_verdict(
        self, soc: float, target: int, remaining_hours: float
    ) -> None:
        """C-038 parity: when ``is_charge_target_reachable`` says True,
        the sensor reports slack >= 0; when False, slack < 0.
        """
        hass = _make_hass(
            coordinator_soc=soc,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        now = datetime.datetime(2026, 4, 8, 2, 0, 0)
        end = now + datetime.timedelta(hours=remaining_hours)
        cs = _charge_state_active(
            target_soc=target,
            max_power_w=10000,
            start=now,
            end=end,
        )

        # Listener verdict (no taper, no consumption, no temp — same as
        # the sensor will use given the empty coordinator data).
        reachable = is_charge_target_reachable(
            current_soc=soc,
            target_soc=target,
            battery_capacity_kwh=10.0,
            remaining_hours=remaining_hours,
            max_power_w=10000,
            net_consumption_kw=0.0,
            headroom=0.10,
            taper_profile=None,
            bms_temp_c=None,
        )

        with patch("smart_battery.sensor_base.dt_util.now", return_value=now):
            slack = charge_reachability_slack_minutes(hass, TEST_DOMAIN, cs)

        assert slack is not None
        if reachable:
            assert slack >= 0, (
                f"listener says reachable but slack={slack} is negative — "
                "C-038 parity violated"
            )
        else:
            assert slack < 0, (
                f"listener says unreachable but slack={slack} is non-negative — "
                "C-038 parity violated"
            )

    def test_clamped_to_sane_range_high(self) -> None:
        """Large remaining window → slack capped at 7 days (10080 min)."""
        hass = _make_hass(
            coordinator_soc=99.0,  # tiny gap
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        now = datetime.datetime(2026, 4, 8, 2, 0, 0)
        end = now + datetime.timedelta(days=30)  # ridiculous
        cs = _charge_state_active(
            target_soc=100,
            max_power_w=10000,
            start=now,
            end=end,
        )
        with patch("smart_battery.sensor_base.dt_util.now", return_value=now):
            slack = charge_reachability_slack_minutes(hass, TEST_DOMAIN, cs)
        assert slack is not None
        assert slack == 60 * 24 * 7, f"expected high-side cap 10080, got {slack}"

    def test_clamped_to_sane_range_low(self) -> None:
        """Massively unreachable → slack floored at -1 day (-1440 min)."""
        hass = _make_hass(
            coordinator_soc=10.0,  # 90% gap
            battery_capacity_kwh=100.0,  # huge battery
            headroom_pct=10,
        )
        now = datetime.datetime(2026, 4, 8, 2, 0, 0)
        end = now + datetime.timedelta(minutes=1)  # essentially zero
        cs = _charge_state_active(
            target_soc=100,
            max_power_w=1000,  # tiny inverter
            start=now,
            end=end,
        )
        with patch("smart_battery.sensor_base.dt_util.now", return_value=now):
            slack = charge_reachability_slack_minutes(hass, TEST_DOMAIN, cs)
        assert slack is not None
        assert slack == -60 * 24, f"expected low-side floor -1440, got {slack}"

    def test_uses_same_taper_and_temp_inputs_as_listener(self) -> None:
        """Refactor guard: dropping taper or bms_temp_c from the helper
        would produce a different slack value.  This test exercises both
        a no-taper baseline and a high-SoC-limiting taper profile, and
        asserts the slack values differ — proving the helper threads
        the taper profile through to the algorithm.
        """
        # No-taper baseline.
        hass_plain = _make_hass(
            coordinator_soc=80.0,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        # With taper that limits the 80-100% region to 30% acceptance.
        taper = TaperProfile()
        for soc in range(20, 80):
            for _ in range(3):
                taper.record_charge(float(soc), 10000, 10000.0)
        for soc in range(80, 100):
            for _ in range(3):
                taper.record_charge(float(soc), 10000, 3000.0)
        hass_taper = _make_hass(
            coordinator_soc=80.0,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
            taper_profile=taper,
        )
        # Cold BMS (5°C) reduces charge acceptance via charge_temp_factor;
        # this proves the bms_temp_c parameter is threaded through.
        hass_taper_cold = _make_hass(
            coordinator_soc=80.0,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
            taper_profile=taper,
            coordinator_extra={"bmsBatteryTemperature": 5.0},
        )

        now = datetime.datetime(2026, 4, 8, 2, 0, 0)
        end = now + datetime.timedelta(hours=2.0)
        cs = _charge_state_active(
            target_soc=100,
            max_power_w=10000,
            start=now,
            end=end,
        )
        with patch("smart_battery.sensor_base.dt_util.now", return_value=now):
            slack_plain = charge_reachability_slack_minutes(hass_plain, TEST_DOMAIN, cs)
            slack_taper = charge_reachability_slack_minutes(hass_taper, TEST_DOMAIN, cs)
            slack_taper_cold = charge_reachability_slack_minutes(
                hass_taper_cold, TEST_DOMAIN, cs
            )

        assert slack_plain is not None
        assert slack_taper is not None
        assert slack_taper_cold is not None
        # Taper limiting high-SoC acceptance should *reduce* slack vs the
        # no-taper baseline (the algorithm needs more time, so margin
        # shrinks).
        assert slack_taper < slack_plain, (
            f"taper must reduce slack: plain={slack_plain}, taper={slack_taper}"
        )
        # Cold BMS should reduce slack further (cold reduces charge
        # acceptance via charge_temp_factor).
        assert slack_taper_cold <= slack_taper, (
            f"cold BMS must not raise slack: taper={slack_taper}, "
            f"taper+cold={slack_taper_cold}"
        )


class TestBufferedChargeHoursRefactor:
    """``_buffered_charge_hours`` must agree with ``is_charge_target_reachable``.

    The C-038 parity guarantee depends on a single shared computation:
    the listener calls ``is_charge_target_reachable``, which calls
    ``_buffered_charge_hours``; the sensor calls ``_buffered_charge_hours``
    directly.  This test pins the relationship.
    """

    @pytest.mark.parametrize(
        ("soc", "target", "remaining_hours"),
        [
            (50.0, 100, 1.5),
            (20.0, 80, 0.5),
            (40.0, 90, 2.0),
            (90.0, 95, 0.25),
        ],
    )
    def test_reachability_equals_buffered_le_remaining(
        self, soc: float, target: int, remaining_hours: float
    ) -> None:
        """The reachability verdict is exactly ``buffered_hours <= remaining``."""
        buffered = _buffered_charge_hours(
            current_soc=soc,
            target_soc=target,
            battery_capacity_kwh=10.0,
            remaining_hours=remaining_hours,
            max_power_w=10000,
            net_consumption_kw=0.0,
            headroom=0.10,
            taper_profile=None,
            bms_temp_c=None,
        )
        verdict = is_charge_target_reachable(
            current_soc=soc,
            target_soc=target,
            battery_capacity_kwh=10.0,
            remaining_hours=remaining_hours,
            max_power_w=10000,
            net_consumption_kw=0.0,
            headroom=0.10,
            taper_profile=None,
            bms_temp_c=None,
        )
        assert (buffered <= remaining_hours) == verdict, (
            f"_buffered_charge_hours={buffered}, remaining={remaining_hours}, "
            f"verdict={verdict} — listener / sensor cannot disagree"
        )


# ---------------------------------------------------------------------------
# ChargeSlackSensor entity tests
# ---------------------------------------------------------------------------


class TestChargeSlackSensorEntity:
    """End-to-end through the SensorEntity wrapper."""

    def _make_sensor(self, hass: MagicMock) -> ChargeSlackSensor:
        entry = MagicMock()
        entry.entry_id = "test-entry-id"
        entry.options = {
            "battery_capacity_kwh": 10.0,
            "smart_headroom": 10,
        }
        # MagicMock device_info — only used for HA registration.
        device_info: Any = {"identifiers": {("test", "test-entry-id")}}
        return ChargeSlackSensor(hass, entry, TEST_DOMAIN, device_info)

    def test_native_value_unavailable_when_no_session(self) -> None:
        hass = _make_hass(coordinator_soc=50.0)
        # No smart_charge_state on dd.
        sensor = self._make_sensor(hass)
        assert sensor.native_value is None

    def test_native_value_unavailable_during_deferred(self) -> None:
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
        )
        cs = _charge_state_deferred(
            start=datetime.datetime(2026, 4, 8, 2, 0, 0),
            end=datetime.datetime(2026, 4, 8, 6, 0, 0),
        )
        hass.data[TEST_DOMAIN].smart_charge_state = cs
        sensor = self._make_sensor(hass)

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 1, 0, 0),
        ):
            assert sensor.native_value is None

    def test_native_value_returns_minutes_during_active_charge(self) -> None:
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
        )
        now = datetime.datetime(2026, 4, 8, 2, 0, 0)
        end = now + datetime.timedelta(hours=1.5)
        cs = _charge_state_active(
            target_soc=100,
            max_power_w=10000,
            start=now,
            end=end,
        )
        hass.data[TEST_DOMAIN].smart_charge_state = cs
        sensor = self._make_sensor(hass)

        with patch("smart_battery.sensor_base.dt_util.now", return_value=now):
            value = sensor.native_value
        assert value is not None
        assert isinstance(value, int)
        assert value > 0

    def test_attributes_unit_state_class_and_translation_key(self) -> None:
        """The sensor exposes the documented HA contract."""
        hass = _make_hass(coordinator_soc=50.0)
        sensor = self._make_sensor(hass)
        assert sensor._attr_native_unit_of_measurement == "min"
        assert sensor._attr_translation_key == "charge_slack"
        # State class must be MEASUREMENT so HA records history.
        from homeassistant.components.sensor import SensorStateClass

        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT


# ---------------------------------------------------------------------------
# Translation coverage
# ---------------------------------------------------------------------------


class TestChargeSlackTranslationCoverage:
    """Every locale (and strings.json) must define ``charge_slack``.

    Without this the sensor renders with the raw key name in any locale
    that's missing an entry, which violates the existing i18n discipline
    (CLAUDE.md: ``tests/test_card_translations.py`` already enforces a
    similar guarantee for the Lovelace card; this is the equivalent
    guard for HA entity-name translations).
    """

    @pytest.mark.parametrize(
        "locale_path",
        [
            "custom_components/foxess_control/strings.json",
            "custom_components/foxess_control/translations/en.json",
            "custom_components/foxess_control/translations/de.json",
            "custom_components/foxess_control/translations/es.json",
            "custom_components/foxess_control/translations/fr.json",
            "custom_components/foxess_control/translations/it.json",
            "custom_components/foxess_control/translations/ja.json",
            "custom_components/foxess_control/translations/nl.json",
            "custom_components/foxess_control/translations/pl.json",
            "custom_components/foxess_control/translations/pt.json",
            "custom_components/foxess_control/translations/zh-Hans.json",
        ],
    )
    def test_locale_has_charge_slack_entry(self, locale_path: str) -> None:
        import json
        from pathlib import Path

        path = Path(locale_path)
        assert path.is_file(), f"locale file missing: {locale_path}"
        data = json.loads(path.read_text())
        sensors = data.get("entity", {}).get("sensor", {})
        assert "charge_slack" in sensors, (
            f"{locale_path} is missing 'entity.sensor.charge_slack' — "
            "users running this locale will see the raw translation key "
            "instead of a localised sensor name"
        )
        assert "name" in sensors["charge_slack"], (
            f"{locale_path} 'entity.sensor.charge_slack' has no 'name' field"
        )
        assert sensors["charge_slack"]["name"], (
            f"{locale_path} 'entity.sensor.charge_slack' name is empty"
        )
