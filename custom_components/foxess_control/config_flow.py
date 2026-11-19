"""Config flow for FoxESS Control integration."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import requests
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    BACKEND_CLOUD,
    BACKEND_LOCAL,
    CONF_ADDITIONAL_PV_POWER_VARIABLE,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_BAT_CHARGE_POWER_ENTITY,
    CONF_BAT_DISCHARGE_POWER_ENTITY,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_CHARGE_POWER_ENTITY,
    CONF_DEVICE_SERIAL,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_EXPORT_LIMIT_ENTITY,
    CONF_FEEDIN_ENERGY_ENTITY,
    CONF_FEEDIN_POWER_ENTITY,
    CONF_GRID_CONSUMPTION_POWER_ENTITY,
    CONF_LOADS_POWER_ENTITY,
    CONF_LOCAL_TOKEN,
    CONF_LOCAL_URL,
    CONF_MIN_SOC_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SCHEDULER_HANDBACK,
    CONF_SOC_ENTITY,
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    CONF_WORK_MODE_ENTITY,
    CONF_WS_ALL_SESSIONS,
    CONF_WS_MODE,
    DEFAULT_LOCAL_URL,
    DEFAULT_SCHEDULER_HANDBACK,
    DOMAIN,
    WS_MODE_ALWAYS,
    WS_MODE_AUTO,
    WS_MODE_SMART_SESSIONS,
)
from .foxess import FoxESSClient, FoxESSRealtimeWS, FoxESSWebSession, Inverter
from .foxess.client import FoxESSApiError
from .foxess.local_client import FoxESSLocalClient, LocalGatewayError
from .foxess.web_session import FoxESSWebAuthError, ensure_password_hash
from .smart_battery.config_flow_base import (
    ENTITY_KEYS,
    battery_options_schema,
    detect_entities,
    entity_mapping_schema,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    "Battery Charge Power": CONF_BAT_CHARGE_POWER_ENTITY,
    "Battery Discharge Power": CONF_BAT_DISCHARGE_POWER_ENTITY,
    "Grid Consumption Power": CONF_GRID_CONSUMPTION_POWER_ENTITY,
    "Feed-in Power": CONF_FEEDIN_POWER_ENTITY,
    "Max Grid Export Limit": CONF_EXPORT_LIMIT_ENTITY,
}


def _detect_foxess_modbus_entities(
    hass: HomeAssistant,
) -> dict[str, str]:
    """Auto-detect foxess_modbus entities from the entity registry."""
    return detect_entities(hass, "foxess_modbus", _MODBUS_NAME_MAP)


_LOGGER = logging.getLogger(__name__)


def _additional_pv_schema_dict(opts: Mapping[str, Any]) -> dict[Any, Any]:
    """Schema fragment for the optional AC-coupled additional PV variable.

    Cloud-only, brand-specific — kept out of the brand-agnostic
    ``battery_options_schema`` (C-021). Blank default preserves current
    behaviour.
    """
    return {
        vol.Optional(
            CONF_ADDITIONAL_PV_POWER_VARIABLE,
            default=opts.get(CONF_ADDITIONAL_PV_POWER_VARIABLE, ""),
        ): str,
    }


def _scheduler_handback_schema_dict(opts: Mapping[str, Any]) -> dict[Any, Any]:
    """Schema fragment for the opt-in scheduler handback (issues #16, #4).

    Brand-specific: the Mode Scheduler master switch and the 10 %
    ``minsocongrid`` floor it enforces are FoxESS concepts, so this stays
    out of ``battery_options_schema`` (C-021).

    The default is load-bearing rather than tidy.  HA replaces the whole
    options dict with whatever this flow returns, and what it returns for
    a field the user did not touch is precisely this default — so
    defaulting to anything but the currently-saved value (falling back to
    ``DEFAULT_SCHEDULER_HANDBACK``, i.e. off) would enable handback, and
    with it writes to the user's own Min SoC register, for anyone who
    merely opens the dialog and presses Submit.

    Declared as a bare ``bool``, which voluptuous enforces as an
    isinstance check: a stringly-typed ``"false"`` is rejected outright
    rather than quietly read as truthy.
    """
    return {
        vol.Optional(
            CONF_SCHEDULER_HANDBACK,
            default=opts.get(CONF_SCHEDULER_HANDBACK, DEFAULT_SCHEDULER_HANDBACK),
        ): bool,
    }


# Shown in place of a percentage when nothing has been captured.  Not a
# number, deliberately: a number here would be a claim about the user's
# inverter that this integration has not actually read.  The surrounding
# translated text explains what the dash means, so the whole of what the
# user reads stays localised.
_MIN_SOC_NOT_CAPTURED = "—"


def _captured_min_soc_text(hass: HomeAssistant) -> str:
    """The captured Min SoC, formatted for the options dialog (C-020).

    ``_min_soc_capture`` reads the user's own floor at most once and never
    re-reads it, which is what stops a session value from becoming "the
    user's value" permanently — but it also means a handback user who
    changes their floor in the FoxESS app has it reverted.  The remedy is
    to toggle the option off and on, which re-captures; a remedy for a
    value the user cannot see is not a remedy, hence this.

    Read-only, and rendered by HA as the toggle's own helper text rather
    than as a disabled field: a field the user can see but not change
    invites them to try.

    Never raises.  The options dialog must open even when setup failed,
    and "we do not know" is a perfectly good thing to say.
    """
    if DOMAIN not in getattr(hass, "data", {}):
        return _MIN_SOC_NOT_CAPTURED
    try:
        from ._helpers import _dd

        captured = _dd(hass).captured_min_soc_on_grid
    except (AttributeError, KeyError, TypeError):
        _LOGGER.debug("Could not read the captured Min SoC for the options form")
        return _MIN_SOC_NOT_CAPTURED
    # 0 is a real captured value — issue #4 is exactly about a 0 % floor —
    # so this must test for None, not for truthiness.
    if captured is None:
        return _MIN_SOC_NOT_CAPTURED
    return f"{captured}%"


def _validate_credentials(api_key: str, device_serial: str) -> None:
    """Validate API credentials by fetching device detail (blocking).

    Honours ``FOXESS_SIMULATOR_URL`` as setup does, so a containerised HA
    pointed at the simulator validates against it (C-029).
    """
    client = FoxESSClient(api_key, base_url=os.environ.get("FOXESS_SIMULATOR_URL"))
    inverter = Inverter(client, device_serial)
    inverter.get_detail()


def _gateway_input(user_input: dict[str, Any]) -> tuple[str, str]:
    """``(url, token)`` from the form, tolerating a trailing ``/`` and the
    ``Bearer `` prefix a secrets.yaml value carries."""
    url = str(user_input.get(CONF_LOCAL_URL, "")).strip().rstrip("/")
    token = str(user_input.get(CONF_LOCAL_TOKEN, "") or "").strip()
    return url, token.removeprefix("Bearer ").strip()


def _gateway_serial(state: dict[str, Any]) -> str:
    return str((state.get("identity") or {}).get("sn") or "")


def _local_title(state: dict[str, Any], serial: str) -> str:
    model = (state.get("identity") or {}).get("model") or "FoxESS"
    return f"{model} {serial} (local)"


def _entry_serial(entry: ConfigEntry) -> str:
    return str(entry.unique_id or entry.data.get(CONF_DEVICE_SERIAL) or "")


def _has_generated_title(entry: ConfigEntry) -> bool:
    """True if the title is one this flow generated (cloud or local form),
    so a switch may rewrite it; a title the user chose is left alone."""
    serial = _entry_serial(entry)
    title = entry.title or ""
    if title == f"FoxESS {serial}":
        return True
    model, sep, rest = title.partition(" ")
    return bool(model) and bool(sep) and rest == f"{serial} (local)"


def _with_gateway_capacity(
    options: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Pre-fill the usable battery capacity from the gateway, unless the
    entry already has one (the user's value wins)."""
    capacity_wh = (state.get("battery") or {}).get("capacity_wh")
    if (
        CONF_BATTERY_CAPACITY_KWH not in options
        and isinstance(capacity_wh, int | float)
        and capacity_wh > 0
    ):
        options[CONF_BATTERY_CAPACITY_KWH] = capacity_wh / 1000.0
    return options


class FoxessControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for FoxESS Control."""

    VERSION = 2

    def __init__(self) -> None:
        super().__init__()
        self._api_data: dict[str, Any] = {}
        self._entity_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: choose how the inverter is reached.

        Cloud (API key) and a foxess-local gateway are always offered;
        entity mode only when a ``foxess_modbus`` config entry exists. HA
        routes a menu selection to ``async_step_<option>`` by convention.
        """
        options = ["cloud", "local"]
        if self.hass.config_entries.async_entries("foxess_modbus"):
            options.append("entity")
        return self.async_show_menu(step_id="user", menu_options=options)

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Local backend: a foxess-local gateway URL and bearer token.

        Validated by reading ``GET /state``. The inverter's serial (from the
        gateway's identity) is the unique id, as in cloud mode, so the same
        inverter cannot be added twice through different backends. The
        gateway's usable battery capacity pre-fills the option discharge
        pacing needs.
        """
        errors: dict[str, str] = {}
        url = DEFAULT_LOCAL_URL
        if user_input is not None:
            url, token = _gateway_input(user_input)
            state, errors = await self._check_gateway(url, token)
            if not errors:
                serial = _gateway_serial(state)
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_local_title(state, serial),
                    data={
                        CONF_BACKEND: BACKEND_LOCAL,
                        CONF_LOCAL_URL: url,
                        CONF_LOCAL_TOKEN: token,
                        CONF_DEVICE_SERIAL: serial,
                    },
                    options=_with_gateway_capacity({}, state),
                )

        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOCAL_URL, default=url): str,
                    vol.Optional(CONF_LOCAL_TOKEN, default=""): str,
                }
            ),
            errors=errors,
        )

    async def _check_gateway(
        self, url: str, token: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Read ``GET /state`` from the gateway: ``(state, errors)``."""
        client = FoxESSLocalClient(async_get_clientsession(self.hass), url, token)
        try:
            state = await client.get_state()
        except LocalGatewayError as err:
            _LOGGER.warning("foxess-local gateway check failed: %s", err)
            if err.code in ("unauthorized", "insufficient_role"):
                return {}, {"base": "local_invalid_auth"}
            return {}, {"base": "local_cannot_connect"}
        if not _gateway_serial(state):
            return state, {"base": "inverter_not_identified"}
        return state, {}

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cloud setup: API key + serial (validated against the cloud API)."""
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
            step_id="cloud",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Required(CONF_DEVICE_SERIAL): str,
                }
            ),
            errors=errors,
        )

    async def async_step_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entity mode: map foxess_modbus entities (no API key)."""
        if user_input is not None:
            self._entity_data = dict(user_input)
            return await self.async_step_entity_battery()

        detected = _detect_foxess_modbus_entities(self.hass)
        return self.async_show_form(
            step_id="entity",
            data_schema=entity_mapping_schema(None, detected),
        )

    async def async_step_entity_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entity mode: battery options, then create the entry.

        The runtime reads entity mode and all entity/battery config from
        ``entry.options`` (``domain_data.py`` builds ``entity_mode`` from
        ``CONF_WORK_MODE_ENTITY`` in options; ``IntegrationConfig`` is built
        from ``dict(entry.options)``). The mappings AND the battery settings
        therefore both go into ``options``; ``data`` is empty (no API key).
        """
        if user_input is not None:
            # Stable unique_id for the API-key-less entity entry: derive from
            # the first foxess_modbus config entry id (falls back to a fixed
            # sentinel) so duplicate entity setups abort cleanly.
            modbus_entries = self.hass.config_entries.async_entries("foxess_modbus")
            uid = (
                f"entity-{modbus_entries[0].entry_id}"
                if modbus_entries
                else "entity-foxess-control"
            )
            await self.async_set_unique_id(uid)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="FoxESS Control (entity mode)",
                data={},
                options={**self._entity_data, **user_input},
            )

        return self.async_show_form(
            step_id="entity_battery",
            data_schema=battery_options_schema(None),
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
        except (OSError, requests.RequestException) as err:
            _LOGGER.warning("FoxESS web login error: %s", err)
            errors["base"] = "web_auth_failed"

        plant_id: str | None = None
        if not errors:
            try:
                client = FoxESSClient(self._api_data[CONF_API_KEY])
                inverter = Inverter(client, self._api_data[CONF_DEVICE_SERIAL])
                plant_id = await self.hass.async_add_executor_job(inverter.get_plant_id)
            except (FoxESSApiError, requests.RequestException, OSError) as err:
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
            except (OSError, requests.RequestException) as err:
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
                if entry is None:
                    raise RuntimeError("Config entry not found during reauth")
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
        """Change how an existing entry reaches the inverter, in place.

        Switching the backend updates *this* config entry rather than
        replacing it. Entity unique ids are ``{entry_id}_{suffix}``, so every
        entity keeps its id, history, dashboards, helpers and options. The
        other backend's settings stay in the entry data, so switching back
        needs nothing re-typed. Entity mode (foxess_modbus) only offers its
        web credentials, as before.
        """
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            raise RuntimeError("Config entry not found during reconfigure")
        self._api_data = dict(entry.data)
        if entry.options.get(CONF_WORK_MODE_ENTITY):
            return await self.async_step_web_credentials(user_input)
        if entry.data.get(CONF_BACKEND) == BACKEND_LOCAL:
            menu = ["reconfigure_local", "reconfigure_cloud"]
        else:
            menu = ["web_credentials", "reconfigure_local"]
        return self.async_show_menu(step_id="reconfigure", menu_options=menu)

    def _session_active(self) -> bool:
        """A reload keeps a running session's schedule group on the inverter
        for recovery, which the other backend cannot recover or remove
        (C-025): switch only between sessions."""
        dd = self.hass.data.get(DOMAIN)
        return dd is not None and (
            dd.smart_charge_state is not None or dd.smart_discharge_state is not None
        )

    async def async_step_reconfigure_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use a foxess-local gateway (or change its URL or token)."""
        entry = self._get_reconfigure_entry()
        if self._session_active():
            return self.async_abort(reason="session_active")
        errors: dict[str, str] = {}
        url = str(entry.data.get(CONF_LOCAL_URL) or DEFAULT_LOCAL_URL)
        token = str(entry.data.get(CONF_LOCAL_TOKEN) or "")
        if user_input is not None:
            url, token = _gateway_input(user_input)
            state, errors = await self._check_gateway(url, token)
            if not errors:
                serial = _gateway_serial(state)
                if serial != _entry_serial(entry):
                    _LOGGER.warning(
                        "Gateway reports a different inverter than this entry"
                    )
                    return self.async_abort(reason="wrong_inverter")
                extra: dict[str, Any] = {}
                if _has_generated_title(entry):
                    extra["title"] = _local_title(state, serial)
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_BACKEND: BACKEND_LOCAL,
                        CONF_LOCAL_URL: url,
                        CONF_LOCAL_TOKEN: token,
                    },
                    options=_with_gateway_capacity(dict(entry.options), state),
                    reason="backend_switched",
                    **extra,
                )

        return self.async_show_form(
            step_id="reconfigure_local",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOCAL_URL, default=url): str,
                    # suggested_value, not default: a default is re-applied
                    # when the field is cleared, so the token could never be
                    # removed.
                    vol.Optional(
                        CONF_LOCAL_TOKEN, description={"suggested_value": token}
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use the FoxESS Cloud API (validated for this entry's inverter)."""
        entry = self._get_reconfigure_entry()
        if self._session_active():
            return self.async_abort(reason="session_active")
        errors: dict[str, str] = {}
        serial = _entry_serial(entry)
        if user_input is not None:
            api_key = str(user_input[CONF_API_KEY]).strip()
            try:
                await self.hass.async_add_executor_job(
                    _validate_credentials, api_key, serial
                )
            except FoxESSApiError as err:
                _LOGGER.warning("FoxESS API rejected credentials: %s", err)
                errors["base"] = "invalid_auth"
            except requests.RequestException as err:
                _LOGGER.warning("Could not reach FoxESS Cloud API: %s", err)
                errors["base"] = "cannot_connect"
            else:
                extra: dict[str, Any] = {}
                if _has_generated_title(entry):
                    extra["title"] = f"FoxESS {serial}"
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_BACKEND: BACKEND_CLOUD,
                        CONF_API_KEY: api_key,
                    },
                    reason="backend_switched",
                    **extra,
                )

        current_key = entry.data.get(CONF_API_KEY)
        key_field = (
            vol.Required(CONF_API_KEY, default=current_key)
            if current_key
            else vol.Required(CONF_API_KEY)
        )
        return self.async_show_form(
            step_id="reconfigure_cloud",
            data_schema=vol.Schema({key_field: str}),
            errors=errors,
        )

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
        schema = schema.extend(_additional_pv_schema_dict(self._config_entry.options))
        schema = schema.extend(
            _scheduler_handback_schema_dict(self._config_entry.options)
        )
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
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "captured_min_soc": _captured_min_soc_text(self.hass),
            },
        )

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
