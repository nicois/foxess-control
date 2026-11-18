"""Client for the foxess-local gateway (local backend).

foxess-local owns the inverter's connection on the LAN and serves a REST +
Server-Sent Events API (``/api/v1``; contract at
``<gateway>/api/v1/openapi.yaml``). This module is the integration's only
knowledge of that API:

* :func:`map_state` / :func:`map_snapshot_event` translate gateway payloads
  (signed **watts**, nested sections) into the cloud-shaped coordinator
  data every consumer already reads (directional **kW** variables, ``SoC``,
  cumulative kWh counters, ``_work_mode``, ``_data_source``).
* :class:`SSEParser` decodes the ``GET /events`` stream incrementally.
* :class:`FoxESSLocalClient` performs the HTTP calls; errors arrive as
  RFC 7807 problem documents and become :class:`LocalGatewayError` with
  the gateway's stable ``code``.

Brand-specific by construction (C-021): nothing here belongs in
``smart_battery/``.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_LOGGER = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
DATA_SOURCE_LOCAL = "local"

# Gateway schedule work modes -> the integration's WorkMode values.
# peak-shaving / power-station are valid gateway modes the integration does
# not model; they map to None (unknown) rather than a wrong guess.
_WORK_MODE_MAP: dict[str, str] = {
    "self-use": "SelfUse",
    "feed-in": "Feedin",
    "backup": "Backup",
    "force-charge": "ForceCharge",
    "force-discharge": "ForceDischarge",
}

# State.energy field -> cloud cumulative counter name (kWh). See
# docs/api/foxess-local-gateway-requirements.md §1.
_ENERGY_MAP: dict[str, str] = {
    "grid_export_kwh": "feedin",
    "grid_import_kwh": "gridConsumption",
    "pv_kwh": "PVEnergyTotal",
    "battery_charge_kwh": "chargeEnergyToTal",
    "battery_discharge_kwh": "dischargeEnergyToTal",
    "load_kwh": "loads",
    "output_kwh": "generation",
}

# (section, field) -> coordinator variable, copied only when not null.
_OPTIONAL_VALUES: tuple[tuple[str, str, str], ...] = (
    ("battery", "pack_voltage_v", "batVolt"),
    ("temperatures", "battery_c", "batTemperature"),
    ("temperatures", "inverter_c", "invTemperation"),
    ("temperatures", "ambient_c", "ambientTemperation"),
    # The coldest cell is the FoxESS portal's "BMS battery temperature".
    ("temperatures", "cell_min_c", "bmsBatteryTemperature"),
)


class LocalGatewayError(Exception):
    """A gateway request failed.

    ``code`` is the problem document's stable machine-readable code
    (``insufficient_role``, ``inverter_offline``, ``write_unverified``, …),
    or ``unreachable`` for transport failures; ``None`` when the gateway
    answered with something that is not a problem document.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        problem: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.problem: dict[str, Any] = problem or {}


def problem_to_error(status: int, body: Any) -> LocalGatewayError:
    """Build a :class:`LocalGatewayError` from an HTTP status and body."""
    if not isinstance(body, dict):
        return LocalGatewayError(f"Gateway returned HTTP {status}", status=status)
    code = body.get("code")
    title = body.get("title") or f"HTTP {status}"
    detail = body.get("detail")
    message = f"{title} ({code}): {detail}" if detail else f"{title} ({code})"
    return LocalGatewayError(
        message,
        status=status,
        code=code if isinstance(code, str) else None,
        problem=body,
    )


def _to_utc_iso(value: Any) -> str | None:
    """Normalise a gateway RFC 3339 timestamp to a UTC ISO string."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(datetime.UTC).isoformat()


def _kw(watts: Any) -> float:
    try:
        return float(watts) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _map_power(power: dict[str, Any]) -> dict[str, float]:
    """Signed watts -> the cloud's directional kW variables."""
    grid = _kw(power.get("grid_w"))  # + import, - export
    battery = _kw(power.get("battery_w"))  # + charging, - discharging
    ac_port = _kw(power.get("ac_port_w"))  # + output, - input
    return {
        "pvPower": max(_kw(power.get("pv_w")), 0.0),
        "loadsPower": max(_kw(power.get("load_w")), 0.0),
        "gridConsumptionPower": max(grid, 0.0),
        "feedinPower": max(-grid, 0.0),
        "batChargePower": max(battery, 0.0),
        "batDischargePower": max(-battery, 0.0),
        "generationPower": max(ac_port, 0.0),
        "epsPower": max(_kw(power.get("eps_w")), 0.0),
    }


def _map_energy(energy: Any) -> dict[str, float]:
    """Lifetime counters (kWh) -> cloud counter names; nulls omitted."""
    if not isinstance(energy, dict):
        return {}
    return {
        var: float(energy[field])
        for field, var in _ENERGY_MAP.items()
        if energy.get(field) is not None
    }


def _freshness(as_of: Any, online: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "_data_source": DATA_SOURCE_LOCAL,
        "_gateway_online": online,
    }
    stamp = _to_utc_iso(as_of)
    if stamp is not None:
        data["_data_last_update"] = stamp
    return data


def map_state(state: dict[str, Any]) -> dict[str, Any]:
    """Translate a ``GET /state`` body into coordinator data."""
    data: dict[str, Any] = {}
    data.update(_map_power(state.get("power") or {}))

    battery = state.get("battery") or {}
    if battery.get("soc_pct") is not None:
        data["SoC"] = battery["soc_pct"]

    for section, field, var in _OPTIONAL_VALUES:
        value = (state.get(section) or {}).get(field)
        if value is not None:
            data[var] = float(value)

    data.update(_map_energy(state.get("energy")))

    mode = (state.get("active_mode") or {}).get("mode")
    data["_work_mode"] = _WORK_MODE_MAP.get(mode) if isinstance(mode, str) else None

    data.update(_freshness(state.get("as_of"), bool(state.get("online"))))
    return data


def map_snapshot_event(event: dict[str, Any]) -> dict[str, Any]:
    """Translate an SSE ``snapshot`` payload (power, SoC, counters).

    Only the keys a snapshot actually carries are returned, so merging it
    over the last full ``/state`` never clobbers temperatures, counters or
    the work mode. A snapshot is evidence the inverter is live.
    """
    data: dict[str, Any] = dict(_map_power(event.get("power") or {}))
    if event.get("soc_pct") is not None:
        data["SoC"] = event["soc_pct"]
    data.update(_map_energy(event.get("energy")))
    data.update(_freshness(event.get("ts"), True))
    return data


class SSEParser:
    """Incremental ``text/event-stream`` decoder.

    :meth:`feed` accepts arbitrary chunks and returns the complete events
    they finish, as ``(id, event, data)`` with ``data`` JSON-decoded.
    Events whose data is not JSON are logged and skipped.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._id: int | None = None
        self._event: str | None = None
        self._data: list[str] = []
        self.last_event_id: int | None = None

    def feed(self, chunk: str) -> list[tuple[int | None, str, Any]]:
        self._buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
        events: list[tuple[int | None, str, Any]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line == "":
                event = self._dispatch()
                if event is not None:
                    events.append(event)
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "id":
                try:
                    self._id = int(value)
                except ValueError:
                    self._id = None
            elif field == "event":
                self._event = value
            elif field == "data":
                self._data.append(value)
        return events

    def _dispatch(self) -> tuple[int | None, str, Any] | None:
        event_id, name, lines = self._id, self._event, self._data
        self._id, self._event, self._data = None, None, []
        if event_id is not None:
            self.last_event_id = event_id
        if not lines:
            return None
        try:
            payload = json.loads("\n".join(lines))
        except ValueError:
            _LOGGER.warning("Gateway sent an undecodable %s event; skipped", name)
            return None
        return (event_id, name or "message", payload)


class FoxESSLocalClient:
    """Async HTTP client for one foxess-local gateway."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str | None = None,
        *,
        timeout_s: float = 15.0,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        if not self._base.endswith(API_PREFIX):
            self._base += API_PREFIX
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    @property
    def base_url(self) -> str:
        return self._base

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method, url, headers=self._headers, timeout=self._timeout, **kwargs
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = None
                if resp.status >= 400:
                    raise problem_to_error(resp.status, body)
                return body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise LocalGatewayError(
                f"Cannot reach the gateway at {self._base}: {err}",
                code="unreachable",
            ) from err

    async def get_state(self) -> dict[str, Any]:
        body = await self._request("GET", "/state")
        if not isinstance(body, dict):
            raise LocalGatewayError("Gateway /state did not return an object")
        return body

    async def get_config(self) -> dict[str, Any]:
        body = await self._request("GET", "/config")
        return body if isinstance(body, dict) else {}

    async def get_compatibility(self) -> dict[str, Any]:
        body = await self._request("GET", "/compatibility")
        return body if isinstance(body, dict) else {}

    async def iter_events(
        self, last_event_id: int | None = None
    ) -> AsyncGenerator[tuple[int | None, str, Any]]:
        """Yield SSE events until the stream ends or fails.

        The caller owns reconnection; :class:`LocalGatewayError` is raised
        for HTTP errors and transport failures alike.
        """
        headers = dict(self._headers)
        headers["Accept"] = "text/event-stream"
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        # No total timeout on a stream; a silent stream is detected by the
        # caller from the 5 s snapshot heartbeat.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
        parser = SSEParser()
        try:
            async with self._session.get(
                f"{self._base}/events", headers=headers, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    try:
                        body = await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        body = None
                    raise problem_to_error(resp.status, body)
                async for chunk in resp.content.iter_any():
                    for event in parser.feed(chunk.decode("utf-8", "replace")):
                        yield event
        except (aiohttp.ClientError, TimeoutError) as err:
            raise LocalGatewayError(
                f"Gateway event stream failed: {err}", code="unreachable"
            ) from err
