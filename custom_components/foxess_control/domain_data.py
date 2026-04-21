"""Typed domain data for FoxESS Control.

Subclasses :class:`SmartBatteryDomainData` with FoxESS-specific fields
(WebSocket, web session, force-op tracking, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .smart_battery.domain_data import EntryData, SmartBatteryDomainData

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Coroutine

    from .foxess import FoxESSRealtimeWS, FoxESSWebSession


@dataclass
class FoxESSEntryData(EntryData):
    """Per-config-entry data with FoxESS-typed fields."""


@dataclass
class FoxESSControlData(SmartBatteryDomainData):
    """Complete runtime state for FoxESS Control in ``hass.data[DOMAIN]``."""

    # WebSocket real-time data
    realtime_ws: FoxESSRealtimeWS | None = None
    web_session: FoxESSWebSession | None = None
    plant_id: str | None = None
    ws_mode: str = ""
    ws_discharge_callback: Callable[..., Any] | None = None
    ws_last_trigger: float = 0.0

    # Force operation tracking
    force_op_timer: asyncio.TimerHandle | None = None
    force_op_end: Any = None

    # Session logging
    session_log_filter: Any = None

    # Debug log handlers
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
