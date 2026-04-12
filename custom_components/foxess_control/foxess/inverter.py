"""High-level inverter control: work modes and battery state."""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from .client import FoxESSClient


class WorkMode(StrEnum):
    SELF_USE = "SelfUse"
    FORCE_CHARGE = "ForceCharge"
    FORCE_DISCHARGE = "ForceDischarge"
    BACKUP = "Backup"
    FEEDIN = "Feedin"


class ScheduleGroup(TypedDict):
    enable: int
    startHour: int
    startMinute: int
    endHour: int
    endMinute: int
    workMode: str
    minSocOnGrid: int
    fdSoc: int
    fdPwr: int


class MinSocSettings(TypedDict):
    minSoc: int
    minSocOnGrid: int


def _parse_real_time(result: Any) -> dict[str, Any]:
    """Extract variable->value map from the real-time query response.

    The API returns [{datas: [{variable, value, ...}, ...], deviceSN, time}].
    """
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict) and "datas" in first:
            return {d["variable"]: d["value"] for d in first["datas"]}
        if isinstance(first, dict) and "variable" in first:
            return {d["variable"]: d["value"] for d in result}
    return {}


class Inverter:
    """Manage a single FoxESS inverter."""

    # Multiplier applied to the inverter's rated capacity (kW) to derive
    # the fdPwr value in watts.  The FoxESS app uses capacity * 1050.
    CAPACITY_TO_FD_PWR = 1050

    def __init__(self, client: FoxESSClient, serial_number: str) -> None:
        self.client = client
        self.sn = serial_number
        self._max_power_w: int | None = None

    @property
    def max_power_w(self) -> int:
        """Inverter rated power in watts, queried from device detail."""
        if self._max_power_w is None:
            detail = self.get_detail()
            capacity_kw: int = detail.get("capacity", 0)
            if capacity_kw <= 0:
                raise RuntimeError(
                    "Could not determine inverter capacity from device detail"
                )
            self._max_power_w = capacity_kw * self.CAPACITY_TO_FD_PWR
        return self._max_power_w

    @classmethod
    def auto_detect(cls, client: FoxESSClient) -> Inverter:
        """Create an Inverter for the first device found on the account."""
        result: Any = client.post(
            "/op/v0/device/list", {"currentPage": 1, "pageSize": 10}
        )
        devices: list[dict[str, Any]] = result.get("data", [])
        if not devices:
            raise RuntimeError("No devices found on this account")
        sn: str = devices[0]["deviceSN"]
        return cls(client, sn)

    # --- State of Charge ---

    def get_soc(self) -> float:
        """Get the current battery state of charge (%)."""
        result: Any = self.client.post(
            "/op/v0/device/real/query",
            {"sn": self.sn, "variables": ["SoC"]},
        )
        data = _parse_real_time(result)
        if "SoC" in data:
            return float(data["SoC"])
        raise RuntimeError("SoC not found in API response")

    def get_battery_status(self) -> dict[str, Any]:
        """Get battery status: SoC, power, temperature, residual energy."""
        variables = [
            "SoC",
            "batChargePower",
            "batDischargePower",
            "batTemperature",
            "batVolt",
            "batCurrent",
        ]
        result: Any = self.client.post(
            "/op/v0/device/real/query",
            {"sn": self.sn, "variables": variables},
        )
        return _parse_real_time(result)

    # --- Min SoC Settings ---

    def get_min_soc(self) -> MinSocSettings:
        """Get min SoC settings."""
        result: MinSocSettings = self.client.get(
            "/op/v0/device/battery/soc/get", {"sn": self.sn}
        )
        return result

    def set_min_soc(self, min_soc: int = 10, min_soc_on_grid: int = 10) -> None:
        """Set min SoC thresholds."""
        self.client.post(
            "/op/v0/device/battery/soc/set",
            {"sn": self.sn, "minSoc": min_soc, "minSocOnGrid": min_soc_on_grid},
        )

    # --- Scheduler / Work Mode ---

    def get_schedule(self) -> dict[str, Any]:
        """Get the current scheduler configuration.

        Returns a dict with 'enable' (int) and 'groups' (list of ScheduleGroup).
        When no scheduler is configured (e.g. mode set via app), the API
        returns ``null``; this method normalises that to an empty schedule.
        """
        result: Any = self.client.post(
            "/op/v0/device/scheduler/get", {"deviceSN": self.sn}
        )
        if result is None:
            return {"enable": 0, "groups": []}
        sched: dict[str, Any] = result
        return sched

    def set_work_mode(
        self,
        mode: WorkMode,
        min_soc_on_grid: int = 11,
        fd_soc: int = 11,
        fd_pwr: int | None = None,
        api_min_soc: int = 11,
    ) -> None:
        """Set the inverter to a single work mode for the entire day.

        Args:
            mode: The work mode to set.
            min_soc_on_grid: Minimum SoC to maintain while on-grid (%).
            fd_soc: Target SoC for force discharge, stop at this level (%).
            fd_pwr: Power limit (watts). None uses inverter rated power.
            api_min_soc: Minimum fdSoc accepted by the API (default 11).

        The FoxESS API requires ``fdSoc >= api_min_soc`` and
        ``minSocOnGrid <= fdSoc``.
        """
        if fd_pwr is None:
            fd_pwr = self.max_power_w

        # ForceCharge typically wants a high target SoC
        if mode == WorkMode.FORCE_CHARGE and fd_soc <= api_min_soc:
            fd_soc = 100

        fd_soc = max(fd_soc, api_min_soc)
        min_soc_on_grid = min(min_soc_on_grid, fd_soc)

        group: ScheduleGroup = {
            "enable": 1,
            "startHour": 0,
            "startMinute": 0,
            "endHour": 23,
            "endMinute": 59,
            "workMode": mode.value,
            "minSocOnGrid": min_soc_on_grid,
            "fdSoc": fd_soc,
            "fdPwr": fd_pwr,
        }
        self.client.post(
            "/op/v0/device/scheduler/enable",
            {"deviceSN": self.sn, "groups": [group]},
        )

    def set_schedule(self, groups: list[ScheduleGroup]) -> None:
        """Set arbitrary scheduler time segments for fine-grained control.

        Each group dict should have: enable, startHour, startMinute,
        endHour, endMinute, workMode, minSocOnGrid, fdSoc, fdPwr.
        """
        self.client.post(
            "/op/v0/device/scheduler/enable",
            {"deviceSN": self.sn, "groups": groups},
        )

    # --- Convenience methods ---

    def self_use(self, min_soc_on_grid: int = 11, api_min_soc: int = 11) -> None:
        """Switch to self-use mode (default operating mode)."""
        self.set_work_mode(
            WorkMode.SELF_USE,
            min_soc_on_grid=min_soc_on_grid,
            api_min_soc=api_min_soc,
        )

    def force_charge(self, min_soc_on_grid: int = 11, target_soc: int = 100) -> None:
        """Force charge the battery from grid + PV.

        Args:
            min_soc_on_grid: Minimum SoC while on-grid (%).
            target_soc: Charge up to this SoC level (%).
        """
        self.set_work_mode(
            WorkMode.FORCE_CHARGE,
            min_soc_on_grid=min_soc_on_grid,
            fd_soc=target_soc,
        )

    def force_discharge(
        self,
        min_soc: int = 11,
        power: int | None = None,
        min_soc_on_grid: int = 11,
        api_min_soc: int = 11,
    ) -> None:
        """Force discharge the battery.

        Args:
            min_soc: Stop discharging at this SoC level (%).
            power: Discharge power limit in watts. None uses inverter rated power.
            min_soc_on_grid: Minimum SoC while on-grid (%).
        """
        self.set_work_mode(
            WorkMode.FORCE_DISCHARGE,
            min_soc_on_grid=min_soc_on_grid,
            fd_soc=min_soc,
            fd_pwr=power,
            api_min_soc=api_min_soc,
        )

    # --- Query current mode ---

    def get_current_mode(self, now: datetime.datetime | None = None) -> WorkMode | None:
        """Get the work mode that is active right now.

        Checks enabled schedule groups against the current time and returns
        the mode of the group whose window contains *now*.  Falls back to
        the first enabled group if no group matches the current time (e.g.
        a full-day 00:00-23:59 window).  Returns ``None`` when no groups
        are enabled.
        """
        schedule = self.get_schedule()
        groups: list[dict[str, Any]] = schedule.get("groups", [])
        enabled = [g for g in groups if g.get("enable") == 1]
        if not enabled:
            return None

        if now is None:
            now = datetime.datetime.now()
        cur_minutes = now.hour * 60 + now.minute

        for group in enabled:
            start = group.get("startHour", 0) * 60 + group.get("startMinute", 0)
            end = group.get("endHour", 0) * 60 + group.get("endMinute", 0)
            if start <= cur_minutes < end:
                try:
                    return WorkMode(group.get("workMode", ""))
                except ValueError:
                    return None
            # Handle midnight-wrapping windows (e.g. 22:00-06:00)
            if start > end and (cur_minutes >= start or cur_minutes < end):
                try:
                    return WorkMode(group.get("workMode", ""))
                except ValueError:
                    return None

        # No enabled group covers the current time — inverter is in SelfUse
        return None

    def get_status_summary(self) -> dict[str, Any]:
        """Get a combined summary of current mode, SoC, and battery state."""
        battery = self.get_battery_status()
        current_mode = self.get_current_mode()
        min_soc = self.get_min_soc()
        return {
            "mode": current_mode.value if current_mode else "Unknown",
            "soc": battery.get("SoC"),
            "charge_power_kw": battery.get("batChargePower"),
            "discharge_power_kw": battery.get("batDischargePower"),
            "temperature_c": battery.get("batTemperature"),
            "min_soc": min_soc.get("minSoc"),
            "min_soc_on_grid": min_soc.get("minSocOnGrid"),
        }

    # --- Device Info ---

    def get_detail(self) -> dict[str, Any]:
        """Get device detail including battery model and capacity."""
        result: dict[str, Any] = self.client.get(
            "/op/v0/device/detail", {"sn": self.sn}
        )
        return result

    def get_real_time(self, variables: list[str]) -> dict[str, Any]:
        """Query arbitrary real-time variables."""
        result: Any = self.client.post(
            "/op/v0/device/real/query",
            {"sn": self.sn, "variables": variables},
        )
        return _parse_real_time(result)
