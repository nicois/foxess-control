"""Binary sensors for FoxESS Control smart charge/discharge status."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from .const import DOMAIN
from .sensor import _device_info
from .smart_battery.sensor_base import (
    SmartChargeActiveSensor as _SmartChargeActiveSensor,
)
from .smart_battery.sensor_base import (
    SmartDischargeActiveSensor as _SmartDischargeActiveSensor,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0
SCAN_INTERVAL = datetime.timedelta(seconds=30)


class SmartChargeActiveSensor(_SmartChargeActiveSensor):
    """FoxESS smart charge active binary sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class SmartDischargeActiveSensor(_SmartDischargeActiveSensor):
    """FoxESS smart discharge active binary sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FoxESS Control binary sensors."""
    async_add_entities(
        [
            SmartChargeActiveSensor(hass, entry),
            SmartDischargeActiveSensor(hass, entry),
        ]
    )
