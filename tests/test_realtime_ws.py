"""Tests for FoxESS WebSocket real-time data mapping and password hashing."""

from __future__ import annotations

import hashlib

import pytest

from custom_components.foxess_control.foxess.realtime_ws import (
    map_ws_to_coordinator,
)
from custom_components.foxess_control.foxess.web_session import (
    ensure_password_hash,
)

# ---------------------------------------------------------------------------
# ensure_password_hash
# ---------------------------------------------------------------------------


class TestEnsurePasswordHash:
    def test_raw_password_is_hashed(self) -> None:
        result = ensure_password_hash("mypassword")
        assert result == hashlib.md5(b"mypassword").hexdigest()

    def test_md5_hex_passed_through(self) -> None:
        md5 = hashlib.md5(b"test").hexdigest()
        assert ensure_password_hash(md5) == md5

    def test_md5_uppercase_lowered(self) -> None:
        md5 = hashlib.md5(b"test").hexdigest().upper()
        assert ensure_password_hash(md5) == md5.lower()

    def test_31_char_string_is_hashed(self) -> None:
        short = "a" * 31
        result = ensure_password_hash(short)
        assert result == hashlib.md5(short.encode()).hexdigest()

    def test_33_char_string_is_hashed(self) -> None:
        long = "a" * 33
        result = ensure_password_hash(long)
        assert result == hashlib.md5(long.encode()).hexdigest()

    def test_non_hex_32_chars_is_hashed(self) -> None:
        non_hex = "g" * 32
        result = ensure_password_hash(non_hex)
        assert result == hashlib.md5(non_hex.encode()).hexdigest()

    def test_empty_string_is_hashed(self) -> None:
        result = ensure_password_hash("")
        assert result == hashlib.md5(b"").hexdigest()


# ---------------------------------------------------------------------------
# generate_signature — WASM request signing
# ---------------------------------------------------------------------------


class TestGenerateSignature:
    def test_known_signature(self) -> None:
        from custom_components.foxess_control.foxess.signature import (
            generate_signature,
        )

        sig = generate_signature("/basic/v0/user/login", "", "en", "1776124242356")
        assert sig == "02ed69731394e020c1a7e28d56a51013.5245784"

    def test_different_timestamp_gives_different_signature(self) -> None:
        from custom_components.foxess_control.foxess.signature import (
            generate_signature,
        )

        sig1 = generate_signature("/basic/v0/user/login", "", "en", "1776124242356")
        sig2 = generate_signature("/basic/v0/user/login", "", "en", "1776124300000")
        assert sig1 != sig2

    def test_signature_format(self) -> None:
        from custom_components.foxess_control.foxess.signature import (
            generate_signature,
        )

        sig = generate_signature("/basic/v0/user/login", "", "en", "1776124242356")
        parts = sig.split(".")
        assert len(parts) == 2
        assert len(parts[0]) == 32  # MD5 hex


# ---------------------------------------------------------------------------
# map_ws_to_coordinator — pure data mapping
# ---------------------------------------------------------------------------


class TestMapWsToCoordinator:
    def _make_msg(self, **node_overrides: object) -> dict[str, object]:
        """Build a minimal WebSocket message with node data."""
        node = {
            "solar": {"power": {"value": "3.5"}},
            "grid": {
                "power": {"value": "0.2"},
                "gridStatus": 3,
            },
            "bat": {
                "power": {"value": "1.5"},
                "soc": 65,
                "charge": 0,
            },
            "load": {"power": {"value": "2.0"}},
        }
        node.update(node_overrides)
        return {"errno": 0, "result": {"node": node, "timeDiff": 5}}

    def test_basic_mapping(self) -> None:
        data = map_ws_to_coordinator(self._make_msg())
        assert data["SoC"] == 65.0
        assert data["pvPower"] == 3.5
        assert data["loadsPower"] == 2.0
        assert data["batDischargePower"] == 1.5  # charge=0 -> discharging
        assert data["batChargePower"] == 0.0

    def test_battery_charging(self) -> None:
        msg = self._make_msg(bat={"power": {"value": "2.0"}, "soc": 45, "charge": 1})
        data = map_ws_to_coordinator(msg)
        assert data["SoC"] == 45.0
        assert data["batChargePower"] == 2.0
        assert data["batDischargePower"] == 0.0

    def test_battery_charge_flag_as_string(self) -> None:
        """WS may send charge flag as string or int."""
        msg = self._make_msg(bat={"power": {"value": "2.0"}, "soc": 45, "charge": "1"})
        data = map_ws_to_coordinator(msg)
        assert data["batChargePower"] == 2.0
        assert data["batDischargePower"] == 0.0

    def test_grid_importing(self) -> None:
        msg = self._make_msg(grid={"power": {"value": "0.5"}, "gridStatus": 3})
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == 0.5
        assert data["feedinPower"] == 0.0

    def test_grid_importing_string_status(self) -> None:
        """gridStatus may arrive as string."""
        msg = self._make_msg(grid={"power": {"value": "11.6"}, "gridStatus": "3"})
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == pytest.approx(11.6)
        assert data["feedinPower"] == 0.0

    def test_grid_exporting(self) -> None:
        msg = self._make_msg(grid={"power": {"value": "1.0"}, "gridStatus": 1})
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == 0.0
        assert data["feedinPower"] == 1.0

    def test_empty_message(self) -> None:
        assert map_ws_to_coordinator({}) == {}

    def test_empty_node(self) -> None:
        assert map_ws_to_coordinator({"result": {"node": {}}}) == {}

    def test_missing_power_value(self) -> None:
        msg = self._make_msg(solar={"power": None})
        data = map_ws_to_coordinator(msg)
        assert "pvPower" not in data

    def test_non_numeric_value_skipped(self) -> None:
        msg = self._make_msg(solar={"power": {"value": "N/A", "unit": "W"}})
        data = map_ws_to_coordinator(msg)
        assert "pvPower" not in data

    def test_zero_power(self) -> None:
        msg = self._make_msg(solar={"power": {"value": "0"}})
        data = map_ws_to_coordinator(msg)
        assert data["pvPower"] == 0.0

    def test_battery_soc_type_conversion(self) -> None:
        msg = self._make_msg(bat={"power": {"value": "0"}, "soc": "75", "charge": 0})
        data = map_ws_to_coordinator(msg)
        assert data["SoC"] == 75.0

    def test_error_message_returns_empty(self) -> None:
        msg = {"errno": 1, "result": {"node": {}}}
        # map_ws_to_coordinator only maps data, caller checks errno
        data = map_ws_to_coordinator(msg)
        assert data == {}

    def test_real_world_sample(self) -> None:
        """Test with actual FoxESS WebSocket message structure.

        Values are in kW (matching the REST API), despite the unit
        field sometimes reading "W".
        """
        msg = {
            "errno": 0,
            "msg": "",
            "result": {
                "node": {
                    "solar": {"power": {"value": "0.809", "unit": "W"}},
                    "grid": {
                        "power": {"value": "0.019", "unit": "W"},
                        "gridStatus": 3,
                        "gridToHidden": -1,
                    },
                    "bat": {
                        "power": {"value": "0.607", "unit": "W"},
                        "soc": 34,
                        "charge": 1,
                        "batToDevice": -1,
                    },
                    "load": {
                        "power": {"value": "0.183", "unit": "W"},
                        "normalLoad": {"power": {"value": "0.183", "unit": "W"}},
                        "backupLoad": {"power": {"value": "0", "unit": "W"}},
                    },
                    "device": {"power": {"value": "0.202", "unit": "W"}},
                    "charger": {"display": False},
                    "heatpump": {"display": False},
                },
                "timeDiff": 5,
                "lastUpdateDate": "Updated  within 1 minute",
                "plantId": "8d3f1896-19a6-40b0-86a1-d892185f5366",
            },
        }
        data = map_ws_to_coordinator(msg)
        assert data["SoC"] == 34.0
        assert data["pvPower"] == pytest.approx(0.809)
        assert data["batChargePower"] == pytest.approx(0.607)
        assert data["batDischargePower"] == 0.0
        assert data["loadsPower"] == pytest.approx(0.183)
        assert data["gridConsumptionPower"] == pytest.approx(0.019)
        assert data["feedinPower"] == 0.0
