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
from homeassistant.util.unit_conversion import (
    BaseUnitConverter,
    EnergyConverter,
    PowerConverter,
    TemperatureConverter,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_SENTINEL = object()

_CONVERTER_FOR_UNIT: dict[str, type[BaseUnitConverter]] = {}
for _conv in (PowerConverter, EnergyConverter, TemperatureConverter):
    for _unit in _conv.VALID_UNITS:
        if _unit is not None:
            _CONVERTER_FOR_UNIT[_unit] = _conv


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
        entity_map: dict[str, tuple[str, str]],
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=domain,
            update_interval=datetime.timedelta(seconds=update_interval_seconds),
        )
        # {polled_variable_name: (entity_id, expected_unit)}
        # e.g. {"SoC": ("sensor.huawei_battery_soc", "%")}
        self._entity_map = entity_map
        self._entity_first_read: dict[str, bool] = {}

    def _log_first_read(
        self, var_name: str, entity_id: str, value: Any = _SENTINEL
    ) -> None:
        """Log the first successful read or first failure for each entity."""
        seen = getattr(self, "_entity_first_read", None)
        if seen is None:
            seen = self._entity_first_read = {}
        if var_name in seen:
            return
        self._entity_first_read[var_name] = True
        if value is not _SENTINEL:
            _LOGGER.info(
                "Entity read OK: %s → %s = %s",
                entity_id,
                var_name,
                value,
            )
        else:
            state_obj = self.hass.states.get(entity_id)
            raw = state_obj.state if state_obj is not None else "<not found>"
            _LOGGER.warning(
                "Entity read FAILED: %s → %s (state=%r)",
                entity_id,
                var_name,
                raw,
            )

    @staticmethod
    def _convert_unit(
        value: float, source_unit: str | None, expected_unit: str
    ) -> float:
        """Convert *value* from *source_unit* to *expected_unit*."""
        if not expected_unit or not source_unit:
            return value
        if source_unit == expected_unit:
            return value
        converter = _CONVERTER_FOR_UNIT.get(expected_unit)
        if converter is not None and source_unit in converter.VALID_UNITS:
            return converter.convert(value, source_unit, expected_unit)
        return value

    async def _async_update_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for var_name, (entity_id, expected_unit) in self._entity_map.items():
            if var_name == "_work_mode":
                continue  # handled separately below
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                try:
                    raw = float(state.state)
                    source_unit = state.attributes.get("unit_of_measurement")
                    data[var_name] = self._convert_unit(raw, source_unit, expected_unit)
                except (ValueError, TypeError):
                    data[var_name] = state.state
                self._log_first_read(var_name, entity_id, data[var_name])
            else:
                self._log_first_read(var_name, entity_id)

        # Work mode from a select entity
        work_mode_entry = self._entity_map.get("_work_mode")
        if work_mode_entry:
            work_mode_eid = work_mode_entry[0]
            state = self.hass.states.get(work_mode_eid)
            if state is not None and state.state not in ("unknown", "unavailable"):
                data["_work_mode"] = state.state
                self._log_first_read("_work_mode", work_mode_eid, state.state)
            else:
                data["_work_mode"] = None
                self._log_first_read("_work_mode", work_mode_eid)
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
