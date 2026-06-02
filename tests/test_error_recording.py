"""Tests for structured operational-error recording."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import pytest

from smart_battery.domain_data import SmartBatteryDomainData
from smart_battery.logging import record_operational_error


def test_domain_data_has_bounded_recent_errors_buffer() -> None:
    dd = SmartBatteryDomainData()
    assert isinstance(dd.recent_errors, deque)
    assert dd.recent_errors.maxlen == 30
    dd2 = SmartBatteryDomainData()
    dd.recent_errors.append({"x": 1})
    assert len(dd2.recent_errors) == 0  # no shared mutable default


def test_records_self_sufficient_log_line(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.roe.line")
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    with caplog.at_level(logging.WARNING, logger="test.roe.line"):
        record_operational_error(
            logger,
            buf,
            category="ws_discovery",
            attempted="battery ID discovery via wsmaitian WS",
            exc=ValueError("200, message='Invalid response status'"),
            hint=(
                "server returned HTTP 200 not 101 — possible regional endpoint mismatch"
            ),
        )
    text = caplog.text
    assert "ws_discovery" in text
    assert "battery ID discovery via wsmaitian WS" in text
    assert "ValueError" in text
    assert "Invalid response status" in text
    assert "regional endpoint mismatch" in text


def test_appends_structured_record() -> None:
    logger = logging.getLogger("test.roe.buf")
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    record_operational_error(
        logger,
        buf,
        category="ws_discovery",
        attempted="discover battery id",
        exc=ValueError("boom"),
        hint="check region",
        context={"host": "www.foxesscloud.com", "plant_id": "abc"},
        severity="warning",
    )
    assert len(buf) == 1
    rec = buf[0]
    assert rec["category"] == "ws_discovery"
    assert rec["attempted"] == "discover battery id"
    assert rec["exc_type"] == "ValueError"
    assert rec["exc_str"] == "boom"
    assert rec["hint"] == "check region"
    assert rec["context"] == {"host": "www.foxesscloud.com", "plant_id": "abc"}
    assert rec["severity"] == "warning"
    assert isinstance(rec["t"], str) and rec["t"]


def test_respects_buffer_maxlen() -> None:
    logger = logging.getLogger("test.roe.cap")
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    for i in range(35):
        record_operational_error(
            logger, buf, category="c", attempted=f"a{i}", exc=ValueError(str(i))
        )
    assert len(buf) == 30
    assert buf[-1]["exc_str"] == "34"
    assert buf[0]["exc_str"] == "5"


def test_buffer_none_logs_without_crashing(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.roe.none")
    with caplog.at_level(logging.ERROR, logger="test.roe.none"):
        record_operational_error(
            logger,
            None,
            category="unexpected",
            attempted="x",
            exc=RuntimeError("y"),
            severity="error",
        )
    assert "RuntimeError" in caplog.text


def test_severity_maps_to_log_level(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.roe.sev")
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    with caplog.at_level(logging.DEBUG, logger="test.roe.sev"):
        record_operational_error(
            logger,
            buf,
            category="c",
            attempted="a",
            exc=ValueError("z"),
            severity="error",
        )
    assert caplog.records[-1].levelno == logging.ERROR


class _FakeWSConnectSession:
    """Minimal aiohttp.ClientSession stand-in whose ws_connect raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.closed = False

    def ws_connect(self, *args: Any, **kwargs: Any) -> Any:
        raise self._exc


@pytest.mark.asyncio
async def test_battery_id_discovery_records_ws_handshake_error() -> None:
    """A WSServerHandshakeError during discovery records a typed,
    region-hinted error and still returns None (issue #8)."""
    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    from custom_components.foxess_control.foxess.web_session import (
        FoxESSWebSession,
    )

    request_info = aiohttp.RequestInfo(
        URL("wss://eu.foxesscloud.com/dew/v0/wsmaitian"),
        "GET",
        CIMultiDictProxy(CIMultiDict()),
    )
    exc = aiohttp.WSServerHandshakeError(
        request_info=request_info,
        history=(),
        status=200,
        message="Invalid response status",
    )

    buf: deque[dict[str, Any]] = deque(maxlen=30)
    session = _FakeWSConnectSession(exc)
    ws = FoxESSWebSession(
        "testuser",
        "d41d8cd98f00b204e9800998ecf8427e",
        base_url="https://eu.foxesscloud.com",
        session=session,  # type: ignore[arg-type]
    )
    # Avoid a real login; discovery only needs a token string.
    ws._token = "tok"  # noqa: SLF001
    ws._last_login = float("inf")  # noqa: SLF001

    result = await ws.async_discover_battery_id("PLANT123", recent_errors=buf)

    assert result is None
    assert len(buf) == 1
    rec = buf[0]
    assert rec["category"] == "ws_discovery"
    assert rec["exc_type"] == "WSServerHandshakeError"
    assert "regional" in (rec["hint"] or "")
    assert rec["context"]["host"] == "https://eu.foxesscloud.com"
    assert rec["context"]["plant_id"] == "PLANT123"


class _FakeGetCtx:
    """Async-context-manager returned by a fake session's .get()."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> Any:
        raise self._exc

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeGetSession:
    """Minimal aiohttp.ClientSession stand-in whose GET raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.closed = False

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return _FakeGetCtx(self._exc)


def _request_info(url: str) -> Any:
    import aiohttp
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    return aiohttp.RequestInfo(URL(url), "GET", CIMultiDictProxy(CIMultiDict()))


@pytest.mark.asyncio
async def test_bms_temp_fetch_records_typed_error_on_client_error() -> None:
    """An aiohttp client error during BMS-temp fetch records a typed,
    bms_temp_fetch error with host context and a hint, and still
    returns None (preserving the existing fallback contract)."""
    import aiohttp

    from custom_components.foxess_control.foxess.web_session import (
        FoxESSWebSession,
    )

    exc = aiohttp.ClientConnectionError("cannot connect to host")

    buf: deque[dict[str, Any]] = deque(maxlen=30)
    session = _FakeGetSession(exc)
    ws = FoxESSWebSession(
        "testuser",
        "d41d8cd98f00b204e9800998ecf8427e",
        base_url="https://eu.foxesscloud.com",
        session=session,  # type: ignore[arg-type]
    )
    ws._token = "tok"  # noqa: SLF001
    ws._last_login = float("inf")  # noqa: SLF001

    result = await ws.async_get_battery_temperature(
        battery_compound_id="BID@SERIAL123",
        recent_errors=buf,
    )

    assert result is None
    assert len(buf) == 1
    rec = buf[0]
    assert rec["category"] == "bms_temp_fetch"
    assert rec["exc_type"] == "ClientConnectionError"
    assert rec["hint"]
    assert rec["context"]["host"] == "https://eu.foxesscloud.com"
    # The raw compound id embeds a serial — must NOT be recorded verbatim.
    assert "SERIAL123" not in str(rec["context"])


# ---------------------------------------------------------------------------
# Entity-mode write failures (foxess_adapter) record typed errors
# ---------------------------------------------------------------------------


def _entity_hass(
    entry_options: dict[str, Any],
    *,
    raise_exc: BaseException,
    entity_states: dict[str, Any] | None = None,
) -> Any:
    """Mock hass whose services.async_call raises *raise_exc*, with
    FoxESSControlData domain data (so recent_errors is reachable)."""
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.foxess_control.const import DOMAIN
    from custom_components.foxess_control.domain_data import (
        FoxESSControlData,
        FoxESSEntryData,
        build_config,
    )

    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=raise_exc)
    states_map = entity_states or {}
    hass.states.get = MagicMock(side_effect=lambda eid: states_map.get(eid))
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData()
    dd.config = build_config(entry_options)
    hass.data = {DOMAIN: dd}
    return hass, dd


def _number_state(unit: str = "W", max_val: float = 15000) -> Any:
    from unittest.mock import MagicMock

    st = MagicMock()
    st.state = "0"
    st.attributes = {"unit_of_measurement": unit, "min": 0, "max": max_val}
    return st


@pytest.mark.asyncio
async def test_entity_workmode_write_failure_records_and_reraises() -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.foxess_control.const import (
        CONF_WORK_MODE_ENTITY,
    )
    from custom_components.foxess_control.foxess.inverter import WorkMode
    from custom_components.foxess_control.foxess_adapter import FoxESSEntityAdapter

    wm = "select.foxess_work_mode"
    opts = {CONF_WORK_MODE_ENTITY: wm}
    hass, dd = _entity_hass(opts, raise_exc=HomeAssistantError("service failed"))
    adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

    with pytest.raises(HomeAssistantError):
        await adapter.apply_mode(hass, WorkMode.SELF_USE)

    assert len(dd.recent_errors) == 1
    rec = dd.recent_errors[0]
    assert rec["category"] == "mode_write"
    assert rec["exc_type"] == "HomeAssistantError"
    assert rec["context"]["entity_id"] == wm


@pytest.mark.asyncio
async def test_entity_power_write_failure_records_and_reraises() -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.foxess_control.const import (
        CONF_DISCHARGE_POWER_ENTITY,
        CONF_WORK_MODE_ENTITY,
    )
    from custom_components.foxess_control.foxess.inverter import WorkMode
    from custom_components.foxess_control.foxess_adapter import FoxESSEntityAdapter

    wm = "select.foxess_work_mode"
    power = "number.foxess_discharge_power"
    opts = {CONF_WORK_MODE_ENTITY: wm, CONF_DISCHARGE_POWER_ENTITY: power}
    # First call (work-mode select) succeeds; the power set_value raises.
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.foxess_control.const import DOMAIN
    from custom_components.foxess_control.domain_data import (
        FoxESSControlData,
        FoxESSEntryData,
        build_config,
    )

    calls = {"n": 0}

    async def _async_call(domain: str, service: str, payload: Any, **kw: Any) -> None:
        calls["n"] += 1
        if service == "set_value":
            raise HomeAssistantError("power write failed")

    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=_async_call)
    hass.states.get = MagicMock(
        side_effect=lambda eid: _number_state() if eid == power else None
    )
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData()
    dd.config = build_config(opts)
    hass.data = {DOMAIN: dd}
    adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

    with pytest.raises(HomeAssistantError):
        await adapter.apply_mode(hass, WorkMode.FORCE_DISCHARGE, power_w=3000)

    assert len(dd.recent_errors) == 1
    rec = dd.recent_errors[0]
    assert rec["category"] == "power_write"
    assert rec["exc_type"] == "HomeAssistantError"
    assert rec["context"]["entity_id"] == power
    assert "value" in rec["context"]


@pytest.mark.asyncio
async def test_entity_min_soc_write_failure_records_and_reraises() -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.foxess_control.const import (
        CONF_MIN_SOC_ENTITY,
        CONF_WORK_MODE_ENTITY,
    )
    from custom_components.foxess_control.foxess.inverter import WorkMode
    from custom_components.foxess_control.foxess_adapter import FoxESSEntityAdapter

    wm = "select.foxess_work_mode"
    min_soc = "number.foxess_min_soc"
    opts = {CONF_WORK_MODE_ENTITY: wm, CONF_MIN_SOC_ENTITY: min_soc}
    # Work-mode select succeeds; min-soc set_value raises.
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.foxess_control.const import DOMAIN
    from custom_components.foxess_control.domain_data import (
        FoxESSControlData,
        FoxESSEntryData,
        build_config,
    )

    async def _async_call(domain: str, service: str, payload: Any, **kw: Any) -> None:
        if service == "set_value":
            raise HomeAssistantError("min soc write failed")

    hass = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=_async_call)
    hass.states.get = MagicMock(side_effect=lambda eid: None)
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData()
    dd.config = build_config(opts)
    hass.data = {DOMAIN: dd}
    adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

    with pytest.raises(HomeAssistantError):
        await adapter.apply_mode(hass, WorkMode.SELF_USE)

    assert len(dd.recent_errors) == 1
    rec = dd.recent_errors[0]
    assert rec["category"] == "min_soc_write"
    assert rec["context"]["entity_id"] == min_soc


@pytest.mark.asyncio
async def test_entity_export_limit_write_failure_records_and_reraises() -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.foxess_control.const import CONF_EXPORT_LIMIT_ENTITY
    from custom_components.foxess_control.foxess_adapter import FoxESSEntityAdapter

    eid = "number.foxess_export_limit"
    opts = {CONF_EXPORT_LIMIT_ENTITY: eid}
    hass, dd = _entity_hass(
        opts,
        raise_exc=HomeAssistantError("export write failed"),
        entity_states={eid: _number_state()},
    )
    adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

    with pytest.raises(HomeAssistantError):
        await adapter.set_export_limit_w(hass, 5000)

    assert len(dd.recent_errors) == 1
    rec = dd.recent_errors[0]
    assert rec["category"] == "export_limit_write"
    assert rec["context"]["entity_id"] == eid
    assert "value" in rec["context"]


@pytest.mark.asyncio
async def test_cloud_adapter_export_limit_write_failure_records_and_reraises() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.foxess_control.const import DOMAIN
    from custom_components.foxess_control.domain_data import (
        FoxESSControlData,
        FoxESSEntryData,
    )
    from custom_components.foxess_control.foxess_adapter import FoxESSCloudAdapter

    eid = "number.foxess_export_limit"
    hass = MagicMock()
    hass.services.async_call = AsyncMock(
        side_effect=HomeAssistantError("export write failed")
    )
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData()
    hass.data = {DOMAIN: dd}

    inverter = MagicMock()
    inverter.max_power_w = 15000
    import datetime as _dt

    now = _dt.datetime(2026, 6, 2, 10, 0, tzinfo=_dt.UTC)
    adapter = FoxESSCloudAdapter(
        hass=hass,
        inverter=inverter,
        min_soc_on_grid=11,
        api_min_soc=10,
        start=now,
        end=now + _dt.timedelta(hours=1),
        export_limit_entity=eid,
    )

    with pytest.raises(HomeAssistantError):
        await adapter.set_export_limit_w(hass, 5000)

    assert len(dd.recent_errors) == 1
    rec = dd.recent_errors[0]
    assert rec["category"] == "export_limit_write"
    assert rec["context"]["entity_id"] == eid
    assert rec["context"]["value"] == 5000
