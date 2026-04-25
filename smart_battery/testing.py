"""Brand-agnostic test doubles for the InverterAdapter Protocol.

This module is part of ``smart_battery/`` and MUST NOT import from any
brand-specific module (C-021, C-039). It exists so tests of the
common package can exercise the listener / service / sensor code
paths against a recording stub that adheres strictly to the
:class:`~smart_battery.adapter.InverterAdapter` Protocol — without
loading a FoxESS client, a simulator, or any brand-specific
response shape.

Why this matters (from the knowledge tree, P-006 brand portability):
the same listener state machine must run against every brand's
adapter. Tests that exercise listeners through the FoxESS adapter
cannot prove "the listener is brand-agnostic" — they only prove
"the listener works against FoxESS". A FakeAdapter that returns
exactly what the Protocol promises and records every call lets the
listener's observable contract be asserted directly, so a future
Huawei / SolaX / Sungrow adapter doesn't require duplicating that
test coverage.

Usage::

    from smart_battery.testing import FakeAdapter

    adapter = FakeAdapter(max_power_w=10000)
    # ...wire adapter into your listener / session fixture...
    # exercise the code under test
    assert adapter.apply_mode_calls[0] == (WorkMode.FORCE_CHARGE, 5000, 100)
    assert adapter.set_export_limit_calls == [5000, 4200]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .types import WorkMode


@dataclass
class ApplyModeCall:
    """One recorded :meth:`InverterAdapter.apply_mode` invocation."""

    mode: WorkMode
    power_w: int | None
    fd_soc: int


@dataclass
class RemoveOverrideCall:
    """One recorded :meth:`InverterAdapter.remove_override` invocation."""

    mode: WorkMode


@dataclass
class FakeAdapter:
    """In-memory :class:`InverterAdapter` that records every call.

    The adapter satisfies the full Protocol contract — apply_mode,
    remove_override, get_max_power_w, set_export_limit_w,
    get_export_limit_w — and returns values the Protocol docstrings
    promise (e.g. None from async methods, int from get_max_power_w,
    int-or-None from get_export_limit_w). No brand-specific response
    shape leaks in.

    Every call is appended to a per-method list so tests can assert
    the exact sequence of Protocol interactions a listener or service
    produces for a given input scenario.  This is the primary way to
    prove that code under ``smart_battery/`` is brand-agnostic: if a
    test passes with FakeAdapter, it will pass with ANY correct
    adapter implementation.

    The optional ``export_limit_w`` seeds the value returned by
    :meth:`get_export_limit_w`; callers can mutate it via the
    ``_export_limit_w`` attribute to simulate external changes
    between ticks.
    """

    max_power_w: int = 10000
    export_limit_w: int | None = None

    apply_mode_calls: list[ApplyModeCall] = field(default_factory=list)
    remove_override_calls: list[RemoveOverrideCall] = field(default_factory=list)
    set_export_limit_calls: list[int] = field(default_factory=list)
    get_export_limit_calls: int = 0

    def __post_init__(self) -> None:
        self._export_limit_w: int | None = self.export_limit_w

    # ------------------------------------------------------------------
    # InverterAdapter Protocol implementation
    # ------------------------------------------------------------------

    async def apply_mode(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
        power_w: int | None = None,
        fd_soc: int = 11,
    ) -> None:
        self.apply_mode_calls.append(
            ApplyModeCall(mode=mode, power_w=power_w, fd_soc=fd_soc)
        )

    async def remove_override(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
    ) -> None:
        self.remove_override_calls.append(RemoveOverrideCall(mode=mode))

    def get_max_power_w(self) -> int:
        return self.max_power_w

    async def set_export_limit_w(
        self,
        hass: HomeAssistant,
        value_w: int,
    ) -> None:
        self.set_export_limit_calls.append(value_w)
        self._export_limit_w = value_w

    async def get_export_limit_w(
        self,
        hass: HomeAssistant,
    ) -> int | None:
        self.get_export_limit_calls += 1
        return self._export_limit_w

    # ------------------------------------------------------------------
    # Convenience accessors for assertions
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all recorded calls without changing configured values.

        Useful between phases of a multi-step test so later-phase
        assertions only see the calls produced after the reset.
        """
        self.apply_mode_calls.clear()
        self.remove_override_calls.clear()
        self.set_export_limit_calls.clear()
        self.get_export_limit_calls = 0

    @property
    def last_apply_mode(self) -> ApplyModeCall | None:
        return self.apply_mode_calls[-1] if self.apply_mode_calls else None

    @property
    def last_remove_override(self) -> RemoveOverrideCall | None:
        return self.remove_override_calls[-1] if self.remove_override_calls else None

    @property
    def modes_applied(self) -> list[WorkMode]:
        """The sequence of WorkMode values passed to apply_mode."""
        return [call.mode for call in self.apply_mode_calls]

    @property
    def power_sequence(self) -> list[int | None]:
        """The sequence of power_w values passed to apply_mode."""
        return [call.power_w for call in self.apply_mode_calls]
