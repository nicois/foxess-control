"""Tests for entity-mode (foxess_modbus interop) functionality."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.foxess_control import (
    _apply_mode_via_entities,
    _async_remove_override,
    _build_entity_map,
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
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
    build_config,
)
from custom_components.foxess_control.foxess.inverter import WorkMode


def _make_entity_hass(entry_options: dict[str, Any]) -> MagicMock:
    """Create a mock hass configured for entity mode tests."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData()
    dd.config = build_config(entry_options)
    hass.data = {DOMAIN: dd}
    entry = MagicMock()
    entry.options = entry_options
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    return hass


class TestIsEntityMode:
    """Tests for entity_mode via IntegrationConfig."""

    def test_returns_false_when_no_work_mode_entity(self) -> None:
        cfg = build_config({})
        assert cfg.entity_mode is False

    def test_returns_true_when_work_mode_entity_set(self) -> None:
        cfg = build_config({CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"})
        assert cfg.entity_mode is True

    def test_returns_false_when_empty_string(self) -> None:
        cfg = build_config({CONF_WORK_MODE_ENTITY: ""})
        assert cfg.entity_mode is False


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
        hass = _make_entity_hass({CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"})

        await _apply_mode_via_entities(hass, WorkMode.SELF_USE)

        hass.services.async_call.assert_called_once_with(
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Self Use"},
        )

    @pytest.mark.asyncio
    async def test_sets_charge_power(self) -> None:
        hass = _make_entity_hass(
            {
                CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
                CONF_CHARGE_POWER_ENTITY: "number.foxess_charge_power",
            }
        )

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
        hass = _make_entity_hass(
            {
                CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
                CONF_DISCHARGE_POWER_ENTITY: "number.foxess_discharge_power",
                CONF_MIN_SOC_ENTITY: "number.foxess_min_soc",
            }
        )

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
        hass = _make_entity_hass(
            {
                CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
                CONF_CHARGE_POWER_ENTITY: "number.foxess_charge_power",
            }
        )

        await _apply_mode_via_entities(hass, WorkMode.FEEDIN, power_w=5000)

        # Only work mode call, power skipped for Feedin
        assert hass.services.async_call.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_power_when_no_entity_configured(self) -> None:
        hass = _make_entity_hass({CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"})

        await _apply_mode_via_entities(hass, WorkMode.FORCE_CHARGE, power_w=5000)

        # Only work mode call, no charge_power_entity configured
        assert hass.services.async_call.call_count == 1

    @pytest.mark.asyncio
    async def test_input_select_uses_input_select_domain(self) -> None:
        """input_select entities must use the input_select service domain."""
        hass = _make_entity_hass(
            {CONF_WORK_MODE_ENTITY: "input_select.foxess_work_mode"}
        )

        await _apply_mode_via_entities(hass, WorkMode.SELF_USE)

        hass.services.async_call.assert_called_once_with(
            "input_select",
            "select_option",
            {"entity_id": "input_select.foxess_work_mode", "option": "Self Use"},
        )

    @pytest.mark.asyncio
    async def test_input_number_uses_input_number_domain(self) -> None:
        """input_number entities must use the input_number service domain."""
        hass = _make_entity_hass(
            {
                CONF_WORK_MODE_ENTITY: "input_select.foxess_work_mode",
                CONF_DISCHARGE_POWER_ENTITY: "input_number.foxess_discharge_power",
                CONF_MIN_SOC_ENTITY: "input_number.foxess_min_soc",
            }
        )

        await _apply_mode_via_entities(
            hass, WorkMode.FORCE_DISCHARGE, power_w=3000, fd_soc=15
        )

        calls = hass.services.async_call.call_args_list
        assert calls[0].args[0] == "input_select"
        assert calls[1].args[0] == "input_number"
        assert calls[2].args[0] == "input_number"

    @pytest.mark.asyncio
    async def test_platform_select_uses_select_domain(self) -> None:
        """Platform-backed select entities use the select service domain."""
        hass = _make_entity_hass(
            {
                CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
                CONF_DISCHARGE_POWER_ENTITY: "number.foxess_discharge_power",
            }
        )

        await _apply_mode_via_entities(hass, WorkMode.FORCE_DISCHARGE, power_w=3000)

        calls = hass.services.async_call.call_args_list
        assert calls[0].args[0] == "select"
        assert calls[1].args[0] == "number"


class TestAsyncRemoveOverride:
    """Tests for _async_remove_override."""

    @pytest.mark.asyncio
    async def test_entity_mode_sets_self_use(self) -> None:
        hass = _make_entity_hass({CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"})

        await _async_remove_override(hass, WorkMode.FORCE_CHARGE)

        hass.services.async_call.assert_called_once_with(
            "select",
            "select_option",
            {"entity_id": "select.foxess_work_mode", "option": "Self Use"},
        )

    @pytest.mark.asyncio
    async def test_cloud_mode_calls_remove_from_schedule(self) -> None:
        from custom_components.foxess_control.foxess.inverter import Inverter

        inverter = MagicMock(spec=Inverter)
        inverter.max_power_w = 10500
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock()
        dd = FoxESSControlData()
        dd.entries["entry1"] = FoxESSEntryData(inverter=inverter)
        dd.config = build_config({}, inverter_max_power_w=10500)
        hass.data = {DOMAIN: dd}
        entry = MagicMock()
        entry.options = {}
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

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
