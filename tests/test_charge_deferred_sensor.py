"""Tests for charge deferred start sensor-side calculations.

Reproduces the bug where is_effectively_charging() and
estimate_charge_remaining() use a simplified deferred start calculation
that disagrees with the listener's full calculate_deferred_start() call.

The simplified formula misses: headroom buffer, taper profile, net
consumption, and BMS temperature — all of which can cause the full
algorithm to compute an earlier deferred start time.  This leads to the
sensor showing "Charge Scheduled" when charging is actually active.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

from smart_battery.algorithms import calculate_deferred_start
from smart_battery.domain_data import EntryData, SmartBatteryDomainData
from smart_battery.sensor_base import (
    estimate_charge_remaining,
    is_effectively_charging,
)
from smart_battery.taper import TaperProfile

TEST_DOMAIN = "foxess_control"


def _make_hass(
    coordinator_soc: float | None = None,
    coordinator_extra: dict[str, Any] | None = None,
    battery_capacity_kwh: float = 10.0,
    headroom_pct: int = 10,
    taper_profile: TaperProfile | None = None,
) -> MagicMock:
    """Create a mock hass with domain data for sensor_base tests.

    Uses canonical SmartBatteryDomainData (not the FoxESS subclass) so
    that get_domain_data / get_first_coordinator / get_first_entry_id
    from smart_battery.domain_data resolve correctly via isinstance.
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


def _make_taper_with_high_soc_limiting() -> TaperProfile:
    """Create a taper profile that significantly limits charge at high SoC.

    Full power acceptance up to 79%, then 30% acceptance above 80%.
    This is realistic for lithium batteries entering CV phase.
    """
    taper = TaperProfile()
    for soc in range(20, 80):
        for _ in range(3):
            taper.record_charge(float(soc), 10000, 10000.0)
    for soc in range(80, 100):
        for _ in range(3):
            taper.record_charge(float(soc), 10000, 3000.0)
    return taper


def _charge_state(**overrides: Any) -> dict[str, Any]:
    """Build a charge state dict with sensible defaults."""
    state: dict[str, Any] = {
        "target_soc": 95,
        "last_power_w": 0,
        "max_power_w": 10000,
        "start": datetime.datetime(2026, 4, 8, 2, 0, 0),
        "end": datetime.datetime(2026, 4, 8, 6, 0, 0),
        "charging_started": False,
    }
    state.update(overrides)
    return state


class TestIsEffectivelyChargingDeferredMismatch:
    """Test that is_effectively_charging uses the full deferred start algorithm.

    The listener calls calculate_deferred_start() with taper, consumption,
    headroom, and BMS temp.  The sensor-side is_effectively_charging() must
    agree with this calculation.
    """

    def test_taper_causes_earlier_start_detected(self) -> None:
        """With taper, the full algorithm starts earlier; sensor should agree.

        Scenario: 10kWh battery, SoC=50%, target=95%, max_power=10kW,
        window 02:00-06:00, taper profile showing 30% acceptance above 80%.

        The full algorithm accounts for slow charging in the 80-95% range,
        so it computes an earlier deferred start.  The simplified formula
        in the sensor ignores taper and computes a later start.

        At a time between the two start times, is_effectively_charging()
        should return True (matching the listener), not False.
        """
        taper = _make_taper_with_high_soc_limiting()
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
            taper_profile=taper,
        )
        cs = _charge_state(
            max_power_w=10000,
            target_soc=95,
            start=datetime.datetime(2026, 4, 8, 2, 0, 0),
            end=datetime.datetime(2026, 4, 8, 6, 0, 0),
        )

        # Calculate the full deferred start (what the listener uses)
        full_deferred = calculate_deferred_start(
            50.0,
            95,
            10.0,
            10000,
            cs["end"],
            start=cs["start"],
            headroom=0.10,
            taper_profile=taper,
        )

        # Pick a time after the full algorithm's deferred start but where
        # the simplified formula would still say "deferred"
        test_time = full_deferred + datetime.timedelta(minutes=5)

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=test_time,
        ):
            result = is_effectively_charging(hass, TEST_DOMAIN, cs)
            assert result is True, (
                f"is_effectively_charging returned False at {test_time} "
                f"but full algorithm deferred start is {full_deferred}"
            )

    def test_high_consumption_causes_earlier_start_detected(self) -> None:
        """With high consumption, the full algorithm starts earlier.

        Scenario: 10kWh battery, SoC=50%, target=90%, max_power=10kW,
        window 02:00-06:00, house consuming 3kW.

        The full algorithm reduces effective charge power by consumption
        (3kW out of 10kW), computing an earlier start.  The simplified
        formula uses (1-h)^2 * max_power which doesn't account for
        actual consumption.
        """
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
            coordinator_extra={"loadsPower": 3.0, "pvPower": 0.0},
        )
        cs = _charge_state(
            max_power_w=10000,
            target_soc=90,
            start=datetime.datetime(2026, 4, 8, 2, 0, 0),
            end=datetime.datetime(2026, 4, 8, 6, 0, 0),
        )

        # Full deferred start with consumption
        full_deferred = calculate_deferred_start(
            50.0,
            90,
            10.0,
            10000,
            cs["end"],
            net_consumption_kw=3.0,
            start=cs["start"],
            headroom=0.10,
        )

        # Pick a time after the full algorithm's start but where the
        # simplified formula (which ignores consumption) says "deferred"
        test_time = full_deferred + datetime.timedelta(minutes=5)

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=test_time,
        ):
            result = is_effectively_charging(hass, TEST_DOMAIN, cs)
            assert result is True, (
                f"is_effectively_charging returned False at {test_time} "
                f"but full algorithm deferred start (with 3kW consumption) "
                f"is {full_deferred}"
            )

    def test_charging_started_true_always_returns_true(self) -> None:
        """Happy path: when charging_started is set, always return True."""
        hass = _make_hass(coordinator_soc=50.0)
        cs = _charge_state(charging_started=True)

        result = is_effectively_charging(hass, TEST_DOMAIN, cs)
        assert result is True

    def test_taper_and_consumption_combined(self) -> None:
        """Combined taper + consumption should cause even earlier start."""
        taper = _make_taper_with_high_soc_limiting()
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
            taper_profile=taper,
            coordinator_extra={"loadsPower": 2.0, "pvPower": 0.0},
        )
        cs = _charge_state(
            max_power_w=10000,
            target_soc=95,
            start=datetime.datetime(2026, 4, 8, 2, 0, 0),
            end=datetime.datetime(2026, 4, 8, 6, 0, 0),
        )

        full_deferred = calculate_deferred_start(
            50.0,
            95,
            10.0,
            10000,
            cs["end"],
            net_consumption_kw=2.0,
            start=cs["start"],
            headroom=0.10,
            taper_profile=taper,
        )

        test_time = full_deferred + datetime.timedelta(minutes=5)

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=test_time,
        ):
            result = is_effectively_charging(hass, TEST_DOMAIN, cs)
            assert result is True, (
                f"is_effectively_charging returned False at {test_time} "
                f"but full algorithm (taper+consumption) deferred start "
                f"is {full_deferred}"
            )


class TestEstimateChargeRemainingDeferredMismatch:
    """Test that estimate_charge_remaining uses the full deferred start algorithm.

    The badge text should show accurate "starts in" timing based on the
    same deferred start calculation the listener uses.
    """

    def test_taper_affects_starts_in_timing(self) -> None:
        """With taper, the 'starts in' time should be shorter (earlier start).

        The simplified formula computes a later start, showing a longer
        wait.  The full algorithm starts earlier, so the badge should show
        a shorter "starts in" duration.
        """
        taper = _make_taper_with_high_soc_limiting()
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
            taper_profile=taper,
        )
        cs = _charge_state(
            max_power_w=10000,
            target_soc=95,
            start=datetime.datetime(2026, 4, 8, 2, 0, 0),
            end=datetime.datetime(2026, 4, 8, 6, 0, 0),
        )

        # Full algorithm gives an earlier start time
        full_deferred = calculate_deferred_start(
            50.0,
            95,
            10.0,
            10000,
            cs["end"],
            start=cs["start"],
            headroom=0.10,
            taper_profile=taper,
        )

        # Test at a point well before both deferred starts, but where
        # the difference in computed wait times is measurable.
        test_time = datetime.datetime(2026, 4, 8, 2, 0, 0)

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=test_time,
        ):
            result = estimate_charge_remaining(hass, TEST_DOMAIN, cs)
            assert result.startswith("starts in"), (
                f"Expected 'starts in ...' but got '{result}'"
            )

            # Calculate expected wait from the full algorithm
            expected_wait_minutes = (full_deferred - test_time).total_seconds() / 60
            # Parse actual wait from the result
            actual_text = result.replace("starts in ", "")
            actual_minutes = _parse_duration_minutes(actual_text)

            # The sensor's "starts in" should be close to the full
            # algorithm's timing (within 10 minutes), not the simplified
            # formula's timing.
            assert abs(actual_minutes - expected_wait_minutes) <= 10, (
                f"Badge shows '{result}' ({actual_minutes}m) but full "
                f"algorithm says {expected_wait_minutes:.0f}m wait. "
                f"Mismatch > 10 minutes indicates simplified formula used."
            )

    def test_consumption_affects_starts_in_timing(self) -> None:
        """With consumption, 'starts in' should show earlier start time."""
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
            coordinator_extra={"loadsPower": 3.0, "pvPower": 0.0},
        )
        cs = _charge_state(
            max_power_w=10000,
            target_soc=90,
            start=datetime.datetime(2026, 4, 8, 2, 0, 0),
            end=datetime.datetime(2026, 4, 8, 6, 0, 0),
        )

        full_deferred = calculate_deferred_start(
            50.0,
            90,
            10.0,
            10000,
            cs["end"],
            net_consumption_kw=3.0,
            start=cs["start"],
            headroom=0.10,
        )

        test_time = datetime.datetime(2026, 4, 8, 2, 0, 0)

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=test_time,
        ):
            result = estimate_charge_remaining(hass, TEST_DOMAIN, cs)
            assert result.startswith("starts in"), (
                f"Expected 'starts in ...' but got '{result}'"
            )

            expected_wait_minutes = (full_deferred - test_time).total_seconds() / 60
            actual_text = result.replace("starts in ", "")
            actual_minutes = _parse_duration_minutes(actual_text)

            assert abs(actual_minutes - expected_wait_minutes) <= 10, (
                f"Badge shows '{result}' ({actual_minutes}m) but full "
                f"algorithm says {expected_wait_minutes:.0f}m wait. "
                f"Mismatch > 10 minutes indicates simplified formula used."
            )

    def test_past_full_deferred_start_shows_window_remaining(self) -> None:
        """After full algorithm's deferred start, show window remaining."""
        taper = _make_taper_with_high_soc_limiting()
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
            taper_profile=taper,
        )
        cs = _charge_state(
            max_power_w=10000,
            target_soc=95,
            start=datetime.datetime(2026, 4, 8, 2, 0, 0),
            end=datetime.datetime(2026, 4, 8, 6, 0, 0),
        )

        full_deferred = calculate_deferred_start(
            50.0,
            95,
            10.0,
            10000,
            cs["end"],
            start=cs["start"],
            headroom=0.10,
            taper_profile=taper,
        )

        # 5 minutes after the full algorithm's deferred start
        test_time = full_deferred + datetime.timedelta(minutes=5)

        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=test_time,
        ):
            result = estimate_charge_remaining(hass, TEST_DOMAIN, cs)
            # Should show window remaining, not "starts in ..."
            assert not result.startswith("starts in"), (
                f"Expected window remaining but got '{result}' — "
                f"sensor thinks charging hasn't started yet"
            )


class TestIsEffectivelyChargingStability:
    """The sensor's charge phase must not oscillate under micro-fluctuations.

    Observed 2026-04-27 (11:00–13:59 live session): the operations sensor
    (``sensor.foxess_smart_operations``) flipped between ``charging`` and
    ``deferred`` many times in a 3-hour window — e.g. 02:38:56 deferred →
    02:39:01 charging (5 seconds) → 02:39:36 deferred (35 seconds) →
    02:41:58 charging (2m 22s).  During the same window the inverter
    ``betriebsmodus`` only transitioned twice (SelfUse ↔ ForceCharge),
    confirming that the inverter hardware was stable but the sensor's
    reported phase was flipping.

    Root cause: ``is_effectively_charging()`` recomputes
    ``calculate_deferred_start()`` on every coordinator refresh (~5 s with
    WebSocket data).  The algorithm is sensitive to ``net_consumption_kw``
    and ``current_soc`` — both of which fluctuate second-to-second in real
    homes (appliance cycling, BMS reporting jitter, solar clouds).  When
    the recomputed ``deferred_start`` straddles ``now``, the ``now >=
    deferred`` comparison flips on every tick, and the sensor state
    oscillates on a 5-second cadence.

    This violates C-020 (operational transparency) — the user literally
    cannot tell from the UI what the system is doing — and C-038 insofar
    as the sensor reports a *different* phase than the listener holds
    stable between 5-minute ticks.

    Invariant: once ``charging_started`` is False (either the initial
    deferred state or a D-043 re-deferral), the sensor must hold that
    phase until the listener's next scheduled run, not flip it based on
    sub-minute fluctuations in consumption or SoC reporting noise.
    """

    def test_no_flip_under_consumption_noise(self) -> None:
        """Consumption jitter between ticks must not flip the phase.

        Reproduces the user's 2026-04-27 live symptom.  Drives
        ``is_effectively_charging`` with a sequence of plausible
        tick-to-tick net-consumption readings (appliance cycling and solar
        flicker both routinely swing site load by ±1 kW on second-scale
        timeframes).  The function must return a single, stable phase —
        not a different answer for each reading.
        """
        hass = _make_hass(
            coordinator_soc=63.5,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        cs = _charge_state(
            target_soc=90,
            max_power_w=5000,
            start=datetime.datetime(2026, 4, 27, 11, 0, 0),
            end=datetime.datetime(2026, 4, 27, 13, 59, 0),
            charging_started=False,
        )

        # Plausible tick-to-tick variation: appliance cycling, BMS
        # reporting jitter, solar flicker.  All readings are within the
        # legitimate "house load" band for a 5 kW system.
        consumption_readings = [1.5, 1.7, 1.6, 1.8, 1.5, 1.4, 1.9, 1.6, 1.7]
        # "now" positioned right on the boundary where the reading-to-reading
        # variation straddles the deferred-start threshold — exactly the
        # conditions under which the user saw rapid state changes.
        test_time = datetime.datetime(2026, 4, 27, 13, 7, 0)

        results: list[bool] = []
        for loads_kw in consumption_readings:
            # Update coordinator so _get_net_consumption reflects this tick.
            coord = hass.data[TEST_DOMAIN].entries["entry1"].coordinator
            coord.data = {"SoC": 63.5, "loadsPower": loads_kw, "pvPower": 0.0}
            with patch(
                "smart_battery.sensor_base.dt_util.now",
                return_value=test_time,
            ):
                results.append(is_effectively_charging(hass, TEST_DOMAIN, cs))

        # The phase must be a single value across the sequence — no
        # ping-ponging on noise.  If the sensor flips even once on this
        # benign fluctuation it reproduces the live symptom.
        unique_results = set(results)
        assert len(unique_results) == 1, (
            f"is_effectively_charging oscillated on consumption noise: "
            f"sequence={results}, unique={unique_results}, "
            f"consumption_readings={consumption_readings}. "
            f"The sensor must hold a stable phase between listener ticks; "
            f"it may not second-guess charging_started=False on sub-minute "
            f"input variation."
        )

    def test_no_flip_under_soc_micro_movement(self) -> None:
        """0.1% SoC movement (BMS interpolation) must not flip the phase.

        The user's live data shows a 0.1% SoC shift (63.4 → 63.5)
        triggered a re-deferral and immediate re-charging within 35
        seconds.  A 0.1% SoC change is below the threshold at which any
        qualitative phase decision should change.
        """
        hass = _make_hass(
            coordinator_soc=63.5,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        cs = _charge_state(
            target_soc=90,
            max_power_w=5000,
            start=datetime.datetime(2026, 4, 27, 11, 0, 0),
            end=datetime.datetime(2026, 4, 27, 13, 59, 0),
            charging_started=False,
        )

        # Pick consumption + time such that the raw algorithm is right on
        # the boundary (deferred ≈ now).  With consumption=1.6kW and the
        # sequence below we straddle the transition point and reproduce
        # the live symptom.
        test_time = datetime.datetime(2026, 4, 27, 13, 7, 0)
        soc_sequence = [63.4, 63.5, 63.4, 63.5, 63.6, 63.5, 63.4]

        results: list[bool] = []
        for soc in soc_sequence:
            coord = hass.data[TEST_DOMAIN].entries["entry1"].coordinator
            coord.data = {"SoC": soc, "loadsPower": 1.6, "pvPower": 0.0}
            with patch(
                "smart_battery.sensor_base.dt_util.now",
                return_value=test_time,
            ):
                results.append(is_effectively_charging(hass, TEST_DOMAIN, cs))

        unique_results = set(results)
        assert len(unique_results) == 1, (
            f"is_effectively_charging oscillated on 0.1% SoC micro-movement: "
            f"sequence={results}, unique={unique_results}, "
            f"soc_sequence={soc_sequence}. "
            f"A 0.1% SoC change (reporting granularity / interpolation "
            f"noise) is not a qualitative change in system state and "
            f"must not flip the reported phase."
        )

    def test_charging_started_true_is_stable(self) -> None:
        """Inverse case: when charging_started=True, phase stays charging.

        Confirms the stability invariant works in both directions — the
        phase does not flip to 'deferred' under noisy inputs when the
        listener has committed to charging.
        """
        hass = _make_hass(
            coordinator_soc=63.5,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        cs = _charge_state(
            target_soc=90,
            max_power_w=5000,
            start=datetime.datetime(2026, 4, 27, 11, 0, 0),
            end=datetime.datetime(2026, 4, 27, 13, 59, 0),
            charging_started=True,
        )

        test_time = datetime.datetime(2026, 4, 27, 13, 7, 0)
        # Heavy noise — should not matter when charging_started=True.
        for loads_kw in [0.1, 3.0, 0.2, 2.5, 0.3, 2.0]:
            coord = hass.data[TEST_DOMAIN].entries["entry1"].coordinator
            coord.data = {"SoC": 63.5, "loadsPower": loads_kw, "pvPower": 0.0}
            with patch(
                "smart_battery.sensor_base.dt_util.now",
                return_value=test_time,
            ):
                assert is_effectively_charging(hass, TEST_DOMAIN, cs) is True

    def test_phase_still_transitions_on_qualitative_change(self) -> None:
        """Stability must not suppress real transitions.

        Once the qualitative input truly justifies a different phase
        (e.g. now is materially past the deferred start even under the
        worst-case / low-consumption estimate), the sensor MUST still
        report the appropriate phase.  Hysteresis that suppresses real
        signal is tuning, not a root-cause fix (C-031).
        """
        hass = _make_hass(
            coordinator_soc=50.0,
            battery_capacity_kwh=10.0,
            headroom_pct=10,
        )
        cs = _charge_state(
            target_soc=90,
            max_power_w=5000,
            start=datetime.datetime(2026, 4, 27, 11, 0, 0),
            end=datetime.datetime(2026, 4, 27, 13, 59, 0),
            charging_started=False,
        )

        # Mid-window with a large SoC gap (40%) — well past the latest
        # plausible deferred start regardless of consumption noise.
        test_time = datetime.datetime(2026, 4, 27, 13, 50, 0)
        coord = hass.data[TEST_DOMAIN].entries["entry1"].coordinator
        coord.data = {"SoC": 50.0, "loadsPower": 0.5, "pvPower": 0.0}
        with patch(
            "smart_battery.sensor_base.dt_util.now",
            return_value=test_time,
        ):
            assert is_effectively_charging(hass, TEST_DOMAIN, cs) is True, (
                "With 40% gap and 9 minutes remaining, the sensor must "
                "report 'charging' — the listener cannot possibly be "
                "deferring in this state.  Hysteresis must not suppress "
                "legitimate transitions."
            )


def _parse_duration_minutes(text: str) -> float:
    """Parse a duration string like '3h 17m' or '42m' into total minutes."""
    hours = 0
    minutes = 0
    parts = text.split()
    for part in parts:
        if part.endswith("h"):
            hours = int(part[:-1])
        elif part.endswith("m"):
            minutes = int(part[:-1])
    return hours * 60 + minutes
