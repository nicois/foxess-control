"""DataUpdateCoordinator for polling the FoxESS Cloud API or external entities."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLLED_VARIABLES
from .smart_battery.coordinator import EntityCoordinator as _EntityCoordinator
from .smart_battery.coordinator import get_coordinator_soc as _get_coordinator_soc

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .foxess.inverter import Inverter

_LOGGER = logging.getLogger(__name__)


def get_coordinator_soc(hass: HomeAssistant) -> float | None:
    """Read SoC from the first available coordinator in hass.data."""
    return _get_coordinator_soc(hass, DOMAIN)


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
        """Fetch real-time data and work mode in a single executor job."""
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

    def inject_realtime_data(self, ws_data: dict[str, Any]) -> None:
        """Merge WebSocket real-time data into the current coordinator data.

        Only overlays the subset of variables the WebSocket provides
        (SoC, power values).  The full REST-polled dataset remains the
        base, so variables not in the WebSocket stream (cumulative energy
        counters, temperatures, etc.) stay current from the last REST poll.
        """
        if self.data is None:
            return
        # Skip if nothing actually changed (avoids redundant entity updates)
        if all(self.data.get(k) == v for k, v in ws_data.items()):
            return
        merged = dict(self.data)
        merged.update(ws_data)
        self.async_set_updated_data(merged)


class FoxESSEntityCoordinator(_EntityCoordinator):
    """Read inverter state from external HA entities (foxess_modbus interop).

    Thin subclass that binds the shared ``EntityCoordinator`` to the
    ``foxess_control`` domain.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_map: dict[str, str],
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            domain=DOMAIN,
            entity_map=entity_map,
            update_interval_seconds=update_interval_seconds,
        )
