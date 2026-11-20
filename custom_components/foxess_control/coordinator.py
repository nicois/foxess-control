"""DataUpdateCoordinator for polling the FoxESS Cloud API or external entities."""

from __future__ import annotations

import asyncio
import collections
import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DEFAULT_POLLING_INTERVAL, DOMAIN, POLLED_VARIABLES
from .foxess.inverter import WorkMode
from .foxess_adapter import _ENTITY_MODE_MAP
from .smart_battery.coordinator import EntityCoordinator as _EntityCoordinator
from .smart_battery.coordinator import get_coordinator_soc as _get_coordinator_soc
from .smart_battery.logging import record_operational_error

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .foxess.inverter import Inverter
    from .foxess.local_client import FoxESSLocalClient

_LOGGER = logging.getLogger(__name__)


def _cfg(hass: HomeAssistant) -> Any:
    """Module-level config accessor (monkeypatchable in tests).

    Module-level seam so ``_fetch_all`` is monkeypatchable and to avoid the
    ``_helpers`` import cycle; other call sites import ``_helpers._cfg``
    directly.

    Delegates to :func:`._helpers._cfg` via a lazy import to avoid a
    circular import at module load (``_helpers`` imports
    ``get_coordinator_soc`` from this module).
    """
    from ._helpers import _cfg as _cfg_impl

    return _cfg_impl(hass)


# Consecutive polls a configured additional PV variable may be absent before
# its persistent absence is surfaced as a config error (C-020/C-026).
_ADDITIONAL_PV_MISSING_LIMIT = 3

# Issue #41: how long the REST poll may keep returning one cloud sample (an
# unchanged response ``time``) before it is reported as stale.  Matches the
# cards' ``STALE_AFTER.api`` (foxess-stale.js), so the badge and the Recent
# Errors entry agree.  Judged by duration, not poll count: the datalogger
# uploads every few minutes, so a 60 s poll legitimately repeats samples.
REST_SAMPLE_STALE_AFTER_S = 900
# Repeated-sample episodes kept for the diagnostics download.
REST_REPEAT_HISTORY = 10


def _coerce_kw(value: Any) -> float:
    """Coerce a polled value to float kW; missing/non-numeric -> 0.0.

    Returns the source number as a float *without unit conversion* —
    correctness assumes the configured variable is already in kW.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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

    # Window for considering a WS injection "fresh enough" to suppress the
    # data_freshness flap.  WS pushes every ~5s; the matching realtime_ws
    # STALE_TIMEOUT is 30s, so anything injected in the last 30s means WS is
    # actively delivering data and the REST poll must not downgrade
    # _data_source to "api".  Beyond this the REST poll is the more
    # authoritative source.
    _WS_FRESH_INJECT_WINDOW_S = 30.0

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
        # Last REST-polled value of the optional additional PV variable
        # (AC-coupled second inverter, e.g. meterPower2), in kW. Held so the
        # WS path can reflect total solar between polls: it is handed to
        # map_ws_to_coordinator as the fallback for frames whose `aux` node
        # is absent or unreadable. 0.0 when the feature is unconfigured or
        # the variable is missing.
        self._additional_pv_kw: float = 0.0
        # Consecutive polls the configured additional PV variable has been
        # absent from the API response. After _ADDITIONAL_PV_MISSING_LIMIT
        # polls we surface a config error once (C-020/C-026) — a typo'd or
        # unsupported variable name otherwise silently contributes 0 forever.
        self._additional_pv_missing_count: int = 0
        # Repeated-sample tracking for the REST poll (issue #41).
        self._rest_sample: str | None = None
        self._rest_sample_first_seen: datetime.datetime | None = None
        self._rest_sample_polls = 0
        self._rest_current_repeat: dict[str, Any] | None = None
        self._rest_repeats: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=REST_REPEAT_HISTORY
        )
        # Monotonic timestamp of the most recent WS inject — distinct from
        # _ws_last_time, which is also written by REST polls and so cannot
        # answer "was the most recent update from WS or REST?".  Used by
        # _async_update_data to suppress the data_freshness flap (C-020):
        # when WS is healthy and pushed recently, the REST poll must not
        # downgrade _data_source to "api".
        self._ws_last_inject_monotonic: float | None = None
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

    def _is_ws_alive(self) -> bool:
        """Return True if a WebSocket object is currently connected.

        Used by ``_async_update_data`` to decide whether the REST poll
        should preserve the ``_data_source = "ws"`` badge or downgrade it.
        """
        domain_data = self.hass.data.get(DOMAIN)
        if domain_data is None:
            return False
        ws = getattr(domain_data, "realtime_ws", None)
        if ws is None:
            return False
        return bool(getattr(ws, "is_connected", False))

    def _ws_recently_injected(self, now_monotonic: float) -> bool:
        """Return True if a WS inject occurred within ``_WS_FRESH_INJECT_WINDOW_S``.

        Distinct from ``_ws_last_time``, which is also written by REST polls
        and so cannot answer "was the most recent update from WS?".
        """
        last = self._ws_last_inject_monotonic
        if last is None:
            return False
        return (now_monotonic - last) < self._WS_FRESH_INJECT_WINDOW_S

    @property
    def additional_pv_kw(self) -> float:
        """Last REST-polled AC-coupled generation term, in kW.

        The WS path's fallback for frames that carry no readable ``aux``
        node.  0.0 when the feature is unconfigured.
        """
        return self._additional_pv_kw

    @property
    def _bms_fetch_interval(self) -> float:
        from ._helpers import _cfg
        from .const import DEFAULT_BMS_POLLING_INTERVAL

        try:
            return _cfg(self.hass).bms_polling_interval
        except Exception:
            return float(DEFAULT_BMS_POLLING_INTERVAL)

    def _track_rest_sample(self, sample: Any, extra_var: str | None) -> None:
        """Follow the cloud sample ``time`` across REST polls (issue #41).

        An unchanged ``time`` means the cloud answered with the sample it
        already gave.  A repeat that lasts longer than
        ``REST_SAMPLE_STALE_AFTER_S`` is recorded once in Recent Errors
        (C-026); every repeat of two polls or more is kept, with whether the
        additional PV variable was in the request, for the diagnostics
        download.
        """
        now = dt_util.utcnow()
        if not isinstance(sample, str) or sample != self._rest_sample:
            if self._rest_current_repeat is not None:
                self._rest_current_repeat["ended_at"] = now.isoformat()
                self._rest_current_repeat = None
            self._rest_sample = sample if isinstance(sample, str) else None
            self._rest_sample_first_seen = now
            self._rest_sample_polls = 1 if self._rest_sample else 0
            return

        self._rest_sample_polls += 1
        first_seen = self._rest_sample_first_seen or now
        if self._rest_current_repeat is None:
            self._rest_current_repeat = {
                "sample_time": sample,
                "first_seen_at": first_seen.isoformat(),
                "ended_at": None,
                "polls": self._rest_sample_polls,
                "additional_pv_power_variable": extra_var,
                "stale": False,
            }
            self._rest_repeats.append(self._rest_current_repeat)
        repeat = self._rest_current_repeat
        repeat["polls"] = self._rest_sample_polls
        seconds = (now - first_seen).total_seconds()
        if seconds > REST_SAMPLE_STALE_AFTER_S and not repeat["stale"]:
            repeat["stale"] = True
            _LOGGER.warning(
                "FoxESS Cloud returned the same real-time sample (%s) for %d "
                "polls over %d min; REST data is stale",
                sample,
                self._rest_sample_polls,
                seconds // 60,
            )
            try:
                from ._helpers import _dd

                buffer = getattr(_dd(self.hass), "recent_errors", None)
            except Exception:  # noqa: BLE001 — recording must not break the poll
                buffer = None
            record_operational_error(
                _LOGGER,
                buffer,
                category="stale_data",
                attempted="poll real-time data from the FoxESS Cloud",
                exc=RuntimeError(
                    "the cloud keeps returning the same sample; values are not updating"
                ),
                hint=(
                    "the FoxESS Cloud is answering with a cached sample; if this "
                    "only happens with an additional PV variable set, say so in "
                    "issue #41"
                ),
                context={
                    "sample_time": sample,
                    "seconds_repeated": int(seconds),
                    "consecutive_polls": self._rest_sample_polls,
                    "additional_pv_power_variable": extra_var,
                },
            )

    def rest_data_as_of(self) -> datetime.datetime:
        """When the REST poll's current sample was first received.

        Used as ``_data_last_update`` for REST data, so a repeated sample
        ages on the cards instead of looking fresh every poll (issue #41).
        """
        return self._rest_sample_first_seen or dt_util.utcnow()

    def rest_freshness_snapshot(self) -> dict[str, Any]:
        """Repeated-sample telemetry for the diagnostics download (#41)."""
        first_seen = self._rest_sample_first_seen
        return {
            "sample_time": self._rest_sample,
            "first_seen_at": first_seen.isoformat() if first_seen else None,
            "consecutive_polls": self._rest_sample_polls,
            "stale_after_s": REST_SAMPLE_STALE_AFTER_S,
            "repeats": [dict(r) for r in self._rest_repeats],
        }

    def _fetch_all(self) -> dict[str, Any]:
        """Fetch real-time data and work mode in a single executor job."""
        try:
            extra_var = _cfg(self.hass).additional_pv_power_variable
        except Exception:
            # Config/domain data unavailable (e.g. early startup) — fall back
            # to the base variable set; the feature is purely additive.
            extra_var = None
        variables = [*POLLED_VARIABLES, extra_var] if extra_var else POLLED_VARIABLES
        data = self.inverter.get_real_time(variables)
        self._track_rest_sample(
            getattr(self.inverter, "last_real_time_sample", None), extra_var
        )

        missing = [v for v in POLLED_VARIABLES if v not in data]
        if missing:
            _LOGGER.debug("Polled variables missing from API response: %s", missing)

        if extra_var:
            extra_kw = _coerce_kw(data.get(extra_var))
            if extra_var in data:
                self._additional_pv_missing_count = 0
            else:
                self._additional_pv_missing_count += 1
                _LOGGER.debug(
                    "Additional PV variable %r not in API response; adding 0",
                    extra_var,
                )
                # Fire ONCE, exactly at the limit, so a persistently-missing
                # (typo'd / unsupported) variable surfaces in diagnostics
                # (C-020/C-026) without spamming on every subsequent poll.
                if self._additional_pv_missing_count == _ADDITIONAL_PV_MISSING_LIMIT:
                    try:
                        domain_data = self.hass.data.get(DOMAIN)
                        buffer = getattr(domain_data, "recent_errors", None)
                        record_operational_error(
                            _LOGGER,
                            buffer,
                            category="config",
                            attempted=(f"poll additional PV variable {extra_var!r}"),
                            exc=ValueError(
                                "configured variable not returned by the "
                                "FoxESS API (check the name; AC-coupled extra "
                                "solar source)"
                            ),
                            hint=(
                                "missing for "
                                f"{_ADDITIONAL_PV_MISSING_LIMIT} consecutive "
                                "polls; verify the variable name is valid for "
                                "this inverter model"
                            ),
                            context={
                                "variable": extra_var,
                                "consecutive_missing": (
                                    self._additional_pv_missing_count
                                ),
                            },
                        )
                    except Exception:  # pragma: no cover - logging must not break poll
                        _LOGGER.debug(
                            "Failed to record missing-additional-PV error",
                            exc_info=True,
                        )
            self._additional_pv_kw = extra_kw
            data["pvPower"] = _coerce_kw(data.get("pvPower")) + extra_kw

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
                plant_id, recent_errors=domain_data.recent_errors
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
                recent_errors=domain_data.recent_errors,
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

    def _record_rest_poll_error(self, err: BaseException, *, severity: str) -> None:
        """Record a REST-poll failure that was absorbed by serving last-known data.

        Replaces the prior bare ``_LOGGER.warning`` so the failure is also
        self-sufficiently captured in the diagnostics ring buffer; the
        control flow (keep last-known, no re-raise) is unchanged.
        """
        domain_data = self.hass.data.get(DOMAIN)
        buffer = getattr(domain_data, "recent_errors", None)
        record_operational_error(
            _LOGGER,
            buffer,
            category="rest_poll",
            attempted="FoxESS REST poll (get_real_time)",
            exc=err,
            hint="kept last-known data; will retry on the next poll",
            context={"last_known_data_retained": True},
            severity=severity,
        )

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
                # Transient cloud-API blip: keep last-known data so the UI
                # does not flap.  Record at info so the diagnostics download
                # shows the gap without alarming on a routine blip (C-026).
                self._record_rest_poll_error(err, severity="info")
                return dict(self.data)
            raise UpdateFailed(f"Error fetching FoxESS data: {err}") from err
        except Exception as err:
            if self.data is not None:
                # Unexpected (non-API) failure but we have last-known data:
                # keep serving it.  Record at warning — this path is not the
                # routine API blip and may indicate a real defect (C-026).
                self._record_rest_poll_error(err, severity="warning")
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

        # Reconcile the last-commanded work mode against what the inverter
        # actually reports (issue #11).  MUST run on the event loop (not in
        # _fetch_all, which runs in an executor thread) because it may create/
        # delete HA Repair issues, which assert event-loop affinity.  Detect-
        # and-surface only; the reconciler swallows its own errors.
        from .foxess_adapter import reconcile_work_mode

        interval = self.update_interval or datetime.timedelta(
            seconds=DEFAULT_POLLING_INTERVAL
        )
        reconcile_work_mode(self.hass, DOMAIN, data.get("_work_mode"), interval)

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
                    min(reported + 0.494, self._soc_interpolated),
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
                min(reported + 0.494, self._soc_interpolated),
            )
            data["_soc_interpolated"] = self._soc_interpolated

        self._soc_last_bat_kw = net_bat_kw
        self._ws_last_time = now
        # Set _data_source = "api" UNLESS WS is currently alive and just
        # delivered a frame, in which case keep the previous "ws" badge
        # to suppress the data_freshness flap (C-020 — operational
        # transparency).  Live failure 2026-05-29: every 5-min REST poll
        # downgraded the badge to "api" even though WS was healthy and
        # pushing every ~5s; the next WS frame ~8s later flipped it back,
        # producing a 16h-long ws → api → ws oscillation.
        if self._ws_recently_injected(now) and self._is_ws_alive():
            data["_data_source"] = "ws"
            data["_data_last_update"] = dt_util.utcnow().isoformat()
        else:
            data["_data_source"] = "api"
            # The sample's first-seen time, not "now": a cloud repeating
            # itself must age on the cards (issue #41, C-020).
            data["_data_last_update"] = self.rest_data_as_of().isoformat()
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
                    min(self._soc_last_reported + 0.494, new_val),
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

        # AC-coupled additional solar: add the last REST-polled external PV
        # term to the WS solar reading so streaming frames also reflect total
        # generation. _additional_pv_kw is 0.0 when unconfigured or before the
        # first REST poll, so this is a no-op for everyone else.
        #
        # Skipped when the frame is marked: map_ws_to_coordinator has then
        # already folded the term in (from the live `aux` node, or from this
        # same REST value passed to it as the fallback) and adding it again
        # would double-count.  The mapper is the preferred place for it —
        # only there can the term reach the C-006 grid-direction balance,
        # which runs before this method ever sees the frame (issue #18).
        # This branch remains for callers that map frames without the
        # opt-in wired through.
        if "_additional_pv_kw" in ws_data:
            ws_data = dict(ws_data)
            del ws_data["_additional_pv_kw"]
        elif self._additional_pv_kw and "pvPower" in ws_data:
            ws_data = dict(ws_data)
            ws_data["pvPower"] = (
                float(ws_data.get("pvPower") or 0.0) + self._additional_pv_kw
            )

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
                    min(float(reported_soc) + 0.494, self._soc_interpolated),
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
                    min(self._soc_last_reported + 0.494, self._soc_interpolated),
                )
        self._soc_last_bat_kw = net_bat_kw
        # Update timestamp AFTER integration so elapsed > 0 on the
        # next message.  Unconditional — SoC interpolation needs
        # timing even when feedin data is absent.
        self._ws_last_time = now
        # Record a "last WS inject" timestamp distinct from _ws_last_time
        # (which is also written by REST polls).  _async_update_data uses
        # this to suppress the data_freshness flap when WS is healthy.
        self._ws_last_inject_monotonic = now

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


# foxess_modbus reports the work mode as its select option label ("Self
# Use"), not the WorkMode value ("SelfUse") that the work-mode ENUM sensor
# declares (issue #24).  Reverse of the write-direction map, plus the
# WorkMode values themselves for entities that already use them.
_ENTITY_LABEL_TO_WORK_MODE: dict[str, str] = {
    **{WorkMode(mode).value: WorkMode(mode).value for mode in _ENTITY_MODE_MAP},
    **{label: WorkMode(mode).value for mode, label in _ENTITY_MODE_MAP.items()},
}


class FoxESSEntityCoordinator(_EntityCoordinator):
    """Read inverter state from external HA entities (foxess_modbus interop).

    Thin subclass that binds the shared ``EntityCoordinator`` to the
    ``foxess_control`` domain and translates foxess_modbus work-mode labels
    into WorkMode values.
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

    async def _async_update_data(self) -> dict[str, Any]:
        data = await super()._async_update_data()
        label = data.get("_work_mode")
        if label is not None:
            # A mode with no WorkMode equivalent (e.g. "Peak Shaving") is
            # reported as unknown rather than crashing the ENUM sensor.
            data["_work_mode"] = _ENTITY_LABEL_TO_WORK_MODE.get(label)
        return data


class FoxESSLocalCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Read inverter state from a foxess-local gateway (local backend).

    Two feeds, one dataset:

    * ``GET /state`` on the coordinator's poll interval seeds and refreshes
      the *full* snapshot — temperatures, work mode, counters, liveness.
    * :meth:`async_run_stream` consumes ``GET /events`` and overlays the
      ~5 s ``snapshot`` power/SoC frames between polls, applies ``config``
      events' full state, and surfaces ``connection`` transitions.

    Overlays update ``self.data`` in place and notify listeners directly
    instead of ``async_set_updated_data`` — the same reason as the cloud
    WebSocket path (``inject_realtime_data``): that helper reschedules the
    refresh timer, so 5 s frames would starve the ``/state`` poll.

    The stream is local and cheap, so unlike the cloud WebSocket it is not
    gated on session state: it runs for the life of the entry.

    Liveness is the gateway's own ``online`` flag (server-computed, never
    recomputed from timestamps — clock skew), plus a client-side heartbeat:
    a stream silent for longer than ``stream_idle_timeout_s`` is treated as
    offline and reconnected, since a half-open TCP connection would
    otherwise freeze the last values on screen (C-020).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: FoxESSLocalClient,
        update_interval_seconds: int,
        *,
        stream_idle_timeout_s: float = 20.0,
        reconnect_delay_s: float = 5.0,
        max_reconnect_delay_s: float = 60.0,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=datetime.timedelta(seconds=update_interval_seconds),
        )
        self.client = client
        self.stream_connected = False
        self.last_stream_error: str | None = None
        self._last_event_id: int | None = None
        self._stream_idle_timeout_s = stream_idle_timeout_s
        self._reconnect_delay_s = reconnect_delay_s
        self._max_reconnect_delay_s = max_reconnect_delay_s

    async def _async_update_data(self) -> dict[str, Any]:
        from .foxess.local_client import LocalGatewayError, map_state

        try:
            state = await self.client.get_state()
        except LocalGatewayError as err:
            raise UpdateFailed(f"Local gateway [{err.code}]: {err}") from err
        return map_state(state)

    def _overlay(self, values: dict[str, Any]) -> None:
        if self.data is None:
            return
        merged = dict(self.data)
        merged.update(values)
        self.data = merged
        self.async_update_listeners()

    def _set_stream_state(self, connected: bool, error: str | None) -> None:
        changed = (connected, error) != (self.stream_connected, self.last_stream_error)
        self.stream_connected = connected
        self.last_stream_error = error
        if changed:
            self.async_update_listeners()

    def _handle_event(self, name: str, payload: Any) -> None:
        from .foxess.local_client import map_snapshot_event, map_state

        if not isinstance(payload, dict):
            return
        if name == "snapshot":
            self._overlay(map_snapshot_event(payload))
        elif name == "config" and isinstance(payload.get("state"), dict):
            self._overlay(map_state(payload["state"]))
        elif name == "connection":
            self._overlay({"_gateway_online": payload.get("state") == "online"})
        elif name in ("resync", "gap"):
            # Events were lost: the next /state read is authoritative.
            self.hass.async_create_task(self.async_request_refresh())

    async def async_run_stream(self) -> None:
        """Consume the event stream forever, reconnecting with backoff.

        Never raises (except cancellation): failures are recorded in
        :attr:`last_stream_error` for diagnostics and the ``/state`` poll
        keeps the data current meanwhile.
        """
        from .foxess.local_client import LocalGatewayError

        delay = self._reconnect_delay_s
        while True:
            events = self.client.iter_events(self._last_event_id)
            try:
                while True:
                    try:
                        event_id, name, payload = await asyncio.wait_for(
                            anext(events), self._stream_idle_timeout_s
                        )
                    except StopAsyncIteration:
                        self._set_stream_state(False, "event stream ended")
                        break
                    except TimeoutError:
                        _LOGGER.warning(
                            "Local gateway event stream silent for %.0fs; "
                            "treating the inverter as offline and reconnecting",
                            self._stream_idle_timeout_s,
                        )
                        self._set_stream_state(False, "event stream went silent")
                        self._overlay({"_gateway_online": False})
                        break
                    if event_id is not None:
                        self._last_event_id = event_id
                    self._set_stream_state(True, None)
                    delay = self._reconnect_delay_s
                    self._handle_event(name, payload)
            except LocalGatewayError as err:
                _LOGGER.warning("Local gateway event stream failed: %s", err)
                self._set_stream_state(False, f"{err.code}: {err}")
            except asyncio.CancelledError:
                self._set_stream_state(False, None)
                raise
            finally:
                await events.aclose()
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay_s)
