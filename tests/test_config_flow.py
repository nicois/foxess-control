"""Tests for config flow and options flow."""

from __future__ import annotations

from typing import Any
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
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
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
    async def test_successful_setup_skipping_web_creds(self) -> None:
        """API key step → web credentials step (skip) → entry created."""
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"},
        )
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        user_input = {CONF_API_KEY: "key123", CONF_DEVICE_SERIAL: "SN001"}

        with patch(
            "custom_components.foxess_control.config_flow._validate_credentials"
        ):
            # Step 1: API credentials — redirects to web_credentials step.
            # async_step_user is now a router; the api-key form lives in
            # async_step_cloud (step_id "cloud"), so call it directly.
            await flow.async_step_cloud(user_input)

        # Step 1 shows the web_credentials form
        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "web_credentials"

        # Step 2: Skip web credentials (empty fields)
        flow.async_show_form.reset_mock()
        await flow.async_step_web_credentials({"web_username": "", "web_password": ""})

        flow.async_create_entry.assert_called_once()
        call_kwargs = flow.async_create_entry.call_args
        assert call_kwargs.kwargs["title"] == "FoxESS SN001"
        # Data should have API key and serial but no web credentials
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
            await flow.async_step_cloud(user_input)

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "cloud"
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
            await flow.async_step_cloud(user_input)

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "cloud"
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
            await flow.async_step_cloud(user_input)

    @pytest.mark.asyncio
    async def test_show_form_when_no_input(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        await flow.async_step_cloud(None)

        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "cloud"
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


class TestReconfigureFlow:
    """Tests for reconfigure (updating web credentials on existing entry)."""

    @pytest.mark.asyncio
    async def test_reconfigure_adds_web_credentials(self) -> None:
        """Reconfigure step updates entry data with web credentials."""
        existing_data = {CONF_API_KEY: "key123", CONF_DEVICE_SERIAL: "SN001"}
        entry = MagicMock()
        entry.data = existing_data
        entry.entry_id = "test_entry_id"

        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()
        flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
        flow.context = {"entry_id": "test_entry_id", "source": "reconfigure"}
        flow._get_reconfigure_entry = MagicMock(return_value=entry)
        flow.async_update_reload_and_abort = MagicMock(
            return_value={"type": "abort", "reason": "reconfigure_successful"},
        )
        flow.async_show_form = MagicMock(return_value={"type": "form"})

        # Step 1: Show form (no input)
        await flow.async_step_reconfigure(None)
        flow.async_show_form.assert_called_once()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "web_credentials"

        # Step 2: Submit credentials (skip validation for unit test)
        with patch.object(flow, "_validate_web_credentials", return_value={}):
            await flow.async_step_web_credentials(
                {CONF_WEB_USERNAME: "user@fox.com", CONF_WEB_PASSWORD: "secret"}
            )

        flow.async_update_reload_and_abort.assert_called_once()
        new_data = flow.async_update_reload_and_abort.call_args.kwargs["data"]
        assert new_data[CONF_API_KEY] == "key123"
        assert new_data[CONF_WEB_USERNAME] == "user@fox.com"
        assert new_data[CONF_WEB_PASSWORD] != "secret"  # hashed

    @pytest.mark.asyncio
    async def test_reconfigure_clears_web_credentials(self) -> None:
        """Submitting empty fields removes web credentials."""
        existing_data = {
            CONF_API_KEY: "key123",
            CONF_DEVICE_SERIAL: "SN001",
            CONF_WEB_USERNAME: "old@fox.com",
            CONF_WEB_PASSWORD: "oldhash",
        }
        entry = MagicMock()
        entry.data = existing_data
        entry.entry_id = "test_entry_id"

        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass()
        flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
        flow.context = {"entry_id": "test_entry_id", "source": "reconfigure"}
        flow._get_reconfigure_entry = MagicMock(return_value=entry)
        flow.async_update_reload_and_abort = MagicMock(
            return_value={"type": "abort", "reason": "reconfigure_successful"},
        )

        await flow.async_step_reconfigure(
            {CONF_WEB_USERNAME: "", CONF_WEB_PASSWORD: ""}
        )

        new_data = flow.async_update_reload_and_abort.call_args.kwargs["data"]
        assert CONF_WEB_USERNAME not in new_data
        assert CONF_WEB_PASSWORD not in new_data
        assert new_data[CONF_API_KEY] == "key123"


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

    def test_detects_max_grid_export_limit(self) -> None:
        """_MODBUS_NAME_MAP maps 'Max Grid Export Limit' to the config key."""
        from custom_components.foxess_control.const import CONF_EXPORT_LIMIT_ENTITY

        hass = MagicMock()
        modbus_entry = MagicMock()
        modbus_entry.entry_id = "modbus1"
        hass.config_entries.async_entries = MagicMock(return_value=[modbus_entry])

        export_limit = MagicMock()
        export_limit.original_name = "Max Grid Export Limit"
        export_limit.entity_id = "number.foxess_inv1_max_grid_export_limit"

        with (
            patch(
                "custom_components.foxess_control.smart_battery.config_flow_base.er.async_get"
            ),
            patch(
                "custom_components.foxess_control.smart_battery.config_flow_base.er.async_entries_for_config_entry",
                return_value=[export_limit],
            ),
        ):
            result = _detect_foxess_modbus_entities(hass)

        assert result == {
            CONF_EXPORT_LIMIT_ENTITY: ("number.foxess_inv1_max_grid_export_limit"),
        }


def _make_hass_modbus(has_modbus: bool) -> MagicMock:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.config_entries.async_entries = MagicMock(
        side_effect=lambda domain: (
            [MagicMock()] if domain == "foxess_modbus" and has_modbus else []
        )
    )
    return hass


class TestConfigFlowRouting:
    @pytest.mark.asyncio
    async def test_user_step_shows_menu_when_modbus_present(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass_modbus(True)
        flow.async_show_menu = MagicMock(return_value={"type": "menu"})
        result = await flow.async_step_user(None)
        assert result["type"] == "menu"
        # cloud + entity options offered
        _, kwargs = flow.async_show_menu.call_args
        assert set(kwargs["menu_options"]) == {"cloud", "entity"}

    @pytest.mark.asyncio
    async def test_user_step_goes_straight_to_cloud_when_no_modbus(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass_modbus(False)
        flow.async_show_form = MagicMock(
            return_value={"type": "form", "step_id": "cloud"}
        )
        result = await flow.async_step_user(None)
        # No menu; the cloud (api-key) form is shown directly.
        assert result["type"] == "form"
        _, kwargs = flow.async_show_form.call_args
        assert kwargs["step_id"] == "cloud"


class TestConfigFlowEntityBranch:
    @pytest.mark.asyncio
    async def test_entity_step_shows_mapping_form(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass_modbus(True)
        flow.async_show_form = MagicMock(
            return_value={"type": "form", "step_id": "entity"}
        )
        with patch(
            "custom_components.foxess_control.config_flow._detect_foxess_modbus_entities",
            return_value={CONF_WORK_MODE_ENTITY: "select.wm"},
        ):
            result = await flow.async_step_entity(None)
        assert result["type"] == "form"
        _, kwargs = flow.async_show_form.call_args
        assert kwargs["step_id"] == "entity"

    @pytest.mark.asyncio
    async def test_entity_then_battery_creates_entry_without_api_key(self) -> None:
        flow = FoxessControlConfigFlow()
        flow.hass = _make_hass_modbus(True)
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        created: dict[str, Any] = {}

        def _create(**kw: Any) -> dict[str, str]:
            created.update(kw)
            return {"type": "create_entry"}

        flow.async_create_entry = MagicMock(side_effect=_create)
        await flow.async_step_entity(
            {CONF_WORK_MODE_ENTITY: "select.wm", CONF_SOC_ENTITY: "sensor.soc"}
        )
        await flow.async_step_entity_battery(
            {
                CONF_BATTERY_CAPACITY_KWH: 10.0,
                CONF_MIN_SOC_ON_GRID: 11,
                CONF_MIN_POWER_CHANGE: 200,
            }
        )
        assert created
        # api_key absent from BOTH data and options
        assert CONF_API_KEY not in created.get("data", {})
        assert CONF_API_KEY not in created.get("options", {})
        # entity mode active: work-mode entity is in OPTIONS (where
        # IntegrationConfig reads it)
        assert created["options"][CONF_WORK_MODE_ENTITY] == "select.wm"
        # battery option carried in options too
        assert created["options"][CONF_BATTERY_CAPACITY_KWH] == 10.0


class TestSchemaBuildersNoEntry:
    """entity_mapping_schema / battery_options_schema must work at setup time
    (no config entry exists yet)."""

    def test_entity_mapping_schema_accepts_none_entry(self) -> None:
        from custom_components.foxess_control.smart_battery.config_flow_base import (
            entity_mapping_schema,
        )

        schema = entity_mapping_schema(None, {CONF_WORK_MODE_ENTITY: "select.wm"})
        # Read declared defaults directly from the schema markers. We cannot call
        # schema({}) here because the other entity fields default to "" and the
        # EntitySelector validator rejects empty strings (this is true for any
        # empty-options entry, not specific to config_entry=None). The intent is
        # to verify the detected work-mode value flows through as the default.
        defaults = {
            str(key): key.default()
            for key in schema.schema
            if getattr(key, "default", None) is not None
        }
        assert defaults[CONF_WORK_MODE_ENTITY] == "select.wm"

    def test_battery_options_schema_accepts_none_entry(self) -> None:
        from custom_components.foxess_control.smart_battery.config_flow_base import (
            battery_options_schema,
        )

        schema = battery_options_schema(None)
        defaults = schema({})
        assert CONF_MIN_SOC_ON_GRID in defaults
        assert CONF_BATTERY_CAPACITY_KWH in defaults


def test_additional_pv_schema_dict_exposes_key() -> None:
    """The AC-coupled additional-PV field appears in the options schema fragment."""
    from custom_components.foxess_control.config_flow import (
        _additional_pv_schema_dict,
    )
    from custom_components.foxess_control.const import (
        CONF_ADDITIONAL_PV_POWER_VARIABLE,
    )

    schema_keys = {str(k) for k in _additional_pv_schema_dict({})}
    assert CONF_ADDITIONAL_PV_POWER_VARIABLE in schema_keys


def test_additional_pv_schema_dict_defaults_to_current_value() -> None:
    """The field defaults to the currently-saved option value."""
    import voluptuous as vol

    from custom_components.foxess_control.config_flow import (
        _additional_pv_schema_dict,
    )
    from custom_components.foxess_control.const import (
        CONF_ADDITIONAL_PV_POWER_VARIABLE,
    )

    frag = _additional_pv_schema_dict(
        {CONF_ADDITIONAL_PV_POWER_VARIABLE: "meterPower2"}
    )
    # Build a schema and confirm the default round-trips.
    schema = vol.Schema(frag)
    # An empty submission should fill the default for the optional key.
    result = schema({})
    assert result.get(CONF_ADDITIONAL_PV_POWER_VARIABLE) == "meterPower2"
