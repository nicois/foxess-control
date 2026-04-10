"""Tests for entity-mode (foxess_modbus interop) functionality."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.foxess_control import (
    _apply_mode_via_entities,
    _async_remove_override,
    _build_entity_map,
    _is_entity_mode,
)
from custom_components.foxess_control.const import (
    CONF_CHARGE_POWER_ENTITY,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_FEEDIN_ENERGY_ENTITY,
    CONF_LOADS_POWER_ENTITY,
    CONF_MIN_SOC_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SOC_ENTITY,
    CONF_WORK_MODE_ENTITY,
    DOMAIN,
)
from custom_components.foxess_control.coordinator import FoxESSEntityCoordinator
from custom_components.foxess_control.foxess.inverter import WorkMode


class TestIsEntityMode:
    """Tests for _is_entity_mode."""

    def test_returns_false_when_no_work_mode_entity(self) -> None:
        hass = MagicMock()
        entry = MagicMock()
        entry.options = {}
        hass.data = {DOMAIN: {"entry1": {"inverter": MagicMock()}}}
        hass.config_entries.async_get_entry.return_value = entry
        assert _is_entity_mode(hass) is False

    def test_returns_true_when_work_mode_entity_set(self) -> None:
        hass = MagicMock()
        entry = MagicMock()
        entry.options = {CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"}
        hass.data = {DOMAIN: {"entry1": {"inverter": MagicMock()}}}
        hass.config_entries.async_get_entry.return_value = entry
        assert _is_entity_mode(hass) is True

    def test_returns_false_when_empty_string(self) -> None:
        hass = MagicMock()
        entry = MagicMock()
        entry.options = {CONF_WORK_MODE_ENTITY: ""}
        hass.data = {DOMAIN: {"entry1": {"inverter": MagicMock()}}}
        hass.config_entries.async_get_entry.return_value = entry
        assert _is_entity_mode(hass) is False

    def test_returns_false_when_no_domain_data(self) -> None:
        hass = MagicMock()
        hass.data = {}
        assert _is_entity_mode(hass) is False


class TestBuildEntityMap:
    """Tests for _build_entity_map."""

    def test_empty_when_no_work_mode(self) -> None:
        assert _build_entity_map({}) == {}

    def test_empty_when_work_mode_empty_string(self) -> None:
        assert _build_entity_map({CONF_WORK_MODE_ENTITY: ""}) == {}

    def test_maps_all_configured_entities(self) -> None:
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_SOC_ENTITY: "sensor.foxess_soc",
            CONF_LOADS_POWER_ENTITY: "sensor.foxess_loads",
            CONF_PV_POWER_ENTITY: "sensor.foxess_pv",
            CONF_FEEDIN_ENERGY_ENTITY: "sensor.foxess_feedin",
        }
        result = _build_entity_map(opts)
        assert result == {
            "_work_mode": "select.foxess_work_mode",
            "SoC": "sensor.foxess_soc",
            "loadsPower": "sensor.foxess_loads",
            "pvPower": "sensor.foxess_pv",
            "feedin": "sensor.foxess_feedin",
        }

    def test_omits_unconfigured_entities(self) -> None:
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_SOC_ENTITY: "sensor.foxess_soc",
        }
        result = _build_entity_map(opts)
        assert "_work_mode" in result
        assert "SoC" in result
        assert "loadsPower" not in result
        assert "feedin" not in result


class TestApplyModeViaEntities:
    """Tests for _apply_mode_via_entities."""

    @pytest.mark.asyncio
    async def test_sets_work_mode(self) -> None:
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        entry = MagicMock()
        entry.options = {CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"}
        hass.data = {DOMAIN: {"entry1": {}}}
        hass.config_entries.async_get_entry.return_value = entry

        await _apply_mode_via_entities(hass, WorkMode.SELF_USE)

        hass.services.async_call.assert_called_once_with(
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Self Use"},
        )

    @pytest.mark.asyncio
    async def test_sets_charge_power(self) -> None:
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        entry = MagicMock()
        entry.options = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_CHARGE_POWER_ENTITY: "number.foxess_charge_power",
        }
        hass.data = {DOMAIN: {"entry1": {}}}
        hass.config_entries.async_get_entry.return_value = entry

        await _apply_mode_via_entities(hass, WorkMode.FORCE_CHARGE, power_w=5000)

        calls = hass.services.async_call.call_args_list
        assert len(calls) == 2
        # First call: set work mode
        assert calls[0].args == (
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Force Charge"},
        )
        # Second call: set power
        assert calls[1].args == (
            "number",
            "set_value",
            {"entity_id": "number.foxess_charge_power", "value": 5000},
        )

    @pytest.mark.asyncio
    async def test_sets_discharge_power_and_min_soc(self) -> None:
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        entry = MagicMock()
        entry.options = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_DISCHARGE_POWER_ENTITY: "number.foxess_discharge_power",
            CONF_MIN_SOC_ENTITY: "number.foxess_min_soc",
        }
        hass.data = {DOMAIN: {"entry1": {}}}
        hass.config_entries.async_get_entry.return_value = entry

        await _apply_mode_via_entities(
            hass,
            WorkMode.FORCE_DISCHARGE,
            power_w=3000,
            fd_soc=15,
        )

        calls = hass.services.async_call.call_args_list
        assert len(calls) == 3
        # Work mode
        assert calls[0].args[2]["option"] == "Force Discharge"
        # Power
        assert calls[1].args[2]["value"] == 3000
        # Min SoC
        assert calls[2].args[2]["value"] == 15

    @pytest.mark.asyncio
    async def test_skips_power_when_not_charge_or_discharge(self) -> None:
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        entry = MagicMock()
        entry.options = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_CHARGE_POWER_ENTITY: "number.foxess_charge_power",
        }
        hass.data = {DOMAIN: {"entry1": {}}}
        hass.config_entries.async_get_entry.return_value = entry

        await _apply_mode_via_entities(hass, WorkMode.FEEDIN, power_w=5000)

        # Only work mode call, power skipped for Feedin
        assert hass.services.async_call.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_power_when_no_entity_configured(self) -> None:
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        entry = MagicMock()
        entry.options = {CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"}
        hass.data = {DOMAIN: {"entry1": {}}}
        hass.config_entries.async_get_entry.return_value = entry

        await _apply_mode_via_entities(hass, WorkMode.FORCE_CHARGE, power_w=5000)

        # Only work mode call, no charge_power_entity configured
        assert hass.services.async_call.call_count == 1


class TestAsyncRemoveOverride:
    """Tests for _async_remove_override."""

    @pytest.mark.asyncio
    async def test_entity_mode_sets_self_use(self) -> None:
        hass = MagicMock()
        hass.services.async_call = AsyncMock()
        entry = MagicMock()
        entry.options = {CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"}
        hass.data = {DOMAIN: {"entry1": {}}}
        hass.config_entries.async_get_entry.return_value = entry

        await _async_remove_override(hass, WorkMode.FORCE_CHARGE)

        hass.services.async_call.assert_called_once_with(
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Self Use"},
        )

    @pytest.mark.asyncio
    async def test_cloud_mode_calls_remove_from_schedule(self) -> None:
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock()
        entry = MagicMock()
        entry.options = {}
        inverter = MagicMock()
        hass.data = {DOMAIN: {"entry1": {"inverter": inverter}}}
        hass.config_entries.async_get_entry.return_value = entry

        await _async_remove_override(hass, WorkMode.FORCE_CHARGE)

        hass.async_add_executor_job.assert_called_once()


class TestEntityCoordinator:
    """Tests for FoxESSEntityCoordinator."""

    @pytest.mark.asyncio
    async def test_reads_entity_states(self) -> None:
        hass = MagicMock()

        soc_state = MagicMock()
        soc_state.state = "75.5"
        loads_state = MagicMock()
        loads_state.state = "1.2"
        work_mode_state = MagicMock()
        work_mode_state.state = "Self Use"

        def get_state(entity_id: str) -> Any:
            return {
                "sensor.foxess_soc": soc_state,
                "sensor.foxess_loads": loads_state,
                "select.foxess_work_mode": work_mode_state,
            }.get(entity_id)

        hass.states.get = get_state

        entity_map = {
            "SoC": "sensor.foxess_soc",
            "loadsPower": "sensor.foxess_loads",
            "_work_mode": "select.foxess_work_mode",
        }

        with patch(
            "custom_components.foxess_control.coordinator."
            "DataUpdateCoordinator.__init__"
        ):
            coord = FoxESSEntityCoordinator.__new__(FoxESSEntityCoordinator)
            coord.hass = hass
            coord._entity_map = entity_map

        data = await coord._async_update_data()
        assert data["SoC"] == 75.5
        assert data["loadsPower"] == 1.2
        assert data["_work_mode"] == "Self Use"

    @pytest.mark.asyncio
    async def test_skips_unavailable_entities(self) -> None:
        hass = MagicMock()

        unavail_state = MagicMock()
        unavail_state.state = "unavailable"
        soc_state = MagicMock()
        soc_state.state = "50.0"

        def get_state(entity_id: str) -> Any:
            return {
                "sensor.foxess_soc": soc_state,
                "sensor.foxess_loads": unavail_state,
            }.get(entity_id)

        hass.states.get = get_state

        entity_map = {
            "SoC": "sensor.foxess_soc",
            "loadsPower": "sensor.foxess_loads",
        }

        with patch(
            "custom_components.foxess_control.coordinator."
            "DataUpdateCoordinator.__init__"
        ):
            coord = FoxESSEntityCoordinator.__new__(FoxESSEntityCoordinator)
            coord.hass = hass
            coord._entity_map = entity_map

        data = await coord._async_update_data()
        assert data["SoC"] == 50.0
        assert "loadsPower" not in data
        assert data["_work_mode"] is None

    @pytest.mark.asyncio
    async def test_missing_entity_returns_none_for_work_mode(self) -> None:
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)

        entity_map = {"_work_mode": "select.foxess_nonexistent"}

        with patch(
            "custom_components.foxess_control.coordinator."
            "DataUpdateCoordinator.__init__"
        ):
            coord = FoxESSEntityCoordinator.__new__(FoxESSEntityCoordinator)
            coord.hass = hass
            coord._entity_map = entity_map

        data = await coord._async_update_data()
        assert data["_work_mode"] is None
