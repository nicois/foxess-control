"""Pure pacing algorithms for smart battery charge and discharge.

All functions in this module are pure — they take numeric inputs and
return numeric outputs with **zero** Home Assistant dependency.  This
makes them trivially testable and reusable across any inverter brand.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from .const import (
    FEEDIN_FALLBACK_RATIO,
    MAX_FEEDIN_HEADROOM,
    MIN_CHARGE_POWER_W,
    MIN_DISCHARGE_POWER_W,
)

if TYPE_CHECKING:
    from .taper import TaperProfile

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
    taper_profile: TaperProfile | None = None,
    bms_temp_c: float | None = None,
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
        return MIN_CHARGE_POWER_W
    if remaining_hours <= 0:
        return max_power_w

    # Check if we're behind the ideal trajectory.
    # When a taper profile is available, compare time-progress vs
    # energy-progress proportionally (accounts for non-linear charge rates).
    # Without taper data, fall back to the linear trajectory check.
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
                actual_energy = soc_energy_kwh(current_soc, battery_capacity_kwh)
                tolerance_kwh = min_power_change_w / 1000.0 * remaining_hours

                if taper_profile is not None:
                    # Taper-aware: compare proportional progress.
                    # time_frac = how much of the window has elapsed
                    # energy_frac = how much energy has been delivered
                    # If energy_frac >= time_frac we are on track.
                    start_soc = (
                        charging_started_energy_kwh / battery_capacity_kwh * 100.0
                    )
                    total_hours = taper_profile.estimate_charge_hours(
                        start_soc,
                        target_soc,
                        battery_capacity_kwh,
                        max_power_w,
                        temp_c=bms_temp_c,
                    )
                    if total_hours > 0:
                        time_frac = min(
                            elapsed_since_charge_started / effective_window,
                            1.0,
                        )
                        energy_delivered = actual_energy - charging_started_energy_kwh
                        energy_frac = energy_delivered / energy_to_add
                        deficit = (time_frac - energy_frac) * energy_to_add
                        if deficit > tolerance_kwh:
                            _LOGGER.debug(
                                "Smart charge: behind taper-adjusted "
                                "schedule (energy %.1f%% vs time %.1f%%, "
                                "deficit %.3f > tolerance %.3f), "
                                "charging at max power",
                                energy_frac * 100,
                                time_frac * 100,
                                deficit,
                                tolerance_kwh,
                            )
                            return max_power_w
                else:
                    # Linear trajectory check (no taper data).
                    progress = min(elapsed_since_charge_started / effective_window, 1.0)
                    ideal_energy_now = (
                        charging_started_energy_kwh + progress * energy_to_add
                    )
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
    return max(MIN_CHARGE_POWER_W, min(int(power_w), max_power_w))


def is_charge_target_reachable(
    current_soc: float,
    target_soc: int,
    battery_capacity_kwh: float,
    remaining_hours: float,
    max_power_w: int,
    net_consumption_kw: float = 0.0,
    headroom: float = 0.10,
    taper_profile: TaperProfile | None = None,
    bms_temp_c: float | None = None,
) -> bool:
    """Return True if the charge target can be reached in the remaining time.

    Uses the same consumption headroom as the deferred start calculation.
    If even max power for the full remaining window (with headroom) can't
    deliver enough energy, the target is unreachable.

    When a *taper_profile* is provided, two estimates are computed:

    1. **Taper-integrated hours** — sums per-SoC-step hours using the
       full taper curve (the same estimate used by pacing).
    2. **Median-ratio hours** — a linear estimate using the median
       trusted ratio across the traversed SoC range as a
       representative taper factor.

    The minimum of these two is taken as the feasibility bound.  This
    is a *feasibility* check (C-022): returning False should mean "no
    plausible scenario reaches the target", not "the pessimistic
    integration says so".  Isolated outlier observations (e.g. a single
    tick where the BMS briefly reported near-zero acceptance) can dominate
    the integrated estimate, pushing it above the remaining window even
    when the typical scenario comfortably fits.  Using the median-ratio
    estimate as a floor prevents those outliers from producing spurious
    HA Repair issues — without masking genuinely unreachable targets,
    since a short window or uniformly low ratios produce a large
    median-ratio estimate too.
    """
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    if energy_needed_kwh <= 0:
        return True
    if remaining_hours <= 0:
        return False

    max_power_kw = max_power_w / 1000.0
    consumption_headroom_kw = max(0.0, net_consumption_kw)
    min_headroom_kw = max_power_kw * headroom
    headroom_kw = max(consumption_headroom_kw, min_headroom_kw)
    effective_charge_kw = max_power_kw - headroom_kw
    if effective_charge_kw <= 0:
        effective_charge_kw = max_power_kw * headroom

    if taper_profile is not None:
        integrated_hours = taper_profile.estimate_charge_hours(
            current_soc,
            target_soc,
            battery_capacity_kwh,
            int(effective_charge_kw * 1000),
            temp_c=bms_temp_c,
        )
        # Median-ratio linear estimate: represents a typical (non-outlier)
        # scenario across the traversed range.  Combines with the
        # multiplicative temperature factor in the same way estimate_charge_hours
        # does internally, so warm vs cold is still distinguished.
        median_ratio = _median_trusted_charge_ratio(
            taper_profile, int(current_soc), int(target_soc)
        )
        temp_factor = taper_profile.charge_temp_factor(bms_temp_c)
        effective_ratio = max(0.05, min(1.0, median_ratio * temp_factor))
        median_hours = energy_needed_kwh / (effective_charge_kw * effective_ratio)
        charge_hours = min(integrated_hours, median_hours)
    else:
        charge_hours = energy_needed_kwh / effective_charge_kw

    denom = 1 - headroom
    buffered_hours = charge_hours / denom if denom > 0 else charge_hours
    return buffered_hours <= remaining_hours


def _median_trusted_charge_ratio(
    taper_profile: TaperProfile, from_soc: int, to_soc: int
) -> float:
    """Median of trusted charge ratios across ``[from_soc, to_soc)``.

    Used by :func:`is_charge_target_reachable` as an outlier-robust
    linear-estimate floor — a single noisy observation cannot pull the
    median low enough to flip the feasibility verdict.

    Falls back to 1.0 (no-taper assumption) when no trusted bins exist
    in the range; this preserves the original linear behaviour for
    fresh or sparsely populated profiles.
    """
    from .taper import MIN_TRUST_COUNT

    lo, hi = (from_soc, to_soc) if from_soc <= to_soc else (to_soc, from_soc)
    ratios = [
        b.ratio
        for soc, b in taper_profile.charge.items()
        if lo <= soc < hi and b.count >= MIN_TRUST_COUNT
    ]
    if not ratios:
        return 1.0
    ratios.sort()
    return ratios[len(ratios) // 2]


PEAK_DECAY_PER_TICK = 0.85
"""Exponential decay factor applied to peak consumption each polling tick.

At 0.85/tick with 5-min polling, half-life is ~21 minutes — long enough
to protect against recurring spikes but short enough to adapt to falling loads.
"""

DISCHARGE_SAFETY_FACTOR = 1.5
"""Multiplier applied to peak consumption for the discharge safety floor.

During forced discharge the inverter output must cover house load or the
shortfall is imported from the grid.  The safety factor provides margin
above the highest *observed* consumption, protecting against inter-poll
spikes that would otherwise cause grid import.
"""

_END_GUARD_MINUTES = 10.0
"""Stop forced discharge when remaining energy above min SoC can't sustain
the safety floor for this many minutes.

At the tail end of a discharge, when nearly all available energy is spent,
the paced power drops to the 100 W minimum — well below house load.  The
shortfall is imported from the grid.  Switching to self-use ~10 min early
lets the inverter serve house load directly from the battery without
importing, at the cost of a small amount of forgone export.

The guard scales with consumption: high loads produce a larger guard
(more at stake), while low loads produce a tiny guard (negligible loss).
"""


def compute_safe_schedule_end(
    current_soc: float,
    min_soc: float,
    capacity_kwh: float,
    power_w: int,
    window_end: datetime.datetime,
    *,
    safety_factor: float = DISCHARGE_SAFETY_FACTOR,
    now: datetime.datetime | None = None,
) -> datetime.datetime:
    """Compute the latest safe schedule end time for forced discharge.

    Returns the time at which the battery would reach *min_soc* at the
    current *power_w* discharge rate, divided by *safety_factor*.
    Capped at *window_end*.

    If HA loses connectivity, the inverter's schedule expires at this
    time and reverts to self-use — the battery is protected without
    HA intervention.
    """
    energy_kwh = (current_soc - min_soc) / 100 * capacity_kwh
    drain_kw = power_w / 1000
    if drain_kw <= 0 or energy_kwh <= 0:
        return window_end
    safe_hours = energy_kwh / drain_kw / safety_factor
    if now is None:
        # Match window_end's timezone awareness
        now = (
            datetime.datetime.now(tz=window_end.tzinfo)
            if window_end.tzinfo
            else datetime.datetime.now()
        )
    safe_end = now + datetime.timedelta(hours=safe_hours)
    return min(safe_end, window_end)


def safety_floor_w(peak_kw: float) -> int:
    """Return the discharge safety floor in watts for a given peak consumption."""
    return int(peak_kw * DISCHARGE_SAFETY_FACTOR * 1000)


def should_suspend_discharge(
    current_soc: float,
    min_soc: int,
    battery_capacity_kwh: float,
    remaining_hours: float,
    net_consumption_kw: float,
    headroom: float = 0.10,
    consumption_peak_kw: float | None = None,
) -> bool:
    """Return True if forced discharge should be suspended.

    Suspension protects the min SoC target.  If household consumption
    alone would drain the battery to (or past) min SoC within the
    remaining window, adding *any* forced discharge power risks
    breaching the floor.

    Also triggers an **end-of-discharge guard**: when the remaining
    energy above *min_soc* can't sustain the safety floor for at least
    ~10 minutes, forced discharge would soon collapse to the 100 W
    minimum — well below house load — causing grid import.  Self-use
    avoids this because the inverter matches house load exactly from
    the battery until *min_soc*, without importing from the grid.

    When *consumption_peak_kw* is provided, the peak (rather than
    instantaneous) consumption is used — this accounts for recent load
    spikes that may recur between polling intervals.
    """
    if remaining_hours <= 0 or battery_capacity_kwh <= 0:
        return False
    energy_kwh = soc_energy_kwh(current_soc - min_soc, battery_capacity_kwh)
    if energy_kwh <= 0:
        return True  # already at or below min SoC
    consumption = max(0.0, net_consumption_kw)
    if consumption_peak_kw is not None:
        consumption = max(consumption, consumption_peak_kw)

    # End-of-discharge guard: forced discharge at rates below house
    # load causes grid import.  When remaining energy is too small to
    # sustain the safety floor (peak × safety factor), the algorithm
    # would soon return the 100 W minimum, creating import.  Switching
    # to self-use lets the inverter serve house load directly from the
    # battery until min_soc without import or export.
    if consumption > 0:
        floor_kw = consumption * DISCHARGE_SAFETY_FACTOR
        guard_kwh = floor_kw * (_END_GUARD_MINUTES / 60.0)
        if energy_kwh < guard_kwh:
            return True

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
    consumption_peak_kw: float | None = None,
) -> int:
    """Calculate the discharge power needed to reach min SoC by window end.

    **Priority ordering** (highest first):

    1. **No grid import** — the discharge rate is floored at
       ``max(current, peak) × DISCHARGE_SAFETY_FACTOR`` so the battery
       always covers house load with a margin for inter-poll spikes.
    2. **Protect min SoC** — handled by the caller via
       :func:`should_suspend_discharge`.
    3. **Meet energy target** — when *feedin_remaining_kwh* is set, the
       target energy is capped so the export budget is spread across the
       remaining window.
    4. **Maximise feed-in** — pacing spreads export over the window.

    When priorities conflict, higher priorities win.  The safety floor
    may cause faster battery drain than the paced rate; this is
    acceptable because avoiding grid import (P1) outweighs meeting the
    exact energy target (P3) or maximising feed-in (P4).

    Returns an integer power in watts, clamped to [100, max_power_w].
    """
    energy_kwh = soc_energy_kwh(current_soc - min_soc, battery_capacity_kwh)
    if energy_kwh <= 0:
        return MIN_DISCHARGE_POWER_W
    if remaining_hours <= 0:
        return max_power_w

    consumption = max(0.0, net_consumption_kw)
    peak = max(consumption, consumption_peak_kw or 0.0)
    safety_floor_w = int(peak * DISCHARGE_SAFETY_FACTOR * 1000)

    # When a feed-in energy limit constrains the session, cap the target
    # energy so the export budget is spread across the full window.
    has_feedin_target = (
        feedin_remaining_kwh is not None
        and feedin_remaining_kwh >= 0
        and remaining_hours > 0
    )
    if has_feedin_target and feedin_remaining_kwh is not None:
        house_absorption_kwh = consumption * remaining_hours
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
                if 0 < safety_floor_w <= max_power_w:
                    return max(MIN_DISCHARGE_POWER_W, safety_floor_w)
                return MIN_DISCHARGE_POWER_W

    effective_hours = remaining_hours * (1 - headroom)
    if effective_hours <= 0:
        effective_hours = remaining_hours
    battery_power_kw = energy_kwh / effective_hours
    # House load assists discharge — subtract it from needed inverter power.
    battery_power_kw -= consumption
    battery_power_kw *= 1 + headroom
    power_w = battery_power_kw * 1000

    # P1: Floor at peak consumption × safety factor to prevent grid import.
    # Uses the highest observed consumption (not just current) so that
    # inter-poll spikes don't cause import.  Skip when peak consumption
    # exceeds inverter capacity (grid import is unavoidable).
    if 0 < safety_floor_w <= max_power_w:
        power_w = max(power_w, safety_floor_w)

    return max(MIN_DISCHARGE_POWER_W, min(int(power_w), max_power_w))


def calculate_deferred_start(
    current_soc: float,
    target_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    end: datetime.datetime,
    net_consumption_kw: float = 0.0,
    start: datetime.datetime | None = None,
    headroom: float = 0.10,
    taper_profile: TaperProfile | None = None,
    bms_temp_c: float | None = None,
) -> datetime.datetime:
    """Calculate the latest time to start charging to reach target SoC by *end*.

    When *taper_profile* is provided, uses the taper-aware time estimate
    instead of the linear ``energy / power`` calculation.  This accounts
    for BMS charge current reduction at high SoC.

    Returns *end* if no charging is needed (SoC already at target).
    Returns a time before *end* otherwise; may be in the past if the
    window is too short to reach the target.
    """
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    if energy_needed_kwh <= 0:
        return end

    # Reduce effective power by consumption headroom — house load eats
    # into the inverter's charge budget regardless of taper.
    max_power_kw = max_power_w / 1000.0
    consumption_headroom_kw = max(0.0, net_consumption_kw)
    min_headroom_kw = max_power_kw * headroom
    headroom_kw = max(consumption_headroom_kw, min_headroom_kw)
    effective_charge_kw = max_power_kw - headroom_kw
    if effective_charge_kw <= 0:
        effective_charge_kw = max_power_kw * headroom
    if effective_charge_kw <= 0:
        return start if start is not None else end

    if taper_profile is not None:
        charge_hours = taper_profile.estimate_charge_hours(
            current_soc,
            target_soc,
            battery_capacity_kwh,
            int(effective_charge_kw * 1000),
            temp_c=bms_temp_c,
        )
    else:
        charge_hours = energy_needed_kwh / effective_charge_kw

    denom = 1 - headroom
    buffered_hours = charge_hours / denom if denom > 0 else charge_hours
    deferred = end - datetime.timedelta(hours=buffered_hours)
    if start is not None and deferred < start:
        deferred = start
    return deferred


def calculate_discharge_deferred_start(
    current_soc: float,
    min_soc: int,
    battery_capacity_kwh: float,
    max_power_w: int,
    end: datetime.datetime,
    net_consumption_kw: float = 0.0,
    start: datetime.datetime | None = None,
    headroom: float = 0.10,
    taper_profile: TaperProfile | None = None,
    feedin_energy_limit_kwh: float | None = None,
    consumption_peak_kw: float | None = None,
    bms_temp_c: float | None = None,
    grid_export_limit_w: int = 0,
) -> datetime.datetime:
    """Calculate the latest time to start forced discharge to meet goals by *end*.

    During the deferred phase the inverter stays in self-use mode, where
    house consumption naturally drains the battery without grid export.

    Two independent deadlines are computed (the earlier wins):

    1. **SoC deadline** — how long full-power discharge takes to drain
       from *current_soc* to *min_soc*.

    2. **Feed-in energy deadline** — self-use does not export, so ALL
       required grid export must come from forced discharge.  House load
       *reduces* net export (``grid_export = discharge - house_load``).
       When *consumption_peak_kw* is provided, the peak (worst-case)
       consumption is used for the effective export rate estimate, making
       the deadline more conservative for volatile loads.

    Returns *end* if no forced discharge is needed.
    """
    max_power_kw = max_power_w / 1000.0
    if max_power_kw <= 0:
        return end

    # --- SoC deadline ---
    soc_deadline = end
    energy_to_discharge_kwh = (current_soc - min_soc) / 100.0 * battery_capacity_kwh
    if energy_to_discharge_kwh > 0:
        # House consumption assists discharge — subtract it.
        consumption = max(0.0, net_consumption_kw)
        effective_kw = max_power_kw - consumption
        if effective_kw <= 0:
            # House load alone exceeds max discharge — self-use will
            # drain the battery without any forced discharge needed.
            discharge_hours = 0.0
        elif taper_profile is not None:
            discharge_hours = taper_profile.estimate_discharge_hours(
                current_soc,
                min_soc,
                battery_capacity_kwh,
                int(effective_kw * 1000),
                temp_c=bms_temp_c,
            )
        else:
            discharge_hours = energy_to_discharge_kwh / effective_kw

        # When a feed-in energy limit constrains the session, the
        # session stops at the export target — not at min_soc.  Always
        # cap discharge time so the SoC deadline reflects feedin drain,
        # not the full SoC drain (D-005).  Re-evaluated each tick.
        if (
            feedin_energy_limit_kwh is not None
            and feedin_energy_limit_kwh > 0
            and effective_kw > 0
        ):
            export_rate_kw = effective_kw
            if grid_export_limit_w > 0:
                export_rate_kw = min(export_rate_kw, grid_export_limit_w / 1000.0)
            if export_rate_kw > 0:
                feedin_hours = feedin_energy_limit_kwh / export_rate_kw
            else:
                feedin_hours = feedin_energy_limit_kwh / effective_kw
            discharge_hours = min(discharge_hours, feedin_hours)

        if discharge_hours > 0:
            denom = 1 - headroom
            buffered_hours = discharge_hours / denom if denom > 0 else discharge_hours
            soc_deadline = end - datetime.timedelta(hours=buffered_hours)

    # --- Feed-in energy deadline ---
    # Use doubled headroom: house consumption is variable and all export
    # must come from forced discharge, so we start earlier to absorb
    # load spikes that reduce net grid export during the burst.
    feedin_deadline = end
    feedin_headroom = min(headroom * 2, MAX_FEEDIN_HEADROOM)
    if feedin_energy_limit_kwh is not None and feedin_energy_limit_kwh > 0:
        # Use peak consumption (if available) for a conservative export
        # rate estimate.  Volatile loads reduce the effective export rate
        # (grid_export = discharge - house), so using the peak means we
        # start earlier — sacrificing self-use time to ensure the export
        # target is achievable even during load spikes.
        consumption = max(0.0, net_consumption_kw)
        if consumption_peak_kw is not None:
            consumption = max(consumption, consumption_peak_kw)
        effective_export_kw = max_power_kw - consumption
        if grid_export_limit_w > 0:
            effective_export_kw = min(effective_export_kw, grid_export_limit_w / 1000.0)
        if effective_export_kw <= 0:
            effective_export_kw = max_power_kw * FEEDIN_FALLBACK_RATIO
        if effective_export_kw <= 0:
            feedin_deadline = start if start is not None else end
        else:
            feedin_hours = feedin_energy_limit_kwh / effective_export_kw
            denom = 1 - feedin_headroom
            buffered_hours = feedin_hours / denom if denom > 0 else feedin_hours
            feedin_deadline = end - datetime.timedelta(hours=buffered_hours)

    # Take the earlier deadline (whichever requires starting sooner).
    deferred = min(soc_deadline, feedin_deadline)
    if start is not None and deferred < start:
        deferred = start
    return deferred
