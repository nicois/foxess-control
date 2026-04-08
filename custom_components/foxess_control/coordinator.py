"""DataUpdateCoordinator for polling the FoxESS Cloud API."""

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

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = await self.hass.async_add_executor_job(
                self.inverter.get_real_time, POLLED_VARIABLES
            )
        except Exception as err:
            raise UpdateFailed(f"Error fetching FoxESS data: {err}") from err
        return data
