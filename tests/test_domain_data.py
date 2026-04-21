"""Tests for FoxESSControlData typed attribute access.

Verifies that battery_compound_id (and other fields) round-trip through
the typed dataclass correctly — the original bridge-layer regression
where compound_id writes were silently lost is now impossible because
the field is a typed dataclass attribute.
"""

from __future__ import annotations

from custom_components.foxess_control.domain_data import FoxESSControlData

COMPOUND_ID = "2d35cd73-c9a7-40f2-82e6-2f97e712126f@60E5M4805BLF116"
ALT_COMPOUND_ID = "aabbccdd-1234-5678-9abc-def012345678@BATSN999"


class TestBatteryCompoundId:
    """Verify battery_compound_id attribute on FoxESSControlData."""

    def test_default_is_none(self) -> None:
        data = FoxESSControlData()
        assert data.battery_compound_id is None

    def test_set_and_read(self) -> None:
        data = FoxESSControlData()
        data.battery_compound_id = COMPOUND_ID
        assert data.battery_compound_id == COMPOUND_ID

    def test_overwrite_updates_value(self) -> None:
        data = FoxESSControlData()
        data.battery_compound_id = COMPOUND_ID
        data.battery_compound_id = ALT_COMPOUND_ID
        assert data.battery_compound_id == ALT_COMPOUND_ID

    def test_reset_to_none(self) -> None:
        data = FoxESSControlData()
        data.battery_compound_id = COMPOUND_ID
        data.battery_compound_id = None
        assert data.battery_compound_id is None

    def test_round_trip_ws_path(self) -> None:
        """Simulate WS path: pop from ws_data dict, store in domain_data."""
        ws_data: dict[str, object] = {"_battery_compound_id": COMPOUND_ID, "soc": 68}
        domain_data = FoxESSControlData()

        compound_id = ws_data.pop("_battery_compound_id", None)
        assert compound_id == COMPOUND_ID
        if isinstance(compound_id, str):
            domain_data.battery_compound_id = compound_id

        assert domain_data.battery_compound_id == COMPOUND_ID
