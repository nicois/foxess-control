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
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DOMAIN,
)
from custom_components.foxess_control.foxess.inverter import Inverter


def _make_hass(
    entry_id: str = "entry1",
    inverter: Inverter | None = None,
    min_soc_on_grid: int = DEFAULT_MIN_SOC_ON_GRID,
    battery_soc_entity: str = "",
    battery_capacity_kwh: float = 0.0,
    min_power_change: int = DEFAULT_MIN_POWER_CHANGE,
) -> MagicMock:
    """Create a mock hass with DOMAIN data populated."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

    if inverter is None:
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

    hass.data = {
        DOMAIN: {
            entry_id: {"inverter": inverter},
            "_smart_discharge_unsubs": [],
            "_smart_charge_unsubs": [],
        }
    }

    # Mock config entry for options lookup
    mock_entry = MagicMock()
    mock_entry.options = {
        CONF_MIN_SOC_ON_GRID: min_soc_on_grid,
        CONF_BATTERY_SOC_ENTITY: battery_soc_entity,
        CONF_BATTERY_CAPACITY_KWH: battery_capacity_kwh,
        CONF_MIN_POWER_CHANGE: min_power_change,
    }
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
        hass.config_entries.async_forward_entry_setups = AsyncMock()
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
        assert hass.services.async_register.call_count == 6
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_entry_does_not_reregister_services(self) -> None:
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(return_value=10500)
        hass.config_entries.async_forward_entry_setups = AsyncMock()
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
        hass.config_entries.async_unload_platforms = AsyncMock()
        hass.data = {DOMAIN: {"entry1": {"inverter": MagicMock()}}}

        entry = MagicMock()
        entry.entry_id = "entry1"

        result = await async_unload_entry(hass, entry)

        assert result is True
        assert DOMAIN not in hass.data
        assert hass.services.async_remove.call_count == 6
        hass.config_entries.async_unload_platforms.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unload_non_last_entry_keeps_services(self) -> None:
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock()
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

    @pytest.mark.asyncio
    async def test_clear_mode_preserves_auto_disabled_groups(self) -> None:
        """Groups disabled by the API after their window are kept and re-enabled."""
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 0,
                    "workMode": "ForceCharge",
                    "startHour": 11,
                    "startMinute": 0,
                    "endHour": 14,
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

        await handler(_make_call({"mode": "ForceDischarge"}))

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceCharge"
        assert groups[0]["enable"] == 1

    @pytest.mark.asyncio
    async def test_clear_all_cancels_smart_charge_listeners(self) -> None:
        inv = MagicMock(spec=Inverter)
        hass = _make_hass(inverter=inv)

        unsub1 = MagicMock()
        unsub2 = MagicMock()
        hass.data[DOMAIN]["_smart_charge_unsubs"] = [unsub1, unsub2]
        hass.data[DOMAIN]["_smart_charge_state"] = {"target_soc": 80}

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({}))

        unsub1.assert_called_once()
        unsub2.assert_called_once()
        assert hass.data[DOMAIN]["_smart_charge_unsubs"] == []
        assert "_smart_charge_state" not in hass.data[DOMAIN]

    @pytest.mark.asyncio
    async def test_clear_all_cancels_smart_discharge_listeners(self) -> None:
        inv = MagicMock(spec=Inverter)
        hass = _make_hass(inverter=inv)

        unsub = MagicMock()
        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [unsub]
        hass.data[DOMAIN]["_smart_discharge_state"] = {"min_soc": 30}

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({}))

        unsub.assert_called_once()
        assert hass.data[DOMAIN]["_smart_discharge_unsubs"] == []
        assert "_smart_discharge_state" not in hass.data[DOMAIN]

    @pytest.mark.asyncio
    async def test_clear_force_charge_cancels_smart_charge_only(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        charge_unsub = MagicMock()
        discharge_unsub = MagicMock()
        hass.data[DOMAIN]["_smart_charge_unsubs"] = [charge_unsub]
        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [discharge_unsub]

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({"mode": "ForceCharge"}))

        charge_unsub.assert_called_once()
        discharge_unsub.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_force_discharge_cancels_smart_discharge_only(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        charge_unsub = MagicMock()
        discharge_unsub = MagicMock()
        hass.data[DOMAIN]["_smart_charge_unsubs"] = [charge_unsub]
        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [discharge_unsub]

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({"mode": "ForceDischarge"}))

        discharge_unsub.assert_called_once()
        charge_unsub.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_feedin_does_not_cancel_smart_listeners(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        charge_unsub = MagicMock()
        discharge_unsub = MagicMock()
        hass.data[DOMAIN]["_smart_charge_unsubs"] = [charge_unsub]
        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [discharge_unsub]

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({"mode": "Feedin"}))

        charge_unsub.assert_not_called()
        discharge_unsub.assert_not_called()


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

    @pytest.mark.asyncio
    async def test_force_charge_cancels_smart_charge(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        unsub = MagicMock()
        hass.data[DOMAIN]["_smart_charge_unsubs"] = [unsub]
        hass.data[DOMAIN]["_smart_charge_state"] = {"target_soc": 80}

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[2].args[2]

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=1)}))

        unsub.assert_called_once()
        assert hass.data[DOMAIN]["_smart_charge_unsubs"] == []
        assert "_smart_charge_state" not in hass.data[DOMAIN]


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

    @pytest.mark.asyncio
    async def test_force_discharge_cancels_smart_discharge(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        unsub = MagicMock()
        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [unsub]
        hass.data[DOMAIN]["_smart_discharge_state"] = {"min_soc": 30}

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[3].args[2]

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=2)}))

        unsub.assert_called_once()
        assert hass.data[DOMAIN]["_smart_discharge_unsubs"] == []
        assert "_smart_discharge_state" not in hass.data[DOMAIN]


class TestHandleSmartDischarge:
    """Tests for handle_smart_discharge service handler."""

    @pytest.mark.asyncio
    async def test_smart_discharge_sets_schedule(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, battery_soc_entity="sensor.battery_soc")

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 30,
                    }
                )
            )

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceDischarge"
        assert groups[0]["startHour"] == 17
        assert groups[0]["endHour"] == 20
        assert groups[0]["fdSoc"] == 11

        # Verify state dict is stored for binary sensor
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["min_soc"] == 30
        assert state["last_power_w"] == 10500
        assert state["soc_entity"] == "sensor.battery_soc"
        assert state["end"] == datetime.datetime(2026, 4, 7, 20, 0, 0)

    @pytest.mark.asyncio
    async def test_smart_discharge_registers_listeners(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, battery_soc_entity="sensor.battery_soc")

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        mock_state_unsub = MagicMock()
        mock_timer_unsub = MagicMock()

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=mock_state_unsub,
            ) as mock_track_state,
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=mock_timer_unsub,
            ) as mock_track_time,
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 30,
                    }
                )
            )

        mock_track_state.assert_called_once()
        assert mock_track_state.call_args.args[1] == ["sensor.battery_soc"]

        mock_track_time.assert_called_once()

        unsubs = hass.data[DOMAIN]["_smart_discharge_unsubs"]
        assert len(unsubs) == 2

    @pytest.mark.asyncio
    async def test_smart_discharge_missing_entity_raises(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(inverter=inv, battery_soc_entity="")

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="Battery SoC entity"),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 30,
                    }
                )
            )

    @pytest.mark.asyncio
    async def test_smart_discharge_cancels_previous(self) -> None:
        """A new smart discharge cancels any existing one."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, battery_soc_entity="sensor.battery_soc")

        prev_unsub = MagicMock()
        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [prev_unsub]

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 30,
                    }
                )
            )

        prev_unsub.assert_called_once()

    @pytest.mark.asyncio
    async def test_soc_threshold_triggers_self_use(self) -> None:
        """SoC at threshold schedules self_use and cancels listeners."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, battery_soc_entity="sensor.battery_soc")

        captured_callback = None

        def capture_state_callback(
            _hass: Any, _entities: Any, callback: Any
        ) -> MagicMock:
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                side_effect=capture_state_callback,
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 30,
                    }
                )
            )

        assert captured_callback is not None

        # Simulate SoC dropping to threshold
        event = MagicMock()
        new_state = MagicMock()
        new_state.state = "30"
        event.data = {"new_state": new_state}

        captured_callback(event)

        # The callback schedules self_use via async_create_task
        hass.async_create_task.assert_called_once()
        # Listeners should be cancelled
        assert hass.data[DOMAIN]["_smart_discharge_unsubs"] == []

    @pytest.mark.asyncio
    async def test_soc_above_threshold_no_op(self) -> None:
        """When SoC is above threshold, nothing happens."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, battery_soc_entity="sensor.battery_soc")

        captured_callback = None

        def capture_state_callback(
            _hass: Any, _entities: Any, callback: Any
        ) -> MagicMock:
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                side_effect=capture_state_callback,
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 30,
                    }
                )
            )

        assert captured_callback is not None

        event = MagicMock()
        new_state = MagicMock()
        new_state.state = "50"
        event.data = {"new_state": new_state}

        captured_callback(event)

        inv.self_use.assert_not_called()


class TestHandleSmartCharge:
    """Tests for handle_smart_charge service handler."""

    @pytest.mark.asyncio
    async def test_smart_charge_sets_schedule(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=10.0,
        )

        # Mock current SoC state
        soc_state = MagicMock()
        soc_state.state = "20"
        hass.states.get = MagicMock(return_value=soc_state)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceCharge"
        assert groups[0]["startHour"] == 2
        assert groups[0]["endHour"] == 6
        assert groups[0]["fdSoc"] == 100

    @pytest.mark.asyncio
    async def test_smart_charge_registers_three_listeners(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=10.0,
        )

        soc_state = MagicMock()
        soc_state.state = "20"
        hass.states.get = MagicMock(return_value=soc_state)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        mock_state_unsub = MagicMock()
        mock_timer_unsub = MagicMock()
        mock_interval_unsub = MagicMock()

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=mock_state_unsub,
            ) as mock_track_state,
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=mock_timer_unsub,
            ) as mock_track_time,
            patch(
                "custom_components.foxess_control.async_track_time_interval",
                return_value=mock_interval_unsub,
            ) as mock_track_interval,
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

        mock_track_state.assert_called_once()
        mock_track_time.assert_called_once()
        mock_track_interval.assert_called_once()

        unsubs = hass.data[DOMAIN]["_smart_charge_unsubs"]
        assert len(unsubs) == 3

    @pytest.mark.asyncio
    async def test_smart_charge_missing_entity_raises(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="",
            battery_capacity_kwh=10.0,
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="Battery SoC entity"),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

    @pytest.mark.asyncio
    async def test_smart_charge_missing_capacity_raises(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=0.0,
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="Battery capacity"),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

    @pytest.mark.asyncio
    async def test_smart_charge_soc_at_target_raises(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=10.0,
        )

        soc_state = MagicMock()
        soc_state.state = "80"
        hass.states.get = MagicMock(return_value=soc_state)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="already at or above"),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

    @pytest.mark.asyncio
    async def test_smart_charge_cancels_previous(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=10.0,
        )

        soc_state = MagicMock()
        soc_state.state = "20"
        hass.states.get = MagicMock(return_value=soc_state)

        prev_unsub = MagicMock()
        hass.data[DOMAIN]["_smart_charge_unsubs"] = [prev_unsub]

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

        prev_unsub.assert_called_once()

    @pytest.mark.asyncio
    async def test_soc_at_target_triggers_self_use(self) -> None:
        """SoC reaching target schedules self_use and cancels listeners."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=10.0,
        )

        soc_state = MagicMock()
        soc_state.state = "20"
        hass.states.get = MagicMock(return_value=soc_state)

        captured_callback = None

        def capture_state_callback(
            _hass: Any, _entities: Any, callback: Any
        ) -> MagicMock:
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                side_effect=capture_state_callback,
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

        assert captured_callback is not None

        event = MagicMock()
        new_state = MagicMock()
        new_state.state = "80"
        event.data = {"new_state": new_state}

        captured_callback(event)

        hass.async_create_task.assert_called_once()
        assert hass.data[DOMAIN]["_smart_charge_unsubs"] == []

    @pytest.mark.asyncio
    async def test_periodic_adjustment_updates_schedule(self) -> None:
        """Periodic callback recalculates power and updates schedule."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=10.0,
            min_power_change=100,
        )

        soc_state = MagicMock()
        soc_state.state = "20"
        hass.states.get = MagicMock(return_value=soc_state)

        captured_interval_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval_callback
            captured_interval_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

        assert captured_interval_callback is not None

        # Reset set_schedule call count from initial setup
        inv.set_schedule.reset_mock()

        # Simulate SoC advancing to 60% — power drops from 1500W to 1000W
        soc_state_50 = MagicMock()
        soc_state_50.state = "60"
        hass.states.get = MagicMock(return_value=soc_state_50)

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 4, 0, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 4, 0, 0))

        inv.set_schedule.assert_called_once()

    @pytest.mark.asyncio
    async def test_periodic_adjustment_skips_below_threshold(self) -> None:
        """Power change below min_power_change is skipped."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=10.0,
            min_power_change=5000,
        )

        soc_state = MagicMock()
        soc_state.state = "20"
        hass.states.get = MagicMock(return_value=soc_state)

        captured_interval_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval_callback
            captured_interval_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

        assert captured_interval_callback is not None
        inv.set_schedule.reset_mock()

        # SoC barely changed — power delta should be below threshold
        soc_state_21 = MagicMock()
        soc_state_21.state = "21"
        hass.states.get = MagicMock(return_value=soc_state_21)

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 2, 5, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 2, 5, 0))

        inv.set_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_periodic_adjustment_skips_unavailable_soc(self) -> None:
        """Periodic callback skips when SoC is unavailable."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_soc_entity="sensor.battery_soc",
            battery_capacity_kwh=10.0,
        )

        soc_state = MagicMock()
        soc_state.state = "20"
        hass.states.get = MagicMock(return_value=soc_state)

        captured_interval_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval_callback
            captured_interval_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.async_track_state_change_event",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(2, 0),
                        "end_time": datetime.time(6, 0),
                        "target_soc": 80,
                    }
                )
            )

        assert captured_interval_callback is not None
        inv.set_schedule.reset_mock()

        # SoC unavailable
        unavail_state = MagicMock()
        unavail_state.state = "unavailable"
        hass.states.get = MagicMock(return_value=unavail_state)

        with patch(
            "custom_components.foxess_control.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 3, 0, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 3, 0, 0))

        inv.set_schedule.assert_not_called()
