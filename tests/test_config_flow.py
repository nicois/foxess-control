"""Tests for config flow and options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from custom_components.foxess_control.config_flow import (
    FoxessControlConfigFlow,
    FoxessControlOptionsFlow,
    _detect_foxess_modbus_entities,
)
from custom_components.foxess_control.const import (
    CONF_API_KEY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGE_POWER_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    CONF_SOC_ENTITY,
    CONF_WORK_MODE_ENTITY,
)
from custom_components.foxess_control.foxess.client import FoxESSApiError


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    return hass


class TestConfigFlow:
    """Tests for FoxessControlConfigFlow."""

    @pytest.mark.asyncio
    async def test_successful_setup(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"},
        )

        user_input = {CONF_API_KEY: "key123", CONF_DEVICE_SERIAL: "SN001"}

        with patch(
            "custom_components.foxess_control.config_flow._validate_credentials"
        ):
            await flow.async_step_user(user_input)

        flow.async_create_entry.assert_called_once()
        call_kwargs = flow.async_create_entry.call_args
        assert call_kwargs.kwargs["title"] == "FoxESS SN001"
        assert call_kwargs.kwargs["data"] == user_input

    @pytest.mark.asyncio
    async def test_api_error_shows_invalid_auth(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        user_input = {CONF_API_KEY: "bad-key", CONF_DEVICE_SERIAL: "SN001"}

        with patch(
            "custom_components.foxess_control.config_flow._validate_credentials",
            side_effect=FoxESSApiError(41809, "Token invalid"),
        ):
            await flow.async_step_user(user_input)

        flow.async_show_form.assert_called_once()
        errors: dict[str, str] = flow.async_show_form.call_args.kwargs["errors"]
        assert errors["base"] == "invalid_auth"

    @pytest.mark.asyncio
    async def test_network_error_shows_cannot_connect(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        user_input = {CONF_API_KEY: "key123", CONF_DEVICE_SERIAL: "SN001"}

        with patch(
            "custom_components.foxess_control.config_flow._validate_credentials",
            side_effect=requests.ConnectionError("DNS failure"),
        ):
            await flow.async_step_user(user_input)

        flow.async_show_form.assert_called_once()
        errors: dict[str, str] = flow.async_show_form.call_args.kwargs["errors"]
        assert errors["base"] == "cannot_connect"

    @pytest.mark.asyncio
    async def test_unexpected_error_propagates(self) -> None:
        """Programming errors should not be caught."""
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()

        user_input = {CONF_API_KEY: "key123", CONF_DEVICE_SERIAL: "SN001"}

        with (
            patch(
                "custom_components.foxess_control.config_flow._validate_credentials",
                side_effect=TypeError("bug"),
            ),
            pytest.raises(TypeError, match="bug"),
        ):
            await flow.async_step_user(user_input)

    @pytest.mark.asyncio
    async def test_show_form_when_no_input(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        await flow.async_step_user(None)

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["errors"] == {}


def _make_options_flow(
    options: dict[str, object] | None = None,
    has_modbus: bool = False,
) -> FoxessControlOptionsFlow:
    """Create an options flow with mocked hass."""
    config_entry = MagicMock()
    config_entry.options = options or {}

    hass = MagicMock()
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda domain: (
            [MagicMock()] if domain == "foxess_modbus" and has_modbus else []
        )
    )

    flow = FoxessControlOptionsFlow(config_entry)
    flow.hass = hass
    return flow


class TestOptionsFlow:
    """Tests for FoxessControlOptionsFlow."""

    @pytest.mark.asyncio
    async def test_creates_entry_with_input(self) -> None:
        flow = _make_options_flow({CONF_MIN_SOC_ON_GRID: 20})
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"},
        )

        await flow.async_step_init({CONF_MIN_SOC_ON_GRID: 25})

        flow.async_create_entry.assert_called_once_with(data={CONF_MIN_SOC_ON_GRID: 25})

    @pytest.mark.asyncio
    async def test_shows_form_with_current_value(self) -> None:
        flow = _make_options_flow({CONF_MIN_SOC_ON_GRID: 30})
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        await flow.async_step_init(None)

        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_value_when_no_option(self) -> None:
        flow = _make_options_flow({})
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        await flow.async_step_init(None)

        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_saves_battery_capacity(self) -> None:
        flow = _make_options_flow(
            {
                CONF_MIN_SOC_ON_GRID: 20,
                CONF_BATTERY_CAPACITY_KWH: 0.0,
                CONF_MIN_POWER_CHANGE: 500,
            }
        )
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"},
        )

        await flow.async_step_init(
            {
                CONF_MIN_SOC_ON_GRID: 20,
                CONF_BATTERY_CAPACITY_KWH: 10.5,
                CONF_MIN_POWER_CHANGE: 300,
            }
        )

        flow.async_create_entry.assert_called_once()
        data = flow.async_create_entry.call_args.kwargs["data"]
        assert data[CONF_BATTERY_CAPACITY_KWH] == 10.5
        assert data[CONF_MIN_POWER_CHANGE] == 300

    @pytest.mark.asyncio
    async def test_no_modbus_clears_stale_entity_options(self) -> None:
        """When foxess_modbus is removed, entity options are cleared."""
        flow = _make_options_flow(
            {
                CONF_MIN_SOC_ON_GRID: 20,
                CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            }
        )
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"},
        )

        await flow.async_step_init(
            {
                CONF_MIN_SOC_ON_GRID: 20,
                CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            }
        )

        data = flow.async_create_entry.call_args.kwargs["data"]
        assert CONF_WORK_MODE_ENTITY not in data

    @pytest.mark.asyncio
    async def test_modbus_detected_shows_modbus_step(self) -> None:
        """When foxess_modbus is installed, init proceeds to modbus step."""
        flow = _make_options_flow({CONF_MIN_SOC_ON_GRID: 20}, has_modbus=True)
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        with patch(
            "custom_components.foxess_control.config_flow._detect_foxess_modbus_entities",
            return_value={},
        ):
            await flow.async_step_init({CONF_MIN_SOC_ON_GRID: 25})

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "modbus"

    @pytest.mark.asyncio
    async def test_modbus_step_merges_data(self) -> None:
        """Modbus step merges entity choices with init data."""
        flow = _make_options_flow({}, has_modbus=True)
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"},
        )

        # Simulate init step storing data
        flow._init_data = {CONF_MIN_SOC_ON_GRID: 20}

        await flow.async_step_modbus(
            {
                CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            }
        )

        data = flow.async_create_entry.call_args.kwargs["data"]
        assert data[CONF_MIN_SOC_ON_GRID] == 20
        assert data[CONF_WORK_MODE_ENTITY] == "select.foxess_work_mode"


class TestDetectFoxessModbusEntities:
    """Tests for _detect_foxess_modbus_entities."""

    def test_no_modbus_entries(self) -> None:
        hass = MagicMock()
        hass.config_entries.async_entries = MagicMock(return_value=[])
        assert _detect_foxess_modbus_entities(hass) == {}

    def test_detects_entities_by_original_name(self) -> None:
        hass = MagicMock()
        modbus_entry = MagicMock()
        modbus_entry.entry_id = "modbus1"
        hass.config_entries.async_entries = MagicMock(return_value=[modbus_entry])

        work_mode = MagicMock()
        work_mode.original_name = "Work Mode"
        work_mode.entity_id = "select.foxess_inv1_work_mode"

        soc = MagicMock()
        soc.original_name = "Battery SoC"
        soc.entity_id = "sensor.foxess_inv1_battery_soc"

        charge_power = MagicMock()
        charge_power.original_name = "Force Charge Power"
        charge_power.entity_id = "number.foxess_inv1_force_charge_power"

        unrelated = MagicMock()
        unrelated.original_name = "Inverter Temperature"
        unrelated.entity_id = "sensor.foxess_inv1_inverter_temp"

        with (
            patch(
                "custom_components.foxess_control.smart_battery.config_flow_base.er.async_get"
            ),
            patch(
                "custom_components.foxess_control.smart_battery.config_flow_base.er.async_entries_for_config_entry",
                return_value=[work_mode, soc, charge_power, unrelated],
            ),
        ):
            result = _detect_foxess_modbus_entities(hass)

        assert result == {
            CONF_WORK_MODE_ENTITY: "select.foxess_inv1_work_mode",
            CONF_SOC_ENTITY: "sensor.foxess_inv1_battery_soc",
            CONF_CHARGE_POWER_ENTITY: "number.foxess_inv1_force_charge_power",
        }
