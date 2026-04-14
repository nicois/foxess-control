"""FoxESS-specific InverterAdapter implementations.

Two adapters: :class:`FoxESSCloudAdapter` (schedule-based control via
the FoxESS Cloud API) and :class:`FoxESSEntityAdapter` (entity-based
control via foxess_modbus interop).

Also contains schedule utility functions (sanitisation, merging,
placeholder filtering) that implement FoxESS API constraints C-008
through C-011.
"""

from __future__ import annotations

import datetime  # noqa: TC003 — used at runtime by adapters
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.persistent_notification import async_create as pn_create
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHARGE_POWER_ENTITY,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_MIN_SOC_ENTITY,
    CONF_WORK_MODE_ENTITY,
    DEFAULT_API_MIN_SOC,
)
from .foxess import Inverter, WorkMode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .foxess.inverter import ScheduleGroup

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schedule utility functions (moved from __init__.py)
# ---------------------------------------------------------------------------

# Modes that this integration creates or treats as a safe baseline.
_MANAGED_WORK_MODES = frozenset(
    {
        WorkMode.SELF_USE.value,
        WorkMode.FORCE_CHARGE.value,
        WorkMode.FORCE_DISCHARGE.value,
        WorkMode.FEEDIN.value,
    }
)

_SCHEDULE_GROUP_KEYS = {
    "enable",
    "startHour",
    "startMinute",
    "endHour",
    "endMinute",
    "workMode",
    "minSocOnGrid",
    "fdSoc",
    "fdPwr",
}

_PLACEHOLDER_MODES = {"Invalid", ""}


def _to_minutes(hour: int, minute: int) -> int:
    """Convert hour:minute to minutes since midnight."""
    return hour * 60 + minute


def _groups_overlap(a: ScheduleGroup, b: ScheduleGroup) -> bool:
    """Check whether two schedule groups have overlapping time windows."""
    a_start = _to_minutes(a["startHour"], a["startMinute"])
    a_end = _to_minutes(a["endHour"], a["endMinute"])
    b_start = _to_minutes(b["startHour"], b["startMinute"])
    b_end = _to_minutes(b["endHour"], b["endMinute"])
    return a_start < b_end and b_start < a_end


def _is_placeholder(group: dict[str, Any]) -> bool:
    """Check if a group is an API placeholder (not a real schedule entry).

    The FoxESS API always returns 8 groups.  Unused slots come back as
    either ``workMode: "Invalid"`` / ``""`` **or** as zero-duration
    ``SelfUse`` groups (00:00–00:00).  Both forms must be filtered out
    when re-writing the schedule; leaving the zero-duration SelfUse
    groups in causes API error 42023 ("Time overlap").
    """
    if group.get("workMode", "") in _PLACEHOLDER_MODES:
        return True
    if any(k in group for k in ("startHour", "startMinute", "endHour", "endMinute")):
        start = group.get("startHour", 0) * 60 + group.get("startMinute", 0)
        end = group.get("endHour", 0) * 60 + group.get("endMinute", 0)
        if start == end:
            return True
    return False


def _sanitize_group(raw: dict[str, Any]) -> ScheduleGroup:
    """Strip unknown fields and fix invalid values in an API-returned group."""
    group: ScheduleGroup = {k: raw[k] for k in _SCHEDULE_GROUP_KEYS if k in raw}  # type: ignore[assignment]
    if "fdSoc" in group:
        group["fdSoc"] = max(group["fdSoc"], DEFAULT_API_MIN_SOC)
    if "minSocOnGrid" in group and "fdSoc" in group:
        group["minSocOnGrid"] = min(group["minSocOnGrid"], group["fdSoc"])
    return group


def _check_schedule_safe(
    groups: list[dict[str, Any]],
    hass: HomeAssistant | None = None,
) -> None:
    """Raise if the schedule contains modes this integration does not manage."""
    for group in groups:
        if _is_placeholder(group):
            continue
        mode = group.get("workMode", "")
        if mode and mode not in _MANAGED_WORK_MODES:
            time_range = (
                f"{group.get('startHour', 0):02d}:{group.get('startMinute', 0):02d}"
                f"–{group.get('endHour', 0):02d}:{group.get('endMinute', 0):02d}"
            )
            message = (
                f"The inverter schedule contains a **{mode}** group "
                f"({time_range}) which is not managed by this integration. "
                f"FoxESS Control expects Self Use as the default work mode "
                f"and will not modify the schedule while an unmanaged mode "
                f"is present.\n\n"
                f"Please remove the '{mode}' schedule group via the "
                f"FoxESS app, then retry the operation."
            )
            if hass is not None:
                pn_create(
                    hass,
                    message=message,
                    title="FoxESS Control: unmanaged work mode detected",
                    notification_id="foxess_control_unmanaged_mode",
                )
            raise ServiceValidationError(message)


def _is_expired(group: ScheduleGroup) -> bool:
    """Check if a group's end time has already passed today."""
    now = dt_util.now()
    group_end = _to_minutes(group["endHour"], group["endMinute"])
    current = _to_minutes(now.hour, now.minute)
    return group_end <= current


def _merge_with_existing(
    inverter: Inverter,
    new_group: ScheduleGroup,
    work_mode: WorkMode,
    force: bool = False,
) -> list[ScheduleGroup]:
    """Fetch the current schedule, remove same-mode groups, and merge."""
    schedule = inverter.get_schedule()
    existing: list[dict[str, Any]] = schedule.get("groups", [])
    _LOGGER.debug("Current schedule has %d groups: %s", len(existing), existing)
    _check_schedule_safe(existing)

    kept: list[ScheduleGroup] = []
    for raw_group in existing:
        if _is_placeholder(raw_group):
            continue
        group = _sanitize_group(raw_group)
        if group.get("workMode") == work_mode.value:
            _LOGGER.debug("Removing existing %s group", work_mode.value)
            continue
        if group.get("workMode") == WorkMode.SELF_USE.value:
            _LOGGER.debug("Dropping SelfUse baseline group")
            continue
        group["enable"] = 1
        if _groups_overlap(group, new_group):
            if force:
                _LOGGER.debug(
                    "Force-removing conflicting %s group", group.get("workMode")
                )
                continue
            raise ServiceValidationError(
                f"New {work_mode.value} window conflicts with an existing "
                f"{group.get('workMode')} override "
                f"({group['startHour']:02d}:{group['startMinute']:02d}"
                f"-{group['endHour']:02d}:{group['endMinute']:02d})"
            )
        kept.append(group)

    kept.append(new_group)
    _LOGGER.debug("Setting schedule with %d groups: %s", len(kept), kept)
    return kept


def _build_override_group(
    now: datetime.datetime,
    end: datetime.datetime,
    work_mode: WorkMode,
    inverter: Inverter,
    min_soc_on_grid: int,
    fd_soc: int,
    fd_pwr: int | None = None,
    api_min_soc: int = DEFAULT_API_MIN_SOC,
) -> ScheduleGroup:
    """Build a single ScheduleGroup for a timed override."""
    fd_soc = max(fd_soc, api_min_soc)
    min_soc_on_grid = min(min_soc_on_grid, fd_soc)
    return {
        "enable": 1,
        "startHour": now.hour,
        "startMinute": now.minute,
        "endHour": end.hour,
        "endMinute": end.minute,
        "workMode": work_mode.value,
        "minSocOnGrid": min_soc_on_grid,
        "fdSoc": fd_soc,
        "fdPwr": fd_pwr if fd_pwr is not None else inverter.max_power_w,
    }


def _remove_mode_from_schedule(
    inverter: Inverter,
    mode: WorkMode,
    min_soc_on_grid: int,
) -> None:
    """Remove all groups of *mode* from the schedule, keeping other modes."""
    schedule = inverter.get_schedule()
    raw_groups = schedule.get("groups", [])
    _LOGGER.debug(
        "Removing %s: current schedule has %d groups: %s",
        mode.value,
        len(raw_groups),
        raw_groups,
    )
    _check_schedule_safe(raw_groups)
    kept: list[ScheduleGroup] = []
    for raw_group in raw_groups:
        if _is_placeholder(raw_group):
            continue
        if raw_group.get("workMode") == mode.value:
            continue
        group = _sanitize_group(raw_group)
        group["enable"] = 1
        kept.append(group)
    if kept:
        _LOGGER.debug("After filtering: %d groups remain: %s", len(kept), kept)
        inverter.set_schedule(kept)
    else:
        _LOGGER.debug(
            "No groups remain after removing %s, reverting to SelfUse", mode.value
        )
        inverter.self_use(min_soc_on_grid)


# ---------------------------------------------------------------------------
# Entity mode map
# ---------------------------------------------------------------------------

_ENTITY_MODE_MAP: dict[str, str] = {
    WorkMode.SELF_USE: "Self Use",
    WorkMode.FORCE_CHARGE: "Force Charge",
    WorkMode.FORCE_DISCHARGE: "Force Discharge",
    WorkMode.BACKUP: "Back-up",
    WorkMode.FEEDIN: "Feed-in First",
}


# ---------------------------------------------------------------------------
# Adapter classes
# ---------------------------------------------------------------------------


class FoxESSCloudAdapter:
    """InverterAdapter for FoxESS cloud API (schedule-based) control.

    Wraps the FoxESS schedule merge/build logic into the standard
    ``apply_mode`` / ``remove_override`` interface. Caches the active
    schedule groups so subsequent power adjustments can mutate ``fdPwr``
    in-place without re-reading the schedule from the API.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        inverter: Inverter,
        min_soc_on_grid: int,
        api_min_soc: int,
        start: datetime.datetime,
        end: datetime.datetime,
        force: bool = False,
    ) -> None:
        self._hass = hass
        self._inverter = inverter
        self._min_soc_on_grid = min_soc_on_grid
        self._api_min_soc = api_min_soc
        self._start = start
        self._end = end
        self._force = force
        self._groups: list[ScheduleGroup] = []

    def get_max_power_w(self) -> int:
        return self._inverter.max_power_w

    def set_groups(self, groups: list[ScheduleGroup]) -> None:
        """Seed the cached groups (from initial service handler write)."""
        self._groups = list(groups)

    def get_groups(self) -> list[ScheduleGroup]:
        """Return current cached groups for session persistence."""
        return list(self._groups)

    async def apply_mode(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
        power_w: int | None = None,
        fd_soc: int = 11,
    ) -> None:
        """Set inverter mode via schedule merge.

        If cached groups exist with a matching mode, mutates ``fdPwr``
        in-place (avoids re-reading the schedule from the API on every
        tick).  Otherwise builds a new override group and merges.
        """
        # Fast path: mutate fdPwr in cached groups
        if self._groups:
            for g in self._groups:
                if g.get("workMode") == mode.value:
                    if power_w is not None:
                        g["fdPwr"] = power_w
                    break
            await hass.async_add_executor_job(self._inverter.set_schedule, self._groups)
            return

        # Slow path: build from scratch (initial call or post-recovery)
        now = dt_util.now()
        group = _build_override_group(
            now,
            self._end,
            mode,
            self._inverter,
            self._min_soc_on_grid,
            fd_soc,
            fd_pwr=power_w,
            api_min_soc=self._api_min_soc,
        )
        try:
            groups = await hass.async_add_executor_job(
                _merge_with_existing,
                self._inverter,
                group,
                mode,
                self._force,
            )
        except ServiceValidationError:
            _LOGGER.warning(
                "Schedule conflict during %s apply_mode, skipping",
                mode.value,
            )
            raise
        self._groups = groups
        await hass.async_add_executor_job(self._inverter.set_schedule, groups)

    async def remove_override(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
    ) -> None:
        """Remove the override, reverting to self-use."""
        await hass.async_add_executor_job(
            _remove_mode_from_schedule,
            self._inverter,
            mode,
            self._min_soc_on_grid,
        )
        self._groups = []


class FoxESSEntityAdapter:
    """InverterAdapter for FoxESS entity-mode (foxess_modbus) control.

    Reads entity IDs from the config entry options passed at construction
    time, avoiding any dependency on ``__init__.py`` accessor functions.
    """

    def __init__(
        self,
        entry_options: dict[str, Any],
        max_power_w: int,
    ) -> None:
        self._opts = entry_options
        self._max_power_w = max_power_w

    def get_max_power_w(self) -> int:
        return self._max_power_w

    async def apply_mode(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
        power_w: int | None = None,
        fd_soc: int = 11,
    ) -> None:
        """Set inverter mode by writing to foxess_modbus entities."""
        _LOGGER.debug(
            "Entity backend: setting mode=%s power=%s fd_soc=%d",
            mode,
            f"{power_w}W" if power_w is not None else "unchanged",
            fd_soc,
        )

        mode_option = _ENTITY_MODE_MAP.get(mode)
        if mode_option:
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": self._opts[CONF_WORK_MODE_ENTITY], "option": mode_option},
            )

        if power_w is not None and mode in (
            WorkMode.FORCE_CHARGE,
            WorkMode.FORCE_DISCHARGE,
        ):
            power_entity = (
                self._opts.get(CONF_CHARGE_POWER_ENTITY)
                if mode == WorkMode.FORCE_CHARGE
                else self._opts.get(CONF_DISCHARGE_POWER_ENTITY)
            )
            if power_entity:
                await hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": power_entity, "value": power_w},
                )

        min_soc_entity = self._opts.get(CONF_MIN_SOC_ENTITY)
        if min_soc_entity and mode == WorkMode.FORCE_DISCHARGE:
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": min_soc_entity, "value": fd_soc},
            )

    async def remove_override(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
    ) -> None:
        """Revert to self-use mode."""
        await self.apply_mode(hass, WorkMode.SELF_USE)
