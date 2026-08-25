"""Scheduler writes must respect the device-declared parameter ranges.

Issues #12 (EVO 10-5-H), #14 (H3-12.0-M) and #17 (H3-15.0-Smart): every
scheduler write is rejected with ``FoxESS API error 40257: Parameters do
not meet expectations``, while polling, real-time data and setup all work.

Root cause, established from the live API: ``/op/v3/device/scheduler/get``
returns a per-device ``properties`` map declaring the accepted range of
every schedule-group field.  On the owner's KH10 the declared ``fdpwr``
ceiling is 10500 W, which is exactly the ``capacity x 1050`` value this
integration writes — so the heuristic works there by coincidence.  Other
model families declare the plain nameplate rating (H3-12.0-M → 12000 W,
EVO 10-5-H → 5000 W), so ``capacity x 1050`` overshoots and every write —
including the SelfUse baseline written on teardown — is rejected.

These tests drive the real ``Inverter`` / ``FoxESSClient`` /
``FoxESSCloudAdapter`` against the simulator (C-028) with the declared
limits of a failing model, and assert the observable contract: the write
is accepted and the resulting override is actually in force.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.foxess_control.foxess.client import FoxESSApiError, FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter, WorkMode

if TYPE_CHECKING:
    from .conftest import SimulatorHandle


@pytest.fixture(autouse=True)
def _disable_throttle() -> None:
    """Disable request throttling in tests."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


def _make_inv(sim: SimulatorHandle) -> Inverter:
    client = FoxESSClient("test-api-key", base_url=sim.url)
    return Inverter(client, "SIM0001")


def _configure_h3_12(sim: SimulatorHandle) -> None:
    """Shape the simulator like the H3-12.0-M from issue #14.

    ``capacity`` 12 kW → the integration's heuristic yields 12600 W, but
    the device declares 12000 W as its ``fdPwr`` ceiling.
    """
    sim.set(
        device_type="H3-12.0-M",
        max_power_w=12600,
        fd_pwr_max_w=12000,
        max_grid_export_limit_w=12000,
    )


def _configure_evo_5(sim: SimulatorHandle) -> None:
    """Shape the simulator like the EVO 10-5-H from issue #12."""
    sim.set(
        device_type="EVO 10-5-H",
        max_power_w=5250,
        fd_pwr_max_w=5000,
        max_grid_export_limit_w=5000,
    )


def _written_groups(sim: SimulatorHandle, mode: WorkMode) -> list[dict[str, Any]]:
    state = sim.state()
    return [g for g in state["schedule_groups"] if g["workMode"] == mode.value]


class TestForceDischargeAgainstDeclaredLimits:
    """The failing-model reproduction: full-power force discharge."""

    def test_force_discharge_accepted_on_h3(self, foxess_sim: SimulatorHandle) -> None:
        """Issue #17/#14: force discharge must not be rejected with 40257."""
        _configure_h3_12(foxess_sim)
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20)

        groups = _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)
        assert groups, "no ForceDischarge group is in force on the inverter"
        assert groups[0]["fdPwr"] <= 12000, (
            f"fdPwr {groups[0]['fdPwr']} exceeds the device-declared ceiling of 12000 W"
        )

    def test_feedin_accepted_on_evo(self, foxess_sim: SimulatorHandle) -> None:
        """Issue #12: the EVO rejects *any* write, including Feed-in."""
        _configure_evo_5(foxess_sim)
        inv = _make_inv(foxess_sim)

        inv.set_work_mode(WorkMode.FEEDIN)

        groups = _written_groups(foxess_sim, WorkMode.FEEDIN)
        assert groups, "no Feedin group is in force on the inverter"
        assert groups[0]["fdPwr"] <= 5000

    def test_self_use_teardown_accepted_on_h3(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """C-025: the SelfUse baseline written on teardown must be accepted.

        ``self_use()`` also pins ``fdPwr`` at the inverter's rated power, so
        an over-large value breaks session teardown as well as session
        start — leaving a discharge override in place (P-001/P-002).
        """
        _configure_h3_12(foxess_sim)
        inv = _make_inv(foxess_sim)

        inv.self_use(min_soc_on_grid=15)

        groups = _written_groups(foxess_sim, WorkMode.SELF_USE)
        assert groups, "SelfUse baseline was not applied"

    def test_max_power_reported_within_declared_limit(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """The pacing ceiling must be the real one, not the overshoot.

        ``max_power_w`` feeds ``InverterAdapter.get_max_power_w()`` and the
        pacing algorithms; reporting 12600 W on a 12000 W device would make
        every full-power decision unachievable (P-003, C-020).
        """
        _configure_h3_12(foxess_sim)
        inv = _make_inv(foxess_sim)

        assert inv.max_power_w == 12000

    def test_explicit_power_above_declared_limit_accepted(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """A user-supplied ``power:`` above the ceiling must still write.

        ``foxess_control.feedin`` and ``force_discharge`` accept an explicit
        power in watts; a value above the device ceiling must be capped
        rather than failing the whole service call (large user base — prefer
        the safe thing over refusing).
        """
        _configure_evo_5(foxess_sim)
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20, power=9000)

        groups = _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)
        assert groups, "no ForceDischarge group is in force on the inverter"
        assert groups[0]["fdPwr"] == 5000


class TestNoRegressionForWorkingModels:
    """The KH10 path must keep building exactly the payload it builds today."""

    def test_kh10_payload_unchanged(self, foxess_sim: SimulatorHandle) -> None:
        """Declared ceiling == capacity x 1050 → identical payload."""
        inv = _make_inv(foxess_sim)
        assert inv.max_power_w == 10500

        inv.self_use(min_soc_on_grid=11)

        groups = foxess_sim.state()["schedule_groups"]
        assert groups == [
            {
                "enable": 1,
                "startHour": 0,
                "startMinute": 0,
                "endHour": 23,
                "endMinute": 59,
                "workMode": "SelfUse",
                "minSocOnGrid": 11,
                "fdSoc": 11,
                "fdPwr": 10500,
            }
        ]

    def test_schedule_write_event_reports_payload_as_written(
        self, foxess_sim: SimulatorHandle, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ``schedule_write`` event must carry the clamped payload.

        Replay harnesses treat the event as the groups list as written to
        the API; emitting the pre-clamp values would desynchronise them.
        """
        _configure_evo_5(foxess_sim)
        inv = _make_inv(foxess_sim)

        with caplog.at_level(logging.INFO):
            inv.force_discharge(min_soc=20)

        writes = [
            rec.payload["groups"]  # type: ignore[attr-defined]
            for rec in caplog.records
            if getattr(rec, "event", None) == "schedule_write"
        ]
        assert writes, "no schedule_write event emitted"
        assert writes[-1][0]["fdPwr"] == 5000

    def test_properties_endpoint_absent_falls_back_to_capacity(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """No declared properties → today's behaviour, unchanged.

        Firmware/regions without ``/op/v3/device/scheduler/get`` must keep
        working exactly as before rather than failing setup.
        """
        foxess_sim.set(scheduler_properties_supported=False)
        inv = _make_inv(foxess_sim)

        assert inv.max_power_w == 10500
        inv.self_use(min_soc_on_grid=11)
        assert _written_groups(foxess_sim, WorkMode.SELF_USE)

    def test_missing_capacity_still_raises(self, foxess_sim: SimulatorHandle) -> None:
        """Setup must still fail loudly when capacity cannot be determined.

        Issue #13 (a batteryless M1-800-E micro-inverter) is a *different*
        failure; the declared-range probe must not paper over it.
        """
        foxess_sim.set(max_power_w=0)
        inv = _make_inv(foxess_sim)

        with pytest.raises(RuntimeError, match="capacity"):
            _ = inv.max_power_w


class TestModelStringIsIrrelevant:
    """``inverter_model: null`` in the bug reports was a red herring."""

    def test_absent_device_type_still_writes(self, foxess_sim: SimulatorHandle) -> None:
        """Device detail with no ``deviceType`` must not break writes."""
        _configure_h3_12(foxess_sim)
        foxess_sim.set(device_type=None)
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20)

        assert inv.device_type is None
        assert _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)

    def test_unrecognised_device_type_still_writes(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """A model string the code has never seen must not break writes."""
        _configure_h3_12(foxess_sim)
        foxess_sim.set(device_type="TOTALLY-NEW-MODEL-9000")
        inv = _make_inv(foxess_sim)

        inv.force_discharge(min_soc=20)

        assert inv.device_type == "TOTALLY-NEW-MODEL-9000"
        assert _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)

    def test_device_type_is_surfaced_for_diagnostics(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """C-020/C-026: diagnostics must report the real model name.

        All three reports showed ``inverter_model: null``, which sent the
        investigation after a model-specific payload-shaping theory that
        does not exist.  The field was reading a non-existent attribute.
        """
        from custom_components.foxess_control.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        _configure_h3_12(foxess_sim)
        inv = _make_inv(foxess_sim)
        _ = inv.max_power_w  # warm the detail cache, as setup does

        import asyncio
        from collections import deque
        from types import SimpleNamespace

        from custom_components.foxess_control.const import DOMAIN

        dd = SimpleNamespace(
            entries={"e1": SimpleNamespace(coordinator=None, inverter=inv)},
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

        result = asyncio.run(async_get_config_entry_diagnostics(hass, entry))

        assert result["environment"]["inverter_model"] == "H3-12.0-M"


class TestWorkModeEnumeration:
    """A mode outside the declared enumeration is diagnosable, not opaque."""

    def test_unsupported_mode_warns_with_supported_list(
        self, foxess_sim: SimulatorHandle, caplog: pytest.LogCaptureFixture
    ) -> None:
        """C-020: the log must name the mode and what the device supports.

        The write is still attempted — the declared enumeration is advisory,
        and refusing on it would break users whose device under-reports.
        """
        foxess_sim.set(
            scheduler_work_modes=["SelfUse", "Backup", "ForceCharge"],
        )
        inv = _make_inv(foxess_sim)

        with caplog.at_level(logging.WARNING), pytest.raises(FoxESSApiError):
            inv.set_work_mode(WorkMode.FEEDIN)

        assert any(
            "Feedin" in rec.getMessage() and "SelfUse" in rec.getMessage()
            for rec in caplog.records
            if rec.levelno >= logging.WARNING
        ), (
            "no warning naming the unsupported mode and the device's "
            f"supported modes; records={[r.getMessage() for r in caplog.records]}"
        )


class TestThroughCloudAdapter:
    """Production routes schedule writes through FoxESSCloudAdapter."""

    @pytest.mark.asyncio
    async def test_apply_mode_force_discharge_accepted(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        from custom_components.foxess_control.foxess_adapter import FoxESSCloudAdapter

        _configure_h3_12(foxess_sim)
        inv = _make_inv(foxess_sim)

        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

        now = datetime.datetime.now()
        adapter = FoxESSCloudAdapter(
            hass,
            inv,
            min_soc_on_grid=15,
            api_min_soc=11,
            start=now,
            end=now + datetime.timedelta(hours=1),
        )

        assert adapter.get_max_power_w() == 12000

        await adapter.apply_mode(hass, WorkMode.FORCE_DISCHARGE, fd_soc=20)

        groups = _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)
        assert groups, "adapter.apply_mode did not put a ForceDischarge group in force"
        assert groups[0]["fdPwr"] <= 12000
