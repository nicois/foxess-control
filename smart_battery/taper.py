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

Temperature effects are physically independent of SoC effects (CV-phase
vs lithium plating protection), so the model uses a multiplicative
decomposition: effective_ratio = soc_ratio(soc) * temp_factor(temp_c).
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

# Minimum actual power (W) to accept an observation.  If the inverter
# reports less than this when a significant charge/discharge was requested,
# the reading is likely a sensor error or unit mismatch, not real taper.
MIN_ACTUAL_W = 50

# Minimum observations before trusting a temperature bin.  Slightly higher
# than SoC's MIN_TRUST_COUNT because temperature observations are noisier.
MIN_TEMP_TRUST_COUNT = 3

# Nearest-neighbor search range for temperature bins (°C).
TEMP_NEIGHBOR_RANGE = 3


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
    charge_temp: dict[int, TaperBin] = field(default_factory=dict)
    discharge_temp: dict[int, TaperBin] = field(default_factory=dict)

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
        if actual_w < MIN_ACTUAL_W:
            # Implausibly low — likely a sensor error or unit mismatch,
            # not genuine BMS taper.  Skip to avoid corrupting the profile.
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

    # -- Temperature recording ------------------------------------------------

    def record_charge_temp(
        self,
        temp_c: float,
        soc: float,
        requested_w: int,
        actual_w: float,
    ) -> None:
        """Record a charge temperature-taper observation."""
        self._record_temp(
            self.charge_temp, self.charge, temp_c, soc, requested_w, actual_w
        )

    def record_discharge_temp(
        self,
        temp_c: float,
        soc: float,
        requested_w: int,
        actual_w: float,
    ) -> None:
        """Record a discharge temperature-taper observation."""
        self._record_temp(
            self.discharge_temp, self.discharge, temp_c, soc, requested_w, actual_w
        )

    @staticmethod
    def _record_temp(
        temp_bins: dict[int, TaperBin],
        soc_bins: dict[int, TaperBin],
        temp_c: float,
        soc: float,
        requested_w: int,
        actual_w: float,
    ) -> None:
        if requested_w < MIN_REQUESTED_W:
            return
        if actual_w < MIN_ACTUAL_W:
            return

        raw_ratio = actual_w / requested_w

        # Factor out the SoC effect to isolate temperature contribution.
        soc_ratio = TaperProfile._ratio(soc_bins, soc)
        if soc_ratio <= MIN_RATIO:
            return  # can't divide reliably

        temp_factor = raw_ratio / soc_ratio
        temp_factor = max(MIN_RATIO, min(MAX_RATIO, temp_factor))

        bucket = max(-20, min(60, int(temp_c)))

        existing = temp_bins.get(bucket)
        if existing is None or existing.count == 0:
            temp_bins[bucket] = TaperBin(ratio=temp_factor, count=1)
        else:
            new_ratio = EMA_ALPHA * temp_factor + (1 - EMA_ALPHA) * existing.ratio
            temp_bins[bucket] = TaperBin(
                ratio=max(MIN_RATIO, min(MAX_RATIO, new_ratio)),
                count=existing.count + 1,
            )

    # -- Querying -----------------------------------------------------------

    def charge_ratio(self, soc: float, temp_c: float | None = None) -> float:
        """Expected actual/requested ratio for charging at *soc* %."""
        return max(
            MIN_RATIO,
            min(
                MAX_RATIO,
                self._ratio(self.charge, soc) * self.charge_temp_factor(temp_c),
            ),
        )

    def discharge_ratio(self, soc: float, temp_c: float | None = None) -> float:
        """Expected actual/requested ratio for discharging at *soc* %."""
        return max(
            MIN_RATIO,
            min(
                MAX_RATIO,
                self._ratio(self.discharge, soc) * self.discharge_temp_factor(temp_c),
            ),
        )

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

    # -- Temperature factor -------------------------------------------------

    def charge_temp_factor(self, temp_c: float | None) -> float:
        """Temperature adjustment factor for charging."""
        return self._temp_factor(self.charge_temp, temp_c)

    def discharge_temp_factor(self, temp_c: float | None) -> float:
        """Temperature adjustment factor for discharging."""
        return self._temp_factor(self.discharge_temp, temp_c)

    @staticmethod
    def _temp_factor(bins: dict[int, TaperBin], temp_c: float | None) -> float:
        if temp_c is None:
            return 1.0

        bucket = max(-20, min(60, int(temp_c)))
        b = bins.get(bucket)
        if b is not None and b.count >= MIN_TEMP_TRUST_COUNT:
            return b.ratio

        # Nearest-neighbor — walk outward up to TEMP_NEIGHBOR_RANGE °C
        for offset in range(1, TEMP_NEIGHBOR_RANGE + 1):
            for candidate in (bucket - offset, bucket + offset):
                if -20 <= candidate <= 60:
                    b = bins.get(candidate)
                    if b is not None and b.count >= MIN_TEMP_TRUST_COUNT:
                        return b.ratio

        # Edge extrapolation
        trusted = [k for k, v in bins.items() if v.count >= MIN_TEMP_TRUST_COUNT]
        if trusted:
            if bucket > max(trusted):
                return bins[max(trusted)].ratio
            if bucket < min(trusted):
                return bins[min(trusted)].ratio

        return 1.0  # no data — assume no temperature effect

    # -- Time estimation ----------------------------------------------------

    def estimate_charge_hours(
        self,
        current_soc: float,
        target_soc: int,
        capacity_kwh: float,
        max_power_w: int,
        temp_c: float | None = None,
    ) -> float:
        """Estimate hours to charge from *current_soc* to *target_soc*.

        Numerically integrates over 1% SoC steps, applying the taper
        ratio at each step.
        """
        return self._estimate_hours(
            self.charge,
            current_soc,
            target_soc,
            capacity_kwh,
            max_power_w,
            temp_factor=self.charge_temp_factor(temp_c),
        )

    def estimate_discharge_hours(
        self,
        current_soc: float,
        min_soc: int,
        capacity_kwh: float,
        max_power_w: int,
        temp_c: float | None = None,
    ) -> float:
        """Estimate hours to discharge from *current_soc* to *min_soc*."""
        return self._estimate_hours(
            self.discharge,
            current_soc,
            min_soc,
            capacity_kwh,
            max_power_w,
            temp_factor=self.discharge_temp_factor(temp_c),
        )

    @staticmethod
    def _estimate_hours(
        bins: dict[int, TaperBin],
        from_soc: float,
        to_soc: int,
        capacity_kwh: float,
        max_power_w: int,
        temp_factor: float = 1.0,
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
                soc_ratio = TaperProfile._ratio(bins, float(soc_pct))
                ratio = max(MIN_RATIO, min(MAX_RATIO, soc_ratio * temp_factor))
                effective_kw = max_power_kw * ratio
                total_hours += energy_per_pct / effective_kw
        else:
            # Discharging: from_soc → to_soc (descending)
            start = int(from_soc)
            end = int(to_soc)
            for soc_pct in range(start, end, -1):
                soc_ratio = TaperProfile._ratio(bins, float(soc_pct))
                ratio = max(MIN_RATIO, min(MAX_RATIO, soc_ratio * temp_factor))
                effective_kw = max_power_kw * ratio
                total_hours += energy_per_pct / effective_kw

        return total_hours

    # -- Validation ---------------------------------------------------------

    def is_plausible(self) -> bool:
        """Check whether the stored profile looks sane.

        A healthy profile has most ratios well above MIN_RATIO.  If the
        median trusted ratio is suspiciously low, the profile was likely
        corrupted (e.g. by a sensor unit mismatch) and should be discarded.
        """
        for bins in (self.charge, self.discharge):
            trusted = [b.ratio for b in bins.values() if b.count >= MIN_TRUST_COUNT]
            if not trusted:
                continue
            median = sorted(trusted)[len(trusted) // 2]
            if median <= MIN_RATIO * 2:
                return False

        for bins in (self.charge_temp, self.discharge_temp):
            trusted = [
                b.ratio for b in bins.values() if b.count >= MIN_TEMP_TRUST_COUNT
            ]
            if not trusted:
                continue
            median = sorted(trusted)[len(trusted) // 2]
            if median <= MIN_RATIO * 2:
                return False

        return True

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict for HA Store."""
        result: dict[str, Any] = {
            "charge": {str(k): [b.ratio, b.count] for k, b in self.charge.items()},
            "discharge": {
                str(k): [b.ratio, b.count] for k, b in self.discharge.items()
            },
        }
        if self.charge_temp:
            result["charge_temp"] = {
                str(k): [b.ratio, b.count] for k, b in self.charge_temp.items()
            }
        if self.discharge_temp:
            result["discharge_temp"] = {
                str(k): [b.ratio, b.count] for k, b in self.discharge_temp.items()
            }
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaperProfile:
        """Deserialize from HA Store data."""
        return cls(
            charge=cls._deserialize_bins(data.get("charge"), key_range=(0, 100)),
            discharge=cls._deserialize_bins(data.get("discharge"), key_range=(0, 100)),
            charge_temp=cls._deserialize_bins(
                data.get("charge_temp"), key_range=(-20, 60)
            ),
            discharge_temp=cls._deserialize_bins(
                data.get("discharge_temp"), key_range=(-20, 60)
            ),
        )

    @staticmethod
    def _deserialize_bins(raw: Any, key_range: tuple[int, int]) -> dict[int, TaperBin]:
        """Deserialize a dict of bins, clamping keys to *key_range*."""
        result: dict[int, TaperBin] = {}
        lo, hi = key_range
        for k, v in (raw or {}).items():
            try:
                key = int(k)
                key = max(lo, min(hi, key))
                ratio, count = float(v[0]), int(v[1])
                result[key] = TaperBin(
                    ratio=max(MIN_RATIO, min(MAX_RATIO, ratio)), count=count
                )
            except (ValueError, TypeError, IndexError):
                continue
        return result
