"""Sensors for FoxESS Control inverter override status."""

from __future__ import annotations

import collections
import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util  # noqa: F401 — test patching target

from .const import DOMAIN
from .coordinator import FoxESSDataCoordinator
from .smart_battery.sensor_base import (
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
from .smart_battery.sensor_base import (
    get_coordinator_value,
    get_soc_value,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

SCAN_INTERVAL = datetime.timedelta(seconds=30)

# If this input_boolean exists and is "on", the integration captures log
# messages into a sensor entity queryable via the HA REST API.
DEBUG_LOG_ENTITY = "input_boolean.foxess_control_debug_log"
_DEBUG_LOG_BUFFER_SIZE = 200


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Build DeviceInfo so all sensors are grouped under one device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="FoxESS",
        manufacturer="FoxESS",
    )


# ---------------------------------------------------------------------------
# FoxESS-specific thin subclasses — bind DOMAIN + _device_info
# ---------------------------------------------------------------------------


class InverterOverrideStatusSensor(_OverrideStatusSensor):
    """FoxESS override status sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class SmartOperationsOverviewSensor(_SmartOperationsOverviewSensor):
    """FoxESS smart operations overview sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class ChargePowerSensor(_ChargePowerSensor):
    """FoxESS charge power sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class ChargeWindowSensor(_ChargeWindowSensor):
    """FoxESS charge window sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class ChargeRemainingSensor(_ChargeRemainingSensor):
    """FoxESS charge remaining sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class DischargePowerSensor(_DischargePowerSensor):
    """FoxESS discharge power sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class DischargeWindowSensor(_DischargeWindowSensor):
    """FoxESS discharge window sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class DischargeRemainingSensor(_DischargeRemainingSensor):
    """FoxESS discharge remaining sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


class BatteryForecastSensor(_BatteryForecastSensor):
    """FoxESS battery forecast sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, DOMAIN, _device_info(entry))


# ---------------------------------------------------------------------------
# Re-export helpers used by tests
# ---------------------------------------------------------------------------


def _get_soc_value(hass: Any) -> float | None:
    """Read the current SoC from the coordinator."""
    return get_soc_value(hass, DOMAIN)


def _get_coordinator_value(hass: HomeAssistant, key: str) -> float | None:
    """Read a numeric value from the first available coordinator."""
    return get_coordinator_value(hass, DOMAIN, key)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FoxESS Control sensors."""
    entities: list[SensorEntity] = [
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

    coordinator: FoxESSDataCoordinator | None = (
        hass.data[DOMAIN].get(entry.entry_id, {}).get("coordinator")
    )
    if coordinator is not None:
        entities.extend(
            FoxESSPolledSensor(coordinator, entry, desc)
            for desc in POLLED_SENSOR_DESCRIPTIONS
        )
        entities.append(FoxESSWorkModeSensor(coordinator, entry))

    # Opt-in debug log capture
    result = setup_debug_log(hass, entry)
    if result is not None:
        sensor, handler = result
        entities.append(sensor)
        hass.data[DOMAIN].setdefault("_debug_log_handlers", []).append(handler)

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Coordinator-backed sensors — polled from the FoxESS Cloud API
# ---------------------------------------------------------------------------


class _PolledSensorDescription:
    """Descriptor for a coordinator-backed sensor."""

    __slots__ = (
        "variable",
        "name",
        "unique_id_suffix",
        "device_class",
        "unit",
        "state_class",
        "icon",
    )

    def __init__(
        self,
        variable: str,
        name: str,
        unique_id_suffix: str,
        device_class: SensorDeviceClass | None,
        unit: str,
        state_class: SensorStateClass,
        icon: str,
    ) -> None:
        self.variable = variable
        self.name = name
        self.unique_id_suffix = unique_id_suffix
        self.device_class = device_class
        self.unit = unit
        self.state_class = state_class
        self.icon = icon


POLLED_SENSOR_DESCRIPTIONS: list[_PolledSensorDescription] = [
    _PolledSensorDescription(
        "SoC",
        "Battery SoC",
        "battery_soc",
        SensorDeviceClass.BATTERY,
        "%",
        SensorStateClass.MEASUREMENT,
        "mdi:battery",
    ),
    _PolledSensorDescription(
        "batChargePower",
        "Charge Rate",
        "bat_charge_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:battery-charging",
    ),
    _PolledSensorDescription(
        "batDischargePower",
        "Discharge Rate",
        "bat_discharge_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:battery-arrow-down",
    ),
    _PolledSensorDescription(
        "loadsPower",
        "House Load",
        "loads_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:home-lightning-bolt",
    ),
    _PolledSensorDescription(
        "pvPower",
        "Solar Power",
        "pv_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-power",
    ),
    _PolledSensorDescription(
        "batTemperature",
        "Battery Temperature",
        "bat_temperature",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer",
    ),
    _PolledSensorDescription(
        "gridConsumptionPower",
        "Grid Consumption",
        "grid_consumption",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:transmission-tower-import",
    ),
    _PolledSensorDescription(
        "feedinPower",
        "Grid Feed-in",
        "feedin_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:transmission-tower-export",
    ),
    _PolledSensorDescription(
        "generationPower",
        "Generation",
        "generation_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-power-variant",
    ),
    _PolledSensorDescription(
        "batVolt",
        "Battery Voltage",
        "bat_volt",
        SensorDeviceClass.VOLTAGE,
        "V",
        SensorStateClass.MEASUREMENT,
        "mdi:flash-triangle",
    ),
    _PolledSensorDescription(
        "batCurrent",
        "Battery Current",
        "bat_current",
        SensorDeviceClass.CURRENT,
        "A",
        SensorStateClass.MEASUREMENT,
        "mdi:current-dc",
    ),
    _PolledSensorDescription(
        "pv1Power",
        "PV1 Power",
        "pv1_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-panel",
    ),
    _PolledSensorDescription(
        "pv2Power",
        "PV2 Power",
        "pv2_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-panel",
    ),
    _PolledSensorDescription(
        "ambientTemperation",
        "Ambient Temperature",
        "ambient_temp",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer",
    ),
    _PolledSensorDescription(
        "invTemperation",
        "Inverter Temperature",
        "inverter_temp",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer-alert",
    ),
    # Cumulative energy counters (lifetime kWh)
    _PolledSensorDescription(
        "feedin",
        "Grid Feed-in Energy",
        "feedin_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:transmission-tower-export",
    ),
    _PolledSensorDescription(
        "gridConsumption",
        "Grid Consumption Energy",
        "grid_consumption_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:transmission-tower-import",
    ),
    _PolledSensorDescription(
        "generation",
        "Solar Generation Energy",
        "generation_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:solar-power-variant",
    ),
    _PolledSensorDescription(
        "chargeEnergyToTal",
        "Battery Charge Energy",
        "charge_energy_total",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-charging",
    ),
    _PolledSensorDescription(
        "dischargeEnergyToTal",
        "Battery Discharge Energy",
        "discharge_energy_total",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-arrow-down",
    ),
    _PolledSensorDescription(
        "loads",
        "House Load Energy",
        "loads_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:home-lightning-bolt",
    ),
    _PolledSensorDescription(
        "energyThroughput",
        "Battery Throughput",
        "energy_throughput",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-sync",
    ),
    # Grid connection
    _PolledSensorDescription(
        "meterPower",
        "Grid Meter Power",
        "meter_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:meter-electric",
    ),
    _PolledSensorDescription(
        "RVolt",
        "Grid Voltage",
        "grid_voltage",
        SensorDeviceClass.VOLTAGE,
        "V",
        SensorStateClass.MEASUREMENT,
        "mdi:flash-triangle",
    ),
    _PolledSensorDescription(
        "RCurrent",
        "Grid Current",
        "grid_current",
        SensorDeviceClass.CURRENT,
        "A",
        SensorStateClass.MEASUREMENT,
        "mdi:current-ac",
    ),
    _PolledSensorDescription(
        "RFreq",
        "Grid Frequency",
        "grid_frequency",
        None,
        "Hz",
        SensorStateClass.MEASUREMENT,
        "mdi:sine-wave",
    ),
    # EPS / backup
    _PolledSensorDescription(
        "epsPower",
        "EPS Power",
        "eps_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:power-plug-battery",
    ),
]


class FoxESSPolledSensor(CoordinatorEntity[FoxESSDataCoordinator], SensorEntity):
    """Sensor backed by the DataUpdateCoordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FoxESSDataCoordinator,
        entry: ConfigEntry,
        desc: _PolledSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._variable = desc.variable
        self._attr_unique_id = f"{entry.entry_id}_{desc.unique_id_suffix}"
        self._attr_name = desc.name
        self._attr_device_class = desc.device_class
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_state_class = desc.state_class
        self._attr_icon = desc.icon
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._variable)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None


class FoxESSWorkModeSensor(CoordinatorEntity[FoxESSDataCoordinator], SensorEntity):
    """Sensor showing the inverter's current work mode."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:state-machine"

    def __init__(
        self,
        coordinator: FoxESSDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_work_mode"
        self._attr_name = "Work Mode"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        val: str | None = self.coordinator.data.get("_work_mode")
        return val


# ---------------------------------------------------------------------------
# Debug log capture — opt-in via input_boolean.foxess_control_debug_log
# ---------------------------------------------------------------------------


class _DebugLogHandler(logging.Handler):
    """Logging handler that captures records into a bounded deque."""

    def __init__(
        self,
        buffer: collections.deque[dict[str, str]],
        original_level: int = logging.NOTSET,
    ) -> None:
        super().__init__()
        self._buffer = buffer
        self.original_level = original_level

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(
                {
                    "t": datetime.datetime.fromtimestamp(
                        record.created, tz=datetime.UTC
                    ).isoformat(timespec="seconds"),
                    "level": record.levelname,
                    "msg": self.format(record),
                }
            )
        except Exception:  # noqa: BLE001
            self.handleError(record)


class DebugLogSensor(SensorEntity):
    """Sensor exposing recent foxess_control log entries as attributes."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:math-log"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        entry: ConfigEntry,
        buffer: collections.deque[dict[str, str]],
    ) -> None:
        self._buffer = buffer
        self._attr_unique_id = f"{entry.entry_id}_debug_log"
        self._attr_name = "Debug Log"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self._buffer)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entries": list(self._buffer)}


def setup_debug_log(
    hass: Any,
    entry: ConfigEntry,
) -> tuple[DebugLogSensor, _DebugLogHandler] | None:
    """Attach a log handler and return a sensor if debug logging is opted-in."""
    state = hass.states.get(DEBUG_LOG_ENTITY)
    if state is None or state.state != "on":
        return None

    buf: collections.deque[dict[str, str]] = collections.deque(
        maxlen=_DEBUG_LOG_BUFFER_SIZE
    )
    logger = logging.getLogger("custom_components.foxess_control")
    handler = _DebugLogHandler(buf, original_level=logger.level)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.DEBUG:
        logger.setLevel(logging.DEBUG)

    sensor = DebugLogSensor(entry, buf)
    return sensor, handler
