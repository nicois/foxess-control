"""Config flow for FoxESS Control integration."""

from __future__ import annotations

import logging
from typing import Any

import requests
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_API_KEY,
    CONF_CHARGE_POWER_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_FEEDIN_ENERGY_ENTITY,
    CONF_LOADS_POWER_ENTITY,
    CONF_MIN_SOC_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SOC_ENTITY,
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    CONF_WORK_MODE_ENTITY,
    CONF_WS_ALL_SESSIONS,
    CONF_WS_MODE,
    DOMAIN,
    WS_MODE_ALWAYS,
    WS_MODE_AUTO,
    WS_MODE_SMART_SESSIONS,
)
from .foxess import FoxESSClient, FoxESSRealtimeWS, FoxESSWebSession, Inverter
from .foxess.client import FoxESSApiError
from .foxess.web_session import FoxESSWebAuthError, ensure_password_hash
from .smart_battery.config_flow_base import (
    ENTITY_KEYS,
    battery_options_schema,
    detect_entities,
    entity_mapping_schema,
)

# Map foxess_modbus original_name → our CONF_* key.
_MODBUS_NAME_MAP: dict[str, str] = {
    "Work Mode": CONF_WORK_MODE_ENTITY,
    "Force Charge Power": CONF_CHARGE_POWER_ENTITY,
    "Force Discharge Power": CONF_DISCHARGE_POWER_ENTITY,
    "Min SoC": CONF_MIN_SOC_ENTITY,
    "Battery SoC": CONF_SOC_ENTITY,
    "Load Power": CONF_LOADS_POWER_ENTITY,
    "PV Power": CONF_PV_POWER_ENTITY,
    "Feed-in Total": CONF_FEEDIN_ENERGY_ENTITY,
}


def _detect_foxess_modbus_entities(
    hass: HomeAssistant,
) -> dict[str, str]:
    """Auto-detect foxess_modbus entities from the entity registry."""
    return detect_entities(hass, "foxess_modbus", _MODBUS_NAME_MAP)


_LOGGER = logging.getLogger(__name__)


def _validate_credentials(api_key: str, device_serial: str) -> None:
    """Validate API credentials by fetching device detail (blocking)."""
    client = FoxESSClient(api_key)
    inverter = Inverter(client, device_serial)
    inverter.get_detail()


class FoxessControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for FoxESS Control."""

    VERSION = 2

    def __init__(self) -> None:
        super().__init__()
        self._api_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    _validate_credentials,
                    user_input[CONF_API_KEY],
                    user_input[CONF_DEVICE_SERIAL],
                )
            except FoxESSApiError as err:
                _LOGGER.warning("FoxESS API rejected credentials: %s", err)
                errors["base"] = "invalid_auth"
            except requests.RequestException as err:
                _LOGGER.warning("Could not reach FoxESS Cloud API: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_SERIAL])
                self._abort_if_unique_id_configured()
                self._api_data = user_input
                return await self.async_step_web_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Required(CONF_DEVICE_SERIAL): str,
                }
            ),
            errors=errors,
        )

    async def _validate_web_credentials(
        self,
        username: str,
        password_hash: str,
    ) -> dict[str, str]:
        """Validate web login, plantId discovery, and WebSocket.

        Returns a dict of errors (empty on success).
        """
        errors: dict[str, str] = {}
        session = FoxESSWebSession(username, password_hash)
        try:
            await session.async_login()
        except FoxESSWebAuthError as err:
            _LOGGER.warning("FoxESS web login failed: %s", err)
            errors["base"] = "web_auth_failed"
        except Exception as err:
            _LOGGER.warning("FoxESS web login error: %s", err)
            errors["base"] = "web_auth_failed"

        plant_id: str | None = None
        if not errors:
            try:
                client = FoxESSClient(self._api_data[CONF_API_KEY])
                inverter = Inverter(client, self._api_data[CONF_DEVICE_SERIAL])
                plant_id = await self.hass.async_add_executor_job(inverter.get_plant_id)
            except Exception as err:
                _LOGGER.warning("Could not discover plantId: %s", err)
                errors["base"] = "ws_connect_failed"

        if not errors and plant_id is not None:
            ws = FoxESSRealtimeWS(
                plant_id,
                session,
                on_data=lambda _: None,  # type: ignore[arg-type,return-value]
                on_disconnect=lambda: None,
            )
            try:
                await ws.async_connect()
            except Exception as err:
                _LOGGER.warning("WebSocket test connection failed: %s", err)
                errors["base"] = "ws_connect_failed"
            finally:
                await ws.async_disconnect()

        await session.async_close()
        return errors

    async def async_step_web_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optional step: web portal credentials for real-time data.

        The password is hashed (MD5) before storage.  If the user pastes
        an MD5 hex string directly, it is stored as-is.
        """
        errors: dict[str, str] = {}
        is_reconfigure = self.source == "reconfigure"

        if user_input is not None:
            username = user_input.get(CONF_WEB_USERNAME, "").strip()
            raw_password = user_input.get(CONF_WEB_PASSWORD, "").strip()
            password_hash = ""

            if username and raw_password:
                password_hash = ensure_password_hash(raw_password)
                _LOGGER.debug(
                    "Web credentials: user=%s, input_len=%d, hash=%s...%s, source=%s",
                    username,
                    len(raw_password),
                    password_hash[:6],
                    password_hash[-4:],
                    self.source,
                )
                errors = await self._validate_web_credentials(username, password_hash)

            if not errors:
                full_data = {**self._api_data}
                if username and raw_password:
                    full_data[CONF_WEB_USERNAME] = username
                    full_data[CONF_WEB_PASSWORD] = password_hash
                else:
                    # Blank fields clear web credentials
                    full_data.pop(CONF_WEB_USERNAME, None)
                    full_data.pop(CONF_WEB_PASSWORD, None)

                if is_reconfigure:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(),
                        data=full_data,
                    )
                return self.async_create_entry(
                    title=f"FoxESS {self._api_data[CONF_DEVICE_SERIAL]}",
                    data=full_data,
                )

        current_username = self._api_data.get(CONF_WEB_USERNAME, "")
        return self.async_show_form(
            step_id="web_credentials",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_WEB_USERNAME, default=current_username): str,
                    vol.Optional(CONF_WEB_PASSWORD, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication when API key becomes invalid."""
        self._api_data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt the user for a new API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            new_key = user_input[CONF_API_KEY]
            serial = self._api_data[CONF_DEVICE_SERIAL]
            try:
                await self.hass.async_add_executor_job(
                    _validate_credentials, new_key, serial
                )
            except FoxESSApiError as err:
                _LOGGER.warning("FoxESS API rejected credentials: %s", err)
                errors["base"] = "invalid_auth"
            except requests.RequestException as err:
                _LOGGER.warning("Could not reach FoxESS Cloud API: %s", err)
                errors["base"] = "cannot_connect"
            else:
                entry = self.hass.config_entries.async_get_entry(
                    self.context["entry_id"]
                )
                assert entry is not None
                updated_data = {**entry.data, CONF_API_KEY: new_key}
                return self.async_update_reload_and_abort(entry, data=updated_data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow updating web credentials on an existing entry."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        self._api_data = dict(entry.data)
        return await self.async_step_web_credentials(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow handler."""
        return FoxessControlOptionsFlow(config_entry)


class FoxessControlOptionsFlow(OptionsFlow):
    """Handle options for FoxESS Control."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._init_data: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the core options."""
        if user_input is not None:
            # Check if foxess_modbus is installed — if so, continue to entity step.
            if self.hass.config_entries.async_entries("foxess_modbus"):
                self._init_data = user_input
                return await self.async_step_modbus()
            # No foxess_modbus — clear any stale entity options and save.
            for key in ENTITY_KEYS:
                user_input.pop(key, None)
            return self.async_create_entry(data=user_input)

        schema = battery_options_schema(self._config_entry)
        # Show WebSocket option only when web credentials are configured
        if self._config_entry.data.get(CONF_WEB_USERNAME):
            opts = self._config_entry.options
            if CONF_WS_MODE in opts:
                current_ws = str(opts[CONF_WS_MODE])
            elif opts.get(CONF_WS_ALL_SESSIONS):
                current_ws = WS_MODE_SMART_SESSIONS
            else:
                current_ws = WS_MODE_AUTO
            schema = schema.extend(
                {
                    vol.Optional(
                        CONF_WS_MODE,
                        default=current_ws,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=WS_MODE_AUTO,
                                    label="Auto (paced discharge only)",
                                ),
                                SelectOptionDict(
                                    value=WS_MODE_SMART_SESSIONS,
                                    label="All smart sessions",
                                ),
                                SelectOptionDict(
                                    value=WS_MODE_ALWAYS,
                                    label="Always connected",
                                ),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_modbus(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure foxess_modbus entity mappings."""
        if user_input is not None:
            return self.async_create_entry(data={**self._init_data, **user_input})

        detected = _detect_foxess_modbus_entities(self.hass)
        schema = entity_mapping_schema(self._config_entry, detected)

        return self.async_show_form(
            step_id="modbus",
            data_schema=schema,
        )
