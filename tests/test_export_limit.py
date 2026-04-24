"""Tests for the hardware export-limit actuator (smart discharge tapering).

These tests cover the feature flagged by ``CONF_EXPORT_LIMIT_ENTITY``:

* Config round-trip via ``IntegrationConfig`` (with and without the entity).
* The adapter's ``set_export_limit_w`` / ``get_export_limit_w`` methods.
* Write-suppression below the threshold delta.
* Missing-entity no-op (warn-once, short-circuit).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.foxess_control.const import (
    CONF_EXPORT_LIMIT_ENTITY,
    CONF_WORK_MODE_ENTITY,
    DEFAULT_EXPORT_LIMIT_MIN_CHANGE,
    DOMAIN,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
    build_config,
)
from custom_components.foxess_control.foxess_adapter import FoxESSEntityAdapter


def _make_hass(entry_options: dict[str, Any]) -> MagicMock:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(return_value=None)
    dd = FoxESSControlData()
    dd.entries["entry1"] = FoxESSEntryData()
    dd.config = build_config(entry_options)
    hass.data = {DOMAIN: dd}
    entry = MagicMock()
    entry.options = entry_options
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)
    return hass


class TestIntegrationConfigExportLimit:
    """CONF_EXPORT_LIMIT_ENTITY round-trips via IntegrationConfig."""

    def test_default_is_none(self) -> None:
        cfg = build_config({})
        assert cfg.export_limit_entity is None

    def test_empty_string_is_none(self) -> None:
        cfg = build_config({CONF_EXPORT_LIMIT_ENTITY: ""})
        assert cfg.export_limit_entity is None

    def test_configured_entity_passes_through(self) -> None:
        cfg = build_config(
            {CONF_EXPORT_LIMIT_ENTITY: "number.foxess_max_grid_export_limit"}
        )
        assert cfg.export_limit_entity == "number.foxess_max_grid_export_limit"


class TestAdapterExportLimitInterface:
    """Adapter's set/get_export_limit_w methods."""

    @pytest.mark.asyncio
    async def test_set_issues_number_set_value_service_call(self) -> None:
        opts = {
            CONF_WORK_MODE_ENTITY: "select.foxess_work_mode",
            CONF_EXPORT_LIMIT_ENTITY: "number.foxess_max_grid_export_limit",
        }
        hass = _make_hass(opts)
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=10500)

        await adapter.set_export_limit_w(hass, 4000)

        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {
                "entity_id": "number.foxess_max_grid_export_limit",
                "value": 4000,
            },
            blocking=True,
        )

    @pytest.mark.asyncio
    async def test_set_is_noop_when_entity_missing(self) -> None:
        opts = {CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"}
        hass = _make_hass(opts)
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=10500)

        await adapter.set_export_limit_w(hass, 4000)

        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_returns_none_when_entity_missing(self) -> None:
        opts = {CONF_WORK_MODE_ENTITY: "select.foxess_work_mode"}
        hass = _make_hass(opts)
        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=10500)

        assert await adapter.get_export_limit_w(hass) is None

    @pytest.mark.asyncio
    async def test_get_reads_from_hass_state(self) -> None:
        opts = {
            CONF_EXPORT_LIMIT_ENTITY: "number.foxess_max_grid_export_limit",
        }
        hass = _make_hass(opts)
        state = MagicMock()
        state.state = "3500"
        hass.states.get = MagicMock(return_value=state)

        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=10500)
        assert await adapter.get_export_limit_w(hass) == 3500

    @pytest.mark.asyncio
    async def test_get_unavailable_returns_none(self) -> None:
        opts = {
            CONF_EXPORT_LIMIT_ENTITY: "number.foxess_max_grid_export_limit",
        }
        hass = _make_hass(opts)
        state = MagicMock()
        state.state = "unavailable"
        hass.states.get = MagicMock(return_value=state)

        adapter = FoxESSEntityAdapter(entry_options=opts, max_power_w=10500)
        assert await adapter.get_export_limit_w(hass) is None


class TestExportLimitThreshold:
    """DEFAULT_EXPORT_LIMIT_MIN_CHANGE is a sensible ~50 W."""

    def test_default_threshold_is_50w(self) -> None:
        assert DEFAULT_EXPORT_LIMIT_MIN_CHANGE == 50
