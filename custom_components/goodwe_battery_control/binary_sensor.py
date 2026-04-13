"""GoodWe Battery Control binary sensor platform."""

from __future__ import annotations

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


class SmartChargeActiveSensor(_SmartChargeActiveSensor):
    """GoodWe smart charge active binary sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class SmartDischargeActiveSensor(_SmartDischargeActiveSensor):
    """GoodWe smart discharge active binary sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GoodWe Battery Control binary sensors."""
    async_add_entities(
        [
            SmartChargeActiveSensor(hass, entry),
            SmartDischargeActiveSensor(hass, entry),
        ]
    )
