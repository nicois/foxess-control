"""WebSocket client for FoxESS Cloud real-time inverter data.

Connects to the undocumented ``/dew/v0/wsmaitian`` endpoint which
streams inverter power data every ~5 seconds.  Used only during active
forced discharge to provide the pacing algorithm with fresh data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .web_session import FoxESSWebSession

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data mapping — pure function, no I/O
# ---------------------------------------------------------------------------


def _parse_power(power_obj: dict[str, Any] | None) -> tuple[float, str] | None:
    """Extract numeric value and unit from a WebSocket power object.

    Returns ``(value, unit)`` where *unit* is the raw ``unit`` field
    from the WS message (e.g. ``"W"``, ``"kW"``, or ``""``).
    The WS normally sends watts but sometimes sends kW for specific
    fields — the unit field is the authoritative indicator.
    """
    if power_obj is None:
        return None
    val = power_obj.get("value")
    if val is None:
        return None
    try:
        return float(val), str(power_obj.get("unit", "W"))
    except (ValueError, TypeError):
        return None


def _to_kw(parsed: tuple[float, str] | None) -> float | None:
    """Convert a parsed power value to kW, respecting the unit field."""
    if parsed is None:
        return None
    value, unit = parsed
    if unit == "kW":
        _LOGGER.debug("WS power value already in kW: %.3f (unit=%s)", value, unit)
        return value
    return value / 1000.0


_POWER_KEYS = (
    "batChargePower",
    "batDischargePower",
    "pvPower",
    "loadsPower",
    "gridConsumptionPower",
    "feedinPower",
)


def _is_plausible(candidate: dict[str, Any], reference: dict[str, Any] | None) -> bool:
    """Return False if any power key in *candidate* diverges >10x from *reference*.

    Edge cases preserved from the original coordinator-level filter:
    - *reference* is ``None`` or missing the key → accept (first message).
    - Reference value <= 0.1 kW → accept (ramp-up from near-zero).
    - Candidate value == 0 → accept (genuine stop).
    """
    if reference is None:
        return True
    for key in _POWER_KEYS:
        cand_val = candidate.get(key)
        ref_val = reference.get(key)
        if (
            cand_val is not None
            and ref_val is not None
            and ref_val > 0.1
            and cand_val > 0
            and (cand_val / ref_val > 10 or ref_val / cand_val > 10)
        ):
            _LOGGER.warning(
                "WS %s diverges >10x: candidate=%.4f, "
                "last_accepted=%.4f — dropping anomalous message",
                key,
                cand_val,
                ref_val,
            )
            return False
    return True


def map_ws_to_coordinator(ws_msg: dict[str, Any]) -> dict[str, Any]:
    """Map a WebSocket message to coordinator variable names.

    The WebSocket normally sends power values in **watts** (as strings)
    but the cloud API sometimes sends individual fields in **kW**.
    Each power object's ``unit`` field is checked: ``"kW"`` means the
    value is already in kW; anything else (``"W"``, absent) means watts
    and is divided by 1000.  This handles mixed units within a single
    message.
    """
    node = ws_msg.get("result", {}).get("node", {})
    if not node:
        return {}

    data: dict[str, Any] = {}

    # Battery SoC
    bat = node.get("bat", {})
    soc = bat.get("soc")
    if soc is not None:
        with contextlib.suppress(ValueError, TypeError):
            data["SoC"] = float(soc)

    # Battery power — direction indicated by bat.charge (1=charging)
    bat_kw = _to_kw(_parse_power(bat.get("power")))
    if bat_kw is not None:
        is_charging = str(bat.get("charge")) == "1"
        data["batChargePower"] = bat_kw if is_charging else 0.0
        data["batDischargePower"] = bat_kw if not is_charging else 0.0

    # Solar power
    solar_kw = _to_kw(_parse_power(node.get("solar", {}).get("power")))
    if solar_kw is not None:
        data["pvPower"] = solar_kw

    # House load
    load_kw = _to_kw(_parse_power(node.get("load", {}).get("power")))
    if load_kw is not None:
        data["loadsPower"] = load_kw

    # Grid power — derive direction from the power balance, falling back
    # to gridStatus when the balance is unreliable (e.g. unmeasured
    # external generation makes the predicted magnitude diverge from the
    # actual grid reading).
    grid = node.get("grid", {})
    grid_kw = _to_kw(_parse_power(grid.get("power")))
    if grid_kw is not None:
        solar = data.get("pvPower")
        load = data.get("loadsPower")
        bat_charge = data.get("batChargePower", 0.0)
        bat_discharge = data.get("batDischargePower", 0.0)

        if solar is not None and load is not None:
            net = load + bat_charge - bat_discharge - solar
            predicted_kw = abs(net)
            # When the balance-predicted magnitude is close to the actual
            # grid reading, the balance is trustworthy.  When they diverge
            # significantly (ratio > 3x or predicted ≈ 0 while grid is
            # substantial), an unmeasured source is skewing the balance —
            # fall back to gridStatus.
            balance_reliable = (
                grid_kw < 0.05
                or predicted_kw > 0.05
                and max(grid_kw / predicted_kw, predicted_kw / grid_kw) < 3.0
            )
            if balance_reliable:
                importing = net > 0
            else:
                _LOGGER.debug(
                    "Grid balance unreliable: predicted=%.3f kW, "
                    "actual=%.3f kW — using gridStatus",
                    predicted_kw,
                    grid_kw,
                )
                importing = str(grid.get("gridStatus", "")) == "3"
        else:
            importing = str(grid.get("gridStatus", "")) == "3"

        if importing:
            data["gridConsumptionPower"] = grid_kw
            data["feedinPower"] = 0.0
        else:
            data["gridConsumptionPower"] = 0.0
            data["feedinPower"] = grid_kw

    battery_id = bat.get("batteryId")
    bat_sn_list = bat.get("multipleBatterySoc", [])
    if battery_id and bat_sn_list:
        first_sn = bat_sn_list[0].get("batSn", "")
        if first_sn:
            data["_battery_compound_id"] = f"{battery_id}@{first_sn}"

    if data:
        _LOGGER.debug(
            "WS mapped data: %s (gridStatus=%s) raw_node=%s",
            data,
            grid.get("gridStatus") if grid else None,
            node,
        )

    return data


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------


class FoxESSRealtimeWS:
    """Async WebSocket client for the FoxESS real-time data stream."""

    WS_URL = "wss://www.foxesscloud.com/dew/v0/wsmaitian"
    RECONNECT_MAX_ATTEMPTS = 5
    RECONNECT_BASE_DELAY = 5.0
    RECONNECT_MAX_DELAY = 60.0
    STALE_TIMEOUT = 30.0  # no message in this many seconds = dead
    MAX_TIME_DIFF = 30  # skip messages older than this (seconds)

    def __init__(
        self,
        plant_id: str,
        web_session: FoxESSWebSession,
        on_data: Callable[[dict[str, Any]], Awaitable[None]],
        on_disconnect: Callable[[], None],
        ws_url: str | None = None,
        should_reconnect: Callable[[], bool] | None = None,
    ) -> None:
        self._plant_id = plant_id
        self._web_session = web_session
        self._on_data = on_data
        self._on_disconnect = on_disconnect
        # The single reconciliation predicate: "should the live WS
        # connection exist right now?".  Wired by the brand layer to
        # ``_should_start_realtime_ws(hass)`` — the SAME gate the start
        # chokepoint (``_maybe_start_realtime_ws``) consults.  Checked by
        # ``_try_reconnect`` before scheduling any reconnect I/O so the
        # otherwise-autonomous reconnect loop cannot resurrect a
        # connection the gate says should be DOWN (the idle
        # connect→stale→reconnect leak, live 2026-06-07: the loop gated
        # only on the instance-local ``_no_reconnect``/``_stop_event``
        # flags and never saw the gate's answer).  ``None`` preserves the
        # legacy unconditional-reconnect behaviour (e.g. ``always`` mode
        # and tests that supply no predicate).  C-020 / C-025 / D-008.
        self.should_reconnect = should_reconnect
        if ws_url is not None:
            self.WS_URL = ws_url
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._connected = False
        self._stop_event = asyncio.Event()
        # Set by ``request_stop`` to prevent ``_try_reconnect`` from
        # re-establishing the connection once a shutdown has been
        # requested.  Distinct from ``_stop_event`` (which terminates
        # the listen loop) so a caller can hold the listen loop alive
        # for a brief "linger" phase while guaranteeing the
        # connection will not be revived if the listen loop hits an
        # error path during that window.  Once set, every entry to
        # ``_try_reconnect`` short-circuits before scheduling any
        # network I/O — fixing the leak where a CLOSED/stale event
        # during the linger triggered a fresh connection that
        # outlived the stop sequence.
        self._no_reconnect = False
        self._last_useful_data: float = asyncio.get_event_loop().time()
        self._last_accepted: dict[str, Any] | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def is_active(self) -> bool:
        """Return True if connected or actively reconnecting."""
        return self._listen_task is not None and not self._listen_task.done()

    async def async_connect(self) -> None:
        """Connect to the WebSocket and start listening."""
        if self._connected:
            return
        self._stop_event.clear()
        self._no_reconnect = False
        token = await self._web_session.async_ensure_token()
        await self._do_connect(token)
        self._listen_task = asyncio.ensure_future(self._listen_loop())

    def request_stop(self) -> None:
        """Disable reconnect for this instance.

        Sets the ``_no_reconnect`` flag so ``_try_reconnect`` exits
        before scheduling any reconnect attempt.  Intended to be
        called by the stop coordinator (``_stop_realtime_ws``) before
        the optional linger phase: once requested, no error path
        inside the listen loop can resurrect the connection while
        the linger is waiting for a final data frame.

        Idempotent and synchronous — safe to call from any context.
        Does NOT terminate the listen loop or close the WS; use
        ``async_disconnect`` for full teardown.
        """
        self._no_reconnect = True

    async def _do_connect(self, token: str) -> None:
        """Establish the WebSocket connection."""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        encoded_token = quote(token, safe="")
        url = (
            f"{self.WS_URL}?plantId={self._plant_id}"
            f"&token={encoded_token}&platform=web&lang=en"
        )
        self._ws = await self._http_session.ws_connect(
            url,
            heartbeat=20.0,
            timeout=aiohttp.ClientWSTimeout(ws_close=30.0),
        )
        await self._ws.send_str("getdata")
        self._last_accepted = None
        self._connected = True
        self._last_useful_data = asyncio.get_event_loop().time()
        _LOGGER.info("FoxESS WebSocket connected (plant=%s)", self._plant_id)

    async def _listen_loop(self) -> None:
        """Receive messages, reconnect on failure."""
        while not self._stop_event.is_set():
            try:
                msg = await asyncio.wait_for(
                    self._ws.receive(),  # type: ignore[union-attr]
                    timeout=self.STALE_TIMEOUT,
                )
            except TimeoutError:
                _LOGGER.warning(
                    "FoxESS WebSocket stale (no data in %.0fs)",
                    self.STALE_TIMEOUT,
                )
                await self._try_reconnect()
                if not self._connected:
                    break
                continue
            except Exception:
                _LOGGER.debug("FoxESS WebSocket receive error", exc_info=True)
                await self._try_reconnect()
                if not self._connected:
                    break
                continue

            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                _LOGGER.info("FoxESS WebSocket closed by server")
                await self._try_reconnect()
                if not self._connected:
                    break
                continue

            if msg.type == aiohttp.WSMsgType.ERROR:
                _LOGGER.warning("FoxESS WebSocket error: %s", msg.data)
                await self._try_reconnect()
                if not self._connected:
                    break
                continue

            if msg.type != aiohttp.WSMsgType.TEXT:
                continue

            try:
                data = msg.json()
            except ValueError:
                _LOGGER.debug("FoxESS WebSocket: ignoring non-JSON message")
                continue

            if data.get("errno", 0) != 0:
                _LOGGER.debug("FoxESS WebSocket error message: %s", data.get("msg"))
                continue

            # Skip stale messages — timeDiff is seconds since the
            # inverter last reported.  The first message after connect is
            # typically 30-200+ seconds old; fresh updates have timeDiff ≈ 5.
            time_diff = data.get("result", {}).get("timeDiff")
            if isinstance(time_diff, int | float) and time_diff > self.MAX_TIME_DIFF:
                _LOGGER.debug(
                    "FoxESS WebSocket: skipping stale message (timeDiff=%s)",
                    time_diff,
                )
                # Check if we've gone too long without useful data.
                # Stale keepalive frames from the cloud (e.g. after
                # another client steals the stream) reset receive()
                # but carry no useful data.
                now = asyncio.get_event_loop().time()
                if now - self._last_useful_data > self.STALE_TIMEOUT:
                    _LOGGER.warning(
                        "FoxESS WebSocket: no useful data in %.0fs "
                        "(only stale frames), reconnecting",
                        now - self._last_useful_data,
                    )
                    await self._try_reconnect()
                    if not self._connected:
                        break
                continue

            mapped = map_ws_to_coordinator(data)
            if mapped:
                if not _is_plausible(mapped, self._last_accepted):
                    continue
                self._last_accepted = mapped
                self._last_useful_data = asyncio.get_event_loop().time()
                try:
                    await self._on_data(mapped)
                except Exception:
                    _LOGGER.debug("Error in WebSocket data callback", exc_info=True)

        # Loop exited — signal disconnection
        self._connected = False
        self._on_disconnect()

    def _reconnect_allowed(self) -> bool:
        """Return True if the reconnect loop may (re)establish the WS.

        Single reconciliation point for the reconnect decision.  Three
        ways the answer can be "no":

        * ``_no_reconnect`` — ``request_stop``/``async_disconnect`` have
          requested teardown (the 2026-05-31 linger-race guard).
        * ``_stop_event`` — the listen loop is shutting down.
        * ``should_reconnect()`` returns False — the brand-layer gate
          (``_should_start_realtime_ws``) says the WS should be DOWN
          right now (no active session).  Before this, ``_try_reconnect``
          was fully autonomous and never consulted the gate, so during
          confirmed idle a connect→stale(30s)→disconnect→reconnect(6s)
          cycle self-perpetuated below the start-gate's visibility — the
          ``data_freshness`` ws↔api sawtooth (C-020 leak, live
          2026-06-07).  A ``None`` predicate keeps the legacy
          unconditional-reconnect behaviour (``always`` mode).  The
          legitimate case is preserved: during an ACTIVE session the gate
          returns True, so a stale/dropped WS still reconnects
          (D-008/D-009).
        """
        if self._no_reconnect or self._stop_event.is_set():
            return False
        if self.should_reconnect is not None and not self.should_reconnect():
            _LOGGER.info(
                "FoxESS WebSocket: gate says WS should be down "
                "(no active session) — not reconnecting"
            )
            return False
        return True

    async def _try_reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff.

        Short-circuits immediately if reconnect is not allowed
        (``_reconnect_allowed`` — shutdown requested, listen loop
        stopping, or the brand-layer gate says the WS should be down).
        This consolidates three guards: the 2026-05-31 linger-race guard
        (``_no_reconnect`` set by ``request_stop`` so a CLOSED/stale frame
        during ``_stop_realtime_ws``'s linger cannot resurrect the
        connection), the ``_stop_event`` shutdown guard, and the
        2026-06-07 idle-leak guard (the gate predicate — the reconnect
        loop now respects the same answer the start chokepoint does).
        """
        if not self._reconnect_allowed():
            self._connected = False
            return
        self._connected = False
        # Signal that WS data is no longer flowing so the coordinator
        # can update data_source immediately (e.g. badge shows "API").
        self._on_disconnect()
        await self._close_ws()

        for attempt in range(self.RECONNECT_MAX_ATTEMPTS):
            if not self._reconnect_allowed():
                return

            delay = min(
                self.RECONNECT_BASE_DELAY * (2**attempt) + random.uniform(0, 3),
                self.RECONNECT_MAX_DELAY,
            )
            _LOGGER.info(
                "FoxESS WebSocket reconnecting in %.1fs (attempt %d/%d)",
                delay,
                attempt + 1,
                self.RECONNECT_MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)

            if not self._reconnect_allowed():
                return

            try:
                token = await self._web_session.async_ensure_token()
                await self._do_connect(token)
                return  # success
            except Exception:
                _LOGGER.debug("FoxESS WebSocket reconnect failed", exc_info=True)
                # Invalidate the cached token — the cloud may have
                # revoked it (e.g. another client logged in).  The
                # next attempt will do a fresh login.
                self._web_session._token = None  # noqa: SLF001

        _LOGGER.warning("FoxESS WebSocket: max reconnect attempts reached, giving up")

    async def _close_ws(self) -> None:
        """Close the WebSocket connection if open."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def async_disconnect(self) -> None:
        """Cleanly disconnect and stop the listen loop."""
        # Set both flags BEFORE any await so that any concurrently
        # running ``_try_reconnect`` exits at its next checkpoint
        # without scheduling further network I/O.  Setting
        # ``_no_reconnect`` here in addition to ``_stop_event`` is
        # defensive: a caller that uses ``async_disconnect`` directly
        # (without going through ``_stop_realtime_ws``'s linger
        # phase) gets the same guarantee.
        self._no_reconnect = True
        self._stop_event.set()
        self._connected = False
        await self._close_ws()
        if self._listen_task is not None and not self._listen_task.done():
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._listen_task
            self._listen_task = None
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
        _LOGGER.info("FoxESS WebSocket disconnected")
