"""Typed domain data for FoxESS Control.

Subclasses :class:`SmartBatteryDomainData` with FoxESS-specific fields
(WebSocket, web session, force-op tracking, etc.).
"""

from __future__ import annotations

import contextlib
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

    adapter: Any = None


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

    # Work mode (cleared on session cancel for immediate UI update)
    work_mode: str | None = None

    # Proactive schedule conflict detection
    upcoming_conflicts: list[str] = field(default_factory=list)

    # Session cancel hook return type (brand-specific)
    on_session_cancel: Callable[[], Coroutine[Any, Any, None] | None] | None = None

    # --- Bridge layer for incremental migration ---
    # Maps old string keys to dataclass attributes so existing code using
    # domain_data["_key"] / domain_data.get("_key") keeps working while
    # files are migrated one at a time.  Remove after full migration.

    _KEY_MAP: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._KEY_MAP = {
            "_smart_charge_state": "smart_charge_state",
            "_smart_discharge_state": "smart_discharge_state",
            "_smart_error_state": "smart_error_state",
            "_smart_charge_unsubs": "smart_charge_unsubs",
            "_smart_discharge_unsubs": "smart_discharge_unsubs",
            "_smart_charge_session_id": "",
            "_smart_discharge_session_id": "",
            "_store": "store",
            "_taper_profile": "taper_profile",
            "_on_session_cancel": "on_session_cancel",
            "_pending_override_cleanup": "pending_override_cleanup",
            "_realtime_ws": "realtime_ws",
            "_web_session": "web_session",
            "_plant_id": "plant_id",
            "_ws_mode": "ws_mode",
            "_ws_discharge_callback": "ws_discharge_callback",
            "_ws_last_trigger": "ws_last_trigger",
            "_force_op_timer": "force_op_timer",
            "_force_op_end": "force_op_end",
            "_session_log_filter": "session_log_filter",
            "_debug_log_handlers": "debug_log_handlers",
            "_work_mode": "work_mode",
        }

    def _resolve_key(self, key: str) -> str | None:
        return self._KEY_MAP.get(key)

    def __getitem__(self, key: str) -> Any:
        attr = self._resolve_key(key)
        if attr:
            return getattr(self, attr)
        if key in self.entries:
            ed = self.entries[key]
            return {"coordinator": ed.coordinator, "inverter": ed.inverter}
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        attr = self._resolve_key(key)
        if attr:
            object.__setattr__(self, attr, value)
            return
        if isinstance(value, dict) and ("coordinator" in value or "inverter" in value):
            ed = self.entries.get(key) or FoxESSEntryData()
            ed.coordinator = value.get("coordinator")
            ed.inverter = value.get("inverter")
            self.entries[key] = ed
            return
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            if self._resolve_key(key):
                return True
            return key in self.entries
        return False

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def pop(self, key: str, *args: Any) -> Any:
        attr = self._resolve_key(key)
        if attr:
            val = getattr(self, attr)
            object.__setattr__(self, attr, None if not isinstance(val, list) else [])
            return val
        if key in self.entries:
            ed = self.entries.pop(key)
            return {"coordinator": ed.coordinator, "inverter": ed.inverter}
        if args:
            return args[0]
        raise KeyError(key)

    def setdefault(self, key: str, default: Any = None) -> Any:
        attr = self._resolve_key(key)
        if attr:
            val = getattr(self, attr)
            if val is None or (isinstance(val, list) and not val):
                object.__setattr__(self, attr, default)
                return default
            return val
        if key not in self.entries and isinstance(default, dict):
            ed = FoxESSEntryData()
            ed.coordinator = default.get("coordinator")
            ed.inverter = default.get("inverter")
            self.entries[key] = ed
            return default
        return self.get(key, default)

    def __iter__(self):  # type: ignore[no-untyped-def]
        """Iterate over entry_ids + underscore keys (for legacy iteration)."""
        yield from self.entries
        yield from self._KEY_MAP

    def items(self):  # type: ignore[no-untyped-def]
        """Yield (key, value) pairs for legacy dict iteration."""
        for key in self:
            with contextlib.suppress(KeyError):
                yield key, self[key]
