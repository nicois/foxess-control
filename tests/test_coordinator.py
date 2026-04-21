"""Tests for the FoxESSDataCoordinator."""

from __future__ import annotations

import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.foxess_control.const import POLLED_VARIABLES
from custom_components.foxess_control.coordinator import FoxESSDataCoordinator
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
)
from custom_components.foxess_control.foxess.inverter import Inverter, WorkMode


def _make_coordinator(
    inverter: Inverter | None = None,
    update_interval: int = 300,
) -> FoxESSDataCoordinator:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    # Use a real dict for hass.data so domain data lookups behave
    # like production (MagicMock would make every .get() truthy,
    # triggering BMS fetch scheduling in tests that don't set up a
    # web session).
    hass.data = {}
    if inverter is None:
        inverter = MagicMock(spec=Inverter)
    with patch("homeassistant.helpers.frame.report_usage"):
        coord = FoxESSDataCoordinator(hass, inverter, update_interval)
    return coord


class TestAsyncUpdateData:
    """Tests for _async_update_data."""

    @pytest.mark.asyncio
    async def test_calls_get_real_time_with_polled_variables(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 75.0, "batChargePower": 1.2}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        coord = _make_coordinator(inverter=inv)

        result = await coord._async_update_data()

        inv.get_real_time.assert_called_once_with(POLLED_VARIABLES)
        assert result["SoC"] == 75.0
        assert result["batChargePower"] == 1.2

    @pytest.mark.asyncio
    async def test_wraps_exceptions_in_update_failed(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.side_effect = RuntimeError("API error")
        coord = _make_coordinator(inverter=inv)

        with pytest.raises(UpdateFailed, match="API error"):
            await coord._async_update_data()

    @pytest.mark.asyncio
    async def test_rest_failure_keeps_last_data(self) -> None:
        """When REST fails but we have data, return last-known."""
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.side_effect = RuntimeError("API error")
        coord = _make_coordinator(inverter=inv)
        # Simulate having existing data (e.g. from a previous poll or WS)
        coord.data = {"SoC": 46.0, "_data_source": "ws"}

        result = await coord._async_update_data()
        assert result["SoC"] == 46.0
        # Should NOT raise UpdateFailed

    @pytest.mark.asyncio
    async def test_rest_failure_raises_when_no_data(self) -> None:
        """When REST fails with no existing data, raise UpdateFailed."""
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.side_effect = RuntimeError("API error")
        coord = _make_coordinator(inverter=inv)
        # No existing data — use object.__setattr__ to bypass type check
        object.__setattr__(coord, "data", None)

        with pytest.raises(UpdateFailed, match="API error"):
            await coord._async_update_data()

    @pytest.mark.asyncio
    async def test_returns_empty_dict_from_api(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {}
        inv.get_current_mode.return_value = None
        coord = _make_coordinator(inverter=inv)

        result = await coord._async_update_data()
        assert result["_work_mode"] is None


class TestWorkMode:
    """Tests for work mode fetching in _async_update_data."""

    @pytest.mark.asyncio
    async def test_work_mode_included_in_data(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        coord = _make_coordinator(inverter=inv)

        result = await coord._async_update_data()
        assert result["_work_mode"] == "SelfUse"

    @pytest.mark.asyncio
    async def test_work_mode_force_charge(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0}
        inv.get_current_mode.return_value = WorkMode.FORCE_CHARGE
        coord = _make_coordinator(inverter=inv)

        result = await coord._async_update_data()
        assert result["_work_mode"] == "ForceCharge"

    @pytest.mark.asyncio
    async def test_work_mode_none_when_no_groups(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0}
        inv.get_current_mode.return_value = None
        coord = _make_coordinator(inverter=inv)

        result = await coord._async_update_data()
        assert result["_work_mode"] is None

    @pytest.mark.asyncio
    async def test_work_mode_failure_is_non_fatal(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0, "pvPower": 3.2}
        inv.get_current_mode.side_effect = RuntimeError("scheduler API down")
        coord = _make_coordinator(inverter=inv)

        result = await coord._async_update_data()
        # Real-time data should still be present
        assert result["SoC"] == 50.0
        assert result["pvPower"] == 3.2
        # Work mode should be None (not raise)
        assert result["_work_mode"] is None


class TestInjectRealtimeData:
    """Tests for inject_realtime_data and feed-in energy integration."""

    def _make_coord_with_data(
        self, data: dict[str, object] | None = None
    ) -> FoxESSDataCoordinator:
        coord = _make_coordinator()
        coord.data = data if data is not None else {"SoC": 50.0, "feedin": 100.0}
        return coord

    def test_basic_merge(self) -> None:
        coord = self._make_coord_with_data()
        coord.inject_realtime_data({"SoC": 55.0})
        assert coord.data is not None
        assert coord.data["SoC"] == 55.0
        assert coord.data["feedin"] == 100.0  # unchanged

    def test_skip_when_data_is_none(self) -> None:
        coord = _make_coordinator()
        # Explicitly ensure data is None (not set by helper)
        object.__setattr__(coord, "data", None)
        coord.inject_realtime_data({"SoC": 55.0})  # should not raise

    def test_skip_when_nothing_changed(self) -> None:
        coord = self._make_coord_with_data(
            {
                "SoC": 50.0,
                "feedin": 100.0,
                "_data_source": "ws",
                "_soc_interpolated": 50.0,
            }
        )
        # Seed interpolation state so no new data is generated
        coord._soc_interpolated = 50.0
        coord._soc_last_reported = 50.0
        # Patch to detect if async_set_updated_data is called
        coord.async_set_updated_data = MagicMock()  # type: ignore[method-assign]
        coord.inject_realtime_data({"SoC": 50.0})
        coord.async_set_updated_data.assert_not_called()

    def test_feedin_not_integrated_on_first_ws_update(self) -> None:
        """First WS message establishes baseline — no integration yet."""
        coord = self._make_coord_with_data({"SoC": 50.0, "feedin": 100.0})
        coord.inject_realtime_data({"feedinPower": 5.0, "SoC": 51.0})
        assert coord.data is not None
        # feedin should not change on first update (no elapsed time)
        assert coord.data["feedin"] == 100.0

    def test_feedin_integrated_on_second_ws_update(self) -> None:
        """Second WS message integrates power over elapsed time."""
        coord = self._make_coord_with_data({"SoC": 50.0, "feedin": 100.0})

        # First update: establishes baseline
        coord.inject_realtime_data({"feedinPower": 5.0, "SoC": 50.0})

        # Simulate 5 seconds elapsed
        coord._ws_last_time = time.monotonic() - 5.0

        # Second update: 5kW for 5 seconds = 5 * (5/3600) ≈ 0.00694 kWh
        coord.inject_realtime_data({"feedinPower": 5.0, "SoC": 50.0})
        assert coord.data is not None
        delta = coord.data["feedin"] - 100.0
        assert delta == pytest.approx(5.0 * 5.0 / 3600.0, rel=0.01)

    def test_feedin_trapezoidal_integration(self) -> None:
        """Integration uses average of previous and current power."""
        coord = self._make_coord_with_data({"SoC": 50.0, "feedin": 100.0})

        # First update at 2kW
        coord.inject_realtime_data({"feedinPower": 2.0, "SoC": 50.0})
        coord._ws_last_time = time.monotonic() - 10.0

        # Second update at 4kW — average should be 3kW
        coord.inject_realtime_data({"feedinPower": 4.0, "SoC": 50.0})
        assert coord.data is not None
        delta = coord.data["feedin"] - 100.0
        expected = 3.0 * 10.0 / 3600.0  # avg 3kW for 10s
        assert delta == pytest.approx(expected, rel=0.01)

    def test_feedin_accumulates_across_updates(self) -> None:
        """Multiple WS updates accumulate into the feedin counter."""
        coord = self._make_coord_with_data({"SoC": 50.0, "feedin": 100.0})

        coord.inject_realtime_data({"feedinPower": 5.0, "SoC": 50.0})
        coord._ws_last_time = time.monotonic() - 5.0

        coord.inject_realtime_data({"feedinPower": 5.0, "SoC": 50.0})
        first_feedin = coord.data["feedin"]
        assert first_feedin > 100.0

        coord._ws_last_time = time.monotonic() - 5.0
        coord.inject_realtime_data({"feedinPower": 5.0, "SoC": 50.0})
        assert coord.data["feedin"] > first_feedin

    def test_zero_feedin_power_no_accumulation(self) -> None:
        """Zero feedinPower should not increase the feedin counter."""
        coord = self._make_coord_with_data({"SoC": 50.0, "feedin": 100.0})

        coord.inject_realtime_data({"feedinPower": 0.0, "SoC": 50.0})
        coord._ws_last_time = time.monotonic() - 5.0
        coord.inject_realtime_data({"feedinPower": 0.0, "SoC": 50.0})

        assert coord.data is not None
        assert coord.data["feedin"] == 100.0

    @pytest.mark.asyncio
    async def test_rest_poll_resets_integration_state(self) -> None:
        """REST poll should reset WS integration and restore authoritative feedin."""
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0, "feedin": 200.0}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        coord = _make_coordinator(inverter=inv)
        coord.data = {"SoC": 50.0, "feedin": 100.0}

        # Simulate WS integration state
        coord._ws_last_time = time.monotonic()
        coord._ws_feedin_power_kw = 5.0

        # REST poll resets WS feed-in integration state
        result = await coord._async_update_data()
        assert result["feedin"] == 200.0
        assert coord._ws_feedin_power_kw == 0.0
        # _ws_last_time is preserved (used for SoC interpolation between polls)
        assert coord._ws_last_time is not None

    def test_no_feedin_in_base_data(self) -> None:
        """If coordinator has no feedin value, WS integration is skipped."""
        coord = self._make_coord_with_data({"SoC": 50.0})  # no feedin key

        coord.inject_realtime_data({"feedinPower": 5.0, "SoC": 51.0})
        coord._ws_last_time = time.monotonic() - 5.0
        coord.inject_realtime_data({"feedinPower": 5.0, "SoC": 52.0})

        assert coord.data is not None
        assert "feedin" not in coord.data


class TestDataSourceTracking:
    """Tests for _data_source provenance tracking (C-020)."""

    def _make_coord_with_data(
        self, data: dict[str, object] | None = None
    ) -> FoxESSDataCoordinator:
        coord = _make_coordinator()
        coord.data = data if data is not None else {"SoC": 50.0}
        return coord

    @pytest.mark.asyncio
    async def test_rest_poll_sets_api_source(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        coord = _make_coordinator(inverter=inv)
        result = await coord._async_update_data()
        assert result["_data_source"] == "api"

    def test_ws_injection_sets_ws_source(self) -> None:
        coord = self._make_coord_with_data({"SoC": 50.0, "_data_source": "api"})
        coord.inject_realtime_data({"SoC": 55.0})
        assert coord.data is not None
        assert coord.data["_data_source"] == "ws"

    @pytest.mark.asyncio
    async def test_rest_poll_resets_source_after_ws(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        coord = _make_coordinator(inverter=inv)
        # Simulate WS having set the source
        coord.data = {"SoC": 50.0, "_data_source": "ws"}
        result = await coord._async_update_data()
        assert result["_data_source"] == "api"


class TestSocInterpolationDuringDischarge:
    """Reproduce and verify SoC interpolation during WS-driven discharge.

    Simulates a discharge at ~8.7 kW on a 10 kWh battery with WS
    messages arriving every 5 seconds.
    """

    @staticmethod
    def _make_coord() -> FoxESSDataCoordinator:
        from custom_components.foxess_control.const import DOMAIN

        coord = _make_coordinator()
        coord.data = {"SoC": 97.0, "_data_source": "ws"}
        entry = MagicMock()
        entry.options = {"battery_capacity_kwh": 10.0}
        # Wire hass.data as a real dict so _get_capacity_kwh can
        # iterate entry_ids and look up the config entry.
        dd = FoxESSControlData()
        dd.entries["test-entry"] = FoxESSEntryData()
        coord.hass.data = {DOMAIN: dd}  # type: ignore[assignment]
        coord.hass.config_entries.async_get_entry = MagicMock(  # type: ignore[method-assign]
            return_value=entry
        )
        return coord

    @staticmethod
    def _ws_msg(
        soc: int,
        discharge_kw: float = 8.7,
        *,
        include_feedin: bool = True,
    ) -> dict[str, object]:
        msg: dict[str, object] = {
            "SoC": soc,
            "batChargePower": 0.0,
            "batDischargePower": discharge_kw,
        }
        if include_feedin:
            msg["feedinPower"] = max(0.0, discharge_kw - 0.5)
        return msg

    def test_interpolation_decreases_between_ticks(self) -> None:
        """Interpolated SoC should decrease between integer ticks."""
        coord = self._make_coord()

        base = time.monotonic()
        with patch("time.monotonic", return_value=base):
            coord.inject_realtime_data(self._ws_msg(97))
        initial = coord._soc_interpolated

        # 30s gap to produce a visible delta (~0.07%)
        with patch("time.monotonic", return_value=base + 30):
            coord.inject_realtime_data(self._ws_msg(97))
        after = coord._soc_interpolated

        assert after is not None and initial is not None
        assert after < initial, (
            f"Interpolated SoC should decrease during discharge: {initial} -> {after}"
        )

    def test_clamp_on_tick_change_display_value(self) -> None:
        """On tick change, Math.round(interpolated) must equal the new tick.

        Reproduces the production bug: charging at 10.5 kW on 10 kWh,
        SoC ticks 49→50.  Interpolation was running ahead at ~51.
        The old clamp [50, 50.94] allowed round(50.9) = 51 in the
        Lovelace header while sensor.foxess_battery_soc showed 50.

        The clamp must keep the interpolated value within ±0.5 of
        the reported tick so Math.round() always agrees with the entity.
        """
        coord = self._make_coord()

        # Simulate charging: interpolation ran ahead to 51.2 while
        # the inverter still reported SoC=49.
        coord.data = {"SoC": 49, "_data_source": "ws"}
        base = time.monotonic()
        with patch("time.monotonic", return_value=base):
            coord.inject_realtime_data(
                {"SoC": 49, "batChargePower": 10.5, "batDischargePower": 0.0}
            )
        coord._soc_interpolated = 51.2
        coord._soc_last_reported = 49.0

        # Now the tick changes: inverter reports SoC=50
        with patch("time.monotonic", return_value=base + 5):
            coord.inject_realtime_data(
                {"SoC": 50, "batChargePower": 10.5, "batDischargePower": 0.0}
            )

        # The JS card header does Math.round(interpolated).
        # It must equal the reported integer tick.
        assert round(coord._soc_interpolated) == 50, (
            f"Math.round({coord._soc_interpolated}) = "
            f"{round(coord._soc_interpolated)}, expected 50"
        )

    def test_interpolation_without_feedin_data(self) -> None:
        """SoC integration must run even when feedinPower is absent."""
        coord = self._make_coord()

        base = time.monotonic()
        with patch("time.monotonic", return_value=base):
            coord.inject_realtime_data(self._ws_msg(95, include_feedin=True))
        initial = coord._soc_interpolated

        with patch("time.monotonic", return_value=base + 30):
            coord.inject_realtime_data(self._ws_msg(95, include_feedin=False))
        after = coord._soc_interpolated

        assert after is not None and initial is not None
        assert after < initial, (
            f"Interpolation should run without feedin: {initial} -> {after}"
        )

    def test_smooth_decline_across_ticks(self) -> None:
        """Simulate 2 min of discharge — SoC decreases monotonically."""
        coord = self._make_coord()

        base = time.monotonic()
        values: list[float] = []

        for step in range(24):  # 24 * 5s = 120s
            t = base + step * 5
            elapsed_min = step * 5 / 60
            real_soc = 97.0 - 1.45 * elapsed_min
            tick = int(real_soc)

            with patch("time.monotonic", return_value=t):
                coord.inject_realtime_data(self._ws_msg(tick))

            interp = coord.data.get("_soc_interpolated")
            if interp is not None:
                values.append(round(interp, 1))

        # Monotonically decreasing (allow equal for rounding)
        for i in range(1, len(values)):
            assert values[i] <= values[i - 1], (
                f"SoC increased at step {i}: "
                f"{values[i - 1]} -> {values[i]}\n"
                f"Full: {values}"
            )

        # Meaningful decrease over 2 minutes
        total_drop = values[0] - values[-1]
        assert total_drop > 1.0, (
            f"Expected >1% drop over 2min at 8.7kW, got {total_drop}\nFull: {values}"
        )

    def test_smooth_decline_without_feedin(self) -> None:
        """Same as above but feedinPower absent — real FoxESS WS scenario.

        Without feedin, the broken code only updates via tick-change
        clamping (staircase). The fixed code integrates battery power
        regardless and produces smooth sub-percent changes.
        """
        coord = self._make_coord()

        base = time.monotonic()
        values: list[float] = []

        for step in range(24):
            t = base + step * 5
            elapsed_min = step * 5 / 60
            real_soc = 97.0 - 1.45 * elapsed_min
            tick = int(real_soc)

            with patch("time.monotonic", return_value=t):
                coord.inject_realtime_data(self._ws_msg(tick, include_feedin=False))

            interp = coord.data.get("_soc_interpolated")
            if interp is not None:
                values.append(round(interp, 1))

        # Must have sub-integer changes — not just tick-boundary jumps.
        # Count distinct values: staircase gives ~3 (97, 96, 95);
        # smooth interpolation gives 10+ distinct values.
        distinct = len(set(values))
        assert distinct >= 8, (
            f"Expected smooth decline (8+ distinct values), "
            f"got {distinct} (staircase?)\nFull: {values}"
        )


class TestSocExtrapolationDoesNotStarvePoll:
    """Regression: SoC extrapolation ticks called async_set_updated_data,
    which cancels and reschedules the REST poll timer. If the extrapolation
    fires more frequently than the poll interval, REST polls never run and
    all entities show stale data indefinitely.

    Production symptom: coordinator logs show only "Manually updated"
    (from async_set_updated_data) and never "Finished fetching" (from
    the normal poll cycle). All sensor values are frozen.
    """

    @pytest.mark.asyncio
    async def test_extrapolation_tick_does_not_call_async_set_updated_data(
        self,
    ) -> None:
        """The extrapolation tick must not call async_set_updated_data."""
        from custom_components.foxess_control.const import DOMAIN

        coord = _make_coordinator(update_interval=300)
        inv: Any = coord.inverter
        inv.get_real_time.return_value = {
            "SoC": 47,
            "batChargePower": 0.0,
            "batDischargePower": 0.571,
        }
        inv.get_current_mode.return_value = None

        entry = MagicMock()
        entry.options = {"battery_capacity_kwh": 10.0}
        dd = FoxESSControlData()
        dd.entries["test-entry"] = FoxESSEntryData()
        coord.hass.data = {DOMAIN: dd}  # type: ignore[assignment]
        coord.hass.config_entries.async_get_entry = MagicMock(  # type: ignore[method-assign]
            return_value=entry
        )

        registered_callbacks: list[Any] = []

        def fake_call_later(_hass: Any, _delay: float, cb: Any) -> MagicMock:
            registered_callbacks.append(cb)
            return MagicMock()

        base = time.monotonic()
        with (
            patch("time.monotonic", return_value=base),
            patch(
                "custom_components.foxess_control.coordinator.async_call_later",
                side_effect=fake_call_later,
            ),
        ):
            data = await coord._async_update_data()
            coord.data = data

        assert registered_callbacks, "Extrapolation should be scheduled"

        original = coord.async_set_updated_data
        set_updated_calls: list[Any] = []

        def spy(d: Any) -> None:
            set_updated_calls.append(d)
            original(d)

        coord.async_set_updated_data = spy  # type: ignore[assignment]

        # Advance 60s — enough for the 0.571kW discharge on 10kWh to
        # change the rounded SoC (0.571/10*100 * 60/3600 ≈ 0.095%)
        with patch("time.monotonic", return_value=base + 60):
            tick_cb = registered_callbacks[-1]
            tick_cb(None)

        assert not set_updated_calls, (
            "Extrapolation tick must NOT call async_set_updated_data "
            "(it starves the REST poll timer). "
            f"Called {len(set_updated_calls)} time(s)."
        )


class TestBmsTemperatureFetch:
    """Verify BMS temperature is fetched even when WS injections are active.

    Production symptom: sensor.foxess_bms_battery_temperature is always
    'unknown'.  Root cause: _fetch_bms_temperature runs only during REST
    polls (_async_update_data), but inject_realtime_data → async_set_updated_data
    continuously resets the poll timer.  With WS messages every ~5 s and a
    300 s poll interval, the REST poll is starved and _fetch_bms_temperature
    never fires after the first refresh (which runs before the web session
    is stored).
    """

    @staticmethod
    def _make_coord_with_domain_data() -> tuple[
        FoxESSDataCoordinator, FoxESSControlData
    ]:
        """Create a coordinator wired to a real FoxESSControlData instance."""
        from custom_components.foxess_control.const import DOMAIN
        from custom_components.foxess_control.domain_data import (
            FoxESSControlData,
            FoxESSEntryData,
        )

        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        coord = _make_coordinator(inverter=inv)

        dd = FoxESSControlData()
        dd.entries["test-entry"] = FoxESSEntryData()
        coord.hass.data = {DOMAIN: dd}  # type: ignore[assignment]

        entry = MagicMock()
        entry.options = {"battery_capacity_kwh": 10.0}
        coord.hass.config_entries.async_get_entry = MagicMock(  # type: ignore[method-assign]
            return_value=entry
        )
        return coord, dd

    @pytest.mark.asyncio
    async def test_fetch_bms_reads_web_session_from_domain_data(self) -> None:
        """_fetch_bms_temperature reads web_session through FoxESSControlData bridge."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(return_value=23.5)
        dd.web_session = web_session
        dd.battery_compound_id = "abc@SN123"

        data: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data)

        assert data.get("bmsBatteryTemperature") == 23.5
        web_session.async_get_battery_temperature.assert_awaited_once_with(
            battery_compound_id="abc@SN123",
        )

    @pytest.mark.asyncio
    async def test_fetch_bms_skips_when_web_session_missing(self) -> None:
        """Without web_session, _fetch_bms_temperature returns early."""
        coord, dd = self._make_coord_with_domain_data()
        dd.battery_compound_id = "abc@SN123"
        # web_session is None (default)

        data: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data)

        assert "bmsBatteryTemperature" not in data

    @pytest.mark.asyncio
    async def test_fetch_bms_logs_when_compound_id_missing(self) -> None:
        """With web_session but no compound_id AND re-discovery fails, no temp."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_discover_battery_id = AsyncMock(return_value=None)
        dd.web_session = web_session
        # compound_id is None (default)

        data: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data)

        assert "bmsBatteryTemperature" not in data

    @pytest.mark.asyncio
    async def test_bms_temperature_available_during_ws_active_polling(self) -> None:
        """BMS temperature must be fetched even when WS is the primary data source.

        This is the core regression test.  Production flow:
        1. First REST poll runs BEFORE web_session is stored → no BMS temp
        2. Web session is stored in FoxESSControlData
        3. WS starts, inject_realtime_data fires every ~5 s
        4. Each injection calls async_set_updated_data which resets the
           REST poll timer → REST poll (and _fetch_bms_temperature) is starved

        The fix must ensure _fetch_bms_temperature runs periodically even
        when WS injections are the primary data path.
        """
        coord, dd = self._make_coord_with_domain_data()

        # --- Step 1: First REST poll (before web_session) ---
        data = await coord._async_update_data()
        coord.data = data
        assert "bmsBatteryTemperature" not in data, (
            "First poll should not have BMS temp (web_session not yet stored)"
        )

        # --- Step 2: Store web_session and compound_id (simulating __init__.py) ---
        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(return_value=31.5)
        dd.web_session = web_session
        dd.battery_compound_id = "bat-id@SN001"

        # Wire hass.async_create_task to schedule real tasks on the
        # running loop (not get_event_loop() which is unreliable under
        # xdist and deprecated in Python 3.12+).
        import asyncio

        pending_tasks: list[asyncio.Task[None]] = []

        def _create_task(coro: Any, **kwargs: Any) -> asyncio.Task[None]:
            task = asyncio.get_running_loop().create_task(coro)
            pending_tasks.append(task)
            return task

        coord.hass.async_create_task = _create_task  # type: ignore[assignment]

        # --- Step 3: Simulate sustained WS injection (replaces REST polls) ---
        coord.inject_realtime_data(
            {"SoC": 50.0, "batChargePower": 0.0, "batDischargePower": 0.0}
        )

        # Drain the background BMS fetch task(s)
        for task in pending_tasks:
            await task

        assert coord.data is not None
        assert coord.data.get("bmsBatteryTemperature") == 31.5, (
            "BMS temperature must appear in coordinator data during WS-active "
            "period.  With the bug, inject_realtime_data → async_set_updated_data "
            "starves the REST poll so _fetch_bms_temperature never runs after "
            "the first refresh."
        )


class TestBmsTemperatureEarlyReturnLogging:
    """Verify that _fetch_bms_temperature logs at WARNING level on early returns.

    Production symptom: sensor.foxess_bms_battery_temperature shows 'unknown'
    with no diagnostic information.  The early-return paths when web_session
    is None or battery_compound_id is missing logged only at DEBUG level,
    making the failure completely invisible in production (C-020, C-026).
    """

    @staticmethod
    def _make_coord_with_domain_data() -> tuple[
        FoxESSDataCoordinator, FoxESSControlData
    ]:
        from custom_components.foxess_control.const import DOMAIN
        from custom_components.foxess_control.domain_data import (
            FoxESSControlData,
            FoxESSEntryData,
        )

        inv = MagicMock(spec=Inverter)
        coord = _make_coordinator(inverter=inv)

        dd = FoxESSControlData()
        dd.entries["test-entry"] = FoxESSEntryData()
        coord.hass.data = {DOMAIN: dd}  # type: ignore[assignment]
        return coord, dd

    @pytest.mark.asyncio
    async def test_warning_logged_when_web_session_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When web_session is None, a WARNING-level log must be emitted.

        This makes the failure visible in production logs without requiring
        DEBUG-level logging to be enabled (C-020, C-026).
        """
        coord, dd = self._make_coord_with_domain_data()
        dd.battery_compound_id = "abc@SN123"
        # web_session remains None (default)

        data: dict[str, Any] = {}
        with caplog.at_level(
            logging.WARNING, logger="custom_components.foxess_control.coordinator"
        ):
            await coord._fetch_bms_temperature(data)

        assert "bmsBatteryTemperature" not in data
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("web session" in msg.lower() for msg in warning_messages), (
            f"Expected a WARNING-level log mentioning 'web session' when "
            f"web_session is None, but got: {warning_messages}"
        )

    @pytest.mark.asyncio
    async def test_warning_logged_when_compound_id_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """compound_id None + re-discovery fails => WARNING emitted."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_discover_battery_id = AsyncMock(return_value=None)
        dd.web_session = web_session
        # compound_id remains None (default)

        data: dict[str, Any] = {}
        with caplog.at_level(
            logging.WARNING, logger="custom_components.foxess_control.coordinator"
        ):
            await coord._fetch_bms_temperature(data)

        assert "bmsBatteryTemperature" not in data
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("compound" in msg.lower() for msg in warning_messages), (
            f"Expected a WARNING-level log mentioning 'compound' when "
            f"battery_compound_id is missing, but got: {warning_messages}"
        )

    @pytest.mark.asyncio
    async def test_warning_logged_when_both_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When both web_session and compound_id are missing, at least one WARNING."""
        coord, dd = self._make_coord_with_domain_data()
        # Both remain None (default)

        data: dict[str, Any] = {}
        with caplog.at_level(
            logging.WARNING, logger="custom_components.foxess_control.coordinator"
        ):
            await coord._fetch_bms_temperature(data)

        assert "bmsBatteryTemperature" not in data
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert len(warning_messages) >= 1, (
            "Expected at least one WARNING-level log when both web_session and "
            "compound_id are missing, but got none"
        )

    @pytest.mark.asyncio
    async def test_info_logged_on_successful_fetch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Happy path: INFO log with temperature value, no warnings."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(return_value=22.0)
        dd.web_session = web_session
        dd.battery_compound_id = "bat@SN001"

        data: dict[str, Any] = {}
        with caplog.at_level(
            logging.INFO, logger="custom_components.foxess_control.coordinator"
        ):
            await coord._fetch_bms_temperature(data)

        assert data.get("bmsBatteryTemperature") == 22.0
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not warning_messages, (
            f"No warnings expected on successful BMS fetch, but got: {warning_messages}"
        )
        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("22.0" in msg for msg in info_messages), (
            f"Expected INFO log with temperature value, but got: {info_messages}"
        )

    @pytest.mark.asyncio
    async def test_info_logged_when_no_value_returned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When web portal returns None, INFO log (not silent)."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(return_value=None)
        dd.web_session = web_session
        dd.battery_compound_id = "bat@SN001"

        data: dict[str, Any] = {}
        with caplog.at_level(
            logging.INFO, logger="custom_components.foxess_control.coordinator"
        ):
            await coord._fetch_bms_temperature(data)

        assert "bmsBatteryTemperature" not in data
        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("no value" in msg.lower() for msg in info_messages), (
            f"Expected INFO log about no value returned, but got: {info_messages}"
        )

    @pytest.mark.asyncio
    async def test_no_value_preserves_previous_temperature(self) -> None:
        """When the web portal returns None, the last known value is kept."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(return_value=None)
        dd.web_session = web_session
        dd.battery_compound_id = "bat@SN001"

        coord.data = {"bmsBatteryTemperature": 16.8}
        data: dict[str, Any] = {}
        await coord._fetch_bms_temperature(data)

        assert data["bmsBatteryTemperature"] == 16.8

    @pytest.mark.asyncio
    async def test_exception_preserves_previous_temperature(self) -> None:
        """When the fetch raises, the last known value is kept."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(
            side_effect=RuntimeError("server error")
        )
        dd.web_session = web_session
        dd.battery_compound_id = "bat@SN001"

        coord.data = {"bmsBatteryTemperature": 15.5}
        data: dict[str, Any] = {}
        await coord._fetch_bms_temperature(data)

        assert data["bmsBatteryTemperature"] == 15.5

    @pytest.mark.asyncio
    async def test_no_value_no_previous_leaves_absent(self) -> None:
        """When there's no previous value and fetch returns None, key stays absent."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(return_value=None)
        dd.web_session = web_session
        dd.battery_compound_id = "bat@SN001"

        coord.data = None  # type: ignore[assignment]
        data: dict[str, Any] = {}
        await coord._fetch_bms_temperature(data)

        assert "bmsBatteryTemperature" not in data

    @pytest.mark.asyncio
    async def test_no_warning_when_domain_data_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When domain data itself is not available, DEBUG is acceptable.

        This is a transient startup condition, not a persistent failure.
        """
        coord = _make_coordinator()
        coord.hass.data = {}  # type: ignore[assignment]

        data: dict[str, Any] = {}
        with caplog.at_level(
            logging.DEBUG, logger="custom_components.foxess_control.coordinator"
        ):
            await coord._fetch_bms_temperature(data)

        assert "bmsBatteryTemperature" not in data
        # Domain data missing is a startup race — DEBUG is fine
        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not warning_messages, (
            f"Domain data missing is transient — no WARNING expected, "
            f"but got: {warning_messages}"
        )


class TestBmsCompoundIdRediscovery:
    """After HA restart, battery_compound_id is lost (in-memory only).

    Production symptom: sensor.foxess_bms_battery_temperature freezes at
    its last-known value and never updates.  All other sensors update
    normally via REST polling.  The one-shot _discover_battery_id() task
    at startup may fail (network, WS timeout) and is never retried.
    """

    @staticmethod
    def _make_coord_with_domain_data() -> tuple[
        FoxESSDataCoordinator, FoxESSControlData
    ]:
        from custom_components.foxess_control.const import DOMAIN
        from custom_components.foxess_control.domain_data import (
            FoxESSControlData,
            FoxESSEntryData,
        )

        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        inv.get_plant_id.return_value = "plant-123"
        coord = _make_coordinator(inverter=inv)

        dd = FoxESSControlData()
        dd.entries["test-entry"] = FoxESSEntryData()
        coord.hass.data = {DOMAIN: dd}  # type: ignore[assignment]

        entry = MagicMock()
        entry.options = {"battery_capacity_kwh": 10.0}
        coord.hass.config_entries.async_get_entry = MagicMock(  # type: ignore[method-assign]
            return_value=entry
        )
        return coord, dd

    @pytest.mark.asyncio
    async def test_rediscovery_attempted_when_compound_id_missing(self) -> None:
        """When web_session exists but compound_id is None, discovery is retried."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(return_value=23.5)
        web_session.async_discover_battery_id = AsyncMock(return_value="bat-id@SN001")
        dd.web_session = web_session
        dd.plant_id = "plant-123"

        data: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data)

        assert dd.battery_compound_id == "bat-id@SN001"
        assert data.get("bmsBatteryTemperature") == 23.5

    @pytest.mark.asyncio
    async def test_temperature_fetched_when_compound_id_available(self) -> None:
        """Control case: BMS temp is fetched when compound_id IS available."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_get_battery_temperature = AsyncMock(return_value=18.0)
        dd.web_session = web_session
        dd.battery_compound_id = "existing-bat@SN001"

        data: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data)

        assert data.get("bmsBatteryTemperature") == 18.0
        web_session.async_get_battery_temperature.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rediscovery_failure_does_not_crash(self) -> None:
        """When rediscovery fails, _fetch_bms_temperature degrades gracefully."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_discover_battery_id = AsyncMock(return_value=None)
        dd.web_session = web_session
        dd.plant_id = "plant-123"

        data: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data)

        assert dd.battery_compound_id is None
        assert "bmsBatteryTemperature" not in data

    @pytest.mark.asyncio
    async def test_rediscovery_uses_plant_id(self) -> None:
        """Re-discovery passes the plant_id to async_discover_battery_id."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_discover_battery_id = AsyncMock(return_value="new-bat@SN002")
        web_session.async_get_battery_temperature = AsyncMock(return_value=20.0)
        dd.web_session = web_session
        dd.plant_id = "my-plant-456"

        data: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data)

        web_session.async_discover_battery_id.assert_awaited_once_with("my-plant-456")

    @pytest.mark.asyncio
    async def test_rediscovery_discovers_plant_id_if_missing(self) -> None:
        """When plant_id is also missing, it's discovered via inverter first."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_discover_battery_id = AsyncMock(return_value="bat@SN003")
        web_session.async_get_battery_temperature = AsyncMock(return_value=21.0)
        dd.web_session = web_session
        dd.plant_id = None

        data: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data)

        assert dd.plant_id == "plant-123"
        assert dd.battery_compound_id == "bat@SN003"

    @pytest.mark.asyncio
    async def test_rediscovery_has_backoff(self) -> None:
        """Re-discovery attempts are throttled to avoid spamming."""
        coord, dd = self._make_coord_with_domain_data()

        web_session = AsyncMock()
        web_session.async_discover_battery_id = AsyncMock(return_value=None)
        dd.web_session = web_session
        dd.plant_id = "plant-123"

        data1: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data1)
        assert web_session.async_discover_battery_id.await_count == 1

        data2: dict[str, Any] = {"SoC": 50.0}
        await coord._fetch_bms_temperature(data2)
        assert web_session.async_discover_battery_id.await_count == 1, (
            "Re-discovery should be throttled — second attempt within "
            "backoff window should not call async_discover_battery_id again"
        )


class TestWsDivergenceFiltering:
    """WebSocket messages with >10x power divergence must be dropped.

    Production symptom: sensor.foxess_discharge_rate intermittently jumps
    from ~5.5kW to ~0.5kW every 5-10 seconds then recovers.  The FoxESS
    WebSocket occasionally sends messages with gridStatus=3 and drastically
    lower power values.  The coordinator already detects the divergence and
    logs a warning, but still applies the anomalous value to sensor state.

    The fix must DROP (not apply) WS messages that trigger the divergence
    warning, preventing the sensor from jumping around.

    Related constraints: C-004 (WS watts/kW), C-005 (stale filtering),
    C-020 (UI must reflect reality).
    """

    @staticmethod
    def _make_coord_with_discharge_data() -> FoxESSDataCoordinator:
        """Create a coordinator simulating an active discharge at ~5.5kW."""
        coord = _make_coordinator()
        coord.data = {
            "SoC": 80.0,
            "batChargePower": 0.0,
            "batDischargePower": 5.52,
            "feedinPower": 5.01,
            "loadsPower": 0.51,
            "pvPower": 0.0,
            "feedin": 100.0,
            "_data_source": "ws",
            "_data_last_update": "2026-04-21T08:38:29+00:00",
        }
        return coord

    def test_anomalous_ws_message_is_dropped(self) -> None:
        """A WS message with >10x lower batDischargePower must not be applied.

        Reproduces the production scenario: discharge at 5.52kW, then a
        message arrives with 0.517kW (gridStatus=3 anomaly).  The sensor
        must remain at 5.52kW.
        """
        coord = self._make_coord_with_discharge_data()

        # Inject the anomalous message (~10x lower discharge power)
        coord.inject_realtime_data(
            {
                "SoC": 80.0,
                "batChargePower": 0.0,
                "batDischargePower": 0.517,
                "feedinPower": 0.051,
                "loadsPower": 0.466,
                "pvPower": 0.0,
            }
        )

        assert coord.data is not None
        assert coord.data["batDischargePower"] == 5.52, (
            f"Anomalous WS message should be dropped, but batDischargePower "
            f"was updated to {coord.data['batDischargePower']}"
        )

    def test_normal_ws_message_is_applied(self) -> None:
        """A WS message with similar power values must be applied normally."""
        coord = self._make_coord_with_discharge_data()

        coord.inject_realtime_data(
            {
                "SoC": 80.0,
                "batChargePower": 0.0,
                "batDischargePower": 5.50,
                "feedinPower": 5.03,
                "loadsPower": 0.47,
                "pvPower": 0.0,
            }
        )

        assert coord.data is not None
        assert coord.data["batDischargePower"] == 5.50, (
            f"Normal WS message should be applied, but batDischargePower "
            f"is {coord.data['batDischargePower']}"
        )

    def test_legitimate_large_change_accepted_when_both_sides_small(self) -> None:
        """When existing power is small (<0.1kW), any WS value is accepted.

        This covers the case where discharge has just started or stopped
        and the previous value was near zero.  The >10x check should not
        block legitimate transitions from near-zero to a real value.
        """
        coord = _make_coordinator()
        coord.data = {
            "SoC": 80.0,
            "batChargePower": 0.0,
            "batDischargePower": 0.05,  # near-zero (just started)
            "feedin": 100.0,
            "_data_source": "ws",
            "_data_last_update": "2026-04-21T08:38:29+00:00",
        }

        # Real discharge ramps up
        coord.inject_realtime_data(
            {
                "SoC": 80.0,
                "batChargePower": 0.0,
                "batDischargePower": 5.50,
            }
        )

        assert coord.data is not None
        assert coord.data["batDischargePower"] == 5.50, (
            f"Transition from near-zero to real power should be accepted, "
            f"but batDischargePower is {coord.data['batDischargePower']}"
        )

    def test_legitimate_stop_accepted_when_ws_value_is_zero(self) -> None:
        """When WS reports zero power, it should be accepted (discharge stopped).

        The divergence filter must not block a genuine stop (ws=0.0).
        The existing check already has a `ws_val > 0` guard that allows this.
        """
        coord = self._make_coord_with_discharge_data()

        coord.inject_realtime_data(
            {
                "SoC": 80.0,
                "batChargePower": 0.0,
                "batDischargePower": 0.0,  # discharge stopped
                "feedinPower": 0.0,
                "loadsPower": 0.51,
                "pvPower": 0.0,
            }
        )

        assert coord.data is not None
        assert coord.data["batDischargePower"] == 0.0, (
            f"Genuine stop (ws=0.0) should be accepted, "
            f"but batDischargePower is {coord.data['batDischargePower']}"
        )

    def test_charge_anomaly_also_dropped(self) -> None:
        """Same filter applies to batChargePower, not just discharge."""
        coord = _make_coordinator()
        coord.data = {
            "SoC": 30.0,
            "batChargePower": 3.80,
            "batDischargePower": 0.0,
            "feedin": 100.0,
            "_data_source": "ws",
            "_data_last_update": "2026-04-21T08:38:29+00:00",
        }

        coord.inject_realtime_data(
            {
                "SoC": 30.0,
                "batChargePower": 0.35,  # >10x lower — anomalous
                "batDischargePower": 0.0,
            }
        )

        assert coord.data is not None
        assert coord.data["batChargePower"] == 3.80, (
            f"Anomalous charge WS message should be dropped, but batChargePower "
            f"was updated to {coord.data['batChargePower']}"
        )

    def test_divergence_warning_still_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The divergence warning must still be logged even when dropping."""
        coord = self._make_coord_with_discharge_data()

        with caplog.at_level(
            logging.WARNING, logger="custom_components.foxess_control.coordinator"
        ):
            coord.inject_realtime_data(
                {
                    "SoC": 80.0,
                    "batChargePower": 0.0,
                    "batDischargePower": 0.517,
                    "feedinPower": 0.051,
                    "loadsPower": 0.466,
                    "pvPower": 0.0,
                }
            )

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("diverges" in msg for msg in warning_messages), (
            "Expected a WARNING-level log about divergence, "
            f"but got: {warning_messages}"
        )
