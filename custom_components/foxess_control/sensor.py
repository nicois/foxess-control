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
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util  # noqa: F401 — test patching target

from .const import CONF_DEVICE_SERIAL, CONF_WEB_USERNAME, DOMAIN
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

PARALLEL_UPDATES = 0
SCAN_INTERVAL = datetime.timedelta(seconds=30)

# If this input_boolean exists and is "on", the integration captures log
# messages into a sensor entity queryable via the HA REST API.
DEBUG_LOG_ENTITY = "input_boolean.foxess_control_debug_log"
_DEBUG_LOG_BUFFER_SIZE = 75


def _device_info(entry: ConfigEntry, *, model: str | None = None) -> DeviceInfo:
    """Build DeviceInfo so all sensors are grouped under one device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="FoxESS",
        manufacturer="FoxESS",
        model=model,
        serial_number=entry.data.get(CONF_DEVICE_SERIAL),
        configuration_url="https://www.foxesscloud.com",
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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        from .domain_data import FoxESSControlData

        dd = self.hass.data.get(DOMAIN)
        if isinstance(dd, FoxESSControlData):
            if dd.upcoming_conflicts:
                attrs["upcoming_conflicts"] = dd.upcoming_conflicts
            if dd.replay_pending is not None:
                attrs["replay_pending"] = True
                attrs["replay_type"] = dd.replay_pending.get("type")
                attrs["replay_attempts"] = dd.replay_attempts
        return attrs


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

    entry_data = entry.runtime_data
    coordinator: FoxESSDataCoordinator | None = (
        entry_data.coordinator if entry_data else None
    )
    if coordinator is not None:
        inverter = entry_data.inverter
        model: str | None = inverter.device_type if inverter else None
        entities.extend(
            FoxESSPolledSensor(coordinator, entry, desc, model=model)
            for desc in POLLED_SENSOR_DESCRIPTIONS
        )
        entities.append(FoxESSWorkModeSensor(coordinator, entry))
        entities.append(FoxESSDataFreshnessSensor(coordinator, entry))

    # Opt-in debug log capture
    result = setup_debug_log(hass, entry)
    if result is not None:
        sensors, handlers = result
        entities.extend(sensors)
        hass.data[DOMAIN].setdefault("_debug_log_handlers", []).extend(handlers)

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
        "entity_category",
        "enabled_default",
        "display_precision",
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
        *,
        entity_category: EntityCategory | None = None,
        enabled_default: bool = True,
        display_precision: int | None = None,
    ) -> None:
        self.variable = variable
        self.name = name
        self.unique_id_suffix = unique_id_suffix
        self.device_class = device_class
        self.unit = unit
        self.state_class = state_class
        self.icon = icon
        self.entity_category = entity_category
        self.enabled_default = enabled_default
        self.display_precision = display_precision


POLLED_SENSOR_DESCRIPTIONS: list[_PolledSensorDescription] = [
    _PolledSensorDescription(
        "SoC",
        "Battery SoC",
        "battery_soc",
        SensorDeviceClass.BATTERY,
        "%",
        SensorStateClass.MEASUREMENT,
        "mdi:battery",
        display_precision=0,
    ),
    _PolledSensorDescription(
        "batChargePower",
        "Charge Rate",
        "bat_charge_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:battery-charging",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "batDischargePower",
        "Discharge Rate",
        "bat_discharge_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:battery-arrow-down",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "loadsPower",
        "House Load",
        "loads_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:home-lightning-bolt",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "pvPower",
        "Solar Power",
        "pv_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-power",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "batTemperature",
        "Battery Temperature",
        "bat_temperature",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer",
        entity_category=EntityCategory.DIAGNOSTIC,
        display_precision=1,
    ),
    _PolledSensorDescription(
        "bmsBatteryTemperature",
        "BMS Battery Temperature",
        "bms_battery_temperature",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer-low",
        display_precision=1,
    ),
    _PolledSensorDescription(
        "gridConsumptionPower",
        "Grid Consumption",
        "grid_consumption",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:transmission-tower-import",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "feedinPower",
        "Grid Feed-in",
        "feedin_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:transmission-tower-export",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "generationPower",
        "Generation",
        "generation_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-power-variant",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "batVolt",
        "Battery Voltage",
        "bat_volt",
        SensorDeviceClass.VOLTAGE,
        "V",
        SensorStateClass.MEASUREMENT,
        "mdi:flash-triangle",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=1,
    ),
    _PolledSensorDescription(
        "batCurrent",
        "Battery Current",
        "bat_current",
        SensorDeviceClass.CURRENT,
        "A",
        SensorStateClass.MEASUREMENT,
        "mdi:current-dc",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=1,
    ),
    _PolledSensorDescription(
        "pv1Power",
        "PV1 Power",
        "pv1_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-panel",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=2,
    ),
    _PolledSensorDescription(
        "pv2Power",
        "PV2 Power",
        "pv2_power",
        SensorDeviceClass.POWER,
        "kW",
        SensorStateClass.MEASUREMENT,
        "mdi:solar-panel",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=2,
    ),
    _PolledSensorDescription(
        "ambientTemperation",
        "Ambient Temperature",
        "ambient_temp",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=1,
    ),
    _PolledSensorDescription(
        "invTemperation",
        "Inverter Temperature",
        "inverter_temp",
        SensorDeviceClass.TEMPERATURE,
        "°C",
        SensorStateClass.MEASUREMENT,
        "mdi:thermometer-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=1,
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
        display_precision=2,
    ),
    _PolledSensorDescription(
        "gridConsumption",
        "Grid Consumption Energy",
        "grid_consumption_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:transmission-tower-import",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "generation",
        "Solar Generation Energy",
        "generation_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:solar-power-variant",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "chargeEnergyToTal",
        "Battery Charge Energy",
        "charge_energy_total",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-charging",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "dischargeEnergyToTal",
        "Battery Discharge Energy",
        "discharge_energy_total",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-arrow-down",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "loads",
        "House Load Energy",
        "loads_energy",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:home-lightning-bolt",
        display_precision=2,
    ),
    _PolledSensorDescription(
        "energyThroughput",
        "Battery Throughput",
        "energy_throughput",
        SensorDeviceClass.ENERGY,
        "kWh",
        SensorStateClass.TOTAL_INCREASING,
        "mdi:battery-sync",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=2,
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
        entity_category=EntityCategory.DIAGNOSTIC,
        display_precision=2,
    ),
    _PolledSensorDescription(
        "RVolt",
        "Grid Voltage",
        "grid_voltage",
        SensorDeviceClass.VOLTAGE,
        "V",
        SensorStateClass.MEASUREMENT,
        "mdi:flash-triangle",
        entity_category=EntityCategory.DIAGNOSTIC,
        display_precision=1,
    ),
    _PolledSensorDescription(
        "RCurrent",
        "Grid Current",
        "grid_current",
        SensorDeviceClass.CURRENT,
        "A",
        SensorStateClass.MEASUREMENT,
        "mdi:current-ac",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=1,
    ),
    _PolledSensorDescription(
        "RFreq",
        "Grid Frequency",
        "grid_frequency",
        None,
        "Hz",
        SensorStateClass.MEASUREMENT,
        "mdi:sine-wave",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=1,
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
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        display_precision=2,
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
        *,
        model: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._variable = desc.variable
        self._attr_unique_id = f"{entry.entry_id}_{desc.unique_id_suffix}"
        self._attr_translation_key = desc.unique_id_suffix
        self._attr_device_class = desc.device_class
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_state_class = desc.state_class
        self._attr_icon = desc.icon
        self._attr_device_info = _device_info(entry, model=model)
        if desc.entity_category is not None:
            self._attr_entity_category = desc.entity_category
        if not desc.enabled_default:
            self._attr_entity_registry_enabled_default = False
        if desc.display_precision is not None:
            self._attr_suggested_display_precision = desc.display_precision
        # Only expose data_source when multiple sources are configured
        self._has_multiple_sources = bool(entry.data.get(CONF_WEB_USERNAME))

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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._has_multiple_sources or self.coordinator.data is None:
            return None
        source = self.coordinator.data.get("_data_source")
        if source is None:
            return None
        return {"data_source": source}


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
        self._attr_translation_key = "work_mode"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        val: str | None = self.coordinator.data.get("_work_mode")
        return val


class FoxESSDataFreshnessSensor(CoordinatorEntity[FoxESSDataCoordinator], SensorEntity):
    """Sensor exposing data source and staleness for Lovelace cards."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:clock-check-outline"

    def __init__(
        self,
        coordinator: FoxESSDataCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_data_freshness"
        self._attr_translation_key = "data_freshness"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("_data_source")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        last_update = self.coordinator.data.get("_data_last_update")
        if last_update is None:
            return None
        now = dt_util.utcnow()
        try:
            updated_at = datetime.datetime.fromisoformat(last_update)
            age_seconds = round((now - updated_at).total_seconds())
        except (ValueError, TypeError):
            return {"last_update": last_update}
        return {
            "last_update": last_update,
            "age_seconds": max(0, age_seconds),
        }


# ---------------------------------------------------------------------------
# Debug log capture — opt-in via input_boolean.foxess_control_debug_log
# ---------------------------------------------------------------------------


class _DebugLogHandler(logging.Handler):
    """Logging handler that captures records into a bounded deque."""

    def __init__(
        self,
        buffer: collections.deque[dict[str, Any]],
        original_level: int = logging.NOTSET,
    ) -> None:
        super().__init__()
        self._buffer = buffer
        self.original_level = original_level

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry: dict[str, Any] = {
                "t": datetime.datetime.fromtimestamp(
                    record.created, tz=datetime.UTC
                ).isoformat(timespec="seconds"),
                "level": record.levelname,
                "msg": self.format(record),
            }
            session: dict[str, Any] = getattr(record, "session", {})
            if session:
                entry["session"] = session
            self._buffer.append(entry)
        except Exception:  # noqa: BLE001
            self.handleError(record)


class DebugLogSensor(SensorEntity):
    """Sensor exposing recent foxess_control log entries as attributes."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:math-log"
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = frozenset({"entries"})

    def __init__(
        self,
        entry: ConfigEntry,
        buffer: collections.deque[dict[str, Any]],
    ) -> None:
        self._buffer = buffer
        self._attr_unique_id = f"{entry.entry_id}_debug_log"
        self._attr_translation_key = "debug_log"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self._buffer)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entries": list(self._buffer)}


class InfoLogSensor(SensorEntity):
    """Sensor exposing recent INFO+ log entries as attributes.

    Same rolling buffer as DebugLogSensor but captures only INFO and
    above, so operational messages (session events, BMS fetches, mode
    changes) are retained much longer than in the DEBUG buffer.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:information-outline"
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = frozenset({"entries"})

    def __init__(
        self,
        entry: ConfigEntry,
        buffer: collections.deque[dict[str, Any]],
    ) -> None:
        self._buffer = buffer
        self._attr_unique_id = f"{entry.entry_id}_info_log"
        self._attr_translation_key = "info_log"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self._buffer)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entries": list(self._buffer)}


class _InitDebugLogHandler(logging.Handler):
    """Logging handler that captures the first N records, then stops."""

    def __init__(
        self,
        buffer: list[dict[str, Any]],
        maxlen: int,
        original_level: int = logging.NOTSET,
    ) -> None:
        super().__init__()
        self._buffer = buffer
        self._maxlen = maxlen
        self.original_level = original_level

    def emit(self, record: logging.LogRecord) -> None:
        if len(self._buffer) >= self._maxlen:
            return
        try:
            entry: dict[str, Any] = {
                "t": datetime.datetime.fromtimestamp(
                    record.created, tz=datetime.UTC
                ).isoformat(timespec="seconds"),
                "level": record.levelname,
                "msg": self.format(record),
            }
            session: dict[str, Any] = getattr(record, "session", {})
            if session:
                entry["session"] = session
            self._buffer.append(entry)
        except Exception:  # noqa: BLE001
            self.handleError(record)


class InitDebugLogSensor(SensorEntity):
    """Sensor capturing startup log entries (non-wrapping)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:flag-checkered"
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = frozenset({"entries"})

    def __init__(
        self,
        entry: ConfigEntry,
        buffer: list[dict[str, Any]],
        maxlen: int,
    ) -> None:
        self._buffer = buffer
        self._maxlen = maxlen
        self._attr_unique_id = f"{entry.entry_id}_init_debug_log"
        self._attr_translation_key = "init_debug_log"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self._buffer)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"entries": list(self._buffer), "capacity": self._maxlen}


def setup_debug_log(
    hass: Any,
    entry: ConfigEntry,
) -> tuple[list[SensorEntity], list[logging.Handler]] | None:
    """Attach log handlers and return sensors if debug logging is opted-in."""
    state = hass.states.get(DEBUG_LOG_ENTITY)
    if state is None or state.state != "on":
        return None

    logger = logging.getLogger("custom_components.foxess_control")
    original_level = logger.level
    sensors: list[SensorEntity] = []
    handlers: list[logging.Handler] = []

    buf: collections.deque[dict[str, Any]] = collections.deque(
        maxlen=_DEBUG_LOG_BUFFER_SIZE
    )
    handler = _DebugLogHandler(buf, original_level=original_level)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    sensors.append(DebugLogSensor(entry, buf))
    handlers.append(handler)

    info_buf: collections.deque[dict[str, Any]] = collections.deque(
        maxlen=_DEBUG_LOG_BUFFER_SIZE
    )
    info_handler = _DebugLogHandler(info_buf, original_level=original_level)
    info_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    info_handler.setLevel(logging.INFO)
    logger.addHandler(info_handler)
    sensors.append(InfoLogSensor(entry, info_buf))
    handlers.append(info_handler)

    init_buf: list[dict[str, Any]] = []
    init_handler = _InitDebugLogHandler(
        init_buf, maxlen=_DEBUG_LOG_BUFFER_SIZE, original_level=original_level
    )
    init_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    init_handler.setLevel(logging.DEBUG)
    logger.addHandler(init_handler)
    sensors.append(InitDebugLogSensor(entry, init_buf, _DEBUG_LOG_BUFFER_SIZE))
    handlers.append(init_handler)

    if logger.getEffectiveLevel() > logging.DEBUG:
        logger.setLevel(logging.DEBUG)

    return sensors, handlers
