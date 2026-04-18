"""Entity-mode coordinator — reads inverter state from HA entities.

Brand integrations that use cloud APIs provide their own coordinator.
This coordinator is for entity-mode (local Modbus) interop, where
another integration (e.g. huawei_solar, solax-modbus) exposes the
inverter's state as HA entities.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class EntityCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Read inverter state from external HA entities.

    The returned data dict has the same shape as a cloud-API coordinator
    so all sensors and smart-session logic can consume it identically:
    ``{"SoC": float, "loadsPower": float, "pvPower": float, ...}``
    """

    def __init__(
        self,
        hass: HomeAssistant,
        domain: str,
        entity_map: dict[str, str],
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=domain,
            update_interval=datetime.timedelta(seconds=update_interval_seconds),
        )
        # {polled_variable_name: entity_id}
        # e.g. {"SoC": "sensor.huawei_battery_soc"}
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

        data["_data_source"] = "modbus"
        data["_data_last_update"] = dt_util.utcnow().isoformat()
        return data


def get_coordinator_soc(
    hass: HomeAssistant,
    domain: str,
) -> float | None:
    """Read SoC from the first available coordinator in hass.data[domain]."""
    from .domain_data import get_first_coordinator

    coordinator = get_first_coordinator(hass, domain)
    if coordinator is not None and coordinator.data:
        try:
            return float(coordinator.data["SoC"])
        except (KeyError, ValueError, TypeError):
            pass
    return None
