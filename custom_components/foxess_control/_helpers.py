"""FoxESS Control — shared helpers, accessors, and service schemas.

Extracted from __init__.py so that both __init__.py and _services.py can
import these without circular dependencies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import get_coordinator_soc
from .foxess import Inverter, WorkMode
from .foxess_adapter import FoxESSEntityAdapter
from .smart_battery.listeners import (
    cancel_smart_charge as _sb_cancel_smart_charge,
)
from .smart_battery.listeners import (
    cancel_smart_discharge as _sb_cancel_smart_discharge,
)
from .smart_battery.session import (
    clear_stored_session as _sb_clear_stored_session,
)
from .smart_battery.session import (
    save_session as _sb_save_session,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store

    from .domain_data import FoxESSControlData, IntegrationConfig
    from .smart_battery.taper import TaperProfile as _TaperProfile

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service name constants
# ---------------------------------------------------------------------------

SERVICE_CLEAR_OVERRIDES = "clear_overrides"
SERVICE_FEEDIN = "feedin"
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_SMART_CHARGE = "smart_charge"
SERVICE_SMART_DISCHARGE = "smart_discharge"

# ---------------------------------------------------------------------------
# Storage / session constants
# ---------------------------------------------------------------------------

STORAGE_KEY = "foxess_control_sessions"
STORAGE_VERSION = 1
MAX_SOC_UNAVAILABLE_COUNT = 3

VALID_MODES = [m.value for m in WorkMode]

_MANAGED_WORK_MODES = frozenset(
    {
        WorkMode.SELF_USE.value,
        WorkMode.FORCE_CHARGE.value,
        WorkMode.FORCE_DISCHARGE.value,
        WorkMode.FEEDIN.value,
    }
)

# ---------------------------------------------------------------------------
# Service schemas
# ---------------------------------------------------------------------------

SCHEMA_CLEAR_OVERRIDES = vol.Schema(
    {
        vol.Optional("mode"): vol.In(VALID_MODES),
    }
)

SCHEMA_FORCE_CHARGE = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Optional("start_time"): cv.time,
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_FORCE_DISCHARGE = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Optional("start_time"): cv.time,
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_FEEDIN = vol.Schema(
    {
        vol.Required("duration"): cv.time_period,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Optional("start_time"): cv.time,
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_SMART_DISCHARGE = vol.Schema(
    {
        vol.Required("start_time"): cv.time,
        vol.Required("end_time"): cv.time,
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
        vol.Required("min_soc"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Optional("feedin_energy_limit_kwh"): vol.All(
            vol.Coerce(float), vol.Range(min=0.1)
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

SCHEMA_SMART_CHARGE = vol.Schema(
    {
        vol.Required("start_time"): cv.time,
        vol.Required("end_time"): cv.time,
        vol.Required("target_soc"): vol.All(int, vol.Range(min=5, max=100)),
        vol.Optional("power"): vol.All(int, vol.Range(min=100)),
    },
    extra=vol.ALLOW_EXTRA,
)

# ---------------------------------------------------------------------------
# Domain data accessors
# ---------------------------------------------------------------------------


def _dd(hass: HomeAssistant) -> FoxESSControlData:
    """Return the typed domain data."""
    from .domain_data import FoxESSControlData

    data = hass.data[DOMAIN]
    if isinstance(data, FoxESSControlData):
        return data
    from .smart_battery.domain_data import _convert_legacy_dict

    return _convert_legacy_dict(data)  # type: ignore[return-value]


def _cfg(hass: HomeAssistant) -> IntegrationConfig:
    """Return the cached IntegrationConfig."""
    config = _dd(hass).config
    if config is None:
        raise ServiceValidationError(
            "Integration not fully loaded",
            translation_domain=DOMAIN,
            translation_key="no_integration",
        )
    return config


def _first_entry_id(hass: HomeAssistant) -> str:
    """Return the entry_id of the first real config entry in domain data.

    NOTE: Services currently operate on a single inverter only.
    If multiple config entries exist, only the first is used.
    """
    dd = hass.data.get(DOMAIN)
    if dd is not None:
        for eid in _dd(hass).entries:
            return eid
    raise ServiceValidationError(
        "No FoxESS Control integration configured",
        translation_domain=DOMAIN,
        translation_key="no_integration",
    )


def _get_inverter(hass: HomeAssistant) -> Inverter:
    """Get the first configured Inverter instance."""
    entry_id = _first_entry_id(hass)
    inv: Inverter = _dd(hass).entries[entry_id].inverter
    return inv


def _get_first_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the first real config entry."""
    entry_id = _first_entry_id(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise ServiceValidationError(
            "No FoxESS Control integration configured",
            translation_domain=DOMAIN,
            translation_key="no_integration",
        )
    return entry


def _get_entity_adapter(hass: HomeAssistant) -> FoxESSEntityAdapter:
    """Build a one-shot entity adapter from the current config."""
    return FoxESSEntityAdapter(
        entry_options=dict(_get_first_entry(hass).options),
        max_power_w=_cfg(hass).max_power_w,
    )


async def _apply_mode_via_entities(
    hass: HomeAssistant,
    mode: WorkMode,
    power_w: int | None = None,
    fd_soc: int = 11,
) -> None:
    """Set inverter mode via the entity adapter."""
    adapter = _get_entity_adapter(hass)
    await adapter.apply_mode(hass, mode, power_w, fd_soc)


# ---------------------------------------------------------------------------
# Coordinator value readers
# ---------------------------------------------------------------------------


def _get_current_soc(hass: HomeAssistant) -> float | None:
    """Get current battery SoC from the coordinator.

    Returns None if SoC is unavailable.
    """
    return get_coordinator_soc(hass)


def _get_net_consumption(hass: HomeAssistant) -> float:
    """Return net site consumption (loads minus solar) in kW.

    Reads ``loadsPower`` and ``pvPower`` from the coordinator.
    Returns ``0.0`` when coordinator data is unavailable so callers
    fall back to the previous no-offset behaviour.
    """
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return 0.0
    dd = _dd(hass)
    for _eid, entry_data in dd.entries.items():
        coordinator = entry_data.coordinator
        if coordinator is not None and coordinator.data:
            try:
                loads = float(coordinator.data.get("loadsPower", 0))
                pv = float(coordinator.data.get("pvPower", 0))
                return loads - pv
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Failed to parse loadsPower=%r / pvPower=%r "
                    "from coordinator, using 0",
                    coordinator.data.get("loadsPower"),
                    coordinator.data.get("pvPower"),
                )
                return 0.0
    return 0.0


def _get_feedin_energy_kwh(hass: HomeAssistant) -> float | None:
    """Return cumulative grid feed-in energy in kWh from the coordinator.

    Reads the ``feedin`` variable (lifetime counter) rather than
    the instantaneous ``feedinPower``.  Returns ``None`` when
    coordinator data is unavailable.
    """
    domain_data = hass.data.get(DOMAIN)
    if domain_data is None:
        return None
    dd = _dd(hass)
    for _eid, entry_data in dd.entries.items():
        coordinator = entry_data.coordinator
        if coordinator is not None and coordinator.data:
            raw = coordinator.data.get("feedin")
            if raw is None:
                return None
            try:
                return float(raw)
            except (ValueError, TypeError):
                return None
    return None


def _get_taper_profile(hass: HomeAssistant) -> _TaperProfile | None:
    """Return the adaptive taper profile from domain data."""
    if DOMAIN not in hass.data:
        return None
    return _dd(hass).taper_profile


async def _save_taper_profile(hass: HomeAssistant, profile: _TaperProfile) -> None:
    """Persist the taper profile to the session Store."""
    if DOMAIN not in hass.data:
        return
    store: Store[dict[str, Any]] | None = _dd(hass).store
    if store is None:
        return
    stored: dict[str, Any] = await store.async_load() or {}
    stored["taper_profile"] = profile.to_dict()
    await store.async_save(stored)


# ---------------------------------------------------------------------------
# Session cancel / persist
# ---------------------------------------------------------------------------


def _cancel_smart_charge(hass: HomeAssistant, *, clear_storage: bool = True) -> Any:
    """Cancel any active smart charge listeners and clear stored session.

    Returns the WS stop coroutine (if any) so callers that need ordered
    shutdown can await it after override removal.
    """
    return _sb_cancel_smart_charge(hass, DOMAIN, clear_storage=clear_storage)


def _cancel_smart_discharge(hass: HomeAssistant, *, clear_storage: bool = True) -> Any:
    """Cancel any active smart discharge listeners and clear stored session.

    Returns the WS stop coroutine (if any) so callers that need ordered
    shutdown can await it after override removal.
    """
    return _sb_cancel_smart_discharge(hass, DOMAIN, clear_storage=clear_storage)


async def _save_session(hass: HomeAssistant, key: str, data: dict[str, Any]) -> None:
    """Persist a smart session to storage."""
    store: Store[dict[str, Any]] | None = _dd(hass).store
    await _sb_save_session(store, key, data)


async def _clear_stored_session(hass: HomeAssistant, key: str) -> None:
    """Remove a smart session from storage."""
    store: Store[dict[str, Any]] | None = (
        _dd(hass).store if DOMAIN in hass.data else None
    )
    await _sb_clear_stored_session(store, key)
