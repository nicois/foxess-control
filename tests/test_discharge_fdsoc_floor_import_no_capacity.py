"""P-001: fdSoc-floor grid import on the CLOUD path with capacity UNSET.

Companion to ``test_discharge_fdsoc_floor_import.py``.  That module proves
the capacity-KNOWN path: with pacing enabled, C-017/G2 removes the
ForceDischarge override while SoC is still strictly ABOVE min_soc, so the
inverter is never left at the fdSoc floor importing house load.

This module proves the SAME observable P-001 guarantee for the
capacity-UNKNOWN path — the DEFAULT for an unknown (possibly large)
fraction of users, because ``battery_capacity_kwh`` defaults to 0.0 and no
config-flow field forces it.

Mechanism of the bug
--------------------
``pacing_enabled = not full_power and battery_capacity_kwh > 0``.  With
capacity 0 this is False, so the entire suspend/pacing block in
``_check_discharge_soc_inner`` — including ``_handle_suspend_resume``
(G1/G2, the capacity-dependent end-of-discharge guard) — is SKIPPED.  The
only remaining stop mechanism is ``_check_soc_threshold``, which:

* fires only at/after ``SoC <= min_soc`` (never strictly above), and
* requires 2 consecutive at-threshold ticks before removing the override.

So on the cloud path (active ForceDischarge group at ``fdSoc = min_soc``)
the inverter sits at the fdSoc floor — discharging stopped, house load
supplied by the GRID — for ~1 tick (~60s) AFTER SoC reaches min_soc,
until the second confirmation removes the override.  That is the P-001
import window.

The required behaviour (user's stated correct behaviour): smart discharge
"should continue to use battery until the discharge event is deleted,
causing it to revert to the default self-use behaviour without drawing
[from grid]."  That is capacity-INDEPENDENT: when SoC reaches the session
target, the ForceDischarge override must be removed PROMPTLY so the
inverter reverts to self-use (which serves house load from the battery
down to the real reserve, without importing) — closing the import window.

This module drives the REAL cloud discharge listener tick with a recording
adapter spy, capacity 0 (pacing disabled), NO export-limit actuator, and
steps SoC down through min_soc.  It asserts the override is removed at or
before the FIRST tick SoC reaches min_soc — i.e. the inverter is NOT left
in ForceDischarge at the fdSoc floor for an extended (multi-tick) window.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.foxess_control import _register_services
from custom_components.foxess_control.const import (
    CONF_API_MIN_SOC,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_GRID_EXPORT_LIMIT,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    CONF_SMART_HEADROOM,
    CONF_WORK_MODE_ENTITY,
    DEFAULT_API_MIN_SOC,
    DEFAULT_MIN_POWER_CHANGE,
    DEFAULT_MIN_SOC_ON_GRID,
    DEFAULT_SMART_HEADROOM,
    DOMAIN,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
    build_config,
)
from custom_components.foxess_control.foxess.inverter import Inverter
from custom_components.foxess_control.smart_battery.types import WorkMode

from .conftest import _get_handler


def _make_cloud_listener_hass(
    inverter: Inverter,
    *,
    battery_capacity_kwh: float,
    coordinator_data: dict[str, Any],
    grid_export_limit_w: int = 0,
) -> MagicMock:
    """Hass fixture for the user's CLOUD config: NO export-limit actuator.

    ``coordinator_data`` is the live (mutable) dict the listener reads SoC /
    load / solar from — mutate it between ticks to step SoC down.
    """
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, **kwargs: asyncio.ensure_future(coro)
    )
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=None)

    mock_store = MagicMock()
    mock_store.async_load = AsyncMock(return_value={})
    mock_store.async_save = AsyncMock()

    mock_coordinator = MagicMock()
    mock_coordinator.data = coordinator_data
    mock_coordinator.update_interval = datetime.timedelta(seconds=300)
    mock_coordinator.async_request_refresh = AsyncMock()

    dd = FoxESSControlData()
    mock_entry = MagicMock()
    options: dict[str, Any] = {
        CONF_MIN_SOC_ON_GRID: DEFAULT_MIN_SOC_ON_GRID,
        CONF_BATTERY_CAPACITY_KWH: battery_capacity_kwh,
        CONF_MIN_POWER_CHANGE: DEFAULT_MIN_POWER_CHANGE,
        CONF_API_MIN_SOC: DEFAULT_API_MIN_SOC,
        CONF_SMART_HEADROOM: DEFAULT_SMART_HEADROOM,
        CONF_GRID_EXPORT_LIMIT: grid_export_limit_w,
        CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
        # NO CONF_EXPORT_LIMIT_ENTITY: this is the user's cloud config.
    }
    mock_entry.options = options

    entry_data = FoxESSEntryData(coordinator=mock_coordinator, inverter=inverter)
    entry_data.entry = mock_entry
    dd.entries["entry1"] = entry_data
    dd.smart_discharge_unsubs = []
    dd.smart_charge_unsubs = []
    dd.store = mock_store
    dd.config = build_config(options, inverter_max_power_w=inverter.max_power_w)
    hass.data = {DOMAIN: dd}

    hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)
    return hass


def _make_call(data: dict[str, Any] | None = None) -> MagicMock:
    call = MagicMock()
    call.data = data or {}
    return call


async def _start_cloud_discharge_with_spy(
    hass: MagicMock,
    inv: MagicMock,
    call_data: dict[str, Any],
    *,
    now: datetime.datetime,
) -> tuple[Any, MagicMock]:
    """Start a discharge session (cloud config) and return (tick_cb, spy)."""
    inv.get_schedule.return_value = {"enable": 0, "groups": []}

    captured_callback: list[Any] = []

    def capture_interval(_h: Any, callback: Any, _i: Any) -> MagicMock:
        captured_callback.append(callback)
        return MagicMock()

    spy = MagicMock()
    spy.apply_mode = AsyncMock()
    spy.remove_override = AsyncMock()
    spy.set_export_limit_w = AsyncMock()
    spy.get_export_limit_w = AsyncMock(return_value=None)
    spy.get_max_power_w = MagicMock(return_value=inv.max_power_w)
    spy.set_groups = MagicMock()
    spy.on_session_started = MagicMock()

    _register_services(hass)
    handler = _get_handler(hass, "smart_discharge")

    with (
        patch(
            "custom_components.foxess_control._build_foxess_adapter",
            return_value=spy,
        ),
        patch(
            "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.foxess_control.smart_battery.services.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.foxess_control.smart_battery.listeners."
            "async_track_point_in_time",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.foxess_control.smart_battery.listeners."
            "async_track_time_interval",
            side_effect=capture_interval,
        ),
    ):
        await handler(_make_call(call_data))

    assert captured_callback, "Tick callback must have been registered"
    return captured_callback[0], spy


def _removed_force_discharge(spy: MagicMock) -> bool:
    """True if remove_override(FORCE_DISCHARGE) was awaited at least once."""
    for call in spy.remove_override.await_args_list:
        args = call.args
        if len(args) >= 2 and args[1] == WorkMode.FORCE_DISCHARGE:
            return True
    return False


class TestCloudFdSocFloorNoImportCapacityUnset:
    """Override removed at/before SoC reaches min_soc, even with capacity 0.

    Drives the real cloud discharge listener tick with pacing DISABLED
    (``battery_capacity_kwh == 0``) across the un-paced full-power approach
    to min_soc.  Asserts the ForceDischarge override is removed (self-use
    re-engaged) at or before the first tick SoC reaches min_soc — so the
    inverter is never left in ForceDischarge at the fdSoc floor for an
    extended window importing house load (P-001).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("load_kw", [0.3, 0.5, 1.0, 2.0])
    async def test_override_removed_no_import_window(self, load_kw: float) -> None:
        min_soc = 50
        cap = 60.0  # the REAL physical capacity, used only to model SoC drain
        start = datetime.datetime(2026, 6, 1, 19, 0, 0)
        end = datetime.datetime(2026, 6, 1, 19, 18, 0)

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        coord = {"SoC": 50.6, "loadsPower": load_kw, "pvPower": 0.0}
        # Capacity UNSET (0.0) — the default; pacing_enabled is False.
        hass = _make_cloud_listener_hass(
            inv, battery_capacity_kwh=0.0, coordinator_data=coord
        )

        tick, spy = await _start_cloud_discharge_with_spy(
            hass,
            inv,
            {
                "start_time": start.time(),
                "end_time": end.time(),
                "min_soc": min_soc,
            },
            now=start,
        )

        ds = hass.data[DOMAIN].smart_discharge_state
        assert ds is not None
        # Confirm the bug precondition: pacing really is disabled here.
        assert not ds.get("pacing_enabled"), (
            "Test precondition: capacity-0 session must have pacing disabled"
        )
        # Arm an already-active session (per-tick power/suspend path runs).
        ds["discharging_started"] = True
        ds["start"] = start
        ds["end"] = end

        # No hardware actuator on the cloud config.
        spy.set_export_limit_w.assert_not_called()

        # Step SoC down minute-by-minute under un-paced (full-power) drain.
        # The override MUST be removed at or before the first tick where SoC
        # reaches min_soc — otherwise the inverter sits at fdSoc importing.
        removed_at_soc: float | None = None
        import_window_at_soc: float | None = None
        soc = 50.6
        for minute in range(1, 30):
            now = start + datetime.timedelta(minutes=minute)
            coord["SoC"] = soc
            spy.remove_override.reset_mock()
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=now,
            ):
                await tick(now)
            await asyncio.sleep(0)

            if _removed_force_discharge(spy):
                removed_at_soc = soc
                break

            # Still in ForceDischarge this tick.  If SoC is at/below the fdSoc
            # floor here WITHOUT the override having been removed, the inverter
            # is sitting at fdSoc with house load → grid import (P-001).
            if soc <= min_soc:
                import_window_at_soc = soc
                break

            # Pacing disabled → fdPwr pinned at max; model full-power drain.
            paced_w = ds.get("last_power_w", inv.max_power_w) or inv.max_power_w
            drain_kwh = paced_w / 1000 * (60 / 3600)
            soc -= drain_kwh / cap * 100

        assert import_window_at_soc is None, (
            f"load={load_kw}kW: tick observed SoC={import_window_at_soc}% "
            f"<= min_soc ({min_soc}%) while ForceDischarge override still "
            "ACTIVE — the inverter sits at the fdSoc floor and imports house "
            "load from the grid (P-001 import window)."
        )
        assert removed_at_soc is not None, (
            f"load={load_kw}kW: session never removed the ForceDischarge "
            "override across the approach to min_soc."
        )
        assert removed_at_soc >= min_soc, (
            f"load={load_kw}kW: override removed at SoC={removed_at_soc}%, "
            f"below min_soc {min_soc}%."
        )

    @pytest.mark.asyncio
    async def test_no_premature_removal_well_above_min_soc(self) -> None:
        """Capacity-0 session must NOT remove the override while SoC is well
        above min_soc — no early session kill / no premature self-use revert.
        """
        min_soc = 50
        start = datetime.datetime(2026, 6, 1, 19, 0, 0)
        end = datetime.datetime(2026, 6, 1, 19, 18, 0)

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        coord = {"SoC": 80.0, "loadsPower": 0.5, "pvPower": 0.0}
        hass = _make_cloud_listener_hass(
            inv, battery_capacity_kwh=0.0, coordinator_data=coord
        )

        tick, spy = await _start_cloud_discharge_with_spy(
            hass,
            inv,
            {
                "start_time": start.time(),
                "end_time": end.time(),
                "min_soc": min_soc,
            },
            now=start,
        )
        ds = hass.data[DOMAIN].smart_discharge_state
        assert ds is not None
        ds["discharging_started"] = True
        ds["start"] = start
        ds["end"] = end

        # A handful of ticks at SoC well above min_soc — never near the floor.
        for minute in range(1, 5):
            now = start + datetime.timedelta(minutes=minute)
            coord["SoC"] = 80.0 - minute  # 79, 78, 77, 76 — far above 50
            with patch(
                "custom_components.foxess_control.smart_battery.listeners.dt_util.now",
                return_value=now,
            ):
                await tick(now)
            await asyncio.sleep(0)

        assert not _removed_force_discharge(spy), (
            "Override removed while SoC well above min_soc — premature "
            "session kill / unwanted self-use revert."
        )
