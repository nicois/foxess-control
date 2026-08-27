"""Capture and restore the user's own persistent Min SoC on grid.

``handback.plan_handback`` enforces one rule above all others: *never
invent a Min SoC — restore only what was captured*.  **That rule is only
as strong as the capture.**  If the captured value is read after something
has written a session value into ``MinSocOnGrid``, the policy layer will
faithfully restore the session value, and the decision layer will look
immaculate while doing it.

That is not hypothetical.  This integration has already shipped exactly
that defect: an adapter wrote a session target into the persistent Min SoC
floor and never put the user's value back, so once the session ended the
inverter imported from the grid to hold a floor the user never chose
(P-001, P-002).  This module is where that must not happen again, and it
is deliberately paranoid about four things:

1. **Capture at most once, and persist it.**  Once a value is captured it
   is authoritative and is never re-read.  Any later read may see a
   session value, so "refresh it while we're here" is the bug, not a
   convenience.

2. **Crash recovery comes first.**  If the Store already holds a value,
   that *is* the user's value: a previous run may have died mid-session
   leaving a session floor on the device.  So the stored value is written
   back, and never overwritten by what the device currently reports.

3. **Only capture from a clean device.**  With nothing stored yet, the
   register is read only when no session is active and no managed
   *override* group (ForceCharge/ForceDischarge/Feedin) is on the
   inverter.  Ordering matters: this runs *after*
   ``_schedule_reconcile.reconcile_schedule``, which removes orphaned
   managed groups left by a teardown that did not complete — so "clean"
   means clean, not "clean apart from yesterday's leftovers".

4. **"Not captured" is honest.**  If capture is impossible — API failure,
   unclean device — nothing is stored, ``plan_handback`` receives ``None``
   and restores nothing.  A later setup may try again.  Guessing is never
   an option, and neither is raising: this runs inside
   ``async_setup_entry`` and must not be able to break setup or a session.

Reads go through ``Inverter.get_min_soc`` (``battery/soc/get``, the
documented Open API surface).  Writes go through
``Inverter.set_setting("MinSocOnGrid", …)`` rather than
``set_min_soc``, because ``battery/soc/set`` writes ``minSoc`` *and*
``minSocOnGrid`` in one request: restoring only the captured on-grid floor
through it would mean inventing an off-grid one — the same sin, aimed at
the other register.  That the two API surfaces are one device register is
pinned by ``TestDirectMinSocOnGrid`` in
``tests/test_handback_foxess.py``.

Brand-specific (the FoxESS Mode Scheduler and its 10 % ``minsocongrid``
floor are FoxESS concepts), so it lives here, not in ``smart_battery/``
(C-021, C-039).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .foxess import WorkMode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .foxess.inverter import Inverter

_LOGGER = logging.getLogger(__name__)

# The persisted key.  Part of the on-disk contract: renaming it orphans
# every existing install's captured floor, so it must be a deliberate
# migration rather than a refactor.
_STORE_KEY = "user_min_soc_on_grid"

# The device setting that is the same register as ``minSocOnGrid`` in
# ``battery/soc/{get,set}``.
_SETTING_MIN_SOC_ON_GRID = "MinSocOnGrid"

# An enabled group in one of these modes means a session's override is on
# the inverter, so the device is not in a state worth capturing from.
# SelfUse is excluded deliberately: a whole-day SelfUse group is the
# *normal* idle state this integration leaves behind after every session
# (C-025), and group values never move the persistent register — treating
# it as unclean would mean capture never happens on a real install and the
# feature would silently restore nothing forever.
_MANAGED_OVERRIDE_MODES = (
    WorkMode.FORCE_CHARGE.value,
    WorkMode.FORCE_DISCHARGE.value,
    WorkMode.FEEDIN.value,
)


def has_managed_override_group(groups: list[dict[str, Any]]) -> bool:
    """Is a managed override group present and enabled on the inverter?

    Presence, not "in force right now".  A FoxESS group recurs daily, so a
    ForceDischarge group outside its window today will drive the inverter
    again tomorrow; its existence is what says a session owns this device.
    Being time-independent also keeps the capture decision free of
    once-a-day flakes (C-031).
    """
    return any(
        g.get("enable") == 1 and g.get("workMode") in _MANAGED_OVERRIDE_MODES
        for g in groups
    )


async def load_captured_min_soc(hass: HomeAssistant) -> int | None:
    """The captured floor from persistent storage, or None if never captured.

    ``0`` is a real captured value (issue #4 is precisely about a 0 % floor),
    so this must distinguish it from "absent" — a truthiness test here
    silently turns issue #4 back into "restore nothing".
    """
    from ._helpers import _dd

    store = _dd(hass).store
    if store is None:
        return None
    try:
        stored: dict[str, Any] = await store.async_load() or {}
    except Exception:  # noqa: BLE001 — a corrupt store must not break setup
        _LOGGER.debug("Could not read the captured Min SoC", exc_info=True)
        return None
    value = stored.get(_STORE_KEY)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        _LOGGER.debug("Stored Min SoC %r is not a number; ignoring it", value)
        return None


async def _save_captured_min_soc(hass: HomeAssistant, value: int) -> None:
    """Persist *value* alongside the other keys already in the Store."""
    from ._helpers import _dd

    store = _dd(hass).store
    if store is None:
        return
    stored: dict[str, Any] = await store.async_load() or {}
    stored[_STORE_KEY] = value
    await store.async_save(stored)


async def restore_min_soc_on_grid(
    hass: HomeAssistant, inverter: Inverter, value: int | None
) -> bool:
    """Write *value* back to the persistent Min SoC register.

    ``None`` means **nothing was captured**, which means do nothing at all —
    not "write a sensible default".  Returns whether the device was written.

    Verbatim: no clamping to the scheduler's 10 % minimum (the whole point
    of issue #4 is that the register accepts less than the scheduler does)
    and no rounding.  Only ``MinSocOnGrid`` is touched, so the off-grid
    ``minSoc`` the user chose is left alone.
    """
    if value is None:
        _LOGGER.debug(
            "No captured Min SoC on grid to restore; leaving the inverter's "
            "own floor untouched"
        )
        return False
    await hass.async_add_executor_job(
        inverter.set_setting, _SETTING_MIN_SOC_ON_GRID, str(value)
    )
    return True


async def async_setup_min_soc_capture(
    hass: HomeAssistant, inverter: Inverter | None
) -> None:
    """Capture the user's Min SoC floor, or restore it after a crash.

    Called once from ``async_setup_entry``, **after**
    ``reconcile_schedule`` — see the module docstring for why the ordering
    is load-bearing.  Cloud backend only.  Never raises: this sits in the
    setup path, and a floor we could not capture is a feature that stays
    dormant, not an integration that fails to load.

    Runs regardless of whether handback is enabled.  Capturing early is the
    only way the value can be the user's own: waiting until they opt in
    would mean reading the register after the integration might already
    have written to it.
    """
    try:
        from ._helpers import _cfg, _dd

        if inverter is None or _cfg(hass).entity_mode:
            return
        dd = _dd(hass)

        # 1. Crash recovery.  A stored value is the user's value, full stop.
        stored = await load_captured_min_soc(hass)
        if stored is not None:
            dd.captured_min_soc_on_grid = stored
            await _restore_after_restart(hass, inverter, stored)
            return

        # 2. Nothing captured yet — and a session owns the floor it holds.
        if dd.smart_charge_state is not None or dd.smart_discharge_state is not None:
            _LOGGER.debug(
                "Not capturing the Min SoC floor: a smart session is active, "
                "so the value on the device is not the user's own"
            )
            return

        # 3. ...nor is it, if a managed override group is on the inverter.
        schedule = await hass.async_add_executor_job(inverter.get_schedule)
        from .foxess_adapter import _is_placeholder

        groups = [g for g in schedule.get("groups", []) if not _is_placeholder(g)]
        if has_managed_override_group(groups):
            _LOGGER.warning(
                "Not capturing the inverter's Min SoC floor: a managed "
                "override group is still on the schedule, so the floor on "
                "the device may be a session value rather than your own.  "
                "Scheduler handback will restore nothing until a restart "
                "finds the inverter idle"
            )
            return

        # 4. Clean.  Read it once, and never again.
        settings = await hass.async_add_executor_job(inverter.get_min_soc)
        raw = settings.get("minSocOnGrid")
        if raw is None or isinstance(raw, bool):
            _LOGGER.debug("Inverter reported no minSocOnGrid; nothing captured")
            return
        value = int(raw)
        dd.captured_min_soc_on_grid = value
        await _save_captured_min_soc(hass, value)
        _LOGGER.info(
            "Captured the inverter's own Min SoC on grid (%s%%) — the only "
            "value a scheduler handback will ever put back",
            value,
        )
    except Exception:  # noqa: BLE001 — must never break integration setup
        _LOGGER.debug(
            "Could not capture the inverter's Min SoC on grid (non-critical); "
            "scheduler handback will restore nothing until a later restart "
            "succeeds",
            exc_info=True,
        )


async def _restore_after_restart(
    hass: HomeAssistant, inverter: Inverter, captured: int
) -> None:
    """Put the captured floor back if the device is holding something else.

    A previous run may have died with a session floor in the register, and
    a recurring floor the user never chose makes the inverter import from
    the grid to hold it (P-001, P-002).  Skipped when the device already
    agrees, so a healthy restart costs one read and no write.

    Failure is swallowed: the captured value stays in the Store, so the
    next setup can try again.  Losing it would be the worse outcome by far.
    """
    try:
        current = (await hass.async_add_executor_job(inverter.get_min_soc)).get(
            "minSocOnGrid"
        )
        if current == captured:
            return
        await restore_min_soc_on_grid(hass, inverter, captured)
        _LOGGER.warning(
            "Restored your own Min SoC on grid (%s%%) over the %s%% left on "
            "the inverter — a previous run did not put it back, most likely "
            "a restart during a smart session",
            captured,
            current,
        )
    except Exception:  # noqa: BLE001 — the captured value must survive this
        _LOGGER.warning(
            "Could not restore your Min SoC on grid (%s%%) to the inverter; "
            "it is still recorded and will be retried on the next restart",
            captured,
            exc_info=True,
        )
