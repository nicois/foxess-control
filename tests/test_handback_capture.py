"""Capturing and restoring the user's own persistent Min SoC floor.

``plan_handback`` (tests/test_handback_policy.py) enforces one rule above
all others: *never invent a Min SoC — restore only what was captured*.
**That rule is only as strong as the capture.**  If
``captured_min_soc_on_grid`` is read *after* something has written a
session value into ``MinSocOnGrid``, the policy layer will faithfully
restore the session value, and the decision layer will look immaculate
while doing it.

That is not hypothetical.  This integration has already shipped exactly
that defect: an adapter wrote a session target into the persistent Min SoC
floor and never put the user's value back, so once the session ended the
inverter imported from the grid to hold a floor the user never chose
(P-001, P-002).  These tests exist to make it impossible to reintroduce.

The four properties under test, in the order they matter:

1. **The captured value predates any integration write.**  Capture happens
   at most once and is then authoritative; nothing re-reads the device
   later, because any later read may see a session value.
2. **A session value is never captured.**  If the device is not in a clean
   state, capture declines and stores nothing.
3. **Crash recovery comes first.**  A stored value is the user's value.  A
   previous run may have died with a session floor on the device, so the
   stored value is written back — never overwritten by what the device
   currently reports.
4. **"Not captured" means restore nothing.**  A default is never
   substituted, and ``None`` is a genuine no-op that leaves the device
   untouched, not merely an absence of exceptions.

Everything drives the real ``Inverter`` over HTTP against a fresh
simulator (C-028) and a real Home Assistant ``Store``, and asserts on
observable device and storage state — never on which method was called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from custom_components.foxess_control._helpers import (
    STORAGE_KEY,
    STORAGE_VERSION,
    _dd,
)
from custom_components.foxess_control._min_soc_capture import (
    async_setup_min_soc_capture,
    load_captured_min_soc,
    restore_min_soc_on_grid,
)
from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    IntegrationConfig,
)
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter
from custom_components.foxess_control.handback import plan_handback

if TYPE_CHECKING:
    import pathlib

    from .conftest import SimulatorHandle

# The persisted key, spelled out rather than imported.  A capture is only
# useful if it survives a restart, so the on-disk name is part of the
# contract: renaming it must be a deliberate migration, not a refactor
# that quietly orphans every existing install's captured floor.
_STORE_KEY = "user_min_soc_on_grid"

# The integration's *configured* min-SoC-on-grid, deliberately non-zero and
# different from every user value used below.  Any implementation that
# substitutes this default instead of restoring what it captured shows up
# as a wrong number rather than as a silent pass.
_CONFIGURED_MIN_SOC = 11


@pytest.fixture(autouse=True)
def _disable_throttle() -> None:
    """Disable request throttling (and retry backoff) in tests."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


@pytest_asyncio.fixture  # type: ignore[untyped-decorator]
async def capture_hass(tmp_path: pathlib.Path) -> Any:
    """A real HomeAssistant with a real Store rooted in *tmp_path*.

    ``HomeAssistant()`` captures the running event loop in ``__init__``, so
    it must be built inside an async context (mirrors
    tests/test_schedule_reconcile.py).  The config dir is per-test so no
    captured value leaks between tests (C-028's independence rule applies
    to storage as much as to the simulator).
    """
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.storage import Store

    ha = HomeAssistant(str(tmp_path))
    ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
    ha.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    dd = FoxESSControlData()
    dd.config = IntegrationConfig(
        min_soc_on_grid=_CONFIGURED_MIN_SOC,
        api_min_soc=11,
        battery_capacity_kwh=10.0,
        min_power_change=100,
        max_power_w=10000,
        grid_export_limit_w=5000,
        smart_headroom=0.10,
        bms_polling_interval=300.0,
        ws_mode="auto",
        entity_mode=False,
    )
    dd.store = Store[dict[str, Any]](ha, STORAGE_VERSION, STORAGE_KEY)
    ha.data[DOMAIN] = dd
    return ha


def _inv(sim: SimulatorHandle) -> Inverter:
    return Inverter(FoxESSClient("test-api-key", base_url=sim.url), "SIM0001")


def _pin_midday(sim: SimulatorHandle) -> None:
    """Pin simulated time inside every full-day window (see C-031).

    Whole-day groups run 00:00-23:59 and the simulator matches
    ``start <= now < end``, so at 23:59 exactly no group is active.
    """
    sim.set(sim_time="2026-01-15T12:00:00+00:00")


async def _seed_store(hass: Any, value: int) -> None:
    """Persist *value* as a previous run's capture."""
    store = _dd(hass).store
    assert store is not None
    stored = await store.async_load() or {}
    stored[_STORE_KEY] = value
    await store.async_save(stored)


async def _stored_value(hass: Any) -> Any:
    store = _dd(hass).store
    assert store is not None
    return (await store.async_load() or {}).get(_STORE_KEY)


def _write_session_floor(inv: Inverter, value: int) -> None:
    """Put a session floor in the persistent register.

    Uses ``battery/soc/set`` — the very API surface the shipped defect used
    to write a session target into the user's persistent floor.  Capture
    must never see this value.
    """
    inv.set_min_soc(min_soc=10, min_soc_on_grid=value)


class TestCapturePredatesAnyIntegrationWrite:
    """The test that would have caught the historical defect."""

    @pytest.mark.asyncio
    async def test_captured_value_survives_a_later_session_write(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Capture 7, run a real session, and the capture must still say 7.

        A second setup — an HA restart or a config-entry reload, both of
        which re-run ``async_setup_entry`` — must not re-read the device.
        By then the register may hold a session floor, and re-reading is
        precisely how a session value becomes "the user's value" forever.
        """
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "7")

        await async_setup_min_soc_capture(capture_hass, inv)
        assert _dd(capture_hass).captured_min_soc_on_grid == 7
        assert await _stored_value(capture_hass) == 7

        # A real session write, and the register moved to a session floor.
        inv.force_discharge(min_soc=20, power=3000)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid == 7, (
            "the captured floor became the session value — the shipped "
            "defect, reproduced through the capture instead of the policy"
        )
        assert await _stored_value(capture_hass) == 7

    @pytest.mark.asyncio
    async def test_capture_is_not_repeated_once_stored(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Even a *clean* device must not re-capture over a stored value.

        A user who changes their floor in the FoxESS app is a different
        problem (and a different feature); an opportunistic re-read is not
        the answer to it, because the same code path also runs when the
        register holds a session value.
        """
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "7")
        await async_setup_min_soc_capture(capture_hass, inv)

        inv.set_setting("MinSocOnGrid", "19")
        await async_setup_min_soc_capture(capture_hass, inv)

        assert await _stored_value(capture_hass) == 7
        assert _dd(capture_hass).captured_min_soc_on_grid == 7


class TestNeverCapturesASessionValue:
    """An unclean device yields nothing, never a guess."""

    @pytest.mark.asyncio
    async def test_declines_with_a_managed_group_on_the_inverter(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        inv.force_discharge(min_soc=20, power=3000)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid is None, (
            "captured a session floor as if the user had chosen it"
        )
        assert await _stored_value(capture_hass) is None

    @pytest.mark.asyncio
    async def test_declining_leaves_the_device_alone(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Declining must be inert, not "declining plus a tidy-up write"."""
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        inv.force_discharge(min_soc=20, power=3000)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert foxess_sim.state()["min_soc_on_grid"] == 11

    @pytest.mark.asyncio
    async def test_declines_while_a_session_is_active(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """A recovered session owns the floor; its value is not the user's."""
        inv = _inv(foxess_sim)
        _write_session_floor(inv, 11)
        _dd(capture_hass).smart_discharge_state = {"min_soc": 11}

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid is None
        assert await _stored_value(capture_hass) is None

    @pytest.mark.asyncio
    async def test_declines_while_a_charge_session_is_active(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        inv = _inv(foxess_sim)
        _write_session_floor(inv, 11)
        _dd(capture_hass).smart_charge_state = {"target_soc": 100}

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid is None

    @pytest.mark.asyncio
    async def test_a_self_use_baseline_group_does_not_block_capture(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Guards against a gate so strict the feature never arms.

        A whole-day SelfUse group is the *normal* idle state this
        integration leaves behind after every session (C-025), and group
        values never move the persistent register (pinned by
        test_handback_foxess.py::test_schedule_write_leaves_min_soc_on_grid
        _setting_alone).  Treating it as "unclean" would mean capture never
        happens on a real install, and the feature would silently restore
        nothing forever.
        """
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "7")
        inv.self_use(min_soc_on_grid=_CONFIGURED_MIN_SOC)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid == 7


class TestCrashRecovery:
    """A stored value is the user's value, and outranks the device."""

    @pytest.mark.asyncio
    async def test_stored_zero_is_written_back_over_a_leftover_floor(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Store holds 0; the device reports 11 from a run that died."""
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 0)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert inv.get_min_soc()["minSocOnGrid"] == 0, (
            "a leftover session floor outlived the restart; the inverter "
            "would import from the grid to hold a floor the user never "
            "chose (P-001, P-002)"
        )
        assert _dd(capture_hass).captured_min_soc_on_grid == 0
        assert await _stored_value(capture_hass) == 0, (
            "the device value overwrote the store — the captured floor is "
            "supposed to be the thing that cannot be lost"
        )

    @pytest.mark.asyncio
    async def test_restores_through_the_documented_register(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """``setting/get`` must agree, since it is the same register.

        Two API surfaces onto one device register (pinned by
        test_handback_foxess.py::test_setting_and_battery_soc_endpoints_are
        _one_value): a restore that only satisfied one of them would be
        broken on real hardware with every test green.
        """
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 0)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert inv.get_setting("MinSocOnGrid")["value"] == "0"


class TestRestoreIsSurgical:
    """Restoring the on-grid floor must move nothing else."""

    @pytest.mark.asyncio
    async def test_restore_does_not_move_the_off_grid_min_soc(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """``battery/soc/set`` writes *both* registers in one request.

        Restoring only the captured ``minSocOnGrid`` through it would mean
        inventing a ``minSoc`` — the exact sin this whole task exists to
        prevent, just aimed at the other register (P-002).
        """
        inv = _inv(foxess_sim)
        inv.set_min_soc(min_soc=5, min_soc_on_grid=7)

        await restore_min_soc_on_grid(capture_hass, inv, 0)

        assert inv.get_min_soc()["minSocOnGrid"] == 0
        assert inv.get_min_soc()["minSoc"] == 5, (
            "restoring the on-grid floor moved the off-grid floor too"
        )

    @pytest.mark.asyncio
    async def test_restore_creates_no_schedule_group(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """The floor is a device setting; handback must stay off the scheduler."""
        inv = _inv(foxess_sim)

        await restore_min_soc_on_grid(capture_hass, inv, 4)

        assert foxess_sim.state()["schedule_groups"] == []
        assert foxess_sim.state()["min_soc_on_grid"] == 4


class TestRestoreNothingWhenNothingWasCaptured:
    """``None`` is a real answer, and it means *do not touch the device*."""

    @pytest.mark.asyncio
    async def test_none_writes_nothing_at_all(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        inv = _inv(foxess_sim)
        inv.set_min_soc(min_soc=5, min_soc_on_grid=13)
        before = foxess_sim.state()

        result = await restore_min_soc_on_grid(capture_hass, inv, None)

        assert result is False
        after = foxess_sim.state()
        assert after["min_soc_on_grid"] == before["min_soc_on_grid"] == 13, (
            "restoring nothing changed the floor"
        )
        assert after["min_soc"] == before["min_soc"] == 5
        assert after["schedule_groups"] == before["schedule_groups"]

    @pytest.mark.asyncio
    async def test_none_does_not_fall_back_to_the_configured_default(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """The configured option is the integration's number, not the user's.

        ``min_soc_on_grid`` in the integration options is what *sessions*
        pass to the scheduler.  Writing it into the persistent register on
        a handback would be the shipped defect with a tidier spelling.
        """
        inv = _inv(foxess_sim)
        inv.set_min_soc(min_soc=5, min_soc_on_grid=13)

        await restore_min_soc_on_grid(capture_hass, inv, None)

        assert foxess_sim.state()["min_soc_on_grid"] != _CONFIGURED_MIN_SOC
        assert foxess_sim.state()["min_soc_on_grid"] == 13


class TestIssue4EndToEnd:
    """A user floor of 0 % must survive a smart session.

    Issue #4: the Mode Scheduler declares ``minsocongrid.range.min = 10``
    and rejects less with errno 40257, so a session's group *cannot* carry
    0.  Handing the inverter back to its own settings is the only way a 0 %
    floor can hold, and this is that path end to end at this layer —
    through the real ``plan_handback`` decision, not around it.
    """

    @pytest.mark.asyncio
    async def test_zero_floor_survives_a_session(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "0")

        await async_setup_min_soc_capture(capture_hass, inv)
        assert _dd(capture_hass).captured_min_soc_on_grid == 0, (
            "0 is falsy; a truthiness test somewhere just lost issue #4"
        )

        inv.force_discharge(min_soc=20, power=3000)
        written = [
            g
            for g in foxess_sim.state()["schedule_groups"]
            if g["workMode"] == "ForceDischarge"
        ]
        assert written and written[0]["minSocOnGrid"] >= 10, (
            "premise: the scheduler cannot express a floor below 10%"
        )
        # ...and the register itself now holds a session floor, which is the
        # state the user was left in before this feature existed.
        _write_session_floor(inv, 11)

        # The session ends.  Handback reads the captured floor back out of
        # storage — not out of memory, and never out of the device — which is
        # the path a restart takes.
        plan = plan_handback(
            enabled=True,
            entity_mode=False,
            session_active=False,
            unmanaged_modes=[],
            scheduler_supported=True,
            captured_min_soc_on_grid=await load_captured_min_soc(capture_hass),
        )
        assert plan.act is True
        assert plan.restore_min_soc_on_grid == 0, (
            "0 did not survive the round trip through storage — a falsy-vs-None "
            "confusion here silently turns issue #4 back into 'restore nothing'"
        )
        await restore_min_soc_on_grid(capture_hass, inv, plan.restore_min_soc_on_grid)

        assert inv.get_min_soc()["minSocOnGrid"] == 0, (
            "the user's 0% floor did not survive the session"
        )
        assert foxess_sim.state()["min_soc_on_grid"] != _CONFIGURED_MIN_SOC


class TestCaptureFailureIsSurvivable:
    """A failed capture must cost nothing but the capture."""

    @pytest.mark.asyncio
    async def test_api_failure_does_not_raise(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Setup must complete: this runs inside ``async_setup_entry``."""
        inv = _inv(foxess_sim)
        foxess_sim.fault("api_500")

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid is None
        assert await _stored_value(capture_hass) is None

    @pytest.mark.asyncio
    async def test_a_failed_capture_stores_no_guess(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Retrying later is fine; recording a guess is not."""
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "7")
        foxess_sim.fault("api_500")

        await async_setup_min_soc_capture(capture_hass, inv)
        assert await _stored_value(capture_hass) is None

        foxess_sim.clear_fault()
        await async_setup_min_soc_capture(capture_hass, inv)

        assert await _stored_value(capture_hass) == 7
        assert _dd(capture_hass).captured_min_soc_on_grid == 7

    @pytest.mark.asyncio
    async def test_a_failed_restore_does_not_raise(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Crash recovery must not be able to abort setup either."""
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 0)
        foxess_sim.fault("api_500")

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid == 0, (
            "a failed write must not lose the captured value; the next "
            "setup has to be able to try again"
        )

    @pytest.mark.asyncio
    async def test_entity_mode_captures_nothing(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """No cloud Mode Scheduler, no handback, nothing to capture."""
        dd = _dd(capture_hass)
        assert dd.config is not None
        dd.config = IntegrationConfig(
            **{**dd.config.__dict__, "entity_mode": True},
        )
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "7")

        await async_setup_min_soc_capture(capture_hass, inv)

        assert dd.captured_min_soc_on_grid is None
        assert await _stored_value(capture_hass) is None


class TestLoadCapturedMinSoc:
    """The store accessor the handback execution will read."""

    @pytest.mark.asyncio
    async def test_absent_is_none(self, capture_hass: Any) -> None:
        assert await load_captured_min_soc(capture_hass) is None

    @pytest.mark.asyncio
    async def test_zero_round_trips(self, capture_hass: Any) -> None:
        """0 must survive the store, not be flattened to "nothing stored"."""
        await _seed_store(capture_hass, 0)
        assert await load_captured_min_soc(capture_hass) == 0
