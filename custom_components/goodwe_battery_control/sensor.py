"""GoodWe Battery Control sensor platform."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .smart_battery.sensor_base import (  # noqa: E501
    BatteryForecastSensor as _BatteryForecastSensor,
)
from .smart_battery.sensor_base import (
    ChargePowerSensor as _ChargePowerSensor,
)
from .smart_battery.sensor_base import (
    ChargeRemainingSensor as _ChargeRemainingSensor,
)
from .smart_battery.sensor_base import (
    ChargeWindowSensor as _ChargeWindowSensor,
)
from .smart_battery.sensor_base import (
    DischargePowerSensor as _DischargePowerSensor,
)
from .smart_battery.sensor_base import (
    DischargeRemainingSensor as _DischargeRemainingSensor,
)
from .smart_battery.sensor_base import (
    DischargeWindowSensor as _DischargeWindowSensor,
)
from .smart_battery.sensor_base import (
    OverrideStatusSensor as _OverrideStatusSensor,
)
from .smart_battery.sensor_base import (
    SmartOperationsOverviewSensor as _SmartOperationsOverviewSensor,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build DeviceInfo so all sensors are grouped under one device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="GoodWe",
        manufacturer="GoodWe",
    )


class InverterOverrideStatusSensor(_OverrideStatusSensor):
    """GoodWe override status sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class SmartOperationsOverviewSensor(_SmartOperationsOverviewSensor):
    """GoodWe smart operations overview sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class ChargePowerSensor(_ChargePowerSensor):
    """GoodWe smart charge power sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class ChargeWindowSensor(_ChargeWindowSensor):
    """GoodWe smart charge window sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class ChargeRemainingSensor(_ChargeRemainingSensor):
    """GoodWe smart charge remaining sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class DischargePowerSensor(_DischargePowerSensor):
    """GoodWe smart discharge power sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class DischargeWindowSensor(_DischargeWindowSensor):
    """GoodWe smart discharge window sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class DischargeRemainingSensor(_DischargeRemainingSensor):
    """GoodWe smart discharge remaining sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class BatteryForecastSensor(_BatteryForecastSensor):
    """GoodWe battery forecast sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GoodWe Battery Control sensors."""
    async_add_entities(
        [
            InverterOverrideStatusSensor(hass, entry),
            SmartOperationsOverviewSensor(hass, entry),
            ChargePowerSensor(hass, entry),
            ChargeWindowSensor(hass, entry),
            ChargeRemainingSensor(hass, entry),
            DischargePowerSensor(hass, entry),
            DischargeWindowSensor(hass, entry),
            DischargeRemainingSensor(hass, entry),
            BatteryForecastSensor(hass, entry),
        ]
    )
