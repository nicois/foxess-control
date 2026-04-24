"""Typed domain data for FoxESS Control.

Subclasses :class:`SmartBatteryDomainData` with FoxESS-specific fields
(WebSocket, web session, force-op tracking, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .smart_battery.domain_data import EntryData, SmartBatteryDomainData

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from .foxess import FoxESSRealtimeWS, FoxESSWebSession


@dataclass(frozen=True)
class IntegrationConfig:
    """Snapshot of config entry options, built once at setup time.

    Replaces the scattered ``_get_X(hass)`` accessor functions that each
    independently looked up the first config entry and read an option.
    Rebuilt on options update via ``async_setup_entry`` (HA reloads the
    entry on options change).
    """

    min_soc_on_grid: int
    api_min_soc: int
    battery_capacity_kwh: float
    min_power_change: int
    max_power_w: int
    grid_export_limit_w: int
    smart_headroom: float
    bms_polling_interval: float
    ws_mode: str
    entity_mode: bool


def build_config(
    entry_options: dict[str, Any],
    inverter_max_power_w: int | None = None,
) -> IntegrationConfig:
    """Build an IntegrationConfig from a config entry's options dict."""
    from .const import (
        CONF_API_MIN_SOC,
        CONF_BATTERY_CAPACITY_KWH,
        CONF_BMS_POLLING_INTERVAL,
        CONF_GRID_EXPORT_LIMIT,
        CONF_INVERTER_POWER,
        CONF_MIN_POWER_CHANGE,
        CONF_MIN_SOC_ON_GRID,
        CONF_SMART_HEADROOM,
        CONF_WORK_MODE_ENTITY,
        CONF_WS_ALL_SESSIONS,
        CONF_WS_MODE,
        DEFAULT_API_MIN_SOC,
        DEFAULT_BMS_POLLING_INTERVAL,
        DEFAULT_GRID_EXPORT_LIMIT,
        DEFAULT_INVERTER_POWER,
        DEFAULT_MIN_POWER_CHANGE,
        DEFAULT_MIN_SOC_ON_GRID,
        DEFAULT_SMART_HEADROOM,
        WS_MODE_AUTO,
        WS_MODE_SMART_SESSIONS,
    )

    configured_power = entry_options.get(CONF_INVERTER_POWER)
    if configured_power:
        max_power_w = int(configured_power)
    elif inverter_max_power_w is not None:
        max_power_w = inverter_max_power_w
    else:
        max_power_w = DEFAULT_INVERTER_POWER

    if CONF_WS_MODE in entry_options:
        ws_mode = str(entry_options[CONF_WS_MODE])
    elif entry_options.get(CONF_WS_ALL_SESSIONS):
        ws_mode = WS_MODE_SMART_SESSIONS
    else:
        ws_mode = WS_MODE_AUTO

    headroom_pct: int = entry_options.get(CONF_SMART_HEADROOM, DEFAULT_SMART_HEADROOM)

    return IntegrationConfig(
        min_soc_on_grid=entry_options.get(
            CONF_MIN_SOC_ON_GRID, DEFAULT_MIN_SOC_ON_GRID
        ),
        api_min_soc=int(entry_options.get(CONF_API_MIN_SOC, DEFAULT_API_MIN_SOC)),
        battery_capacity_kwh=float(entry_options.get(CONF_BATTERY_CAPACITY_KWH, 0.0)),
        min_power_change=int(
            entry_options.get(CONF_MIN_POWER_CHANGE, DEFAULT_MIN_POWER_CHANGE)
        ),
        max_power_w=max_power_w,
        grid_export_limit_w=int(
            entry_options.get(CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT)
        ),
        smart_headroom=headroom_pct / 100.0,
        bms_polling_interval=float(
            entry_options.get(CONF_BMS_POLLING_INTERVAL, DEFAULT_BMS_POLLING_INTERVAL)
        ),
        ws_mode=ws_mode,
        entity_mode=bool(entry_options.get(CONF_WORK_MODE_ENTITY)),
    )


@dataclass
class FoxESSEntryData(EntryData):
    """Per-config-entry data with FoxESS-typed fields."""


@dataclass
class FoxESSControlData(SmartBatteryDomainData):
    """Complete runtime state for FoxESS Control in ``hass.data[DOMAIN]``."""

    config: IntegrationConfig | None = None

    # WebSocket real-time data
    realtime_ws: FoxESSRealtimeWS | None = None
    web_session: FoxESSWebSession | None = None
    plant_id: str | None = None
    ws_mode: str = ""
    ws_discharge_callback: Callable[..., Any] | None = None
    ws_last_trigger: float = 0.0

    # Debug log handlers (session context filter is attached directly
    # to each handler — see sensor.setup_debug_log for the rationale)
    debug_log_handlers: list[Any] = field(default_factory=list)

    # BMS battery compound ID (batteryId@batSn from WS or discovery)
    battery_compound_id: str | None = None

    # Work mode (cleared on session cancel for immediate UI update)
    work_mode: str | None = None

    # Proactive schedule conflict detection
    upcoming_conflicts: list[str] = field(default_factory=list)

    # Session replay after circuit breaker abort
    replay_pending: dict[str, Any] | None = None
    replay_unsub: Any = None
    replay_attempts: int = 0
    on_circuit_breaker_abort: Callable[..., Any] | None = None

    # Session cancel hook return type (brand-specific)
    on_session_cancel: Callable[[], Coroutine[Any, Any, None] | None] | None = None
