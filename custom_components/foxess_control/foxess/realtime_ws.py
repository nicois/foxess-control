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


def _parse_power(power_obj: dict[str, Any] | None) -> float | None:
    """Extract numeric watts from a WebSocket power object.

    The WebSocket reports power values in **watts** (as strings),
    despite the ``unit`` field sometimes reading ``"W"`` or being
    absent.  Returns the raw watt value; callers convert to kW.
    """
    if power_obj is None:
        return None
    val = power_obj.get("value")
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _detect_kw_unit(raw_values: list[float]) -> bool:
    """Detect whether raw WS power values are in kW instead of watts.

    The WS normally sends watts (e.g. 5290) but sometimes sends kW
    (e.g. 5.29).  If ALL non-zero raw values are < 50, they are almost
    certainly already in kW — a real household with any load running
    will have at least one value > 100W.  50kW is well above any
    residential inverter, so values >= 50 are always watts.
    """
    nonzero = [v for v in raw_values if v > 0]
    if not nonzero:
        return False
    return max(nonzero) < 50


def map_ws_to_coordinator(ws_msg: dict[str, Any]) -> dict[str, Any]:
    """Map a WebSocket message to coordinator variable names.

    The WebSocket normally sends power values in **watts** (as strings).
    The coordinator (and REST API) use **kW**, so we divide by 1000.
    However, the cloud API sometimes sends values already in kW.
    We detect this by checking whether all raw values are < 50 and
    skip the /1000 conversion when they are.
    Only populates fields that are present in the message.
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

    # Collect all raw power values to detect kW-vs-watts unit.
    bat_raw = _parse_power(bat.get("power"))
    solar_raw = _parse_power(node.get("solar", {}).get("power"))
    load_raw = _parse_power(node.get("load", {}).get("power"))
    grid = node.get("grid", {})
    grid_raw = _parse_power(grid.get("power"))

    raw_values = [v for v in (bat_raw, solar_raw, load_raw, grid_raw) if v is not None]
    already_kw = _detect_kw_unit(raw_values)
    divisor = 1.0 if already_kw else 1000.0

    if already_kw:
        _LOGGER.warning(
            "WS power values appear to be in kW (max raw=%.2f < 50), "
            "skipping /1000 conversion. raw: bat=%s solar=%s load=%s grid=%s",
            max(raw_values) if raw_values else 0,
            bat_raw,
            solar_raw,
            load_raw,
            grid_raw,
        )

    # Battery power — direction indicated by bat.charge (1=charging)
    if bat_raw is not None:
        bat_kw = bat_raw / divisor
        is_charging = str(bat.get("charge")) == "1"
        data["batChargePower"] = bat_kw if is_charging else 0.0
        data["batDischargePower"] = bat_kw if not is_charging else 0.0

    # Solar power
    if solar_raw is not None:
        data["pvPower"] = solar_raw / divisor

    # House load
    if load_raw is not None:
        data["loadsPower"] = load_raw / divisor

    # Grid power — derive direction from the power balance rather than
    # the unreliable gridStatus field (whose meaning varies by firmware).
    # grid = load + bat_charge - bat_discharge - solar
    # Positive → importing from grid; negative → exporting to grid.
    if grid_raw is not None:
        grid_kw = grid_raw / divisor
        # Use power balance when all components are available (already kW).
        solar = data.get("pvPower")
        load = data.get("loadsPower")
        bat_charge = data.get("batChargePower", 0.0)
        bat_discharge = data.get("batDischargePower", 0.0)

        if solar is not None and load is not None:
            net = load + bat_charge - bat_discharge - solar
            importing = net > 0
        else:
            # Fall back to gridStatus (best effort)
            importing = str(grid.get("gridStatus", "")) == "3"

        if importing:
            data["gridConsumptionPower"] = grid_kw
            data["feedinPower"] = 0.0
        else:
            data["gridConsumptionPower"] = 0.0
            data["feedinPower"] = grid_kw

    if data:
        _LOGGER.debug(
            "WS mapped data: %s (gridStatus=%s)",
            data,
            grid.get("gridStatus") if grid else None,
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
    ) -> None:
        self._plant_id = plant_id
        self._web_session = web_session
        self._on_data = on_data
        self._on_disconnect = on_disconnect
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._connected = False
        self._stop_event = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def async_connect(self) -> None:
        """Connect to the WebSocket and start listening."""
        if self._connected:
            return
        self._stop_event.clear()
        token = await self._web_session.async_ensure_token()
        await self._do_connect(token)
        self._listen_task = asyncio.ensure_future(self._listen_loop())

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
        self._connected = True
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
                continue

            mapped = map_ws_to_coordinator(data)
            if mapped:
                try:
                    await self._on_data(mapped)
                except Exception:
                    _LOGGER.debug("Error in WebSocket data callback", exc_info=True)

        # Loop exited — signal disconnection
        self._connected = False
        self._on_disconnect()

    async def _try_reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        self._connected = False
        await self._close_ws()

        for attempt in range(self.RECONNECT_MAX_ATTEMPTS):
            if self._stop_event.is_set():
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

            if self._stop_event.is_set():
                return

            try:
                token = await self._web_session.async_ensure_token()
                await self._do_connect(token)
                return  # success
            except Exception:
                _LOGGER.debug("FoxESS WebSocket reconnect failed", exc_info=True)

        _LOGGER.warning("FoxESS WebSocket: max reconnect attempts reached, giving up")

    async def _close_ws(self) -> None:
        """Close the WebSocket connection if open."""
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def async_disconnect(self) -> None:
        """Cleanly disconnect and stop the listen loop."""
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
