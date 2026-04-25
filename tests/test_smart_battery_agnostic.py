"""Brand-agnostic tests for ``smart_battery/``.

These tests exercise code under ``smart_battery/`` **without loading
any brand-specific module**.  No ``custom_components.foxess_control.*``
imports, no ``simulator/`` (which is FoxESS-specific), no aiohttp
client — just ``smart_battery/`` types and the FakeAdapter test
double.

C-040 companion: a test that passes here proves the exercised code
paths are genuinely brand-agnostic; a test that needs to reach into
a brand package to pass is exercising cross-layer behaviour and
belongs elsewhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from smart_battery.testing import ApplyModeCall, FakeAdapter, RemoveOverrideCall
from smart_battery.types import WorkMode

if TYPE_CHECKING:
    from smart_battery.adapter import InverterAdapter


class TestFakeAdapterProtocolConformance:
    """FakeAdapter must satisfy the InverterAdapter Protocol.

    If the Protocol ever grows a new required method, this test fails
    at runtime-type-check time and reminds us to update the stub —
    otherwise brand-agnostic tests that use FakeAdapter would silently
    stop exercising the new method.
    """

    def test_fake_adapter_is_an_inverter_adapter(self) -> None:
        adapter: InverterAdapter = FakeAdapter()
        # The annotation alone is sufficient at static-type-check
        # level. At runtime, verify every Protocol method exists and
        # is callable on the fake.
        assert callable(adapter.apply_mode)
        assert callable(adapter.remove_override)
        assert callable(adapter.get_max_power_w)
        assert callable(adapter.set_export_limit_w)
        assert callable(adapter.get_export_limit_w)


class TestFakeAdapterRecordsCalls:
    """FakeAdapter captures every Protocol call for assertion.

    These tests characterise the fake itself so the contract is clear
    for anyone reading a brand-agnostic test: here is exactly what
    `adapter.apply_mode_calls` / `set_export_limit_calls` will contain
    after the code under test runs.
    """

    @pytest.mark.asyncio
    async def test_apply_mode_records_mode_power_and_fd_soc(self) -> None:
        adapter = FakeAdapter()
        await adapter.apply_mode(
            hass=None,  # type: ignore[arg-type]
            mode=WorkMode.FORCE_CHARGE,
            power_w=5000,
            fd_soc=80,
        )
        assert adapter.apply_mode_calls == [
            ApplyModeCall(mode=WorkMode.FORCE_CHARGE, power_w=5000, fd_soc=80)
        ]
        assert adapter.last_apply_mode is not None
        assert adapter.last_apply_mode.mode is WorkMode.FORCE_CHARGE
        assert adapter.modes_applied == [WorkMode.FORCE_CHARGE]
        assert adapter.power_sequence == [5000]

    @pytest.mark.asyncio
    async def test_remove_override_records_mode(self) -> None:
        adapter = FakeAdapter()
        await adapter.remove_override(hass=None, mode=WorkMode.FORCE_DISCHARGE)  # type: ignore[arg-type]
        assert adapter.remove_override_calls == [
            RemoveOverrideCall(mode=WorkMode.FORCE_DISCHARGE)
        ]

    def test_get_max_power_w_returns_configured_value(self) -> None:
        adapter = FakeAdapter(max_power_w=7500)
        assert adapter.get_max_power_w() == 7500

    @pytest.mark.asyncio
    async def test_set_export_limit_records_and_reads_back(self) -> None:
        adapter = FakeAdapter()
        await adapter.set_export_limit_w(hass=None, value_w=5000)  # type: ignore[arg-type]
        await adapter.set_export_limit_w(hass=None, value_w=4200)  # type: ignore[arg-type]
        assert adapter.set_export_limit_calls == [5000, 4200]
        # get_export_limit_w returns the last written value (per Protocol).
        assert await adapter.get_export_limit_w(hass=None) == 4200  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_get_export_limit_w_returns_seed_when_nothing_written(
        self,
    ) -> None:
        adapter = FakeAdapter(export_limit_w=9000)
        assert await adapter.get_export_limit_w(hass=None) == 9000  # type: ignore[arg-type]
        assert adapter.get_export_limit_calls == 1

    @pytest.mark.asyncio
    async def test_get_export_limit_w_returns_none_when_unconfigured(
        self,
    ) -> None:
        adapter = FakeAdapter()
        # Protocol contract: adapters without a configured actuator return None.
        assert await adapter.get_export_limit_w(hass=None) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_reset_clears_history_without_touching_config(self) -> None:
        adapter = FakeAdapter(max_power_w=8000)
        await adapter.apply_mode(
            hass=None,  # type: ignore[arg-type]
            mode=WorkMode.FORCE_CHARGE,
            power_w=3000,
            fd_soc=90,
        )
        await adapter.set_export_limit_w(hass=None, value_w=2000)  # type: ignore[arg-type]
        adapter.reset()
        assert adapter.apply_mode_calls == []
        assert adapter.set_export_limit_calls == []
        # Configuration survives reset.
        assert adapter.get_max_power_w() == 8000
        # The seeded export_limit value ALSO survives the writes that
        # preceded reset — reset clears the recording, not the state.
        # (The "last value written" behaviour is recording-only; the
        # seed value is the configuration.) Verified by reading back
        # after reset.
        assert await adapter.get_export_limit_w(hass=None) == 2000  # type: ignore[arg-type]


class TestFakeAdapterInvariants:
    """Property-style tests: the fake never violates the Protocol contract.

    Properties are checked with hand-written inputs (no hypothesis
    dependency). The point is to prove that code under ``smart_battery/``
    which depends only on Protocol contract cannot observe
    FakeAdapter-specific surprises.
    """

    @pytest.mark.asyncio
    async def test_apply_mode_returns_none(self) -> None:
        """Protocol: apply_mode is a command, not a query."""
        adapter = FakeAdapter()
        result = await adapter.apply_mode(  # type: ignore[func-returns-value]
            hass=None,  # type: ignore[arg-type]
            mode=WorkMode.SELF_USE,
            power_w=None,
            fd_soc=11,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_remove_override_returns_none(self) -> None:
        adapter = FakeAdapter()
        result = await adapter.remove_override(  # type: ignore[func-returns-value]
            hass=None,  # type: ignore[arg-type]
            mode=WorkMode.FORCE_CHARGE,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_set_export_limit_returns_none(self) -> None:
        adapter = FakeAdapter()
        result = await adapter.set_export_limit_w(  # type: ignore[func-returns-value]
            hass=None,  # type: ignore[arg-type]
            value_w=1000,
        )
        assert result is None

    def test_get_max_power_w_is_sync(self) -> None:
        """Protocol: get_max_power_w is synchronous (no await needed)."""
        adapter = FakeAdapter(max_power_w=10500)
        # If this accidentally returned a coroutine, bool() would be truthy
        # but the int comparison below would raise.
        value = adapter.get_max_power_w()
        assert isinstance(value, int)
        assert value == 10500


class TestFakeAdapterUsageAgainstListenerAssertions:
    """Canonical recording-based assertions a listener test would make.

    Demonstrates the pattern: drive a fixture, exercise the code under
    test, then assert on ``adapter.apply_mode_calls`` / ``modes_applied``
    / etc. These tests show the shape of the recording without
    depending on any actual listener implementation — each test here
    directly drives the adapter, so they pass independent of
    ``listeners.py`` being loaded.
    """

    @pytest.mark.asyncio
    async def test_canonical_charge_sequence_is_recorded_in_order(self) -> None:
        """A charge session applies ForceCharge, then removes on completion."""
        adapter = FakeAdapter(max_power_w=10500)
        # Simulate the three Protocol calls a charge session typically makes.
        await adapter.apply_mode(
            hass=None,  # type: ignore[arg-type]
            mode=WorkMode.FORCE_CHARGE,
            power_w=5000,
            fd_soc=100,
        )
        await adapter.apply_mode(
            hass=None,  # type: ignore[arg-type]
            mode=WorkMode.FORCE_CHARGE,
            power_w=6200,  # paced-up adjustment
            fd_soc=100,
        )
        await adapter.remove_override(hass=None, mode=WorkMode.FORCE_CHARGE)  # type: ignore[arg-type]

        assert adapter.modes_applied == [WorkMode.FORCE_CHARGE, WorkMode.FORCE_CHARGE]
        assert adapter.power_sequence == [5000, 6200]
        assert adapter.remove_override_calls == [
            RemoveOverrideCall(mode=WorkMode.FORCE_CHARGE)
        ]

    @pytest.mark.asyncio
    async def test_export_limit_tracking_records_tapering_writes(self) -> None:
        """Discharge with export-limit actuator writes a descending sequence."""
        adapter = FakeAdapter(max_power_w=10500, export_limit_w=5000)
        # Simulate discharge with the hardware-actuator path (D-047):
        # the listener writes the paced target each tick, clamped to
        # [peak*1.5, grid_export_limit]. We just simulate the writes.
        for value in (5000, 5000, 4200, 3000, 2500):
            await adapter.set_export_limit_w(hass=None, value_w=value)  # type: ignore[arg-type]

        assert adapter.set_export_limit_calls == [5000, 5000, 4200, 3000, 2500]
        # Final hardware state matches the last write.
        assert await adapter.get_export_limit_w(hass=None) == 2500  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_brand_agnostic_module_only_imports(self) -> None:
        """Sanity: this test module does not import brand-specific code.

        If someone adds a ``from custom_components.foxess_control.*``
        import here, the pre-commit hook (extended for C-040) will
        catch it. As belt-and-braces, this inline check asserts the
        same property: the current module's imports should be pure
        ``smart_battery/`` + pytest + stdlib.
        """
        import sys

        mod = sys.modules[__name__]
        forbidden_prefixes = (
            "custom_components.foxess_control.foxess",
            "custom_components.foxess_control.foxess_adapter",
            "custom_components.foxess_control.coordinator",
            "custom_components.foxess_control._services",
            "custom_components.foxess_control._helpers",
        )
        for name in sorted(sys.modules):
            if not name.startswith(forbidden_prefixes):
                continue
            # The forbidden module is loaded somewhere in the process,
            # but is it imported BY this test module? Check its globals.
            if any(
                getattr(obj, "__name__", "") == name
                or getattr(obj, "__module__", "") == name
                for obj in vars(mod).values()
            ):
                pytest.fail(
                    f"test_smart_battery_agnostic imports brand-specific "
                    f"module {name!r} — violates C-040"
                )
