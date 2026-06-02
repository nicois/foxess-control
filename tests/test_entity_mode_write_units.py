"""Entity-mode write-path unit conversion tests.

Production bug (user report): in entity mode, ``FoxESSEntityAdapter``
writes raw watts to foxess_modbus ``number.*`` target entities whose
``unit_of_measurement`` is ``kW``. foxess_modbus clamps incoming values
outside its declared range, so every write saturates at the clamp
ceiling (e.g. 3500 W → 15 kW, 5000 W → 15 kW, 10500 W → 15 kW): the
pacing algorithm has no effective control.

These tests pin the observable contract: the ``value`` passed to
``hass.services.async_call("number", "set_value", ...)`` must be
expressed in the target entity's native unit, clamped to its declared
min/max when present, and a missing/unknown unit must surface as a
warning (not silently corrupt the write).

The tests also guard the neighbourhood: ``min_soc`` writes (percent,
not power) must never be re-scaled, and ``select_option`` work-mode
writes must be untouched.

Constraints enforced: C-020 (operational transparency — the inverter
does what the pacing algorithm asked), C-021 / C-039 (the converter
lives in the brand layer, not ``smart_battery/``).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.foxess_control.const import (
    CONF_CHARGE_POWER_ENTITY,
    CONF_DISCHARGE_POWER_ENTITY,
    CONF_EXPORT_LIMIT_ENTITY,
    CONF_MIN_SOC_ENTITY,
    CONF_WORK_MODE_ENTITY,
    DOMAIN,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
    build_config,
)
from custom_components.foxess_control.foxess.inverter import WorkMode
from custom_components.foxess_control.foxess_adapter import FoxESSEntityAdapter

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _state(
    *,
    state: str = "0",
    unit: str | None = "kW",
    min_val: float | None = 0,
    max_val: float | None = 15,
    step: float | None = 0.001,
) -> MagicMock:
    """Build a MagicMock HA state with the foxess_modbus number-entity shape."""
    attrs: dict[str, Any] = {}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    if min_val is not None:
        attrs["min"] = min_val
    if max_val is not None:
        attrs["max"] = max_val
    if step is not None:
        attrs["step"] = step
    st = MagicMock()
    st.state = state
    st.attributes = attrs
    return st


def _make_hass(
    entry_options: dict[str, Any],
    entity_states: dict[str, MagicMock] | None = None,
) -> MagicMock:
    """Mock hass — async_call spy + states.get lookup table."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    states_map = entity_states or {}
    hass.states.get = MagicMock(side_effect=lambda eid: states_map.get(eid))
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData()
    dd.config = build_config(entry_options)
    hass.data = {DOMAIN: dd}
    entry = MagicMock()
    entry.options = entry_options
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    return hass


def _power_set_value_calls(hass: MagicMock, entity_id: str) -> list[float | int]:
    """Return the ``value`` arg from every ``number.set_value`` call to entity_id."""
    vals: list[float | int] = []
    for call in hass.services.async_call.call_args_list:
        args = call.args
        if len(args) >= 3 and args[1] == "set_value":
            payload = args[2]
            if payload.get("entity_id") == entity_id:
                vals.append(payload["value"])
    return vals


# --------------------------------------------------------------------------
# apply_mode — power writes
# --------------------------------------------------------------------------


class TestApplyModePowerConversion:
    """The power value passed to number.set_value must match the target unit."""

    @pytest.mark.asyncio
    async def test_force_charge_kw_target_converts_watts_to_kw(self) -> None:
        charge_eid = "number.foxess_force_charge_power"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_CHARGE_POWER_ENTITY: charge_eid,
        }
        hass = _make_hass(opts, {charge_eid: _state(unit="kW", max_val=15)})
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

        await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, power_w=3500)

        vals = _power_set_value_calls(hass, charge_eid)
        assert vals == [pytest.approx(3.5)], (
            f"Expected 3.5 kW written to {charge_eid}, got {vals}"
        )

    @pytest.mark.asyncio
    async def test_force_discharge_kw_target_converts_watts_to_kw(self) -> None:
        discharge_eid = "number.foxess_force_discharge_power"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_DISCHARGE_POWER_ENTITY: discharge_eid,
        }
        hass = _make_hass(opts, {discharge_eid: _state(unit="kW", max_val=15)})
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

        await adapter.apply_mode(hass, WorkMode.FORCE_DISCHARGE, power_w=10500)

        vals = _power_set_value_calls(hass, discharge_eid)
        assert vals == [pytest.approx(10.5)], (
            f"Expected 10.5 kW written to {discharge_eid}, got {vals}"
        )

    @pytest.mark.asyncio
    async def test_watts_target_passes_power_through_unchanged(self) -> None:
        charge_eid = "number.foxess_force_charge_power_w"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_CHARGE_POWER_ENTITY: charge_eid,
        }
        hass = _make_hass(opts, {charge_eid: _state(unit="W", max_val=15000)})
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

        await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, power_w=3500)

        vals = _power_set_value_calls(hass, charge_eid)
        assert vals == [3500], f"Expected 3500 W passthrough, got {vals}"

    @pytest.mark.asyncio
    async def test_missing_unit_passes_through_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        charge_eid = "number.foxess_force_charge_power"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_CHARGE_POWER_ENTITY: charge_eid,
        }
        # Entity exists but declares no unit_of_measurement.
        hass = _make_hass(
            opts, {charge_eid: _state(unit=None, max_val=None, min_val=None)}
        )
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

        with caplog.at_level(logging.WARNING):
            await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, power_w=3500)

        vals = _power_set_value_calls(hass, charge_eid)
        assert vals == [3500], f"Expected 3500 W passthrough, got {vals}"
        # Visible warning — user needs to know the unit is missing.
        assert any(
            "unit" in record.message.lower() and charge_eid in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        ), (
            "Expected a warning mentioning the missing unit and the entity ID, "
            f"got: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_power_exceeds_max_clamps_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        charge_eid = "number.foxess_force_charge_power"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_CHARGE_POWER_ENTITY: charge_eid,
        }
        hass = _make_hass(opts, {charge_eid: _state(unit="kW", max_val=15, min_val=0)})
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=20000)

        with caplog.at_level(logging.WARNING):
            # 20 kW requested; target max is 15 kW — must clamp to 15.
            await adapter.apply_mode(hass, WorkMode.FORCE_CHARGE, power_w=20000)

        vals = _power_set_value_calls(hass, charge_eid)
        assert vals == [pytest.approx(15.0)], (
            f"Expected 15.0 kW (clamped from 20 kW), got {vals}"
        )
        assert any(
            "clamp" in record.message.lower() or "max" in record.message.lower()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ), "Expected a warning noting the clamp"


# --------------------------------------------------------------------------
# set_export_limit_w
# --------------------------------------------------------------------------


class TestSetExportLimitConversion:
    """The export-limit value must match the target entity's unit."""

    @pytest.mark.asyncio
    async def test_kw_target_converts_watts_to_kw(self) -> None:
        limit_eid = "number.foxess_max_grid_export_limit"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_EXPORT_LIMIT_ENTITY: limit_eid,
        }
        hass = _make_hass(opts, {limit_eid: _state(unit="kW", max_val=15)})
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=10500)

        await adapter.set_export_limit_w(hass, 5000)

        vals = _power_set_value_calls(hass, limit_eid)
        assert vals == [pytest.approx(5.0)], (
            f"Expected 5.0 kW written to {limit_eid}, got {vals}"
        )

    @pytest.mark.asyncio
    async def test_watts_target_passes_through_unchanged(self) -> None:
        limit_eid = "number.foxess_max_grid_export_limit_w"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_EXPORT_LIMIT_ENTITY: limit_eid,
        }
        hass = _make_hass(opts, {limit_eid: _state(unit="W", max_val=15000)})
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=10500)

        await adapter.set_export_limit_w(hass, 5000)

        vals = _power_set_value_calls(hass, limit_eid)
        assert vals == [5000], f"Expected 5000 W passthrough, got {vals}"

    @pytest.mark.asyncio
    async def test_missing_unit_passes_through_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        limit_eid = "number.foxess_max_grid_export_limit"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_EXPORT_LIMIT_ENTITY: limit_eid,
        }
        hass = _make_hass(
            opts, {limit_eid: _state(unit=None, max_val=None, min_val=None)}
        )
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=10500)

        with caplog.at_level(logging.WARNING):
            await adapter.set_export_limit_w(hass, 5000)

        vals = _power_set_value_calls(hass, limit_eid)
        assert vals == [5000], f"Expected 5000 W passthrough, got {vals}"
        assert any(
            "unit" in record.message.lower() and limit_eid in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        ), (
            "Expected a warning mentioning the missing unit and the entity ID, "
            f"got: {[r.message for r in caplog.records]}"
        )


# --------------------------------------------------------------------------
# Neighbourhood guard — non-power writes must NOT be re-scaled.
# --------------------------------------------------------------------------


class TestNonPowerWritesUntouched:
    """SoC writes are in percent — they must never be scaled by the unit helper."""

    @pytest.mark.asyncio
    async def test_min_soc_percent_passes_through(self) -> None:
        discharge_eid = "number.foxess_force_discharge_power"
        min_soc_eid = "number.foxess_min_soc"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_DISCHARGE_POWER_ENTITY: discharge_eid,
            CONF_MIN_SOC_ENTITY: min_soc_eid,
        }
        hass = _make_hass(
            opts,
            {
                discharge_eid: _state(unit="kW", max_val=15),
                # Realistic SoC entity: percent, 10..100, step 1.
                min_soc_eid: _state(unit="%", min_val=10, max_val=100, step=1),
            },
        )
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=15000)

        await adapter.apply_mode(
            hass, WorkMode.FORCE_DISCHARGE, power_w=3500, fd_soc=15
        )

        # Discharge power (kW target) — must be converted.
        assert _power_set_value_calls(hass, discharge_eid) == [pytest.approx(3.5)]
        # min_soc is a percentage, not watts — must pass through unchanged.
        assert _power_set_value_calls(hass, min_soc_eid) == [15], (
            "min_soc (% unit) must not be re-scaled by the power unit helper"
        )


# --------------------------------------------------------------------------
# Persistent min-SoC-on-grid floor vs session discharge target (P-001/P-002).
# --------------------------------------------------------------------------


class TestPersistentMinSocFloorRestored:
    """Smart discharge must not leave the inverter's *persistent* on-grid
    min-SoC floor raised to the session discharge *target*.

    Live symptom (06-01 evening session, target min_soc=50%, real reserve
    11%): the entity backend wrote ``fd_soc`` (the session target, 50%) into
    the foxess_modbus *Min SoC* register — which is the inverter's PERSISTENT
    self-use on-grid floor, not a per-session knob. When the session ended
    and the inverter reverted to self-use, the floor was still 50%, so the
    inverter refused to discharge the battery below 50% and served house load
    from the grid (~1.4 kW import) instead. That violates P-001 (no import
    after forced discharge) and P-002 (the *session* min_soc must not become
    the persistent reserve).

    The observable contract: after a discharge session ends (revert to
    self-use), the persistent Min SoC entity must be restored to the user's
    configured reserve (e.g. 11%), NOT left at the session target (50%).
    C-025 (session-boundary cleanliness): per-session state must not leak.
    """

    @pytest.mark.asyncio
    async def test_teardown_restores_persistent_min_soc_to_reserve(self) -> None:
        min_soc_eid = "number.foxess_min_soc"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_DISCHARGE_POWER_ENTITY: "number.foxess_force_discharge_power",
            CONF_MIN_SOC_ENTITY: min_soc_eid,
        }
        hass = _make_hass(
            opts,
            {
                "number.foxess_force_discharge_power": _state(unit="kW", max_val=15),
                min_soc_eid: _state(unit="%", min_val=10, max_val=100, step=1),
            },
        )
        # User's genuine outage reserve is 11%, far below the session target.
        adapter = FoxESSEntityAdapter(
            entry_options=opts, max_power_w=15000, min_soc_on_grid=11
        )

        # Active discharge: stop target (fd_soc) is 50%.
        await adapter.apply_mode(
            hass, WorkMode.FORCE_DISCHARGE, power_w=3500, fd_soc=50
        )
        # Session ends → revert to self-use (the teardown path).
        await adapter.remove_override(hass, WorkMode.FORCE_DISCHARGE)

        writes = _power_set_value_calls(hass, min_soc_eid)
        # During discharge the target (50) is written; the LAST write — on
        # teardown — must restore the persistent reserve (11), not leave 50.
        assert writes, "expected at least one Min SoC write"
        assert writes[-1] == 11, (
            "After the session ends, the persistent Min SoC floor must be "
            f"restored to the configured reserve (11%), got writes={writes}. "
            "Leaving it at the session target (50%) makes self-use import "
            "from the grid instead of discharging the battery (P-001/P-002)."
        )

    @pytest.mark.asyncio
    async def test_active_discharge_still_targets_session_min_soc(self) -> None:
        # C-002 regression guard: while discharging, the stop target written
        # to the inverter must remain the session min_soc (50), so force
        # discharge still stops at the session target.
        min_soc_eid = "number.foxess_min_soc"
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_DISCHARGE_POWER_ENTITY: "number.foxess_force_discharge_power",
            CONF_MIN_SOC_ENTITY: min_soc_eid,
        }
        hass = _make_hass(
            opts,
            {
                "number.foxess_force_discharge_power": _state(unit="kW", max_val=15),
                min_soc_eid: _state(unit="%", min_val=10, max_val=100, step=1),
            },
        )
        adapter = FoxESSEntityAdapter(
            entry_options=opts, max_power_w=15000, min_soc_on_grid=11
        )

        await adapter.apply_mode(
            hass, WorkMode.FORCE_DISCHARGE, power_w=3500, fd_soc=50
        )

        writes = _power_set_value_calls(hass, min_soc_eid)
        assert writes == [50], (
            "Active force-discharge must write the session target (50) as the "
            f"force-discharge stop SoC, got {writes}"
        )
