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

    A pure, silent predicate: it answers "does *candidate* sit in the same
    power regime as *reference*?" and nothing more.  It does NOT decide
    whether to drop a frame — that decision belongs to
    :meth:`FoxESSRealtimeWS._process_mapped_frame`, which uses this
    predicate twice (against the last accepted frame AND against a pending
    divergent frame) to tell a *sustained* legitimate transition apart from
    a *transient* single-frame glitch.

    Edge cases preserved from the original coordinator-level filter:
    - *reference* is ``None`` or missing the key → True (first message).
    - Reference value <= 0.1 kW → True (ramp-up from near-zero).
    - Candidate value == 0 → True (genuine stop).
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
            return False
    return True


def _parse_aux_power(node: dict[str, Any]) -> float | None:
    """Read the ``aux`` node's power in kW, or ``None`` if unreadable.

    ``aux`` is the wsmaitian counterpart of the REST ``meterPower2``
    variable (the native app's "Gen Load"): generation from a second,
    AC-coupled inverter measured on an auxiliary meter channel.  It
    never appears in the FoxESS ``solar`` reading because it is not
    wired into the FoxESS PV strings.

    Every other observed node nests its reading under ``power``
    (``solar``, ``load``, ``grid``, ``bat``, ``device``), so that is the
    primary shape.  A bare power object is also accepted rather than
    silently dropping the term, since the node is absent from every
    frame we have captured (DC-coupled plants do not emit it) and its
    exact shape is therefore attested only by user report.

    Units follow C-004 via the shared helpers: watts unless the power
    object's ``unit`` field says ``kW``.
    """
    aux = node.get("aux")
    if not isinstance(aux, dict):
        return None
    nested = aux.get("power")
    if isinstance(nested, dict):
        return _to_kw(_parse_power(nested))
    return _to_kw(_parse_power(aux))


def map_ws_to_coordinator(
    ws_msg: dict[str, Any],
    *,
    additional_pv_enabled: bool = False,
    additional_pv_fallback_kw: float = 0.0,
) -> dict[str, Any]:
    """Map a WebSocket message to coordinator variable names.

    The WebSocket normally sends power values in **watts** (as strings)
    but the cloud API sometimes sends individual fields in **kW**.
    Each power object's ``unit`` field is checked: ``"kW"`` means the
    value is already in kW; anything else (``"W"``, absent) means watts
    and is divided by 1000.  This handles mixed units within a single
    message.

    ``additional_pv_enabled`` opts in to the AC-coupled generation term
    (the ``aux`` node) for users who configured
    ``additional_pv_power_variable``.  It must stay **off** by default:
    on a DC-coupled system ``aux`` may carry something else entirely, so
    an unconfigured user's mapped output has to be byte-identical
    whether or not the frame has an ``aux`` node.

    When enabled the term is folded into ``pvPower`` **before** the
    grid-direction balance below, which is the whole point: the balance
    is what decides import vs export (C-006), and on an AC-coupled site
    it cannot get the sign right while it believes generation is zero
    (issue #18).  ``additional_pv_fallback_kw`` is the coordinator's
    last REST-polled value, used when a frame carries no readable
    ``aux`` — keeping a stale term beats dropping it, because dropping
    it puts the balance straight back into the failure mode.

    The applied term is reported back as ``_additional_pv_kw`` so the
    coordinator knows not to add its own copy on top.
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

    # AC-coupled generation (opt-in).  Prefer the live ``aux`` reading;
    # fall back to the coordinator's last REST-polled value.  A live 0 is
    # authoritative and must win over a stale non-zero (sunset case), so
    # the marker is set whenever the term is applied at all — including
    # when it is zero.
    if additional_pv_enabled and "pvPower" in data:
        aux_kw = _parse_aux_power(node)
        extra_kw = aux_kw if aux_kw is not None else additional_pv_fallback_kw
        data["pvPower"] = data["pvPower"] + extra_kw
        data["_additional_pv_kw"] = extra_kw

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
        additional_pv: Callable[[], tuple[bool, float]] | None = None,
    ) -> None:
        self._plant_id = plant_id
        self._web_session = web_session
        self._on_data = on_data
        self._on_disconnect = on_disconnect
        # AC-coupled generation opt-in, resolved per frame so a config
        # change and each fresh REST poll take effect immediately.
        # Returns ``(enabled, rest_fallback_kw)``.  Wired by the brand
        # layer to ``additional_pv_power_variable`` plus the
        # coordinator's last polled value; ``None`` means "not
        # configured", which is also what a raising provider degrades to
        # (the config-flow probe constructs this client without one).
        self._additional_pv = additional_pv
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
        # A divergent frame that has NOT yet been corroborated.  When a
        # frame diverges >10x from ``_last_accepted`` we cannot yet tell a
        # genuine regime change (idle→charge, charge→idle, discharge start)
        # from a one-off garbage reading (the 2026-04-27 incident: frames
        # flashing to ~20% of true value).  We hold the first divergent
        # frame here and drop it; if the NEXT frame agrees with it (same
        # new regime), the transition is *sustained* and we accept it.  A
        # lone divergent frame followed by a return to the old regime is a
        # *transient* glitch and stays dropped.  This bounds the blackhole
        # to a single frame instead of "until a REST poll re-anchors the
        # reference" (live 2026-06-15 charge-start data loss).
        self._pending: dict[str, Any] | None = None

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
        self._pending = None
        self._connected = True
        self._last_useful_data = asyncio.get_event_loop().time()
        _LOGGER.info("FoxESS WebSocket connected (plant=%s)", self._plant_id)

    def _process_mapped_frame(self, mapped: dict[str, Any]) -> dict[str, Any] | None:
        """Decide whether a mapped WS frame should be injected.

        Returns the frame to forward to the coordinator, or ``None`` if it
        should be dropped.  Owns the ``_last_accepted`` / ``_pending``
        corroboration state — the single place the plausibility filter
        makes its accept/drop decision (the listen loop's ~line-401 call
        site).

        The filter must catch genuine single-frame anomalies (a transient
        garbage reading — the 2026-04-27 incident) WITHOUT rejecting a
        *sustained* legitimate transition (idle→charge, charge→idle,
        discharge start).  A real transition is corroborated across
        consecutive frames; a glitch is not.  The single-frame >10x guard
        (``_is_plausible``) alone cannot tell them apart, so it dropped
        every frame of a charge start until a REST poll re-anchored the
        reference (live 2026-06-15: idle batChargePower≈0.359 kW → ≈10 kW
        was a 27x jump, above the 0.1 kW near-zero exemption).

        Algorithm (confirm-before-drop / accept-on-repeat):

        * In-regime (plausible vs ``_last_accepted``) → accept, clear any
          pending divergent frame.
        * Divergent, and a pending frame exists that the candidate AGREES
          with (same new regime) → the new regime is *sustained*: accept
          and re-anchor.  Only the very first frame of the transition was
          dropped.
        * Divergent with no corroboration → hold as pending and drop.  If
          the next frame returns to the old regime, the pending frame was a
          lone glitch and stays dropped (cleared on the next in-regime
          accept).

        Preserves the original edge cases via ``_is_plausible``: no
        reference / missing key / near-zero reference / candidate==0 are
        all "plausible" and accepted on the first branch.  C-004 (the
        watts→kW mapping) and C-005 (stale-frame discard) are handled
        upstream and untouched.
        """
        if _is_plausible(mapped, self._last_accepted):
            self._last_accepted = mapped
            self._pending = None
            return mapped

        # Divergent from the last accepted regime.  Corroborated by the
        # pending frame?  (Both diverge from the stale reference but agree
        # with EACH OTHER → a sustained new regime, not a one-off.)
        if self._pending is not None and _is_plausible(mapped, self._pending):
            _LOGGER.info(
                "WS divergent regime corroborated by consecutive frames "
                "— accepting transition (last_accepted=%s, now=%s)",
                self._last_accepted,
                mapped,
            )
            self._last_accepted = mapped
            self._pending = None
            return mapped

        # First (or uncorroborated) divergent frame — hold and drop.  A
        # genuine transition is confirmed by the next frame; a transient
        # glitch is discarded when the stream returns to the old regime.
        _LOGGER.warning(
            "WS frame diverges >10x from last accepted (%s vs %s) "
            "— holding for corroboration, dropping this frame",
            mapped,
            self._last_accepted,
        )
        self._pending = mapped
        return None

    def _resolve_additional_pv(self) -> tuple[bool, float]:
        """Resolve the AC-coupled opt-in for the frame about to be mapped.

        Degrades to "not configured" if no provider was supplied or the
        provider raises (e.g. domain data not ready during startup) — a
        data-enrichment lookup must never cost us the frame.
        """
        if self._additional_pv is None:
            return (False, 0.0)
        try:
            enabled, fallback_kw = self._additional_pv()
            return (bool(enabled), float(fallback_kw))
        except Exception:
            _LOGGER.debug("Additional-PV provider failed", exc_info=True)
            return (False, 0.0)

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

            enabled, fallback_kw = self._resolve_additional_pv()
            mapped = map_ws_to_coordinator(
                data,
                additional_pv_enabled=enabled,
                additional_pv_fallback_kw=fallback_kw,
            )
            if mapped:
                accepted = self._process_mapped_frame(mapped)
                if accepted is None:
                    continue
                self._last_useful_data = asyncio.get_event_loop().time()
                try:
                    await self._on_data(accepted)
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
