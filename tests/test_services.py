"""Tests for service handlers and integration setup/unload."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.foxess_control import (
    _get_inverter,
    _get_min_soc_on_grid,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.foxess_control.const import (
    CONF_API_KEY,
    CONF_API_MIN_SOC,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_DEVICE_SERIAL,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    CONF_SMART_HEADROOM,
    DEFAULT_API_MIN_SOC,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_SMART_HEADROOM,
    DOMAIN,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
)
from custom_components.foxess_control.foxess.inverter import Inverter


def _make_hass(
    entry_id: str = "entry1",
    inverter: Inverter | None = None,
    min_soc_on_grid: int = DEFAULT_MIN_SOC_ON_GRID,
    battery_capacity_kwh: float = 0.0,
    min_power_change: int = DEFAULT_MIN_POWER_CHANGE,
    api_min_soc: int = DEFAULT_API_MIN_SOC,
    smart_headroom: int = DEFAULT_SMART_HEADROOM,
    coordinator_data: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock hass with DOMAIN data populated.

    *coordinator_data* populates the coordinator mock's ``.data`` attribute.
    Pass ``None`` (default) to create a coordinator with no data, or a dict
    like ``{"SoC": 50.0}`` to simulate polled values.
    """
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, **kwargs: asyncio.ensure_future(coro)
    )

    if inverter is None:
        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500

    mock_store = MagicMock()
    mock_store.async_load = AsyncMock(return_value={})
    mock_store.async_save = AsyncMock()

    mock_coordinator = MagicMock()
    mock_coordinator.data = coordinator_data
    mock_coordinator.update_interval = datetime.timedelta(seconds=300)

    dd = FoxESSControlData()
    dd.entries[entry_id] = FoxESSEntryData(
        coordinator=mock_coordinator, inverter=inverter
    )
    dd.smart_discharge_unsubs = []
    dd.smart_charge_unsubs = []
    dd.store = mock_store
    hass.data = {DOMAIN: dd}

    # Mock config entry for options lookup
    mock_entry = MagicMock()
    mock_entry.options = {
        CONF_MIN_SOC_ON_GRID: min_soc_on_grid,
        CONF_BATTERY_CAPACITY_KWH: battery_capacity_kwh,
        CONF_MIN_POWER_CHANGE: min_power_change,
        CONF_API_MIN_SOC: api_min_soc,
        CONF_SMART_HEADROOM: smart_headroom,
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
        dd = FoxESSControlData()
        dd.entries["entry1"] = FoxESSEntryData(inverter=inv)
        hass = MagicMock()
        hass.data = {DOMAIN: dd}
        assert _get_inverter(hass) is inv

    def test_raises_when_no_entries(self) -> None:
        hass = MagicMock()
        hass.data = {DOMAIN: FoxESSControlData()}
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
    async def test_async_setup_registers_services(self) -> None:
        """Services are registered in async_setup (before any entry loads)."""
        hass = MagicMock()
        assert await async_setup(hass, {}) is True
        assert hass.services.async_register.call_count == 6

    @pytest.mark.asyncio
    async def test_setup_entry_does_not_register_services(self) -> None:
        """async_setup_entry should NOT register services (async_setup does)."""
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(return_value=10500)
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        mock_store = MagicMock()
        mock_store.async_load = AsyncMock(return_value={})
        mock_store.async_save = AsyncMock()

        hass.data = {DOMAIN: {"_store": mock_store}}

        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.data = {CONF_API_KEY: "key", CONF_DEVICE_SERIAL: "SN001"}
        entry.options = {}

        with (
            patch("custom_components.foxess_control.FoxESSClient"),
            patch("custom_components.foxess_control.Inverter") as mock_inv_cls,
            patch(
                "custom_components.foxess_control._recover_sessions",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.foxess_control.FoxESSDataCoordinator",
            ) as mock_coord_cls,
            patch(
                "custom_components.foxess_control._register_card_frontend",
                new_callable=AsyncMock,
            ),
        ):
            mock_inv = MagicMock()
            mock_inv.max_power_w = 10500
            mock_inv_cls.return_value = mock_inv
            mock_coord = MagicMock()
            mock_coord.async_config_entry_first_refresh = AsyncMock()
            mock_coord_cls.return_value = mock_coord

            assert await async_setup_entry(hass, entry) is True

        assert DOMAIN in hass.data
        hass.services.async_register.assert_not_called()
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_entry_does_not_reregister_services(self) -> None:
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(return_value=10500)
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        mock_store = MagicMock()
        mock_store.async_load = AsyncMock(return_value={})
        mock_store.async_save = AsyncMock()
        dd = FoxESSControlData()
        dd.entries["existing"] = FoxESSEntryData(inverter=MagicMock())
        dd.store = mock_store
        hass.data = {DOMAIN: dd}

        entry = MagicMock()
        entry.entry_id = "entry2"
        entry.data = {CONF_API_KEY: "key", CONF_DEVICE_SERIAL: "SN002"}
        entry.options = {}

        with (
            patch("custom_components.foxess_control.FoxESSClient"),
            patch("custom_components.foxess_control.Inverter") as mock_inv_cls,
            patch(
                "custom_components.foxess_control._recover_sessions",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.foxess_control.FoxESSDataCoordinator",
            ) as mock_coord_cls,
        ):
            mock_inv = MagicMock()
            mock_inv.max_power_w = 10500
            mock_inv_cls.return_value = mock_inv
            mock_coord = MagicMock()
            mock_coord.async_config_entry_first_refresh = AsyncMock()
            mock_coord_cls.return_value = mock_coord

            assert await async_setup_entry(hass, entry) is True

        # Services should NOT be registered again
        hass.services.async_register.assert_not_called()


class TestUnloadEntry:
    """Tests for async_unload_entry."""

    @pytest.mark.asyncio
    async def test_unload_last_entry_cleans_domain_data(self) -> None:
        """Unloading the last entry cleans domain data but leaves services
        (services are registered in async_setup and managed by HA core).
        """
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock()
        dd = FoxESSControlData()
        dd.entries["entry1"] = FoxESSEntryData(inverter=MagicMock())
        hass.data = {DOMAIN: dd}

        entry = MagicMock()
        entry.entry_id = "entry1"

        result = await async_unload_entry(hass, entry)

        assert result is True
        assert DOMAIN not in hass.data
        hass.services.async_remove.assert_not_called()
        hass.config_entries.async_unload_platforms.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unload_non_last_entry_keeps_services(self) -> None:
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock()
        dd = FoxESSControlData()
        dd.entries["entry1"] = FoxESSEntryData(inverter=MagicMock())
        dd.entries["entry2"] = FoxESSEntryData(inverter=MagicMock())
        hass.data = {DOMAIN: dd}

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
        assert hass.data[DOMAIN].get("_smart_charge_state") is None

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
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None

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
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
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
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
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
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
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
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
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
    async def test_force_charge_with_start_time(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[2].args[2]

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
        ):
            await handler(
                _make_call(
                    {
                        "duration": datetime.timedelta(hours=1),
                        "start_time": datetime.time(20, 0),
                    }
                )
            )

        groups = inv.set_schedule.call_args.args[0]
        assert groups[0]["startHour"] == 20
        assert groups[0]["endHour"] == 21

    @pytest.mark.asyncio
    async def test_force_charge_replace_conflicts_removes_overlap(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceDischarge",
                    "startHour": 14,
                    "startMinute": 0,
                    "endHour": 16,
                    "endMinute": 0,
                    "fdSoc": 11,
                    "minSocOnGrid": 11,
                    "fdPwr": 10500,
                },
            ],
        }
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[2].args[2]

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            await handler(
                _make_call(
                    {
                        "duration": datetime.timedelta(hours=1),
                        "replace_conflicts": True,
                    }
                )
            )

        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceCharge"

    @pytest.mark.asyncio
    async def test_force_charge_rejects_overlap_without_replace(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceDischarge",
                    "startHour": 14,
                    "startMinute": 0,
                    "endHour": 16,
                    "endMinute": 0,
                    "fdSoc": 11,
                    "minSocOnGrid": 11,
                    "fdPwr": 10500,
                },
            ],
        }
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[2].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="conflicts with"),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=1)}))

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
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=1)}))

        unsub.assert_called_once()
        assert hass.data[DOMAIN]["_smart_charge_unsubs"] == []
        assert hass.data[DOMAIN].get("_smart_charge_state") is None

    @pytest.mark.asyncio
    async def test_force_charge_cancels_smart_discharge(self) -> None:
        """force_charge must cancel an active smart_discharge session."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        unsub = MagicMock()
        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [unsub]
        hass.data[DOMAIN]["_smart_discharge_state"] = {"min_soc": 20}

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[2].args[2]

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 14, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=1)}))

        unsub.assert_called_once()
        assert hass.data[DOMAIN]["_smart_discharge_unsubs"] == []
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None


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
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
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
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
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
    async def test_force_discharge_with_power(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[3].args[2]

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
        ):
            await handler(
                _make_call(
                    {
                        "duration": datetime.timedelta(hours=2),
                        "power": 5000,
                    }
                )
            )

        groups = inv.set_schedule.call_args.args[0]
        assert groups[0]["fdPwr"] == 5000

    @pytest.mark.asyncio
    async def test_force_discharge_replace_conflicts_removes_overlap(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceCharge",
                    "startHour": 17,
                    "startMinute": 0,
                    "endHour": 19,
                    "endMinute": 0,
                    "fdSoc": 100,
                    "minSocOnGrid": 11,
                    "fdPwr": 10500,
                },
            ],
        }
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[3].args[2]

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
        ):
            await handler(
                _make_call(
                    {
                        "duration": datetime.timedelta(hours=2),
                        "replace_conflicts": True,
                    }
                )
            )

        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceDischarge"

    @pytest.mark.asyncio
    async def test_force_discharge_rejects_overlap_without_replace(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceCharge",
                    "startHour": 17,
                    "startMinute": 0,
                    "endHour": 19,
                    "endMinute": 0,
                    "fdSoc": 100,
                    "minSocOnGrid": 11,
                    "fdPwr": 10500,
                },
            ],
        }
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[3].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="conflicts with"),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=2)}))

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
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=2)}))

        unsub.assert_called_once()
        assert hass.data[DOMAIN]["_smart_discharge_unsubs"] == []
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None

    @pytest.mark.asyncio
    async def test_force_discharge_uses_custom_api_min_soc(self) -> None:
        """fdSoc uses the configured api_min_soc instead of hardcoded 11."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, api_min_soc=8)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[3].args[2]

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=2)}))

        groups = inv.set_schedule.call_args.args[0]
        assert groups[0]["fdSoc"] == 8

    @pytest.mark.asyncio
    async def test_force_discharge_cancels_smart_charge(self) -> None:
        """force_discharge must cancel an active smart_charge session."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)

        unsub = MagicMock()
        hass.data[DOMAIN]["_smart_charge_unsubs"] = [unsub]
        hass.data[DOMAIN]["_smart_charge_state"] = {"target_soc": 80}

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[3].args[2]

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
        ):
            await handler(_make_call({"duration": datetime.timedelta(hours=2)}))

        unsub.assert_called_once()
        assert hass.data[DOMAIN]["_smart_charge_unsubs"] == []
        assert hass.data[DOMAIN].get("_smart_charge_state") is None


class TestSmartChargeCoordinatorFallback:
    """Tests for smart charge using coordinator SoC when no external entity."""

    @pytest.mark.asyncio
    async def test_smart_charge_works_with_coordinator_soc(self) -> None:
        """Smart charge uses coordinator SoC."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Session should be active — coordinator SoC was used
        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["target_soc"] == 80

    @pytest.mark.asyncio
    async def test_smart_discharge_works_with_coordinator_soc(self) -> None:
        """Smart discharge uses coordinator SoC."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["min_soc"] == 30


class TestHandleSmartDischarge:
    """Tests for handle_smart_discharge service handler."""

    @pytest.mark.asyncio
    async def test_smart_discharge_sets_schedule(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, coordinator_data={"SoC": 80.0})

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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
        assert state["end"] == datetime.datetime(2026, 4, 7, 20, 0, 0)

    @pytest.mark.asyncio
    async def test_schedule_horizon_set_on_immediate_start(self) -> None:
        """schedule_horizon must be set when discharge starts immediately.

        The __init__.py service handler bypasses the adapter's apply_mode
        for the initial schedule setup (building/merging groups directly).
        It must still compute and store the safe schedule horizon so the
        sensor attribute is available from the first state update.

        Without this, schedule_horizon is only set when the listener's
        pacing loop calls apply_mode with a changed power — which may
        never happen if power stays at max throughout the session.
        """
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 35.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        # now=17:07 is past the deferred deadline (~17:06:50) for
        # SoC=35→30 at 10.5kW over a 10-min window, so discharge
        # starts immediately rather than deferring.
        now = datetime.datetime(2026, 4, 7, 17, 7, 0)
        with (
            patch(
                "custom_components.foxess_control.dt_util.now",
                return_value=now,
            ),
            patch(
                "custom_components.foxess_control.smart_battery.services.dt_util.now",
                return_value=now,
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=now,
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(17, 10),
                        "min_soc": 30,
                    }
                )
            )

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["discharging_started"] is True
        horizon = state.get("schedule_horizon")
        assert horizon is not None, (
            "schedule_horizon must be set on immediate discharge start"
        )
        # Horizon should be an ISO timestamp before the window end
        assert "T" in horizon
        end_iso = state["end"].isoformat()
        assert horizon < end_iso, (
            f"Horizon {horizon} should be before session end {end_iso}"
        )

    @pytest.mark.asyncio
    async def test_smart_discharge_registers_listeners(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, coordinator_data={"SoC": 80.0})

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        mock_timer_unsub = MagicMock()
        mock_interval_unsub = MagicMock()

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=mock_timer_unsub,
            ) as mock_track_time,
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=mock_interval_unsub,
            ) as mock_track_interval,
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

        mock_track_time.assert_called_once()
        mock_track_interval.assert_called_once()

        unsubs = hass.data[DOMAIN]["_smart_discharge_unsubs"]
        assert len(unsubs) == 2

    @pytest.mark.asyncio
    async def test_smart_discharge_missing_entity_raises(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(inverter=inv)

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="Battery SoC is not available"),
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
        hass = _make_hass(inverter=inv, coordinator_data={"SoC": 80.0})

        prev_unsub = MagicMock()
        hass.data[DOMAIN]["_smart_discharge_unsubs"] = [prev_unsub]

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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
        hass = _make_hass(inverter=inv, coordinator_data={"SoC": 80.0})

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
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

        assert captured_interval is not None

        # Simulate SoC dropping to threshold via coordinator
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 30.0}

        # First reading: registers count=1, doesn't cancel yet
        await captured_interval(datetime.datetime(2026, 4, 7, 18, 0, 0))
        assert hass.data[DOMAIN].get("_smart_discharge_state") is not None

        # Second consecutive reading: confirms and cancels
        await captured_interval(datetime.datetime(2026, 4, 7, 18, 1, 0))

        # The callback removes the override via _remove_mode_from_schedule
        inv.self_use.assert_called_once()
        # Listeners should be cancelled
        assert hass.data[DOMAIN]["_smart_discharge_unsubs"] == []

    @pytest.mark.asyncio
    async def test_soc_above_threshold_no_op(self) -> None:
        """When SoC is above threshold, nothing happens."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, coordinator_data={"SoC": 80.0})

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
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

        assert captured_interval is not None

        # SoC still above threshold via coordinator
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 50.0}

        await captured_interval(datetime.datetime(2026, 4, 7, 18, 0, 0))

        inv.self_use.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_discharge_paced_initial_power(self) -> None:
        """With battery capacity configured, initial power is paced."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 80.0, "loadsPower": 0.0, "pvPower": 0.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                    }
                )
            )

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["pacing_enabled"] is True
        assert state["max_power_w"] == 10500
        # With a long window and moderate SoC, discharge is deferred
        assert state["discharging_started"] is False
        assert state["last_power_w"] == 0
        assert state["discharging_started_at"] is None

    @pytest.mark.asyncio
    async def test_smart_discharge_no_pacing_without_capacity(self) -> None:
        """Without battery capacity, falls back to max power."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=0.0,
            coordinator_data={"SoC": 80.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                    }
                )
            )

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["pacing_enabled"] is False
        assert state["last_power_w"] == 10500

    @pytest.mark.asyncio
    async def test_smart_discharge_explicit_power_caps_pacing(self) -> None:
        """User-provided power acts as ceiling for paced discharge."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 80.0, "loadsPower": 0.0, "pvPower": 0.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                        "power": 2000,
                    }
                )
            )

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["max_power_w"] == 2000
        assert state["last_power_w"] <= 2000

    @pytest.mark.asyncio
    async def test_smart_discharge_pacing_callback_adjusts_power(self) -> None:
        """The interval callback recalculates and applies paced power."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 80.0, "loadsPower": 0.0, "pvPower": 0.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                    }
                )
            )

        assert captured_interval is not None

        # Simulate SoC dropping and time advancing past deferred start
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 50.0,
            "loadsPower": 0.0,
            "pvPower": 0.0,
        }

        # At 19:40, only 20 min remain — deferral should end, discharge starts
        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 19, 40, 0),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 19, 40, 0))

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["discharging_started"] is True
        assert state["last_power_w"] > 0

    @pytest.mark.asyncio
    async def test_deferred_to_discharging_triggers_ws(self) -> None:
        """WS lifecycle must be checked when deferred phase ends.

        The periodic timer fires the discharge callback every 60s.  When
        the deferred phase ends (forced discharge starts), the callback
        must trigger _maybe_start_realtime_ws so WebSocket connects for
        real-time data.  Previously the timer ran the unwrapped callback,
        so WS never connected after a deferred start.
        """
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 80.0, "loadsPower": 0.0, "pvPower": 0.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                    }
                )
            )

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["discharging_started"] is False

        ws_cb = hass.data[DOMAIN].get("_ws_discharge_callback")
        assert ws_cb is not None, "_ws_discharge_callback must be set after setup"

        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 50.0,
            "loadsPower": 0.0,
            "pvPower": 0.0,
        }

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 19, 40, 0),
            ),
            patch(
                "custom_components.foxess_control._maybe_start_realtime_ws",
                new_callable=AsyncMock,
            ) as mock_ws,
        ):
            await ws_cb(datetime.datetime(2026, 4, 7, 19, 40, 0))

        assert state["discharging_started"] is True
        mock_ws.assert_called_once()

    @pytest.mark.asyncio
    async def test_deferred_discharge_does_not_start_before_window(self) -> None:
        """Deferred discharge must not start forced discharge before window start.

        The listener recalculates the deferred start time each tick.  If it
        omits the window start floor, the algorithm can return a time before
        the window opens, causing the inverter to discharge early.

        Scenario: 10 kWh battery at 90%, min_soc=20%, window 18:00–18:30.
        The algorithm needs ~47 min to drain 70% at 10.5 kW, so without the
        start clamp it returns ~17:43 — 17 min before the window opens.
        """
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 90.0, "loadsPower": 0.5, "pvPower": 0.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        # Create session at 17:30, window 18:00–18:30.  The initial
        # deferred check at session creation passes start= correctly,
        # so should_defer=True (17:30 < 18:00).
        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 30, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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
                        "start_time": datetime.time(18, 0),
                        "end_time": datetime.time(18, 30),
                        "min_soc": 20,
                    }
                )
            )

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["discharging_started"] is False

        # Listener fires at 17:50.  Without the start= floor, the
        # algorithm returns 17:43, so 17:50 >= 17:43 triggers discharge
        # 10 minutes before the window opens.
        ws_cb = hass.data[DOMAIN].get("_ws_discharge_callback")
        assert ws_cb is not None

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 17, 50, 0),
        ):
            await ws_cb(datetime.datetime(2026, 4, 7, 17, 50, 0))

        assert state["discharging_started"] is False, (
            "Discharge must not start before the window opens"
        )

    @pytest.mark.asyncio
    async def test_smart_discharge_feedin_limit_constrains_pacing(self) -> None:
        """Feed-in energy limit caps paced power to spread export budget."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={
                "SoC": 80.0,
                "loadsPower": 0.5,
                "pvPower": 0.0,
                "feedin": 100.0,
            },
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            # With feedin limit: power should be lower than without
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                        "feedin_energy_limit_kwh": 3.0,
                    }
                )
            )

        constrained = hass.data[DOMAIN]["_smart_discharge_state"]
        assert constrained["feedin_energy_limit_kwh"] == 3.0
        # Both cases are deferred with this long window, but the feedin
        # session should be stored with its limit preserved
        assert constrained["discharging_started"] is False
        assert constrained["last_power_w"] == 0


class TestDischargeSocUnavailability:
    """Tests for discharge SoC unavailability abort (C-019)."""

    @pytest.mark.asyncio
    async def test_discharge_soc_unavailable_aborts(self) -> None:
        """Smart discharge aborts after MAX_SOC_UNAVAILABLE_COUNT misses."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0},
        )

        captured_interval_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval_callback
            captured_interval_callback = callback
            return MagicMock()

        from custom_components.foxess_control import (
            MAX_SOC_UNAVAILABLE_COUNT,
            _register_services,
        )

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
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

        assert captured_interval_callback is not None
        inv.set_schedule.reset_mock()

        # Make SoC unavailable
        hass.data[DOMAIN]["entry1"]["coordinator"].data = None

        for i in range(MAX_SOC_UNAVAILABLE_COUNT):
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, i + 1, 0),
            ):
                await captured_interval_callback(
                    datetime.datetime(2026, 4, 7, 17, i + 1, 0)
                )

        # Session should be cancelled
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None
        assert hass.data[DOMAIN]["_smart_discharge_unsubs"] == []

    @pytest.mark.asyncio
    async def test_discharge_soc_available_resets_count(self) -> None:
        """An available SoC reading resets the unavailable counter."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0},
        )

        captured_interval_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval_callback
            captured_interval_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
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

        assert captured_interval_callback is not None

        # Two unavailable readings
        hass.data[DOMAIN]["entry1"]["coordinator"].data = None
        for t in [1, 2]:
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, t, 0),
            ):
                await captured_interval_callback(
                    datetime.datetime(2026, 4, 7, 17, t, 0)
                )

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["soc_unavailable_count"] == 2

        # One available reading resets the counter
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 70.0}
        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 18, 0, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 18, 0, 0))

        assert state["soc_unavailable_count"] == 0


class TestCallbackExceptionSafety:
    """Tests for C-024: session aborts on uncaught exception in callback."""

    @pytest.mark.asyncio
    async def test_charge_callback_exception_cancels(self) -> None:
        """Uncaught exception in charge callback cancels session."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={"SoC": 20.0},
        )

        captured = None

        def capture(_h: Any, cb: Any, _i: Any) -> MagicMock:
            nonlocal captured
            captured = cb
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture,
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

        assert captured is not None
        assert hass.data[DOMAIN].get("_smart_charge_state") is not None

        from custom_components.foxess_control.smart_battery.const import (
            MAX_CONSECUTIVE_ADAPTER_ERRORS,
        )

        # Fire enough errors to exceed the retry threshold
        for i in range(MAX_CONSECUTIVE_ADAPTER_ERRORS):
            with (
                patch(
                    "custom_components.foxess_control.smart_battery.listeners._get_current_soc",
                    side_effect=RuntimeError("sensor exploded"),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                    return_value=datetime.datetime(2026, 4, 7, 2, 5 + i, 0),
                ),
            ):
                await captured(datetime.datetime(2026, 4, 7, 2, 5 + i, 0))

        # Session should be cancelled after repeated errors
        assert hass.data[DOMAIN].get("_smart_charge_state") is None

    @pytest.mark.asyncio
    async def test_discharge_callback_exception_cancels(self) -> None:
        """Uncaught exception in discharge callback cancels session."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0},
        )

        captured = None

        def capture(_h: Any, cb: Any, _i: Any) -> MagicMock:
            nonlocal captured
            captured = cb
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture,
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

        assert captured is not None
        assert hass.data[DOMAIN].get("_smart_discharge_state") is not None

        from custom_components.foxess_control.smart_battery.const import (
            MAX_CONSECUTIVE_ADAPTER_ERRORS,
        )

        # Fire enough errors to exceed the retry threshold
        for i in range(MAX_CONSECUTIVE_ADAPTER_ERRORS):
            with (
                patch(
                    "custom_components.foxess_control.smart_battery.listeners._get_net_consumption",
                    side_effect=RuntimeError("sensor exploded"),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                    return_value=datetime.datetime(2026, 4, 7, 17, 1 + i, 0),
                ),
            ):
                await captured(datetime.datetime(2026, 4, 7, 17, 1 + i, 0))

        # Session should be cancelled after repeated errors
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None


class TestErrorSurfacing:
    """Tests for C-026: proactive error surfacing."""

    @pytest.mark.asyncio
    async def test_soc_abort_records_error_state(self) -> None:
        """SoC unavailability abort writes to _smart_error_state."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={"SoC": 20.0},
        )

        captured = None

        def capture(_h: Any, cb: Any, _i: Any) -> MagicMock:
            nonlocal captured
            captured = cb
            return MagicMock()

        from custom_components.foxess_control import (
            MAX_SOC_UNAVAILABLE_COUNT,
            _register_services,
        )

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture,
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

        assert captured is not None
        assert hass.data[DOMAIN].get("_smart_error_state") is None

        # Make SoC unavailable and fire until abort
        hass.data[DOMAIN]["entry1"]["coordinator"].data = None
        for i in range(MAX_SOC_UNAVAILABLE_COUNT):
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 5 * (i + 1), 0),
            ):
                await captured(datetime.datetime(2026, 4, 7, 2, 5 * (i + 1), 0))

        # Error state should exist
        err = hass.data[DOMAIN].get("_smart_error_state")
        assert err is not None
        assert "SoC unavailable" in err["last_error"]
        assert err["error_count"] == 1
        assert err["last_error_at"] is not None

    @pytest.mark.asyncio
    async def test_new_session_clears_error(self) -> None:
        """Starting a new session clears the previous error state."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0},
        )

        # Inject a previous error
        hass.data[DOMAIN]["_smart_error_state"] = {
            "last_error": "old error",
            "last_error_at": "2026-04-07T00:00:00",
            "error_count": 5,
        }

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Error should be cleared
        assert hass.data[DOMAIN].get("_smart_error_state") is None


class TestSessionBoundaryCleanness:
    """Tests for C-025: no transient state leaks between sessions."""

    @pytest.mark.asyncio
    async def test_discharge_state_isolated_between_sessions(self) -> None:
        """Transient state from one discharge session doesn't leak to the next."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, coordinator_data={"SoC": 80.0})

        from custom_components.foxess_control import (
            _cancel_smart_discharge,
            _register_services,
        )

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        async def _start_session() -> None:
            with (
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                    return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                    return_value=MagicMock(),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Session 1: start and inject transient state
        await _start_session()
        state1 = hass.data[DOMAIN]["_smart_discharge_state"]
        state1["consumption_peak_kw"] = 7.5
        state1["taper_tick"] = 42
        state1["feedin_prev_kwh"] = 123.4
        state1["feedin_stop_scheduled"] = True
        state1["soc_unavailable_count"] = 2

        session1_id = state1["session_id"]

        # Cancel session 1
        _cancel_smart_discharge(hass)
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None

        # Session 2: start fresh
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        await _start_session()
        state2 = hass.data[DOMAIN]["_smart_discharge_state"]

        # Different session
        assert state2["session_id"] != session1_id

        # No leaked transient state
        assert state2.get("taper_tick") is None
        assert state2.get("feedin_prev_kwh") is None
        assert state2.get("feedin_stop_scheduled") is None
        assert state2["soc_unavailable_count"] == 0
        assert state2["soc_below_min_count"] == 0
        # consumption_peak_kw is initialised from current net consumption
        # (not leaked from previous session's 7.5)
        assert state2["consumption_peak_kw"] != 7.5

    @pytest.mark.asyncio
    async def test_charge_state_isolated_between_sessions(self) -> None:
        """Transient state from one charge session doesn't leak to the next."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={"SoC": 20.0},
        )

        from custom_components.foxess_control import (
            _cancel_smart_charge,
            _register_services,
        )

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        async def _start_session() -> None:
            with (
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                    return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                    return_value=MagicMock(),
                ),
                patch(
                    "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Session 1: start and inject transient state
        await _start_session()
        state1 = hass.data[DOMAIN]["_smart_charge_state"]
        state1["taper_tick"] = 99
        state1["soc_unavailable_count"] = 2
        session1_id = state1["session_id"]

        # Cancel session 1
        _cancel_smart_charge(hass)
        assert hass.data[DOMAIN].get("_smart_charge_state") is None

        # Session 2: start fresh
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        await _start_session()
        state2 = hass.data[DOMAIN]["_smart_charge_state"]

        assert state2["session_id"] != session1_id
        assert state2.get("taper_tick") is None
        assert state2["soc_unavailable_count"] == 0


class TestHandleSmartCharge:
    """Tests for handle_smart_charge service handler."""

    @pytest.mark.asyncio
    async def test_smart_charge_defers_when_window_long_enough(self) -> None:
        """With a small battery and long window, charging is deferred."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Schedule should NOT be set yet — charging is deferred
        inv.set_schedule.assert_not_called()

        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["charging_started"] is False
        assert state["groups"] is None
        assert state["last_power_w"] == 0

    @pytest.mark.asyncio
    async def test_smart_charge_immediate_when_window_tight(self) -> None:
        """With a large battery and household load, there's not enough time to defer."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={"SoC": 20.0, "loadsPower": 3.0, "pvPower": 0.0},
        )

        # 60kWh * 60% = 36kWh needed; 10.5kW - 3kW load = 7.5kW eff; 36/7.5 = 4.8h > 4h

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Should set schedule immediately — window is too tight to defer
        inv.set_schedule.assert_called_once()
        groups = inv.set_schedule.call_args.args[0]
        assert len(groups) == 1
        assert groups[0]["workMode"] == "ForceCharge"
        assert groups[0]["fdSoc"] == 100

        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["charging_started"] is True

    @pytest.mark.asyncio
    async def test_smart_charge_registers_two_listeners(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        mock_timer_unsub = MagicMock()
        mock_interval_unsub = MagicMock()

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=mock_timer_unsub,
            ) as mock_track_time,
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        mock_track_time.assert_called_once()
        mock_track_interval.assert_called_once()

        unsubs = hass.data[DOMAIN]["_smart_charge_unsubs"]
        assert len(unsubs) == 2

    @pytest.mark.asyncio
    async def test_smart_charge_missing_entity_raises(self) -> None:
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="Battery SoC is not available"),
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
            battery_capacity_kwh=0.0,
            coordinator_data={"SoC": 20.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
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
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 80.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            pytest.raises(ServiceValidationError, match="at or above"),
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
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        prev_unsub = MagicMock()
        hass.data[DOMAIN]["_smart_charge_unsubs"] = [prev_unsub]

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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
    async def test_soc_at_target_monitors_until_window_ends(self) -> None:
        """SoC reaching target keeps session alive for monitoring."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        assert captured_interval is not None

        # Simulate SoC reaching target via coordinator
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 80.0}

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 5, 0, 0),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 5, 0, 0))
            # Session stays alive with target_reached flag
            state = hass.data[DOMAIN].get("_smart_charge_state")
            assert state is not None
            assert state.get("target_reached") is True

            # Simulate SoC dropping below target (consumption spike)
            hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 75.0}
            await captured_interval(datetime.datetime(2026, 4, 7, 5, 5, 0))
            state = hass.data[DOMAIN].get("_smart_charge_state")
            assert state is not None
            assert state.get("target_reached") is not True

    @pytest.mark.asyncio
    async def test_periodic_adjustment_updates_schedule(self) -> None:
        """Periodic callback recalculates power and updates schedule."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        # Large capacity + load → immediate start (no deferral)
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            min_power_change=100,
            coordinator_data={
                "SoC": 20.0,
                "loadsPower": 3.0,
                "pvPower": 0.0,
            },
        )

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
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # At 60% with 2h left + 3kW load: 60kWh*20%/2h + 3kW = 6000+3000 = 9000W
        # vs initial ~12kW → delta well above 100W threshold
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 60.0,
            "loadsPower": 3.0,
            "pvPower": 0.0,
        }

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 4, 0, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 4, 0, 0))

        inv.set_schedule.assert_called_once()

    @pytest.mark.asyncio
    async def test_deferred_charge_starts_when_time_arrives(self) -> None:
        """Periodic callback starts charging when deferred start time is reached."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        # Small capacity → deferred
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            min_power_change=100,
            coordinator_data={"SoC": 20.0},
        )

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
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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
        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["charging_started"] is False
        inv.set_schedule.assert_not_called()

        # At 05:50, SoC still 20% → deferred_start ≈ 05:17 → now past it

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 5, 50, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 5, 50, 0))

        # Now charging should have started
        inv.set_schedule.assert_called_once()
        assert state["charging_started"] is True
        assert state["groups"] is not None

    @pytest.mark.asyncio
    async def test_deferred_charge_keeps_waiting_when_solar_raises_soc(self) -> None:
        """If solar raises SoC during wait, deferred start pushes later."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            min_power_change=100,
            coordinator_data={"SoC": 20.0},
        )

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
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # At 05:20, SoC rose to 60% via solar → only 20% needed
        # 10kWh * 20% = 2kWh; 80% of 10.5kW = 8.4kW; 2/8.4 = 0.238h
        # + 10% time buffer: 0.238/0.9 = 0.264h ≈ 16min
        # deferred_start = 05:44 → still in the future at 05:20
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 60.0}

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 5, 20, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 5, 20, 0))

        # Still deferred — solar pushed the start time later
        inv.set_schedule.assert_not_called()
        assert hass.data[DOMAIN]["_smart_charge_state"]["charging_started"] is False

    @pytest.mark.asyncio
    async def test_periodic_adjustment_skips_below_threshold(self) -> None:
        """Power change below min_power_change is skipped."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        # 10kWh battery → power well below max so threshold logic applies
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            min_power_change=5000,
            coordinator_data={"SoC": 20.0},
        )

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
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
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
        # Large capacity → immediate start
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={"SoC": 20.0},
        )

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
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Make SoC unavailable by clearing coordinator data
        hass.data[DOMAIN]["entry1"]["coordinator"].data = None

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 3, 0, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 3, 0, 0))

        inv.set_schedule.assert_not_called()
        # Session should still be active (not yet at threshold)
        assert hass.data[DOMAIN].get("_smart_charge_state") is not None

    @pytest.mark.asyncio
    async def test_soc_unavailable_aborts_after_threshold(self) -> None:
        """Smart charge aborts after MAX_SOC_UNAVAILABLE_COUNT consecutive misses."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={"SoC": 20.0},
        )

        captured_interval_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval_callback
            captured_interval_callback = callback
            return MagicMock()

        from custom_components.foxess_control import (
            MAX_SOC_UNAVAILABLE_COUNT,
            _register_services,
        )

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Make SoC unavailable
        hass.data[DOMAIN]["entry1"]["coordinator"].data = None

        # Fire unavailable checks up to threshold
        for i in range(MAX_SOC_UNAVAILABLE_COUNT):
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 5 * (i + 1), 0),
            ):
                await captured_interval_callback(
                    datetime.datetime(2026, 4, 7, 2, 5 * (i + 1), 0)
                )

        # Session should be cancelled and override removed
        assert hass.data[DOMAIN].get("_smart_charge_state") is None
        assert hass.data[DOMAIN]["_smart_charge_unsubs"] == []

    @pytest.mark.asyncio
    async def test_soc_available_resets_unavailable_count(self) -> None:
        """An available SoC reading resets the unavailable counter."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            min_power_change=100,
            coordinator_data={"SoC": 20.0},
        )

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
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        # Two unavailable readings
        hass.data[DOMAIN]["entry1"]["coordinator"].data = None

        for t in [5, 10]:
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, t, 0),
            ):
                await captured_interval_callback(datetime.datetime(2026, 4, 7, 2, t, 0))

        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["soc_unavailable_count"] == 2

        # One available reading resets the counter
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 60.0}

        inv.set_schedule.reset_mock()
        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 4, 0, 0),
        ):
            await captured_interval_callback(datetime.datetime(2026, 4, 7, 4, 0, 0))

        assert state["soc_unavailable_count"] == 0

    @pytest.mark.asyncio
    async def test_deferred_charge_aborts_on_conflict(self) -> None:
        """Deferred charge aborts if a conflict exists when starting."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            min_power_change=100,
            coordinator_data={"SoC": 20.0},
        )

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
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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
        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["charging_started"] is False

        # Someone added a conflicting ForceDischarge while we were deferred
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceDischarge",
                    "startHour": 5,
                    "startMinute": 0,
                    "endHour": 6,
                    "endMinute": 0,
                    "minSocOnGrid": 15,
                    "fdSoc": 11,
                    "fdPwr": 10500,
                },
            ],
        }

        # At 05:50, deferred start has passed → tries to start charging.
        # Conflict persists across retries, so session aborts after threshold.
        from custom_components.foxess_control.smart_battery.const import (
            MAX_CONSECUTIVE_ADAPTER_ERRORS,
        )

        for i in range(MAX_CONSECUTIVE_ADAPTER_ERRORS):
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 5, 50 + i, 0),
            ):
                await captured_interval_callback(
                    datetime.datetime(2026, 4, 7, 5, 50 + i, 0)
                )

        # Should abort — conflict persisted across retries
        assert hass.data[DOMAIN].get("_smart_charge_state") is None
        assert hass.data[DOMAIN]["_smart_charge_unsubs"] == []
        # Should NOT have set a schedule
        inv.set_schedule.assert_not_called()


class TestSessionPersistence:
    """Tests for saving and clearing sessions in persistent storage."""

    @pytest.mark.asyncio
    async def test_smart_charge_saves_session(self) -> None:
        """Starting a smart charge persists session data to store."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 8, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        store = hass.data[DOMAIN]["_store"]
        store.async_delay_save.assert_called()
        data_func = store.async_delay_save.call_args.args[0]
        saved = data_func()
        assert "smart_charge" in saved
        sc = saved["smart_charge"]
        assert sc["date"] == "2026-04-08"
        assert sc["start_hour"] == 2
        assert sc["end_hour"] == 6
        assert sc["target_soc"] == 80

    @pytest.mark.asyncio
    async def test_smart_discharge_saves_session(self) -> None:
        """Starting a smart discharge persists session data to store."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0},
        )

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 8, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        store = hass.data[DOMAIN]["_store"]
        store.async_delay_save.assert_called()
        data_func = store.async_delay_save.call_args.args[0]
        saved = data_func()
        assert "smart_discharge" in saved
        sd = saved["smart_discharge"]
        assert sd["date"] == "2026-04-08"
        assert sd["min_soc"] == 30
        assert sd["end_hour"] == 20

    @pytest.mark.asyncio
    async def test_cancel_smart_charge_clears_store(self) -> None:
        """Cancelling a smart charge clears it from the store."""
        inv = MagicMock(spec=Inverter)
        hass = _make_hass(inverter=inv)

        # Pre-populate store with a session
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={"smart_charge": {"date": "2026-04-08"}}
        )

        hass.data[DOMAIN]["_smart_charge_state"] = {"target_soc": 80}

        from custom_components.foxess_control import _cancel_smart_charge

        _cancel_smart_charge(hass)

        # Let the async_create_task run
        await asyncio.sleep(0)

        store.async_save.assert_called()
        saved = store.async_save.call_args.args[0]
        assert "smart_charge" not in saved


class TestRecoverSessions:
    """Tests for _recover_sessions on startup."""

    @pytest.mark.asyncio
    async def test_stale_session_cleaned_up(self) -> None:
        """Sessions from a different day are discarded."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(inverter=inv)
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_charge": {
                    "date": "2026-04-07",
                    "start_hour": 2,
                    "start_minute": 0,
                    "end_hour": 6,
                    "end_minute": 0,
                    "target_soc": 80,
                    "max_power_w": 10500,
                    "battery_capacity_kwh": 10.0,
                    "min_soc_on_grid": 15,
                    "min_power_change": 500,
                    "force": False,
                    "charging_started": True,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 3, 0, 0),
        ):
            await _recover_sessions(hass, inv)

        # Session should be cleared
        assert hass.data[DOMAIN].get("_smart_charge_state") is None
        store.async_save.assert_called()
        saved = store.async_save.call_args.args[0]
        assert "smart_charge" not in saved

    @pytest.mark.asyncio
    async def test_expired_session_cleaned_up(self) -> None:
        """Sessions whose window has passed are cleaned up."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_charge": {
                    "date": "2026-04-08",
                    "start_hour": 2,
                    "start_minute": 0,
                    "end_hour": 6,
                    "end_minute": 0,
                    "target_soc": 80,
                    "max_power_w": 10500,
                    "battery_capacity_kwh": 10.0,
                    "min_soc_on_grid": 15,
                    "min_power_change": 500,
                    "force": False,
                    "charging_started": True,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        # Time is after the end window
        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 7, 0, 0),
        ):
            await _recover_sessions(hass, inv)

        # Should clean up the ForceCharge from schedule
        assert hass.data[DOMAIN].get("_smart_charge_state") is None
        store.async_save.assert_called()

    @pytest.mark.asyncio
    async def test_active_charge_session_resumed(self) -> None:
        """An active smart charge session is resumed with listeners."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceCharge",
                    "startHour": 2,
                    "startMinute": 0,
                    "endHour": 6,
                    "endMinute": 0,
                    "minSocOnGrid": 15,
                    "fdSoc": 100,
                    "fdPwr": 5000,
                }
            ],
        }
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 50.0},
        )
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_charge": {
                    "date": "2026-04-08",
                    "start_hour": 2,
                    "start_minute": 0,
                    "end_hour": 6,
                    "end_minute": 0,
                    "target_soc": 80,
                    "max_power_w": 10500,
                    "battery_capacity_kwh": 10.0,
                    "min_soc_on_grid": 15,
                    "min_power_change": 500,
                    "force": False,
                    "charging_started": True,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 8, 4, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await _recover_sessions(hass, inv)

        # State should be rebuilt
        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["target_soc"] == 80
        assert state["charging_started"] is True
        assert state["soc_unavailable_count"] == 0

        # Listeners should be registered (timer + interval)
        unsubs = hass.data[DOMAIN]["_smart_charge_unsubs"]
        assert len(unsubs) == 2

    @pytest.mark.asyncio
    async def test_recovered_charge_listener_sets_fdsoc_100(self) -> None:
        """After recovery, the interval callback must set fdSoc=100 for charge.

        Recovery always starts with empty adapter groups (slow path).
        If apply_mode defaults fd_soc to 11 instead of 100, the inverter
        charges to 11% instead of the target — a major regression.
        """
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceCharge",
                    "startHour": 2,
                    "startMinute": 0,
                    "endHour": 6,
                    "endMinute": 0,
                    "minSocOnGrid": 15,
                    "fdSoc": 100,
                    "fdPwr": 5000,
                }
            ],
        }
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 50.0, "loadsPower": 0.0, "pvPower": 0.0},
        )
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_charge": {
                    "date": "2026-04-08",
                    "start_hour": 2,
                    "start_minute": 0,
                    "end_hour": 6,
                    "end_minute": 0,
                    "target_soc": 80,
                    "max_power_w": 10500,
                    "battery_capacity_kwh": 10.0,
                    "min_soc_on_grid": 15,
                    "min_power_change": 500,
                    "force": False,
                    "charging_started": True,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 8, 4, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await _recover_sessions(hass, inv)

        assert captured_interval is not None

        # Reset set_schedule call count from recovery
        inv.set_schedule.reset_mock()
        inv.get_schedule.return_value = {"enable": 0, "groups": []}

        # Change SoC so the power recalculation produces a different value,
        # forcing the listener to call apply_mode (which hits the slow path
        # because recovered sessions start with empty adapter groups).
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 30.0,
            "loadsPower": 0.0,
            "pvPower": 0.0,
        }
        # Ensure power change exceeds threshold
        hass.data[DOMAIN]["_smart_charge_state"]["last_power_w"] = 0

        # Fire the interval callback — adapter has empty groups (slow path)
        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 4, 5, 0),
        ):
            await captured_interval(datetime.datetime(2026, 4, 8, 4, 5, 0))

        inv.set_schedule.assert_called()
        groups = inv.set_schedule.call_args.args[0]
        charge_group = next(g for g in groups if g.get("workMode") == "ForceCharge")
        assert charge_group["fdSoc"] == 100, (
            f"fdSoc should be 100 for charge, got {charge_group['fdSoc']}"
        )

    @pytest.mark.asyncio
    async def test_deferred_charge_recovery_shows_zero_power(self) -> None:
        """Recovered deferred charge session should show last_power_w=0."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceCharge",
                    "startHour": 4,
                    "startMinute": 0,
                    "endHour": 6,
                    "endMinute": 0,
                    "minSocOnGrid": 15,
                    "fdSoc": 100,
                    "fdPwr": 5000,
                }
            ],
        }
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 30.0},
        )
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_charge": {
                    "date": "2026-04-08",
                    "start_hour": 4,
                    "start_minute": 0,
                    "end_hour": 6,
                    "end_minute": 0,
                    "target_soc": 80,
                    "max_power_w": 10500,
                    "battery_capacity_kwh": 10.0,
                    "min_soc_on_grid": 15,
                    "min_power_change": 500,
                    "force": False,
                    "charging_started": False,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 8, 3, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await _recover_sessions(hass, inv)

        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["charging_started"] is False
        assert state["last_power_w"] == 0

    @pytest.mark.asyncio
    async def test_no_matching_group_discards_session(self) -> None:
        """If the inverter has no matching group, the session is discarded."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv)
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_charge": {
                    "date": "2026-04-08",
                    "start_hour": 2,
                    "start_minute": 0,
                    "end_hour": 6,
                    "end_minute": 0,
                    "target_soc": 80,
                    "max_power_w": 10500,
                    "battery_capacity_kwh": 10.0,
                    "min_soc_on_grid": 15,
                    "min_power_change": 500,
                    "force": False,
                    "charging_started": True,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 8, 4, 0, 0),
        ):
            await _recover_sessions(hass, inv)

        assert hass.data[DOMAIN].get("_smart_charge_state") is None
        store.async_save.assert_called()

    @pytest.mark.asyncio
    async def test_deferred_charge_session_resumed(self) -> None:
        """A deferred (not yet charging) session resumes correctly."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 20.0},
        )
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_charge": {
                    "date": "2026-04-08",
                    "start_hour": 2,
                    "start_minute": 0,
                    "end_hour": 6,
                    "end_minute": 0,
                    "target_soc": 80,
                    "max_power_w": 10500,
                    "battery_capacity_kwh": 10.0,
                    "min_soc_on_grid": 15,
                    "min_power_change": 500,
                    "force": False,
                    "charging_started": False,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 8, 3, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await _recover_sessions(hass, inv)

        # Deferred session resumes (no group needed)
        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state["charging_started"] is False
        assert len(hass.data[DOMAIN]["_smart_charge_unsubs"]) == 2

    @pytest.mark.asyncio
    async def test_active_discharge_session_resumed(self) -> None:
        """An active smart discharge session is resumed."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceDischarge",
                    "startHour": 17,
                    "startMinute": 0,
                    "endHour": 20,
                    "endMinute": 0,
                    "minSocOnGrid": 15,
                    "fdSoc": 11,
                    "fdPwr": 5000,
                }
            ],
        }
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 50.0},
        )
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_discharge": {
                    "date": "2026-04-08",
                    "start_hour": 17,
                    "start_minute": 0,
                    "end_hour": 20,
                    "end_minute": 0,
                    "min_soc": 30,
                    "last_power_w": 5000,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 8, 18, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await _recover_sessions(hass, inv)

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["min_soc"] == 30
        unsubs = hass.data[DOMAIN]["_smart_discharge_unsubs"]
        assert len(unsubs) == 2

    @pytest.mark.asyncio
    async def test_discharge_recovery_with_schedule_horizon(self) -> None:
        """Discharge session recovers when inverter schedule uses horizon end time.

        C-027 sets the schedule end to a safe horizon (e.g. 19:24) that is
        earlier than the session window end (e.g. 20:01). Recovery must still
        find the schedule group even when the end times don't match exactly.
        """
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceDischarge",
                    "startHour": 18,
                    "startMinute": 0,
                    "endHour": 19,
                    "endMinute": 24,
                    "minSocOnGrid": 15,
                    "fdSoc": 11,
                    "fdPwr": 8054,
                }
            ],
        }
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 68.0},
        )
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_discharge": {
                    "date": "2026-04-18",
                    "start_hour": 18,
                    "start_minute": 0,
                    "end_hour": 20,
                    "end_minute": 1,
                    "min_soc": 40,
                    "last_power_w": 8054,
                    "discharging_started": True,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 18, 19, 6, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await _recover_sessions(hass, inv)

        assert hass.data[DOMAIN].get("_smart_discharge_state") is not None, (
            "Discharge session should be recovered when inverter schedule "
            "uses horizon end time different from session window end"
        )
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["min_soc"] == 40

    @pytest.mark.asyncio
    async def test_charge_recovery_with_schedule_horizon(self) -> None:
        """Charge session recovers when schedule end differs from window end."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "workMode": "ForceCharge",
                    "startHour": 1,
                    "startMinute": 0,
                    "endHour": 4,
                    "endMinute": 30,
                    "minSocOnGrid": 15,
                    "fdSoc": 100,
                    "fdPwr": 5000,
                }
            ],
        }
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0},
        )
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={
                "smart_charge": {
                    "date": "2026-04-18",
                    "start_hour": 1,
                    "start_minute": 0,
                    "end_hour": 6,
                    "end_minute": 0,
                    "target_soc": 100,
                    "last_power_w": 5000,
                    "charging_started": True,
                }
            }
        )

        from custom_components.foxess_control import _recover_sessions

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 18, 3, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await _recover_sessions(hass, inv)

        assert hass.data[DOMAIN].get("_smart_charge_state") is not None, (
            "Charge session should be recovered when inverter schedule "
            "uses horizon end time different from session window end"
        )

    @pytest.mark.asyncio
    async def test_empty_store_no_op(self) -> None:
        """No stored sessions means nothing to recover."""
        inv = MagicMock(spec=Inverter)
        hass = _make_hass(inverter=inv)
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(return_value=None)

        from custom_components.foxess_control import _recover_sessions

        await _recover_sessions(hass, inv)

        assert hass.data[DOMAIN].get("_smart_charge_state") is None
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None
        store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_corrupted_charge_session_discarded(self) -> None:
        """Corrupted charge data (missing keys) is discarded gracefully."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(inverter=inv)
        store = hass.data[DOMAIN]["_store"]
        # Missing all required time fields
        store.async_load = AsyncMock(
            return_value={"smart_charge": {"date": "2026-04-09"}}
        )

        from custom_components.foxess_control import _recover_sessions

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 9, 3, 0, 0),
        ):
            await _recover_sessions(hass, inv)

        assert hass.data[DOMAIN].get("_smart_charge_state") is None
        store.async_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_corrupted_discharge_session_discarded(self) -> None:
        """Corrupted discharge data is discarded gracefully."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        hass = _make_hass(inverter=inv)
        store = hass.data[DOMAIN]["_store"]
        store.async_load = AsyncMock(
            return_value={"smart_discharge": {"date": "2026-04-09"}}
        )

        from custom_components.foxess_control import _recover_sessions

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 9, 12, 0, 0),
        ):
            await _recover_sessions(hass, inv)

        assert hass.data[DOMAIN].get("_smart_discharge_state") is None
        store.async_save.assert_called_once()


class TestSocStabilityCounters:
    """Tests for SoC stability counters (require 2 consecutive readings)."""

    @pytest.mark.asyncio
    async def test_charge_single_above_target_does_not_cancel(self) -> None:
        """A single SoC reading at target should not cancel the session."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        assert captured_interval is not None

        # SoC jumps to target
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 80.0}

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 4, 0, 0),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 4, 0, 0))

        # Session should still be active with target_reached flag
        state = hass.data[DOMAIN].get("_smart_charge_state")
        assert state is not None
        assert state.get("target_reached") is True

    @pytest.mark.asyncio
    async def test_charge_soc_drops_below_target_resets_counter(self) -> None:
        """If SoC drops back below target, the counter resets."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 20.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        assert captured_interval is not None

        # SoC at target → count=1
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 80.0}
        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 4, 0, 0),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 4, 0, 0))

        assert hass.data[DOMAIN]["_smart_charge_state"].get("target_reached") is True

        # SoC drops back → target_reached clears, session resumes
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 78.0}
        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 4, 5, 0),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 4, 5, 0))

        state = hass.data[DOMAIN]["_smart_charge_state"]
        assert state.get("target_reached") is not True

    @pytest.mark.asyncio
    async def test_discharge_single_below_threshold_no_cancel(self) -> None:
        """A single SoC reading at/below threshold does not cancel discharge."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, coordinator_data={"SoC": 80.0})

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
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

        assert captured_interval is not None

        # SoC drops to threshold
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 30.0}
        await captured_interval(datetime.datetime(2026, 4, 7, 18, 0, 0))

        # Session still active after just one reading
        state = hass.data[DOMAIN].get("_smart_discharge_state")
        assert state is not None
        assert state["soc_below_min_count"] == 1

    @pytest.mark.asyncio
    async def test_discharge_soc_recovers_resets_counter(self) -> None:
        """If discharge SoC goes back above threshold, counter resets."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(inverter=inv, coordinator_data={"SoC": 80.0})

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 10, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
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

        assert captured_interval is not None

        # SoC dips below → count=1
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 29.0}
        await captured_interval(datetime.datetime(2026, 4, 7, 18, 0, 0))
        assert hass.data[DOMAIN]["_smart_discharge_state"]["soc_below_min_count"] == 1

        # SoC recovers → counter resets
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {"SoC": 35.0}
        await captured_interval(datetime.datetime(2026, 4, 7, 18, 1, 0))
        assert hass.data[DOMAIN]["_smart_discharge_state"]["soc_below_min_count"] == 0


class TestFeedinEnergyLimit:
    """Tests for the feed-in energy limit on smart discharge."""

    @pytest.mark.asyncio
    async def test_feedin_limit_stops_discharge(self) -> None:
        """Discharge stops when feedin counter exceeds the limit."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        # feedin=100.0 at session start
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0, "feedin": 100.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                        "feedin_energy_limit_kwh": 2.0,
                    }
                )
            )

        assert captured_interval is not None
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["feedin_energy_limit_kwh"] == 2.0
        assert state["feedin_start_kwh"] is None

        # First tick: baseline deferred — listener captures it now
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 75.0,
            "feedin": 100.0,
        }
        await captured_interval(datetime.datetime(2026, 4, 7, 17, 5, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["feedin_start_kwh"] == 100.0

        # Counter has increased by 2.5 kWh (> 2.0 limit)
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 70.0,
            "feedin": 102.5,
        }
        await captured_interval(datetime.datetime(2026, 4, 7, 17, 30, 0))

        # Session should be cancelled
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None

    @pytest.mark.asyncio
    async def test_feedin_counter_tracks_across_intervals(self) -> None:
        """Feed-in energy is tracked by comparing counter to start snapshot."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0, "feedin": 500.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                        "feedin_energy_limit_kwh": 3.0,
                    }
                )
            )

        assert captured_interval is not None

        # First tick: captures baseline (feedin_start_kwh deferred to listener)
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 75.0,
            "feedin": 500.0,
        }
        await captured_interval(datetime.datetime(2026, 4, 7, 17, 5, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None
        assert state["feedin_start_kwh"] == 500.0

        # Counter +1 kWh — under limit
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 70.0,
            "feedin": 501.0,
        }
        await captured_interval(datetime.datetime(2026, 4, 7, 18, 0, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None

        # Counter +2 kWh — still under
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 65.0,
            "feedin": 502.0,
        }
        await captured_interval(datetime.datetime(2026, 4, 7, 19, 0, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None

        # Counter +3 kWh — at limit, should cancel
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 60.0,
            "feedin": 503.0,
        }
        await captured_interval(datetime.datetime(2026, 4, 7, 19, 30, 0))
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None

    @pytest.mark.asyncio
    async def test_no_feedin_limit_skips_tracking(self) -> None:
        """Without feedin_energy_limit_kwh, no feed-in tracking occurs."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0, "feedin": 500.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                    }
                )
            )

        assert captured_interval is not None

        # Even with large counter increase, session continues (no limit set)
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 70.0,
            "feedin": 600.0,
        }
        await captured_interval(datetime.datetime(2026, 4, 7, 18, 0, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None

    @pytest.mark.asyncio
    async def test_feedin_counter_unavailable_does_not_cancel(self) -> None:
        """When feedin counter is unavailable, session continues."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0, "feedin": 100.0},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                        "feedin_energy_limit_kwh": 2.0,
                    }
                )
            )

        assert captured_interval is not None

        # feedin counter missing from coordinator data
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 70.0,
        }
        await captured_interval(datetime.datetime(2026, 4, 7, 18, 0, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None

    @pytest.mark.asyncio
    async def test_feedin_schedules_early_stop_when_close_to_limit(self) -> None:
        """Early stop is scheduled using observed export rate."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0, "feedin": 100.0},
        )

        captured_interval = None
        point_in_time_calls: list[Any] = []

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        def capture_point_in_time(_hass: Any, callback: Any, when: Any) -> MagicMock:
            point_in_time_calls.append((callback, when))
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        pit_patch = patch(
            "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
            side_effect=capture_point_in_time,
        )

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            pit_patch,
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                        "feedin_energy_limit_kwh": 1.0,
                    }
                )
            )

        assert captured_interval is not None
        # One point_in_time call for the window end timer
        initial_point_calls = len(point_in_time_calls)

        # Baseline tick: listener captures feedin_start_kwh from fresh data
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 78.0,
            "feedin": 100.0,
        }
        with (
            pit_patch,
            patch(
                "custom_components.foxess_control.dt_util.utcnow",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 5),
            ),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 17, 0, 5))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None
        assert state["feedin_start_kwh"] == 100.0

        # Poll 1: exported 0.30 kWh. No previous reading yet →
        # no observed rate, no early stop scheduled.
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 70.0,
            "feedin": 100.3,
        }
        with (
            pit_patch,
            patch(
                "custom_components.foxess_control.dt_util.utcnow",
                return_value=datetime.datetime(2026, 4, 7, 17, 5, 0),
            ),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 17, 5, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None
        assert state.get("feedin_prev_kwh") == 100.3
        assert len(point_in_time_calls) == initial_point_calls  # No stop scheduled

        # Poll 2: exported 0.60 kWh total (0.30 this interval).
        # observed_rate = (100.6 - 100.3) / 0.08333 = 3.6 kW
        # energy_next_poll = 3.6 * 0.08333 = 0.30 kWh
        # remaining = 0.40 kWh > 0.30 → no early stop
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 65.0,
            "feedin": 100.6,
        }
        with (
            pit_patch,
            patch(
                "custom_components.foxess_control.dt_util.utcnow",
                return_value=datetime.datetime(2026, 4, 7, 17, 10, 0),
            ),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 17, 10, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None
        assert state["last_power_w"] == 10500  # Unchanged
        assert len(point_in_time_calls) == initial_point_calls  # Still no stop

        # Poll 3: exported 0.85 kWh total (0.25 this interval).
        # observed_rate = (100.85 - 100.6) / 0.08333 = 3.0 kW
        # remaining = 0.15 kWh, energy_next_poll = 0.25
        # 0.15 <= 0.25 → schedule early stop
        # seconds_to_target = 0.15 / 3.0 * 3600 = 180s
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 62.0,
            "feedin": 100.85,
        }
        with (
            pit_patch,
            patch(
                "custom_components.foxess_control.dt_util.utcnow",
                return_value=datetime.datetime(2026, 4, 7, 17, 15, 0),
            ),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 17, 15, 0))
        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None
        assert state.get("feedin_stop_scheduled") is True
        assert len(point_in_time_calls) == initial_point_calls + 1

        # Verify the scheduled stop time is ~180s from 17:15
        _stop_cb, stop_time = point_in_time_calls[-1]
        expected_stop = datetime.datetime(2026, 4, 7, 17, 18, 0)
        assert abs((stop_time - expected_stop).total_seconds()) < 5

        # Invoking the early stop callback cancels the discharge
        await _stop_cb(stop_time)
        assert hass.data[DOMAIN].get("_smart_discharge_state") is None

    @pytest.mark.asyncio
    async def test_feedin_no_early_stop_when_plenty_remaining(self) -> None:
        """No early stop scheduled when remaining energy is large."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0, "feedin": 100.0},
        )

        captured_interval = None
        point_in_time_calls: list[Any] = []

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        def capture_point_in_time(_hass: Any, callback: Any, when: Any) -> MagicMock:
            point_in_time_calls.append((callback, when))
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        pit_patch = patch(
            "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
            side_effect=capture_point_in_time,
        )

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            pit_patch,
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                        "feedin_energy_limit_kwh": 3.0,
                    }
                )
            )

        assert captured_interval is not None
        initial_point_calls = len(point_in_time_calls)

        # Baseline tick: listener captures feedin_start_kwh from fresh data
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 78.0,
            "feedin": 100.0,
        }
        with (
            pit_patch,
            patch(
                "custom_components.foxess_control.dt_util.utcnow",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 5),
            ),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 17, 0, 5))

        # Poll 1: establish observed rate baseline
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 75.0,
            "feedin": 100.3,
        }
        with (
            pit_patch,
            patch(
                "custom_components.foxess_control.dt_util.utcnow",
                return_value=datetime.datetime(2026, 4, 7, 17, 5, 0),
            ),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 17, 5, 0))

        # Poll 2: exported 1.0 kWh total → 2.0 kWh remaining.
        # observed_rate = (101.0 - 100.3) / 0.08333 = 8.4 kW
        # energy_next_poll = 8.4 * 0.08333 = 0.70 kWh
        # 2.0 > 0.70 → no early stop
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 70.0,
            "feedin": 101.0,
        }
        with (
            pit_patch,
            patch(
                "custom_components.foxess_control.dt_util.utcnow",
                return_value=datetime.datetime(2026, 4, 7, 17, 10, 0),
            ),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 17, 10, 0))

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state is not None
        assert state["last_power_w"] == 10500  # Unchanged
        # No new point_in_time calls beyond the initial window end timer
        assert len(point_in_time_calls) == initial_point_calls


class TestFeedinBaselineDeferred:
    """Feed-in baseline must be captured by the listener, not at session start."""

    @pytest.mark.asyncio
    async def test_feedin_baseline_not_captured_at_session_start(self) -> None:
        """feedin_start_kwh should be None after session setup.

        The coordinator value at session start may be stale (e.g. last API
        poll was minutes ago). The listener captures the baseline on its
        first tick, by which time WS delivers fresh data.
        """
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0, "feedin": 776.1},
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
            ),
        ):
            await handler(
                _make_call(
                    {
                        "start_time": datetime.time(17, 0),
                        "end_time": datetime.time(20, 0),
                        "min_soc": 10,
                        "feedin_energy_limit_kwh": 1.0,
                    }
                )
            )

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["feedin_energy_limit_kwh"] == 1.0
        assert state["feedin_start_kwh"] is None, (
            "Baseline should be deferred to listener, not captured from "
            "potentially stale coordinator data at session start"
        )

        # Simulate WS delivering fresh data on first listener tick
        hass.data[DOMAIN]["entry1"]["coordinator"].data = {
            "SoC": 79.0,
            "feedin": 776.31,
        }
        assert captured_interval is not None
        await captured_interval(datetime.datetime(2026, 4, 7, 17, 0, 5))

        state = hass.data[DOMAIN]["_smart_discharge_state"]
        assert state["feedin_start_kwh"] == 776.31, (
            "Baseline should be captured from fresh WS data on first tick"
        )


class TestRemainingZeroCancels:
    """Tests for active cancellation when remaining time <= 0."""

    @pytest.mark.asyncio
    async def test_expired_window_cancels_charge_session(self) -> None:
        """When adjustment fires after window expired, it actively cancels."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={
                "SoC": 20.0,
                "loadsPower": 3.0,
                "pvPower": 0.0,
            },
        )

        captured_interval = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_interval
            captured_interval = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        assert captured_interval is not None
        assert hass.data[DOMAIN]["_smart_charge_state"]["charging_started"]

        # Simulate callback firing after window has expired
        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 6, 1, 0),
        ):
            await captured_interval(datetime.datetime(2026, 4, 7, 6, 1, 0))

        # Session should be cancelled
        assert hass.data[DOMAIN].get("_smart_charge_state") is None
        assert hass.data[DOMAIN]["_smart_charge_unsubs"] == []
        # Override should be removed (self_use called via _remove_mode_from_schedule)
        inv.self_use.assert_called()


class TestTransientApiErrorResilience:
    """Sessions must survive transient API errors (device offline, DNS timeout).

    Reproduces production incident 2026-04-17: a DNS outage caused
    set_schedule to raise FoxESSApiError(41935, "Device offline").
    The catch-all handler aborted the charge session instead of
    retrying on the next timer tick.
    """

    @pytest.mark.asyncio
    async def test_charge_survives_transient_api_error(self) -> None:
        """A single API error during charge adjustment must not abort the session."""
        from custom_components.foxess_control.foxess.client import FoxESSApiError

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={
                "SoC": 20.0,
                "loadsPower": 3.0,
                "pvPower": 0.0,
            },
        )

        captured_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        assert captured_callback is not None
        assert hass.data[DOMAIN]["_smart_charge_state"]["charging_started"]

        # Make set_schedule raise a transient API error (device offline)
        inv.set_schedule.side_effect = FoxESSApiError(
            41935, "Device offline, Please connect and retry"
        )

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 3, 0, 0),
        ):
            await captured_callback(datetime.datetime(2026, 4, 7, 3, 0, 0))

        # Session must still be alive
        assert hass.data[DOMAIN].get("_smart_charge_state") is not None

    @pytest.mark.asyncio
    async def test_discharge_survives_transient_api_error(self) -> None:
        """A single API error during discharge adjustment must not abort the session."""
        from custom_components.foxess_control.foxess.client import FoxESSApiError

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 80.0},
        )

        captured_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[5].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
                side_effect=capture_interval,
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
        assert hass.data[DOMAIN]["_smart_discharge_state"]["discharging_started"]

        # Make set_schedule raise a transient API error
        inv.set_schedule.side_effect = FoxESSApiError(
            41935, "Device offline, Please connect and retry"
        )

        with patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=datetime.datetime(2026, 4, 7, 18, 0, 0),
        ):
            await captured_callback(datetime.datetime(2026, 4, 7, 18, 0, 0))

        # Session must still be alive
        assert hass.data[DOMAIN].get("_smart_discharge_state") is not None

    @pytest.mark.asyncio
    async def test_charge_aborts_after_repeated_errors(self) -> None:
        """Persistent API errors (>= MAX_CONSECUTIVE_ADAPTER_ERRORS) must abort."""
        from custom_components.foxess_control import _register_services
        from custom_components.foxess_control.foxess.client import FoxESSApiError
        from custom_components.foxess_control.smart_battery.const import (
            MAX_CONSECUTIVE_ADAPTER_ERRORS,
        )

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={
                "SoC": 20.0,
                "loadsPower": 3.0,
                "pvPower": 0.0,
            },
        )

        captured_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        assert captured_callback is not None
        inv.set_schedule.side_effect = FoxESSApiError(41935, "Device offline")

        for i in range(MAX_CONSECUTIVE_ADAPTER_ERRORS):
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 3, i, 0),
            ):
                await captured_callback(datetime.datetime(2026, 4, 7, 3, i, 0))

        assert hass.data[DOMAIN].get("_smart_charge_state") is None


class TestStaleWorkModeAfterCleanupFailure:
    """Work mode must not stay stale when session abort's cleanup fails.

    Reproduces production bug 2026-04-17: DNS outage aborted the charge
    session.  The error handler called cancel_smart_charge (clearing
    _work_mode to None) then _remove_charge_override (API call to clean
    the schedule).  But the API was still down, so the removal also
    failed.  The next REST poll re-read ForceCharge from the still-dirty
    schedule, and the overview card showed "Force Charge" indefinitely.
    """

    @pytest.mark.asyncio
    async def test_clear_overrides_clears_work_mode_immediately(self) -> None:
        """clear_overrides must set _work_mode to None before awaiting API."""
        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {
            "groups": [
                {
                    "enable": 1,
                    "startHour": 2,
                    "startMinute": 0,
                    "endHour": 6,
                    "endMinute": 0,
                    "workMode": "ForceCharge",
                    "minSocOnGrid": 11,
                    "fdSoc": 11,
                    "fdPwr": 0,
                }
            ]
        }
        hass = _make_hass(
            inverter=inv,
            coordinator_data={"SoC": 50.0, "_work_mode": "ForceCharge"},
        )

        # Register the session-cancel hook (normally done by async_setup_entry)
        from custom_components.foxess_control import (
            _first_entry_id,
            _register_services,
        )

        def _on_session_cancel() -> None:
            entry_id = _first_entry_id(hass)
            coordinator = hass.data[DOMAIN][entry_id]["coordinator"]
            if coordinator.data is not None:
                coordinator.data["_work_mode"] = None

        hass.data[DOMAIN]["_on_session_cancel"] = _on_session_cancel

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[0].args[2]

        await handler(_make_call({}))

        coordinator = hass.data[DOMAIN]["entry1"]["coordinator"]
        assert coordinator.data["_work_mode"] is None

    @pytest.mark.asyncio
    async def test_failed_cleanup_schedules_pending_retry(self) -> None:
        """When override removal fails during abort, a retry must be pending."""
        from custom_components.foxess_control.foxess.client import FoxESSApiError
        from custom_components.foxess_control.smart_battery.const import (
            MAX_CONSECUTIVE_ADAPTER_ERRORS,
        )

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        inv.get_schedule.return_value = {"enable": 0, "groups": []}
        hass = _make_hass(
            inverter=inv,
            battery_capacity_kwh=60.0,
            coordinator_data={
                "SoC": 20.0,
                "loadsPower": 3.0,
                "pvPower": 0.0,
                "_work_mode": "ForceCharge",
            },
        )

        captured_callback = None

        def capture_interval(_hass: Any, callback: Any, _interval: Any) -> MagicMock:
            nonlocal captured_callback
            captured_callback = callback
            return MagicMock()

        from custom_components.foxess_control import _register_services

        _register_services(hass)
        handler = hass.services.async_register.call_args_list[4].args[2]

        with (
            patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 2, 0, 0),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_point_in_time",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.foxess_control.smart_battery.listeners.async_track_time_interval",
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

        assert captured_callback is not None
        assert hass.data[DOMAIN]["_smart_charge_state"]["charging_started"]

        # Make ALL API calls fail (simulates DNS outage)
        inv.set_schedule.side_effect = FoxESSApiError(41935, "Device offline")
        inv.self_use.side_effect = FoxESSApiError(41935, "Device offline")

        # Fire enough errors to abort the session — cleanup also fails
        for i in range(MAX_CONSECUTIVE_ADAPTER_ERRORS):
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 3, i, 0),
            ):
                await captured_callback(datetime.datetime(2026, 4, 7, 3, i, 0))

        # Session is aborted
        assert hass.data[DOMAIN].get("_smart_charge_state") is None

        # A pending cleanup must be stored so the coordinator can retry
        pending = hass.data[DOMAIN].get("_pending_override_cleanup")
        assert pending is not None
        assert pending["mode"] == "ForceCharge"
