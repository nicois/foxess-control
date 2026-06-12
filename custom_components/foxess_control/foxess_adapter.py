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

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONF_CHARGE_POWER_ENTITY,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_EXPORT_LIMIT_ENTITY,
    CONF_MIN_SOC_ENTITY,
    CONF_WORK_MODE_ENTITY,
    DEFAULT_API_MIN_SOC,
    DOMAIN,
)
from .foxess import Inverter, WorkMode
from .smart_battery.algorithms import (
    DISCHARGE_SAFETY_FACTOR,
    compute_safe_schedule_end,
)
from .smart_battery.logging import record_operational_error
from .smart_battery.reconcile import CommandKind

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _recent_errors(hass: HomeAssistant) -> Any:
    """Return the domain ring buffer for operational errors, or None.

    Defensive: a write may be attempted during teardown when domain data
    is gone — recording is best-effort and must never mask the original
    write failure (which is re-raised by the caller).
    """
    try:
        from ._helpers import _dd

        return _dd(hass).recent_errors
    except Exception:  # noqa: BLE001 — buffer lookup is best-effort
        return None


# Entity/HA-service write failures we can confidently enumerate: HA service
# layer errors plus the value-conversion errors the write bodies can raise.
_WRITE_ERRORS: tuple[type[Exception], ...] = (
    HomeAssistantError,
    ValueError,
    TypeError,
)


# ---------------------------------------------------------------------------
# Work-mode reconciliation (issue #11): detect when the inverter ignores a
# commanded schedule write and keeps reporting a different work mode.
# ---------------------------------------------------------------------------

# Additive margin on top of one poll interval before a persisting mode
# mismatch is treated as a real conflict (issue #11).
SCHEDULE_RECONCILE_MARGIN = datetime.timedelta(seconds=60)

_SCHEDULE_NOT_APPLIED_ISSUE = "schedule_not_applied"


class ScheduleNotApplied(Exception):
    """The inverter's reported work mode diverges from what was commanded.

    Constructed (not raised into control flow) to give
    ``record_operational_error`` a BaseException with a useful message.
    """


def _record_commanded_mode(
    hass: HomeAssistant,
    mode: WorkMode,
    *,
    kind: CommandKind = CommandKind.APPLY,
) -> None:
    """Persist the work-mode intent just commanded via a cloud schedule write.

    ``kind`` records what we did with ``mode`` (the *watched* mode):

    - ``APPLY`` (default): we applied an override and expect the inverter
      to report ``mode``.
    - ``REMOVE``: we removed an override; ``mode`` is the mode that was
      removed.  The reconciler conflicts only if the inverter STILL reports
      it (an unrelated managed group is fine).

    The timestamp is reset only when the ``(kind, mode)`` intent *changes*.
    The listener re-issues the same mode every adjust tick; if each re-issue
    reset the clock, a persisting conflict could never cross the grace
    window (the reconciler would always see it as freshly commanded).  A
    removal of a just-applied mode is a *new* expectation, so the kind
    change resets the clock too.
    """
    try:
        from ._helpers import _dd

        dd = _dd(hass)
        if dd.commanded_work_mode != mode.value or dd.commanded_kind != kind.value:
            dd.commanded_work_mode = mode.value
            dd.commanded_kind = kind.value
            dd.commanded_work_mode_at = dt_util.utcnow().isoformat()
    except Exception:  # noqa: BLE001 — recording is best-effort
        _LOGGER.debug("Failed to record commanded work mode (non-critical)")


def _create_schedule_not_applied_issue(
    hass: HomeAssistant,
    domain: str,
    *,
    commanded: str,
    reported: str,
) -> None:
    """Raise a Repair issue: the inverter is not applying schedule changes."""
    try:
        async_create_issue(
            hass,
            domain,
            _SCHEDULE_NOT_APPLIED_ISSUE,
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key=_SCHEDULE_NOT_APPLIED_ISSUE,
            translation_placeholders={
                "commanded": commanded,
                "reported": reported,
            },
        )
    except Exception:  # noqa: BLE001 — Repair surfacing is best-effort
        _LOGGER.debug("Failed to create schedule_not_applied issue (non-critical)")


def _clear_schedule_not_applied_issue(hass: HomeAssistant, domain: str) -> None:
    """Dismiss the schedule_not_applied Repair issue."""
    try:
        async_delete_issue(hass, domain, _SCHEDULE_NOT_APPLIED_ISSUE)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Failed to clear schedule_not_applied issue (non-critical)")


def reconcile_work_mode(
    hass: HomeAssistant,
    domain: str,
    reported_mode: str | None,
    poll_interval: datetime.timedelta,
) -> None:
    """Reconcile the last-commanded work mode against the polled mode.

    Detect-and-surface only (issue #11): on a confirmed conflict, record
    an operational error and raise a Repair issue; clear the issue when
    the modes reconcile.  Never raises — must not break the poll.
    """
    try:
        from ._helpers import _dd
        from .smart_battery.reconcile import (
            SELF_USE,
            ReconcileVerdict,
            reconcile_commanded_mode,
        )

        dd = _dd(hass)
        commanded = dd.commanded_work_mode
        commanded_at_iso = dd.commanded_work_mode_at
        if commanded is None or commanded_at_iso is None:
            return
        # Legacy/None commanded_kind (intent recorded before this field
        # existed, or a persisted pre-upgrade session) falls back to APPLY —
        # the pre-fix behaviour; it self-heals on the next remove_override.
        kind = (
            CommandKind.REMOVE
            if dd.commanded_kind == CommandKind.REMOVE.value
            else CommandKind.APPLY
        )
        commanded_at = datetime.datetime.fromisoformat(commanded_at_iso)
        grace = poll_interval + SCHEDULE_RECONCILE_MARGIN
        verdict = reconcile_commanded_mode(
            kind, commanded, commanded_at, reported_mode, dt_util.utcnow(), grace
        )
        if verdict is ReconcileVerdict.CONFLICT:
            reported = reported_mode or SELF_USE
            if kind is CommandKind.REMOVE:
                # The watched mode is the one we removed; conflict means it
                # is STILL active.  Surface "commanded SelfUse" (the intended
                # post-removal state) so the Repair placeholders read sensibly.
                msg = (
                    f"commanded removal of {commanded} but inverter still "
                    f"reports {reported}"
                )
                placeholder_commanded = SELF_USE
            else:
                msg = f"commanded {commanded} but inverter reports {reported}"
                placeholder_commanded = commanded
            record_operational_error(
                _LOGGER,
                _recent_errors(hass),
                category="schedule_not_applied",
                attempted="reconcile commanded work mode against polled mode",
                exc=ScheduleNotApplied(msg),
                hint=(
                    "the inverter reports a different work mode than was "
                    "commanded — it may not be applying schedule changes; "
                    "check inverter firmware/compatibility"
                ),
                context={
                    "commanded": commanded,
                    "kind": kind.value,
                    "reported": reported,
                    "since": commanded_at_iso,
                },
            )
            _create_schedule_not_applied_issue(
                hass, domain, commanded=placeholder_commanded, reported=reported
            )
        elif verdict is ReconcileVerdict.OK:
            _clear_schedule_not_applied_issue(hass, domain)
        # WITHIN_GRACE: do nothing (neither surface nor clear yet).
    except Exception:  # noqa: BLE001 — reconciliation must never break the poll
        _LOGGER.debug("Work-mode reconciliation failed (non-critical)", exc_info=True)


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


def check_schedule_conflicts(
    groups: list[dict[str, Any]],
) -> list[str]:
    """Return descriptions of unmanaged schedule groups (non-raising)."""
    conflicts: list[str] = []
    for group in groups:
        if _is_placeholder(group):
            continue
        mode = group.get("workMode", "")
        if mode and mode not in _MANAGED_WORK_MODES:
            time_range = (
                f"{group.get('startHour', 0):02d}:{group.get('startMinute', 0):02d}"
                f"–{group.get('endHour', 0):02d}:{group.get('endMinute', 0):02d}"
            )
            conflicts.append(f"{mode} ({time_range})")
    return conflicts


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
                async_create_issue(
                    hass,
                    DOMAIN,
                    "unmanaged_work_mode",
                    is_fixable=False,
                    severity=IssueSeverity.ERROR,
                    translation_key="unmanaged_work_mode",
                    translation_placeholders={
                        "work_mode": mode,
                        "time_range": time_range,
                    },
                )
            raise ServiceValidationError(
                message,
                translation_domain=DOMAIN,
                translation_key="unmanaged_work_mode",
                translation_placeholders={
                    "work_mode": mode,
                    "time_range": time_range,
                },
            )
    if hass is not None:
        async_delete_issue(hass, DOMAIN, "unmanaged_work_mode")


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
            conflict_range = (
                f"{group['startHour']:02d}:{group['startMinute']:02d}"
                f"-{group['endHour']:02d}:{group['endMinute']:02d}"
            )
            raise ServiceValidationError(
                f"New {work_mode.value} window conflicts with existing "
                f"{group.get('workMode')} override ({conflict_range})",
                translation_domain=DOMAIN,
                translation_key="schedule_conflict",
                translation_placeholders={
                    "new_mode": work_mode.value,
                    "existing_mode": group.get("workMode", "?"),
                    "time_range": (
                        f"{group['startHour']:02d}:{group['startMinute']:02d}"
                        f"-{group['endHour']:02d}:{group['endMinute']:02d}"
                    ),
                },
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
        capacity_kwh: float = 0,
        soc_getter: Any = None,
        export_limit_entity: str | None = None,
        on_session_started_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._hass = hass
        self._inverter = inverter
        self._min_soc_on_grid = min_soc_on_grid
        self._api_min_soc = api_min_soc
        self._start = start
        self._end = end
        self._force = force
        self._capacity_kwh = capacity_kwh
        self._soc_getter = soc_getter
        self._groups: list[ScheduleGroup] = []
        self._export_limit_entity = export_limit_entity
        self._warned_missing_export_limit = False
        self._first_export_write_logged = False
        self._on_session_started_cb = on_session_started_cb

    def get_max_power_w(self) -> int:
        return self._inverter.max_power_w

    def set_groups(self, groups: list[ScheduleGroup]) -> None:
        """Seed the cached groups (from initial service handler write)."""
        self._groups = list(groups)

    def get_groups(self) -> list[ScheduleGroup]:
        """Return current cached groups for session persistence."""
        return list(self._groups)

    def _safe_end(
        self, mode: WorkMode, power_w: int | None, fd_soc: int
    ) -> datetime.datetime:
        """Compute safe schedule end for discharge; full end for other modes."""
        if (
            mode != WorkMode.FORCE_DISCHARGE
            or not power_w
            or power_w <= 0
            or self._capacity_kwh <= 0
            or self._soc_getter is None
        ):
            return self._end
        soc = self._soc_getter()
        if soc is None:
            return self._end
        return compute_safe_schedule_end(
            soc,
            fd_soc,
            self._capacity_kwh,
            power_w,
            self._end,
            safety_factor=DISCHARGE_SAFETY_FACTOR,
        )

    async def apply_mode(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
        power_w: int | None = None,
        fd_soc: int = 11,
    ) -> None:
        """Set inverter mode via schedule merge.

        For forced discharge, the schedule end time is set to a safe
        horizon based on current SoC, discharge rate, and safety factor.
        If HA loses connectivity, the inverter's schedule expires at
        this horizon and reverts to self-use.
        """
        safe_end = self._safe_end(mode, power_w, fd_soc)

        # Surface the horizon in session state for the Lovelace card
        if mode == WorkMode.FORCE_DISCHARGE and safe_end != self._end:
            from .smart_battery.domain_data import get_domain_data

            ds = get_domain_data(hass, DOMAIN).smart_discharge_state
            if ds is not None:
                ds["schedule_horizon"] = safe_end.isoformat()

        # Fast path: mutate fdPwr and end time in cached groups
        if self._groups:
            for g in self._groups:
                if g.get("workMode") == mode.value:
                    if power_w is not None:
                        g["fdPwr"] = power_w
                    g["endHour"] = safe_end.hour
                    g["endMinute"] = safe_end.minute
                    break
            await hass.async_add_executor_job(self._inverter.set_schedule, self._groups)
            _record_commanded_mode(hass, mode)
            return

        # Slow path: build from scratch (initial call or post-recovery)
        now = dt_util.now()
        group = _build_override_group(
            now,
            safe_end,
            mode,
            self._inverter,
            self._min_soc_on_grid,
            fd_soc,
            fd_pwr=power_w,
            api_min_soc=self._api_min_soc,
        )
        try:
            # Pre-check schedule safety from async context so persistent
            # notifications can be created if unmanaged modes are found.
            schedule = await hass.async_add_executor_job(self._inverter.get_schedule)
            _check_schedule_safe(schedule.get("groups", []), hass)

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
        _record_commanded_mode(hass, mode)

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
        # Record the REMOVED mode as the watched mode: a conflict is only
        # the inverter STILL reporting it.  Recording SelfUse here caused a
        # false-positive Repair whenever a standalone managed group (e.g.
        # Feed-in) made get_current_mode report a non-SelfUse mode.
        _record_commanded_mode(hass, mode, kind=CommandKind.REMOVE)
        self._groups = []

    async def set_export_limit_w(
        self,
        hass: HomeAssistant,
        value_w: int,
    ) -> None:
        """Set the hardware Max Grid Export Limit (cross-integration)."""
        entity_id = self._export_limit_entity
        if not entity_id:
            if not self._warned_missing_export_limit:
                _LOGGER.debug(
                    "Smart discharge: export-limit entity unconfigured; "
                    "hardware-actuator writes disabled."
                )
                self._warned_missing_export_limit = True
            return
        domain = _entity_service_domain(entity_id, "number")
        try:
            await hass.services.async_call(
                domain,
                "set_value",
                {"entity_id": entity_id, "value": int(value_w)},
                blocking=True,
            )
            if not self._first_export_write_logged:
                _LOGGER.info(
                    "Export-limit write OK: %s ← %d W",
                    entity_id,
                    value_w,
                )
                self._first_export_write_logged = True
        except _WRITE_ERRORS as err:
            _LOGGER.warning(
                "Export-limit write FAILED: %s ← %d W",
                entity_id,
                value_w,
            )
            record_operational_error(
                _LOGGER,
                _recent_errors(hass),
                category="export_limit_write",
                attempted="cloud-mode export-limit set_value (cross-integration)",
                exc=err,
                hint=(
                    "HA service call to the export-limit entity failed — check "
                    "the entity exists and is available"
                ),
                context={"entity_id": entity_id, "value": value_w},
            )
            raise
        except Exception as err:
            _LOGGER.warning(
                "Export-limit write FAILED: %s ← %d W",
                entity_id,
                value_w,
            )
            record_operational_error(
                _LOGGER,
                _recent_errors(hass),
                category="unexpected",
                attempted="cloud-mode export-limit set_value (cross-integration)",
                exc=err,
                context={"entity_id": entity_id, "value": value_w},
                severity="error",
            )
            raise

    async def get_export_limit_w(
        self,
        hass: HomeAssistant,
    ) -> int | None:
        """Read the current hardware Max Grid Export Limit."""
        entity_id = self._export_limit_entity
        if not entity_id:
            return None
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", "", None):
            return None
        try:
            return int(float(state.state))
        except (TypeError, ValueError):
            return None

    def on_session_started(self, *, session_type: str) -> None:
        """Forward the deferred→active transition to the brand layer.

        The brand-agnostic listener invokes this synchronously inside
        the tick that flips ``charging_started`` /
        ``discharging_started`` to True.  Used by the FoxESS layer to
        bring up the real-time WebSocket immediately rather than
        waiting up to ``SMART_CHARGE_ADJUST_SECONDS`` (5 min) for the
        next periodic adjust tick — see C-020.

        The callback is injected by ``_build_foxess_adapter`` and
        wraps ``_maybe_start_realtime_ws`` in
        ``hass.async_create_task`` so the listener tick stays
        non-blocking.
        """
        if self._on_session_started_cb is not None:
            self._on_session_started_cb(session_type)


def _entity_service_domain(entity_id: str, default: str) -> str:
    """Derive service domain from entity ID (input_select → input_select)."""
    prefix = entity_id.split(".", 1)[0]
    return prefix if prefix.startswith("input_") else default


def _convert_and_clamp_power_for_write(
    hass: HomeAssistant,
    entity_id: str,
    value_w: int | float,
) -> float | int:
    """Return *value_w* in the target entity's unit, clamped to its min/max.

    Entity-mode control writes power values to foxess_modbus (and similar)
    ``number.*`` entities. Those entities declare their own
    ``unit_of_measurement`` — commonly ``kW`` with ``max=15`` — and
    silently clamp out-of-range writes. Passing raw watts to a kW target
    therefore saturates every write at the clamp ceiling, disabling the
    pacing algorithm. See D-056.

    Rules:

    * Target unit ``W`` (or unit matches watts): passthrough.
    * Target unit ``kW`` / another ``PowerConverter`` unit: convert from
      watts to that unit using HA's built-in ``PowerConverter``.
    * Unknown / missing unit: passthrough and emit a warning — the user
      needs to know the integration cannot validate the write.
    * After conversion, clamp to ``min``/``max`` attributes when present;
      log a warning when the requested value exceeded ``max``.

    This helper lives in the brand layer (C-021 / C-039): it touches
    foxess_modbus-style target-entity attributes and must not be added to
    ``smart_battery/``.
    """
    state = hass.states.get(entity_id)
    attrs: dict[str, Any]
    raw_attrs = getattr(state, "attributes", None) if state is not None else None
    attrs = raw_attrs if isinstance(raw_attrs, dict) else {}

    raw_unit = attrs.get("unit_of_measurement")
    unit: str | None = raw_unit if isinstance(raw_unit, str) and raw_unit else None

    # Step 1: unit conversion.
    if unit is None:
        _LOGGER.warning(
            "Target entity %s has no unit_of_measurement; passing %s W "
            "through unchanged. Set the entity's unit (kW or W) to allow "
            "FoxESS Control to scale writes correctly.",
            entity_id,
            value_w,
        )
        converted: float | int = value_w
    elif unit == "W":
        converted = value_w
    elif unit in PowerConverter.VALID_UNITS:
        converted = PowerConverter.convert(float(value_w), "W", unit)
    else:
        _LOGGER.warning(
            "Target entity %s has unrecognised power unit %r; passing "
            "%s W through unchanged.",
            entity_id,
            unit,
            value_w,
        )
        converted = value_w

    # Step 2: clamp to the entity's declared range when present.
    def _to_float(val: Any) -> float | None:
        if not isinstance(val, int | float):
            return None
        return float(val)

    max_f = _to_float(attrs.get("max"))
    if max_f is not None and converted > max_f:
        _LOGGER.warning(
            "Requested %s (from %s W) exceeds %s max=%s; "
            "clamping to max. Pacing may be capped by the target "
            "entity's declared range.",
            converted,
            value_w,
            entity_id,
            max_f,
        )
        converted = max_f

    min_f = _to_float(attrs.get("min"))
    if min_f is not None and converted < min_f:
        converted = min_f

    return converted


class FoxESSEntityAdapter:
    """InverterAdapter for FoxESS entity-mode (foxess_modbus) control.

    Reads entity IDs from the config entry options passed at construction
    time, avoiding any dependency on ``__init__.py`` accessor functions.
    """

    def __init__(
        self,
        entry_options: dict[str, Any],
        max_power_w: int,
        min_soc_on_grid: int = DEFAULT_API_MIN_SOC,
    ) -> None:
        self._opts = entry_options
        self._max_power_w = max_power_w
        # The user's configured persistent on-grid min-SoC reserve. Used to
        # restore the inverter's persistent Min SoC floor on session teardown
        # so a discharge session does not leave it raised to the session
        # target (P-001/P-002, C-025).
        self._min_soc_on_grid = min_soc_on_grid
        self._first_write: dict[str, bool] = {}
        self._warned_missing_export_limit = False

    def get_max_power_w(self) -> int:
        return self._max_power_w

    def _log_first_write(
        self, entity_id: str, action: str, value: Any, error: Exception | None = None
    ) -> None:
        """Log the first write attempt for each entity."""
        if entity_id in self._first_write:
            return
        self._first_write[entity_id] = True
        if error is None:
            _LOGGER.info(
                "Entity write OK: %s ← %s.%s(%s)",
                entity_id,
                _entity_service_domain(entity_id, "select"),
                action,
                value,
            )
        else:
            _LOGGER.warning(
                "Entity write FAILED: %s ← %s.%s(%s): %s",
                entity_id,
                _entity_service_domain(entity_id, "select"),
                action,
                value,
                error,
            )

    def _record_write_error(
        self,
        hass: HomeAssistant,
        *,
        category: str,
        attempted: str,
        exc: BaseException,
        entity_id: str,
        value: Any,
        severity: str = "warning",
    ) -> None:
        """Record an entity-mode write failure to the diagnostics ring buffer.

        The write itself is still re-raised by the caller — this only adds a
        self-sufficient diagnostic so a silent (to the user) inverter-no-op
        becomes visible via the diagnostics download (C-020/C-026).
        """
        record_operational_error(
            _LOGGER,
            _recent_errors(hass),
            category=category,
            attempted=attempted,
            exc=exc,
            hint=(
                "HA service call to the foxess_modbus target entity failed — "
                "check the entity exists and is available"
            ),
            context={"entity_id": entity_id, "value": value},
            severity=severity,
        )

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
            wm_entity = self._opts[CONF_WORK_MODE_ENTITY]
            try:
                await hass.services.async_call(
                    _entity_service_domain(wm_entity, "select"),
                    "select_option",
                    {"entity_id": wm_entity, "option": mode_option},
                )
                self._log_first_write(wm_entity, "select_option", mode_option)
            except _WRITE_ERRORS as err:
                self._log_first_write(wm_entity, "select_option", mode_option, err)
                self._record_write_error(
                    hass,
                    category="mode_write",
                    attempted="entity-mode work-mode select_option",
                    exc=err,
                    entity_id=wm_entity,
                    value=mode_option,
                )
                raise
            except Exception as err:
                self._record_write_error(
                    hass,
                    category="unexpected",
                    attempted="entity-mode work-mode select_option",
                    exc=err,
                    entity_id=wm_entity,
                    value=mode_option,
                    severity="error",
                )
                raise

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
                # Convert watts to the target entity's declared unit and clamp
                # to its min/max. Prevents foxess_modbus's range-clamp from
                # saturating every write at the ceiling (D-056).
                value = _convert_and_clamp_power_for_write(hass, power_entity, power_w)
                try:
                    await hass.services.async_call(
                        _entity_service_domain(power_entity, "number"),
                        "set_value",
                        {"entity_id": power_entity, "value": value},
                    )
                    self._log_first_write(power_entity, "set_value", value)
                except _WRITE_ERRORS as err:
                    self._log_first_write(power_entity, "set_value", value, err)
                    self._record_write_error(
                        hass,
                        category="power_write",
                        attempted="entity-mode force power set_value",
                        exc=err,
                        entity_id=power_entity,
                        value=value,
                    )
                    raise
                except Exception as err:
                    self._record_write_error(
                        hass,
                        category="unexpected",
                        attempted="entity-mode force power set_value",
                        exc=err,
                        entity_id=power_entity,
                        value=value,
                        severity="error",
                    )
                    raise

        # The foxess_modbus 'Min SoC' register is the inverter's PERSISTENT
        # on-grid floor. During force discharge it doubles as the stop SoC,
        # so we write the session target (fd_soc). On any non-discharge mode
        # (self-use teardown) we MUST restore it to the user's configured
        # reserve — otherwise the session target is left as the persistent
        # floor and self-use imports from the grid instead of discharging the
        # battery down to the real reserve (P-001/P-002, C-025).
        min_soc_entity = self._opts.get(CONF_MIN_SOC_ENTITY)
        if min_soc_entity:
            min_soc_value = (
                fd_soc if mode == WorkMode.FORCE_DISCHARGE else self._min_soc_on_grid
            )
            try:
                await hass.services.async_call(
                    _entity_service_domain(min_soc_entity, "number"),
                    "set_value",
                    {"entity_id": min_soc_entity, "value": min_soc_value},
                )
                self._log_first_write(min_soc_entity, "set_value", min_soc_value)
            except _WRITE_ERRORS as err:
                self._log_first_write(min_soc_entity, "set_value", min_soc_value, err)
                self._record_write_error(
                    hass,
                    category="min_soc_write",
                    attempted="entity-mode min-SoC set_value",
                    exc=err,
                    entity_id=min_soc_entity,
                    value=min_soc_value,
                )
                raise
            except Exception as err:
                self._record_write_error(
                    hass,
                    category="unexpected",
                    attempted="entity-mode min-SoC set_value",
                    exc=err,
                    entity_id=min_soc_entity,
                    value=min_soc_value,
                    severity="error",
                )
                raise

    async def remove_override(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
    ) -> None:
        """Revert to self-use mode."""
        await self.apply_mode(hass, WorkMode.SELF_USE)

    async def set_export_limit_w(
        self,
        hass: HomeAssistant,
        value_w: int,
    ) -> None:
        """Set the hardware Max Grid Export Limit (foxess_modbus)."""
        entity_id = self._opts.get(CONF_EXPORT_LIMIT_ENTITY)
        if not entity_id:
            if not self._warned_missing_export_limit:
                _LOGGER.warning(
                    "Smart discharge: export-limit entity unconfigured; "
                    "skipping hardware write."
                )
                self._warned_missing_export_limit = True
            return
        domain = _entity_service_domain(entity_id, "number")
        # Convert watts to the target entity's declared unit and clamp to
        # its min/max (D-056).
        value = _convert_and_clamp_power_for_write(hass, entity_id, value_w)
        try:
            await hass.services.async_call(
                domain,
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
            self._log_first_write(entity_id, "set_value", value)
        except _WRITE_ERRORS as err:
            self._log_first_write(entity_id, "set_value", value, err)
            self._record_write_error(
                hass,
                category="export_limit_write",
                attempted="entity-mode export-limit set_value",
                exc=err,
                entity_id=entity_id,
                value=value,
            )
            raise
        except Exception as err:
            self._record_write_error(
                hass,
                category="unexpected",
                attempted="entity-mode export-limit set_value",
                exc=err,
                entity_id=entity_id,
                value=value,
                severity="error",
            )
            raise

    async def get_export_limit_w(
        self,
        hass: HomeAssistant,
    ) -> int | None:
        """Read the current hardware Max Grid Export Limit."""
        entity_id = self._opts.get(CONF_EXPORT_LIMIT_ENTITY)
        if not entity_id:
            return None
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", "", None):
            return None
        try:
            return int(float(state.state))
        except (TypeError, ValueError):
            return None

    def on_session_started(self, *, session_type: str) -> None:
        """No-op for entity-mode brands — no WebSocket to bring up."""
