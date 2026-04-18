"""Shared types for smart battery control integrations."""

from __future__ import annotations

import datetime  # noqa: TC003 — used at runtime by factory functions
import uuid
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
    groups: list[ScheduleGroup] | None
    last_power_w: int
    soc_unavailable_count: int
    soc_above_target_count: int
    consecutive_error_count: int
    taper_tick: int


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
    soc_unavailable_count: int
    consecutive_error_count: int
    taper_tick: int
    target_power_w: int
    schedule_horizon: str | None


def create_charge_session(
    *,
    start: datetime.datetime,
    end: datetime.datetime,
    target_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    initial_power: int,
    min_soc_on_grid: int,
    min_power_change: int,
    api_min_soc: int,
    force: bool,
    current_soc: float | None,
    should_defer: bool,
    now: datetime.datetime,
    groups: list[Any] | None = None,
) -> ChargeSessionState:
    """Build a fully-initialized charge session state dict."""
    return ChargeSessionState(
        session_id=str(uuid.uuid4()),
        groups=groups,
        start=start,
        end=end,
        target_soc=target_soc,
        battery_capacity_kwh=battery_capacity_kwh,
        max_power_w=max_power_w,
        last_power_w=initial_power,
        min_soc_on_grid=min_soc_on_grid,
        min_power_change=min_power_change,
        api_min_soc=api_min_soc,
        charging_started=not should_defer,
        charging_started_at=None if should_defer else now,
        charging_started_energy_kwh=(
            None
            if should_defer
            else (
                current_soc / 100.0 * battery_capacity_kwh
                if current_soc is not None
                else None
            )
        ),
        force=force,
        soc_unavailable_count=0,
        start_soc=current_soc,
    )


def create_discharge_session(
    *,
    start: datetime.datetime,
    end: datetime.datetime,
    min_soc: int,
    max_power_w: int,
    initial_power: int,
    battery_capacity_kwh: float,
    min_power_change: int,
    pacing_enabled: bool,
    current_soc: float | None,
    net_consumption: float,
    should_defer: bool,
    now: datetime.datetime,
    feedin_energy_limit: float | None = None,
    schedule_horizon: str | None = None,
    groups: list[Any] | None = None,
) -> DischargeSessionState:
    """Build a fully-initialized discharge session state dict."""
    return DischargeSessionState(
        session_id=str(uuid.uuid4()),
        groups=groups if groups is not None else [],
        start=start,
        end=end,
        min_soc=min_soc,
        max_power_w=max_power_w,
        last_power_w=initial_power,
        soc_below_min_count=0,
        soc_unavailable_count=0,
        feedin_energy_limit_kwh=feedin_energy_limit,
        feedin_start_kwh=None,
        battery_capacity_kwh=battery_capacity_kwh,
        min_power_change=min_power_change,
        pacing_enabled=pacing_enabled,
        start_soc=current_soc,
        discharging_started=not should_defer,
        discharging_started_at=None if should_defer else now,
        consumption_peak_kw=max(0.0, net_consumption),
        schedule_horizon=schedule_horizon,
    )
