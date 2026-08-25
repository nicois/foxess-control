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
    battery_id: str = "sim-battery-id-001"
    battery_sn: str = "SIM0001BAT001"

    # Model name reported as ``deviceType`` by /op/v0/device/detail
    # (e.g. "KH10", "H3-12.0-M").  ``None`` models a device-detail
    # response with no ``deviceType`` field at all.
    device_type: str | None = "KH10"

    # Battery
    soc: float = 50.0
    battery_temperature: float = 25.0
    battery_capacity_kwh: float = 10.0
    max_power_w: int = 10500

    # Hardware Max Grid Export Limit (watts).  Modelled as the
    # foxess_modbus "Max Grid Export Limit" number entity — the
    # inverter refuses to export more than this and curtails battery
    # discharge accordingly.  Defaults high so existing tests are
    # unaffected.
    max_grid_export_limit_w: int = 10500

    # External power (set via backchannel)
    solar_kw: float = 0.0
    load_kw: float = 0.5

    # Second meter (e.g. an AC-coupled solar inverter reported on a
    # separate FoxESS meter channel such as ``meterPower2``).  Served
    # verbatim from ``get_real_time_response`` so a coordinator
    # configured with an "alternate solar source" variable can sum it
    # into pvPower.  Defaults to 0.0 so existing tests are unaffected.
    meter_power2_kw: float = 0.0

    # Fuzzing: add noise to readings to prevent test overfitting
    fuzzing: bool = True

    # Derived power flows (computed by tick)
    bat_charge_kw: float = 0.0
    bat_discharge_kw: float = 0.0
    grid_import_kw: float = 0.0
    grid_export_kw: float = 0.0

    # Schedule
    schedule_groups: list[ScheduleGroup] = field(default_factory=list)
    # Test seam (issue #11): when True, /scheduler/enable returns success
    # (errno 0) but does NOT apply the groups — models a firmware that
    # ACKs the write at the API but silently fails to apply it.
    silent_drop_schedule: bool = False
    schedule_enabled: bool = False

    # Min SoC thresholds
    min_soc: int = 10
    min_soc_on_grid: int = 10

    # Cumulative energy counters (kWh)
    feedin_total_kwh: float = 0.0
    grid_consumption_total_kwh: float = 0.0
    charge_total_kwh: float = 0.0
    discharge_total_kwh: float = 0.0
    # ``generation`` — the inverter's cumulative AC *output* energy.  NOT
    # photovoltaic yield: it includes energy the battery discharged, so it
    # rises overnight with zero sun.  Verified against a live KH10
    # (2026-08-25/26): Δgeneration 16.5 kWh == Δloads 7.2 + Δfeedin 9.2 −
    # Δimport 0.0 while pvPower stayed at 0.  See
    # docs/api/foxess-cloud-api.md.
    generation_total_kwh: float = 0.0
    # ``PVEnergyTotal`` — the genuine PV-only lifetime yield counter.  Flat
    # whenever the panels produce nothing, however hard the battery is
    # discharging.  This is the correct HA Energy-dashboard solar source.
    pv_energy_total_kwh: float = 0.0
    loads_total_kwh: float = 0.0

    # Variables this device does not support.  The real
    # ``/op/v0/device/real/query`` silently OMITS an unsupported (or
    # misspelled) variable from ``datas`` and still answers ``errno: 0`` —
    # probed read-only against a live KH10 on 2026-08-26.  Modelling that
    # lets tests prove a shared POLLED_VARIABLES entry degrades gracefully
    # on models that lack it (C-033).
    unsupported_variables: list[str] = field(default_factory=list)

    # Simulated time (starts at real time, advanced by tick/fast_forward)
    sim_time: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(tz=datetime.UTC)
    )

    # Fault injection
    active_fault: str | None = None
    fault_remaining: int = 0  # auto-clear after N requests (0 = permanent)

    # Taper simulation: linear charge reduction above this SoC threshold
    charge_taper_soc: float = 90.0
    # Taper simulation: linear discharge reduction below this SoC threshold
    discharge_taper_soc: float = 15.0
    # Taper simulation: temperature threshold below which charge rate is reduced
    cold_taper_temp: float = 15.0

    # Round-trip battery efficiency (1.0 = lossless, 0.95 = 5% loss)
    efficiency: float = 1.0

    # Signature validation (off by default for backward compatibility)
    validate_signatures: bool = False
    api_key: str = "test-api-key"

    # WS config overrides
    ws_unit: str = "W"  # "W" or "kW"
    ws_time_diff: int = 5  # normal

    # Autonomous rate limiting (per-endpoint, seconds between requests)
    # Default 0 = disabled, so existing tests are unaffected.
    rate_limit_seconds: float = 0.0

    # --- Device-declared scheduler limits ---------------------------------
    # ``/op/v3/device/scheduler/get`` returns, per device, a ``properties``
    # map describing the accepted range of every schedule-group field plus
    # the supported work-mode enumeration.  ``/scheduler/enable`` rejects
    # any group whose values fall outside those ranges with errno 40257
    # ("Parameters do not meet expectations").
    #
    # ``fd_pwr_max_w`` is the declared ``fdPwr`` ceiling.  ``None`` means
    # "same as ``max_power_w``", which models a KH10: its declared ceiling
    # (10500 W) happens to equal the ``capacity x 1050`` value the FoxESS
    # app writes, which is why that heuristic works on a KH but overshoots
    # on other model families (H3 / EVO — issues #12, #14, #17).
    fd_pwr_max_w: int | None = None
    fd_soc_min: int = 10
    fd_soc_max: int = 100
    # Work modes the device declares in ``properties.workmode.enumList``.
    # Empty list models a device that declares no enumeration.
    scheduler_work_modes: list[str] = field(
        default_factory=lambda: [
            "SelfUse",
            "Feedin",
            "Backup",
            "ForceCharge",
            "ForceDischarge",
        ]
    )
    # When False, /op/v3/device/scheduler/get is not served (HTTP 404) —
    # models a firmware/region where the properties endpoint is absent.
    scheduler_properties_supported: bool = True

    def fd_pwr_limit_w(self) -> int:
        """Maximum ``fdPwr`` this device accepts in a schedule group."""
        return self.max_power_w if self.fd_pwr_max_w is None else self.fd_pwr_max_w

    def get_device_detail_response(self) -> dict[str, Any]:
        """Build the ``/op/v0/device/detail`` result.

        ``capacity`` is the nameplate rating in kW, as the real API
        returns it.  ``deviceType`` is omitted entirely when
        ``device_type`` is None.
        """
        detail: dict[str, Any] = {
            "capacity": self.max_power_w / 1050,
            "hasBattery": True,
            "hasPV": True,
            "function": {"scheduler": True},
        }
        if self.device_type is not None:
            detail["deviceType"] = self.device_type
        return detail

    def get_scheduler_properties_response(self) -> dict[str, Any]:
        """Build the ``/op/v3/device/scheduler/get`` result."""
        properties: dict[str, Any] = {
            "fdpwr": {
                "unit": "W",
                "precision": 1.0,
                "range": {"min": 0.0, "max": float(self.fd_pwr_limit_w())},
            },
            "fdsoc": {
                "unit": "%",
                "precision": 1.0,
                "range": {
                    "min": float(self.fd_soc_min),
                    "max": float(self.fd_soc_max),
                },
            },
            "minsocongrid": {
                "unit": "%",
                "precision": 1.0,
                "range": {"min": 10.0, "max": 100.0},
            },
            "starthour": {
                "unit": "",
                "precision": 1.0,
                "range": {"min": 0.0, "max": 23.0},
            },
            "endhour": {
                "unit": "",
                "precision": 1.0,
                "range": {"min": 0.0, "max": 23.0},
            },
        }
        if self.scheduler_work_modes:
            properties["workmode"] = {
                "enumList": list(self.scheduler_work_modes),
                "unit": "",
                "precision": 1.0,
            }
        return {
            "enable": 1 if self.schedule_enabled else 0,
            "maxGroupCount": 8,
            "groups": [],
            "properties": properties,
        }

    def check_schedule_group(self, group: dict[str, Any]) -> str | None:
        """Return a rejection reason for *group*, or None when acceptable.

        Mirrors the live API's parameter validation: values outside the
        declared ranges — and work modes outside the declared enumeration —
        are rejected with errno 40257.
        """
        mode = group.get("workMode", "")
        if mode in ("Invalid", ""):
            return None
        if self.scheduler_work_modes and mode not in self.scheduler_work_modes:
            return f"workMode {mode} not supported by this device"
        fd_pwr = group.get("fdPwr")
        if isinstance(fd_pwr, int | float) and fd_pwr > self.fd_pwr_limit_w():
            return f"fdPwr {fd_pwr} exceeds device maximum {self.fd_pwr_limit_w()}"
        fd_soc = group.get("fdSoc")
        if isinstance(fd_soc, int | float) and not (
            self.fd_soc_min <= fd_soc <= self.fd_soc_max
        ):
            return (
                f"fdSoc {fd_soc} outside device range "
                f"{self.fd_soc_min}-{self.fd_soc_max}"
            )
        return None

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

    def _charge_taper_factor(self) -> float:
        """BMS reduces charge acceptance above charge_taper_soc."""
        if self.soc <= self.charge_taper_soc:
            return 1.0
        return max(0.0, (100.0 - self.soc) / (100.0 - self.charge_taper_soc))

    def _discharge_taper_factor(self) -> float:
        """BMS reduces discharge rate near min_soc (linear from discharge_taper_soc)."""
        if self.soc >= self.discharge_taper_soc:
            return 1.0
        if self.soc <= self.min_soc:
            return 0.0
        return (self.soc - self.min_soc) / (self.discharge_taper_soc - self.min_soc)

    def _temp_charge_taper_factor(self) -> float:
        """BMS reduces charge acceptance at low temperatures."""
        if self.battery_temperature >= self.cold_taper_temp:
            return 1.0
        if self.battery_temperature <= 0.0:
            return 0.5
        return 0.5 + 0.5 * (self.battery_temperature / self.cold_taper_temp)

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

        charge_taper = self._charge_taper_factor() * self._temp_charge_taper_factor()
        discharge_taper = self._discharge_taper_factor()

        if mode == "ForceCharge":
            target_charge_kw = (
                (group.fdPwr / 1000.0) if group else (self.max_power_w / 1000.0)
            )
            solar_to_load = min(self.solar_kw, self.load_kw)
            solar_to_bat = self.solar_kw - solar_to_load
            grid_to_load = self.load_kw - solar_to_load
            max_accept = min(target_charge_kw, self.max_power_w / 1000.0) * charge_taper
            self.bat_charge_kw = min(solar_to_bat + max_accept, max_accept)
            grid_charge = max(0.0, self.bat_charge_kw - solar_to_bat)
            self.grid_import_kw = grid_to_load + grid_charge

        elif mode == "ForceDischarge":
            target_discharge_kw = (
                (group.fdPwr / 1000.0) if group else (self.max_power_w / 1000.0)
            )
            effective = min(target_discharge_kw, self.max_power_w / 1000.0)
            self.bat_discharge_kw = effective * discharge_taper
            net_export = self.bat_discharge_kw + self.solar_kw - self.load_kw
            if net_export > 0:
                self.grid_export_kw = net_export
            else:
                self.grid_import_kw = -net_export

        elif mode == "Feedin":
            target_kw = (group.fdPwr / 1000.0) if group else (self.max_power_w / 1000.0)
            effective = min(target_kw, self.max_power_w / 1000.0)
            self.bat_discharge_kw = effective * discharge_taper
            net = self.bat_discharge_kw + self.solar_kw - self.load_kw
            if net > 0:
                self.grid_export_kw = net
            else:
                self.grid_import_kw = -net

        else:  # SelfUse
            solar_to_load = min(self.solar_kw, self.load_kw)
            remaining_load = self.load_kw - solar_to_load
            excess_solar = self.solar_kw - solar_to_load

            if excess_solar > 0:
                max_accept = min(excess_solar, self.max_power_w / 1000.0) * charge_taper
                if self.soc >= 100.0:
                    max_accept = 0.0
                self.bat_charge_kw = max_accept
                leftover = excess_solar - self.bat_charge_kw
                if leftover > 0:
                    self.grid_export_kw = leftover
            if remaining_load > 0:
                available = min(remaining_load, self.max_power_w / 1000.0)
                self.bat_discharge_kw = available * discharge_taper
                shortfall = remaining_load - self.bat_discharge_kw
                if shortfall > 0:
                    self.grid_import_kw = shortfall

        # Clamp charge at fdSoc (inverter stops charging when target reached)
        if group and mode == "ForceCharge" and self.soc >= group.fdSoc:
            self.bat_charge_kw = 0.0
            self.grid_import_kw = max(0, self.load_kw - self.solar_kw)
            self.grid_export_kw = max(0, self.solar_kw - self.load_kw)

        # Clamp discharge at fdSoc (inverter stops discharging at target floor)
        if group and mode in ("ForceDischarge", "Feedin") and self.soc <= group.fdSoc:
            self.bat_discharge_kw = 0.0
            self.grid_import_kw = max(0, self.load_kw - self.solar_kw)
            self.grid_export_kw = max(0, self.solar_kw - self.load_kw)

        # Clamp discharge at min_soc
        if self.soc <= self.min_soc and self.bat_discharge_kw > 0:
            self.bat_discharge_kw = 0.0
            self.grid_import_kw = max(0, self.load_kw - self.solar_kw)
            self.grid_export_kw = max(0, self.solar_kw - self.load_kw)

        # Clamp charge at 100%
        if self.soc >= 100.0 and self.bat_charge_kw > 0:
            self.bat_charge_kw = 0.0

        # Hardware Max Grid Export Limit: inverter curtails export at
        # the configured cap.  Battery discharge is reduced to match
        # (no dumping to load — the inverter just exports less).
        cap_kw = self.max_grid_export_limit_w / 1000.0
        if self.grid_export_kw > cap_kw:
            overshoot = self.grid_export_kw - cap_kw
            self.grid_export_kw = cap_kw
            if self.bat_discharge_kw > 0:
                self.bat_discharge_kw = max(0.0, self.bat_discharge_kw - overshoot)

        # Update SoC (apply efficiency: charging stores less, discharging draws more)
        net_bat_kw = self.bat_charge_kw - self.bat_discharge_kw
        if net_bat_kw > 0:
            delta_kwh = net_bat_kw * dt_hours * self.efficiency
        else:
            delta_kwh = (
                net_bat_kw * dt_hours / self.efficiency if self.efficiency > 0 else 0.0
            )
        delta_pct = delta_kwh / self.battery_capacity_kwh * 100.0
        self.soc = max(0.0, min(100.0, self.soc + delta_pct))

        # Update cumulative counters
        self.feedin_total_kwh += self.grid_export_kw * dt_hours
        self.grid_consumption_total_kwh += self.grid_import_kw * dt_hours
        self.charge_total_kwh += self.bat_charge_kw * dt_hours
        self.discharge_total_kwh += self.bat_discharge_kw * dt_hours
        # PV-only yield (PVEnergyTotal): panels only.
        self.pv_energy_total_kwh += self.solar_kw * dt_hours
        # Inverter AC output (generation): what left the inverter towards
        # the house and the grid, whatever its source (PV *or* battery).
        self.generation_total_kwh += self.inverter_output_kw * dt_hours
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

    @property
    def inverter_output_kw(self) -> float:
        """Instantaneous AC output of the inverter (``generationPower``).

        Energy leaving the inverter towards the house and the grid,
        regardless of whether it came from the panels or the battery:
        ``load + export - import``, floored at zero (while charging from
        the grid the inverter is a net consumer, and the real counter does
        not run backwards).  Matches the live KH10 observation that
        ``generationPower`` tracked battery discharge power overnight with
        ``pvPower`` pinned at 0.
        """
        return max(0.0, self.load_kw + self.grid_export_kw - self.grid_import_kw)

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
            "generationPower": f(self.inverter_output_kw),
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
            "PVEnergyTotal": self.pv_energy_total_kwh,
            "chargeEnergyToTal": self.charge_total_kwh,
            "dischargeEnergyToTal": self.discharge_total_kwh,
            "loads": self.loads_total_kwh,
            "energyThroughput": self.charge_total_kwh + self.discharge_total_kwh,
            "meterPower": self.grid_import_kw - self.grid_export_kw,
            "meterPower2": f(self.meter_power2_kw),
            "RVolt": 240.0,
            "RCurrent": (self.grid_import_kw - self.grid_export_kw) * 1000 / 240,
            "RFreq": 50.0,
            "epsPower": 0.0,
            "ResidualEnergy": self.soc / 100.0 * self.battery_capacity_kwh,
        }
        unsupported = set(self.unsupported_variables)
        datas = []
        for v in variables:
            # Unknown / unsupported names are silently omitted (errno stays
            # 0) — see the ``unsupported_variables`` field docstring.
            if v in var_map and v not in unsupported:
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
                        "batteryId": self.battery_id,
                        "multipleBatterySoc": [
                            {"batSn": self.battery_sn, "soc": int(self.soc)},
                        ],
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
            "device_type": self.device_type,
            "plant_id": self.plant_id,
            "fd_pwr_max_w": self.fd_pwr_limit_w(),
            "soc": round(self.soc, 2),
            "soc_int": int(self.soc),
            "battery_capacity_kwh": self.battery_capacity_kwh,
            "max_power_w": self.max_power_w,
            "solar_kw": round(self.solar_kw, 3),
            "load_kw": round(self.load_kw, 3),
            "meter_power2_kw": round(self.meter_power2_kw, 3),
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
            "validate_signatures": self.validate_signatures,
            "api_key": self.api_key,
            "ws_unit": self.ws_unit,
            "ws_time_diff": self.ws_time_diff,
            "feedin_total_kwh": round(self.feedin_total_kwh, 3),
            "grid_consumption_total_kwh": round(self.grid_consumption_total_kwh, 3),
            "generation_total_kwh": round(self.generation_total_kwh, 3),
            "pv_energy_total_kwh": round(self.pv_energy_total_kwh, 3),
            "unsupported_variables": list(self.unsupported_variables),
            "rate_limit_seconds": self.rate_limit_seconds,
        }

    def reset(self) -> None:
        """Reset to defaults."""
        self.__init__()  # type: ignore[misc]
