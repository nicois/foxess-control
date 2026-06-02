"""P-001 investigation: fdSoc-floor grid import on the CLOUD discharge path.

Background (live observation, 2026-06-01 Sydney evening, pre-1.0.17 code):
a smart-discharge session drove the battery to its min_soc / fdSoc target
~15 min EARLY (un-paced full-power drain), and grid import appeared as SoC
reached the target and persisted ~2 min, partly AFTER the session went idle.

The 1.0.17 release (commit 855eca0) fixed the full-power-no-taper pacing
bug.  This module asks the OPEN question: *with the 1.0.17 pacing fix in
place, can the CLOUD path still cause grid import when a smart-discharge
session reaches its fdSoc target / tears down?*

Mechanism under test
--------------------
On the cloud path the active ForceDischarge schedule group carries
``fdSoc = session min_soc`` (see ``_start_deferred_discharge`` →
``apply_mode(..., fd_soc=min_soc)`` and ``_build_override_group``).

Per ``docs/api/foxess-cloud-api.md`` §5, ``ForceDischarge`` means
"discharge battery down to ``fdSoc``" and ``fdSoc`` is the SoC at which
the inverter STOPS discharging.  In ForceDischarge mode the inverter does
not follow house load like SelfUse — so once SoC == fdSoc, if house load
remains, the shortfall is supplied by the grid (P-001 violation).  The
only thing that prevents this is the integration switching to SelfUse
(``remove_override``) BEFORE SoC reaches fdSoc.

The mechanism that must do that is the end-of-discharge guard
(C-017 / G2, ``should_suspend_discharge``): it suspends ~10 min before the
remaining energy can no longer sustain the safety floor, switching to
self-use so the inverter serves house load directly from the battery
(no forced-discharge floor, no import).

These tests PROVE that, under realistic 1.0.17-paced cloud conditions,
G2 fires and the ForceDischarge override is removed while SoC is still
STRICTLY ABOVE min_soc — i.e. the inverter is never left in
ForceDischarge at SoC == fdSoc with house load.  They also prove the
cloud adapter's ``remove_override`` actually reverts the schedule to
SelfUse so the inverter then follows house load (no import).

The tests exercise the user's exact reported config: CLOUD adapter, NO
hardware export-limit actuator entity (``data_freshness: ws``, no
foxess_modbus entities) → SOFTWARE pacing path.
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
    battery_capacity_kwh: float = 60.0,
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
    """Start a discharge session (cloud config) and return (tick_cb, spy).

    The recording spy stands in for the cloud adapter and conforms to the
    InverterAdapter protocol.  ``set_export_limit_w`` returns immediately;
    on the user's no-actuator config the listener never calls it.
    """
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


class TestCloudFdSocFloorNoImport:
    """C-017/G2 removes the ForceDischarge override before SoC == fdSoc.

    Drives the real cloud discharge listener tick across the paced approach
    to min_soc and asserts the override is removed (self-use re-engaged)
    while SoC is STRICTLY above min_soc — so the inverter is never left in
    ForceDischarge at the fdSoc floor with house load (no import, P-001).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("load_kw", [0.3, 0.5, 1.0, 2.0])
    async def test_g2_removes_override_above_min_soc(self, load_kw: float) -> None:
        min_soc = 50
        cap = 60.0
        start = datetime.datetime(2026, 6, 1, 19, 0, 0)
        # Window ends 18 min after start — the tail where pacing collapses.
        end = datetime.datetime(2026, 6, 1, 19, 18, 0)

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        coord = {"SoC": 50.6, "loadsPower": load_kw, "pvPower": 0.0}
        hass = _make_cloud_listener_hass(
            inv, battery_capacity_kwh=cap, coordinator_data=coord
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
        # Arm an already-active paced session (discharging_started True so the
        # per-tick power/suspend path runs, not the deferred-start path).
        ds["discharging_started"] = True
        ds["pacing_enabled"] = True
        ds["start"] = start
        ds["end"] = end
        ds["consumption_peak_kw"] = load_kw

        # No hardware actuator on the cloud config.
        spy.set_export_limit_w.assert_not_called()

        # Step SoC down minute-by-minute under paced discharge.  At each tick
        # record the SoC the listener observed and whether it removed the
        # override (G1/G2/threshold).  The override MUST be removed while SoC
        # is still strictly above min_soc.
        removed_at_soc: float | None = None
        observed_at_or_below_min_while_active = False
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

            # Still in ForceDischarge this tick.  If SoC is at/below the
            # fdSoc floor here, the inverter is sitting at fdSoc with house
            # load → grid import (P-001 violation).
            if soc <= min_soc:
                observed_at_or_below_min_while_active = True
                break

            # Battery drain this tick = the paced power the listener just
            # wrote (software pacing: fdPwr modulated via apply_mode).
            paced_w = ds.get("last_power_w", 0)
            drain_kwh = paced_w / 1000 * (60 / 3600)
            soc -= drain_kwh / cap * 100

        assert not observed_at_or_below_min_while_active, (
            f"load={load_kw}kW: tick observed SoC<=min_soc ({min_soc}%) while "
            "ForceDischarge override still ACTIVE — inverter would sit at "
            "fdSoc and import house load (P-001)."
        )
        assert removed_at_soc is not None, (
            f"load={load_kw}kW: session never removed the ForceDischarge "
            "override across the approach to min_soc."
        )
        assert removed_at_soc > min_soc, (
            f"load={load_kw}kW: override removed at SoC={removed_at_soc}%, "
            f"not strictly above min_soc {min_soc}% — no margin before fdSoc."
        )


class TestCloudRemoveOverrideRevertsToSelfUse:
    """The cloud adapter teardown actually reverts ForceDischarge → SelfUse.

    Proves that once the listener calls remove_override, the inverter is no
    longer in ForceDischarge (which holds fdSoc by importing) but in SelfUse
    (which serves house load from the battery down to minSocOnGrid) — so the
    fdSoc floor is no longer the operative limit and there is no import.
    """

    @pytest.mark.asyncio
    async def test_remove_override_writes_self_use(self) -> None:
        from custom_components.foxess_control.foxess_adapter import FoxESSCloudAdapter

        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

        inv = MagicMock(spec=Inverter)
        inv.max_power_w = 10500
        # Active schedule: a single ForceDischarge group at fdSoc=50.
        inv.get_schedule.return_value = {
            "enable": 1,
            "groups": [
                {
                    "enable": 1,
                    "startHour": 19,
                    "startMinute": 0,
                    "endHour": 19,
                    "endMinute": 18,
                    "workMode": WorkMode.FORCE_DISCHARGE.value,
                    "minSocOnGrid": 11,
                    "fdSoc": 50,
                    "fdPwr": 10500,
                }
            ],
        }
        set_schedule_calls: list[Any] = []
        inv.set_schedule = MagicMock(side_effect=set_schedule_calls.append)
        inv.self_use = MagicMock()

        adapter = FoxESSCloudAdapter(
            hass,
            inv,
            min_soc_on_grid=11,
            api_min_soc=11,
            start=datetime.datetime(2026, 6, 1, 19, 0),
            end=datetime.datetime(2026, 6, 1, 19, 18),
        )

        await adapter.remove_override(hass, WorkMode.FORCE_DISCHARGE)

        # No ForceDischarge group must remain.  With only the FD group present
        # the adapter reverts to SelfUse (self_use call); had other groups
        # existed it would have written a schedule with the FD group dropped.
        if set_schedule_calls:
            written = set_schedule_calls[-1]
            assert all(
                g.get("workMode") != WorkMode.FORCE_DISCHARGE.value for g in written
            ), "ForceDischarge group still present after remove_override"
        else:
            inv.self_use.assert_called_once()
