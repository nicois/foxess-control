"""Switching an existing entry between the cloud and local backends.

Entity unique ids are ``{entry_id}_{suffix}``, so switching the backend *in
place* (reconfigure, same config entry) keeps every entity, its entity id,
recorder history, dashboards, helpers and options. Removing the entry and
adding a new one loses all of it, which is what made trying foxess-local
(and going back) expensive.

The gateway side is real HTTP against the simulator's gateway (C-028); the
cloud credential check is patched, as in ``test_config_flow.py``, because
it targets the real cloud URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.foxess_control.config_flow import FoxessControlConfigFlow
from custom_components.foxess_control.const import (
    BACKEND_CLOUD,
    BACKEND_LOCAL,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_DEVICE_SERIAL,
    CONF_LOCAL_TOKEN,
    CONF_LOCAL_URL,
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    CONF_WORK_MODE_ENTITY,
    DOMAIN,
)
from custom_components.foxess_control.domain_data import FoxESSControlData
from custom_components.foxess_control.foxess.client import FoxESSApiError

if TYPE_CHECKING:
    from tests.conftest import SimulatorHandle

_VALIDATE = "custom_components.foxess_control.config_flow._validate_credentials"

CLOUD_DATA = {
    CONF_API_KEY: "key123",
    CONF_DEVICE_SERIAL: "SIM0001",
    CONF_WEB_USERNAME: "user@fox.com",
    CONF_WEB_PASSWORD: "hash",
}


def _entry(
    data: dict[str, Any],
    *,
    title: str = "FoxESS SIM0001",
    options: dict[str, Any] | None = None,
) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.unique_id = "SIM0001"
    entry.title = title
    entry.data = data
    entry.options = options or {}
    return entry


def _flow(entry: MagicMock, dd: FoxESSControlData | None = None) -> Any:
    flow = FoxessControlConfigFlow()
    flow.hass = MagicMock()
    flow.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    flow.hass.data = {DOMAIN: dd or FoxESSControlData()}
    flow.context = {"entry_id": "e1", "source": "reconfigure"}
    flow._get_reconfigure_entry = MagicMock(return_value=entry)  # type: ignore[method-assign]
    flow.async_update_reload_and_abort = MagicMock(  # type: ignore[method-assign]
        return_value={"type": "abort", "reason": "backend_switched"}
    )
    flow.async_show_form = MagicMock(return_value={"type": "form"})  # type: ignore[method-assign]
    flow.async_show_menu = MagicMock(return_value={"type": "menu"})  # type: ignore[method-assign]
    flow.async_abort = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda reason: {"type": "abort", "reason": reason}
    )
    return flow


async def _submit_local(flow: Any, user_input: dict[str, Any] | None) -> Any:
    async with aiohttp.ClientSession() as session:
        with patch(
            "custom_components.foxess_control.config_flow.async_get_clientsession",
            return_value=session,
        ):
            return await flow.async_step_reconfigure_local(user_input)


def _update(flow: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = flow.async_update_reload_and_abort.call_args.kwargs
    return kwargs


class TestReconfigureMenu:
    @pytest.mark.asyncio
    async def test_cloud_entry_offers_web_credentials_and_the_gateway(self) -> None:
        flow = _flow(_entry(dict(CLOUD_DATA)))
        await flow.async_step_reconfigure(None)
        assert flow.async_show_menu.call_args.kwargs["menu_options"] == [
            "web_credentials",
            "reconfigure_local",
        ]

    @pytest.mark.asyncio
    async def test_local_entry_offers_the_gateway_and_the_cloud(self) -> None:
        flow = _flow(
            _entry({CONF_BACKEND: BACKEND_LOCAL, CONF_DEVICE_SERIAL: "SIM0001"})
        )
        await flow.async_step_reconfigure(None)
        assert flow.async_show_menu.call_args.kwargs["menu_options"] == [
            "reconfigure_local",
            "reconfigure_cloud",
        ]

    @pytest.mark.asyncio
    async def test_entity_mode_entry_keeps_the_web_credentials_form(self) -> None:
        """Entity mode reads foxess_modbus entities; switching it is out of
        scope, so reconfigure behaves as before."""
        flow = _flow(
            _entry({}, options={CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"})
        )
        await flow.async_step_reconfigure(None)
        flow.async_show_menu.assert_not_called()
        assert flow.async_show_form.call_args.kwargs["step_id"] == "web_credentials"


class TestSwitchToLocal:
    @pytest.mark.asyncio
    async def test_switch_updates_the_same_entry_and_keeps_cloud_credentials(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Same entry → same entities. The cloud key and web login stay, so
        switching back needs nothing re-typed."""
        foxess_sim.set(local_gateway_token="fl_tok", battery_capacity_kwh=41.94)
        entry = _entry(dict(CLOUD_DATA))
        flow = _flow(entry)

        result = await _submit_local(
            flow, {CONF_LOCAL_URL: foxess_sim.url + "/", CONF_LOCAL_TOKEN: "fl_tok"}
        )

        assert result["reason"] == "backend_switched"
        kwargs = _update(flow)
        assert flow.async_update_reload_and_abort.call_args.args[0] is entry
        assert kwargs["data"] == {
            **CLOUD_DATA,
            CONF_BACKEND: BACKEND_LOCAL,
            CONF_LOCAL_URL: foxess_sim.url,
            CONF_LOCAL_TOKEN: "fl_tok",
        }
        assert kwargs["reason"] == "backend_switched"

    @pytest.mark.asyncio
    async def test_existing_options_win_over_the_gateway_capacity(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(battery_capacity_kwh=41.94)
        flow = _flow(
            _entry(dict(CLOUD_DATA), options={CONF_BATTERY_CAPACITY_KWH: 20.0})
        )
        await _submit_local(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert _update(flow)["options"][CONF_BATTERY_CAPACITY_KWH] == 20.0

    @pytest.mark.asyncio
    async def test_missing_capacity_is_filled_from_the_gateway(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(battery_capacity_kwh=41.94)
        flow = _flow(_entry(dict(CLOUD_DATA), options={"min_soc": 15}))
        await _submit_local(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert _update(flow)["options"] == {
            "min_soc": 15,
            CONF_BATTERY_CAPACITY_KWH: pytest.approx(41.94),
        }

    @pytest.mark.asyncio
    async def test_generated_title_follows_the_backend(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        flow = _flow(_entry(dict(CLOUD_DATA), title="FoxESS SIM0001"))
        await _submit_local(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert _update(flow)["title"] == "KH10 SIM0001 (local)"

    @pytest.mark.asyncio
    async def test_a_title_the_user_chose_is_kept(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        flow = _flow(_entry(dict(CLOUD_DATA), title="Garage battery"))
        await _submit_local(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert "title" not in _update(flow)

    @pytest.mark.asyncio
    async def test_a_gateway_for_another_inverter_is_refused(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Re-pointing an entry at a different inverter would graft one
        inverter's history onto another's entities."""
        foxess_sim.set(device_sn="OTHER0002")
        flow = _flow(_entry(dict(CLOUD_DATA)))
        result = await _submit_local(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert result == {"type": "abort", "reason": "wrong_inverter"}
        flow.async_update_reload_and_abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_refused_token_shows_the_form_again(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(local_gateway_token="fl_right")
        flow = _flow(_entry(dict(CLOUD_DATA)))
        await _submit_local(
            flow, {CONF_LOCAL_URL: foxess_sim.url, CONF_LOCAL_TOKEN: "fl_wrong"}
        )
        kwargs = flow.async_show_form.call_args.kwargs
        assert kwargs["step_id"] == "reconfigure_local"
        assert kwargs["errors"] == {"base": "local_invalid_auth"}
        flow.async_update_reload_and_abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_entry_form_is_prefilled_with_its_gateway(self) -> None:
        flow = _flow(
            _entry(
                {
                    CONF_BACKEND: BACKEND_LOCAL,
                    CONF_LOCAL_URL: "http://gw:18080",
                    CONF_LOCAL_TOKEN: "fl_tok",
                    CONF_DEVICE_SERIAL: "SIM0001",
                }
            )
        )
        await _submit_local(flow, None)
        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        defaults = {str(k): k.default() for k in schema.schema}
        # The token is prefilled too, so changing only the URL does not mean
        # re-typing it; the field is a masked password input.
        assert defaults == {
            CONF_LOCAL_URL: "http://gw:18080",
            CONF_LOCAL_TOKEN: "fl_tok",
        }


class TestSwitchToCloud:
    LOCAL_DATA = {
        **CLOUD_DATA,
        CONF_BACKEND: BACKEND_LOCAL,
        CONF_LOCAL_URL: "http://gw:18080",
        CONF_LOCAL_TOKEN: "fl_tok",
    }

    @pytest.mark.asyncio
    async def test_form_is_prefilled_with_the_kept_api_key(self) -> None:
        flow = _flow(_entry(dict(self.LOCAL_DATA)))
        await flow.async_step_reconfigure_cloud(None)
        kwargs = flow.async_show_form.call_args.kwargs
        assert kwargs["step_id"] == "reconfigure_cloud"
        defaults = {str(k): k.default() for k in kwargs["data_schema"].schema}
        assert defaults == {CONF_API_KEY: "key123"}

    @pytest.mark.asyncio
    async def test_switch_back_validates_the_key_for_this_inverter(self) -> None:
        entry = _entry(dict(self.LOCAL_DATA), title="KH10 SIM0001 (local)")
        flow = _flow(entry)
        with patch(_VALIDATE) as validate:
            result = await flow.async_step_reconfigure_cloud({CONF_API_KEY: "key456"})
        validate.assert_called_once_with("key456", "SIM0001")
        assert result["reason"] == "backend_switched"
        kwargs = _update(flow)
        assert flow.async_update_reload_and_abort.call_args.args[0] is entry
        # The gateway settings are kept for the next switch.
        assert kwargs["data"] == {
            **self.LOCAL_DATA,
            CONF_BACKEND: BACKEND_CLOUD,
            CONF_API_KEY: "key456",
        }
        assert kwargs["title"] == "FoxESS SIM0001"

    @pytest.mark.asyncio
    async def test_rejected_key_shows_the_form_again(self) -> None:
        flow = _flow(_entry(dict(self.LOCAL_DATA)))
        with patch(_VALIDATE, side_effect=FoxESSApiError(41809, "Token invalid")):
            await flow.async_step_reconfigure_cloud({CONF_API_KEY: "bad"})
        assert flow.async_show_form.call_args.kwargs["errors"] == {
            "base": "invalid_auth"
        }
        flow.async_update_reload_and_abort.assert_not_called()


class TestActiveSessionBlocksTheSwitch:
    """A reload keeps a running session's schedule group on the inverter
    for recovery; the other backend could not recover or remove it (C-025)."""

    @pytest.mark.asyncio
    async def test_switch_to_local_is_refused_mid_session(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        dd = FoxESSControlData()
        dd.smart_discharge_state = MagicMock()
        flow = _flow(_entry(dict(CLOUD_DATA)), dd)
        result = await _submit_local(flow, {CONF_LOCAL_URL: foxess_sim.url})
        assert result == {"type": "abort", "reason": "session_active"}
        flow.async_update_reload_and_abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_to_cloud_is_refused_mid_session(self) -> None:
        dd = FoxESSControlData()
        dd.smart_charge_state = MagicMock()
        flow = _flow(_entry(dict(TestSwitchToCloud.LOCAL_DATA)), dd)
        result = await flow.async_step_reconfigure_cloud(None)
        assert result == {"type": "abort", "reason": "session_active"}
