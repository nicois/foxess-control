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
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from custom_components.foxess_control._helpers import (
    STORAGE_KEY,
    STORAGE_VERSION,
    _dd,
)
from custom_components.foxess_control._min_soc_capture import (
    async_recapture_on_opt_in,
    async_setup_min_soc_capture,
    load_captured_min_soc,
    restore_min_soc_on_grid,
)
from custom_components.foxess_control.const import (
    CONF_SCHEDULER_HANDBACK,
    DEFAULT_SCHEDULER_HANDBACK,
    DOMAIN,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    IntegrationConfig,
    build_config,
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


def _set_handback(hass: Any, enabled: bool) -> None:
    """Rebuild ``dd.config`` with the handback option set (C-035).

    The fixture deliberately leaves it **off**, matching the shipped
    default, so any test that expects the integration to write to the
    user's Min SoC register has to ask for it in as many words.
    """
    dd = _dd(hass)
    assert dd.config is not None
    dd.config = IntegrationConfig(
        **{**dd.config.__dict__, "scheduler_handback": enabled}
    )


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
        ``TestDirectSettingsAndScheduleAreIndependent`` in
        tests/test_handback_foxess.py).  Treating it as "unclean" would
        mean capture never happened on a real install, and the feature
        would silently restore nothing forever.
        """
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "7")
        inv.self_use(min_soc_on_grid=_CONFIGURED_MIN_SOC)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid == 7


class TestCrashRecovery:
    """A stored value is the user's value, and outranks the device.

    Every test here opts in, because writing to the user's Min SoC register
    is only ever licensed by the option — see
    :class:`TestTheOptionGatesTheWrite`.
    """

    @pytest.mark.asyncio
    async def test_stored_zero_is_written_back_over_a_leftover_floor(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Store holds 0; the device reports 11 from a run that died."""
        inv = _inv(foxess_sim)
        _set_handback(capture_hass, True)
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
        ``TestDirectMinSocOnGrid`` in tests/test_handback_foxess.py): a
        restore that satisfied only one of them would be broken on real
        hardware with every test green.
        """
        inv = _inv(foxess_sim)
        _set_handback(capture_hass, True)
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
        _set_handback(capture_hass, True)
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


class TestTheOptionGatesTheWrite:
    """Opt-in, default off — and "off" has to mean *no writes at all*.

    The crash-recovery restore is the one place this feature writes to the
    user's inverter without a ``plan_handback`` decision in front of it: it
    runs at setup, before any plan exists.  Ungated, it would write to a
    register the integration has no business touching on an install that
    never opted in — and because the captured value is authoritative and
    never re-read, a user who changed their floor in the FoxESS app and
    then restarted would be silently reverted, permanently.  That would
    break the guarantee the whole feature rests on: **existing installs
    behave exactly as they did before the upgrade**.

    Capture is deliberately *not* gated.  It is a read, it is harmless, and
    reading early is the only way the value can be the user's own if they
    opt in later — waiting until opt-in would mean reading a register the
    integration might by then have written to.
    """

    @pytest.mark.asyncio
    async def test_option_off_does_not_write_to_the_device(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """The named regression: store 0, device 11, option off → no write."""
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 0)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert foxess_sim.state()["min_soc_on_grid"] == 11, (
            "handback is off, yet the integration overwrote the inverter's "
            "Min SoC floor — an install that never opted in must behave "
            "exactly as it did before the upgrade"
        )

    @pytest.mark.asyncio
    async def test_option_off_still_remembers_the_captured_value(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Declining to write must not mean forgetting."""
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 0)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert _dd(capture_hass).captured_min_soc_on_grid == 0
        assert await load_captured_min_soc(capture_hass) == 0

    @pytest.mark.asyncio
    async def test_option_on_does_write(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Identical state, option on: the leftover floor is corrected."""
        inv = _inv(foxess_sim)
        _set_handback(capture_hass, True)
        await _seed_store(capture_hass, 0)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert foxess_sim.state()["min_soc_on_grid"] == 0

    @pytest.mark.asyncio
    async def test_capture_still_happens_with_the_option_off(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """So opting in later has a genuine value to work with."""
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "7")

        await async_setup_min_soc_capture(capture_hass, inv)

        assert await load_captured_min_soc(capture_hass) == 7
        assert _dd(capture_hass).captured_min_soc_on_grid == 7

    @pytest.mark.asyncio
    async def test_capture_with_the_option_off_writes_nothing(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Capturing is a read.  Nothing about it may change the device."""
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        inv.set_min_soc(min_soc=5, min_soc_on_grid=7)
        before = foxess_sim.state()

        await async_setup_min_soc_capture(capture_hass, inv)

        after = foxess_sim.state()
        assert after["min_soc_on_grid"] == before["min_soc_on_grid"] == 7
        assert after["min_soc"] == before["min_soc"] == 5
        assert after["schedule_groups"] == before["schedule_groups"]
        assert after["work_mode_direct"] == before["work_mode_direct"]
        assert after["scheduler_enabled"] == before["scheduler_enabled"]

    @pytest.mark.asyncio
    async def test_turning_the_option_back_off_stops_the_writes(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """On → off must be a real off, not a one-way door."""
        inv = _inv(foxess_sim)
        _set_handback(capture_hass, True)
        await _seed_store(capture_hass, 0)
        _write_session_floor(inv, 11)
        await async_setup_min_soc_capture(capture_hass, inv)
        assert foxess_sim.state()["min_soc_on_grid"] == 0

        _set_handback(capture_hass, False)
        _write_session_floor(inv, 11)

        await async_setup_min_soc_capture(capture_hass, inv)

        assert foxess_sim.state()["min_soc_on_grid"] == 11, (
            "the restore kept firing after the option was turned off"
        )


class TestOptingInRecapturesTheFloor:
    """Off → on is the one moment the register is unambiguously the user's.

    The captured value is authoritative and never re-read, so without this
    a handback user who legitimately changes their floor in the FoxESS app
    would be reverted forever with no way back.  The transition itself is
    the remedy: at the instant someone opts in, whatever the register holds
    is by definition what they chose, so it is re-read then — and toggling
    the option off and on again is a manual remedy that needs no new
    service and no new UI.

    Re-capture keeps every guard the first capture has.  In particular it
    will not read a register a session owns, and a re-capture that declines
    **keeps the previously captured value** rather than trading a stale
    floor for no floor at all.
    """

    @pytest.mark.asyncio
    async def test_opting_in_replaces_the_stored_value(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        inv.set_setting("MinSocOnGrid", "3")

        replaced = await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=False, now_enabled=True
        )

        assert replaced is True
        assert await load_captured_min_soc(capture_hass) == 3, (
            "opting in did not re-read the floor, so a user who changed it "
            "in the FoxESS app is stuck with the old one forever"
        )
        assert _dd(capture_hass).captured_min_soc_on_grid == 3

    @pytest.mark.asyncio
    async def test_opting_in_can_recapture_zero(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """The issue-#4 value must not be lost to a truthiness test."""
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        inv.set_setting("MinSocOnGrid", "0")

        await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=False, now_enabled=True
        )

        assert await load_captured_min_soc(capture_hass) == 0

    @pytest.mark.asyncio
    async def test_opting_in_writes_nothing_to_the_device(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Re-capture is a read; the restore is the reload's job."""
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        inv.set_min_soc(min_soc=5, min_soc_on_grid=3)
        before = foxess_sim.state()

        await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=False, now_enabled=True
        )

        after = foxess_sim.state()
        assert after["min_soc_on_grid"] == before["min_soc_on_grid"] == 3
        assert after["min_soc"] == before["min_soc"] == 5
        assert after["schedule_groups"] == before["schedule_groups"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("was_enabled", "now_enabled"),
        [
            (True, True),  # an unrelated option changed
            (True, False),  # opting out
            (False, False),  # never opted in
        ],
    )
    async def test_only_the_off_to_on_transition_recaptures(
        self,
        foxess_sim: SimulatorHandle,
        capture_hass: Any,
        was_enabled: bool,
        now_enabled: bool,
    ) -> None:
        """Every other options change must leave the captured floor alone.

        Re-capturing on *any* options edit would reintroduce the very
        hazard this exists to close: an options save during a session, or
        after a handback wrote the register, would record a value the user
        never chose.
        """
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        inv.set_setting("MinSocOnGrid", "3")

        replaced = await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=was_enabled, now_enabled=now_enabled
        )

        assert replaced is False
        assert await load_captured_min_soc(capture_hass) == 7

    @pytest.mark.asyncio
    async def test_opting_in_mid_session_keeps_the_old_value(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """A declined re-capture must not trade a stale floor for none.

        Losing the captured value would mean restoring nothing forever
        after, which is strictly worse than restoring a slightly stale
        floor the user did once choose.
        """
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        _write_session_floor(inv, 11)
        _dd(capture_hass).smart_discharge_state = {"min_soc": 11}

        replaced = await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=False, now_enabled=True
        )

        assert replaced is False
        assert await load_captured_min_soc(capture_hass) == 7, (
            "a re-capture that could not read a clean value threw away the "
            "value it already had"
        )

    @pytest.mark.asyncio
    async def test_opting_in_with_a_managed_group_keeps_the_old_value(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        _pin_midday(foxess_sim)
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        inv.force_discharge(min_soc=20, power=3000)
        _write_session_floor(inv, 11)

        replaced = await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=False, now_enabled=True
        )

        assert replaced is False
        assert await load_captured_min_soc(capture_hass) == 7

    @pytest.mark.asyncio
    async def test_opting_in_captures_for_the_first_time(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """Nothing stored yet — e.g. every earlier setup found a session."""
        inv = _inv(foxess_sim)
        inv.set_setting("MinSocOnGrid", "3")

        replaced = await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=False, now_enabled=True
        )

        assert replaced is True
        assert await load_captured_min_soc(capture_hass) == 3

    @pytest.mark.asyncio
    async def test_api_failure_on_opt_in_keeps_the_old_value(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """An options save must never raise, and never lose the floor."""
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        foxess_sim.fault("api_500")

        replaced = await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=False, now_enabled=True
        )

        assert replaced is False
        assert await load_captured_min_soc(capture_hass) == 7

    @pytest.mark.asyncio
    async def test_entity_mode_does_not_recapture(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        dd = _dd(capture_hass)
        assert dd.config is not None
        dd.config = IntegrationConfig(**{**dd.config.__dict__, "entity_mode": True})
        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        inv.set_setting("MinSocOnGrid", "3")

        replaced = await async_recapture_on_opt_in(
            capture_hass, inv, was_enabled=False, now_enabled=True
        )

        assert replaced is False
        assert await load_captured_min_soc(capture_hass) == 7

    @pytest.mark.asyncio
    async def test_no_inverter_does_not_recapture(self, capture_hass: Any) -> None:
        await _seed_store(capture_hass, 7)

        replaced = await async_recapture_on_opt_in(
            capture_hass, None, was_enabled=False, now_enabled=True
        )

        assert replaced is False
        assert await load_captured_min_soc(capture_hass) == 7


class TestOptionsUpdateWiring:
    """The transition has to be visible where it is actually detected.

    Home Assistant calls the update listener *after* ``entry.options`` has
    been replaced but *before* the reload rebuilds ``dd.config``, so
    ``dd.config`` is the only place the previous value still exists.  An
    implementation that read the new value on both sides would compare it
    to itself and never detect anything — and every unit test of
    ``async_recapture_on_opt_in`` would still pass, because it is handed
    the two values directly.  Hence this one test of the wiring.

    Re-capture runs *before* the reload deliberately: the reload's own
    crash-recovery pass would otherwise write the stale floor back to the
    device and then the re-capture would read the value we had just
    written, laundering our number into the user's.
    """

    @pytest.mark.asyncio
    async def test_opting_in_through_the_options_flow_recaptures(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        from custom_components.foxess_control import _async_update_options
        from custom_components.foxess_control.domain_data import FoxESSEntryData

        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        inv.set_setting("MinSocOnGrid", "3")
        dd = _dd(capture_hass)
        # dd.config still carries the PREVIOUS options (handback off).
        dd.entries["e1"] = FoxESSEntryData(coordinator=MagicMock(), inverter=inv)
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.options = {CONF_SCHEDULER_HANDBACK: True}
        capture_hass.config_entries = MagicMock()
        capture_hass.config_entries.async_reload = AsyncMock()

        await _async_update_options(capture_hass, entry)

        assert await load_captured_min_soc(capture_hass) == 3, (
            "opting in through the options flow did not re-read the floor"
        )
        capture_hass.config_entries.async_reload.assert_awaited_once_with("e1")

    @pytest.mark.asyncio
    async def test_an_unrelated_options_change_leaves_the_floor_alone(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        from custom_components.foxess_control import _async_update_options
        from custom_components.foxess_control.domain_data import FoxESSEntryData

        inv = _inv(foxess_sim)
        await _seed_store(capture_hass, 7)
        inv.set_setting("MinSocOnGrid", "3")
        dd = _dd(capture_hass)
        dd.entries["e1"] = FoxESSEntryData(coordinator=MagicMock(), inverter=inv)
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.options = {"polling_interval": 120}
        capture_hass.config_entries = MagicMock()
        capture_hass.config_entries.async_reload = AsyncMock()

        await _async_update_options(capture_hass, entry)

        assert await load_captured_min_soc(capture_hass) == 7
        capture_hass.config_entries.async_reload.assert_awaited_once_with("e1")

    @pytest.mark.asyncio
    async def test_a_failed_recapture_still_reloads_the_entry(
        self, foxess_sim: SimulatorHandle, capture_hass: Any
    ) -> None:
        """The reload is the listener's actual job; nothing may block it."""
        from custom_components.foxess_control import _async_update_options

        await _seed_store(capture_hass, 7)
        # No entry data at all — the most awkward shape the hook can meet.
        entry = MagicMock()
        entry.entry_id = "missing"
        entry.options = {CONF_SCHEDULER_HANDBACK: True}
        capture_hass.config_entries = MagicMock()
        capture_hass.config_entries.async_reload = AsyncMock()

        await _async_update_options(capture_hass, entry)

        capture_hass.config_entries.async_reload.assert_awaited_once_with("missing")


class TestTheOptionItself:
    """The option is opt-in and defaults off, in the config layer too."""

    def test_default_is_off(self) -> None:
        assert DEFAULT_SCHEDULER_HANDBACK is False

    def test_build_config_defaults_it_off(self) -> None:
        """An existing install's options dict has no such key (C-035)."""
        assert build_config({}).scheduler_handback is False

    @pytest.mark.parametrize("value", [True, False])
    def test_build_config_reads_the_option(self, value: bool) -> None:
        config = build_config({CONF_SCHEDULER_HANDBACK: value})
        assert config.scheduler_handback is value
