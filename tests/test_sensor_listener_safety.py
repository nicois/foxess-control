"""Regression tests for C-026 sensor-listener Repair surfacing.

Production incident 2026-04-25: ``async_write_ha_state`` raised
``ValueError: Sensor sensor.foxess_smart_operations provides state value
'scheduled', which is not in the list of options provided`` from within
``DataUpdateCoordinator.async_update_listeners``. The underlying
options-list bug has been fixed (commit 9b95dec), but the observability
failure remains:

* ``async_update_listeners`` iterates listeners with no try/except, so a
  single listener exception halts the whole fan-out. Every listener
  registered AFTER the failing sensor never receives the update.
* No Repair issue was created, the sensor kept its stale state, and the
  user had no UI signal that every FoxESS sensor had stopped updating
  for 50+ minutes.

C-026 requires persistent errors to be surfaced via the UI, not just
logs. This test locks in the new contract:

1. A failing sensor listener does NOT halt iteration — subsequent
   listeners still get their updates.
2. A Repair issue is created identifying the offending sensor and the
   exception, so the user can see "something is wrong with sensor X" in
   the Home Assistant UI.
3. The Repair issue is idempotent on repeated failures (no spam).
4. The Repair issue is dismissed when the next successful write lands.
5. The protection applies to every sensor-listener callback, not just
   ``SmartOperationsOverviewSensor`` (proved with a second class).

Traces: C-020 (UI shows truth), C-026 (errors surfaced proactively).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.foxess_control.const import DOMAIN
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
)
from custom_components.foxess_control.sensor import (
    InverterOverrideStatusSensor,
    SmartOperationsOverviewSensor,
)

# ---------------------------------------------------------------------------
# Minimal coordinator that mirrors HA's DataUpdateCoordinator iteration.
#
# We deliberately replicate the production contract — "listeners are a
# dict of callbacks and a tick calls each one in registration order" —
# instead of using a MagicMock, because the bug lives in the iteration
# semantics. A mock here would pass regardless of whether iteration
# actually continues. C-028 (simulator over mocks) extends by analogy:
# use the real code path, not a test double, for the thing under test.
# ---------------------------------------------------------------------------


class _ListenerCoordinator:
    """Listener ordering identical to ``DataUpdateCoordinator``."""

    def __init__(self) -> None:
        # dict preserves insertion order; HA also uses a dict-of-listeners
        # internally (see update_coordinator.DataUpdateCoordinator).
        self._listeners: dict[int, Any] = {}
        self._next_id = 0
        self.data: dict[str, Any] | None = {"SoC": 50.0}

    def async_add_listener(self, callback: Any, _context: Any = None) -> Any:
        lid = self._next_id
        self._next_id += 1
        self._listeners[lid] = callback

        def _remove() -> None:
            self._listeners.pop(lid, None)

        return _remove

    def async_update_listeners(self) -> None:
        """Fan out updates.

        This MUST NOT catch exceptions — that is the production contract.
        If a listener raises, iteration halts. The wrapping is the
        responsibility of the sensor-side callback.
        """
        for cb in list(self._listeners.values()):
            cb()


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _make_hass() -> HomeAssistant:
    """Real Home Assistant with bus + issue registry available.

    Not run inside an event loop — ``async_create_issue`` /
    ``async_delete_issue`` are synchronous (``@callback``) and only touch
    ``hass.data`` + ``hass.bus``; they do not await.
    """
    ha = HomeAssistant("/tmp")
    # issue_registry.async_get uses ``@singleton(DATA_REGISTRY)`` which
    # caches on hass.data — seed it with a fresh registry.
    ha.data[ir.DATA_REGISTRY] = ir.IssueRegistry(ha)
    # Bypass event-loop-thread checks — IssueRegistry mutations verify
    # they run on the loop, but in these tests there is no running loop.
    # MagicMock with no spec silently accepts any args.
    ha.verify_event_loop_thread = MagicMock()  # type: ignore[method-assign]
    return ha


def _make_entry(entry_id: str = "entry_sensor_safety") -> Any:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {}
    entry.options = {"battery_capacity_kwh": 10.0}
    entry.runtime_data = None
    return entry


def _seed_domain(hass: HomeAssistant, coord: _ListenerCoordinator) -> None:
    dd = FoxESSControlData()
    entry_data = FoxESSEntryData(inverter=MagicMock(), coordinator=coord)
    dd.entries["entry_sensor_safety"] = entry_data
    hass.data[DOMAIN] = dd
    # Note: ``hass.config_entries`` is None here — the tests exercise only
    # the ``_safe_write_ha_state`` helper path, which does not read
    # ``native_value`` (that happens inside the HA SensorEntity base
    # class, which we bypass by monkey-patching ``async_write_ha_state``
    # itself).  If the helper grows to read options, wire up a stub then.


def _wire_sensor(sensor: Any, raising_exc: Exception | None) -> None:
    """Monkeypatch ``async_write_ha_state`` to raise or to no-op."""
    if raising_exc is not None:

        def _raise() -> None:
            raise raising_exc

        sensor.async_write_ha_state = _raise
    else:
        sensor.async_write_ha_state = MagicMock()


# ---------------------------------------------------------------------------
# Neighbourhood cases
# ---------------------------------------------------------------------------


class TestSensorListenerFailureSurfacesRepair:
    """C-026 — sensor write failures must appear as UI Repair issues."""

    @pytest.mark.asyncio
    async def test_failing_listener_does_not_halt_iteration(self) -> None:
        """Other sensors still get their update when one listener raises.

        Load-bearing assertion: ``_ListenerCoordinator`` has no native
        try/except. Without the fix, sensor B's callback never fires.
        """
        hass = _make_hass()
        coord = _ListenerCoordinator()
        _seed_domain(hass, coord)

        # Sensor A will raise when its write is called.
        from smart_battery.sensor_base import _safe_write_ha_state

        sensor_a = SmartOperationsOverviewSensor(hass, _make_entry())
        sensor_a.entity_id = "sensor.foxess_smart_operations"
        _wire_sensor(sensor_a, ValueError("fake: not in the list of options"))

        sensor_b = InverterOverrideStatusSensor(hass, _make_entry())
        sensor_b.entity_id = "sensor.foxess_override_status"
        _wire_sensor(sensor_b, None)

        # Wire both listeners, mirroring the production helper.
        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor_a))
        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor_b))

        # Trigger one fan-out — sensor A will raise, sensor B must still run.
        coord.async_update_listeners()

        # Sensor B's callback ran — its async_write_ha_state was called.
        assert sensor_b.async_write_ha_state.call_count == 1, (  # type: ignore[attr-defined]
            "Sensor B's listener was skipped — iteration halted at sensor A"
        )

    @pytest.mark.asyncio
    async def test_repair_issue_created_with_entity_id(self) -> None:
        """A failure creates a Repair keyed by entity_id with the exc message."""
        hass = _make_hass()
        coord = _ListenerCoordinator()
        _seed_domain(hass, coord)

        from smart_battery.sensor_base import _safe_write_ha_state

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        sensor.entity_id = "sensor.foxess_smart_operations"
        _wire_sensor(sensor, ValueError("not in the list of options"))
        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor))

        coord.async_update_listeners()

        registry = ir.async_get(hass)
        issues = [i for i in registry.issues.values() if i.domain == DOMAIN]
        assert issues, (
            "No Repair issues registered after listener failure — the user "
            "has no UI signal that the sensor is broken (C-026 violation)"
        )
        offending = [
            i
            for i in issues
            if i.data and i.data.get("entity_id") == "sensor.foxess_smart_operations"
        ]
        assert offending, (
            f"Repair issue missing entity_id identifying the failing sensor; "
            f"issues: {[(i.issue_id, i.data) for i in issues]}"
        )

    @pytest.mark.asyncio
    async def test_repeated_failures_do_not_spam(self) -> None:
        """Repeated failures reuse one Repair issue (idempotent key)."""
        hass = _make_hass()
        coord = _ListenerCoordinator()
        _seed_domain(hass, coord)

        from smart_battery.sensor_base import _safe_write_ha_state

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        sensor.entity_id = "sensor.foxess_smart_operations"
        _wire_sensor(sensor, ValueError("not in the list of options"))
        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor))

        for _ in range(5):
            coord.async_update_listeners()

        registry = ir.async_get(hass)
        sensor_issues = [
            i
            for i in registry.issues.values()
            if i.domain == DOMAIN
            and i.data
            and i.data.get("entity_id") == "sensor.foxess_smart_operations"
        ]
        assert len(sensor_issues) == 1, (
            f"Expected exactly one Repair for the failing sensor (idempotent key), "
            f"got {len(sensor_issues)}: {[i.issue_id for i in sensor_issues]}"
        )

    @pytest.mark.asyncio
    async def test_recovery_dismisses_repair(self) -> None:
        """After a successful write, the Repair issue is cleared."""
        hass = _make_hass()
        coord = _ListenerCoordinator()
        _seed_domain(hass, coord)

        from smart_battery.sensor_base import _safe_write_ha_state

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        sensor.entity_id = "sensor.foxess_smart_operations"
        # First: a failure — issue created.
        _wire_sensor(sensor, ValueError("not in the list of options"))
        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor))
        coord.async_update_listeners()

        registry = ir.async_get(hass)
        pre = [
            i
            for i in registry.issues.values()
            if i.domain == DOMAIN
            and i.data
            and i.data.get("entity_id") == "sensor.foxess_smart_operations"
        ]
        assert pre, "Pre-condition: Repair should exist after failure"

        # Now the sensor recovers — replace the raising stub with a no-op
        # and trigger another fan-out.
        sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        coord.async_update_listeners()

        post = [
            i
            for i in registry.issues.values()
            if i.domain == DOMAIN
            and i.data
            and i.data.get("entity_id") == "sensor.foxess_smart_operations"
            and i.active
        ]
        assert not post, (
            f"Repair issue not cleared after successful write — the user will "
            f"see a stale 'sensor broken' issue forever. Issues still present: "
            f"{[(i.issue_id, i.active) for i in post]}"
        )

    @pytest.mark.asyncio
    async def test_pattern_applies_to_other_sensor_classes(self) -> None:
        """The helper protects every sensor listener, not just one class."""
        hass = _make_hass()
        coord = _ListenerCoordinator()
        _seed_domain(hass, coord)

        from smart_battery.sensor_base import _safe_write_ha_state

        # Use a different sensor class (OverrideStatusSensor) and prove
        # its failure also surfaces a Repair and doesn't halt iteration.
        sensor_a = InverterOverrideStatusSensor(hass, _make_entry())
        sensor_a.entity_id = "sensor.foxess_override_status"
        _wire_sensor(sensor_a, RuntimeError("fake inverter detach"))

        sensor_b = SmartOperationsOverviewSensor(hass, _make_entry())
        sensor_b.entity_id = "sensor.foxess_smart_operations"
        _wire_sensor(sensor_b, None)

        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor_a))
        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor_b))
        coord.async_update_listeners()

        # (a) Later listener still ran.
        assert sensor_b.async_write_ha_state.call_count == 1, (  # type: ignore[attr-defined]
            "Later listener did not run — safe-write helper isn't wired on "
            "OverrideStatusSensor path"
        )
        # (b) Repair surfaced for the failing sensor.
        registry = ir.async_get(hass)
        hits = [
            i
            for i in registry.issues.values()
            if i.domain == DOMAIN
            and i.data
            and i.data.get("entity_id") == "sensor.foxess_override_status"
        ]
        assert hits, (
            "OverrideStatusSensor failure did not surface a Repair issue — "
            "the safe-write helper is not universal"
        )

    @pytest.mark.asyncio
    async def test_helper_logs_the_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The log message is still produced — helper augments, not replaces."""
        hass = _make_hass()
        coord = _ListenerCoordinator()
        _seed_domain(hass, coord)

        from smart_battery.sensor_base import _safe_write_ha_state

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        sensor.entity_id = "sensor.foxess_smart_operations"
        msg = "value 'scheduled' is not in the list of options provided"
        _wire_sensor(sensor, ValueError(msg))
        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor))

        with caplog.at_level(logging.ERROR, logger="smart_battery.sensor_base"):
            coord.async_update_listeners()

        assert any(
            "sensor.foxess_smart_operations" in r.getMessage() for r in caplog.records
        ), (
            f"Error log missing or does not name the sensor; records: "
            f"{[r.getMessage() for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Sanity: the helper also returns cleanly on a non-raising listener
# ---------------------------------------------------------------------------


class TestSafeWriteHelperHappyPath:
    """The helper must be invisible when there is no failure."""

    @pytest.mark.asyncio
    async def test_no_issue_when_write_succeeds(self) -> None:
        hass = _make_hass()
        coord = _ListenerCoordinator()
        _seed_domain(hass, coord)

        from smart_battery.sensor_base import _safe_write_ha_state

        sensor = SmartOperationsOverviewSensor(hass, _make_entry())
        sensor.entity_id = "sensor.foxess_smart_operations"
        _wire_sensor(sensor, None)
        coord.async_add_listener(lambda: _safe_write_ha_state(hass, DOMAIN, sensor))
        coord.async_update_listeners()

        registry = ir.async_get(hass)
        hits = [i for i in registry.issues.values() if i.domain == DOMAIN]
        assert not hits, (
            f"Happy-path write created a spurious Repair issue: "
            f"{[i.issue_id for i in hits]}"
        )
