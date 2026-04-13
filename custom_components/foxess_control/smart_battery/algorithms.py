"""Pure pacing algorithms for smart battery charge and discharge.

All functions in this module are pure — they take numeric inputs and
return numeric outputs with **zero** Home Assistant dependency.  This
makes them trivially testable and reusable across any inverter brand.
"""

from __future__ import annotations

import datetime
import logging

_LOGGER = logging.getLogger(__name__)


def soc_energy_kwh(soc: float, capacity_kwh: float) -> float:
    """Convert a SoC percentage to energy in kWh."""
    return soc / 100.0 * capacity_kwh


def calculate_charge_power(
    current_soc: float,
    target_soc: int,
    battery_capacity_kwh: float,
    remaining_hours: float,
    max_power_w: int,
    net_consumption_kw: float = 0.0,
    headroom: float = 0.10,
    charging_started_energy_kwh: float | None = None,
    elapsed_since_charge_started: float = 0.0,
    effective_charge_window: float = 0.0,
    min_power_change_w: int = 0,
) -> int:
    """Calculate the charge power needed to reach target SoC in remaining time.

    *headroom* is a fraction (e.g. 0.10 for 10%) controlling how much
    spare capacity to reserve.  It is applied both as a time buffer (plan
    to finish in ``1 - headroom`` of the remaining time) and as a power
    multiplier (``1 + headroom``).

    When *charging_started_energy_kwh* and *elapsed_since_charge_started*
    are provided, the function checks whether the current energy is behind
    the ideal headroom-adjusted trajectory.  If the actual energy is below
    the ideal by more than a tolerance, *max_power_w* is returned to catch
    up.

    When *net_consumption_kw* is positive the house is drawing power that
    competes with the battery for inverter capacity, so we add it to the
    required charge rate.

    Returns an integer power in watts, clamped to [100, max_power_w].
    """
    target_energy_kwh = soc_energy_kwh(target_soc, battery_capacity_kwh)
    energy_needed_kwh = target_energy_kwh - soc_energy_kwh(
        current_soc, battery_capacity_kwh
    )
    if energy_needed_kwh <= 0:
        return 100
    if remaining_hours <= 0:
        return max_power_w

    # Check if we're behind the ideal headroom-adjusted trajectory.
    if (
        charging_started_energy_kwh is not None
        and elapsed_since_charge_started > 0
        and effective_charge_window > 0
        and headroom > 0
    ):
        effective_window = effective_charge_window * (1 - headroom)
        if effective_window > 0:
            energy_to_add = target_energy_kwh - charging_started_energy_kwh
            if energy_to_add > 0:
                progress = min(elapsed_since_charge_started / effective_window, 1.0)
                ideal_energy_now = (
                    charging_started_energy_kwh + progress * energy_to_add
                )
                actual_energy = soc_energy_kwh(current_soc, battery_capacity_kwh)
                tolerance_kwh = min_power_change_w / 1000.0 * remaining_hours
                deficit = ideal_energy_now - actual_energy
                if deficit > tolerance_kwh:
                    _LOGGER.debug(
                        "Smart charge: behind schedule "
                        "(%.2f kWh < ideal %.2f kWh, "
                        "deficit %.3f > tolerance %.3f), "
                        "charging at max power",
                        actual_energy,
                        ideal_energy_now,
                        deficit,
                        tolerance_kwh,
                    )
                    return max_power_w

    # Plan to finish in (1 - headroom) of the remaining time so there is
    # a buffer if consumption spikes or the inverter can't sustain full power.
    effective_hours = remaining_hours * (1 - headroom)
    if effective_hours <= 0:
        effective_hours = remaining_hours
    battery_power_kw = energy_needed_kwh / effective_hours
    total_power_kw = battery_power_kw + max(0.0, net_consumption_kw)
    # Over-provision the charge rate so unexpected load doesn't prevent
    # reaching the target.
    total_power_kw *= 1 + headroom
    power_w = total_power_kw * 1000
    return max(100, min(int(power_w), max_power_w))


def should_suspend_discharge(
    current_soc: float,
    min_soc: int,
    battery_capacity_kwh: float,
    remaining_hours: float,
    net_consumption_kw: float,
    headroom: float = 0.10,
) -> bool:
    """Return True if forced discharge should be suspended.

    Suspension protects the min SoC target.  If household consumption
    alone would drain the battery to (or past) min SoC within the
    remaining window, adding *any* forced discharge power risks
    breaching the floor.
    """
    if remaining_hours <= 0 or battery_capacity_kwh <= 0:
        return False
    energy_kwh = soc_energy_kwh(current_soc - min_soc, battery_capacity_kwh)
    if energy_kwh <= 0:
        return True  # already at or below min SoC
    consumption = max(0.0, net_consumption_kw)
    if consumption <= 0:
        return False  # no house load — no risk from forced discharge
    hours_to_min = energy_kwh / consumption
    return hours_to_min <= remaining_hours * (1 + headroom)


def calculate_discharge_power(
    current_soc: float,
    min_soc: int,
    battery_capacity_kwh: float,
    remaining_hours: float,
    max_power_w: int,
    net_consumption_kw: float = 0.0,
    headroom: float = 0.10,
    feedin_remaining_kwh: float | None = None,
) -> int:
    """Calculate the discharge power needed to reach min SoC by window end.

    Unlike charge, household consumption *assists* discharge (the load
    drains the battery alongside the inverter), so *net_consumption_kw*
    is **subtracted** from the required discharge rate.

    When *feedin_remaining_kwh* is provided, the target energy is capped
    so the grid export budget is spread evenly across the remaining window.

    Returns an integer power in watts, clamped to [100, max_power_w].
    """
    energy_kwh = soc_energy_kwh(current_soc - min_soc, battery_capacity_kwh)
    if energy_kwh <= 0:
        return 100
    if remaining_hours <= 0:
        return max_power_w
    # When a feed-in energy limit constrains the session, cap the target
    # energy so the export budget is spread across the full window.
    if (
        feedin_remaining_kwh is not None
        and feedin_remaining_kwh >= 0
        and remaining_hours > 0
    ):
        house_absorption_kwh = max(0.0, net_consumption_kw) * remaining_hours
        max_drain_kwh = feedin_remaining_kwh + house_absorption_kwh
        if max_drain_kwh < energy_kwh:
            _LOGGER.debug(
                "Smart discharge: capping target energy %.2f kWh -> %.2f kWh "
                "(feedin_remaining=%.2f kWh, house_absorption=%.2f kWh)",
                energy_kwh,
                max_drain_kwh,
                feedin_remaining_kwh,
                house_absorption_kwh,
            )
            energy_kwh = max_drain_kwh
            if energy_kwh <= 0:
                return 100
    effective_hours = remaining_hours * (1 - headroom)
    if effective_hours <= 0:
        effective_hours = remaining_hours
    battery_power_kw = energy_kwh / effective_hours
    # House load assists discharge — subtract it from needed inverter power.
    battery_power_kw -= max(0.0, net_consumption_kw)
    if battery_power_kw <= 0:
        return 100
    battery_power_kw *= 1 + headroom
    power_w = battery_power_kw * 1000
    return max(100, min(int(power_w), max_power_w))


def calculate_deferred_start(
    current_soc: float,
    target_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    end: datetime.datetime,
    net_consumption_kw: float = 0.0,
    start: datetime.datetime | None = None,
    headroom: float = 0.10,
) -> datetime.datetime:
    """Calculate the latest time to start charging to reach target SoC by *end*.

    Returns *end* if no charging is needed (SoC already at target).
    Returns a time before *end* otherwise; may be in the past if the
    window is too short to reach the target.
    """
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    if energy_needed_kwh <= 0:
        return end
    max_power_kw = max_power_w / 1000.0
    consumption_headroom_kw = max(0.0, net_consumption_kw)
    min_headroom_kw = max_power_kw * headroom
    headroom_kw = max(consumption_headroom_kw, min_headroom_kw)
    effective_charge_kw = max_power_kw - headroom_kw
    if effective_charge_kw <= 0:
        effective_charge_kw = max_power_kw * headroom
    charge_hours = energy_needed_kwh / effective_charge_kw
    buffered_hours = charge_hours / (1 - headroom)
    deferred = end - datetime.timedelta(hours=buffered_hours)
    if start is not None and deferred < start:
        deferred = start
    return deferred
