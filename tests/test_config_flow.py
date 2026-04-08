"""Tests for config flow and options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from custom_components.foxess_control.config_flow import (
    FoxessControlConfigFlow,
    FoxessControlOptionsFlow,
)
from custom_components.foxess_control.const import (
    CONF_API_KEY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_MIN_SOC_ON_GRID,
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


class TestOptionsFlow:
    """Tests for FoxessControlOptionsFlow."""

    @pytest.mark.asyncio
    async def test_creates_entry_with_input(self) -> None:
        config_entry = MagicMock()
        config_entry.options = {CONF_MIN_SOC_ON_GRID: 20}

        flow = FoxessControlOptionsFlow(config_entry)
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"},
        )

        await flow.async_step_init({CONF_MIN_SOC_ON_GRID: 25})

        flow.async_create_entry.assert_called_once_with(data={CONF_MIN_SOC_ON_GRID: 25})

    @pytest.mark.asyncio
    async def test_shows_form_with_current_value(self) -> None:
        config_entry = MagicMock()
        config_entry.options = {CONF_MIN_SOC_ON_GRID: 30}

        flow = FoxessControlOptionsFlow(config_entry)
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        await flow.async_step_init(None)

        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_value_when_no_option(self) -> None:
        config_entry = MagicMock()
        config_entry.options = {}

        flow = FoxessControlOptionsFlow(config_entry)
        flow.async_show_form = MagicMock(
            return_value={"type": "form"},
        )

        await flow.async_step_init(None)

        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_saves_battery_soc_entity(self) -> None:
        config_entry = MagicMock()
        config_entry.options = {
            CONF_MIN_SOC_ON_GRID: 20,
            CONF_BATTERY_SOC_ENTITY: "",
        }

        flow = FoxessControlOptionsFlow(config_entry)
        flow.async_create_entry = MagicMock(
            return_value={"type": "create_entry"},
        )

        await flow.async_step_init(
            {
                CONF_MIN_SOC_ON_GRID: 25,
                CONF_BATTERY_SOC_ENTITY: "sensor.battery_soc",
            }
        )

        flow.async_create_entry.assert_called_once()
        data = flow.async_create_entry.call_args.kwargs["data"]
        assert data[CONF_BATTERY_SOC_ENTITY] == "sensor.battery_soc"
