"""DataUpdateCoordinator for polling the FoxESS Cloud API or external entities."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLLED_VARIABLES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .foxess.inverter import Inverter

_LOGGER = logging.getLogger(__name__)


def get_coordinator_soc(hass: HomeAssistant) -> float | None:
    """Read SoC from the first available coordinator in hass.data.

    Shared by ``_get_current_soc`` (__init__) and ``_get_soc_value`` (sensor)
    so the coordinator-fallback logic lives in exactly one place.
    """
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return None
    for key in domain_data:
        if not str(key).startswith("_"):
            entry_data = domain_data.get(key)
            if isinstance(entry_data, dict):
                coordinator = entry_data.get("coordinator")
                if coordinator is not None and coordinator.data:
                    try:
                        return float(coordinator.data["SoC"])
                    except (KeyError, ValueError, TypeError):
                        pass
    return None


class FoxESSDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch real-time variables from the FoxESS Cloud API."""

    def __init__(
        self,
        hass: HomeAssistant,
        inverter: Inverter,
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(seconds=update_interval_seconds),
        )
        self.inverter = inverter

    def _fetch_all(self) -> dict[str, Any]:
        """Fetch real-time data and work mode in a single executor job.

        Batching both API calls into one job avoids the 5-second inter-request
        throttle that would otherwise fire between two separate executor jobs.
        """
        data = self.inverter.get_real_time(POLLED_VARIABLES)

        missing = [v for v in POLLED_VARIABLES if v not in data]
        if missing:
            _LOGGER.debug("Polled variables missing from API response: %s", missing)

        try:
            mode = self.inverter.get_current_mode()
            data["_work_mode"] = mode.value if mode is not None else None
        except Exception:
            _LOGGER.debug("Failed to fetch work mode, skipping", exc_info=True)
            data["_work_mode"] = None

        return data

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = await self.hass.async_add_executor_job(
                self._fetch_all
            )
        except Exception as err:
            raise UpdateFailed(f"Error fetching FoxESS data: {err}") from err
        return data


class FoxESSEntityCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Read inverter state from external HA entities (foxess_modbus interop).

    Instead of querying the FoxESS Cloud API, this coordinator reads
    sensor/select entity states from another integration (typically
    foxess_modbus).  The returned data dict has the same shape as
    :class:`FoxESSDataCoordinator` so all sensors and smart-session
    logic can consume it identically.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_map: dict[str, str],
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(seconds=update_interval_seconds),
        )
        # {polled_variable_name: entity_id}  e.g. {"SoC": "sensor.foxess_battery_soc"}
        self._entity_map = entity_map

    async def _async_update_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for var_name, entity_id in self._entity_map.items():
            if var_name == "_work_mode":
                continue  # handled separately below
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                try:
                    data[var_name] = float(state.state)
                except (ValueError, TypeError):
                    data[var_name] = state.state

        # Work mode from a select entity
        work_mode_eid = self._entity_map.get("_work_mode")
        if work_mode_eid:
            state = self.hass.states.get(work_mode_eid)
            data["_work_mode"] = (
                state.state
                if state is not None and state.state not in ("unknown", "unavailable")
                else None
            )
        else:
            data["_work_mode"] = None

        return data
