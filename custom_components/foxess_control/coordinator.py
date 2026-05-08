"""DataUpdateCoordinator for polling the FoxESS Cloud API or external entities."""

from __future__ import annotations

import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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

    # BMS temperature is fetched from the web portal on its own cadence.
    # Historically this avoided a starvation bug where WS injections called
    # async_set_updated_data() and reset the REST poll timer; the injection
    # path now updates self.data in place without touching the refresh timer,
    # so this separate schedule is no longer strictly required but is kept
    # as a safety net and to throttle portal-side requests.
    _BMS_REDISCOVERY_BACKOFF = 300.0

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
        # BMS temperature polling state (separate from REST poll timer)
        self._bms_last_fetch: float = 0.0
        self._bms_fetch_in_flight: bool = False
        self._bms_last_rediscovery_attempt: float = 0.0
        # SoC interpolation state — integrates power between integer ticks
        self._soc_interpolated: float | None = None
        self._soc_last_reported: float | None = None
        self._soc_last_bat_kw: float = 0.0  # net: positive=charging
        # Periodic SoC extrapolation between REST polls
        self._soc_interp_cancel: Callable[[], None] | None = None

    def _get_capacity_kwh(self) -> float:
        """Read battery capacity from config (needed for SoC integration)."""
        from ._helpers import _cfg

        try:
            return _cfg(self.hass).battery_capacity_kwh
        except Exception:
            return 0.0

    @property
    def _bms_fetch_interval(self) -> float:
        from ._helpers import _cfg
        from .const import DEFAULT_BMS_POLLING_INTERVAL

        try:
            return _cfg(self.hass).bms_polling_interval
        except Exception:
            return float(DEFAULT_BMS_POLLING_INTERVAL)

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

    async def _retry_pending_cleanup(self) -> None:
        """Attempt to remove a stale schedule override left by a failed abort."""
        from .foxess.inverter import WorkMode
        from .foxess_adapter import _remove_mode_from_schedule

        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return
        pending = domain_data.pending_override_cleanup
        if pending is None:
            return
        mode_str = pending.get("mode", "")
        try:
            mode = WorkMode(mode_str)
        except ValueError:
            _LOGGER.warning(
                "Pending override cleanup: invalid mode '%s', discarding", mode_str
            )
            domain_data.pending_override_cleanup = None
            return
        from ._helpers import _cfg

        try:
            min_soc_on_grid = _cfg(self.hass).min_soc_on_grid
        except Exception:
            min_soc_on_grid = 11
        try:
            await self.hass.async_add_executor_job(
                _remove_mode_from_schedule,
                self.inverter,
                mode,
                min_soc_on_grid,
            )
            domain_data.pending_override_cleanup = None
            _LOGGER.info("Pending override cleanup succeeded: removed %s", mode.value)
        except Exception:
            _LOGGER.warning(
                "Pending override cleanup failed, will retry next poll: %s",
                mode.value,
            )

    def _preserve_bms_temperature(self, data: dict[str, Any]) -> None:
        """Carry forward the last known BMS temperature into *data*."""
        if self.data and "bmsBatteryTemperature" in self.data:
            data["bmsBatteryTemperature"] = self.data["bmsBatteryTemperature"]

    async def _rediscover_battery_compound_id(self, domain_data: Any) -> str | None:
        """Attempt to rediscover the battery compound ID via the web session.

        Called when compound_id is missing (e.g. after HA restart where
        the in-memory value was lost and the one-shot startup discovery
        failed).  Throttled by _BMS_REDISCOVERY_BACKOFF.
        """
        now = time.monotonic()
        if (
            self._bms_last_rediscovery_attempt > 0
            and now - self._bms_last_rediscovery_attempt < self._BMS_REDISCOVERY_BACKOFF
        ):
            _LOGGER.debug(
                "BMS temperature: compound ID re-discovery throttled "
                "(%.0fs since last attempt)",
                now - self._bms_last_rediscovery_attempt,
            )
            return None

        self._bms_last_rediscovery_attempt = now
        web_session = domain_data.web_session

        plant_id = domain_data.plant_id
        if not plant_id:
            try:
                plant_id = await self.hass.async_add_executor_job(
                    self.inverter.get_plant_id
                )
                domain_data.plant_id = plant_id
                _LOGGER.info("BMS re-discovery: discovered plant_id: %s", plant_id)
            except Exception:
                _LOGGER.warning(
                    "BMS re-discovery: could not discover plant_id", exc_info=True
                )
                return None

        if not plant_id:
            return None

        try:
            compound_id: str | None = await web_session.async_discover_battery_id(
                plant_id
            )
            if compound_id:
                domain_data.battery_compound_id = compound_id
                _LOGGER.info(
                    "BMS re-discovery: found battery compound ID: %s", compound_id
                )
            else:
                _LOGGER.warning(
                    "BMS re-discovery: battery compound ID not found — "
                    "will retry in %.0fs",
                    self._BMS_REDISCOVERY_BACKOFF,
                )
            return compound_id
        except Exception:
            _LOGGER.warning(
                "BMS re-discovery: failed to discover battery compound ID",
                exc_info=True,
            )
            return None

    async def _fetch_bms_temperature(self, data: dict[str, Any]) -> None:
        """Fetch BMS min cell temperature from the web portal and merge into data."""
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            _LOGGER.debug("BMS temperature: domain data not available")
            return
        web_session = domain_data.web_session
        if web_session is None:
            _LOGGER.warning(
                "BMS temperature: no web session configured — check web "
                "credentials in the integration options"
            )
            return
        compound_id = domain_data.battery_compound_id
        if not compound_id:
            compound_id = await self._rediscover_battery_compound_id(domain_data)
            if not compound_id:
                self._preserve_bms_temperature(data)
                return
        try:
            temp = await web_session.async_get_battery_temperature(
                battery_compound_id=compound_id,
            )
            if temp is not None:
                data["bmsBatteryTemperature"] = temp
                self._bms_last_fetch = time.monotonic()
                _LOGGER.info("BMS temperature: %.1f°C", temp)
            else:
                _LOGGER.info("BMS temperature: no value returned from web portal")
                self._preserve_bms_temperature(data)
        except Exception:
            _LOGGER.info("BMS temperature fetch failed", exc_info=True)
            self._preserve_bms_temperature(data)

    async def _async_update_data(self) -> dict[str, Any]:
        from .foxess.client import FoxESSApiError

        try:
            data: dict[str, Any] = await self.hass.async_add_executor_job(
                self._fetch_all
            )
        except FoxESSApiError as err:
            if err.is_auth_error:
                raise ConfigEntryAuthFailed(
                    "FoxESS API key is invalid or expired"
                ) from err
            if self.data is not None:
                _LOGGER.warning("REST poll failed, keeping last-known data: %s", err)
                return dict(self.data)
            raise UpdateFailed(f"Error fetching FoxESS data: {err}") from err
        except Exception as err:
            if self.data is not None:
                _LOGGER.warning("REST poll failed, keeping last-known data: %s", err)
                return dict(self.data)
            raise UpdateFailed(f"Error fetching FoxESS data: {err}") from err

        # Retry pending override cleanup from a previous failed session abort.
        # Stored by listeners when adapter.remove_override() fails (e.g. DNS
        # outage).  On each successful REST poll, attempt the cleanup again.
        await self._retry_pending_cleanup()

        # Fetch BMS battery temperature from the web portal (if configured).
        # This is the min cell temperature from the BMS — more operationally
        # relevant than the Open API's batTemperature (inverter sensor).
        await self._fetch_bms_temperature(data)

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
            reported = float(rest_soc)
            if self._soc_interpolated is None:
                self._soc_interpolated = reported
                self._soc_last_reported = reported
            elif reported != self._soc_last_reported:
                # Integer SoC tick changed — clamp the interpolated
                # value into the rounding bucket of the new tick so
                # Math.round(interpolated) always equals the entity.
                self._soc_interpolated = max(
                    reported - 0.5,
                    min(reported + 0.44, self._soc_interpolated),
                )
                self._soc_last_reported = reported
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
            # Keep within the rounding bucket of the authoritative tick
            self._soc_interpolated = max(
                reported - 0.5,
                min(reported + 0.44, self._soc_interpolated),
            )
            data["_soc_interpolated"] = self._soc_interpolated

        self._soc_last_bat_kw = net_bat_kw
        self._ws_last_time = now
        data["_data_source"] = "api"
        data["_data_last_update"] = dt_util.utcnow().isoformat()
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
            if self._soc_last_reported is not None:
                new_val = max(
                    self._soc_last_reported - 0.5,
                    min(self._soc_last_reported + 0.44, new_val),
                )
            new_rounded = round(new_val, 2)
            old_rounded = round(self._soc_interpolated, 2)

            self._soc_interpolated = new_val
            self._ws_last_time = now

            if new_rounded != old_rounded:
                merged = dict(self.data)
                merged["_soc_interpolated"] = new_val
                self.data = merged
                self.async_update_listeners()

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

        compound_id = ws_data.pop("_battery_compound_id", None)
        if compound_id:
            domain_data = self.hass.data.get(DOMAIN)
            if domain_data is not None:
                domain_data.battery_compound_id = compound_id

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

        # Integrate battery power into sub-percent SoC estimate.
        # Uses the same trapezoidal approach as feedin integration.
        charge_kw = ws_data.get("batChargePower", 0.0)
        discharge_kw = ws_data.get("batDischargePower", 0.0)
        net_bat_kw = charge_kw - discharge_kw  # positive = charging
        reported_soc = ws_data.get("SoC")

        if reported_soc is not None:
            if self._soc_interpolated is None:
                self._soc_interpolated = float(reported_soc)
                self._soc_last_reported = float(reported_soc)
            elif float(reported_soc) != self._soc_last_reported:
                # Integer SoC tick changed — clamp into rounding bucket
                self._soc_interpolated = max(
                    float(reported_soc) - 0.5,
                    min(float(reported_soc) + 0.44, self._soc_interpolated),
                )
                self._soc_last_reported = float(reported_soc)

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
            # Keep interpolated within the rounding bucket of the
            # last authoritative tick so Math.round() matches the entity.
            if self._soc_last_reported is not None:
                self._soc_interpolated = max(
                    self._soc_last_reported - 0.5,
                    min(self._soc_last_reported + 0.44, self._soc_interpolated),
                )
        self._soc_last_bat_kw = net_bat_kw
        # Update timestamp AFTER integration so elapsed > 0 on the
        # next message.  Unconditional — SoC interpolation needs
        # timing even when feedin data is absent.
        self._ws_last_time = now

        # Expose interpolated SoC for display (sensors, progress bars)
        if self._soc_interpolated is not None:
            ws_data = dict(ws_data) if not isinstance(ws_data, dict) else ws_data
            ws_data["_soc_interpolated"] = self._soc_interpolated

        ws_data["_data_source"] = "ws"
        ws_data["_data_last_update"] = dt_util.utcnow().isoformat()

        # Skip if nothing actually changed (avoids redundant entity updates).
        # Exclude _data_last_update — it always differs.
        if all(
            self.data.get(k) == v
            for k, v in ws_data.items()
            if k != "_data_last_update"
        ):
            return
        merged = dict(self.data)
        merged.update(ws_data)
        # NOTE: deliberately NOT calling self.async_set_updated_data(merged).
        # That helper cancels and reschedules the DataUpdateCoordinator's
        # refresh timer, so WS messages arriving faster than update_interval
        # would starve the REST poll indefinitely and leave ~25 REST-only
        # fields (cumulative energy, PV string power, voltages/currents,
        # temps, freq, meter, EPS) stale while WS is active.
        # Instead, update self.data in place and notify listeners directly;
        # the refresh timer continues to fire on its configured cadence.
        self.data = merged
        self.last_update_success = True
        self.async_update_listeners()

        # BMS temperature polling on its own cadence (see class docstring).
        self._maybe_schedule_bms_fetch()

    def _maybe_schedule_bms_fetch(self) -> None:
        """Schedule a BMS temperature fetch if the interval has elapsed."""
        if self._bms_fetch_in_flight:
            return
        # Quick pre-check: skip if no web session is configured (avoids
        # creating a coroutine that would immediately return).
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None or domain_data.web_session is None:
            return
        now = time.monotonic()
        if (
            self._bms_last_fetch > 0
            and now - self._bms_last_fetch < self._bms_fetch_interval
        ):
            return
        self._bms_fetch_in_flight = True

        async def _do_fetch() -> None:
            try:
                if self.data is None:
                    return
                data = dict(self.data)
                await self._fetch_bms_temperature(data)
                temp = data.get("bmsBatteryTemperature")
                if temp is not None and self.data is not None:
                    merged = dict(self.data)
                    merged["bmsBatteryTemperature"] = temp
                    self.async_set_updated_data(merged)
            finally:
                self._bms_fetch_in_flight = False

        self.hass.async_create_task(_do_fetch(), name="foxess_bms_temp_fetch")


class FoxESSEntityCoordinator(_EntityCoordinator):
    """Read inverter state from external HA entities (foxess_modbus interop).

    Thin subclass that binds the shared ``EntityCoordinator`` to the
    ``foxess_control`` domain.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_map: dict[str, tuple[str, str]],
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            domain=DOMAIN,
            entity_map=entity_map,
            update_interval_seconds=update_interval_seconds,
        )
