"""A rejected inverter write must be diagnosable from the download alone.

Issue #17 (H3-12.0-E, Europe, 8.6 kWh) reports ``FoxESS API error 40257:
Parameters do not meet expectations`` on 1.0.22-beta.4 — *after* the C-042
declared-range clamp shipped.  Their diagnostics prove the clamp works
(``max_power_w`` 12000, not the 12600 the ``capacity x 1050`` heuristic
would give) and that ``ForceCharge`` is in the declared enumeration, so
some *other* schedule-group parameter is out of range.  The download
cannot say which, because of three observability gaps:

1. The listener's retry path caught the exception, logged it and returned
   without calling ``record_operational_error`` — so ``recent_errors``
   was **empty** despite repeated failures that aborted the session.
   That is the single most important error class in the integration, and
   it defeated D-059 and C-026 exactly where they matter most.
2. The rejected payload was retained nowhere.  It existed only in the
   ``schedule_write`` event, which is emitted *after* a successful POST.
3. ``scheduler_limits`` reported two derived fields (``fd_pwr_max_w``,
   ``work_modes``) out of the full ``properties`` map the device declares
   via ``POST /op/v3/device/scheduler/get`` — so the declared ``fdsoc`` /
   ``minsocongrid`` ranges, any of which could be the cause, were invisible.

These tests are observability-only: no clamp is added, no control
decision changes, and C-024's circuit-breaker semantics are pinned so
recording cannot perturb them.

Layering (C-021 / C-039 / C-040):

* ``TestListenerRecordsAdapterFailures`` and ``TestBufferPolicy`` exercise
  ``smart_battery/listeners.py`` — brand-agnostic code — through
  :class:`smart_battery.testing.FakeAdapter`, never a brand adapter.
* ``TestFoxESSRetainsRejectedPayload`` and ``TestDeclaredLimitsInDiagnostics``
  drive the real ``FoxESSClient`` / ``Inverter`` over HTTP against the
  simulator (C-028, fresh instance per test).
* ``TestGlueLayer`` closes the gap between the two: the brand-agnostic
  listener recording the payload that the *real* brand adapter attached.
  Both halves passing in isolation is what let the previous
  export-limit regression through (P-001 postmortem).
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from smart_battery.const import (
    CIRCUIT_BREAKER_TICKS_BEFORE_ABORT,
    MAX_CONSECUTIVE_ADAPTER_ERRORS,
)
from smart_battery.domain_data import SmartBatteryDomainData
from smart_battery.listeners import _with_circuit_breaker
from smart_battery.testing import FakeAdapter
from smart_battery.types import WorkMode

if TYPE_CHECKING:
    from .conftest import SimulatorHandle

_DOMAIN = "sb_agnostic_test"

# The reporter's failure, verbatim from issue #17.
_ERRNO = 40257
_API_MESSAGE = "Parameters do not meet expectations"

# The C-039 seam: the brand layer knows what it tried to write, the
# brand-agnostic listener that catches the exception does not and must not
# import a brand module to find out.  So the brand annotates the exception
# it re-raises with this attribute and the listener reads it back.  Named
# here as a literal on purpose — it is the observable contract between the
# two layers, and asserting the literal is what stops the two sides
# drifting apart behind a shared constant.
_CONTEXT_ATTR = "diagnostic_context"


# ---------------------------------------------------------------------------
# Brand-agnostic rig (C-040): FakeAdapter + the real listener helper
# ---------------------------------------------------------------------------


@dataclass
class RejectingAdapter(FakeAdapter):
    """FakeAdapter whose ``apply_mode`` raises, like a rejected write.

    Strictly a :class:`~smart_battery.adapter.InverterAdapter`: it adds no
    brand-specific surface, only a failure mode every brand can produce.
    ``attached`` is whatever a brand layer chose to annotate the exception
    with, so this fake can model the FoxESS 40257 case without importing
    FoxESS.
    """

    error: BaseException | None = None
    attached: dict[str, Any] | None = None

    async def apply_mode(
        self,
        hass: Any,
        mode: WorkMode,
        power_w: int | None = None,
        fd_soc: int = 11,
    ) -> None:
        await super().apply_mode(hass, mode, power_w, fd_soc)
        if self.error is not None:
            if self.attached is not None:
                setattr(self.error, _CONTEXT_ATTR, dict(self.attached))
            raise self.error


class _Errno40257(Exception):
    """Stand-in for a brand client's API error, carrying an ``errno``."""

    def __init__(self, errno: int, msg: str) -> None:
        self.errno = errno
        self.msg = msg
        super().__init__(f"API error {errno}: {msg}")


_ATTEMPTED_GROUPS: list[dict[str, Any]] = [
    {
        "enable": 1,
        "startHour": 2,
        "startMinute": 0,
        "endHour": 5,
        "endMinute": 30,
        "workMode": "ForceCharge",
        "minSocOnGrid": 15,
        "fdSoc": 100,
        "fdPwr": 12000,
    }
]


def _attached_write_record() -> dict[str, Any]:
    """What a brand layer attaches to the exception for a rejected write."""
    return {
        "at": "2026-08-30T02:05:00+00:00",
        "endpoint": "/op/v0/device/scheduler/enable",
        "call_site": "Inverter.set_schedule",
        "groups": [dict(g) for g in _ATTEMPTED_GROUPS],
        "exc_type": "FoxESSApiError",
        "error": f"FoxESS API error {_ERRNO}: {_API_MESSAGE}",
        "errno": _ERRNO,
        "api_message": _API_MESSAGE,
    }


def _make_hass() -> tuple[MagicMock, SmartBatteryDomainData]:
    """Minimal hass carrying brand-agnostic domain data."""
    hass = MagicMock()
    dd = SmartBatteryDomainData()
    hass.data = {_DOMAIN: dd}
    return hass, dd


def _fresh_state() -> dict[str, Any]:
    return {"session_id": "sess-1"}


async def _tick(
    hass: MagicMock,
    cur_state: dict[str, Any],
    adapter: RejectingAdapter,
    aborts: list[str],
    *,
    session_label: str = "charge",
) -> None:
    """Drive one listener tick through the real circuit-breaker wrapper."""

    async def _inner(state: dict[str, Any]) -> None:
        await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, 12000, fd_soc=100)

    async def _on_abort() -> None:
        aborts.append(session_label)

    await _with_circuit_breaker(
        cur_state,
        session_label,
        _inner,
        _on_abort,
        hass,
        _DOMAIN,
    )


def _adapter_errors(dd: SmartBatteryDomainData) -> list[dict[str, Any]]:
    return [r for r in dd.recent_errors if r.get("category") == "adapter_error"]


def _breaker_records(dd: SmartBatteryDomainData) -> list[dict[str, Any]]:
    return [r for r in dd.recent_errors if r.get("category") == "circuit_breaker_open"]


# ---------------------------------------------------------------------------
# Gap 1 + 2, brand-agnostic half
# ---------------------------------------------------------------------------


class TestListenerRecordsAdapterFailures:
    """The listener's retry path must feed ``recent_errors`` (D-059, C-026).

    The reporter's buffer was empty while the session failed and aborted.
    The buffer is the only structured error channel a user can send us
    without being asked to enable debug logging, so a silent retry path
    means every 40257 report arrives with no evidence at all.
    """

    @pytest.mark.asyncio
    async def test_first_failure_lands_in_recent_errors(self) -> None:
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )

        await _tick(hass, _fresh_state(), adapter, [])

        assert len(dd.recent_errors) == 1, (
            "a failing adapter write must be recorded to the diagnostics "
            f"buffer; buffer={list(dd.recent_errors)}"
        )
        rec = dd.recent_errors[0]
        assert rec["category"] == "adapter_error"
        assert rec["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_record_carries_errno_message_and_attempted_payload(self) -> None:
        """The three facts triage needs, in one structured record."""
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )

        await _tick(hass, _fresh_state(), adapter, [])

        rec = dd.recent_errors[0]
        ctx = rec["context"]
        # The errno — the thing the reporter quotes and we cannot map back
        # to a field without it.
        assert ctx["errno"] == _ERRNO
        # The device's own message, verbatim.
        assert _API_MESSAGE in rec["exc_str"]
        assert ctx["attempted_write"]["api_message"] == _API_MESSAGE
        # The payload the device rejected — the whole point of the change.
        assert ctx["attempted_write"]["groups"] == _ATTEMPTED_GROUPS
        assert ctx["attempted_write"]["endpoint"] == "/op/v0/device/scheduler/enable"
        assert ctx["attempted_write"]["call_site"] == "Inverter.set_schedule"
        # Which session, and how far into the retry budget.
        assert ctx["session_type"] == "charge"
        assert ctx["consecutive_error_count"] == 1
        assert ctx["max_consecutive"] == MAX_CONSECUTIVE_ADAPTER_ERRORS
        # Self-sufficient log/record: exception type is named too.
        assert rec["exc_type"] == "_Errno40257"

    @pytest.mark.asyncio
    async def test_discharge_sessions_are_recorded_too(self) -> None:
        """Both session families use the same wrapper — pin both."""
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )

        await _tick(hass, _fresh_state(), adapter, [], session_label="discharge")

        assert dd.recent_errors[0]["context"]["session_type"] == "discharge"

    @pytest.mark.asyncio
    async def test_failure_without_attached_context_still_recorded(self) -> None:
        """A brand that attaches nothing must still produce a usable record.

        ``attach_error_context`` is an optional seam: an adapter that never
        calls it (a plain ``TimeoutError`` from an HTTP layer, say) must not
        lose its record — otherwise the fix would only work for one brand's
        one code path (P-006).
        """
        hass, dd = _make_hass()
        adapter = RejectingAdapter(error=TimeoutError("read timed out"))

        await _tick(hass, _fresh_state(), adapter, [])

        rec = dd.recent_errors[0]
        assert rec["exc_type"] == "TimeoutError"
        assert "read timed out" in rec["exc_str"]
        assert rec["context"]["session_type"] == "charge"
        assert "attempted_write" not in rec["context"]

    @pytest.mark.asyncio
    async def test_breaker_opening_is_recorded_as_its_own_event(self) -> None:
        """C-024's escalation must be visible in the buffer, not only in logs.

        The transition from "retrying" to "holding position" is the moment
        the user's session stopped being adjusted.  C-020 says they must be
        able to tell that from the UI/diagnostics without reading logs.
        """
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )
        state = _fresh_state()

        for _ in range(MAX_CONSECUTIVE_ADAPTER_ERRORS):
            await _tick(hass, state, adapter, [])

        breaker = _breaker_records(dd)
        assert len(breaker) == 1, (
            f"circuit-breaker opening not recorded; buffer={list(dd.recent_errors)}"
        )
        assert breaker[0]["severity"] == "error"
        ctx = breaker[0]["context"]
        assert ctx["errno"] == _ERRNO
        assert ctx["attempted_write"]["groups"] == _ATTEMPTED_GROUPS
        assert ctx["consecutive_error_count"] == MAX_CONSECUTIVE_ADAPTER_ERRORS

    @pytest.mark.asyncio
    async def test_successful_ticks_record_nothing(self) -> None:
        """Normal operation must leave the buffer empty.

        A buffer that fills during healthy operation is worse than no
        buffer: the 30 slots would be evicted before anything interesting
        happened.
        """
        hass, dd = _make_hass()
        adapter = RejectingAdapter(error=None)
        state = _fresh_state()

        for _ in range(10):
            await _tick(hass, state, adapter, [])

        assert list(dd.recent_errors) == []
        assert len(adapter.apply_mode_calls) == 10


# ---------------------------------------------------------------------------
# Buffer policy + C-024 integrity
# ---------------------------------------------------------------------------


class TestBufferPolicy:
    """Recording must not flood the 30-slot buffer, and must not perturb C-024.

    Policy under test: consecutive *identical* failures collapse into one
    entry carrying ``repeat`` and ``last_t``; the circuit-breaker opening
    is a separate category so escalation is always visible.  So the entire
    life of a session that fails identically every tick — 3 failures then
    5 hold ticks then abort — costs exactly **2** buffer entries, and a
    session that is replayed and fails the same way again costs **0** more.
    """

    @pytest.mark.asyncio
    async def test_whole_failing_session_costs_two_entries(self) -> None:
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )
        state = _fresh_state()
        aborts: list[str] = []

        total = MAX_CONSECUTIVE_ADAPTER_ERRORS + CIRCUIT_BREAKER_TICKS_BEFORE_ABORT
        for _ in range(total):
            await _tick(hass, state, adapter, aborts)

        assert aborts == ["charge"], "session must still abort exactly once (C-024)"
        assert len(dd.recent_errors) == 2, (
            "a session that fails identically on every tick must cost two "
            f"buffer entries, not {len(dd.recent_errors)}; "
            f"buffer={list(dd.recent_errors)}"
        )
        errors = _adapter_errors(dd)
        assert len(errors) == 1
        assert errors[0]["repeat"] == MAX_CONSECUTIVE_ADAPTER_ERRORS, (
            "the collapsed entry must count the suppressed repeats, so no "
            "information is lost"
        )
        assert errors[0]["last_t"] >= errors[0]["t"]
        assert len(_breaker_records(dd)) == 1

    @pytest.mark.asyncio
    async def test_replayed_sessions_do_not_evict_the_buffer(self) -> None:
        """Ten identically-failing sessions must not fill 30 slots.

        Session replay restarts an aborted session, so the naive
        "append every failure" implementation scales with the number of
        replays — which is exactly how one error class evicts everything
        else that a triage would need to see.
        """
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )
        aborts: list[str] = []
        total = MAX_CONSECUTIVE_ADAPTER_ERRORS + CIRCUIT_BREAKER_TICKS_BEFORE_ABORT

        for _ in range(10):
            state = _fresh_state()
            for _tick_n in range(total):
                await _tick(hass, state, adapter, aborts)

        assert len(aborts) == 10
        assert len(dd.recent_errors) == 2, (
            "80 failing ticks across 10 replayed sessions must still cost "
            f"two entries; buffer={list(dd.recent_errors)}"
        )
        assert _adapter_errors(dd)[0]["repeat"] == 10 * MAX_CONSECUTIVE_ADAPTER_ERRORS
        assert _breaker_records(dd)[0]["repeat"] == 10

    @pytest.mark.asyncio
    async def test_a_different_failure_is_a_different_entry(self) -> None:
        """Collapsing must key on the failure, not on the category.

        If a second, different rejection appears — a different errno, or the
        same errno on a different payload — that is new information and must
        not be swallowed into the first entry's repeat count.
        """
        hass, dd = _make_hass()
        state = _fresh_state()

        first = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )
        await _tick(hass, state, first, [])

        other_payload = _attached_write_record()
        other_payload["groups"][0]["fdSoc"] = 9
        second = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=other_payload,
        )
        await _tick(hass, state, second, [])

        third = RejectingAdapter(error=_Errno40257(41935, "Device offline"))
        await _tick(hass, state, third, [])

        errors = _adapter_errors(dd)
        assert len(errors) == 3, (
            "three distinct failures must be three entries; "
            f"buffer={list(dd.recent_errors)}"
        )
        assert [e.get("repeat") for e in errors] == [1, 1, 1]

    @pytest.mark.asyncio
    async def test_breaker_opens_on_exactly_the_third_consecutive_failure(
        self,
    ) -> None:
        """C-024 unchanged: recording must not shift the escalation point."""
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )
        state = _fresh_state()
        aborts: list[str] = []

        for expected in range(1, MAX_CONSECUTIVE_ADAPTER_ERRORS):
            await _tick(hass, state, adapter, aborts)
            assert state["consecutive_error_count"] == expected
            assert not state.get("circuit_open"), (
                f"breaker must not open on failure {expected} of "
                f"{MAX_CONSECUTIVE_ADAPTER_ERRORS}"
            )

        await _tick(hass, state, adapter, aborts)
        assert state["consecutive_error_count"] == MAX_CONSECUTIVE_ADAPTER_ERRORS
        assert state["circuit_open"] is True
        assert state["circuit_open_ticks"] == 0
        assert "circuit_open_since" in state
        assert aborts == [], "opening the breaker must hold position, not abort"

    @pytest.mark.asyncio
    async def test_breaker_holds_then_aborts_after_five_more_ticks(self) -> None:
        """C-024 unchanged: 5 hold ticks, no adapter calls, then abort."""
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )
        state = _fresh_state()
        aborts: list[str] = []

        for _ in range(MAX_CONSECUTIVE_ADAPTER_ERRORS):
            await _tick(hass, state, adapter, aborts)
        calls_when_open = len(adapter.apply_mode_calls)

        for tick in range(CIRCUIT_BREAKER_TICKS_BEFORE_ABORT - 1):
            await _tick(hass, state, adapter, aborts)
            assert state["circuit_open_ticks"] == tick + 1
            assert aborts == []

        assert len(adapter.apply_mode_calls) == calls_when_open, (
            "the adapter must not be called while the breaker is open"
        )

        await _tick(hass, state, adapter, aborts)
        assert aborts == ["charge"]
        assert dd.smart_error_state is not None
        assert "aborted" in dd.smart_error_state["last_error"]

    @pytest.mark.asyncio
    async def test_recovery_still_resets_the_breaker(self) -> None:
        """A successful tick must clear the count, as before."""
        hass, dd = _make_hass()
        adapter = RejectingAdapter(
            error=_Errno40257(_ERRNO, _API_MESSAGE),
            attached=_attached_write_record(),
        )
        state = _fresh_state()

        for _ in range(MAX_CONSECUTIVE_ADAPTER_ERRORS - 1):
            await _tick(hass, state, adapter, [])
        assert state["consecutive_error_count"] == MAX_CONSECUTIVE_ADAPTER_ERRORS - 1

        adapter.error = None
        await _tick(hass, state, adapter, [])
        assert state["consecutive_error_count"] == 0
        assert not state.get("circuit_open")


# ---------------------------------------------------------------------------
# Gap 2, brand half: the FoxESS write path retains what it sent
# ---------------------------------------------------------------------------


def _make_inv(sim: SimulatorHandle) -> Any:
    from custom_components.foxess_control.foxess.client import FoxESSClient
    from custom_components.foxess_control.foxess.inverter import Inverter

    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
    return Inverter(FoxESSClient("test-api-key", base_url=sim.url), "SIM0001")


def _reject_force_charge(sim: SimulatorHandle) -> None:
    """Make the device reject a ForceCharge write with errno 40257.

    Modelled on issue #17: setup, polling and the declared-range probe all
    succeed, and only the schedule write is refused — wholesale, naming no
    field.  Which parameter the simulated device objects to is deliberately
    *not* the one under test; the test is that we retain enough to find out.
    """
    sim.set(
        device_type="H3-12.0-E",
        max_power_w=12600,
        fd_pwr_max_w=12000,
        max_grid_export_limit_w=5000,
        scheduler_work_modes=[
            "Backup",
            "Feedin",
            "ForceDischarge",
            "PeakShaving",
            "SelfUse",
        ],
    )


class TestFoxESSRetainsRejectedPayload:
    """The groups we sent, the errno and the message must survive the failure."""

    def test_rejected_write_is_retained_on_the_inverter(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        from custom_components.foxess_control.foxess.client import FoxESSApiError

        _reject_force_charge(foxess_sim)
        inv = _make_inv(foxess_sim)

        with pytest.raises(FoxESSApiError):
            inv.force_charge(min_soc_on_grid=15, target_soc=100)

        failure = inv.last_write_failure
        assert failure is not None, (
            "the rejected payload was not retained anywhere a user can send us"
        )
        assert failure["errno"] == _ERRNO
        assert failure["api_message"] == _API_MESSAGE
        assert failure["endpoint"] == "/op/v0/device/scheduler/enable"
        assert failure["call_site"] == "Inverter.set_work_mode"
        assert failure["exc_type"] == "FoxESSApiError"
        groups = failure["groups"]
        assert len(groups) == 1
        # Every field the device could have objected to, as written.
        assert groups[0]["workMode"] == "ForceCharge"
        assert groups[0]["fdPwr"] == 12000
        assert groups[0]["fdSoc"] == 100
        assert groups[0]["minSocOnGrid"] == 15
        assert failure["at"]

    def test_rejected_write_is_attached_to_the_exception(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """The brand-agnostic listener reads the payload off the exception.

        C-039: ``smart_battery/listeners.py`` must not import the FoxESS
        inverter to find out what was attempted, so the brand annotates the
        exception it raises and the listener reads it back.
        """
        from custom_components.foxess_control.foxess.client import FoxESSApiError

        _reject_force_charge(foxess_sim)
        inv = _make_inv(foxess_sim)

        with pytest.raises(FoxESSApiError) as excinfo:
            inv.force_charge(min_soc_on_grid=15, target_soc=100)

        ctx = getattr(excinfo.value, _CONTEXT_ATTR, None)
        assert ctx is not None, (
            "the exception carries no diagnostic context, so the "
            "brand-agnostic listener cannot record what was attempted"
        )
        assert ctx["errno"] == _ERRNO
        assert ctx["groups"][0]["workMode"] == "ForceCharge"
        assert ctx == inv.last_write_failure

    def test_annotation_never_masks_the_original_failure(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Retention is best-effort: the write error must always win.

        An exception that refuses attribute assignment must not be turned
        into an ``AttributeError`` on its way up — that would replace a
        diagnosable API error with a confusing one, and (worse) change which
        exception type the circuit breaker sees.
        """
        inv = _make_inv(foxess_sim)
        _ = inv.max_power_w  # warm the detail + declared-range caches

        class _Hostile(Exception):
            def __setattr__(self, name: str, value: Any) -> None:
                raise AttributeError(f"{name} is read-only")

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise _Hostile("upstream exploded")

        inv.client.post = _boom  # type: ignore[method-assign]

        with pytest.raises(_Hostile):
            inv.self_use(min_soc_on_grid=11)

        # Retention still happened where it could.
        assert inv.last_write_failure is not None
        assert inv.last_write_failure["exc_type"] == "_Hostile"

    def test_retained_payload_holds_no_secrets(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Schedule groups are numbers and a mode name — nothing sensitive.

        ``REDACT_KEYS`` covers serials and tokens by key name, which does
        not help if a secret is embedded in a value, so assert directly.
        """
        from custom_components.foxess_control.foxess.client import FoxESSApiError

        _reject_force_charge(foxess_sim)
        inv = _make_inv(foxess_sim)

        with pytest.raises(FoxESSApiError):
            inv.force_charge(min_soc_on_grid=15, target_soc=100)

        blob = json.dumps(inv.last_write_failure)
        assert "SIM0001" not in blob
        assert "test-api-key" not in blob

    def test_successful_write_retains_no_failure(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Normal operation must not populate the failure field."""
        inv = _make_inv(foxess_sim)

        inv.self_use(min_soc_on_grid=11)

        assert inv.last_write_failure is None
        assert inv.last_write_ok_at is not None

    def test_recovery_is_distinguishable_from_a_stale_failure(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """A historical failure must not read as a current one (C-020).

        Retaining the failure forever without a marker for the last
        *successful* write would make every report look broken.
        """
        from custom_components.foxess_control.foxess.client import FoxESSApiError

        _reject_force_charge(foxess_sim)
        inv = _make_inv(foxess_sim)
        with pytest.raises(FoxESSApiError):
            inv.force_charge(min_soc_on_grid=15, target_soc=100)
        assert inv.last_write_ok_at is None

        inv.self_use(min_soc_on_grid=11)

        assert inv.last_write_failure is not None
        assert inv.last_write_ok_at is not None
        assert inv.last_write_ok_at >= inv.last_write_failure["at"]


# ---------------------------------------------------------------------------
# Gap 3: diagnostics reports the full declared property map
# ---------------------------------------------------------------------------


def _diagnostics_for(inverter: Any) -> dict[str, Any]:
    from custom_components.foxess_control.const import DOMAIN
    from custom_components.foxess_control.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    dd = SimpleNamespace(
        entries={"e1": SimpleNamespace(coordinator=None, inverter=inverter)},
        smart_charge_state=None,
        smart_discharge_state=None,
        smart_error_state=None,
        realtime_ws=None,
        taper_profile=None,
        ws_mode="auto",
        recent_errors=deque(maxlen=30),
        web_session=None,
        plant_id="p1",
        battery_compound_id=None,
    )
    hass = MagicMock()
    hass.data = {DOMAIN: dd}
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {}
    entry.options = {}
    return asyncio.run(async_get_config_entry_diagnostics(hass, entry))


class TestDeclaredLimitsInDiagnostics:
    """``scheduler_limits`` must report everything the device declared.

    Reporting only ``fd_pwr_max_w`` and ``work_modes`` hid the declared
    ``fdsoc`` / ``minsocongrid`` ranges and ``maxGroupCount`` — any of
    which could be the H3-12.0-E's objection.
    """

    def test_full_declared_properties_are_reported(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        _reject_force_charge(foxess_sim)
        inv = _make_inv(foxess_sim)
        _ = inv.max_power_w  # setup warms the probe

        limits = _diagnostics_for(inv)["environment"]["scheduler_limits"]

        # The derived fields existing consumers rely on survive.
        assert limits["fd_pwr_max_w"] == 12000
        assert "ForceDischarge" in limits["work_modes"]
        # ... and the full declared map is now there too.
        props = limits["properties"]
        assert props["fdpwr"]["range"] == {"min": 0.0, "max": 12000.0}
        assert props["fdsoc"]["range"] == {"min": 10.0, "max": 100.0}
        assert props["minsocongrid"]["range"] == {"min": 10.0, "max": 100.0}
        assert props["starthour"]["range"] == {"min": 0.0, "max": 23.0}
        assert props["workmode"]["enumList"] == [
            "Backup",
            "Feedin",
            "ForceDischarge",
            "PeakShaving",
            "SelfUse",
        ]
        assert limits["max_group_count"] == 8

    def test_never_probed_is_null(self, foxess_sim: SimulatorHandle) -> None:
        """``None`` must keep meaning "we never asked", not "nothing declared".

        Reading the snapshot must never trigger I/O — diagnostics runs on
        the event loop.
        """
        inv = _make_inv(foxess_sim)

        assert inv.declared_limits_snapshot is None

    def test_device_declaring_nothing_reports_an_empty_map(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Probed-but-nothing-declared must be distinguishable from unprobed."""
        foxess_sim.set(scheduler_properties_supported=False)
        inv = _make_inv(foxess_sim)
        _ = inv.max_power_w

        limits = _diagnostics_for(inv)["environment"]["scheduler_limits"]

        assert limits is not None, (
            "probed-and-declared-nothing must not be reported as unprobed"
        )
        assert limits["properties"] == {}
        assert limits["fd_pwr_max_w"] is None
        assert limits["work_modes"] is None
        assert limits["max_group_count"] is None

    def test_rejected_payload_reaches_the_diagnostics_download(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """The whole point: the reporter's next download names the payload."""
        from custom_components.foxess_control.foxess.client import FoxESSApiError

        _reject_force_charge(foxess_sim)
        inv = _make_inv(foxess_sim)
        with pytest.raises(FoxESSApiError):
            inv.force_charge(min_soc_on_grid=15, target_soc=100)

        schedule = _diagnostics_for(inv)["schedule"]

        assert isinstance(schedule, dict)
        failure = schedule["last_write_failure"]
        assert failure is not None
        assert failure["errno"] == _ERRNO
        assert failure["api_message"] == _API_MESSAGE
        assert failure["groups"][0]["workMode"] == "ForceCharge"
        assert schedule["last_write_ok_at"] is None


# ---------------------------------------------------------------------------
# Glue layer: real brand adapter -> brand-agnostic listener -> buffer
# ---------------------------------------------------------------------------


class TestGlueLayer:
    """The two halves must actually meet.

    Each half passing in isolation is what let the export-limit P-001
    regression through: the config quadrant and the glue layer were never
    tested together.  This drives the *real* ``FoxESSCloudAdapter`` and
    ``Inverter`` against the simulator through the *real*, brand-agnostic
    circuit-breaker wrapper, and asserts the reporter's evidence appears.
    """

    @pytest.mark.asyncio
    async def test_real_rejected_write_lands_in_recent_errors(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        from custom_components.foxess_control.const import DOMAIN
        from custom_components.foxess_control.domain_data import (
            FoxESSControlData,
            FoxESSEntryData,
        )
        from custom_components.foxess_control.foxess_adapter import FoxESSCloudAdapter

        _reject_force_charge(foxess_sim)
        inv = _make_inv(foxess_sim)

        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
        dd = FoxESSControlData()
        dd.entries["e1"] = FoxESSEntryData(coordinator=None, inverter=inv)
        hass.data = {DOMAIN: dd}

        now = datetime.datetime.now()
        adapter = FoxESSCloudAdapter(
            hass,
            inv,
            min_soc_on_grid=15,
            api_min_soc=11,
            start=now,
            end=now + datetime.timedelta(hours=3),
        )

        state: dict[str, Any] = {"session_id": "s1"}

        async def _inner(_state: dict[str, Any]) -> None:
            await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, 12000, fd_soc=100)

        async def _on_abort() -> None:
            return None

        await _with_circuit_breaker(state, "charge", _inner, _on_abort, hass, DOMAIN)

        assert len(dd.recent_errors) == 1, (
            "a real 40257 rejection during a session must reach the "
            f"diagnostics buffer; buffer={list(dd.recent_errors)}"
        )
        rec = dd.recent_errors[0]
        assert rec["category"] == "adapter_error"
        assert _API_MESSAGE in rec["exc_str"]
        ctx = rec["context"]
        assert ctx["errno"] == _ERRNO
        write = ctx["attempted_write"]
        assert write["api_message"] == _API_MESSAGE
        assert write["groups"][0]["workMode"] == "ForceCharge"
        assert write["groups"][0]["fdPwr"] == 12000
        assert write["endpoint"] == "/op/v0/device/scheduler/enable"
