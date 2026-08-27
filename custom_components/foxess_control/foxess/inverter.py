"""High-level inverter control: work modes and battery state."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any

from ..smart_battery.events import SCHEDULE_WRITE, emit_event
from ..smart_battery.types import MinSocSettings, ScheduleGroup, WorkMode

if TYPE_CHECKING:
    from .client import FoxESSClient

__all__ = ["Inverter", "MinSocSettings", "ScheduleGroup", "WorkMode"]

_LOGGER = logging.getLogger(__name__)

_SCHEDULE_ENDPOINT = "/op/v0/device/scheduler/enable"

# Returns, per device, a ``properties`` map describing the accepted range
# of every schedule-group field plus the supported work-mode enumeration.
# Read-only; the write itself still goes to ``_SCHEDULE_ENDPOINT``.
_SCHEDULER_PROPERTIES_ENDPOINT = "/op/v3/device/scheduler/get"

# The Mode Scheduler *master switch*: schedule groups only drive the
# inverter while it is on.  Distinct from the per-group ``enable`` field
# and from the group list itself — removing every group does not turn the
# switch off (issue #16).
_SCHEDULER_FLAG_ENDPOINT = "/op/v1/device/scheduler/get/flag"
_SCHEDULER_SET_ENDPOINT = "/op/v0/device/scheduler/set"

# The device's own settings, reached *without* the Mode Scheduler.  Note
# these take ``sn`` where the scheduler endpoints take ``deviceSN``.
_SETTING_GET_ENDPOINT = "/op/v0/device/setting/get"
_SETTING_SET_ENDPOINT = "/op/v0/device/setting/set"

# Setting keys used by this module.  ``WorkMode`` here is the *device*
# work mode, not a schedule group's ``workMode`` field.
_SETTING_WORK_MODE = "WorkMode"

# Mapping from schedule-group field name to the (lower-cased) key the API
# uses for it in the declared ``properties`` map.  Only fields whose value
# this module chooses are listed; the time fields are already bounded by
# construction (C-009).
_CLAMPED_FIELDS: tuple[tuple[str, str], ...] = (
    ("fdPwr", "fdpwr"),
    ("fdSoc", "fdsoc"),
    ("minSocOnGrid", "minsocongrid"),
)

_PLACEHOLDER_WORK_MODES = frozenset({"Invalid", ""})


def _parse_real_time(result: Any) -> dict[str, Any]:
    """Extract variable->value map from the real-time query response.

    The API returns [{datas: [{variable, value, ...}, ...], deviceSN, time}].
    """
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict) and "datas" in first:
            return {d["variable"]: d["value"] for d in first["datas"]}
        if isinstance(first, dict) and "variable" in first:
            return {d["variable"]: d["value"] for d in result}
    return {}


class Inverter:
    """Manage a single FoxESS inverter."""

    # Multiplier applied to the inverter's rated capacity (kW) to derive
    # the fdPwr value in watts.  The FoxESS app uses capacity * 1050.
    CAPACITY_TO_FD_PWR = 1050

    def __init__(self, client: FoxESSClient, serial_number: str) -> None:
        self.client = client
        self.sn = serial_number
        self._max_power_w: int | None = None
        self._device_type: str | None = None
        self._scheduler_properties: dict[str, Any] | None = None
        self._scheduler_properties_probed = False
        self._warned_work_modes: set[str] = set()
        self._warned_scheduler_enable = False

    @property
    def max_power_w(self) -> int:
        """Inverter rated power in watts — the ceiling for ``fdPwr`` writes.

        Derived from the ``capacity`` field of device detail (kW) via the
        ``capacity * 1050`` factor the FoxESS app uses, then clamped to the
        maximum ``fdPwr`` the device itself declares (see
        :attr:`fd_pwr_limit_w`).

        The ``* 1050`` factor was reverse-engineered from a KH10, whose
        declared ceiling happens to be exactly ``capacity * 1050``.  Other
        model families (H3, EVO) declare the plain nameplate rating, so the
        factor overshoots and *every* schedule write — including the SelfUse
        baseline written on session teardown — is rejected with errno 40257
        (issues #12, #14, #17).  Clamping is one-directional: the declared
        ceiling can only lower the value, never raise it above the rating.
        """
        if self._max_power_w is None:
            detail = self.get_detail()
            capacity_kw: float = detail.get("capacity", 0)
            if capacity_kw <= 0:
                raise RuntimeError(
                    "Could not determine inverter capacity from device detail"
                )
            self._device_type = detail.get("deviceType")
            rated_w = int(capacity_kw * self.CAPACITY_TO_FD_PWR)
            declared = self.fd_pwr_limit_w
            if declared is not None and declared < rated_w:
                _LOGGER.info(
                    "Inverter %s declares a maximum fdPwr of %d W; using that "
                    "instead of the %d W derived from its %s kW rating",
                    self._device_type or self.sn,
                    declared,
                    rated_w,
                    capacity_kw,
                )
                rated_w = declared
            self._max_power_w = rated_w
        return self._max_power_w

    @property
    def device_type(self) -> str | None:
        """Inverter model name, cached from device detail.

        ``None`` until :attr:`max_power_w` has been read (which happens at
        integration setup), or when the device omits ``deviceType``.  The
        model name is informational only — no payload shaping depends on it.
        """
        return self._device_type

    # --- Device-declared scheduler limits ---

    @property
    def scheduler_properties(self) -> dict[str, Any]:
        """Per-device schedule-group field metadata, or ``{}``.

        Probed once, lazily.  Any failure (endpoint absent on older
        firmware/regions, transport error, unexpected shape) yields ``{}``
        so callers fall back to the previous capacity-only behaviour rather
        than failing setup — this must never turn a working install into a
        broken one.
        """
        if not self._scheduler_properties_probed:
            self._scheduler_properties_probed = True
            self._scheduler_properties = self._probe_scheduler_properties()
        return self._scheduler_properties or {}

    def _probe_scheduler_properties(self) -> dict[str, Any] | None:
        try:
            result: Any = self.client.post(
                _SCHEDULER_PROPERTIES_ENDPOINT, {"deviceSN": self.sn}
            )
        except Exception:  # noqa: BLE001 — capability probe is best-effort
            _LOGGER.debug(
                "Could not read declared scheduler properties from %s; "
                "falling back to the capacity-derived fdPwr ceiling",
                _SCHEDULER_PROPERTIES_ENDPOINT,
                exc_info=True,
            )
            return None
        if not isinstance(result, dict):
            return None
        props = result.get("properties")
        return props if isinstance(props, dict) else None

    def _declared_range(self, field: str) -> tuple[int, int] | None:
        """Return the (min, max) the device declares for *field*, or None."""
        entry = self.scheduler_properties.get(field)
        if not isinstance(entry, dict):
            return None
        raw = entry.get("range")
        if not isinstance(raw, dict):
            return None
        low, high = raw.get("min"), raw.get("max")
        if not isinstance(low, int | float) or not isinstance(high, int | float):
            return None
        if high <= 0 or high < low:
            return None
        return int(low), int(high)

    @property
    def fd_pwr_limit_w(self) -> int | None:
        """Maximum ``fdPwr`` the device declares it accepts, or ``None``."""
        declared = self._declared_range("fdpwr")
        return declared[1] if declared else None

    @property
    def declared_work_modes(self) -> frozenset[str]:
        """Work modes the device declares it supports, or an empty set."""
        entry = self.scheduler_properties.get("workmode")
        if isinstance(entry, dict):
            modes = entry.get("enumList")
            if isinstance(modes, list):
                return frozenset(str(m) for m in modes)
        return frozenset()

    @property
    def declared_limits_snapshot(self) -> dict[str, Any] | None:
        """Already-probed declared limits, or ``None`` if not probed yet.

        Never triggers I/O, so it is safe to read from the event loop (the
        diagnostics platform).  ``None`` distinguishes "never probed" from
        "probed and the device declared nothing".
        """
        if not self._scheduler_properties_probed:
            return None
        return {
            "fd_pwr_max_w": self.fd_pwr_limit_w,
            "work_modes": sorted(self.declared_work_modes) or None,
        }

    def _clamp_to_declared_ranges(
        self, groups: list[ScheduleGroup]
    ) -> list[ScheduleGroup]:
        """Clamp group values into the ranges the device declares.

        Out-of-range values are rejected wholesale with errno 40257, which
        the user sees as an opaque service failure.  Clamping keeps the
        operation working with the closest value the hardware accepts,
        which is always the safe direction: ``fdPwr`` can only come down
        (less export, never more import) and ``fdSoc`` / ``minSocOnGrid``
        can only come up (a higher reserve — P-002 over P-003/P-004).

        Caller dicts are never mutated: the cloud adapter caches its groups
        and re-writes them each tick, so clamping must be idempotent and
        must not overwrite the session's intended values.
        """
        ranges = {api: self._declared_range(prop) for api, prop in _CLAMPED_FIELDS}
        if not any(ranges.values()):
            return list(groups)

        clamped: list[ScheduleGroup] = []
        for group in groups:
            adjusted: dict[str, Any] = dict(group)
            for key, bounds in ranges.items():
                value = adjusted.get(key)
                if bounds is None or not isinstance(value, int | float):
                    continue
                if isinstance(value, bool):  # pragma: no cover - defensive
                    continue
                new_value = int(min(max(value, bounds[0]), bounds[1]))
                if new_value != value:
                    _LOGGER.info(
                        "Clamping schedule %s from %s to %d "
                        "(device-declared range %d-%d)",
                        key,
                        value,
                        new_value,
                        bounds[0],
                        bounds[1],
                    )
                    adjusted[key] = new_value
            # C-008: minSocOnGrid <= fdSoc must survive the clamp.
            fd_soc, min_soc = adjusted.get("fdSoc"), adjusted.get("minSocOnGrid")
            if (
                isinstance(fd_soc, int | float)
                and isinstance(min_soc, int | float)
                and min_soc > fd_soc
            ):
                adjusted["minSocOnGrid"] = int(fd_soc)
            clamped.append(adjusted)  # type: ignore[arg-type]
        return clamped

    def _warn_unsupported_work_modes(self, groups: list[ScheduleGroup]) -> None:
        """Log once per mode the device does not list as supported (C-020).

        Advisory only — the write is still attempted, because refusing on a
        device that under-reports its enumeration would break an install
        that works today.
        """
        declared = self.declared_work_modes
        if not declared:
            return
        for group in groups:
            mode = str(group.get("workMode", ""))
            if mode in _PLACEHOLDER_WORK_MODES or mode in declared:
                continue
            if mode in self._warned_work_modes:
                continue
            self._warned_work_modes.add(mode)
            _LOGGER.warning(
                "Inverter %s does not list work mode '%s' among the modes it "
                "supports (%s); the scheduler write will probably be rejected "
                "with FoxESS API error 40257",
                self._device_type or self.sn,
                mode,
                ", ".join(sorted(declared)),
            )

    @classmethod
    def auto_detect(cls, client: FoxESSClient) -> Inverter:
        """Create an Inverter for the first device found on the account."""
        result: Any = client.post(
            "/op/v0/device/list", {"currentPage": 1, "pageSize": 10}
        )
        devices: list[dict[str, Any]] = result.get("data", [])
        if not devices:
            raise RuntimeError("No devices found on this account")
        sn: str = devices[0]["deviceSN"]
        return cls(client, sn)

    # --- State of Charge ---

    def get_soc(self) -> float:
        """Get the current battery state of charge (%)."""
        result: Any = self.client.post(
            "/op/v0/device/real/query",
            {"sn": self.sn, "variables": ["SoC"]},
        )
        data = _parse_real_time(result)
        if "SoC" in data:
            return float(data["SoC"])
        raise RuntimeError("SoC not found in API response")

    def get_battery_status(self) -> dict[str, Any]:
        """Get battery status: SoC, power, temperature, residual energy."""
        variables = [
            "SoC",
            "batChargePower",
            "batDischargePower",
            "batTemperature",
            "batVolt",
            "batCurrent",
        ]
        result: Any = self.client.post(
            "/op/v0/device/real/query",
            {"sn": self.sn, "variables": variables},
        )
        return _parse_real_time(result)

    # --- Min SoC Settings ---

    def get_min_soc(self) -> MinSocSettings:
        """Get min SoC settings."""
        result: MinSocSettings = self.client.get(
            "/op/v0/device/battery/soc/get", {"sn": self.sn}
        )
        return result

    def set_min_soc(self, min_soc: int = 10, min_soc_on_grid: int = 10) -> None:
        """Set min SoC thresholds."""
        self.client.post(
            "/op/v0/device/battery/soc/set",
            {"sn": self.sn, "minSoc": min_soc, "minSocOnGrid": min_soc_on_grid},
        )

    # --- Scheduler / Work Mode ---

    def get_schedule(self) -> dict[str, Any]:
        """Get the current scheduler configuration.

        Returns a dict with 'enable' (int) and 'groups' (list of ScheduleGroup).
        When no scheduler is configured (e.g. mode set via app), the API
        returns ``null``; this method normalises that to an empty schedule.
        """
        result: Any = self.client.post(
            "/op/v0/device/scheduler/get", {"deviceSN": self.sn}
        )
        if result is None:
            return {"enable": 0, "groups": []}
        sched: dict[str, Any] = result
        return sched

    # --- Mode Scheduler master switch ---

    def get_scheduler_flag(self) -> dict[str, bool]:
        """Whether Mode Scheduler is enabled, and whether it is supported.

        Returns ``{"enable": bool, "support": bool}``.  ``support`` is
        False on devices with no scheduler at all (e.g. a batteryless
        micro-inverter).

        Raises:
            ValueError: the response was not the expected shape.  This used
                to degrade to all-False, which made one malformed reply
                indistinguishable from a device that genuinely has no Mode
                Scheduler.  Handback declined either way, so the *decision*
                was fail-safe — but the reason it logged was a confident
                claim about the user's hardware, sending them to look for a
                firmware limitation that does not exist.  A log that lies is
                worse than one that says "unknown" (C-020, P-005).

                Raising means there is exactly **one** signal for "we do not
                know": an exception.  A failed request and a malformed reply
                are indistinguishable in the only respect that matters, and
                collapsing them onto one mechanism makes it impossible to
                treat unknown as False by forgetting to check a sentinel.
                :meth:`probe_scheduler_support` is the tri-state wrapper for
                callers that want the capability without the exception.
        """
        result: Any = self.client.post(_SCHEDULER_FLAG_ENDPOINT, {"deviceSN": self.sn})
        if not isinstance(result, dict):
            raise ValueError(
                f"inverter {self._device_type or self.sn} returned an "
                f"unexpected Mode Scheduler flag response ({result!r}); "
                "whether it supports Mode Scheduler is unknown"
            )
        return {
            "enable": bool(result.get("enable", False)),
            "support": bool(result.get("support", False)),
        }

    def probe_scheduler_support(self) -> bool | None:
        """Does this device support Mode Scheduler?  ``None`` if unknown.

        The tri-state the handback policy consumes.  ``True``/``False`` are
        the device's own answer; ``None`` means the question could not be
        answered — the request failed, or the reply was not the expected
        shape.  Only a caller that can tell those apart can log an honest
        reason, which is the whole point (C-020, P-005).

        Read-only and best-effort: it never raises and never writes, so it
        is safe to call on a path that must not be blocked by it.
        """
        try:
            return self.get_scheduler_flag()["support"]
        except Exception:  # noqa: BLE001 — "unknown" is a valid answer here
            _LOGGER.debug(
                "Could not determine Mode Scheduler support for inverter %s",
                self._device_type or self.sn,
                exc_info=True,
            )
            return None

    def set_scheduler_enabled(self, enable: bool) -> None:
        """Turn the Mode Scheduler master switch on or off.

        Raises on failure — callers that must not be blocked by it use
        :meth:`_ensure_scheduler_enabled` instead.
        """
        self.client.post(
            _SCHEDULER_SET_ENDPOINT,
            {"deviceSN": self.sn, "enable": 1 if enable else 0},
        )

    def _ensure_scheduler_enabled(self) -> None:
        """Turn the master switch on before a schedule write, best-effort.

        **Do not delete this as a redundant call.**  Schedule groups only
        drive the inverter while the Mode Scheduler master switch is on.
        Removing every group does *not* turn the switch off (issue #16 —
        FoxCloud still showed the inverter as scheduler-controlled with no
        groups left), but the converse is **unverified**: nobody has
        confirmed whether ``scheduler/enable`` turns the switch back *on*
        from off, and confirming it would mean writing to a production
        home battery.

        Once anything turns the switch off — the scheduler handback
        feature, the FoxESS app, the web portal — a session that relied on
        that coupling would write a schedule that silently never fires:
        errno 0, nothing surfaced to the user, no mode change, no
        discharge (P-003, P-005, C-020).  Enabling the switch explicitly
        removes the dependency on the answer.  The call is idempotent, so
        the cost is one extra request per schedule write — at the 60 s
        discharge-pacing cadence, well inside the observed rate-limit
        budget (docs/api/foxess-cloud-api.md §7), and trivial next to a
        session that never fires.

        Failure is tolerated deliberately: the endpoint is absent on some
        firmware and regions, and a device that reports ``support: false``
        rejects it outright.  A failure here must never abort a write that
        would otherwise have worked, so it is logged and the write
        proceeds — exactly the pre-existing behaviour.  Warned once per
        inverter instance, then demoted to debug: pacing writes the
        schedule every 60 s, so a warning per write would bury the log of
        every install whose firmware lacks the endpoint, while total
        silence would make the handback failure mode undiagnosable.
        """
        try:
            self.set_scheduler_enabled(True)
        except Exception:  # noqa: BLE001 — best-effort; never block the write
            if not self._warned_scheduler_enable:
                self._warned_scheduler_enable = True
                _LOGGER.warning(
                    "Could not turn the Mode Scheduler master switch on via %s "
                    "on inverter %s; writing the schedule anyway.  If work "
                    "mode changes appear to be ignored, check that Mode "
                    "Scheduler is enabled in the FoxESS app",
                    _SCHEDULER_SET_ENDPOINT,
                    self._device_type or self.sn,
                    exc_info=True,
                )
            else:
                _LOGGER.debug(
                    "Mode Scheduler master switch still not settable via %s",
                    _SCHEDULER_SET_ENDPOINT,
                    exc_info=True,
                )

    # --- Direct device settings (off-scheduler) ---

    def get_setting(self, key: str) -> dict[str, Any]:
        """Read one inverter setting, with its declared range/enumeration.

        Reaches the device's own settings rather than the Mode Scheduler,
        so the values it reports are what the inverter does when no
        schedule group is in force.

        Response shape varies by key — ``WorkMode`` carries an ``enumList``
        and no ``range``, the SoC keys the reverse — and ``value`` is a
        string even for numeric settings.  Nothing is validated here: an
        unexpected shape is returned as-is (a non-dict result degrades to
        ``{}``) so callers decide what a missing declaration means, rather
        than having an exception decided for them.
        """
        result: Any = self.client.post(
            _SETTING_GET_ENDPOINT, {"sn": self.sn, "key": key}
        )
        return result if isinstance(result, dict) else {}

    def set_setting(self, key: str, value: str) -> None:
        """Write one inverter setting.

        *value* is a string even for numeric settings, matching what
        :meth:`get_setting` reads back.  Raises on failure.
        """
        self.client.post(
            _SETTING_SET_ENDPOINT,
            {"sn": self.sn, "key": key, "value": value},
        )

    def set_work_mode_direct(self, mode: str) -> None:
        """Set the work mode *without* the scheduler.

        **Not** :meth:`set_work_mode`, which writes a whole-day *schedule
        group*.  This writes the device's own ``WorkMode`` *setting*, which
        governs only while no schedule group is in force — so while a
        session's group is active (this integration writes 00:00-23:59
        groups) this call has no visible effect whatsoever.  The names are
        kept far apart deliberately: confusing them would silently move
        control between the scheduler and the direct settings.

        *mode* is a plain ``str``, not :class:`WorkMode`, because the two
        enumerations genuinely differ.  The direct one (observed on a KH10:
        PeakShaving, Feedin, Backup, SelfUse) offers no ForceCharge or
        ForceDischarge, which is precisely why smart sessions must keep
        using the scheduler and this surface can only govern the idle
        state; it also offers PeakShaving, which :class:`WorkMode` has no
        member for.

        Raises:
            ValueError: *mode* is outside the enumeration the device
                declares.  Unlike the schedule path — which warns and
                writes anyway, because refusing there could break installs
                that work today — this refuses, because writing an
                undeclared value earns an opaque errno 40257 and nothing
                yet depends on this surface.  A device that declares no
                enumeration at all (older firmware) is not blocked: there
                is no basis on which to refuse.
        """
        declared = self.get_setting(_SETTING_WORK_MODE).get("enumList")
        if isinstance(declared, list) and declared and mode not in declared:
            raise ValueError(
                f"inverter {self._device_type or self.sn} does not declare work "
                f"mode {mode!r} as directly settable; it accepts "
                f"{sorted(str(m) for m in declared)}"
            )
        self.set_setting(_SETTING_WORK_MODE, mode)

    def _post_schedule(self, groups: list[ScheduleGroup], call_site: str) -> None:
        """POST groups to ``/scheduler/enable`` and emit a SCHEDULE_WRITE event.

        Every inverter schedule write flows through this helper so replay
        harnesses observe one ``schedule_write`` event per API call,
        regardless of whether the caller used :meth:`set_schedule` or
        :meth:`set_work_mode`.  Payload shape matches the docstring in
        :mod:`smart_battery.events`: the groups list as written to the
        API plus whatever the API returned.  The POST runs first so a
        failing write does not produce a misleading event.

        This is also the single choke point where every group is clamped
        to the ranges the device declares it accepts, so no caller — not
        even a user-supplied ``power:`` service argument — can produce a
        payload the inverter rejects with errno 40257, and where the Mode
        Scheduler master switch is turned on so the groups actually take
        effect (see :meth:`_ensure_scheduler_enabled`).
        """
        payload = self._clamp_to_declared_ranges(groups)
        self._warn_unsupported_work_modes(payload)
        # Groups are inert while the master switch is off, and whether this
        # POST implies the switch is unverified — so assert it explicitly.
        self._ensure_scheduler_enabled()
        response = self.client.post(
            _SCHEDULE_ENDPOINT,
            {"deviceSN": self.sn, "groups": payload},
        )
        # Copy the groups list defensively so downstream payload
        # consumers cannot mutate the caller's data through the event.
        emit_event(
            _LOGGER,
            SCHEDULE_WRITE,
            groups=[dict(g) for g in payload],
            response=response,
            endpoint=_SCHEDULE_ENDPOINT,
            call_site=call_site,
        )

    def set_work_mode(
        self,
        mode: WorkMode,
        min_soc_on_grid: int = 11,
        fd_soc: int = 11,
        fd_pwr: int | None = None,
        api_min_soc: int = 11,
    ) -> None:
        """Set the inverter to a single work mode for the entire day.

        Writes a *schedule group* covering 00:00-23:59, so it needs the Mode
        Scheduler master switch on (see :meth:`_ensure_scheduler_enabled`).
        **Not** :meth:`set_work_mode_direct`, which writes the device's
        ``WorkMode`` setting instead and cannot express a forced mode.  All
        session control goes through this method; only the idle state can go
        through the direct one.

        Args:
            mode: The work mode to set.
            min_soc_on_grid: Minimum SoC to maintain while on-grid (%).
            fd_soc: Target SoC for force discharge, stop at this level (%).
            fd_pwr: Power limit (watts). None uses inverter rated power.
            api_min_soc: Minimum fdSoc accepted by the API (default 11).

        The FoxESS API requires ``fdSoc >= api_min_soc`` and
        ``minSocOnGrid <= fdSoc``.
        """
        if fd_pwr is None:
            fd_pwr = self.max_power_w

        # ForceCharge typically wants a high target SoC
        if mode == WorkMode.FORCE_CHARGE and fd_soc <= api_min_soc:
            fd_soc = 100

        fd_soc = max(fd_soc, api_min_soc)
        min_soc_on_grid = min(min_soc_on_grid, fd_soc)

        group: ScheduleGroup = {
            "enable": 1,
            "startHour": 0,
            "startMinute": 0,
            "endHour": 23,
            "endMinute": 59,
            "workMode": mode.value,
            "minSocOnGrid": min_soc_on_grid,
            "fdSoc": fd_soc,
            "fdPwr": fd_pwr,
        }
        self._post_schedule([group], call_site="Inverter.set_work_mode")

    def set_schedule(self, groups: list[ScheduleGroup]) -> None:
        """Set arbitrary scheduler time segments for fine-grained control.

        Each group dict should have: enable, startHour, startMinute,
        endHour, endMinute, workMode, minSocOnGrid, fdSoc, fdPwr.
        """
        self._post_schedule(list(groups), call_site="Inverter.set_schedule")

    # --- Convenience methods ---

    def self_use(self, min_soc_on_grid: int = 11, api_min_soc: int = 11) -> None:
        """Switch to self-use mode (default operating mode)."""
        self.set_work_mode(
            WorkMode.SELF_USE,
            min_soc_on_grid=min_soc_on_grid,
            api_min_soc=api_min_soc,
        )

    def force_charge(self, min_soc_on_grid: int = 11, target_soc: int = 100) -> None:
        """Force charge the battery from grid + PV.

        Args:
            min_soc_on_grid: Minimum SoC while on-grid (%).
            target_soc: Charge up to this SoC level (%).
        """
        self.set_work_mode(
            WorkMode.FORCE_CHARGE,
            min_soc_on_grid=min_soc_on_grid,
            fd_soc=target_soc,
        )

    def force_discharge(
        self,
        min_soc: int = 11,
        power: int | None = None,
        min_soc_on_grid: int = 11,
        api_min_soc: int = 11,
    ) -> None:
        """Force discharge the battery.

        Args:
            min_soc: Stop discharging at this SoC level (%).
            power: Discharge power limit in watts. None uses inverter rated power.
            min_soc_on_grid: Minimum SoC while on-grid (%).
        """
        self.set_work_mode(
            WorkMode.FORCE_DISCHARGE,
            min_soc_on_grid=min_soc_on_grid,
            fd_soc=min_soc,
            fd_pwr=power,
            api_min_soc=api_min_soc,
        )

    # --- Query current mode ---

    def get_current_mode(self, now: datetime.datetime | None = None) -> WorkMode | None:
        """Get the work mode that is active right now.

        Checks enabled schedule groups against the current time and returns
        the mode of the group whose window contains *now*.  Falls back to
        the first enabled group if no group matches the current time (e.g.
        a full-day 00:00-23:59 window).  Returns ``None`` when no groups
        are enabled.
        """
        schedule = self.get_schedule()
        groups: list[dict[str, Any]] = schedule.get("groups", [])
        enabled = [g for g in groups if g.get("enable") == 1]
        if not enabled:
            return None

        if now is None:
            now = datetime.datetime.now()
        cur_minutes = now.hour * 60 + now.minute

        for group in enabled:
            start = group.get("startHour", 0) * 60 + group.get("startMinute", 0)
            end = group.get("endHour", 0) * 60 + group.get("endMinute", 0)
            if start <= cur_minutes < end:
                try:
                    return WorkMode(group.get("workMode", ""))
                except ValueError:
                    return None
            # Handle midnight-wrapping windows (e.g. 22:00-06:00)
            if start > end and (cur_minutes >= start or cur_minutes < end):
                try:
                    return WorkMode(group.get("workMode", ""))
                except ValueError:
                    return None

        # No enabled group covers the current time — inverter is in SelfUse
        return None

    def get_status_summary(self) -> dict[str, Any]:
        """Get a combined summary of current mode, SoC, and battery state."""
        battery = self.get_battery_status()
        current_mode = self.get_current_mode()
        min_soc = self.get_min_soc()
        return {
            "mode": current_mode.value if current_mode else "Unknown",
            "soc": battery.get("SoC"),
            "charge_power_kw": battery.get("batChargePower"),
            "discharge_power_kw": battery.get("batDischargePower"),
            "temperature_c": battery.get("batTemperature"),
            "min_soc": min_soc.get("minSoc"),
            "min_soc_on_grid": min_soc.get("minSocOnGrid"),
        }

    # --- Plant / Device Info ---

    def get_plant_id(self) -> str:
        """Discover the plantId for this device via the Open API."""
        result: Any = self.client.post(
            "/op/v0/plant/list", {"currentPage": 1, "pageSize": 10}
        )
        plants: list[dict[str, Any]] = result.get("data", [])
        if not plants:
            raise RuntimeError("No plants found on this account")
        return str(plants[0]["stationID"])

    def get_detail(self) -> dict[str, Any]:
        """Get device detail including battery model and capacity."""
        result: dict[str, Any] = self.client.get(
            "/op/v0/device/detail", {"sn": self.sn}
        )
        return result

    def get_real_time(self, variables: list[str]) -> dict[str, Any]:
        """Query arbitrary real-time variables."""
        result: Any = self.client.post(
            "/op/v0/device/real/query",
            {"sn": self.sn, "variables": variables},
        )
        return _parse_real_time(result)
