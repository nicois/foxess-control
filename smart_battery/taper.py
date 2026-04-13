"""Adaptive BMS taper model for smart battery control.

Lithium batteries enter a constant-voltage (CV) phase at high SoC,
reducing charge acceptance below the inverter's configured limit.
Similarly, the BMS may limit discharge at very low SoC.

This module maintains a SoC-indexed profile of observed actual/requested
power ratios, using exponential moving average (EMA) for adaptation.
The profile is used to:
  - Estimate realistic charge/discharge time (accounting for taper)
  - Avoid false "behind schedule" alerts at high SoC
  - Calculate more accurate deferred start times

The model adapts naturally to temperature changes: recent observations
(reflecting current conditions) dominate via EMA weighting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# EMA smoothing factor — 0.3 adapts in ~3-5 observations while
# smoothing single-tick noise.
EMA_ALPHA = 0.3

# Minimum observation count before a bin's ratio is trusted.
MIN_TRUST_COUNT = 2

# Ratio bounds — 0.05 floor prevents divide-by-zero in time estimation;
# 1.0 cap means the battery never accepts more than requested.
MIN_RATIO = 0.05
MAX_RATIO = 1.0

# Only record observations when requested power is at least this value,
# to avoid pollution from ramp-up/ramp-down transients.
MIN_REQUESTED_W = 500


@dataclass
class TaperBin:
    """A single SoC bucket's taper observation."""

    ratio: float  # EMA of actual/requested power
    count: int  # number of observations


@dataclass
class TaperProfile:
    """SoC-indexed profile of BMS charge/discharge taper behaviour."""

    charge: dict[int, TaperBin] = field(default_factory=dict)
    discharge: dict[int, TaperBin] = field(default_factory=dict)

    # -- Recording ----------------------------------------------------------

    def record_charge(self, soc: float, requested_w: int, actual_w: float) -> None:
        """Record a charge taper observation."""
        self._record(self.charge, soc, requested_w, actual_w)

    def record_discharge(self, soc: float, requested_w: int, actual_w: float) -> None:
        """Record a discharge taper observation."""
        self._record(self.discharge, soc, requested_w, actual_w)

    @staticmethod
    def _record(
        bins: dict[int, TaperBin],
        soc: float,
        requested_w: int,
        actual_w: float,
    ) -> None:
        if requested_w < MIN_REQUESTED_W:
            return
        raw_ratio = actual_w / requested_w
        ratio = max(MIN_RATIO, min(MAX_RATIO, raw_ratio))
        bucket = int(soc)
        bucket = max(0, min(100, bucket))

        existing = bins.get(bucket)
        if existing is None or existing.count == 0:
            bins[bucket] = TaperBin(ratio=ratio, count=1)
        else:
            new_ratio = EMA_ALPHA * ratio + (1 - EMA_ALPHA) * existing.ratio
            bins[bucket] = TaperBin(
                ratio=max(MIN_RATIO, min(MAX_RATIO, new_ratio)),
                count=existing.count + 1,
            )

    # -- Querying -----------------------------------------------------------

    def charge_ratio(self, soc: float) -> float:
        """Expected actual/requested ratio for charging at *soc* %."""
        return self._ratio(self.charge, soc)

    def discharge_ratio(self, soc: float) -> float:
        """Expected actual/requested ratio for discharging at *soc* %."""
        return self._ratio(self.discharge, soc)

    @staticmethod
    def _ratio(bins: dict[int, TaperBin], soc: float) -> float:
        bucket = max(0, min(100, int(soc)))
        b = bins.get(bucket)
        if b is not None and b.count >= MIN_TRUST_COUNT:
            return b.ratio

        # Nearest-neighbor interpolation — walk outward up to 5% SoC
        for offset in range(1, 6):
            for candidate in (bucket - offset, bucket + offset):
                if 0 <= candidate <= 100:
                    b = bins.get(candidate)
                    if b is not None and b.count >= MIN_TRUST_COUNT:
                        return b.ratio

        # Edge extrapolation: if we're beyond all observed data, use the
        # nearest edge bin.  BMS taper only gets worse at extremes — using
        # the edge ratio is conservative and far better than assuming 1.0.
        trusted = [k for k, v in bins.items() if v.count >= MIN_TRUST_COUNT]
        if trusted:
            if bucket > max(trusted):
                return bins[max(trusted)].ratio
            if bucket < min(trusted):
                return bins[min(trusted)].ratio

        return 1.0  # no data at all — assume no taper

    # -- Time estimation ----------------------------------------------------

    def estimate_charge_hours(
        self,
        current_soc: float,
        target_soc: int,
        capacity_kwh: float,
        max_power_w: int,
    ) -> float:
        """Estimate hours to charge from *current_soc* to *target_soc*.

        Numerically integrates over 1% SoC steps, applying the taper
        ratio at each step.
        """
        return self._estimate_hours(
            self.charge, current_soc, target_soc, capacity_kwh, max_power_w
        )

    def estimate_discharge_hours(
        self,
        current_soc: float,
        min_soc: int,
        capacity_kwh: float,
        max_power_w: int,
    ) -> float:
        """Estimate hours to discharge from *current_soc* to *min_soc*."""
        return self._estimate_hours(
            self.discharge, current_soc, min_soc, capacity_kwh, max_power_w
        )

    @staticmethod
    def _estimate_hours(
        bins: dict[int, TaperBin],
        from_soc: float,
        to_soc: int,
        capacity_kwh: float,
        max_power_w: int,
    ) -> float:
        if max_power_w <= 0 or capacity_kwh <= 0:
            return 0.0

        max_power_kw = max_power_w / 1000.0
        energy_per_pct = capacity_kwh / 100.0
        total_hours = 0.0

        # Determine direction and range
        if from_soc < to_soc:
            # Charging: from_soc → to_soc (ascending)
            start = int(from_soc)
            end = int(to_soc)
            for soc_pct in range(start, end):
                ratio = TaperProfile._ratio(bins, float(soc_pct))
                effective_kw = max_power_kw * ratio
                total_hours += energy_per_pct / effective_kw
        else:
            # Discharging: from_soc → to_soc (descending)
            start = int(from_soc)
            end = int(to_soc)
            for soc_pct in range(start, end, -1):
                ratio = TaperProfile._ratio(bins, float(soc_pct))
                effective_kw = max_power_kw * ratio
                total_hours += energy_per_pct / effective_kw

        return total_hours

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict for HA Store."""
        return {
            "charge": {str(k): [b.ratio, b.count] for k, b in self.charge.items()},
            "discharge": {
                str(k): [b.ratio, b.count] for k, b in self.discharge.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaperProfile:
        """Deserialize from HA Store data."""
        charge: dict[int, TaperBin] = {}
        discharge: dict[int, TaperBin] = {}

        for k, v in (data.get("charge") or {}).items():
            try:
                soc = int(k)
                ratio, count = float(v[0]), int(v[1])
                charge[soc] = TaperBin(
                    ratio=max(MIN_RATIO, min(MAX_RATIO, ratio)), count=count
                )
            except (ValueError, TypeError, IndexError):
                continue

        for k, v in (data.get("discharge") or {}).items():
            try:
                soc = int(k)
                ratio, count = float(v[0]), int(v[1])
                discharge[soc] = TaperBin(
                    ratio=max(MIN_RATIO, min(MAX_RATIO, ratio)), count=count
                )
            except (ValueError, TypeError, IndexError):
                continue

        return cls(charge=charge, discharge=discharge)
