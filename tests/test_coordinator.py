"""Tests for the FoxESSDataCoordinator."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.foxess_control.const import POLLED_VARIABLES
from custom_components.foxess_control.coordinator import FoxESSDataCoordinator
from custom_components.foxess_control.foxess.inverter import Inverter, WorkMode


def _make_coordinator(
    inverter: Inverter | None = None,
    update_interval: int = 300,
) -> FoxESSDataCoordinator:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
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
        coord.hass.data = {DOMAIN: {"test-entry": {}}}  # type: ignore[assignment]
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
        """On tick change, round(interpolated, 1) must not exceed new tick."""
        coord = self._make_coord()

        coord.inject_realtime_data(self._ws_msg(97))
        coord._soc_interpolated = 96.5
        coord._soc_last_reported = 97.0

        coord.inject_realtime_data(self._ws_msg(96))

        displayed = round(coord._soc_interpolated, 1)
        assert displayed <= 96.9, (
            f"Displayed value {displayed} should not exceed tick 96 "
            f"(raw: {coord._soc_interpolated})"
        )
        assert coord._soc_interpolated >= 96.0

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
