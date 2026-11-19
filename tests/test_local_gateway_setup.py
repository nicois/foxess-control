"""Local gateway backend: integration setup and the read-only boundary.

Phase 1 of the local backend is read-only: setup builds the
``FoxESSLocalCoordinator`` (no cloud ``Inverter``) and starts its event
stream; every control service refuses with a translated, explanatory
error instead of crashing on the absent inverter (C-020).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError

from custom_components.foxess_control import _get_inverter, async_setup_entry
from custom_components.foxess_control.const import (
    BACKEND_LOCAL,
    CONF_BACKEND,
    CONF_DEVICE_SERIAL,
    CONF_LOCAL_TOKEN,
    CONF_LOCAL_URL,
    DOMAIN,
)
from custom_components.foxess_control.coordinator import FoxESSLocalCoordinator
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
)

if TYPE_CHECKING:
    from tests.conftest import SimulatorHandle


def _hass() -> MagicMock:
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    store = MagicMock()
    store.async_load = AsyncMock(return_value={})
    store.async_save = AsyncMock()
    dd = FoxESSControlData()
    dd.store = store
    hass.data = {DOMAIN: dd}
    return hass


def _entry(url: str, token: str = "") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "local1"
    entry.data = {
        CONF_BACKEND: BACKEND_LOCAL,
        CONF_LOCAL_URL: url,
        CONF_LOCAL_TOKEN: token,
        CONF_DEVICE_SERIAL: "SIM0001",
    }
    entry.options = {}
    return entry


async def _first_refresh(self: FoxESSLocalCoordinator) -> None:
    """Stand-in for HA's first refresh: same data path, no entry state."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    try:
        self.data = await self._async_update_data()
    except UpdateFailed as err:
        raise ConfigEntryNotReady(str(err)) from err


async def _setup(hass: MagicMock, entry: MagicMock) -> bool:
    async with aiohttp.ClientSession() as session:
        with (
            patch(
                "custom_components.foxess_control.async_get_clientsession",
                return_value=session,
            ),
            patch("homeassistant.helpers.frame.report_usage"),
            patch("custom_components.foxess_control.FoxESSClient") as cloud_client,
            patch(
                "custom_components.foxess_control._register_card_frontend",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.foxess_control._register_websocket_api",
            ),
            patch.object(
                FoxESSLocalCoordinator,
                "async_config_entry_first_refresh",
                _first_refresh,
            ),
        ):
            result: bool = await async_setup_entry(hass, entry)
        cloud_client.assert_not_called()
    return result


def _close_stream_coroutines(entry: MagicMock) -> list[Any]:
    """Return the coroutines handed to the entry, closing them unawaited."""
    coros = [c.args[1] for c in entry.async_create_background_task.call_args_list]
    for coro in coros:
        coro.close()
    return coros


class TestSetup:
    @pytest.mark.asyncio
    async def test_local_entry_builds_the_local_coordinator(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(fuzzing=False, soc=61.0, local_gateway_token="fl_tok")
        hass = _hass()
        entry = _entry(foxess_sim.url, "fl_tok")

        assert await _setup(hass, entry) is True

        entry_data: FoxESSEntryData = hass.data[DOMAIN].entries["local1"]
        assert entry_data.inverter is None
        coordinator = entry_data.coordinator
        assert isinstance(coordinator, FoxESSLocalCoordinator)
        assert coordinator.client.base_url == f"{foxess_sim.url}/api/v1"
        assert coordinator.data["SoC"] == 61
        assert coordinator.data["_data_source"] == "local"
        assert hass.data[DOMAIN].config.entity_mode is False

    @pytest.mark.asyncio
    async def test_event_stream_is_started_for_the_life_of_the_entry(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Started as an *entry* background task, so HA cancels it on
        unload — no stream outlives the entry (C-025)."""
        hass = _hass()
        entry = _entry(foxess_sim.url)
        await _setup(hass, entry)
        coros = _close_stream_coroutines(entry)
        assert len(coros) == 1
        assert coros[0].__qualname__ == "FoxESSLocalCoordinator.async_run_stream"

    @pytest.mark.asyncio
    async def test_unreachable_gateway_retries_setup(self) -> None:
        hass = _hass()
        entry = _entry("http://127.0.0.1:9")
        with pytest.raises(ConfigEntryNotReady, match="unreachable"):
            await _setup(hass, entry)
        entry.async_create_background_task.assert_not_called()


class TestReadOnlyBoundary:
    def test_control_services_refuse_on_the_local_backend(self) -> None:
        hass = MagicMock()
        entry = _entry("http://gateway:18080")
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)
        dd = FoxESSControlData()
        dd.entries["local1"] = FoxESSEntryData(coordinator=MagicMock(), inverter=None)
        hass.data = {DOMAIN: dd}

        with pytest.raises(ServiceValidationError) as excinfo:
            _get_inverter(hass)
        assert excinfo.value.translation_key == "local_control_unavailable"


class TestSessionsPersistedByTheOtherBackend:
    """An entry switched to local can find smart sessions that were
    persisted while it was on the cloud (a reload keeps them for recovery).
    The local backend cannot control the inverter yet, so recovery must
    drop them rather than crash setup on the missing inverter."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("end_hour", "end_minute"),
        [(23, 59), (0, 0)],
        ids=["window_still_open", "window_passed"],
    )
    async def test_setup_succeeds_and_the_sessions_are_dropped(
        self, foxess_sim: SimulatorHandle, end_hour: int, end_minute: int
    ) -> None:
        import datetime

        noon = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.UTC)
        session = {
            "date": "2026-11-19",
            "start_hour": 0,
            "start_minute": 0,
            "end_hour": end_hour,
            "end_minute": end_minute,
        }
        hass = _hass()
        store = hass.data[DOMAIN].store
        store.async_load = AsyncMock(
            return_value={"smart_charge": dict(session), "smart_discharge": session}
        )
        entry = _entry(foxess_sim.url)

        with patch("custom_components.foxess_control.dt_util.now", return_value=noon):
            assert await _setup(hass, entry) is True
        _close_stream_coroutines(entry)

        store.async_save.assert_awaited()
        saved = store.async_save.await_args.args[0]
        assert "smart_charge" not in saved
        assert "smart_discharge" not in saved


class TestDeferredSmartDischargeIsRefused:
    """Regression: a smart discharge that would start *deferred* skipped the
    only ``_get_inverter`` call (it sits in the start-now branch), so on the
    read-only local backend it was accepted: a session was recorded,
    listeners attached with no inverter, and the session persisted. The
    refusal must come from the service call, before any state changes, so
    an automation sees it (C-020)."""

    @pytest.mark.asyncio
    async def test_refused_before_any_session_state(self) -> None:
        import datetime

        from custom_components.foxess_control import _register_services
        from tests.test_services import _get_handler, _make_call, _make_hass

        # Same shape as TestHandleSmartDischarge's deferral case: a long
        # window and moderate SoC defer the start.
        hass = _make_hass(
            battery_capacity_kwh=10.0,
            coordinator_data={"SoC": 80.0, "loadsPower": 0.0, "pvPower": 0.0},
        )
        dd: FoxESSControlData = hass.data[DOMAIN]
        dd.entries["entry1"].inverter = None
        hass.config_entries.async_get_entry.return_value.data = {
            CONF_BACKEND: BACKEND_LOCAL
        }
        _register_services(hass)
        handler = _get_handler(hass, "smart_discharge")
        listeners = "custom_components.foxess_control.smart_battery.listeners"

        with (
            patch(
                f"{listeners}.dt_util.now",
                return_value=datetime.datetime(2026, 4, 7, 17, 0, 0),
            ),
            patch(f"{listeners}.async_track_point_in_time", return_value=MagicMock()),
            patch(f"{listeners}.async_track_time_interval", return_value=MagicMock()),
            pytest.raises(ServiceValidationError) as excinfo,
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

        assert excinfo.value.translation_key == "local_control_unavailable"
        assert dd.smart_discharge_state is None
        dd.store.async_save.assert_not_awaited()
