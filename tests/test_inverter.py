"""Tests for Inverter high-level control."""

import json

import pytest
import responses

from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter


@pytest.fixture(autouse=True)
def _disable_throttle() -> None:
    """Disable request throttling in tests."""
    FoxESSClient.MIN_REQUEST_INTERVAL = 0.0


def _make_client() -> FoxESSClient:
    return FoxESSClient("test-api-key")


def _real_time_response(
    variables: dict[str, float], sn: str = "INV001"
) -> dict[str, object]:
    """Build a real-time query response matching the actual API format."""
    return {
        "errno": 0,
        "result": [
            {
                "datas": [
                    {"variable": k, "value": v, "name": k, "unit": ""}
                    for k, v in variables.items()
                ],
                "deviceSN": sn,
                "time": "2026-04-07 10:00:00 AEST+1000",
            }
        ],
    }


@responses.activate
def test_auto_detect() -> None:
    client = _make_client()
    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/list",
        json={
            "errno": 0,
            "result": {"data": [{"deviceSN": "INV001"}], "total": 1},
        },
    )

    inv = Inverter.auto_detect(client)
    assert inv.sn == "INV001"


@responses.activate
def test_get_soc() -> None:
    inv = Inverter(_make_client(), "INV001")

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/real/query",
        json=_real_time_response({"SoC": 75.5}),
    )

    assert inv.get_soc() == 75.5


def _add_detail_response(sn: str = "INV001", capacity: int = 10) -> None:
    """Register a mock device detail response."""
    responses.add(
        responses.GET,
        "https://www.foxesscloud.com/op/v0/device/detail",
        json={"errno": 0, "result": {"deviceSN": sn, "capacity": capacity}},
    )


@responses.activate
def test_self_use() -> None:
    inv = Inverter(_make_client(), "INV001")
    _add_detail_response()

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/scheduler/enable",
        json={"errno": 0, "result": None},
    )

    inv.self_use()

    body = json.loads(responses.calls[1].request.body)  # type: ignore[arg-type]
    assert body["deviceSN"] == "INV001"
    assert len(body["groups"]) == 1
    assert body["groups"][0]["workMode"] == "SelfUse"
    assert body["groups"][0]["startHour"] == 0
    assert body["groups"][0]["endHour"] == 23
    assert body["groups"][0]["fdPwr"] == 10 * Inverter.CAPACITY_TO_FD_PWR


@responses.activate
def test_force_charge() -> None:
    inv = Inverter(_make_client(), "INV001")
    _add_detail_response()

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/scheduler/enable",
        json={"errno": 0, "result": None},
    )

    inv.force_charge(target_soc=80)

    body = json.loads(responses.calls[1].request.body)  # type: ignore[arg-type]
    assert body["groups"][0]["workMode"] == "ForceCharge"
    assert body["groups"][0]["fdSoc"] == 80


@responses.activate
def test_force_discharge() -> None:
    inv = Inverter(_make_client(), "INV001")

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/scheduler/enable",
        json={"errno": 0, "result": None},
    )

    inv.force_discharge(min_soc=20, power=3000)

    body = json.loads(responses.calls[0].request.body)  # type: ignore[arg-type]
    assert body["groups"][0]["workMode"] == "ForceDischarge"
    assert body["groups"][0]["fdSoc"] == 20
    assert body["groups"][0]["fdPwr"] == 3000


@responses.activate
def test_max_power_cached() -> None:
    """max_power_w queries device detail once and caches the result."""
    inv = Inverter(_make_client(), "INV001")
    _add_detail_response(capacity=8)

    assert inv.max_power_w == 8 * Inverter.CAPACITY_TO_FD_PWR
    # Second access should not make another API call
    assert inv.max_power_w == 8 * Inverter.CAPACITY_TO_FD_PWR
    assert len(responses.calls) == 1


@responses.activate
def test_get_battery_status() -> None:
    inv = Inverter(_make_client(), "INV001")

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/real/query",
        json=_real_time_response(
            {
                "SoC": 65,
                "batChargePower": 1.2,
                "batDischargePower": 0,
                "batTemperature": 24.5,
                "batVolt": 52.1,
                "batCurrent": 23.0,
                "ResidualEnergy": 6.5,
            }
        ),
    )

    status = inv.get_battery_status()
    assert status["SoC"] == 65
    assert status["batChargePower"] == 1.2
    assert status["ResidualEnergy"] == 6.5


@responses.activate
def test_get_current_mode() -> None:
    inv = Inverter(_make_client(), "INV001")

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/scheduler/get",
        json={
            "errno": 0,
            "result": {
                "enable": 1,
                "groups": [
                    {
                        "enable": 1,
                        "startHour": 0,
                        "startMinute": 0,
                        "endHour": 23,
                        "endMinute": 59,
                        "workMode": "ForceCharge",
                        "minSocOnGrid": 10,
                        "fdSoc": 100,
                        "fdPwr": 0,
                    },
                    {
                        "enable": 0,
                        "startHour": 0,
                        "startMinute": 0,
                        "endHour": 0,
                        "endMinute": 0,
                        "workMode": "SelfUse",
                        "minSocOnGrid": 10,
                        "fdSoc": 10,
                        "fdPwr": 0,
                    },
                ],
            },
        },
    )

    mode = inv.get_current_mode()
    assert mode == "ForceCharge"


@responses.activate
def test_get_schedule_null_result() -> None:
    """API returns null when no scheduler is configured (mode set via app)."""
    inv = Inverter(_make_client(), "INV001")

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/scheduler/get",
        json={"errno": 0, "result": None},
    )

    schedule = inv.get_schedule()
    assert schedule == {"enable": 0, "groups": []}


@responses.activate
def test_get_current_mode_null_schedule() -> None:
    """get_current_mode handles null schedule gracefully."""
    inv = Inverter(_make_client(), "INV001")

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/scheduler/get",
        json={"errno": 0, "result": None},
    )

    assert inv.get_current_mode() is None


@responses.activate
def test_get_current_mode_none_enabled() -> None:
    inv = Inverter(_make_client(), "INV001")

    responses.add(
        responses.POST,
        "https://www.foxesscloud.com/op/v0/device/scheduler/get",
        json={
            "errno": 0,
            "result": {
                "enable": 0,
                "groups": [
                    {
                        "enable": 0,
                        "startHour": 0,
                        "startMinute": 0,
                        "endHour": 23,
                        "endMinute": 59,
                        "workMode": "SelfUse",
                        "minSocOnGrid": 10,
                        "fdSoc": 10,
                        "fdPwr": 0,
                    },
                ],
            },
        },
    )

    mode = inv.get_current_mode()
    assert mode is None
