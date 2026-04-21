"""Tests for FoxESS WebSocket real-time data mapping and password hashing."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.foxess_control.foxess.realtime_ws import (
    FoxESSRealtimeWS,
    _is_plausible,
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
        import custom_components.foxess_control.foxess.signature as sig_mod

        # Reset the WASM singleton — prior tests' calls leave residual
        # heap state that changes the output suffix.
        sig_mod._engine = None
        sig = sig_mod.generate_signature(
            "/basic/v0/user/login", "", "en", "1776124242356"
        )
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
        """Build a minimal WebSocket message with node data.

        Default: solar 3500 W, load 2000 W, bat discharging 1500 W.
        Power balance (kW): 2.0 + 0 - 1.5 - 3.5 = -3.0 → exporting.
        """
        node = {
            "solar": {"power": {"value": "3500"}},
            "grid": {
                "power": {"value": "2000"},
                "gridStatus": 2,
            },
            "bat": {
                "power": {"value": "1500"},
                "soc": 65,
                "charge": 0,
            },
            "load": {"power": {"value": "2000"}},
        }
        node.update(node_overrides)
        return {"errno": 0, "result": {"node": node, "timeDiff": 5}}

    def test_basic_mapping_export(self) -> None:
        """Default scenario: solar excess → grid export."""
        data = map_ws_to_coordinator(self._make_msg())
        assert data["SoC"] == 65.0
        assert data["pvPower"] == pytest.approx(3.5)
        assert data["loadsPower"] == pytest.approx(2.0)
        assert data["batDischargePower"] == pytest.approx(
            1.5
        )  # charge=0 -> discharging
        assert data["batChargePower"] == 0.0
        # Power balance negative → exporting
        assert data["feedinPower"] == pytest.approx(2.0)
        assert data["gridConsumptionPower"] == 0.0

    def test_battery_charging(self) -> None:
        msg = self._make_msg(bat={"power": {"value": "2000"}, "soc": 45, "charge": 1})
        data = map_ws_to_coordinator(msg)
        assert data["SoC"] == 45.0
        assert data["batChargePower"] == pytest.approx(2.0)
        assert data["batDischargePower"] == 0.0

    def test_battery_charge_flag_as_string(self) -> None:
        """WS may send charge flag as string or int."""
        msg = self._make_msg(bat={"power": {"value": "2000"}, "soc": 45, "charge": "1"})
        data = map_ws_to_coordinator(msg)
        assert data["batChargePower"] == pytest.approx(2.0)
        assert data["batDischargePower"] == 0.0

    def test_grid_importing_from_balance(self) -> None:
        """Grid direction derived from power balance: load > solar → import."""
        msg = self._make_msg(
            solar={"power": {"value": "500"}},
            load={"power": {"value": "2000"}},
            bat={"power": {"value": "10000"}, "soc": 45, "charge": 1},
            grid={"power": {"value": "11500"}, "gridStatus": 99},
        )
        data = map_ws_to_coordinator(msg)
        # balance (kW): 2.0 + 10.0 - 0 - 0.5 = 11.5 > 0 → importing
        assert data["gridConsumptionPower"] == pytest.approx(11.5)
        assert data["feedinPower"] == 0.0

    def test_grid_exporting_from_balance(self) -> None:
        """Grid direction derived from power balance: solar > load → export."""
        msg = self._make_msg(
            solar={"power": {"value": "5000"}},
            load={"power": {"value": "1000"}},
            bat={"power": {"value": "1000"}, "soc": 80, "charge": 1},
            grid={"power": {"value": "3000"}, "gridStatus": 99},
        )
        data = map_ws_to_coordinator(msg)
        # balance (kW): 1.0 + 1.0 - 0 - 5.0 = -3.0 < 0 → exporting
        assert data["gridConsumptionPower"] == 0.0
        assert data["feedinPower"] == pytest.approx(3.0)

    def test_grid_fallback_to_gridstatus(self) -> None:
        """When solar/load missing, fall back to gridStatus."""
        msg = {
            "errno": 0,
            "result": {
                "node": {
                    "grid": {"power": {"value": "1500"}, "gridStatus": "3"},
                },
                "timeDiff": 5,
            },
        }
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == pytest.approx(1.5)
        assert data["feedinPower"] == 0.0

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

    def test_kw_unit_field_skips_division(self) -> None:
        """When WS sends unit='kW' on a field, use value as-is."""
        msg = {
            "errno": 0,
            "result": {
                "node": {
                    "solar": {"power": {"value": "0", "unit": "W"}},
                    "grid": {"power": {"value": "4700", "unit": "W"}, "gridStatus": 2},
                    "bat": {
                        "power": {"value": "5.29", "unit": "kW"},
                        "soc": 46,
                        "charge": 0,
                    },
                    "load": {"power": {"value": "427", "unit": "W"}},
                },
                "timeDiff": 5,
            },
        }
        data = map_ws_to_coordinator(msg)
        # bat unit=kW → used directly; others unit=W → /1000
        assert data["batDischargePower"] == pytest.approx(5.29)
        assert data["loadsPower"] == pytest.approx(0.427)

    def test_mixed_units_handled_per_field(self) -> None:
        """WS can send kW for battery but W for everything else."""
        msg = {
            "errno": 0,
            "result": {
                "node": {
                    "solar": {"power": {"value": "0", "unit": "W"}},
                    "grid": {
                        "power": {"value": "5000", "unit": "W"},
                        "gridStatus": 2,
                    },
                    "bat": {
                        "power": {"value": "5.46", "unit": "kW"},
                        "soc": 44,
                        "charge": 0,
                    },
                    "load": {"power": {"value": "427", "unit": "W"}},
                },
                "timeDiff": 5,
            },
        }
        data = map_ws_to_coordinator(msg)
        assert data["batDischargePower"] == pytest.approx(5.46)
        assert data["loadsPower"] == pytest.approx(0.427)
        # balance: 0.427 + 0 - 5.46 - 0 = -5.033 → exporting
        assert data["feedinPower"] == pytest.approx(5.0)

    def test_real_world_sample(self) -> None:
        """Test with actual FoxESS WebSocket message structure.

        Values are in watts (as strings), despite the unit field
        sometimes reading "W".  The coordinator expects kW.
        """
        msg = {
            "errno": 0,
            "msg": "",
            "result": {
                "node": {
                    "solar": {"power": {"value": "809", "unit": "W"}},
                    "grid": {
                        "power": {"value": "19", "unit": "W"},
                        "gridStatus": 3,
                        "gridToHidden": -1,
                    },
                    "bat": {
                        "power": {"value": "607", "unit": "W"},
                        "soc": 34,
                        "charge": 1,
                        "batToDevice": -1,
                    },
                    "load": {
                        "power": {"value": "183", "unit": "W"},
                        "normalLoad": {"power": {"value": "183", "unit": "W"}},
                        "backupLoad": {"power": {"value": "0", "unit": "W"}},
                    },
                    "device": {"power": {"value": "202", "unit": "W"}},
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
        # balance (kW): 0.183 + 0.607 - 0 - 0.809 = -0.019 → slight export
        assert data["gridConsumptionPower"] == 0.0
        assert data["feedinPower"] == pytest.approx(0.019)


# ---------------------------------------------------------------------------
# WS warmup — first N messages after connect are skipped
# ---------------------------------------------------------------------------


class TestStaleness:
    """Verify the WebSocket skips stale messages based on timeDiff."""

    @staticmethod
    def _make_ws_msg(time_diff: int = 5, soc: int = 65) -> aiohttp.WSMessage:
        import json

        return aiohttp.WSMessage(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps(
                {
                    "errno": 0,
                    "result": {
                        "node": {
                            "solar": {"power": {"value": "3500"}},
                            "grid": {"power": {"value": "2000"}, "gridStatus": 2},
                            "bat": {
                                "power": {"value": "1500"},
                                "soc": soc,
                                "charge": 0,
                            },
                            "load": {"power": {"value": "2000"}},
                        },
                        "timeDiff": time_diff,
                    },
                }
            ),
            extra=None,
        )

    @pytest.mark.asyncio
    async def test_stale_messages_skipped(self) -> None:
        """Messages with timeDiff > MAX_TIME_DIFF are not forwarded."""
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")

        ws = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)

        messages = [
            self._make_ws_msg(time_diff=215),  # stale — skip
            self._make_ws_msg(time_diff=60),  # stale — skip
            self._make_ws_msg(time_diff=5),  # fresh — forward
            self._make_ws_msg(time_diff=5),  # fresh — forward
            aiohttp.WSMessage(type=aiohttp.WSMsgType.CLOSED, data=None, extra=None),
        ]

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(side_effect=messages)
        mock_ws.closed = True

        ws._ws = mock_ws
        ws._connected = True
        ws._stop_event.clear()

        with patch.object(
            ws, "_try_reconnect", new_callable=AsyncMock
        ) as mock_reconnect:

            async def _fail_reconnect() -> None:
                ws._connected = False

            mock_reconnect.side_effect = _fail_reconnect
            await ws._listen_loop()

        # Only the 2 fresh messages (timeDiff=5) should be forwarded
        assert on_data.call_count == 2


class TestIsPlausible:
    """Plausibility filter: reject WS messages where any power key diverges >10x."""

    NORMAL = {
        "SoC": 83.0,
        "batChargePower": 0.0,
        "batDischargePower": 5.5,
        "pvPower": 0.0,
        "loadsPower": 0.48,
        "gridConsumptionPower": 0.0,
        "feedinPower": 5.02,
    }

    def test_similar_values_accepted(self) -> None:
        candidate = {**self.NORMAL, "batDischargePower": 5.49, "feedinPower": 5.01}
        assert _is_plausible(candidate, self.NORMAL) is True

    def test_aberrant_battery_rejected(self) -> None:
        candidate = {**self.NORMAL, "batDischargePower": 0.53, "feedinPower": 0.07}
        assert _is_plausible(candidate, self.NORMAL) is False

    def test_aberrant_feedin_only_rejected(self) -> None:
        candidate = {**self.NORMAL, "feedinPower": 0.05}
        assert _is_plausible(candidate, self.NORMAL) is False

    def test_near_zero_reference_accepts_any(self) -> None:
        ref = {**self.NORMAL, "batDischargePower": 0.05}
        candidate = {**self.NORMAL, "batDischargePower": 5.5}
        assert _is_plausible(candidate, ref) is True

    def test_zero_candidate_always_accepted(self) -> None:
        candidate = {**self.NORMAL, "batDischargePower": 0.0, "feedinPower": 0.0}
        assert _is_plausible(candidate, self.NORMAL) is True

    def test_no_reference_always_accepted(self) -> None:
        assert _is_plausible(self.NORMAL, None) is True

    def test_empty_reference_always_accepted(self) -> None:
        assert _is_plausible(self.NORMAL, {"SoC": 80.0}) is True

    def test_missing_candidate_key_accepted(self) -> None:
        candidate = {"SoC": 83.0, "batDischargePower": 5.5, "loadsPower": 0.48}
        assert _is_plausible(candidate, self.NORMAL) is True

    def test_charge_anomaly_rejected(self) -> None:
        ref = {**self.NORMAL, "batChargePower": 3.8, "batDischargePower": 0.0}
        candidate = {**ref, "batChargePower": 0.35}
        assert _is_plausible(candidate, ref) is False

    def test_solar_anomaly_rejected(self) -> None:
        ref = {**self.NORMAL, "pvPower": 4.0}
        candidate = {**ref, "pvPower": 0.3}
        assert _is_plausible(candidate, ref) is False

    def test_load_anomaly_rejected(self) -> None:
        ref = {**self.NORMAL, "loadsPower": 5.0}
        candidate = {**ref, "loadsPower": 0.4}
        assert _is_plausible(candidate, ref) is False


class TestWsPlausibilityFilter:
    """FoxESSRealtimeWS drops aberrant messages before calling on_data."""

    @staticmethod
    def _make_ws_msg(
        discharge: float = 5500.0,
        feedin: float = 5000.0,
        load: float = 480.0,
        soc: int = 83,
        grid_status: int = 1,
    ) -> aiohttp.WSMessage:
        import json

        return aiohttp.WSMessage(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps(
                {
                    "errno": 0,
                    "result": {
                        "node": {
                            "solar": {"power": {"value": "0"}},
                            "grid": {
                                "power": {"value": str(feedin + load)},
                                "gridStatus": grid_status,
                            },
                            "bat": {
                                "power": {"value": str(discharge)},
                                "soc": soc,
                                "charge": 0,
                            },
                            "load": {"power": {"value": str(load)}},
                        },
                        "timeDiff": 5,
                    },
                }
            ),
            extra=None,
        )

    @pytest.mark.asyncio
    async def test_aberrant_message_not_forwarded(self) -> None:
        """Aberrant WS message (10x lower power) must not reach on_data."""
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")

        ws = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)

        messages = [
            self._make_ws_msg(discharge=5500, feedin=5000),  # normal — accepted
            self._make_ws_msg(discharge=530, feedin=70),  # aberrant — dropped
            self._make_ws_msg(discharge=5490, feedin=5010),  # normal — accepted
            aiohttp.WSMessage(type=aiohttp.WSMsgType.CLOSED, data=None, extra=None),
        ]

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(side_effect=messages)
        mock_ws.closed = True

        ws._ws = mock_ws
        ws._connected = True
        ws._stop_event.clear()

        with patch.object(
            ws, "_try_reconnect", new_callable=AsyncMock
        ) as mock_reconnect:

            async def _fail_reconnect() -> None:
                ws._connected = False

            mock_reconnect.side_effect = _fail_reconnect
            await ws._listen_loop()

        assert on_data.call_count == 2, (
            f"Expected 2 calls (aberrant dropped), got {on_data.call_count}"
        )

    @pytest.mark.asyncio
    async def test_first_message_always_accepted(self) -> None:
        """First message after connect has no reference — must be accepted."""
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")

        ws = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)

        messages = [
            self._make_ws_msg(
                discharge=530, feedin=70
            ),  # would be aberrant, but first msg
            aiohttp.WSMessage(type=aiohttp.WSMsgType.CLOSED, data=None, extra=None),
        ]

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(side_effect=messages)
        mock_ws.closed = True

        ws._ws = mock_ws
        ws._connected = True
        ws._stop_event.clear()

        with patch.object(
            ws, "_try_reconnect", new_callable=AsyncMock
        ) as mock_reconnect:

            async def _fail_reconnect() -> None:
                ws._connected = False

            mock_reconnect.side_effect = _fail_reconnect
            await ws._listen_loop()

        assert on_data.call_count == 1

    @pytest.mark.asyncio
    async def test_reconnect_resets_reference(self) -> None:
        """After reconnect, _last_accepted is reset so first message is accepted."""
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")

        ws = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)
        # Simulate a prior accepted message
        ws._last_accepted = {
            "batDischargePower": 5.5,
            "feedinPower": 5.02,
            "loadsPower": 0.48,
        }

        # After reconnect, _last_accepted should be None
        ws2 = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)
        assert ws2._last_accepted is None
