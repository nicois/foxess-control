"""Shared types for smart battery control integrations."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class WorkMode(StrEnum):
    """Battery work modes common across inverter brands."""

    SELF_USE = "SelfUse"
    FORCE_CHARGE = "ForceCharge"
    FORCE_DISCHARGE = "ForceDischarge"
    BACKUP = "Backup"
    FEEDIN = "Feedin"


class ScheduleGroup(TypedDict):
    """A single time-based schedule slot (cloud-API brands only).

    Entity-mode brands do not use schedule groups — they set modes
    directly via HA entities and rely on HA timers for time windows.
    """

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


class ChargeSessionState(TypedDict, total=False):
    """In-memory state for an active smart charge session."""

    session_id: str
    start: Any  # datetime.datetime
    end: Any  # datetime.datetime
    target_soc: int
    start_soc: float | None
    max_power_w: int
    battery_capacity_kwh: float
    min_soc_on_grid: int
    min_power_change: int
    api_min_soc: int
    force: bool
    charging_started: bool
    charging_started_at: Any  # datetime.datetime | None
    charging_started_energy_kwh: float | None
    target_reached: bool
    groups: list[ScheduleGroup]
    last_power_w: int
    soc_unavailable_count: int


class DischargeSessionState(TypedDict, total=False):
    """In-memory state for an active smart discharge session."""

    session_id: str
    start: Any  # datetime.datetime
    end: Any  # datetime.datetime
    min_soc: int
    start_soc: float | None
    max_power_w: int
    last_power_w: int
    battery_capacity_kwh: float
    min_power_change: int
    pacing_enabled: bool
    feedin_energy_limit_kwh: float | None
    feedin_start_kwh: float | None
    feedin_prev_kwh: float | None
    feedin_stop_scheduled: bool
    suspended: bool
    discharging_started: bool
    discharging_started_at: Any  # datetime.datetime | None
    consumption_peak_kw: float
    groups: list[ScheduleGroup]
    soc_below_min_count: int
