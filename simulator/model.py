"""In-memory inverter state machine.

Models a single FoxESS inverter with battery, solar, load, and grid
power flows.  The model ticks forward in discrete steps, computing
power balance and SoC changes based on the active work mode.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScheduleGroup:
    """A single schedule time slot."""

    enable: int = 1
    startHour: int = 0
    startMinute: int = 0
    endHour: int = 23
    endMinute: int = 59
    workMode: str = "SelfUse"
    minSocOnGrid: int = 10
    fdSoc: int = 100
    fdPwr: int = 10500

    def to_dict(self) -> dict[str, Any]:
        return {
            "enable": self.enable,
            "startHour": self.startHour,
            "startMinute": self.startMinute,
            "endHour": self.endHour,
            "endMinute": self.endMinute,
            "workMode": self.workMode,
            "minSocOnGrid": self.minSocOnGrid,
            "fdSoc": self.fdSoc,
            "fdPwr": self.fdPwr,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ScheduleGroup:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# Placeholder groups returned by the real API for unused slots
def _make_placeholder() -> dict[str, Any]:
    return {
        "enable": 0,
        "startHour": 0,
        "startMinute": 0,
        "endHour": 0,
        "endMinute": 0,
        "workMode": "Invalid",
        "minSocOnGrid": 0,
        "fdSoc": 0,
        "fdPwr": 0,
    }


def _jitter(value: float, pct: float = 0.02) -> float:
    """Add ±pct random noise to a value (default ±2%)."""
    import random

    if abs(value) < 0.001:
        return value
    return value * (1.0 + random.uniform(-pct, pct))


@dataclass
class InverterModel:
    """Simulated FoxESS inverter.

    When ``fuzzing`` is enabled, power values and SoC readings include
    small random jitter (±2%) to prevent tests from overfitting to
    exact values.
    """

    # Identity
    device_sn: str = "SIM0001"
    plant_id: str = "sim-plant-001"

    # Battery
    soc: float = 50.0
    battery_capacity_kwh: float = 10.0
    max_power_w: int = 10500

    # External power (set via backchannel)
    solar_kw: float = 0.0
    load_kw: float = 0.5

    # Fuzzing: add noise to readings to prevent test overfitting
    fuzzing: bool = True

    # Derived power flows (computed by tick)
    bat_charge_kw: float = 0.0
    bat_discharge_kw: float = 0.0
    grid_import_kw: float = 0.0
    grid_export_kw: float = 0.0

    # Schedule
    schedule_groups: list[ScheduleGroup] = field(default_factory=list)
    schedule_enabled: bool = False

    # Min SoC thresholds
    min_soc: int = 10
    min_soc_on_grid: int = 10

    # Cumulative energy counters (kWh)
    feedin_total_kwh: float = 0.0
    grid_consumption_total_kwh: float = 0.0
    charge_total_kwh: float = 0.0
    discharge_total_kwh: float = 0.0
    generation_total_kwh: float = 0.0
    loads_total_kwh: float = 0.0

    # Simulated time (starts at real time, advanced by tick/fast_forward)
    sim_time: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(tz=datetime.UTC)
    )

    # Fault injection
    active_fault: str | None = None
    fault_remaining: int = 0  # auto-clear after N requests (0 = permanent)

    # WS config overrides
    ws_unit: str = "W"  # "W" or "kW"
    ws_time_diff: int = 5  # normal

    def get_active_mode(self) -> str:
        """Return the work mode active at the current simulated time."""
        if not self.schedule_enabled or not self.schedule_groups:
            return "SelfUse"

        now_min = self.sim_time.hour * 60 + self.sim_time.minute
        for g in self.schedule_groups:
            if not g.enable:
                continue
            if g.workMode in ("Invalid", ""):
                continue
            start = g.startHour * 60 + g.startMinute
            end = g.endHour * 60 + g.endMinute
            if start <= now_min < end:
                return g.workMode
        return "SelfUse"

    def get_active_group(self) -> ScheduleGroup | None:
        """Return the schedule group active at the current simulated time."""
        now_min = self.sim_time.hour * 60 + self.sim_time.minute
        for g in self.schedule_groups:
            if not g.enable or g.workMode in ("Invalid", ""):
                continue
            start = g.startHour * 60 + g.startMinute
            end = g.endHour * 60 + g.endMinute
            if start <= now_min < end:
                return g
        return None

    def tick(self, dt_seconds: float) -> None:
        """Advance the model by dt_seconds."""
        dt_hours = dt_seconds / 3600.0
        mode = self.get_active_mode()
        group = self.get_active_group()

        # Reset derived flows
        self.bat_charge_kw = 0.0
        self.bat_discharge_kw = 0.0
        self.grid_import_kw = 0.0
        self.grid_export_kw = 0.0

        if mode == "ForceCharge":
            # Charge at fdPwr from grid, solar assists
            target_charge_kw = (
                (group.fdPwr / 1000.0) if group else (self.max_power_w / 1000.0)
            )
            # Solar goes to load first, remainder to battery
            solar_to_load = min(self.solar_kw, self.load_kw)
            solar_to_bat = self.solar_kw - solar_to_load
            grid_to_load = self.load_kw - solar_to_load
            # Battery charges from grid + remaining solar
            self.bat_charge_kw = min(target_charge_kw, self.max_power_w / 1000.0)
            self.grid_import_kw = grid_to_load + max(
                0, self.bat_charge_kw - solar_to_bat
            )

        elif mode == "ForceDischarge":
            # Discharge at fdPwr
            target_discharge_kw = (
                (group.fdPwr / 1000.0) if group else (self.max_power_w / 1000.0)
            )
            self.bat_discharge_kw = min(target_discharge_kw, self.max_power_w / 1000.0)
            # Solar serves load, battery covers rest + exports
            net_export = self.bat_discharge_kw + self.solar_kw - self.load_kw
            if net_export > 0:
                self.grid_export_kw = net_export
            else:
                self.grid_import_kw = -net_export

        elif mode == "Feedin":
            # Export at fdPwr
            target_kw = (group.fdPwr / 1000.0) if group else (self.max_power_w / 1000.0)
            self.bat_discharge_kw = min(target_kw, self.max_power_w / 1000.0)
            net = self.bat_discharge_kw + self.solar_kw - self.load_kw
            if net > 0:
                self.grid_export_kw = net
            else:
                self.grid_import_kw = -net

        else:  # SelfUse
            # Solar serves load, excess charges battery
            solar_to_load = min(self.solar_kw, self.load_kw)
            remaining_load = self.load_kw - solar_to_load
            excess_solar = self.solar_kw - solar_to_load

            if excess_solar > 0:
                self.bat_charge_kw = min(excess_solar, self.max_power_w / 1000.0)
                leftover = excess_solar - self.bat_charge_kw
                if leftover > 0:
                    self.grid_export_kw = leftover
            if remaining_load > 0:
                # Battery covers remaining load
                self.bat_discharge_kw = min(remaining_load, self.max_power_w / 1000.0)
                shortfall = remaining_load - self.bat_discharge_kw
                if shortfall > 0:
                    self.grid_import_kw = shortfall

        # Clamp discharge at min_soc
        if self.soc <= self.min_soc and self.bat_discharge_kw > 0:
            self.bat_discharge_kw = 0.0
            # Recalculate grid to cover load
            self.grid_import_kw = max(0, self.load_kw - self.solar_kw)
            self.grid_export_kw = max(0, self.solar_kw - self.load_kw)

        # Update SoC
        net_bat_kw = self.bat_charge_kw - self.bat_discharge_kw
        delta_kwh = net_bat_kw * dt_hours
        delta_pct = delta_kwh / self.battery_capacity_kwh * 100.0
        self.soc = max(0.0, min(100.0, self.soc + delta_pct))

        # Update cumulative counters
        self.feedin_total_kwh += self.grid_export_kw * dt_hours
        self.grid_consumption_total_kwh += self.grid_import_kw * dt_hours
        self.charge_total_kwh += self.bat_charge_kw * dt_hours
        self.discharge_total_kwh += self.bat_discharge_kw * dt_hours
        self.generation_total_kwh += self.solar_kw * dt_hours
        self.loads_total_kwh += self.load_kw * dt_hours

        # Advance simulated time
        self.sim_time += datetime.timedelta(seconds=dt_seconds)

    def get_schedule_response(self) -> dict[str, Any]:
        """Return schedule in API format (8 groups, padded with placeholders)."""
        groups = [g.to_dict() for g in self.schedule_groups]
        while len(groups) < 8:
            groups.append(_make_placeholder())
        return {
            "enable": 1 if self.schedule_enabled else 0,
            "groups": groups,
        }

    def set_schedule(self, groups: list[dict[str, Any]]) -> None:
        """Set schedule from API format."""
        self.schedule_groups = [
            ScheduleGroup.from_dict(g)
            for g in groups
            if g.get("workMode") not in ("Invalid", "", None)
            and not (
                g.get("startHour", 0) == g.get("endHour", 0)
                and g.get("startMinute", 0) == g.get("endMinute", 0)
            )
        ]
        self.schedule_enabled = bool(self.schedule_groups)
        self.tick(0)

    def _fuzz(self, value: float) -> float:
        """Apply jitter if fuzzing is enabled."""
        return _jitter(value) if self.fuzzing else value

    def get_real_time_response(self, variables: list[str]) -> list[dict[str, Any]]:
        """Return real-time data in API format (with optional fuzzing)."""
        f = self._fuzz
        var_map: dict[str, float] = {
            "SoC": float(int(self.soc)),  # integer like real API (no fuzz)
            "batChargePower": f(self.bat_charge_kw),
            "batDischargePower": f(self.bat_discharge_kw),
            "loadsPower": f(self.load_kw),
            "pvPower": f(self.solar_kw),
            "gridConsumptionPower": f(self.grid_import_kw),
            "feedinPower": f(self.grid_export_kw),
            "generationPower": f(self.solar_kw),
            "batTemperature": 25.0,
            "batVolt": 52.0,
            "batCurrent": (self.bat_charge_kw - self.bat_discharge_kw) * 1000 / 52,
            "pv1Power": self.solar_kw * 0.5,
            "pv2Power": self.solar_kw * 0.5,
            "ambientTemperation": 20.0,
            "invTemperation": 35.0,
            "feedin": self.feedin_total_kwh,
            "gridConsumption": self.grid_consumption_total_kwh,
            "generation": self.generation_total_kwh,
            "chargeEnergyToTal": self.charge_total_kwh,
            "dischargeEnergyToTal": self.discharge_total_kwh,
            "loads": self.loads_total_kwh,
            "energyThroughput": self.charge_total_kwh + self.discharge_total_kwh,
            "meterPower": self.grid_import_kw - self.grid_export_kw,
            "RVolt": 240.0,
            "RCurrent": (self.grid_import_kw - self.grid_export_kw) * 1000 / 240,
            "RFreq": 50.0,
            "epsPower": 0.0,
            "ResidualEnergy": self.soc / 100.0 * self.battery_capacity_kwh,
        }
        datas = []
        for v in variables:
            if v in var_map:
                datas.append({"variable": v, "value": var_map[v]})
        return [{"datas": datas, "deviceSN": self.device_sn}]

    def get_ws_message(self) -> dict[str, Any]:
        """Build a WebSocket push message from current state (with fuzzing)."""
        is_charging = self.bat_charge_kw > self.bat_discharge_kw
        bat_power = self._fuzz(
            self.bat_charge_kw if is_charging else self.bat_discharge_kw
        )
        solar = self._fuzz(self.solar_kw)
        load = self._fuzz(self.load_kw)
        grid = self._fuzz(self.grid_import_kw + self.grid_export_kw)

        if self.ws_unit == "kW":
            bat_val = f"{bat_power:.3f}"
            solar_val = f"{solar:.3f}"
            load_val = f"{load:.3f}"
            grid_val = f"{grid:.3f}"
        else:
            bat_val = str(int(bat_power * 1000))
            solar_val = str(int(solar * 1000))
            load_val = str(int(load * 1000))
            grid_val = str(int(grid * 1000))

        return {
            "errno": 0,
            "msg": "success",
            "result": {
                "timeDiff": self.ws_time_diff,
                "node": {
                    "bat": {
                        "soc": int(self.soc),
                        "charge": "1" if is_charging else "0",
                        "power": {"value": bat_val, "unit": self.ws_unit},
                    },
                    "solar": {
                        "power": {"value": solar_val, "unit": self.ws_unit},
                    },
                    "load": {
                        "power": {"value": load_val, "unit": self.ws_unit},
                    },
                    "grid": {
                        "power": {"value": grid_val, "unit": self.ws_unit},
                        "gridStatus": 3 if self.grid_import_kw > 0.01 else 2,
                    },
                },
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Full state dump for backchannel."""
        return {
            "device_sn": self.device_sn,
            "plant_id": self.plant_id,
            "soc": round(self.soc, 2),
            "soc_int": int(self.soc),
            "battery_capacity_kwh": self.battery_capacity_kwh,
            "max_power_w": self.max_power_w,
            "solar_kw": round(self.solar_kw, 3),
            "load_kw": round(self.load_kw, 3),
            "bat_charge_kw": round(self.bat_charge_kw, 3),
            "bat_discharge_kw": round(self.bat_discharge_kw, 3),
            "grid_import_kw": round(self.grid_import_kw, 3),
            "grid_export_kw": round(self.grid_export_kw, 3),
            "work_mode": self.get_active_mode(),
            "schedule_enabled": self.schedule_enabled,
            "schedule_groups": [g.to_dict() for g in self.schedule_groups],
            "min_soc": self.min_soc,
            "min_soc_on_grid": self.min_soc_on_grid,
            "sim_time": self.sim_time.isoformat(),
            "active_fault": self.active_fault,
            "ws_unit": self.ws_unit,
            "ws_time_diff": self.ws_time_diff,
            "feedin_total_kwh": round(self.feedin_total_kwh, 3),
            "grid_consumption_total_kwh": round(self.grid_consumption_total_kwh, 3),
        }

    def reset(self) -> None:
        """Reset to defaults."""
        self.__init__()  # type: ignore[misc]
