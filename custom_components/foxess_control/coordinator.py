"""DataUpdateCoordinator for polling the FoxESS Cloud API or external entities."""

from __future__ import annotations

import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, POLLED_VARIABLES
from .smart_battery.coordinator import EntityCoordinator as _EntityCoordinator
from .smart_battery.coordinator import get_coordinator_soc as _get_coordinator_soc

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .foxess.inverter import Inverter

_LOGGER = logging.getLogger(__name__)


def get_coordinator_soc(hass: HomeAssistant) -> float | None:
    """Read SoC from the first available coordinator in hass.data."""
    return _get_coordinator_soc(hass, DOMAIN)


class FoxESSDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch real-time variables from the FoxESS Cloud API."""

    def __init__(
        self,
        hass: HomeAssistant,
        inverter: Inverter,
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(seconds=update_interval_seconds),
        )
        self.inverter = inverter
        # WebSocket feed-in energy integration state
        self._ws_last_time: float | None = None
        self._ws_feedin_power_kw: float = 0.0
        # SoC interpolation state — integrates power between integer ticks
        self._soc_interpolated: float | None = None
        self._soc_last_reported: float | None = None
        self._soc_last_bat_kw: float = 0.0  # net: positive=charging
        # Periodic SoC extrapolation between REST polls
        self._soc_interp_cancel: Callable[[], None] | None = None

    def _get_capacity_kwh(self) -> float:
        """Read battery capacity from config (needed for SoC integration)."""
        from .const import CONF_BATTERY_CAPACITY_KWH

        for key in self.hass.data.get(DOMAIN, {}):
            if not str(key).startswith("_"):
                entry = self.hass.config_entries.async_get_entry(str(key))
                if entry is not None:
                    return float(entry.options.get(CONF_BATTERY_CAPACITY_KWH, 0))
        return 0.0

    def _fetch_all(self) -> dict[str, Any]:
        """Fetch real-time data and work mode in a single executor job."""
        data = self.inverter.get_real_time(POLLED_VARIABLES)

        missing = [v for v in POLLED_VARIABLES if v not in data]
        if missing:
            _LOGGER.debug("Polled variables missing from API response: %s", missing)

        try:
            mode = self.inverter.get_current_mode()
            data["_work_mode"] = mode.value if mode is not None else None
        except Exception:
            _LOGGER.debug("Failed to fetch work mode, skipping", exc_info=True)
            data["_work_mode"] = None

        return data

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = await self.hass.async_add_executor_job(
                self._fetch_all
            )
        except Exception as err:
            # If we already have data (from a previous poll or WS injection),
            # keep showing it rather than making all entities unavailable.
            # This avoids a brief "—" flash when REST is temporarily down
            # but WS was just providing valid data.
            if self.data is not None:
                _LOGGER.warning("REST poll failed, keeping last-known data: %s", err)
                return dict(self.data)
            raise UpdateFailed(f"Error fetching FoxESS data: {err}") from err
        # REST poll is authoritative — reset WebSocket feed-in integration
        self._ws_feedin_power_kw = 0.0

        # SoC interpolation: integrate battery power between polls.
        # On each REST poll, advance the interpolated SoC by the power
        # delta since the last poll, then resync to the authoritative
        # integer SoC when it ticks.  This provides sub-percent
        # estimates even in REST-only mode (no WS).
        rest_soc = data.get("SoC")
        now = time.monotonic()
        charge_kw = data.get("batChargePower", 0.0)
        discharge_kw = data.get("batDischargePower", 0.0)
        net_bat_kw = charge_kw - discharge_kw

        if rest_soc is not None:
            if self._soc_interpolated is None:
                self._soc_interpolated = float(rest_soc)
                self._soc_last_reported = float(rest_soc)
            elif float(rest_soc) != self._soc_last_reported:
                # Integer SoC tick changed — resync
                self._soc_interpolated = float(rest_soc)
                self._soc_last_reported = float(rest_soc)
            elif self._ws_last_time is not None:
                # Same integer tick — advance by power integration
                elapsed_h = (now - self._ws_last_time) / 3600.0
                if elapsed_h > 0:
                    avg_kw = (self._soc_last_bat_kw + net_bat_kw) / 2.0
                    capacity = self._get_capacity_kwh()
                    if capacity > 0:
                        delta_pct = avg_kw * elapsed_h / capacity * 100.0
                        self._soc_interpolated = max(
                            0.0, min(100.0, self._soc_interpolated + delta_pct)
                        )
            data["_soc_interpolated"] = round(self._soc_interpolated, 1)

        self._soc_last_bat_kw = net_bat_kw
        self._ws_last_time = now
        data["_data_source"] = "api"
        self._schedule_soc_extrapolation()
        return data

    _SOC_EXTRAP_INTERVAL = 30  # seconds between extrapolation ticks

    def _schedule_soc_extrapolation(self) -> None:
        """Schedule periodic SoC extrapolation between REST polls.

        Between REST polls the sensor attributes are frozen, so the
        progress bar shows stale integer SoC.  This timer advances the
        interpolated SoC by extrapolating the last-known battery power
        and pushes an update to refresh sensors.  Runs only when there
        is meaningful battery power and no active WebSocket (WS already
        pushes ~5-second updates).
        """
        if self._soc_interp_cancel is not None:
            self._soc_interp_cancel()
            self._soc_interp_cancel = None

        if (
            self._soc_interpolated is None
            or abs(self._soc_last_bat_kw) < 0.01
            or self._get_capacity_kwh() <= 0
        ):
            return

        @callback
        def _tick(_now: datetime.datetime) -> None:
            self._soc_interp_cancel = None
            if self.data is None or self._soc_interpolated is None:
                return
            # Don't extrapolate if WS is providing updates
            if self.data.get("_data_source") == "ws":
                return

            now = time.monotonic()
            if self._ws_last_time is None:
                return
            elapsed_h = (now - self._ws_last_time) / 3600.0
            if elapsed_h <= 0:
                return

            capacity = self._get_capacity_kwh()
            if capacity <= 0:
                return

            delta_pct = self._soc_last_bat_kw * elapsed_h / capacity * 100.0
            new_val = max(0.0, min(100.0, self._soc_interpolated + delta_pct))
            new_rounded = round(new_val, 1)
            old_rounded = round(self._soc_interpolated, 1)

            self._soc_interpolated = new_val
            self._ws_last_time = now

            if new_rounded != old_rounded:
                merged = dict(self.data)
                merged["_soc_interpolated"] = new_rounded
                self.async_set_updated_data(merged)

            # Schedule next tick
            if abs(self._soc_last_bat_kw) >= 0.01:
                self._soc_interp_cancel = async_call_later(
                    self.hass, self._SOC_EXTRAP_INTERVAL, _tick
                )

        self._soc_interp_cancel = async_call_later(
            self.hass, self._SOC_EXTRAP_INTERVAL, _tick
        )

    def inject_realtime_data(self, ws_data: dict[str, Any]) -> None:
        """Merge WebSocket real-time data into the current coordinator data.

        Only overlays the subset of variables the WebSocket provides
        (SoC, power values).  The full REST-polled dataset remains the
        base, so variables not in the WebSocket stream (cumulative energy
        counters, temperatures, etc.) stay current from the last REST poll.

        Additionally, integrates the instantaneous ``feedinPower`` (kW)
        over time to approximate the cumulative ``feedin`` energy counter
        between REST polls.  The REST value is authoritative and resets
        the integration when a new poll arrives.
        """
        if self.data is None:
            return

        # WS provides its own frequent updates — stop REST extrapolation
        if self._soc_interp_cancel is not None:
            self._soc_interp_cancel()
            self._soc_interp_cancel = None

        # Integrate feedinPower into the cumulative feedin energy counter
        now = time.monotonic()
        feedin_power_kw = ws_data.get("feedinPower")
        if feedin_power_kw is not None and self._ws_last_time is not None:
            elapsed_hours = (now - self._ws_last_time) / 3600.0
            if elapsed_hours > 0:
                # Use average of previous and current power (trapezoidal)
                avg_kw = (self._ws_feedin_power_kw + feedin_power_kw) / 2.0
                delta_kwh = avg_kw * elapsed_hours
                if delta_kwh > 0:
                    base_feedin = self.data.get("feedin")
                    if base_feedin is not None:
                        try:
                            ws_data = dict(ws_data)  # don't mutate caller's dict
                            ws_data["feedin"] = float(base_feedin) + delta_kwh
                        except (ValueError, TypeError):
                            pass
        if feedin_power_kw is not None:
            self._ws_feedin_power_kw = feedin_power_kw
            self._ws_last_time = now

        # Integrate battery power into sub-percent SoC estimate.
        # Uses the same trapezoidal approach as feedin integration.
        charge_kw = ws_data.get("batChargePower", 0.0)
        discharge_kw = ws_data.get("batDischargePower", 0.0)
        net_bat_kw = charge_kw - discharge_kw  # positive = charging
        reported_soc = ws_data.get("SoC")

        if reported_soc is not None:
            if self._soc_interpolated is None:
                # First reading — initialise
                self._soc_interpolated = reported_soc
                self._soc_last_reported = reported_soc
            elif reported_soc != self._soc_last_reported:
                # Integer SoC tick changed — resync to authoritative value
                self._soc_interpolated = reported_soc
                self._soc_last_reported = reported_soc

        if self._soc_interpolated is not None and self._ws_last_time is not None:
            elapsed_hours = (now - self._ws_last_time) / 3600.0
            if elapsed_hours > 0:
                avg_kw = (self._soc_last_bat_kw + net_bat_kw) / 2.0
                capacity = self._get_capacity_kwh()
                if capacity > 0:
                    delta_pct = avg_kw * elapsed_hours / capacity * 100.0
                    self._soc_interpolated = max(
                        0.0, min(100.0, self._soc_interpolated + delta_pct)
                    )
        self._soc_last_bat_kw = net_bat_kw

        # Expose interpolated SoC for display (sensors, progress bars)
        if self._soc_interpolated is not None:
            ws_data = dict(ws_data) if not isinstance(ws_data, dict) else ws_data
            ws_data["_soc_interpolated"] = round(self._soc_interpolated, 1)

        ws_data["_data_source"] = "ws"

        # Instrumentation: warn when WS power values are >10x different
        # from existing coordinator values (catches unit mismatch).
        for key in ("batChargePower", "batDischargePower"):
            ws_val = ws_data.get(key)
            cur_val = self.data.get(key)
            if (
                ws_val is not None
                and cur_val is not None
                and cur_val > 0.1
                and ws_val > 0
                and (ws_val / cur_val > 10 or cur_val / ws_val > 10)
            ):
                _LOGGER.warning(
                    "WS %s diverges >10x from coordinator: ws=%.4f, "
                    "existing=%.4f (possible unit mismatch)",
                    key,
                    ws_val,
                    cur_val,
                )

        # Skip if nothing actually changed (avoids redundant entity updates)
        if all(self.data.get(k) == v for k, v in ws_data.items()):
            return
        merged = dict(self.data)
        merged.update(ws_data)
        self.async_set_updated_data(merged)


class FoxESSEntityCoordinator(_EntityCoordinator):
    """Read inverter state from external HA entities (foxess_modbus interop).

    Thin subclass that binds the shared ``EntityCoordinator`` to the
    ``foxess_control`` domain.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_map: dict[str, str],
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            domain=DOMAIN,
            entity_map=entity_map,
            update_interval_seconds=update_interval_seconds,
        )
