"""Tests for FoxESS WebSocket real-time data mapping and password hashing."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

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

    def test_grid_balance_unreliable_unmeasured_generation(self) -> None:
        """External generation not visible to FoxESS skews the balance.

        Balance predicts import 0.28 kW but actual grid is 1.31 kW —
        the 3x+ divergence triggers fallback to gridStatus which correctly
        reports export (gridStatus=2).  Reproduces GitHub issue #3.
        """
        msg = self._make_msg(
            solar={"power": {"value": "0"}},
            load={"power": {"value": "280"}},
            bat={"power": {"value": "5"}, "soc": 100, "charge": 1},
            grid={"power": {"value": "1310"}, "gridStatus": 2},
        )
        data = map_ws_to_coordinator(msg)
        # Balance: 0.28 + 0.005 - 0 - 0 = 0.285 (predicts import)
        # Actual grid: 1.31 kW — ratio 4.6x → balance unreliable
        # gridStatus=2 → exporting
        assert data["gridConsumptionPower"] == 0.0
        assert data["feedinPower"] == pytest.approx(1.31)

    def test_grid_balance_unreliable_importing(self) -> None:
        """Divergent balance falls back to gridStatus=3 → importing."""
        msg = self._make_msg(
            solar={"power": {"value": "5000"}},
            load={"power": {"value": "200"}},
            bat={"power": {"value": "0"}, "soc": 100, "charge": 0},
            grid={"power": {"value": "800"}, "gridStatus": "3"},
        )
        data = map_ws_to_coordinator(msg)
        # Balance: 0.2 + 0 - 0 - 5.0 = -4.8 (predicts export)
        # Actual grid: 0.8 kW — ratio 6x → balance unreliable
        # gridStatus=3 → importing
        assert data["gridConsumptionPower"] == pytest.approx(0.8)
        assert data["feedinPower"] == 0.0

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


class TestSustainedTransitionAccepted:
    """Regression: a SUSTAINED legitimate power transition (idle→charge,
    charge→idle, discharge start) must be accepted, while a TRANSIENT
    single-frame glitch is still rejected.

    Live symptom (2026-06-15, UTC; smart charge start)::

        01:02:33 WS batChargePower diverges >10x: candidate=10.0000,
                 last_accepted=0.3590 — dropping anomalous message
        01:02:37 ... candidate=9.9500, last_accepted=0.3590 — dropping
        (repeats every ~5s until 01:05:02 when a REST poll re-anchors
         last_accepted to a charging-scale value)

    The idle reference (batChargePower≈0.359 kW) is above the 0.1 kW
    near-zero floor, so the ramp-up exemption does not apply, and every
    charging-scale frame (~10 kW, a 27× jump) is dropped by the bare
    single-frame >10× guard.  With no WS frame accepted, nothing is
    injected — the ``data_freshness`` badge sticks on ``api`` and the WS
    goes stale→disconnect→reconnect every ~30s until a REST poll
    re-anchors the reference.

    The fix must distinguish a *sustained* new regime (corroborated by
    consecutive frames) from a *transient* glitch (one frame, then back
    to the old regime).  Both directions and all power keys.
    """

    # ----- frame-processing path: the public method the listen loop calls
    # to decide whether a mapped frame is forwarded.  ``_process_mapped_frame``
    # owns the corroboration state (pending divergent regime) and the
    # ``_last_accepted`` update — the same logic the listen loop runs at its
    # ~line 401 call site.  These tests drive that real path with a SEQUENCE
    # of frames. -----

    @staticmethod
    def _run_sequence(
        ws: FoxESSRealtimeWS, frames: list[dict[str, float]]
    ) -> list[dict[str, float]]:
        """Feed mapped frames through the public frame-processing path and
        return the list of frames that would be injected (reach on_data)."""
        injected: list[dict[str, float]] = []
        for frame in frames:
            out = ws._process_mapped_frame(frame)
            if out is not None:
                injected.append(out)
        return injected

    @staticmethod
    def _ws() -> FoxESSRealtimeWS:
        return FoxESSRealtimeWS("plant1", AsyncMock(), AsyncMock(), MagicMock())

    @pytest.mark.asyncio
    async def test_idle_to_charge_sustained_accepted(self) -> None:
        """idle batChargePower≈0.36 → sustained ≈10 kW must be injected."""
        ws = self._ws()
        ws._last_accepted = {"batChargePower": 0.359, "batDischargePower": 0.0}
        frames = [
            {"batChargePower": 10.0, "batDischargePower": 0.0},
            {"batChargePower": 9.95, "batDischargePower": 0.0},
            {"batChargePower": 10.02, "batDischargePower": 0.0},
        ]
        injected = self._run_sequence(ws, frames)
        # The sustained charging regime must reach the coordinator — the
        # bug drops ALL of these (0 injected).
        assert len(injected) >= 2, (
            f"sustained idle→charge transition dropped: only {len(injected)} "
            f"of {len(frames)} charging frames injected (symptom: every "
            f"charging frame rejected, nothing injected)"
        )
        assert injected[-1]["batChargePower"] == 10.02
        assert ws._last_accepted["batChargePower"] == 10.02

    @pytest.mark.asyncio
    async def test_charge_to_idle_sustained_accepted(self) -> None:
        """≈10 kW → sustained idle ≈0.36 kW (inverse) must be injected."""
        ws = self._ws()
        ws._last_accepted = {"batChargePower": 10.0, "batDischargePower": 0.0}
        frames = [
            {"batChargePower": 0.36, "batDischargePower": 0.0},
            {"batChargePower": 0.35, "batDischargePower": 0.0},
            {"batChargePower": 0.37, "batDischargePower": 0.0},
        ]
        injected = self._run_sequence(ws, frames)
        assert len(injected) >= 2, (
            f"sustained charge→idle transition dropped: only {len(injected)} injected"
        )
        assert ws._last_accepted["batChargePower"] == 0.37

    @pytest.mark.asyncio
    async def test_discharge_start_sustained_accepted(self) -> None:
        """batDischargePower idle→high sustained must be injected."""
        ws = self._ws()
        ws._last_accepted = {"batChargePower": 0.0, "batDischargePower": 0.3}
        frames = [
            {"batChargePower": 0.0, "batDischargePower": 5.5},
            {"batChargePower": 0.0, "batDischargePower": 5.4},
            {"batChargePower": 0.0, "batDischargePower": 5.6},
        ]
        injected = self._run_sequence(ws, frames)
        assert len(injected) >= 2, (
            f"sustained discharge start dropped: only {len(injected)} injected"
        )
        assert ws._last_accepted["batDischargePower"] == 5.6

    @pytest.mark.asyncio
    async def test_transient_single_frame_glitch_still_rejected(self) -> None:
        """The filter's real job: a TRUE one-off glitch (steady ≈10 kW, ONE
        frame at 0.8 kW, then back to ≈10 kW) must be REJECTED.

        This is the 2026-04-27 incident the filter was built for — a lone
        anomalous frame must NOT corrupt displayed power.
        """
        ws = self._ws()
        ws._last_accepted = {"batChargePower": 10.0, "batDischargePower": 0.0}
        frames = [
            {"batChargePower": 9.9, "batDischargePower": 0.0},  # normal
            {"batChargePower": 0.8, "batDischargePower": 0.0},  # GLITCH
            {"batChargePower": 10.1, "batDischargePower": 0.0},  # back to normal
            {"batChargePower": 9.95, "batDischargePower": 0.0},  # normal
        ]
        injected = self._run_sequence(ws, frames)
        glitch_values = [f["batChargePower"] for f in injected]
        assert 0.8 not in glitch_values, (
            f"transient single-frame glitch leaked through: {glitch_values}"
        )
        # The three normal frames are all injected.
        assert len(injected) == 3, f"normal frames dropped: {glitch_values}"

    @pytest.mark.asyncio
    async def test_exactly_10x_accepted(self) -> None:
        """Boundary: exactly 10× is accepted (strict > 10)."""
        ws = self._ws()
        ws._last_accepted = {"batChargePower": 1.0, "batDischargePower": 0.0}
        out = ws._process_mapped_frame(
            {"batChargePower": 10.0, "batDischargePower": 0.0}
        )
        assert out is not None
        assert ws._last_accepted["batChargePower"] == 10.0

    @pytest.mark.asyncio
    async def test_zero_candidate_genuine_stop_accepted(self) -> None:
        """candidate batChargePower==0 (genuine stop) always accepted."""
        ws = self._ws()
        ws._last_accepted = {"batChargePower": 10.0, "batDischargePower": 0.0}
        out = ws._process_mapped_frame(
            {"batChargePower": 0.0, "batDischargePower": 0.0}
        )
        assert out is not None
        assert ws._last_accepted["batChargePower"] == 0.0

    # ----- integration level: drive the real listen loop -----

    @staticmethod
    def _charge_msg(charge_w: float, soc: int = 50) -> aiohttp.WSMessage:
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
                                "power": {"value": str(charge_w)},
                                "gridStatus": 3,
                            },
                            "bat": {
                                "power": {"value": str(charge_w)},
                                "soc": soc,
                                "charge": 1,
                            },
                            "load": {"power": {"value": "0"}},
                        },
                        "timeDiff": 5,
                    },
                }
            ),
            extra=None,
        )

    @pytest.mark.asyncio
    async def test_listen_loop_injects_sustained_charge_transition(self) -> None:
        """Through the REAL listen loop: an idle→charge transition where the
        first accepted frame is idle-scale, then sustained ~10 kW frames,
        must reach on_data — not be black-holed.
        """
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")

        ws = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)

        messages = [
            self._charge_msg(359),  # idle-scale — first frame, accepted
            self._charge_msg(10000),  # charge start — diverges 27× from idle
            self._charge_msg(9950),  # sustained charging
            self._charge_msg(10020),  # sustained charging
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

        # The idle frame plus the sustained charging regime must reach
        # on_data.  The bug forwards only the idle frame (1 call) and
        # drops every ~10 kW charging frame.
        injected_charge = [
            c.args[0]["batChargePower"]
            for c in on_data.call_args_list
            if c.args[0].get("batChargePower", 0) > 5
        ]
        assert len(injected_charge) >= 2, (
            f"listen loop black-holed the charge transition: only "
            f"{on_data.call_count} total frames injected, charging frames "
            f"forwarded={injected_charge} (symptom: badge stuck on api)"
        )


class TestReconnectRespectsGate:
    """Regression (FOURTH, distinct from the three start-gate fixes): the
    autonomous reconnect loop must NOT resurrect the connection when the
    WS should be DOWN (no active session).

    Live evidence (2026-06-07, UTC; ``ws_mode=smart_sessions``, confirmed
    idle — ``intelligente_steuerung=idle``, both laden/entladen off,
    ``betriebsmodus=SelfUse``), straight from the rolling debug log::

        10:20:30  WebSocket connected
        10:20:30  WebSocket: skipping stale message (timeDiff=61)
        10:25:59  WebSocket stale (no data in 30s)
        10:25:59  WebSocket disconnected, falling back to REST polling
        10:25:59  WebSocket reconnecting in 6.0s (attempt 1/5)
        10:26:06  WebSocket connected
        10:26:06  WebSocket: skipping stale message (timeDiff=61)

    A self-perpetuating connect → stale(30s) → disconnect → reconnect(6s)
    cycle running with NO active session, every ~5.5 min — the
    ``data_freshness`` ws↔api sawtooth.  Every frame is ``timeDiff=61`` →
    discarded per C-005, so the WS delivers NOTHING useful: pure churn
    (C-020 leak).

    Root cause: ``_try_reconnect`` is fully autonomous — it gates only on
    the instance-local ``_no_reconnect`` / ``_stop_event`` flags.  It never
    consults ``_should_start_realtime_ws``.  The three prior fixes all live
    in the start chokepoint (``_should_start_realtime_ws`` /
    ``_maybe_start_realtime_ws``), which the reconnect loop never calls —
    so the gate's correct "WS should be DOWN" answer never reaches the
    reconnect decision, and the loop revives the connection 6s later,
    below the coordinator's/start-gate's visibility.

    The unified reconcile: ``FoxESSRealtimeWS`` takes a
    ``should_reconnect`` predicate (wired to ``_should_start_realtime_ws``).
    ``_try_reconnect`` consults it before scheduling any network I/O.
    C-020 (UI reflects true state), C-025 (boundary cleanliness),
    C-005 (stale-discard context), D-008/D-009 (reconnect/linger contract).
    """

    @staticmethod
    def _make_ws(
        should_reconnect: Callable[[], bool] | None,
    ) -> FoxESSRealtimeWS:
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")
        # The reconcile predicate wired to the (mocked) start-gate.  The
        # test exercises the *behaviour* of ``_try_reconnect`` — does it
        # consult the gate before scheduling reconnect I/O?  On
        # pre-fix code the reconnect loop ignores the predicate and
        # reconnects unconditionally, so the gate-False behavioural
        # assertions fail, matching the live idle-reconnect symptom.
        return FoxESSRealtimeWS(
            "plant1",
            web_session,
            on_data,
            on_disconnect,
            should_reconnect=should_reconnect,
        )

    @pytest.mark.asyncio
    async def test_no_reconnect_when_gate_false(self) -> None:
        """Gate False (idle) → _try_reconnect must NOT reconnect.

        Drives the real ``_try_reconnect`` (not a mock) with the
        predicate returning False, exactly as it would during confirmed
        idle.  No ``_do_connect`` may be scheduled, no backoff sleep, and
        the connection must end up DOWN.  On current ``develop`` the
        reconnect loop has no gate awareness and reconnects anyway — this
        test FAILS, matching the live symptom.
        """
        gate_calls = {"n": 0}

        def gate_false() -> bool:
            gate_calls["n"] += 1
            return False

        ws = self._make_ws(gate_false)
        ws._connected = True
        ws._stop_event.clear()

        connect_attempts = {"n": 0}

        async def _count_connect(_token: str) -> None:
            connect_attempts["n"] += 1
            ws._connected = True

        with (
            patch.object(ws, "_do_connect", side_effect=_count_connect),
            patch.object(ws, "_close_ws", new_callable=AsyncMock),
            patch(
                "custom_components.foxess_control.foxess.realtime_ws.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            await ws._try_reconnect()

        assert gate_calls["n"] > 0, (
            "_try_reconnect never consulted the should_reconnect gate — "
            "the reconnect loop is still autonomous (the leak)."
        )
        assert connect_attempts["n"] == 0, (
            "_try_reconnect re-established the WS while the gate said it "
            "should be DOWN (no active session) — the self-perpetuating "
            "idle reconnect cycle (C-020 leak)."
        )
        assert mock_sleep.await_count == 0, (
            "_try_reconnect scheduled a backoff sleep despite the gate "
            "refusing — it should short-circuit before any backoff."
        )
        assert ws._connected is False, "WS must be DOWN when the gate is False."

    @pytest.mark.asyncio
    async def test_reconnect_when_gate_true(self) -> None:
        """Inverse (must NOT regress): active session → reconnect DOES happen.

        During a genuine active session the gate returns True; a
        stale/dropped WS must still reconnect (D-008/D-009 — the
        legitimate reconnect case).
        """
        ws = self._make_ws(lambda: True)
        ws._connected = True
        ws._stop_event.clear()

        connect_attempts = {"n": 0}

        async def _count_connect(_token: str) -> None:
            connect_attempts["n"] += 1
            ws._connected = True

        with (
            patch.object(ws, "_do_connect", side_effect=_count_connect),
            patch.object(ws, "_close_ws", new_callable=AsyncMock),
            patch(
                "custom_components.foxess_control.foxess.realtime_ws.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await ws._try_reconnect()

        assert connect_attempts["n"] == 1, (
            "Active session (gate True): a stale/dropped WS must reconnect "
            "— the legitimate reconnect case was broken."
        )
        assert ws._connected is True

    @pytest.mark.asyncio
    async def test_no_gate_defaults_to_reconnect(self) -> None:
        """Back-compat: no predicate supplied → reconnect as before.

        ``always`` mode and existing call sites that pass no predicate
        must retain the legacy autonomous-reconnect behaviour.
        """
        ws = self._make_ws(None)
        ws._connected = True
        ws._stop_event.clear()

        connect_attempts = {"n": 0}

        async def _count_connect(_token: str) -> None:
            connect_attempts["n"] += 1
            ws._connected = True

        with (
            patch.object(ws, "_do_connect", side_effect=_count_connect),
            patch.object(ws, "_close_ws", new_callable=AsyncMock),
            patch(
                "custom_components.foxess_control.foxess.realtime_ws.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await ws._try_reconnect()

        assert connect_attempts["n"] == 1
