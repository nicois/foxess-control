"""Tests for the FoxESSDataCoordinator."""

from __future__ import annotations

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
