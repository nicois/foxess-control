"""Tests for service handlers and integration setup/unload."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.foxess_control import (
    _get_inverter,
    _get_min_soc_on_grid,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.foxess_control.const import (
    CONF_API_KEY,
    CONF_DEVICE_SERIAL,
    CONF_MIN_SOC_ON_GRID,
    DEFAULT_MIN_SOC_ON_GRID,
    DOMAIN,
)
from custom_components.foxess_control.foxess.inverter import Inverter


def _make_hass(
    entry_id: str = "entry1",
    inverter: Inverter | None = None,
    min_soc_on_grid: int = DEFAULT_MIN_SOC_ON_GRID,
) -> MagicMock:
    """Create a mock hass with DOMAIN data populated."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

    if inverter is None:
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

    hass.data = {DOMAIN: {entry_id: {"inverter": inverter}}}

    # Mock config entry for options lookup
    mock_entry = MagicMock()
    mock_entry.options = {CONF_MIN_SOC_ON_GRID: min_soc_on_grid}
    hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

    return hass


def _make_call(data: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock ServiceCall."""
    call_mock = MagicMock()
    call_mock.data = data or {}
    return call_mock


class TestGetInverter:
    """Tests for _get_inverter helper."""

    def test_returns_inverter(self) -> None:
        inv = MagicMock(spec=Inverter)
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry1": {"inverter": inv}}}
        assert _get_inverter(hass) is inv

    def test_raises_when_no_entries(self) -> None:
        hass = MagicMock()
        hass.data = {DOMAIN: {}}
        with pytest.raises(ServiceValidationError, match="No FoxESS"):
            _get_inverter(hass)


class TestGetMinSocOnGrid:
    """Tests for _get_min_soc_on_grid helper."""

    def test_returns_configured_value(self) -> None:
        hass = _make_hass(min_soc_on_grid=25)
        assert _get_min_soc_on_grid(hass) == 25

    def test_returns_default_when_entry_missing(self) -> None:
        hass = _make_hass()
        hass.config_entries.async_get_entry = MagicMock(return_value=None)
        assert _get_min_soc_on_grid(hass) == DEFAULT_MIN_SOC_ON_GRID


class TestSetupEntry:
    """Tests for async_setup_entry."""

    @pytest.mark.asyncio
    async def test_setup_registers_services(self) -> None:
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(return_value=10500)
        hass.data = {}
        hass.data.setdefault(DOMAIN, {})

        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.data = {CONF_API_KEY: "key", CONF_DEVICE_SERIAL: "SN001"}

        with (
            patch("custom_components.foxess_control.FoxESSClient"),
            patch("custom_components.foxess_control.Inverter") as mock_inv_cls,
        ):
            mock_inv = MagicMock()
            mock_inv.max_power_w = 10500
            mock_inv_cls.return_value = mock_inv

            assert await async_setup_entry(hass, entry) is True

        assert DOMAIN in hass.data
        assert hass.services.async_register.call_count == 4

    @pytest.mark.asyncio
    async def test_second_entry_does_not_reregister_services(self) -> None:
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(return_value=10500)
        hass.data = {DOMAIN: {"existing": {"inverter": MagicMock()}}}

        entry = MagicMock()
        entry.entry_id = "entry2"
        entry.data = {CONF_API_KEY: "key", CONF_DEVICE_SERIAL: "SN002"}

        with (
            patch("custom_components.foxess_control.FoxESSClient"),
            patch("custom_components.foxess_control.Inverter") as mock_inv_cls,
        ):
            mock_inv = MagicMock()
            mock_inv.max_power_w = 10500
            mock_inv_cls.return_value = mock_inv

            assert await async_setup_entry(hass, entry) is True

        # Services should NOT be registered again
        hass.services.async_register.assert_not_called()


class TestUnloadEntry:
    """Tests for async_unload_entry."""

    @pytest.mark.asyncio
    async def test_unload_last_entry_removes_services(self) -> None:
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry1": {"inverter": MagicMock()}}}

        entry = MagicMock()
        entry.entry_id = "entry1"

        result = await async_unload_entry(hass, entry)

        assert result is True
        assert DOMAIN not in hass.data
        assert hass.services.async_remove.call_count == 4

    @pytest.mark.asyncio
    async def test_unload_non_last_entry_keeps_services(self) -> None:
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                "entry1": {"inverter": MagicMock()},
                "entry2": {"inverter": MagicMock()},
            }
        }

        entry = MagicMock()
        entry.entry_id = "entry1"

        result = await async_unload_entry(hass, entry)

        assert result is True
        assert DOMAIN in hass.data
        hass.services.async_remove.assert_not_called()


class TestHandleClearOverrides:
    """Tests for handle_clear_overrides service handler."""

    @pytest.mark.asyncio
    async def test_clear_all_calls_self_use(self) -> None:
        inv = MagicMock(spec=Inverter)
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({}))

        inv.self_use.assert_called_once_with(DEFAULT_MIN_SOC_ON_GRID)

    @pytest.mark.asyncio
    async def test_clear_specific_mode_keeps_others(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceCharge",
                    "startHour": 8,
                    "startMinute": 0,
                    "endHour": 10,
                    "endMinute": 0,
                    "minSocOnGrid": 15,
                    "fdSoc": 100,
                    "fdPwr": 10500,
                },
                {
                    "enable": 1,
                    "workMode": "ForceDischarge",
                    "startHour": 17,
                    "startMinute": 0,
                    "endHour": 20,
                    "endMinute": 0,
                    "minSocOnGrid": 15,
                    "fdSoc": 11,
                    "fdPwr": 10500,
                },
            ],
        }
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({"mode": "ForceCharge"}))

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceDischarge"

    @pytest.mark.asyncio
    async def test_clear_mode_falls_back_to_self_use(self) -> None:
        """When all groups are filtered out, fall back to self_use."""
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceCharge",
                    "startHour": 8,
                    "startMinute": 0,
                    "endHour": 10,
                    "endMinute": 0,
                    "minSocOnGrid": 15,
                    "fdSoc": 100,
                    "fdPwr": 10500,
                },
            ],
        }
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({"mode": "ForceCharge"}))

        inv.self_use.assert_called_once()
        inv.set_schedule.assert_not_called()


class TestHandleFeedin:
    """Tests for handle_feedin service handler."""

    @pytest.mark.asyncio
    async def test_feedin_calls_set_schedule(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[1].args[2]

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=2)}))

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "Feedin"
        assert groups[0]["startHour"] == 14
        assert groups[0]["endHour"] == 16
        assert groups[0]["fdSoc"] == 11

    @pytest.mark.asyncio
    async def test_feedin_with_power(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[1].args[2]

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            await handler(
                _make_call(
                    {
                        "duration": datetime.timedelta(hours=1),
                        "power": 5000,
                    }
                )
            )

        groups = inv.set_schedule.call_args.args[0]
        assert groups[0]["fdPwr"] == 5000


class TestHandleForceCharge:
    """Tests for handle_force_charge service handler."""

    @pytest.mark.asyncio
    async def test_force_charge_calls_set_schedule(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[2].args[2]

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=1)}))

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceCharge"
        assert groups[0]["startHour"] == 14
        assert groups[0]["endHour"] == 15
        assert groups[0]["fdSoc"] == 100

    @pytest.mark.asyncio
    async def test_force_charge_with_power(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[2].args[2]

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            await handler(
                _make_call(
                    {
                        "duration": datetime.timedelta(hours=1),
                        "power": 6000,
                    }
                )
            )

        groups = inv.set_schedule.call_args.args[0]
        assert groups[0]["fdPwr"] == 6000


class TestHandleForceDischarge:
    """Tests for handle_force_discharge service handler."""

    @pytest.mark.asyncio
    async def test_force_discharge_calls_set_schedule(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[3].args[2]

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=2)}))

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceDischarge"
        assert groups[0]["startHour"] == 17
        assert groups[0]["endHour"] == 19
        assert groups[0]["fdSoc"] == 11

    @pytest.mark.asyncio
    async def test_force_discharge_with_start_time(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[3].args[2]

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            await handler(
                _make_call(
                    {
                        "duration": datetime.timedelta(hours=2),
                        "start_time": datetime.time(18, 0),
                    }
                )
            )

        groups = inv.set_schedule.call_args.args[0]
        assert groups[0]["startHour"] == 18
        assert groups[0]["endHour"] == 20
