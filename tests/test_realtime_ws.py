"""Tests for FoxESS WebSocket real-time data mapping and password hashing."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from custom_components.foxess_control.foxess.realtime_ws import (
    FoxESSRealtimeWS,
    GridDirectionTracker,
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
# AC-coupled additional generation via the WS ``aux`` node (issue #18)
# ---------------------------------------------------------------------------


class TestAuxAdditionalGeneration:
    """The WS ``aux`` node carries AC-coupled generation.

    Same quantity as ``meterPower2`` over REST and "Gen Load" in the
    native FoxESS app: a second inverter's output, measured on an
    auxiliary meter channel, which never appears in the FoxESS
    ``solar``/``pvPower`` reading because it is not wired into the
    FoxESS PV strings.

    Opt-in only.  ``aux`` is folded in solely for users who configured
    ``additional_pv_power_variable``; on a DC-coupled system ``aux`` may
    carry something else entirely, so an unconfigured user must see
    byte-identical behaviour.

    Units follow C-004: the WS sends watts as strings unless the power
    object's ``unit`` field says ``kW``.
    """

    # julianjwong's readings from issue #18, one moment in time:
    #   native PV strings   0 W      (nothing on the FoxESS DC inputs)
    #   house load          1.80 kW
    #   AC-coupled Gen Load 3.32 kW
    #   battery             100%, 0 W
    #   native FoxESS app   EXPORTING 1.52 kW   (3.32 - 1.80 = 1.52)
    @staticmethod
    def _julian_msg(aux: object | None = None) -> dict[str, Any]:
        node: dict[str, Any] = {
            "solar": {"power": {"value": "0", "unit": "W"}},
            "load": {"power": {"value": "1800", "unit": "W"}},
            "bat": {"power": {"value": "0", "unit": "W"}, "soc": 100, "charge": 0},
            "grid": {"power": {"value": "1520", "unit": "W"}, "gridStatus": 2},
        }
        if aux is not None:
            node["aux"] = aux
        return {"errno": 0, "result": {"node": node, "timeDiff": 5}}

    # -- opt-in gate ------------------------------------------------------

    def test_aux_ignored_when_not_configured(self) -> None:
        """Unconfigured: an ``aux`` node must change nothing at all.

        Asserts full dict equality against the same frame with no ``aux``
        node, so any leaked term or marker key fails the test.
        """
        with_aux = map_ws_to_coordinator(
            self._julian_msg({"power": {"value": "3320", "unit": "W"}})
        )
        baseline = map_ws_to_coordinator(self._julian_msg())
        assert with_aux == baseline
        assert with_aux["pvPower"] == 0.0
        assert "_additional_pv_kw" not in with_aux

    def test_aux_added_to_pvpower_when_configured(self) -> None:
        msg = self._julian_msg({"power": {"value": "3320", "unit": "W"}})
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["pvPower"] == pytest.approx(3.32)

    # -- units (C-004) ----------------------------------------------------

    def test_aux_watts_string_divided_by_1000(self) -> None:
        """C-004: watts as a string, no ``unit`` field → divide by 1000."""
        msg = self._julian_msg({"power": {"value": "3320"}})
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["pvPower"] == pytest.approx(3.32)
        assert data["_additional_pv_kw"] == pytest.approx(3.32)

    def test_aux_explicit_watts_unit_divided_by_1000(self) -> None:
        msg = self._julian_msg({"power": {"value": "3320", "unit": "W"}})
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["_additional_pv_kw"] == pytest.approx(3.32)

    def test_aux_kw_unit_used_as_is(self) -> None:
        """C-004: ``unit`` of kW means the value is already kW."""
        msg = self._julian_msg({"power": {"value": "3.32", "unit": "kW"}})
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["pvPower"] == pytest.approx(3.32)
        assert data["_additional_pv_kw"] == pytest.approx(3.32)

    def test_aux_numeric_not_string(self) -> None:
        """Value may arrive as a JSON number rather than a string."""
        msg = self._julian_msg({"power": {"value": 3320, "unit": "W"}})
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["pvPower"] == pytest.approx(3.32)

    # -- live value preferred, REST value as fallback ---------------------

    def test_aux_absent_falls_back_to_rest_value(self) -> None:
        """No ``aux`` in the frame → keep the REST-derived term, not zero.

        Dropping it would leave the C-006 balance blind to AC-coupled
        generation and make the sign inference worse, not better.
        """
        data = map_ws_to_coordinator(
            self._julian_msg(),
            additional_pv_enabled=True,
            additional_pv_fallback_kw=3.32,
        )
        assert data["pvPower"] == pytest.approx(3.32)
        assert data["_additional_pv_kw"] == pytest.approx(3.32)

    def test_live_aux_preferred_over_stale_rest_value(self) -> None:
        """A live ``aux`` reading wins over the up-to-5-minute-old REST one."""
        msg = self._julian_msg({"power": {"value": "3320", "unit": "W"}})
        data = map_ws_to_coordinator(
            msg, additional_pv_enabled=True, additional_pv_fallback_kw=0.5
        )
        assert data["pvPower"] == pytest.approx(3.32)
        assert data["_additional_pv_kw"] == pytest.approx(3.32)

    def test_live_zero_aux_beats_stale_nonzero(self) -> None:
        """A live 0 W reading is authoritative — do not re-add a stale value.

        Sunset case: the AC-coupled inverter has stopped, but the last
        REST poll still holds 3.32 kW.  The live zero must win.
        """
        msg = self._julian_msg({"power": {"value": "0", "unit": "W"}})
        data = map_ws_to_coordinator(
            msg, additional_pv_enabled=True, additional_pv_fallback_kw=3.32
        )
        assert data["pvPower"] == 0.0
        assert data["_additional_pv_kw"] == 0.0

    def test_aux_non_numeric_falls_back(self) -> None:
        msg = self._julian_msg({"power": {"value": "N/A", "unit": "W"}})
        data = map_ws_to_coordinator(
            msg, additional_pv_enabled=True, additional_pv_fallback_kw=3.32
        )
        assert data["pvPower"] == pytest.approx(3.32)

    def test_aux_placeholder_dashes_falls_back(self) -> None:
        """The cloud sends ``"--"`` for a channel with no reading.

        Must fall back to the REST value rather than contributing zero
        and flipping the balance back into the issue-#18 failure mode.
        """
        msg = self._julian_msg({"power": {"value": "--", "unit": "W"}})
        data = map_ws_to_coordinator(
            msg, additional_pv_enabled=True, additional_pv_fallback_kw=3.32
        )
        assert data["pvPower"] == pytest.approx(3.32)
        assert data["feedinPower"] == pytest.approx(1.52)

    def test_aux_empty_node_falls_back(self) -> None:
        data = map_ws_to_coordinator(
            self._julian_msg({}),
            additional_pv_enabled=True,
            additional_pv_fallback_kw=3.32,
        )
        assert data["pvPower"] == pytest.approx(3.32)

    def test_aux_as_bare_power_object(self) -> None:
        """Tolerate ``aux`` being the power object itself, not a node.

        Every other observed node nests the reading under ``power``
        (``solar``, ``load``, ``grid``, ``bat``, ``device``), so that is
        the primary shape; this guards the other plausible one rather
        than silently dropping the term.
        """
        msg = self._julian_msg({"value": "3320", "unit": "W"})
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["pvPower"] == pytest.approx(3.32)

    def test_no_pvpower_in_frame_means_no_term(self) -> None:
        """Without a WS solar reading there is nothing to add the term to."""
        msg = {
            "errno": 0,
            "result": {
                "node": {
                    "grid": {"power": {"value": "1520"}, "gridStatus": 2},
                    "aux": {"power": {"value": "3320"}},
                },
                "timeDiff": 5,
            },
        }
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert "pvPower" not in data
        assert "_additional_pv_kw" not in data

    # -- the reported symptom: grid direction (C-006) ---------------------

    def test_grid_direction_is_export_with_live_aux(self) -> None:
        """Issue #18 at the mapper: julianjwong's frame must read export.

        Balance with the AC-coupled term visible (kW):
        1.80 load + 0 charge - 0 discharge - 3.32 solar = -1.52 → export,
        and |−1.52| matches the 1.52 kW grid reading exactly, so the
        balance is judged reliable and gridStatus is not needed.
        """
        msg = self._julian_msg({"power": {"value": "3320", "unit": "W"}})
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["feedinPower"] == pytest.approx(1.52)
        assert data["gridConsumptionPower"] == 0.0

    def test_export_sign_also_correct_from_rest_fallback(self) -> None:
        """The balance must see the term even when it comes from REST.

        Before the fix the coordinator added the REST term *after*
        ``map_ws_to_coordinator`` had already decided the sign, so the
        balance never saw it at all — stale or not.
        """
        data = map_ws_to_coordinator(
            self._julian_msg(),
            additional_pv_enabled=True,
            additional_pv_fallback_kw=3.32,
        )
        assert data["feedinPower"] == pytest.approx(1.52)
        assert data["gridConsumptionPower"] == 0.0

    def test_without_the_term_the_balance_gets_the_sign_wrong(self) -> None:
        """Documents *why* issue #18 happens, and pins the blame on the input.

        With no AC-coupled term available at all, the C-006 balance
        predicts import of 1.80 kW.  The actual grid reading is 1.52 kW,
        a ratio of only 1.18 — comfortably inside the 3x reliability
        window — so the ``gridStatus`` fallback (which says export) is
        never consulted and the site is reported as importing.

        This must stay true after the fix: the correction has to come
        from feeding the balance the AC-coupled term, not from enabling
        a flag.
        """
        data = map_ws_to_coordinator(self._julian_msg(), additional_pv_enabled=True)
        assert data["gridConsumptionPower"] == pytest.approx(1.52)
        assert data["feedinPower"] == 0.0

    def test_zero_term_changes_no_power_value(self) -> None:
        """Enabled but with nothing to add: every power value is unchanged.

        A DC-coupled frame carries no ``aux`` node (the real capture in
        ``test_real_world_sample`` has keys solar/grid/bat/load/device/
        charger/heatpump), so opting in on such a system — or before the
        first REST poll has produced a fallback — must leave the numbers
        exactly as they were.  Only the bookkeeping marker differs.
        """
        msg = self._julian_msg()
        baseline = map_ws_to_coordinator(msg)
        enabled = map_ws_to_coordinator(
            msg, additional_pv_enabled=True, additional_pv_fallback_kw=0.0
        )
        assert enabled.pop("_additional_pv_kw") == 0.0
        assert enabled == baseline


class TestAuxWiredThroughListenLoop:
    """The live ``aux`` term must reach the coordinator, not just the mapper."""

    @staticmethod
    def _ws_msg(aux_w: str | None = "3320") -> aiohttp.WSMessage:
        import json

        node: dict[str, Any] = {
            "solar": {"power": {"value": "0", "unit": "W"}},
            "load": {"power": {"value": "1800", "unit": "W"}},
            "bat": {"power": {"value": "0", "unit": "W"}, "soc": 100, "charge": 0},
            "grid": {"power": {"value": "1520", "unit": "W"}, "gridStatus": 2},
        }
        if aux_w is not None:
            node["aux"] = {"power": {"value": aux_w, "unit": "W"}}
        return aiohttp.WSMessage(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps({"errno": 0, "result": {"node": node, "timeDiff": 5}}),
            extra=None,
        )

    async def _run_one_frame(
        self,
        msg: aiohttp.WSMessage,
        additional_pv: Any = None,
    ) -> dict[str, Any]:
        on_data = AsyncMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")
        ws = FoxESSRealtimeWS(
            "plant1",
            web_session,
            on_data,
            MagicMock(),
            additional_pv=additional_pv,
        )
        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(
            side_effect=[
                msg,
                aiohttp.WSMessage(type=aiohttp.WSMsgType.CLOSED, data=None, extra=None),
            ]
        )
        mock_ws.closed = True
        ws._ws = mock_ws
        ws._connected = True
        ws._stop_event.clear()

        with patch.object(ws, "_try_reconnect", new_callable=AsyncMock) as reconnect:

            async def _fail_reconnect() -> None:
                ws._connected = False

            reconnect.side_effect = _fail_reconnect
            await ws._listen_loop()

        assert on_data.call_count == 1
        forwarded: dict[str, Any] = on_data.call_args.args[0]
        return forwarded

    @pytest.mark.asyncio
    async def test_configured_provider_feeds_live_aux_through(self) -> None:
        forwarded = await self._run_one_frame(
            self._ws_msg(), additional_pv=lambda: (True, 0.0)
        )
        assert forwarded["pvPower"] == pytest.approx(3.32)
        assert forwarded["feedinPower"] == pytest.approx(1.52)
        assert forwarded["gridConsumptionPower"] == 0.0

    @pytest.mark.asyncio
    async def test_no_provider_leaves_frame_untouched(self) -> None:
        """Default construction (e.g. the config-flow probe) opts out."""
        forwarded = await self._run_one_frame(self._ws_msg())
        assert forwarded["pvPower"] == 0.0
        assert "_additional_pv_kw" not in forwarded

    @pytest.mark.asyncio
    async def test_provider_fallback_used_when_frame_has_no_aux(self) -> None:
        forwarded = await self._run_one_frame(
            self._ws_msg(aux_w=None), additional_pv=lambda: (True, 3.32)
        )
        assert forwarded["pvPower"] == pytest.approx(3.32)
        assert forwarded["feedinPower"] == pytest.approx(1.52)

    @pytest.mark.asyncio
    async def test_provider_failure_does_not_break_the_stream(self) -> None:
        """A raising provider must degrade to "not configured", not drop frames."""

        def _boom() -> tuple[bool, float]:
            raise RuntimeError("config unavailable during startup")

        forwarded = await self._run_one_frame(self._ws_msg(), additional_pv=_boom)
        assert forwarded["pvPower"] == 0.0


# ---------------------------------------------------------------------------
# Grid direction with an unmeasured / separately-reported EV charger
# (live capture 2026-10-27, EV on the house side of the inverter, gridStatus
# reads 2 for IMPORT on this firmware but 2 means EXPORT in GH issue #3)
# ---------------------------------------------------------------------------


def _ev_msg(
    *,
    solar: float,
    load: float,
    bat_charge: float,
    grid: float,
    charger: float | None = 0.0,
    grid_status: object = 2,
    grid_flag: object = 1,
    device: float | None = None,
) -> dict[str, object]:
    """Build a WS frame shaped like the live capture (all values in kW)."""

    def p(kw: float) -> dict[str, str]:
        return {"value": f"{kw}", "unit": "kW"}

    grid_node: dict[str, object] = {"power": p(grid), "gridStatus": grid_status}
    if grid_flag is not None:
        grid_node["gridToHidden"] = grid_flag
    node: dict[str, object] = {
        "solar": {"power": p(solar)},
        "grid": grid_node,
        "bat": {
            "power": p(bat_charge),
            "soc": 50,
            "charge": 1,
            "batteryId": "00000000-0000-0000-0000-000000000000",
            "multipleBatterySoc": [{"soc": 50, "charge": 1, "batSn": "SN-PLACEHOLDER"}],
        },
        "load": {"power": p(load)},
        "homeLoad": {"power": p(load)},
        "heatpump": {"display": False, "power": {"value": "--", "unit": "kW"}},
        "aux": {"display": False, "power": {"value": "--", "unit": "kW"}},
    }
    if charger is not None:
        node["charger"] = {"power": p(charger), "state": 0, "chargerToHidden": 1}
    if device is not None:
        node["device"] = {"power": p(device)}
    return {"errno": 0, "result": {"node": node, "timeDiff": 5}}


# (label, solar, load, charger, bat_charge, device, grid) — verbatim from the log
_LIVE_IMPORT_SAMPLES = [
    ("ev_inside_load", 7.76, 2.99, 0.0, 8.51, -0.744, 3.73),
    ("switchover_charger_2.6_load_4.7", 7.71, 4.72, 2.60, 8.52, -0.811, 8.13),
    ("settled_ev_6.9", 7.73, 0.464, 6.90, 8.52, -0.787, 8.15),
    ("settled_ratio_6.5x", 7.69, 0.386, 6.90, 8.52, -0.822, 8.11),
    ("solar_dip", 2.71, 0.309, 6.90, 7.69, -4.98, 12.2),
    ("solar_recovers", 7.26, 0.454, 6.90, 8.52, -1.26, 8.61),
]


class TestGridDirectionEvCharger:
    @pytest.mark.parametrize(
        ("label", "solar", "load", "charger", "bat", "device", "grid"),
        _LIVE_IMPORT_SAMPLES,
        ids=[s[0] for s in _LIVE_IMPORT_SAMPLES],
    )
    def test_live_import_samples_never_report_export(
        self,
        label: str,
        solar: float,
        load: float,
        charger: float,
        bat: float,
        device: float,
        grid: float,
    ) -> None:
        msg = _ev_msg(
            solar=solar,
            load=load,
            charger=charger,
            bat_charge=bat,
            device=device,
            grid=grid,
        )
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == pytest.approx(grid)
        assert data["feedinPower"] == 0.0

    def test_signed_flag_beats_wrong_gridstatus(self) -> None:
        """11:07:56 — balance without EV is 6.5x off; gridStatus=2 is import here."""
        msg = _ev_msg(solar=7.73, load=0.464, charger=None, bat_charge=8.52, grid=8.15)
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == pytest.approx(8.15)
        assert data["feedinPower"] == 0.0

    def test_live_export_sample_still_exports(self) -> None:
        """11:01:01 — before Force Charge: gridStatus=1, gridToHidden=-1."""
        msg = _ev_msg(
            solar=7.67,
            load=0.476,
            charger=0.0,
            bat_charge=2.19,
            grid=5.01,
            grid_status=1,
            grid_flag=-1,
            device=5.48,
        )
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == 0.0
        assert data["feedinPower"] == pytest.approx(5.01)

    def test_flag_accepts_string_values(self) -> None:
        msg = _ev_msg(
            solar=1.0, load=0.5, bat_charge=0.0, grid=5.0, charger=0.0, grid_flag="1"
        )
        assert map_ws_to_coordinator(msg)["gridConsumptionPower"] == pytest.approx(5.0)
        msg = _ev_msg(
            solar=9.0, load=0.5, bat_charge=0.0, grid=5.0, charger=0.0, grid_flag="-1"
        )
        assert map_ws_to_coordinator(msg)["feedinPower"] == pytest.approx(5.0)

    def test_foxess_app_48x_divergence(self) -> None:
        """App sample: solar 8.82, home 0.455, charge 8.51, EV 6.9, grid 7.05.

        Balance without the EV predicts 0.15 kW vs 7.05 actual (48x).  No
        signed flag in the frame: the charger node must close the balance.
        """
        msg = _ev_msg(
            solar=8.82,
            load=0.455,
            charger=6.9,
            bat_charge=8.51,
            grid=7.05,
            grid_flag=None,
        )
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == pytest.approx(7.05)
        assert data["feedinPower"] == 0.0

    def test_charger_node_does_not_change_reported_house_load(self) -> None:
        msg = _ev_msg(solar=7.73, load=0.464, charger=6.9, bat_charge=8.52, grid=8.15)
        data = map_ws_to_coordinator(msg)
        assert data["loadsPower"] == pytest.approx(0.464)

    def test_charger_placeholder_values_ignored(self) -> None:
        """display=False nodes carry '--' — must not break the balance."""
        msg = _ev_msg(
            solar=2.0, load=1.0, charger=None, bat_charge=0.0, grid=1.0, grid_flag=None
        )
        msg["result"]["node"]["charger"] = {  # type: ignore[index]
            "display": False,
            "power": {"value": "--", "unit": "kW"},
        }
        data = map_ws_to_coordinator(msg)
        assert data["feedinPower"] == pytest.approx(1.0)  # 1.0 - 2.0 → export
        assert data["gridConsumptionPower"] == 0.0


def _charger_placeholder(msg: dict[str, object]) -> dict[str, object]:
    """Replace the charger node by what the WS sends while it has no reading."""
    msg["result"]["node"]["charger"] = {  # type: ignore[index]
        "power": {"value": "--", "unit": "kW"},
        "display": True,
        "state": 0,
        "chargerToHidden": 1,
    }
    return msg


class TestGridDirectionChargerPlaceholder:
    """Live capture 2026-10-28 (Force Charge, EV at 32 A)."""

    def test_ev_inside_load_charger_placeholder(self) -> None:
        """charger.power '--' (EV still counted in load): 5.15+8.50-2.03=11.62."""
        msg = _charger_placeholder(
            _ev_msg(solar=2.03, load=5.15, bat_charge=8.50, grid=11.6, charger=None)
        )
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == pytest.approx(11.6)
        assert data["feedinPower"] == 0.0

    def test_settled_ev_in_charger_node(self) -> None:
        """EV split out: 0.905+6.14+6.90-1.75=12.195 vs grid 12.2."""
        msg = _ev_msg(solar=1.75, load=0.905, bat_charge=6.14, grid=12.2, charger=6.90)
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == pytest.approx(12.2)
        assert data["feedinPower"] == 0.0

    @pytest.mark.parametrize("grid_status", [2, 3])
    def test_no_flag_and_placeholder_decided_by_balance(self, grid_status: int) -> None:
        """No gridToHidden, charger '--' (= 0): the balance must decide.

        gridStatus is deliberately 2 (import on this firmware) and 3 (what the
        legacy fallback treated as export) — neither may influence the result.
        """
        msg = _charger_placeholder(
            _ev_msg(
                solar=2.03,
                load=5.15,
                bat_charge=8.50,
                grid=11.6,
                charger=None,
                grid_status=grid_status,
                grid_flag=None,
            )
        )
        tracker = GridDirectionTracker()
        data = map_ws_to_coordinator(msg, tracker)
        assert data["gridConsumptionPower"] == pytest.approx(11.6)
        assert data["feedinPower"] == 0.0
        # the decision came from the balance, not from the status code
        assert tracker._status_meaning == {str(grid_status): True}


class TestGridDirectionTracker:
    """Direction must survive unmeasured load/generation (no flag in frame)."""

    @staticmethod
    def _bare(
        *, solar: float, load: float, bat: float, grid: float, status: object
    ) -> dict[str, object]:
        return _ev_msg(
            solar=solar,
            load=load,
            charger=None,
            bat_charge=bat,
            grid=grid,
            grid_status=status,
            grid_flag=None,
        )

    @pytest.mark.parametrize("ev_kw", [4.0, 5.0, 6.0, 7.0, 8.0])
    def test_variable_unmeasured_load_holds_import(self, ev_kw: float) -> None:
        """5-min averages 11:15: solar 7.7, load 2.7, charge 8.5, real grid 10.3.

        The EV is invisible (no charger node, no flag).  Whatever the
        4-8 kW EV draw is, the ratio to the balance hovers around the 3x
        cutoff — direction must stay import.
        """
        tracker = GridDirectionTracker()
        # A trustworthy frame first (balance matches): learns import / status 2.
        first = map_ws_to_coordinator(
            self._bare(solar=2.0, load=1.0, bat=8.5, grid=7.5, status=2), tracker
        )
        assert first["gridConsumptionPower"] == pytest.approx(7.5)
        grid = 3.5 + ev_kw  # balance (3.5) + unmeasured EV
        data = map_ws_to_coordinator(
            self._bare(solar=7.7, load=2.7, bat=8.5, grid=grid, status=2), tracker
        )
        assert data["gridConsumptionPower"] == pytest.approx(grid)
        assert data["feedinPower"] == 0.0

    def test_ratio_hovering_around_3x_does_not_flip(self) -> None:
        tracker = GridDirectionTracker()
        map_ws_to_coordinator(
            self._bare(solar=2.0, load=1.0, bat=8.5, grid=7.5, status=2), tracker
        )
        # predicted 3.5 vs actual 10.3 (2.9x), 11.1 vs 3.3 (3.4x), 8.9 vs 4.8
        for solar, load, bat, grid in [
            (7.7, 2.7, 8.5, 10.3),
            (5.2, 0.9, 7.6, 11.1),
            (4.4, 0.7, 8.5, 8.9),
            (7.7, 2.7, 8.5, 11.8),
            (7.7, 2.7, 8.5, 10.4),
        ]:
            data = map_ws_to_coordinator(
                self._bare(solar=solar, load=load, bat=bat, grid=grid, status=2),
                tracker,
            )
            assert data["gridConsumptionPower"] == pytest.approx(grid)
            assert data["feedinPower"] == 0.0

    def test_48x_divergence_holds_previous_direction(self) -> None:
        tracker = GridDirectionTracker()
        map_ws_to_coordinator(
            self._bare(solar=2.0, load=1.0, bat=8.5, grid=7.5, status=2), tracker
        )
        data = map_ws_to_coordinator(
            self._bare(solar=8.82, load=0.455, bat=8.51, grid=7.05, status=2), tracker
        )
        assert data["gridConsumptionPower"] == pytest.approx(7.05)
        assert data["feedinPower"] == 0.0

    def test_unknown_gridstatus_holds_previous_direction(self) -> None:
        tracker = GridDirectionTracker()
        map_ws_to_coordinator(
            self._bare(solar=2.0, load=1.0, bat=8.5, grid=7.5, status=2), tracker
        )
        data = map_ws_to_coordinator(
            self._bare(solar=7.7, load=0.5, bat=8.5, grid=8.0, status=99), tracker
        )
        assert data["gridConsumptionPower"] == pytest.approx(8.0)

    def test_gridstatus_meaning_is_learned_per_firmware(self) -> None:
        """Same gridStatus=2 means export on the issue #3 firmware."""
        tracker = GridDirectionTracker()
        # Reliable export frame teaches status 2 == export.
        map_ws_to_coordinator(
            self._bare(solar=5.0, load=1.0, bat=0.0, grid=4.0, status=2), tracker
        )
        # Then an import frame with unmeasured load: status 2 -> export would
        # be wrong only if learned wrongly; here the learned meaning is used.
        data = map_ws_to_coordinator(
            self._bare(solar=0.0, load=0.28, bat=0.005, grid=1.31, status=2), tracker
        )
        assert data["gridConsumptionPower"] == 0.0
        assert data["feedinPower"] == pytest.approx(1.31)

    def test_issue_3_hold_after_export(self) -> None:
        """GH #3: unmeasured generation; status 99 must not flip direction."""
        tracker = GridDirectionTracker()
        map_ws_to_coordinator(
            self._bare(solar=5.0, load=1.0, bat=0.0, grid=4.0, status=1), tracker
        )
        data = map_ws_to_coordinator(
            self._bare(solar=0.0, load=0.28, bat=0.005, grid=1.31, status=99),
            tracker,
        )
        assert data["gridConsumptionPower"] == 0.0
        assert data["feedinPower"] == pytest.approx(1.31)

    def test_ambiguous_gridstatus_is_not_trusted(self) -> None:
        tracker = GridDirectionTracker()
        map_ws_to_coordinator(
            self._bare(solar=5.0, load=1.0, bat=0.0, grid=4.0, status=2), tracker
        )  # status 2 -> export
        map_ws_to_coordinator(
            self._bare(solar=1.0, load=5.0, bat=0.0, grid=4.0, status=2), tracker
        )  # status 2 -> import (now ambiguous; last direction = import)
        data = map_ws_to_coordinator(
            self._bare(solar=7.7, load=0.5, bat=8.5, grid=8.0, status=2), tracker
        )
        assert data["gridConsumptionPower"] == pytest.approx(8.0)  # held import

    def test_near_zero_grid_does_not_overwrite_direction(self) -> None:
        tracker = GridDirectionTracker()
        map_ws_to_coordinator(
            self._bare(solar=2.0, load=1.0, bat=8.5, grid=7.5, status=2), tracker
        )
        map_ws_to_coordinator(
            self._bare(solar=3.0, load=1.0, bat=2.0, grid=0.01, status=2), tracker
        )
        data = map_ws_to_coordinator(
            self._bare(solar=7.7, load=0.5, bat=8.5, grid=8.0, status=99), tracker
        )
        assert data["gridConsumptionPower"] == pytest.approx(8.0)

    def test_signed_flag_wins_over_reliable_balance_disagreement(self) -> None:
        """Flag is the primary signal: a stale balance cannot override it."""
        msg = _ev_msg(
            solar=0.0, load=1.0, charger=0.0, bat_charge=0.0, grid=1.0, grid_flag=-1
        )
        data = map_ws_to_coordinator(msg, GridDirectionTracker())
        assert data["feedinPower"] == pytest.approx(1.0)

    def test_no_tracker_cold_start_keeps_legacy_gridstatus_fallback(self) -> None:
        msg = self._bare(solar=5.0, load=0.2, bat=0.0, grid=0.8, status="3")
        data = map_ws_to_coordinator(msg)
        assert data["gridConsumptionPower"] == pytest.approx(0.8)


class TestGridDirectionWithAcCoupledAux:
    """The opt-in ``aux`` generation term is folded into pvPower first; the
    charger term and the direction tracker then see the combined balance."""

    @staticmethod
    def _msg(
        *, aux: float, charger: float, grid: float, status: object
    ) -> dict[str, object]:
        msg = _ev_msg(
            solar=1.0,
            load=0.5,
            charger=charger,
            bat_charge=0.0,
            grid=grid,
            grid_status=status,
            grid_flag=None,
        )
        msg["result"]["node"]["aux"] = {  # type: ignore[index]
            "display": True,
            "power": {"value": f"{aux}", "unit": "kW"},
        }
        return msg

    def test_aux_makes_balance_reliable_for_export(self) -> None:
        """solar 1 + aux 6 vs load 0.5: export 6.5; gridStatus '3' must not win."""
        msg = self._msg(aux=6.0, charger=0.0, grid=6.5, status="3")
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["pvPower"] == pytest.approx(7.0)
        assert data["feedinPower"] == pytest.approx(6.5)
        assert data["gridConsumptionPower"] == 0.0

    def test_aux_and_charger_together_import(self) -> None:
        """balance = 0.5 + 6.9 - (1 + 4) = 2.4 import; neither term double counts."""
        msg = self._msg(aux=4.0, charger=6.9, grid=2.4, status=1)
        data = map_ws_to_coordinator(msg, additional_pv_enabled=True)
        assert data["gridConsumptionPower"] == pytest.approx(2.4)
        assert data["feedinPower"] == 0.0

    def test_aux_ignored_when_not_enabled_direction_still_held(self) -> None:
        """Not configured: aux is not generation; tracker still holds direction."""
        tracker = GridDirectionTracker()
        map_ws_to_coordinator(
            self._msg(aux=0.0, charger=0.0, grid=1.5, status=2), tracker
        )  # balance 0.5-1.0 = -0.5 vs 1.5 (3x) -> cold start; export by status!=3
        data = map_ws_to_coordinator(
            self._msg(aux=4.0, charger=0.0, grid=6.0, status=2), tracker
        )
        assert data["pvPower"] == pytest.approx(1.0)  # aux not added
        assert data["feedinPower"] == pytest.approx(6.0)


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
