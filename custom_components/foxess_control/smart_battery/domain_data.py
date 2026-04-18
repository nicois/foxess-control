"""Typed domain data for smart battery integrations.

Replaces untyped ``hass.data[domain]`` dict with a dataclass so all
field access is statically checkable.  Brand integrations subclass
:class:`SmartBatteryDomainData` to add brand-specific fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store

    from .taper import TaperProfile


@dataclass
class EntryData:
    """Per-config-entry runtime data (brand-agnostic portion)."""

    coordinator: Any = None
    inverter: Any = None


@dataclass
class SmartBatteryDomainData:
    """Brand-agnostic runtime state stored in ``hass.data[domain]``."""

    # Per-entry data keyed by entry_id
    entries: dict[str, EntryData] = field(default_factory=dict)

    # Smart session state
    smart_charge_state: dict[str, Any] | None = None
    smart_discharge_state: dict[str, Any] | None = None
    smart_error_state: dict[str, Any] | None = None

    # Listener unsubscribe callbacks
    smart_charge_unsubs: list[Callable[[], None]] = field(default_factory=list)
    smart_discharge_unsubs: list[Callable[[], None]] = field(default_factory=list)

    # Session persistence
    store: Store[dict[str, Any]] | None = None

    # Adaptive taper profile
    taper_profile: TaperProfile | None = None

    # Post-cancel hook (brand-specific, e.g. stop WebSocket)
    on_session_cancel: Callable[[], Any] | None = None

    # Pending override cleanup for retry on next poll
    pending_override_cleanup: dict[str, Any] | None = None


def get_domain_data(hass: HomeAssistant, domain: str) -> SmartBatteryDomainData:
    """Return the typed domain data, creating it if absent."""
    data = hass.data.get(domain)
    if isinstance(data, SmartBatteryDomainData):
        return data
    dd = SmartBatteryDomainData()
    hass.data[domain] = dd
    return dd


def get_first_coordinator(hass: HomeAssistant, domain: str) -> Any:
    """Return the coordinator from the first entry, or None."""
    dd = hass.data.get(domain)
    if not isinstance(dd, SmartBatteryDomainData):
        return None
    for entry_data in dd.entries.values():
        if entry_data.coordinator is not None:
            return entry_data.coordinator
    return None


def get_first_entry_id(hass: HomeAssistant, domain: str) -> str | None:
    """Return the first entry_id, or None."""
    dd = hass.data.get(domain)
    if not isinstance(dd, SmartBatteryDomainData):
        return None
    for eid in dd.entries:
        return eid
    return None
