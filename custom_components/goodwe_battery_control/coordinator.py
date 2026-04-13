"""GoodWe entity-mode coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN
from .smart_battery.coordinator import EntityCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class GoodWeEntityCoordinator(EntityCoordinator):
    """Read inverter state from external HA entities (GoodWe interop).

    Thin subclass that binds the shared ``EntityCoordinator`` to the
    ``goodwe_battery_control`` domain.
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
