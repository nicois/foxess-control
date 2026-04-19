"""Tests for FoxESSControlData bridge layer — _battery_compound_id key.

Regression: the _battery_compound_id key was missing from both the
dataclass fields and the _KEY_MAP, so __setitem__ raised KeyError on
write and get() returned None on read.  The compound battery ID
discovery succeeded but was never persisted, leaving
sensor.foxess_bms_battery_temperature permanently "unknown".
"""

from __future__ import annotations

from custom_components.foxess_control.domain_data import FoxESSControlData

COMPOUND_ID = "2d35cd73-c9a7-40f2-82e6-2f97e712126f@60E5M4805BLF116"
ALT_COMPOUND_ID = "aabbccdd-1234-5678-9abc-def012345678@BATSN999"


class TestBatteryCompoundIdBridgeLayer:
    """Verify _battery_compound_id round-trips through FoxESSControlData."""

    def test_setitem_does_not_raise(self) -> None:
        """Writing _battery_compound_id via [] must not raise KeyError."""
        data = FoxESSControlData()
        data["_battery_compound_id"] = COMPOUND_ID  # must not raise

    def test_get_returns_stored_value(self) -> None:
        """get('_battery_compound_id') must return what was stored."""
        data = FoxESSControlData()
        data["_battery_compound_id"] = COMPOUND_ID
        assert data.get("_battery_compound_id") == COMPOUND_ID

    def test_get_returns_none_before_store(self) -> None:
        """get('_battery_compound_id') returns None when never written."""
        data = FoxESSControlData()
        assert data.get("_battery_compound_id") is None

    def test_round_trip_ws_path(self) -> None:
        """Simulate WS path: pop from ws_data dict, store in domain_data."""
        ws_data: dict[str, object] = {"_battery_compound_id": COMPOUND_ID, "soc": 68}
        domain_data = FoxESSControlData()

        compound_id = ws_data.pop("_battery_compound_id", None)
        assert compound_id == COMPOUND_ID
        if compound_id:
            domain_data["_battery_compound_id"] = compound_id

        assert domain_data.get("_battery_compound_id") == COMPOUND_ID

    def test_pop_returns_stored_value(self) -> None:
        """pop('_battery_compound_id') returns the value and resets it."""
        data = FoxESSControlData()
        data["_battery_compound_id"] = COMPOUND_ID
        val = data.pop("_battery_compound_id")
        assert val == COMPOUND_ID
        assert data.get("_battery_compound_id") is None

    def test_pop_default_when_unset(self) -> None:
        """pop('_battery_compound_id', None) returns None when never written."""
        data = FoxESSControlData()
        assert data.pop("_battery_compound_id", None) is None

    def test_contains_after_store(self) -> None:
        """'_battery_compound_id' in data is True after storing."""
        data = FoxESSControlData()
        data["_battery_compound_id"] = COMPOUND_ID
        assert "_battery_compound_id" in data

    def test_contains_before_store(self) -> None:
        """'_battery_compound_id' in data is True even before storing.

        The key is in _KEY_MAP, so __contains__ returns True regardless
        of whether a value has been written (same as other mapped keys).
        """
        data = FoxESSControlData()
        assert "_battery_compound_id" in data

    def test_overwrite_updates_value(self) -> None:
        """Writing a second value overwrites the first."""
        data = FoxESSControlData()
        data["_battery_compound_id"] = COMPOUND_ID
        data["_battery_compound_id"] = ALT_COMPOUND_ID
        assert data.get("_battery_compound_id") == ALT_COMPOUND_ID

    def test_getitem_returns_stored_value(self) -> None:
        """data['_battery_compound_id'] returns stored value."""
        data = FoxESSControlData()
        data["_battery_compound_id"] = COMPOUND_ID
        assert data["_battery_compound_id"] == COMPOUND_ID
