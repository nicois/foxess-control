"""Executing the scheduler handback, automatically and on request (#16, #4).

This is where the handback feature stops being a decision and starts
touching the user's inverter, so these tests are about **order** and
**failure**, not about whether the happy path works.

Two entry points share one executor: the automatic teardown hook, and the
manual ``foxess_control.disable_scheduler`` action issue #16 asked for by
name (``TestDisableSchedulerAction`` at the bottom).  They differ in
exactly two ways, both deliberate and both pinned here — the action does
not consult the opt-in option (the call *is* the consent), and it *raises*
on every refusal instead of only recording one.  Everything else, guards
included, is the same code.

Four properties, in the order they matter:

1. **Session boundary cleanliness outranks handback (C-025).**  The
   managed override group comes off first and is allowed to succeed even
   if every single handback step fails.  Getting this backwards is the
   difference between "the feature did not finish" and "the inverter is
   force-discharging behind a Mode Scheduler the integration just turned
   off, and can no longer steer it".  ``TestOrderIsLoadBearing`` and
   ``TestFailureNeverBlocksCleanliness`` pin it from both sides.

2. **Never act while a session is active.**  ``plan_handback``'s
   ``session_active`` argument is a *snapshot*, and a caller that plans
   and then acts asynchronously can have a session start in between.  The
   guard is therefore re-checked immediately before the master switch goes
   off, and again immediately after — a session that appeared in that
   window has the switch turned straight back on.  See
   ``TestSessionStartRace``.

3. **A default install issues no new API calls at all.**  Not "no writes":
   *no calls*.  ``TestOptionOffChangesNothing`` records every request the
   teardown makes and asserts none of the endpoints this feature
   introduced appear, and that no ``scheduler/set`` carries ``enable: 0``
   (the same endpoint ``_ensure_scheduler_enabled`` already used before
   this feature existed, so the path alone proves nothing — the body
   does).  This is the upgrade-safety guarantee for hundreds of installs.

4. **A silent failure is worse than no feature.**  A user who sees the
   option on believes their inverter was released.  Every failed step is
   recorded via ``record_operational_error`` *and* left in a
   last-outcome record, and a teardown that fails on every poll must not
   evict the other 29 entries of the diagnostics ring buffer.

Everything drives the real ``FoxESSClient`` / ``Inverter`` over HTTP
against a fresh simulator (C-028) and a real Home Assistant with a real
``Store``, and asserts on observable *device* state — the master switch,
the direct work-mode setting, the persistent Min SoC register, the group
list — never on which method was called.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from custom_components.foxess_control._helpers import (
    STORAGE_KEY,
    STORAGE_VERSION,
    _dd,
)
from custom_components.foxess_control._min_soc_capture import (
    async_setup_min_soc_capture,
)
from custom_components.foxess_control.const import (
    CONF_MIN_SOC_ON_GRID,
    CONF_SCHEDULER_HANDBACK,
    CONF_WORK_MODE_ENTITY,
    DOMAIN,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
    build_config,
)
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter, WorkMode
from custom_components.foxess_control.foxess_adapter import (
    FoxESSCloudAdapter,
    _remove_mode_from_schedule,
)
from custom_components.foxess_control.smart_battery.listeners import (
    cancel_smart_discharge,
)

if TYPE_CHECKING:
    import pathlib

    from .conftest import SimulatorHandle

# --- Endpoints -------------------------------------------------------------
# Spelled out rather than imported from the private constants in
# foxess/inverter.py: the point of TestOptionOffChangesNothing is that a
# default install's HTTP traffic is unchanged, and a test that imports the
# same constant the implementation uses would keep passing if someone
# renamed the endpoint on both sides.
_EP_SCHEDULE_WRITE = "/op/v0/device/scheduler/enable"
_EP_SCHEDULER_SET = "/op/v0/device/scheduler/set"
_EP_SCHEDULER_FLAG = "/op/v1/device/scheduler/get/flag"
_EP_SETTING_GET = "/op/v0/device/setting/get"
_EP_SETTING_SET = "/op/v0/device/setting/set"

# The integration's *configured* min-SoC-on-grid.  Deliberately different
# from every user floor used below, so an implementation that substitutes
# this default instead of restoring what was captured shows up as a wrong
# number rather than as a silent pass (P-002).
_CONFIGURED_MIN_SOC = 11

# The category ``record_operational_error`` is called with, and the keys of
# the last-outcome record.  Part of the contract Task 8's diagnostics
# section will read, so they are pinned here rather than left to drift.
_ERROR_CATEGORY = "scheduler_handback"

# The per-step keys of the outcome record, and the Repair issue id issue #17
# surfaces.  Spelled out rather than imported from ``_handback_teardown`` for
# the same reason as the endpoints above: a Repair issue id is a *user-facing*
# identifier tied to a ``translations/<lang>.json`` entry, so a test that
# imported the constant would keep passing through a rename that orphaned
# every locale's text.
_STEP_DISABLE = "disable_scheduler"
_HANDBACK_UNSUPPORTED_ISSUE = "scheduler_handback_unsupported"

# Every key the last-outcome record must carry, and no others.  Spelled out
# rather than derived, because the whole value of the record is that Task 8's
# diagnostics section can rely on its shape: a record missing a key is one
# the download either omits or renders as a blank, and a blank where "acted"
# should be reads as "did not act".
_OUTCOME_KEYS = {"t", "acted", "reason", "steps", "restored_min_soc_on_grid"}

# The dedupe key the top-level guard records under.  Distinct from the
# per-step keys: this one means "a bug in the handback module", not "the
# inverter refused a write", and the two should never collapse into each
# other in the download.
_DEDUPE_UNEXPECTED = f"{_ERROR_CATEGORY}:unexpected"


@pytest.fixture(autouse=True)
def _disable_throttle() -> None:
    """Disable request throttling (and retry backoff) in tests."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


class RecordingClient(FoxESSClient):
    """A real client that also remembers every request it made.

    Subclassed rather than mocked: the HTTP round trip, signing and
    response parsing are all real, and the recording is a side channel.
    That keeps C-028 intact while making "no new API calls" an assertion
    about traffic rather than about the absence of an exception.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, dict(body or {})))
        return super().post(path, body)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, dict(params or {})))
        return super().get(path, params)

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def bodies_for(self, path: str) -> list[dict[str, Any]]:
        return [body for p, body in self.calls if p == path]

    def index_of_last(self, path: str) -> int:
        for i in range(len(self.calls) - 1, -1, -1):
            if self.calls[i][0] == path:
                return i
        raise AssertionError(f"no request to {path} was made at all")

    def index_of_first(self, path: str, **body_match: Any) -> int:
        for i, (p, body) in enumerate(self.calls):
            if p != path:
                continue
            if all(body.get(k) == v for k, v in body_match.items()):
                return i
        raise AssertionError(f"no request to {path} matching {body_match}")


def _options(**overrides: Any) -> dict[str, Any]:
    """Config-entry options with handback OFF unless a test says otherwise."""
    opts: dict[str, Any] = {CONF_MIN_SOC_ON_GRID: _CONFIGURED_MIN_SOC}
    opts.update(overrides)
    return opts


@pytest_asyncio.fixture  # type: ignore[untyped-decorator]
async def teardown_hass(tmp_path: pathlib.Path) -> Any:
    """A real HomeAssistant with a real Store rooted in *tmp_path*.

    ``HomeAssistant()`` captures the running event loop in ``__init__``, so
    it must be built inside an async context (mirrors
    tests/test_handback_capture.py).  The config dir is per-test so no
    captured floor leaks between tests — C-028's independence rule applies
    to storage as much as to the simulator.

    Handback is **off**, because that is what ships.  Tests that want it on
    call :func:`_enable_handback`.
    """
    from unittest.mock import MagicMock

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.storage import Store

    ha = HomeAssistant(str(tmp_path))
    ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
    ha.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    dd = FoxESSControlData()
    dd.config = build_config(_options())
    dd.store = Store[dict[str, Any]](ha, STORAGE_VERSION, STORAGE_KEY)
    ha.data[DOMAIN] = dd
    # ``hass.config_entries`` is populated by HA's own bootstrap, which an
    # unstarted HomeAssistant has not run, so ``_get_first_entry`` would
    # find None.  A stub entry whose ``options`` track the cached
    # IntegrationConfig keeps C-035 honest: both views agree.
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.options = _options()
    # Assigned through an ``Any`` alias rather than with a ``type: ignore``:
    # HA's own types resolve as ``Any`` in pre-commit's isolated mypy env but
    # concretely here, so a narrow ignore is unused in one of the two and
    # strict mypy rejects it there.
    ha_any: Any = ha
    ha_any.config_entries = MagicMock()
    ha_any.config_entries.async_get_entry = MagicMock(return_value=entry)
    return ha


def _configure(hass: Any, **overrides: Any) -> None:
    """Rebuild the cached IntegrationConfig from *overrides* (C-035)."""
    opts = _options(**overrides)
    _dd(hass).config = build_config(opts)
    hass.config_entries.async_get_entry("entry1").options = opts


def _enable_handback(hass: Any) -> None:
    _configure(hass, **{CONF_SCHEDULER_HANDBACK: True})


def _attach(hass: Any, inv: Inverter) -> None:
    _dd(hass).entries["entry1"] = FoxESSEntryData(inverter=inv)


def _make_inv(sim: SimulatorHandle) -> tuple[Inverter, RecordingClient]:
    client = RecordingClient("test-api-key", base_url=sim.url)
    return Inverter(client, "SIM0001"), client


def _pin_midday(sim: SimulatorHandle) -> None:
    """Pin simulated time inside every full-day window (C-031).

    Whole-day groups run 00:00-23:59 and the simulator matches
    ``start <= now < end``, so at 23:59 exactly no group is in force and
    the active mode would read as the direct setting for one minute a day.
    """
    sim.set(sim_time="2026-01-15T12:00:00+00:00")


def _adapter(hass: Any, inv: Inverter) -> FoxESSCloudAdapter:
    """A cloud adapter shaped like the one a live discharge session uses."""
    now = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=datetime.UTC)
    return FoxESSCloudAdapter(
        hass=hass,
        inverter=inv,
        min_soc_on_grid=_CONFIGURED_MIN_SOC,
        api_min_soc=11,
        start=now,
        end=now + datetime.timedelta(hours=1),
        capacity_kwh=10.0,
    )


def _session_state(session_id: str = "sess-1") -> dict[str, Any]:
    """The minimum smart-discharge state the teardown path reads."""
    now = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=datetime.UTC)
    return {
        "session_id": session_id,
        "start": now,
        "end": now + datetime.timedelta(hours=1),
        "min_soc": 20,
        "discharging_started": True,
    }


async def _start_discharge_session(
    hass: Any, sim: SimulatorHandle, inv: Inverter
) -> FoxESSCloudAdapter:
    """Put a real ForceDischarge group on the device and mark the session live."""
    adapter = _adapter(hass, inv)
    await hass.async_add_executor_job(
        lambda: inv.force_discharge(
            min_soc=20, power=3000, min_soc_on_grid=_CONFIGURED_MIN_SOC
        )
    )
    _dd(hass).smart_discharge_state = _session_state()
    assert _written_groups(sim, WorkMode.FORCE_DISCHARGE), (
        "the ForceDischarge group was not written, so the teardown under "
        "test would have nothing to remove"
    )
    return adapter


async def _teardown_discharge(hass: Any, adapter: FoxESSCloudAdapter) -> None:
    """Tear the session down exactly as the listener does.

    ``_remove_discharge_override`` in smart_battery/listeners.py runs
    ``cancel_smart_discharge`` (which clears ``smart_discharge_state``)
    and *then* ``adapter.remove_override``, so by the time the override
    comes off no session is active — which is precisely why handback can
    hang off that call at all.
    """
    cancel_smart_discharge(hass, DOMAIN)
    await adapter.remove_override(hass, WorkMode.FORCE_DISCHARGE)


def _run_handback(hass: Any, inv: Inverter | None) -> Any:
    """Call the teardown executor directly.

    Imported lazily so that the tests which exercise the *hook* (through
    ``adapter.remove_override``) fail on their behavioural assertions
    rather than on a collection-time ImportError — a RED that says "the
    master switch is still on" is worth more than one that says "no such
    module".
    """
    from custom_components.foxess_control._handback_teardown import (
        async_handback_after_teardown,
    )

    return async_handback_after_teardown(hass, inv)


async def _call_clear_overrides(hass: Any) -> None:
    """Invoke the real ``clear_overrides`` service on a real HomeAssistant.

    Registered and called through ``hass.services``, not by fishing the
    handler out of a MagicMock, so the third teardown path is exercised as
    the user reaches it.
    """
    from custom_components.foxess_control import _register_services

    _register_services(hass)
    await hass.services.async_call(DOMAIN, "clear_overrides", {}, blocking=True)


async def _call_disable_scheduler(hass: Any) -> None:
    """Invoke the real ``disable_scheduler`` action, as a user would.

    Through ``hass.services.async_call`` rather than by importing the
    handler: half the contract of an *action* is that it is registered
    under the name issue #16 asked for and accepts a call with no data, and
    calling the function directly would prove neither.
    """
    from custom_components.foxess_control import _register_services

    _register_services(hass)
    await hass.services.async_call(DOMAIN, "disable_scheduler", {}, blocking=True)


async def _refused_disable_scheduler(hass: Any) -> str:
    """Call the action, require it to *refuse*, and return the reason.

    ``ServiceNotFound`` is a **subclass** of ``ServiceValidationError``, so a
    bare ``pytest.raises(ServiceValidationError)`` around an unregistered
    service passes for entirely the wrong reason.  That is not hypothetical:
    on the first RED run of this class, the one refusal test that asserted
    only the exception type and not its message passed while
    ``disable_scheduler`` did not exist at all.  Excluding it explicitly
    means "the action refused" can never be satisfied by "the action is not
    there", now or after a future rename.
    """
    from homeassistant.exceptions import ServiceNotFound, ServiceValidationError

    with pytest.raises(ServiceValidationError) as excinfo:
        await _call_disable_scheduler(hass)
    assert not isinstance(excinfo.value, ServiceNotFound), (
        "foxess_control.disable_scheduler is not registered, so this is HA "
        "reporting an unknown service rather than the action refusing"
    )
    return str(excinfo.value)


def _written_groups(sim: SimulatorHandle, mode: WorkMode) -> list[dict[str, Any]]:
    return [g for g in sim.state()["schedule_groups"] if g["workMode"] == mode.value]


def _assert_released(sim: SimulatorHandle) -> None:
    """Assert the inverter is back under its own settings (issue #16)."""
    state = sim.state()
    assert state["scheduler_enabled"] is False, (
        "the Mode Scheduler master switch is still on — the inverter is "
        "still scheduler-controlled, which is exactly what issue #16 asks "
        "to be released"
    )
    assert state["work_mode_direct"] == WorkMode.SELF_USE.value, (
        f"the device's own WorkMode setting is {state['work_mode_direct']!r}, "
        "not SelfUse — with the scheduler off this is what the inverter "
        "actually does, so it is the only thing that governs the idle state"
    )
    assert state["work_mode"] == WorkMode.SELF_USE.value, (
        f"the inverter is in {state['work_mode']}, not SelfUse"
    )


def _assert_no_managed_override(sim: SimulatorHandle) -> None:
    """C-025: no managed override group may survive the teardown."""
    for mode in (WorkMode.FORCE_DISCHARGE, WorkMode.FORCE_CHARGE, WorkMode.FEEDIN):
        assert not _written_groups(sim, mode), (
            f"a {mode.value} group survived the teardown — session boundary "
            "cleanliness (C-025) outranks handback entirely"
        )


def _last_handback(hass: Any) -> dict[str, Any]:
    record = _dd(hass).last_handback
    assert record is not None, (
        "no last-handback record was left behind; a handback nobody can see "
        "the outcome of is one the user has to take on faith (C-020, D-059)"
    )
    assert isinstance(record, dict)
    assert set(record) == _OUTCOME_KEYS, (
        f"the last-handback record has keys {sorted(record)}, not "
        f"{sorted(_OUTCOME_KEYS)} — a partially-written record makes the "
        "diagnostics download lie about what happened, which is worse than "
        "the failure it is trying to describe"
    )
    return record


def _assert_outcome_coherent(hass: Any) -> None:
    """The record is absent or complete — never partially written.

    Deliberately weaker than :func:`_last_handback`: on the unexpected-failure
    paths "nothing was recorded" is an acceptable answer, because the record
    keeps whatever it held before (and the timestamp shows it is stale).  A
    *half* record is not acceptable on any path, so that is what this pins.
    """
    record = _dd(hass).last_handback
    if record is None:
        return
    assert isinstance(record, dict)
    assert set(record) == _OUTCOME_KEYS, (
        f"the last-handback record was left half-written: {sorted(record)}"
    )


def _handback_errors(hass: Any) -> list[dict[str, Any]]:
    return [e for e in _dd(hass).recent_errors if e.get("category") == _ERROR_CATEGORY]


def _errors_with_dedupe_key(hass: Any, key: str) -> list[dict[str, Any]]:
    return [e for e in _handback_errors(hass) if e.get("dedupe_key") == key]


_BOOM = "deliberate handback bug"


def _break_handback(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``_handback_teardown.<name>`` raise, modelling a bug in this module.

    The only failures the top-level guard exists for are *our own*: no
    inverter response can produce one, because every inverter interaction is
    already inside a ``_step`` that absorbs it.  So the seam is a
    monkeypatch, and it is a narrow one — the attribute is replaced on the
    module the guard lives in, and every assertion is still made against the
    simulator, the ring buffer or the outcome record.

    Handles both the coroutine (``_handback``) and plain-function
    (``_record_outcome``) cases, so a test can name either without knowing
    which it is.
    """
    import inspect

    from custom_components.foxess_control import _handback_teardown as module

    original = getattr(module, name)

    if inspect.iscoroutinefunction(original):

        async def _async_boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(_BOOM)

        monkeypatch.setattr(module, name, _async_boom)
        return

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(_BOOM)

    monkeypatch.setattr(module, name, _boom)


def _wrap_to_start_session(inv: Inverter, method: str, hass: Any) -> None:
    """Make *method* start a smart session as a side effect, after running.

    The only way to test a plan-then-act race is to control when the
    session appears.  The real call still happens (real HTTP, real device
    state); the session start is bolted on afterwards, which places it in
    exactly the window a real service call could land in.
    """
    original = getattr(inv, method)

    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        _dd(hass).smart_discharge_state = _session_state("sess-race")
        return result

    setattr(inv, method, _wrapper)


class TestIssue16TheInverterIsReleased:
    """Option on, session over → the inverter is back under its own settings."""

    @pytest.mark.asyncio
    async def test_discharge_teardown_turns_the_master_switch_off(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Issue #16 end to end: switch off, mode SelfUse, no group left."""
        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        _assert_no_managed_override(foxess_sim)
        _assert_released(foxess_sim)

    @pytest.mark.asyncio
    async def test_direct_work_mode_is_set_even_when_it_was_something_else(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The work-mode write is a real write, not a no-op on a default.

        The simulator's ``work_mode_direct`` defaults to SelfUse, so
        asserting SelfUse on a fresh device proves nothing.  Starting from
        Feedin makes the write observable — and Feedin is a plausible real
        starting point, since it is in the device's declared enumeration.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(work_mode_direct="Feedin")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        assert foxess_sim.state()["work_mode_direct"] == WorkMode.SELF_USE.value

    @pytest.mark.asyncio
    async def test_charge_teardown_releases_the_inverter_too(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Handback governs the idle state, so it follows *every* session."""
        _pin_midday(foxess_sim)
        foxess_sim.set(work_mode_direct="Backup")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = _adapter(teardown_hass, inv)
        await teardown_hass.async_add_executor_job(
            lambda: inv.force_charge(min_soc_on_grid=_CONFIGURED_MIN_SOC)
        )
        assert _written_groups(foxess_sim, WorkMode.FORCE_CHARGE)

        await adapter.remove_override(teardown_hass, WorkMode.FORCE_CHARGE)

        _assert_no_managed_override(foxess_sim)
        _assert_released(foxess_sim)

    @pytest.mark.asyncio
    async def test_outcome_record_says_what_was_done(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """A handback the user cannot see the outcome of is taken on faith."""
        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        record = _last_handback(teardown_hass)
        assert record["acted"] is True
        assert record["reason"]
        assert record["steps"]["disable_scheduler"] == "ok"
        assert record["steps"]["work_mode"] == "ok"
        assert isinstance(record["t"], str) and record["t"]
        assert not _handback_errors(teardown_hass), (
            "a handback that succeeded must not record an operational error"
        )


class TestIssue4TheUsersOwnFloorComesBack:
    """The captured floor is restored verbatim — 0 included."""

    @pytest.mark.asyncio
    async def test_a_zero_floor_captured_before_the_session_is_restored(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Issue #4 end to end.

        A 0 % floor is only reachable outside the Mode Scheduler (the
        scheduler declares ``minsocongrid.range.min = 10``), so it is the
        value the feature exists for — and the one a truthiness test on
        "did we capture anything?" silently turns back into "restore
        nothing".

        The register is moved to 30 after capture, standing in for the
        shipped defect where a session target was written into the
        persistent floor and never put back (P-001, P-002).  Without that
        step the assertion would be vacuously true.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=0)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await async_setup_min_soc_capture(teardown_hass, inv)
        assert _dd(teardown_hass).captured_min_soc_on_grid == 0

        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        foxess_sim.set(min_soc_on_grid=30)

        await _teardown_discharge(teardown_hass, adapter)

        assert foxess_sim.state()["min_soc_on_grid"] == 0, (
            "the user's own 0 % floor was not restored — the inverter will "
            "import from the grid to hold a floor they never chose"
        )
        assert _last_handback(teardown_hass)["restored_min_soc_on_grid"] == 0

    @pytest.mark.asyncio
    async def test_the_configured_default_is_never_substituted(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Restoring 11 % here would be the integration choosing the floor."""
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=3)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await async_setup_min_soc_capture(teardown_hass, inv)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        foxess_sim.set(min_soc_on_grid=50)

        await _teardown_discharge(teardown_hass, adapter)

        state = foxess_sim.state()
        assert state["min_soc_on_grid"] == 3
        assert state["min_soc_on_grid"] != _CONFIGURED_MIN_SOC

    @pytest.mark.asyncio
    async def test_nothing_captured_means_the_floor_is_left_alone(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """ "Restore nothing" is a real answer, not a licence to guess."""
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=42)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        assert _dd(teardown_hass).captured_min_soc_on_grid is None
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        assert foxess_sim.state()["min_soc_on_grid"] == 42
        assert not [
            b
            for b in client.bodies_for(_EP_SETTING_SET)
            if b.get("key") == "MinSocOnGrid"
        ], "the Min SoC register was written despite nothing having been captured"
        assert _last_handback(teardown_hass)["restored_min_soc_on_grid"] is None
        # The rest of the handback still happened: a floor we cannot restore
        # is not a reason to leave the inverter scheduler-controlled.
        _assert_released(foxess_sim)


class TestOrderIsLoadBearing:
    """Groups off first, then the switch.  Never the other way round."""

    @pytest.mark.asyncio
    async def test_the_group_write_precedes_the_master_switch_going_off(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The whole safety argument for this feature is this ordering.

        If the switch goes off first and the group removal then fails, the
        inverter is left force-discharging behind a scheduler the
        integration has just disabled — it can no longer steer, and P-001
        is out of its hands.  Asserted on request *order*, because the end
        state is identical either way and only the intermediate state is
        dangerous.
        """
        _pin_midday(foxess_sim)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        client.calls.clear()

        await _teardown_discharge(teardown_hass, adapter)

        last_group_write = client.index_of_last(_EP_SCHEDULE_WRITE)
        switch_off = client.index_of_first(_EP_SCHEDULER_SET, enable=0)
        assert last_group_write < switch_off, (
            "the Mode Scheduler master switch was turned off before the last "
            "schedule write — a failure in between leaves a forced mode "
            "running behind a scheduler this integration can no longer reach"
        )

    @pytest.mark.asyncio
    async def test_the_switch_goes_off_before_the_work_mode_is_written(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Fixed order: switch off, then work mode, then the floor."""
        _pin_midday(foxess_sim)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        foxess_sim.set(min_soc_on_grid=7)
        await async_setup_min_soc_capture(teardown_hass, inv)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        client.calls.clear()

        await _teardown_discharge(teardown_hass, adapter)

        switch_off = client.index_of_first(_EP_SCHEDULER_SET, enable=0)
        work_mode = client.index_of_first(_EP_SETTING_SET, key="WorkMode")
        floor = client.index_of_first(_EP_SETTING_SET, key="MinSocOnGrid")
        assert switch_off < work_mode < floor


class TestFailureNeverBlocksCleanliness:
    """C-025 outranks handback: the override comes off regardless."""

    @pytest.mark.asyncio
    async def test_group_still_removed_when_every_handback_step_fails(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Firmware without the two write endpoints must not strand a group.

        ``scheduler_set_supported`` / ``setting_set_supported`` both False
        models a device whose ``scheduler/set`` and ``setting/set`` are
        absent — a real firmware/region difference, and the failure mode
        most likely to hit a user who opts in.  Every handback step fails;
        the teardown must still complete and must not raise, because the
        listener treats a raise as "the override is still on" and schedules
        a spurious retry.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False, setting_set_supported=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        _assert_no_managed_override(foxess_sim)
        assert _dd(teardown_hass).pending_override_cleanup is None, (
            "a handback failure was mistaken for a failed override removal"
        )

    @pytest.mark.asyncio
    async def test_every_failed_step_is_recorded(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """A silent failure lets the user believe the inverter was released."""
        _pin_midday(foxess_sim)
        foxess_sim.set(
            min_soc_on_grid=6,
            scheduler_set_supported=False,
            setting_set_supported=False,
        )
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await async_setup_min_soc_capture(teardown_hass, inv)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        recorded = _handback_errors(teardown_hass)
        assert recorded, (
            "no operational error was recorded for a handback in which every "
            "single step failed (C-026, D-059)"
        )
        record = _last_handback(teardown_hass)
        assert record["steps"]["disable_scheduler"] == "failed"
        assert record["steps"]["work_mode"] == "failed"
        assert record["steps"]["min_soc_on_grid"] == "failed"
        # And the device is honestly reported as NOT released.
        assert foxess_sim.state()["scheduler_enabled"] is True

    @pytest.mark.asyncio
    async def test_repeated_failures_do_not_flood_the_error_buffer(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """A teardown that fails every day must not evict all 30 slots.

        The ring buffer holds 30 entries and is the diagnostics download's
        only view of recent trouble.  One failing feature repeating on
        every session boundary would push everything else out, so the
        recording must collapse via ``dedupe_key``.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False, setting_set_supported=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        foxess_sim.set(min_soc_on_grid=8)
        await async_setup_min_soc_capture(teardown_hass, inv)

        for i in range(8):
            adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
            assert i >= 0
            await _teardown_discharge(teardown_hass, adapter)

        recorded = _handback_errors(teardown_hass)
        assert len(recorded) <= 3, (
            f"{len(recorded)} entries for one repeating failure — the "
            "30-entry buffer would be flooded and every other diagnostic "
            "lost (dedupe_key was not used)"
        )
        assert any(int(e.get("repeat", 1)) > 1 for e in recorded), (
            "the repeats were collapsed but the count was not kept, so the "
            "download cannot tell one failure from eight"
        )


class TestTheTopLevelGuardIsRealNotDecoration:
    """A bug in this module must not become a C-025 violation.

    Every step already absorbs its own failure, which means nothing in the
    *normal* operation of this module can reach the outer ``except`` in
    ``async_handback_after_teardown`` — so its "never raises" promise was
    load-bearing and completely untested.  Ablating it (raising as the first
    statement of that ``except``) left all 24 other tests passing.

    That is exactly the promise that quietly stops being true: the first
    refactor to move work out of a ``_step`` — a changed
    ``_record_outcome``, a new input gathered before the plan — reintroduces
    a raise on a path the listener reads as "the override is still on".  It
    would then queue a retry for an override that came off correctly, and
    report a failure the user cannot act on.

    Seams are monkeypatched here rather than provoked through the
    simulator on purpose: the failures being modelled are *our own bugs*, and
    there is no inverter response that produces one.  Every assertion is
    still on observable state — the device, the ring buffer, the record.
    """

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_does_not_reach_the_listener(
        self,
        teardown_hass: Any,
        foxess_sim: SimulatorHandle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """C-025 survives a bug anywhere in the handback module."""
        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        _break_handback(monkeypatch, "_handback")

        # No pytest.raises: propagating is the failure being tested for.
        await _teardown_discharge(teardown_hass, adapter)

        _assert_no_managed_override(foxess_sim)
        assert _dd(teardown_hass).pending_override_cleanup is None, (
            "a bug in the handback module was reported to the listener as a "
            "failed override removal, so a retry was queued for an override "
            "that had already come off correctly"
        )

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_is_recorded_under_its_own_key(
        self,
        teardown_hass: Any,
        foxess_sim: SimulatorHandle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Silence here would make the feature look like it had run."""
        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        _break_handback(monkeypatch, "_handback")

        await _teardown_discharge(teardown_hass, adapter)

        recorded = _errors_with_dedupe_key(teardown_hass, _DEDUPE_UNEXPECTED)
        assert len(recorded) == 1, (
            f"expected one entry under {_DEDUPE_UNEXPECTED!r}, got "
            f"{[e.get('dedupe_key') for e in _handback_errors(teardown_hass)]}"
        )
        assert _BOOM in recorded[0]["exc_str"], (
            "the recorded entry does not carry the failure's own message, so "
            "the download cannot say which bug this was"
        )

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_leaves_a_coherent_outcome_record(
        self,
        teardown_hass: Any,
        foxess_sim: SimulatorHandle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The record must say it did not act, not keep last session's answer.

        Without this, a handback that succeeded yesterday and crashed today
        leaves a record reading ``acted: true`` with every step ``ok``.  The
        timestamp would betray it to a careful reader, which is not the
        standard: the download must not need careful reading (C-020).
        """
        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)

        # A previous session's successful handback, so there is a stale
        # record for a crash to inherit.
        first = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        await _teardown_discharge(teardown_hass, first)
        assert _last_handback(teardown_hass)["acted"] is True

        second = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        _break_handback(monkeypatch, "_handback")
        await _teardown_discharge(teardown_hass, second)

        record = _last_handback(teardown_hass)
        assert record["acted"] is False, (
            "the outcome record still claims the previous session's success, "
            "so the diagnostics download reports a handback that did not "
            "happen"
        )
        assert "unexpected" in record["reason"]
        assert record["restored_min_soc_on_grid"] is None
        assert record["steps"] == {}

    @pytest.mark.asyncio
    async def test_a_failing_outcome_record_is_absent_never_half_written(
        self,
        teardown_hass: Any,
        foxess_sim: SimulatorHandle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The plausible real bug: the bookkeeping itself raises.

        ``_record_outcome`` is the last thing a successful handback does, so
        a raise there arrives *after* every write has landed.  Two things
        must hold: the inverter is still genuinely handed back (the work is
        not undone by its own bookkeeping), and the record is either absent
        or complete — a dict with some keys missing would have the
        diagnostics download describing a handback that never occurred in
        that shape.  The recovery path cannot use ``_record_outcome`` to say
        so, because that is the thing that is broken; the ring buffer is
        what has to carry the news.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=9, work_mode_direct="Feedin")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await async_setup_min_soc_capture(teardown_hass, inv)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        foxess_sim.set(min_soc_on_grid=70)
        _break_handback(monkeypatch, "_record_outcome")

        await _teardown_discharge(teardown_hass, adapter)

        _assert_no_managed_override(foxess_sim)
        _assert_released(foxess_sim)
        assert foxess_sim.state()["min_soc_on_grid"] == 9, (
            "the handback's own bookkeeping failure undid its work"
        )
        _assert_outcome_coherent(teardown_hass)
        assert _errors_with_dedupe_key(teardown_hass, _DEDUPE_UNEXPECTED), (
            "the only surviving channel — the ring buffer — says nothing, so "
            "this failure is invisible everywhere (C-026, D-059)"
        )

    @pytest.mark.asyncio
    async def test_a_repeating_bug_does_not_flood_the_error_buffer(
        self,
        teardown_hass: Any,
        foxess_sim: SimulatorHandle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bug repeats on every session boundary, just as a 404 does."""
        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        _break_handback(monkeypatch, "_handback")

        for _ in range(6):
            adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
            await _teardown_discharge(teardown_hass, adapter)

        recorded = _errors_with_dedupe_key(teardown_hass, _DEDUPE_UNEXPECTED)
        assert len(recorded) == 1, (
            f"{len(recorded)} entries for one repeating bug — six session "
            "boundaries a day would evict every other diagnostic from the "
            "30-entry buffer"
        )
        assert int(recorded[0]["repeat"]) == 6


class TestOptionOffChangesNothing:
    """The shipped default must behave byte-for-byte as it did before."""

    @pytest.mark.asyncio
    async def test_no_new_api_call_is_made(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Not "no writes" — no *calls*.

        Every endpoint this feature introduced is asserted absent.
        ``scheduler/set`` is the exception that proves the rule: it was
        already used before this feature (``_ensure_scheduler_enabled``
        turns the switch *on* before every schedule write), so its
        presence is not evidence of anything.  Its *body* is: nothing may
        send ``enable: 0``.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=17, work_mode_direct="Feedin")
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        client.calls.clear()

        await _teardown_discharge(teardown_hass, adapter)

        paths = client.paths()
        assert _EP_SCHEDULER_FLAG not in paths, (
            "a default install probed Mode Scheduler support — a new request "
            "on every session boundary for a feature that is switched off"
        )
        assert _EP_SETTING_GET not in paths
        assert _EP_SETTING_SET not in paths
        assert not [
            b for b in client.bodies_for(_EP_SCHEDULER_SET) if b.get("enable") == 0
        ], "a default install turned the Mode Scheduler master switch off"

    @pytest.mark.asyncio
    async def test_device_state_is_untouched_and_the_group_still_goes(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Today's behaviour, unchanged — and C-025 still honoured."""
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=17, work_mode_direct="Feedin")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True
        assert state["work_mode_direct"] == "Feedin"
        assert state["min_soc_on_grid"] == 17
        _assert_no_managed_override(foxess_sim)

    @pytest.mark.asyncio
    async def test_the_decline_is_still_recorded_with_a_reason(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """ "Nothing happened, and here is why" is the transparent answer."""
        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        record = _last_handback(teardown_hass)
        assert record["acted"] is False
        assert "not enabled" in record["reason"]
        assert not _handback_errors(teardown_hass), (
            "declining because the user never opted in is not an error"
        )


class TestSessionStartRace:
    """``session_active`` is a snapshot; acting on a stale one is the hazard."""

    @pytest.mark.asyncio
    async def test_an_active_session_declines_outright(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """A charge session outliving a discharge teardown still holds the device."""
        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        _dd(teardown_hass).smart_charge_state = _session_state("charge-still-live")

        await _teardown_discharge(teardown_hass, adapter)

        assert foxess_sim.state()["scheduler_enabled"] is True
        assert _last_handback(teardown_hass)["acted"] is False
        assert "session" in _last_handback(teardown_hass)["reason"]

    @pytest.mark.asyncio
    async def test_a_session_starting_between_plan_and_act_is_caught(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The race Task 3's implementer flagged, made deterministic.

        ``probe_scheduler_support`` is the last input the plan needs, so a
        session that appears as it returns is one that started *after* the
        ``session_active`` snapshot and *before* the first write.  A
        planner that trusts its own snapshot disables the master switch
        under a live session and strands its override.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(work_mode_direct="Feedin")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        cancel_smart_discharge(teardown_hass, DOMAIN)
        _wrap_to_start_session(inv, "probe_scheduler_support", teardown_hass)

        await adapter.remove_override(teardown_hass, WorkMode.FORCE_DISCHARGE)

        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True, (
            "the master switch went off although a session had started — the "
            "new session's schedule group is now inert (C-025)"
        )
        assert state["work_mode_direct"] == "Feedin"
        record = _last_handback(teardown_hass)
        assert record["acted"] is False
        assert "session" in record["reason"]

    @pytest.mark.asyncio
    async def test_a_session_starting_as_the_switch_goes_off_is_healed(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """No pre-check can close the window entirely, so it self-heals.

        The switch is re-checked immediately *after* it goes off; a session
        found there gets the switch turned straight back on and the rest of
        the handback abandoned.  Driven through the executor directly
        because ``set_scheduler_enabled`` is also what
        ``_ensure_scheduler_enabled`` calls during the group removal, and
        the removal must not be the thing that trips the wrapper.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(work_mode_direct="Feedin")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await teardown_hass.async_add_executor_job(
            lambda: inv.force_discharge(
                min_soc=20, power=3000, min_soc_on_grid=_CONFIGURED_MIN_SOC
            )
        )
        await teardown_hass.async_add_executor_job(
            _remove_mode_from_schedule,
            inv,
            WorkMode.FORCE_DISCHARGE,
            _CONFIGURED_MIN_SOC,
        )
        _wrap_to_start_session(inv, "set_scheduler_enabled", teardown_hass)

        await _run_handback(teardown_hass, inv)

        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True, (
            "a session started while the master switch was being turned off "
            "and the switch was left off — its schedule group cannot drive "
            "the inverter"
        )
        assert state["work_mode_direct"] == "Feedin", (
            "the handback carried on after finding a live session"
        )
        assert _last_handback(teardown_hass)["acted"] is False


class TestUnmanagedModes:
    """C-018: a schedule the user built by hand is not ours to rewrite."""

    @pytest.mark.asyncio
    async def test_a_backup_group_declines_the_handback_by_name(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The reason must name the mode, or the log is not actionable."""
        _pin_midday(foxess_sim)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await teardown_hass.async_add_executor_job(
            lambda: inv.set_schedule(
                [
                    {
                        "enable": 1,
                        "startHour": 0,
                        "startMinute": 0,
                        "endHour": 6,
                        "endMinute": 0,
                        "workMode": WorkMode.BACKUP.value,
                        "minSocOnGrid": 11,
                        "fdSoc": 11,
                        "fdPwr": 3000,
                    }
                ]
            )
        )
        client.calls.clear()

        await _run_handback(teardown_hass, inv)

        record = _last_handback(teardown_hass)
        assert record["acted"] is False
        assert WorkMode.BACKUP.value in record["reason"]
        assert foxess_sim.state()["scheduler_enabled"] is True
        assert not [
            b for b in client.bodies_for(_EP_SCHEDULER_SET) if b.get("enable") == 0
        ]

    @pytest.mark.asyncio
    async def test_teardown_with_an_unmanaged_group_leaves_the_switch_on(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Existing C-018 behaviour is preserved: the removal itself refuses.

        ``_remove_mode_from_schedule`` raises on an unmanaged group, so the
        override never comes off — and because handback runs strictly
        *after* a successful removal, it never runs at all.  The switch
        staying on is the whole point: the integration keeps control of a
        schedule it was not allowed to tidy.
        """
        from homeassistant.exceptions import ServiceValidationError

        _pin_midday(foxess_sim)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = _adapter(teardown_hass, inv)
        await teardown_hass.async_add_executor_job(
            lambda: inv.set_schedule(
                [
                    {
                        "enable": 1,
                        "startHour": 0,
                        "startMinute": 0,
                        "endHour": 6,
                        "endMinute": 0,
                        "workMode": WorkMode.BACKUP.value,
                        "minSocOnGrid": 11,
                        "fdSoc": 11,
                        "fdPwr": 3000,
                    },
                    {
                        "enable": 1,
                        "startHour": 8,
                        "startMinute": 0,
                        "endHour": 20,
                        "endMinute": 0,
                        "workMode": WorkMode.FORCE_DISCHARGE.value,
                        "minSocOnGrid": 11,
                        "fdSoc": 20,
                        "fdPwr": 3000,
                    },
                ]
            )
        )

        with pytest.raises(ServiceValidationError):
            await adapter.remove_override(teardown_hass, WorkMode.FORCE_DISCHARGE)

        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True
        assert _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)


class TestEntityMode:
    """Entity mode has no cloud Mode Scheduler, so nothing may happen."""

    @pytest.mark.asyncio
    async def test_entity_mode_touches_no_cloud_surface(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=23, work_mode_direct="Feedin")
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _configure(
            teardown_hass,
            **{
                CONF_SCHEDULER_HANDBACK: True,
                CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            },
        )
        client.calls.clear()

        await _run_handback(teardown_hass, inv)

        assert client.calls == [], (
            f"entity mode made {len(client.calls)} cloud request(s): {client.paths()}"
        )
        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True
        assert state["work_mode_direct"] == "Feedin"
        assert state["min_soc_on_grid"] == 23
        assert "entity mode" in _last_handback(teardown_hass)["reason"]


class TestTheNextSessionStillWorks:
    """The regression that would matter most to a user who opts in."""

    @pytest.mark.asyncio
    async def test_a_session_after_a_handback_takes_effect(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Handback turns the switch off; the next session must turn it on.

        Pinned with ``scheduler_enable_implies_on=False`` — the pessimistic
        half of the unverified API behaviour — because that is the case in
        which a session written after a handback silently never fires:
        errno 0, no error surfaced, no mode change, no discharge.  Task 1
        built ``_ensure_scheduler_enabled`` for exactly this; this proves
        the two halves meet.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_enable_implies_on=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        await _teardown_discharge(teardown_hass, adapter)
        assert foxess_sim.state()["scheduler_enabled"] is False

        second = _adapter(teardown_hass, inv)
        await teardown_hass.async_add_executor_job(
            lambda: inv.force_discharge(
                min_soc=25, power=2500, min_soc_on_grid=_CONFIGURED_MIN_SOC
            )
        )
        _dd(teardown_hass).smart_discharge_state = _session_state("sess-2")

        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True, (
            "the session after a handback wrote its groups behind a disabled "
            "master switch — errno 0, nothing surfaced, and no discharge"
        )
        assert state["work_mode"] == WorkMode.FORCE_DISCHARGE.value, (
            f"inverter is in {state['work_mode']}: the group was accepted but "
            "is not in force"
        )

        # ...and the session after that one hands back again.
        await _teardown_discharge(teardown_hass, second)
        _assert_released(foxess_sim)


class TestClearOverridesHandsBackToo:
    """The user asking for their inverter back is the clearest case of all.

    ``clear_overrides`` does **not** go through ``adapter.remove_override``
    — it writes a whole-day SelfUse group itself — so it is a third
    teardown path, and the one a user reaches for when they explicitly want
    the integration to let go.  Handing back everywhere except there would
    make the feature look broken precisely when it is being asked for.
    """

    @pytest.mark.asyncio
    async def test_clear_overrides_releases_the_inverter(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=4, work_mode_direct="Feedin")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await async_setup_min_soc_capture(teardown_hass, inv)
        await _start_discharge_session(teardown_hass, foxess_sim, inv)
        foxess_sim.set(min_soc_on_grid=60)

        await _call_clear_overrides(teardown_hass)

        _assert_no_managed_override(foxess_sim)
        _assert_released(foxess_sim)
        assert foxess_sim.state()["min_soc_on_grid"] == 4

    @pytest.mark.asyncio
    async def test_clear_overrides_with_the_option_off_changes_nothing(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=4, work_mode_direct="Feedin")
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        await _start_discharge_session(teardown_hass, foxess_sim, inv)
        client.calls.clear()

        await _call_clear_overrides(teardown_hass)

        assert _EP_SETTING_SET not in client.paths()
        assert not [
            b for b in client.bodies_for(_EP_SCHEDULER_SET) if b.get("enable") == 0
        ]
        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True
        assert state["work_mode_direct"] == "Feedin"
        assert state["min_soc_on_grid"] == 4


# ---------------------------------------------------------------------------
# The manual ``disable_scheduler`` action (issue #16, asked for by name).
# ---------------------------------------------------------------------------


def _write_backup_group(hass: Any, inv: Inverter) -> Any:
    """Put an unmanaged Backup group on the device (C-018 bait)."""
    return hass.async_add_executor_job(
        lambda: inv.set_schedule(
            [
                {
                    "enable": 1,
                    "startHour": 0,
                    "startMinute": 0,
                    "endHour": 6,
                    "endMinute": 0,
                    "workMode": WorkMode.BACKUP.value,
                    "minSocOnGrid": 11,
                    "fdSoc": 11,
                    "fdPwr": 3000,
                }
            ]
        )
    )


def _min_soc_writes(client: RecordingClient) -> list[dict[str, Any]]:
    return [
        b for b in client.bodies_for(_EP_SETTING_SET) if b.get("key") == "MinSocOnGrid"
    ]


def _assert_switch_untouched(
    foxess_sim: SimulatorHandle, client: RecordingClient
) -> None:
    """The master switch is on, and no attempt was made to turn it off.

    Both halves matter: ``scheduler_enabled is True`` alone would also hold
    for a device that rejected the write, and a refusal that *tried* is not
    a refusal.
    """
    assert foxess_sim.state()["scheduler_enabled"] is True, (
        "the Mode Scheduler master switch was turned off despite the action "
        "having refused"
    )
    assert not [
        b for b in client.bodies_for(_EP_SCHEDULER_SET) if b.get("enable") == 0
    ], "the action attempted the master-switch write on a path that refuses"


class TestDisableSchedulerAction:
    """``foxess_control.disable_scheduler`` — the manual lever from issue #16.

    Four properties, in the order they matter:

    1. **It applies the same policy, not its own.**  Every guard
       ``plan_handback`` enforces is enforced here, and by the same code: an
       implementation that wrote the master switch directly would release
       the inverter mid-session, or behind a hand-built Backup group, or
       with a Min SoC it invented.  Each of those has a test below, so
       bypassing the policy cannot pass.

    2. **The option is not consulted.**  The whole value of a separate
       action is for the user who does *not* want the automatic behaviour
       but does want their inverter back now; an action that also required
       flipping the option would be a redundant duplicate of the teardown
       hook.  The explicit call is the consent.  Pinned by
       ``test_it_works_with_the_option_off``.

    3. **A refusal is visible.**  Unlike the teardown hook — which must
       never raise, because the listener reads an exception as "the override
       is still on" — this is a service call with a user waiting on it, so
       every refusal raises ``ServiceValidationError`` and names its reason
       (C-020).  A service that silently does nothing is a bad service.

    4. **The Min SoC rule does not bend for it.**  Only a *captured* floor
       is ever put back.  Nothing captured means nothing written — the
       subject of a shipped defect in this integration (P-002).
    """

    @pytest.mark.asyncio
    async def test_the_action_is_registered(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Issue #16 asked for ``foxess_control.disable_scheduler`` by name."""
        from custom_components.foxess_control import _register_services

        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _register_services(teardown_hass)

        assert teardown_hass.services.has_service(DOMAIN, "disable_scheduler"), (
            "no foxess_control.disable_scheduler action is registered — the "
            "manual lever issue #16 asked for does not exist"
        )

    @pytest.mark.asyncio
    async def test_it_releases_the_inverter_on_request(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Switch off, work mode SelfUse, the captured floor put back.

        The register is moved to 55 after capture, standing in for a session
        having written a floor into it: without that step the Min SoC
        assertion would be vacuously true.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=7, work_mode_direct="Feedin")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await async_setup_min_soc_capture(teardown_hass, inv)
        assert _dd(teardown_hass).captured_min_soc_on_grid == 7
        foxess_sim.set(min_soc_on_grid=55)

        await _call_disable_scheduler(teardown_hass)

        _assert_released(foxess_sim)
        assert foxess_sim.state()["min_soc_on_grid"] == 7, (
            "the user's own floor was not put back by the action"
        )
        record = _last_handback(teardown_hass)
        assert record["acted"] is True
        assert record["steps"]["disable_scheduler"] == "ok"
        assert record["steps"]["work_mode"] == "ok"
        assert record["steps"]["min_soc_on_grid"] == "ok"
        assert record["restored_min_soc_on_grid"] == 7

    @pytest.mark.asyncio
    async def test_it_works_with_the_option_off(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """**The decision this task had to make, pinned.**

        The action deliberately does *not* consult
        ``scheduler_handback``.  The person in issue #16 wants their
        inverter released without signing up for it to happen after every
        session; an action that refused until they flipped the option would
        be a duplicate of the teardown hook and useless to them.  Calling
        a service is itself the consent — the option governs the
        *automatic* behaviour only.

        The fixture ships the option **off**, so this test does not enable
        it.  If a future change makes the action honour the option, this is
        the test that must be argued with rather than deleted.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=9, work_mode_direct="Backup")
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        await async_setup_min_soc_capture(teardown_hass, inv)
        assert _dd(teardown_hass).captured_min_soc_on_grid == 9, (
            "capture is not gated on the option, so an install that never "
            "opted in still knows the user's floor"
        )
        foxess_sim.set(min_soc_on_grid=44)

        await _call_disable_scheduler(teardown_hass)

        _assert_released(foxess_sim)
        assert foxess_sim.state()["min_soc_on_grid"] == 9
        assert _last_handback(teardown_hass)["acted"] is True

    @pytest.mark.asyncio
    async def test_it_refuses_during_an_active_session_and_says_why(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """C-025: disabling the scheduler mid-session strands the override.

        The group stops driving the inverter while the integration still
        believes it is discharging — a safety divergence, not untidiness.
        The caller has to be able to tell, so this raises rather than
        logging: a service that silently does nothing is a bad service
        (C-020).
        """
        _pin_midday(foxess_sim)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await _start_discharge_session(teardown_hass, foxess_sim, inv)
        client.calls.clear()

        message = await _refused_disable_scheduler(teardown_hass)

        assert "session" in message, (
            f"the refusal does not say a session is why: {message}"
        )
        _assert_switch_untouched(foxess_sim, client)
        assert _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE), (
            "the action removed the live session's override group — it must "
            "refuse outright, not tidy up after a session it did not start"
        )
        record = _last_handback(teardown_hass)
        assert record["acted"] is False
        assert "session" in record["reason"]

    @pytest.mark.asyncio
    async def test_a_charge_session_refuses_it_too(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Either session family counts — the guard is not discharge-only."""
        _pin_midday(foxess_sim)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _dd(teardown_hass).smart_charge_state = _session_state("charge-live")
        client.calls.clear()

        message = await _refused_disable_scheduler(teardown_hass)

        assert "session" in message
        _assert_switch_untouched(foxess_sim, client)

    @pytest.mark.asyncio
    async def test_a_session_starting_mid_flight_is_caught_and_reported(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The plan-then-act race, through the action rather than the hook.

        ``session_active`` is a snapshot; a smart-charge service call can be
        served on the event loop between taking it and the first write.  The
        action shares the executor's re-checks, so it must both leave the
        switch alone *and* tell the caller — an action that reported success
        here would have the user believe their inverter was released while
        it is running a session's group.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(work_mode_direct="Feedin")
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _wrap_to_start_session(inv, "probe_scheduler_support", teardown_hass)
        client.calls.clear()

        message = await _refused_disable_scheduler(teardown_hass)

        assert "session" in message
        _assert_switch_untouched(foxess_sim, client)
        assert foxess_sim.state()["work_mode_direct"] == "Feedin", (
            "the action carried on past the race guard"
        )
        assert _last_handback(teardown_hass)["acted"] is False

    @pytest.mark.asyncio
    async def test_it_refuses_with_an_unmanaged_mode_present_naming_it(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """C-018: a schedule the user built by hand is not ours to release.

        Naming the mode is the actionable half — "remove it in the FoxESS
        app" is only useful advice if the user is told *what* to remove.
        """
        _pin_midday(foxess_sim)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await _write_backup_group(teardown_hass, inv)
        client.calls.clear()

        message = await _refused_disable_scheduler(teardown_hass)

        assert WorkMode.BACKUP.value in message, (
            "the refusal does not name the unmanaged mode, so the user is "
            f"not told what to remove: {message}"
        )
        _assert_switch_untouched(foxess_sim, client)
        record = _last_handback(teardown_hass)
        assert record["acted"] is False
        assert WorkMode.BACKUP.value in record["reason"]

    @pytest.mark.asyncio
    async def test_nothing_captured_means_the_floor_is_untouched(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """An explicit call is not a licence to invent a floor.

        Choosing a Min SoC is the user's business (P-002).  The action may
        put back what was captured and nothing else — restoring the
        configured 11% here, or any other default, is precisely the shipped
        defect that made an inverter import from the grid to hold a level
        nobody chose.  The rest of the handback still happens.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=42)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        assert _dd(teardown_hass).captured_min_soc_on_grid is None

        await _call_disable_scheduler(teardown_hass)

        assert foxess_sim.state()["min_soc_on_grid"] == 42, (
            "the Min SoC register was changed although nothing was ever "
            "captured — the action invented a floor"
        )
        assert not _min_soc_writes(client), (
            "the Min SoC register was written despite nothing having been "
            f"captured: {_min_soc_writes(client)}"
        )
        _assert_released(foxess_sim)
        record = _last_handback(teardown_hass)
        assert record["steps"]["min_soc_on_grid"] == "skipped"
        assert record["restored_min_soc_on_grid"] is None

    @pytest.mark.asyncio
    async def test_a_captured_zero_floor_is_restored_verbatim(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Issue #4 through the action: 0% is a value, not an absence."""
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=0)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        await async_setup_min_soc_capture(teardown_hass, inv)
        assert _dd(teardown_hass).captured_min_soc_on_grid == 0
        foxess_sim.set(min_soc_on_grid=30)

        await _call_disable_scheduler(teardown_hass)

        assert foxess_sim.state()["min_soc_on_grid"] == 0, (
            "a captured 0% floor was treated as 'nothing captured' — issue "
            "#4 is precisely about a 0% floor being reachable off-scheduler"
        )
        assert _last_handback(teardown_hass)["restored_min_soc_on_grid"] == 0

    @pytest.mark.asyncio
    async def test_entity_mode_is_a_clean_no_op(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """There is no cloud Mode Scheduler to release in entity mode.

        Zero cloud requests — not merely zero writes — and the caller is
        told, because an action that appeared to succeed would leave an
        entity-mode user hunting for a switch that was never touched.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=23, work_mode_direct="Feedin")
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _configure(
            teardown_hass,
            **{CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"},
        )
        client.calls.clear()

        message = await _refused_disable_scheduler(teardown_hass)

        assert "entity mode" in message
        assert client.calls == [], (
            f"entity mode made {len(client.calls)} cloud request(s): {client.paths()}"
        )
        state = foxess_sim.state()
        assert state["scheduler_enabled"] is True
        assert state["work_mode_direct"] == "Feedin"
        assert state["min_soc_on_grid"] == 23
        assert "entity mode" in _last_handback(teardown_hass)["reason"]

    @pytest.mark.asyncio
    async def test_a_device_without_a_scheduler_refuses_by_name(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """A batteryless micro-inverter has nothing to hand back."""
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_supported=False)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        client.calls.clear()

        message = await _refused_disable_scheduler(teardown_hass)

        assert "Mode Scheduler" in message
        _assert_switch_untouched(foxess_sim, client)
        assert _last_handback(teardown_hass)["acted"] is False

    @pytest.mark.asyncio
    async def test_an_unreadable_flag_refuses_as_unknown_not_unsupported(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """ "Could not find out" is not "your hardware has no scheduler".

        A malformed flag reply must not become a confident claim about the
        user's device — the two send them to look in completely different
        places (C-020, P-005).
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_flag_null=True)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        client.calls.clear()

        message = await _refused_disable_scheduler(teardown_hass)

        assert "could not determine" in message.lower(), (
            f"an unreadable flag was not reported as unknown: {message}"
        )
        _assert_switch_untouched(foxess_sim, client)

    @pytest.mark.asyncio
    async def test_a_failed_step_is_recorded_and_does_not_raise(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """A refusal raises; a device that refuses a write does not.

        The distinction is deliberate.  A refusal happens before anything
        is attempted, so "nothing happened, here is why" is the whole
        truth.  A step failure happens after the steps have each been
        attempted independently — some may have taken effect — so an
        exception would misreport a partial handback as a total one, and
        would also make the outcome record and the recent-errors buffer
        (C-026, D-059) the *less* accurate account of the two.  Both write
        endpoints absent models a real firmware/region difference.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(
            min_soc_on_grid=6,
            scheduler_set_supported=False,
            setting_set_supported=False,
        )
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        await async_setup_min_soc_capture(teardown_hass, inv)

        await _call_disable_scheduler(teardown_hass)

        assert _handback_errors(teardown_hass), (
            "every step failed and nothing was recorded — the user is left "
            "believing the action released their inverter (C-026, D-059)"
        )
        record = _last_handback(teardown_hass)
        assert record["steps"]["disable_scheduler"] == "failed"
        assert record["steps"]["work_mode"] == "failed"
        assert record["steps"]["min_soc_on_grid"] == "failed"
        assert record["restored_min_soc_on_grid"] is None, (
            "the record claims the floor was restored when the write failed"
        )
        assert foxess_sim.state()["scheduler_enabled"] is True

    @pytest.mark.asyncio
    async def test_it_does_not_remove_or_add_schedule_groups(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The action releases the switch; it is not a second clear_overrides.

        ``clear_overrides`` already exists for tidying the group list, and
        conflating the two would mean a user asking for local Modbus
        control back also silently lost a SelfUse group they had built.
        """
        _pin_midday(foxess_sim)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        before = foxess_sim.state()["schedule_groups"]
        client.calls.clear()

        await _call_disable_scheduler(teardown_hass)

        assert foxess_sim.state()["schedule_groups"] == before, (
            "the action rewrote the schedule group list"
        )
        assert _EP_SCHEDULE_WRITE not in client.paths(), (
            "the action wrote to the schedule-group endpoint"
        )
        _assert_released(foxess_sim)

    @pytest.mark.asyncio
    async def test_it_can_be_called_twice_in_a_row(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Idempotent: an already-released inverter is not an error.

        A user who is unsure whether the first call worked will press it
        again, and an automation may run it on a schedule.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(min_soc_on_grid=5)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        await async_setup_min_soc_capture(teardown_hass, inv)

        await _call_disable_scheduler(teardown_hass)
        await _call_disable_scheduler(teardown_hass)

        _assert_released(foxess_sim)
        assert foxess_sim.state()["min_soc_on_grid"] == 5
        assert _last_handback(teardown_hass)["acted"] is True
        assert not _handback_errors(teardown_hass)


class TestTheMasterSwitchWriteEndpointIsMissing:
    """Issue #17: an H3-12.0-E whose ``scheduler/set`` answers HTTP 404.

    Reported against 1.0.22-beta.6::

        Could not turn the Mode Scheduler master switch on via
          /op/v0/device/scheduler/set on inverter H3-12.0-E;
          writing the schedule anyway.
        requests.exceptions.HTTPError: 404 Client Error: Not Found

    That endpoint is the only way to turn the switch **off**, so handback
    cannot work on that hardware at all.  Before this, a user could enable
    the option, watch nothing happen at every session boundary forever, and
    find the explanation only by downloading a diagnostics file and reading
    ``last_handback`` — which is the definition of the failure C-020 exists
    to prevent.

    Three things have to be true:

    1. **C-025 is unaffected.**  The session's override group still comes
       off.  Session boundary cleanliness outranks this whole feature, and
       a device that cannot be handed back must not become a device that
       keeps force-discharging.
    2. **The user is told, in the UI.**  A Repair issue, because "you asked
       for something your inverter cannot do" is exactly what Repairs are
       for — persistent, visible without log inspection, and dismissible by
       the one action that does help (turning the option back off).  It is
       raised only when the *option* is on: the action reports synchronously
       and does not need a standing notice.
    3. **It self-heals.**  Nothing is persisted, and the notice is
       withdrawn the moment a master-switch write succeeds, so a transient
       404 or a firmware fix does not leave a permanent scar.
    """

    @staticmethod
    def _issues(hass: Any) -> set[str]:
        from homeassistant.helpers import issue_registry as ir

        return {
            issue_id
            for (domain, issue_id) in ir.async_get(hass).issues
            if domain == DOMAIN
        }

    def _assert_told(self, hass: Any) -> None:
        assert _HANDBACK_UNSUPPORTED_ISSUE in self._issues(hass), (
            "no Repair issue was raised, so a user who switched the option "
            "on is left watching nothing happen at every session boundary "
            "with only a diagnostics download to explain it (C-020, C-026)"
        )

    def _assert_not_told(self, hass: Any) -> None:
        assert _HANDBACK_UNSUPPORTED_ISSUE not in self._issues(hass), (
            "a Repair issue about unsupported hardware is standing when it "
            "should not be"
        )

    @pytest.mark.asyncio
    async def test_the_session_group_still_comes_off(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """C-025 outranks handback entirely (P-002 over P-005)."""
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        _assert_no_managed_override(foxess_sim)
        assert foxess_sim.state()["scheduler_enabled"] is True, (
            "the master switch reads as off on a device that cannot serve "
            "the write — the simulator or the client is lying"
        )

    @pytest.mark.asyncio
    async def test_the_teardown_declines_and_names_the_missing_endpoint(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The session's own schedule write is what discovers it.

        ``_ensure_scheduler_enabled`` attempts the master-switch write before
        every schedule write, so by the time the teardown asks, the answer
        is already known — and the decline can say what is wrong instead of
        silently failing a step it was never going to win.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        record = _last_handback(teardown_hass)
        assert record["acted"] is False
        assert "404" in record["reason"], (
            f"the decline does not say what the inverter did: {record['reason']}"
        )
        assert "master switch" in record["reason"].lower()
        assert record["steps"] == {}, (
            "steps were attempted despite the plan having declined, so the "
            f"decline is decorative: {record['steps']}"
        )

    @pytest.mark.asyncio
    async def test_a_repair_issue_tells_the_user(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The whole point of Part A: the UI says so, not just the log."""
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        self._assert_told(teardown_hass)

    @pytest.mark.asyncio
    async def test_the_repair_issue_names_the_inverter_and_the_endpoint(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """A notice a user cannot act on is noise.

        The model tells them it is about *their* inverter and the endpoint
        ties the notice to the warning already in their log.
        """
        from homeassistant.helpers import issue_registry as ir

        from custom_components.foxess_control.foxess.inverter import (
            _SCHEDULER_SET_ENDPOINT,
        )

        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False, device_type="H3-12.0-E")
        inv, _client = _make_inv(foxess_sim)
        assert inv.max_power_w  # warm the device-detail cache, so the type is known
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)

        await _teardown_discharge(teardown_hass, adapter)

        issue = ir.async_get(teardown_hass).async_get_issue(
            DOMAIN, _HANDBACK_UNSUPPORTED_ISSUE
        )
        assert issue is not None
        placeholders = issue.translation_placeholders or {}
        assert placeholders.get("inverter") == "H3-12.0-E", (
            f"the notice does not name the inverter: {placeholders}"
        )
        assert placeholders.get("endpoint") == _SCHEDULER_SET_ENDPOINT, (
            "the notice does not name the endpoint the user's own log named, "
            f"so the two cannot be connected: {placeholders}"
        )

    @pytest.mark.asyncio
    async def test_the_option_off_raises_no_repair_issue(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """A notice about a feature nobody switched on is pure noise.

        Hundreds of installs run the shipped default.  None of them asked
        for handback, so none of them may be told their inverter cannot do
        it — and the option-off path must still make no API call at all.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        client.calls.clear()

        await _teardown_discharge(teardown_hass, adapter)

        self._assert_not_told(teardown_hass)
        assert _EP_SCHEDULER_FLAG not in client.paths(), (
            "a default install probed Mode Scheduler support anyway"
        )

    @pytest.mark.asyncio
    async def test_turning_the_option_off_withdraws_the_notice(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The notice's only remedy must actually retire it.

        It says "turn the option off"; a notice that then stayed would be
        telling the user their fix did not work.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        await _teardown_discharge(teardown_hass, adapter)
        self._assert_told(teardown_hass)

        _configure(teardown_hass)  # handback back off
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        await _teardown_discharge(teardown_hass, adapter)

        self._assert_not_told(teardown_hass)

    @pytest.mark.asyncio
    async def test_the_notice_is_withdrawn_when_the_endpoint_answers(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """Self-healing: a transient 404 must not scar the install."""
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        await _teardown_discharge(teardown_hass, adapter)
        self._assert_told(teardown_hass)

        foxess_sim.set(scheduler_set_supported=True)
        adapter = await _start_discharge_session(teardown_hass, foxess_sim, inv)
        await _teardown_discharge(teardown_hass, adapter)

        self._assert_not_told(teardown_hass)
        _assert_released(foxess_sim)

    @pytest.mark.asyncio
    async def test_the_action_refuses_once_it_is_known_and_says_why(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """``disable_scheduler`` must refuse, not appear to work.

        The refusal reaches the user as a service error — the surface that
        matters for someone holding a service call open (C-020).
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        # The first attempt is what discovers the 404: it fails a step
        # rather than refusing, because until something has tried, "this
        # firmware has no such endpoint" is not a fact anyone holds.
        await _call_disable_scheduler(teardown_hass)
        client.calls.clear()

        message = await _refused_disable_scheduler(teardown_hass)

        assert "404" in message, (
            f"the refusal does not say what the inverter did: {message}"
        )
        assert "master switch" in message.lower()
        _assert_switch_untouched(foxess_sim, client)

    @pytest.mark.asyncio
    async def test_the_very_first_action_call_still_tells_the_user(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The silent case from the report, closed.

        A step failure deliberately does **not** raise (see
        ``test_a_failed_step_is_recorded_and_does_not_raise``: some steps
        may have taken effect, so an exception would misreport a partial
        handback as a total one).  That leaves the very first call on an H3
        with nothing to show the user unless the discovery itself surfaces
        — so it does, on the spot, rather than waiting for a second call.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        assert inv.scheduler_set_unavailable is False, "nothing has tried yet"

        await _call_disable_scheduler(teardown_hass)

        self._assert_told(teardown_hass)
        record = _last_handback(teardown_hass)
        assert record["steps"]["disable_scheduler"] == "failed"
        assert _errors_with_dedupe_key(
            teardown_hass, f"{_ERROR_CATEGORY}:{_STEP_DISABLE}"
        ), "the failed master-switch write left no operational-error record"

    @pytest.mark.asyncio
    async def test_the_users_own_floor_still_comes_back(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """P-002 does not depend on the master switch being writable.

        The first attempt cannot know the switch is unwritable, and the
        three steps fail independently on purpose: putting the user's own
        floor back is worth doing whether or not the scheduler could be
        released.  An inverter holding a session floor imports from the
        grid to maintain it.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_set_supported=False, min_soc_on_grid=3)
        inv, _client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        await async_setup_min_soc_capture(teardown_hass, inv)
        assert _dd(teardown_hass).captured_min_soc_on_grid == 3
        foxess_sim.set(min_soc_on_grid=51)

        await _call_disable_scheduler(teardown_hass)

        assert foxess_sim.state()["min_soc_on_grid"] == 3, (
            "the user's own floor was abandoned because the master-switch "
            "write failed — the steps are supposed to be independent"
        )

    @pytest.mark.asyncio
    async def test_a_device_without_a_scheduler_is_still_reported_as_such(
        self, teardown_hass: Any, foxess_sim: SimulatorHandle
    ) -> None:
        """The two facts must not be conflated.

        A device reporting ``support: false`` *serves* the endpoint and
        rejects the write with errno 40257, so nothing here may relabel it
        as "this firmware has no such endpoint" — and it gets no Repair
        issue, because the existing ``scheduler_supported`` reason already
        covers it and two notices for one cause is worse than one.
        """
        _pin_midday(foxess_sim)
        foxess_sim.set(scheduler_supported=False)
        inv, client = _make_inv(foxess_sim)
        _attach(teardown_hass, inv)
        _enable_handback(teardown_hass)
        client.calls.clear()

        # No priming call: this refuses on the *first* attempt, because the
        # flag read answers the question outright.  Nothing ever attempts
        # the master-switch write, so nothing can misattribute its failure.
        message = await _refused_disable_scheduler(teardown_hass)

        assert "reports no Mode Scheduler support" in message, (
            f"a device with no scheduler was reported as a missing endpoint: {message}"
        )
        assert "404" not in message
        assert inv.scheduler_set_unavailable is False
        self._assert_not_told(teardown_hass)


class TestDisableSchedulerIsDescribedToTheUser:
    """C-020: an action with no name or description is not usable from the UI.

    HA renders the action picker from ``services.yaml`` and its labels from
    the runtime ``translations/<lang>.json`` — **not** from ``strings.json``,
    which is the developer/Lokalise source.  A locale missing the entry
    shows the bare key or falls back to English, which is the exact live
    failure ``tests/test_runtime_translations_issues.py`` was written for.
    """

    _LOCALES = (
        "en",
        "de",
        "es",
        "fr",
        "it",
        "ja",
        "nl",
        "pl",
        "pt",
        "zh-Hans",
    )

    def test_services_yaml_declares_the_action(self) -> None:
        import pathlib

        import yaml

        root = pathlib.Path(__file__).resolve().parents[1]
        path = root / "custom_components" / "foxess_control" / "services.yaml"
        data = yaml.safe_load(path.read_text())
        assert "disable_scheduler" in data, (
            "services.yaml has no disable_scheduler entry, so the action does "
            "not appear in the HA action picker at all"
        )
        entry = data["disable_scheduler"]
        assert entry.get("name"), "disable_scheduler has no name in services.yaml"
        assert entry.get("description"), (
            "disable_scheduler has no description — a lever with safety "
            "consequences needs one (C-020)"
        )

    def test_strings_json_declares_the_action(self) -> None:
        import json
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        path = root / "custom_components" / "foxess_control" / "strings.json"
        services = json.loads(path.read_text()).get("services", {})
        assert "disable_scheduler" in services, (
            "strings.json has no services.disable_scheduler block, so the "
            "action never reaches Lokalise and the locales drift"
        )
        assert services["disable_scheduler"].get("name")
        assert services["disable_scheduler"].get("description")

    @pytest.mark.parametrize("locale", _LOCALES)
    def test_every_locale_names_and_describes_the_action(self, locale: str) -> None:
        import json
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        path = (
            root
            / "custom_components"
            / "foxess_control"
            / "translations"
            / f"{locale}.json"
        )
        services = json.loads(path.read_text()).get("services", {})
        entry = services.get("disable_scheduler")
        assert entry is not None, (
            f"locale {locale!r} has no services.disable_scheduler entry — HA "
            "loads action labels from translations/<lang>.json, so this user "
            "sees the English fallback or the bare key (C-020)"
        )
        for field in ("name", "description"):
            value = entry.get(field, "")
            assert isinstance(value, str) and value.strip(), (
                f"locale {locale!r}: services.disable_scheduler.{field} must "
                f"be a non-empty string — got {value!r}"
            )

    @pytest.mark.parametrize("locale", _LOCALES)
    def test_every_locale_can_render_the_refusal(self, locale: str) -> None:
        """The refusal is raised with a ``translation_key``, so it needs one.

        Without an ``exceptions`` entry HA renders the bare key in the UI —
        the 2026-05-18 live failure, in a new place.
        """
        import json
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        path = (
            root
            / "custom_components"
            / "foxess_control"
            / "translations"
            / f"{locale}.json"
        )
        exceptions = json.loads(path.read_text()).get("exceptions", {})
        entry = exceptions.get("handback_refused")
        assert entry is not None, (
            f"locale {locale!r} has no exceptions.handback_refused entry — "
            "the action's refusal renders as the bare translation key"
        )
        message = entry.get("message", "")
        assert isinstance(message, str) and message.strip()
        assert "{reason}" in message, (
            f"locale {locale!r}: exceptions.handback_refused.message drops "
            "the {reason} placeholder, so the user is told the action "
            "refused but not why (C-020)"
        )
