"""Tests for the FoxESS Control diagnostics platform."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from custom_components.foxess_control.const import (
    CONF_SCHEDULER_HANDBACK,
    CONF_WORK_MODE_ENTITY,
    DOMAIN,
)
from custom_components.foxess_control.diagnostics import (
    REDACT_KEYS,
    _schedule_section,
    async_get_config_entry_diagnostics,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    FoxESSEntryData,
    build_config,
)
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter

# The recording client already used to prove "a default install makes no new
# API call" for the handback teardown (Task 7).  Imported rather than
# re-implemented: the point of the no-I/O assertion below is the *same*
# property measured the *same* way — real HTTP against the simulator with a
# side-channel record of the traffic — and two copies of it would be free to
# drift apart.
from .test_handback_teardown import RecordingClient

if TYPE_CHECKING:
    from .conftest import SimulatorHandle


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_hass_and_entry(domain_data: Any) -> tuple[Any, Any]:
    hass = MagicMock()
    hass.data = {DOMAIN: domain_data}
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {"api_key": "SECRET", "device_serial": "SN123"}
    entry.options = {"ws_mode": "auto"}
    return hass, entry


def test_diagnostics_includes_recent_errors() -> None:
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    buf.append(
        {
            "t": "2026-06-02T00:00:00+00:00",
            "category": "ws_discovery",
            "attempted": "discover battery id",
            "exc_type": "WSServerHandshakeError",
            "exc_str": "200, message='Invalid response status'",
            "hint": "regional endpoint mismatch",
            "context": {"host": "www.foxesscloud.com"},
            "severity": "warning",
        }
    )
    dd = SimpleNamespace(
        entries={},
        smart_charge_state=None,
        smart_discharge_state=None,
        smart_error_state=None,
        realtime_ws=None,
        taper_profile=None,
        ws_mode="auto",
        recent_errors=buf,
        web_session=None,
        plant_id="p1",
        battery_compound_id=None,
    )
    hass, entry = _make_hass_and_entry(dd)
    result = _run(async_get_config_entry_diagnostics(hass, entry))
    assert "recent_errors" in result
    assert result["recent_errors"][0]["exc_type"] == "WSServerHandshakeError"


def test_diagnostics_environment_reports_host_and_ws_mode() -> None:
    dd = SimpleNamespace(
        entries={},
        smart_charge_state=None,
        smart_discharge_state=None,
        smart_error_state=None,
        realtime_ws=None,
        taper_profile=None,
        ws_mode="auto",
        recent_errors=deque(maxlen=30),
        web_session=SimpleNamespace(BASE_URL="https://www.foxesscloud.com"),
        plant_id="p1",
        battery_compound_id=None,
    )
    hass, entry = _make_hass_and_entry(dd)
    result = _run(async_get_config_entry_diagnostics(hass, entry))
    env = result["environment"]
    assert env["cloud_base_url"] == "https://www.foxesscloud.com"
    assert env["ws_mode"] == "auto"
    assert env["ws_connected"] is False
    assert env["plant_id_present"] is True
    assert env["battery_compound_id_status"] == "missing"


def test_diagnostics_redacts_secrets_everywhere() -> None:
    buf: deque[dict[str, Any]] = deque(maxlen=30)
    buf.append(
        {
            "t": "2026-06-02T00:00:00+00:00",
            "category": "login",
            "attempted": "web login",
            "exc_type": "X",
            "exc_str": "y",
            "hint": None,
            "context": {"api_key": "LEAKED", "host": "h"},
            "severity": "warning",
        }
    )
    dd = SimpleNamespace(
        entries={},
        smart_charge_state=None,
        smart_discharge_state=None,
        smart_error_state=None,
        realtime_ws=None,
        taper_profile=None,
        ws_mode="auto",
        recent_errors=buf,
        web_session=SimpleNamespace(BASE_URL="https://www.foxesscloud.com"),
        plant_id="p1",
        battery_compound_id="uuid@SN999",
    )
    hass, entry = _make_hass_and_entry(dd)
    result = _run(async_get_config_entry_diagnostics(hass, entry))
    flat = str(result)
    assert "SECRET" not in flat
    assert "SN123" not in flat
    assert "LEAKED" not in flat
    assert "SN999" not in flat


class TestScheduleSection:
    def test_reports_cached_snapshot_and_outcome(self) -> None:
        from custom_components.foxess_control.domain_data import FoxESSControlData

        dd = FoxESSControlData()
        dd.last_schedule_snapshot = [
            {"enable": 1, "workMode": "SelfUse", "startHour": 0, "endHour": 23}
        ]
        dd.last_schedule_snapshot_at = "2026-06-18T01:00:00+00:00"
        dd.last_schedule_reconcile = {
            "action": "removed",
            "orphans": ["ForceCharge"],
            "detail": "removed ['ForceCharge']",
        }
        out = _schedule_section(dd, entity_mode=False)
        assert isinstance(out, dict)
        assert out["as_of"] == "2026-06-18T01:00:00+00:00"
        assert out["groups"][0]["workMode"] == "SelfUse"
        assert out["reconcile"]["action"] == "removed"

    def test_entity_mode_reports_na(self) -> None:
        from custom_components.foxess_control.domain_data import FoxESSControlData

        dd = FoxESSControlData()
        out = _schedule_section(dd, entity_mode=True)
        assert out == "n/a (entity mode)"

    def test_no_snapshot_yet(self) -> None:
        from custom_components.foxess_control.domain_data import FoxESSControlData

        dd = FoxESSControlData()
        out = _schedule_section(dd, entity_mode=False)
        assert isinstance(out, dict)
        assert out["as_of"] is None
        assert out["groups"] is None


# ---------------------------------------------------------------------------
# The scheduler-handback section (issues #16, #4)
# ---------------------------------------------------------------------------
#
# The handback is opt-in and off by default, so on almost every install every
# one of its outcomes is a *decline*.  The download therefore has to answer
# "why is my inverter still scheduler-controlled?" for an install where, by
# design, nothing happened — which means the section must be present and
# populated when the option is off, not only when it fired (C-020, C-026,
# D-059).
#
# Two hazards shape these tests:
#
# 1. **``0`` is a real captured floor.**  Issue #4 *is* a 0 % Min SoC, so a
#    falsy test anywhere on this path erases the feature's own use case.  The
#    download must distinguish "captured 0" from "never captured".
# 2. **Diagnostics is downloaded when something is already wrong.**  It must
#    not make an API call that can hang or rate-limit, so the Mode Scheduler
#    flag is reported from a cached snapshot and is ``null`` when nothing has
#    ever observed it — never fetched on demand.

# The keys the section must carry.  Spelled out so that dropping one is a
# failure rather than a quiet omission a reader mistakes for "off".
_HANDBACK_KEYS = {
    "enabled",
    "captured_min_soc_on_grid",
    "min_soc_capture",
    "last_handback",
    "scheduler_flag",
    "scheduler_set_unavailable",
}

# A declined outcome record, in the shape ``_handback_teardown._record_outcome``
# writes and ``tests/test_handback_teardown.py`` pins.  A decline is the case
# that matters most: it is what every default install has, and its reason is
# the actionable half.
_DECLINED_RECORD: dict[str, Any] = {
    "t": "2026-08-30T09:15:00+00:00",
    "acted": False,
    "reason": (
        "releasing the inverter is not enabled, so the Mode Scheduler was "
        "left exactly as it is"
    ),
    "steps": {},
    "restored_min_soc_on_grid": None,
}


def _keys_of(value: Any) -> set[str]:
    """Every dict key appearing anywhere in *value*, recursively."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            found |= _keys_of(item)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= _keys_of(item)
    return found


def _dd_with(**overrides: Any) -> FoxESSControlData:
    """Real domain data with a real config, handback OFF unless overridden."""
    options: dict[str, Any] = {}
    if "handback" in overrides:
        options[CONF_SCHEDULER_HANDBACK] = overrides.pop("handback")
    if overrides.pop("entity_mode", False):
        options[CONF_WORK_MODE_ENTITY] = "select.work_mode"
    dd = FoxESSControlData(config=build_config(options))
    for name, value in overrides.items():
        setattr(dd, name, value)
    return dd


def _handback(dd: FoxESSControlData, inverter: Any = None) -> dict[str, Any]:
    """The ``handback`` section of a full diagnostics download."""
    if inverter is not None:
        dd.entries["e1"] = FoxESSEntryData(inverter=inverter)
    hass, entry = _make_hass_and_entry(dd)
    result = _run(async_get_config_entry_diagnostics(hass, entry))
    assert "handback" in result, (
        "the diagnostics download has no 'handback' section, so a support "
        "request cannot say whether the option is on, what Min SoC was "
        "captured, or why the last handback did nothing"
    )
    section: dict[str, Any] = result["handback"]
    return section


class TestHandbackSection:
    """What the download says about the scheduler handback."""

    def test_present_and_populated_with_the_option_off(self) -> None:
        """A default install must still be diagnosable.

        "Off" is itself the answer to "why did nothing happen?", so the
        section may not be conditional on the feature being enabled.
        """
        section = _handback(_dd_with(handback=False))
        assert set(section) >= _HANDBACK_KEYS, (
            f"missing keys: {_HANDBACK_KEYS - set(section)}"
        )
        assert section["enabled"] is False, (
            "the option's state is not reported as False with the option off, "
            "so a support request cannot distinguish 'never opted in' from "
            "'opted in and silently broken'"
        )

    def test_reports_the_option_on(self) -> None:
        section = _handback(_dd_with(handback=True))
        assert section["enabled"] is True

    def test_captured_zero_is_reported_as_zero(self) -> None:
        """Issue #4 is a 0 % floor — a falsy test here erases the feature.

        ``0`` must survive into the download as ``0``: not omitted, not
        ``None``, not rendered as absent.
        """
        section = _handback(_dd_with(handback=True, captured_min_soc_on_grid=0))
        assert "captured_min_soc_on_grid" in section
        assert section["captured_min_soc_on_grid"] == 0
        assert section["captured_min_soc_on_grid"] is not None, (
            "a captured 0% floor is reported as 'nothing captured', which is "
            "exactly the bug issue #4 is about — the user's own floor is 0%"
        )
        assert section["min_soc_capture"] == "captured"

    def test_never_captured_is_distinguishable_from_captured_zero(self) -> None:
        never = _handback(_dd_with(handback=True, captured_min_soc_on_grid=None))
        zero = _handback(_dd_with(handback=True, captured_min_soc_on_grid=0))
        assert never["captured_min_soc_on_grid"] is None
        assert never["min_soc_capture"] == "never captured"
        assert never["min_soc_capture"] != zero["min_soc_capture"], (
            "'never captured' and 'captured 0%' render identically, so the "
            "download cannot say whether handback will restore anything"
        )

    def test_a_captured_floor_is_reported_verbatim(self) -> None:
        section = _handback(_dd_with(handback=True, captured_min_soc_on_grid=17))
        assert section["captured_min_soc_on_grid"] == 17

    def test_last_handback_round_trips_verbatim(self) -> None:
        """Including a decline and its reason — the default install's case."""
        section = _handback(_dd_with(last_handback=dict(_DECLINED_RECORD)))
        assert section["last_handback"] == _DECLINED_RECORD, (
            "the last-handback record did not survive the download intact, so "
            "the reason the handback declined is unavailable to triage"
        )
        assert section["last_handback"]["acted"] is False
        assert "not enabled" in section["last_handback"]["reason"]

    def test_a_successful_handback_round_trips_verbatim(self) -> None:
        acted = {
            "t": "2026-08-30T09:20:00+00:00",
            "acted": True,
            "reason": "handing the inverter back to its own settings",
            "steps": {
                "disable_scheduler": "ok",
                "work_mode": "ok",
                "min_soc_on_grid": "ok",
            },
            "restored_min_soc_on_grid": 0,
        }
        section = _handback(_dd_with(handback=True, last_handback=dict(acted)))
        assert section["last_handback"] == acted
        assert section["last_handback"]["restored_min_soc_on_grid"] == 0, (
            "a restored 0% floor is reported as 'restored nothing'"
        )

    def test_never_attempted_is_reported_as_null(self) -> None:
        section = _handback(_dd_with(handback=True))
        assert section["last_handback"] is None

    def test_the_section_holds_no_secrets(self) -> None:
        """Asserted, not reasoned about — and not vacuously.

        An earlier "holds no secrets" test passed because nothing was being
        retained at all, so this first proves the section is populated.
        """
        dd = _dd_with(
            handback=True,
            captured_min_soc_on_grid=13,
            last_handback=dict(_DECLINED_RECORD),
        )
        section = _handback(dd)
        assert section["captured_min_soc_on_grid"] == 13
        assert section["last_handback"], "nothing retained — the test is vacuous"

        flat = str(section)
        for secret in ("SECRET", "SN123"):
            assert secret not in flat, f"{secret!r} leaked into the handback section"
        for key in REDACT_KEYS:
            assert key not in _keys_of(section), (
                f"the handback section carries a {key!r} key, which is a "
                "redaction candidate — it must not be there at all"
            )

    def test_it_is_reported_as_unknown_with_no_inverter(self) -> None:
        """Setup may have failed before an inverter existed.

        ``false`` there would be a claim about hardware nobody reached.
        """
        section = _handback(_dd_with(handback=True))
        assert section["scheduler_set_unavailable"] is None

    def test_never_raises_when_domain_data_is_missing(self) -> None:
        hass = MagicMock()
        hass.data = {}
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {}
        entry.options = {}
        assert _run(async_get_config_entry_diagnostics(hass, entry)) == {}

    def test_never_raises_when_setup_failed_part_way(self) -> None:
        """No ``config`` yet: the option's state is unknown, not False.

        Reporting False would be a confident claim that the user never opted
        in, which sends triage to the wrong place (C-020).
        """
        dd = FoxESSControlData()  # config is None — setup did not get that far
        dd.captured_min_soc_on_grid = 0
        section = _handback(dd)
        assert section["enabled"] is None, (
            "an unreadable config is reported as 'option off', which is a "
            "claim about the user's settings that was never read"
        )
        assert section["captured_min_soc_on_grid"] == 0


class TestHandbackSectionMakesNoRequest:
    """Diagnostics is downloaded when something is already wrong.

    Real ``FoxESSClient``/``Inverter`` over HTTP against a fresh simulator
    (C-028), with every request recorded, so "no I/O" is an assertion about
    traffic rather than about the shape of the code.
    """

    @pytest.fixture(autouse=True)
    def _disable_throttle(self) -> None:
        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0

    @staticmethod
    def _inverter(sim: SimulatorHandle) -> tuple[Inverter, RecordingClient]:
        client = RecordingClient("test-api-key", base_url=sim.url)
        inv = Inverter(client, "SIM0001")
        # Warm the caches integration setup warms: ``max_power_w`` fetches
        # device detail and probes the declared scheduler limits, which is
        # pre-existing behaviour on this path and not what is under test.
        # Everything after this point must add nothing.
        _ = inv.max_power_w
        client.calls.clear()
        return inv, client

    def test_generating_diagnostics_issues_no_request(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        inv, client = self._inverter(foxess_sim)
        section = _handback(_dd_with(handback=True), inverter=inv)
        assert client.paths() == [], (
            "generating diagnostics made API requests "
            f"({client.paths()}) — a download taken because the cloud is "
            "already misbehaving must not hang or rate-limit"
        )
        assert set(section) >= _HANDBACK_KEYS

    def test_the_scheduler_flag_is_null_when_never_observed(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """``null`` means "never read", not "read and off".

        An implementation that fetched it on demand would report the live
        value here instead, and would have made a request to do it.
        """
        inv, client = self._inverter(foxess_sim)
        section = _handback(_dd_with(handback=True), inverter=inv)
        assert section["scheduler_flag"] is None, (
            "the Mode Scheduler flag was reported without anything ever "
            f"having read it (requests: {client.paths()})"
        )

    def test_the_scheduler_flag_reports_what_was_last_read(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        foxess_sim.set(scheduler_enabled=True, scheduler_supported=True)
        inv, client = self._inverter(foxess_sim)
        inv.get_scheduler_flag()
        client.calls.clear()

        section = _handback(_dd_with(handback=True), inverter=inv)

        assert client.paths() == [], "reporting the cached flag made a request"
        flag = section["scheduler_flag"]
        assert flag is not None, (
            "the flag was read but the download does not report it, so a "
            "'my inverter is still scheduler-controlled' report cannot say "
            "what the master switch was last seen doing (issue #16)"
        )
        assert flag["enable"] is True
        assert flag["support"] is True
        assert flag["as_of"], "no timestamp — a stale reading looks current"

    def test_the_missing_switch_endpoint_is_reported(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """Issue #17: "handback can never work on this hardware", in writing.

        The Repair issue tells the user; the download has to tell whoever
        reads their support request, and it must do so without asking the
        cloud — a 404 is exactly the sort of thing being investigated when
        someone downloads diagnostics.
        """
        import requests

        foxess_sim.set(scheduler_set_supported=False)
        inv, client = self._inverter(foxess_sim)
        with pytest.raises(requests.HTTPError):
            inv.set_scheduler_enabled(False)
        client.calls.clear()

        section = _handback(_dd_with(handback=True), inverter=inv)

        assert client.paths() == [], "reporting the cached 404 made a request"
        assert section["scheduler_set_unavailable"] is True, (
            "the download does not say the master-switch write endpoint is "
            "absent, so a support request cannot tell 'handback is "
            "impossible on this model' from 'handback is broken'"
        )

    def test_no_claim_is_made_before_anything_has_tried(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """A fresh inverter has learned nothing, and says so."""
        inv, _client = self._inverter(foxess_sim)

        section = _handback(_dd_with(handback=True), inverter=inv)

        assert section["scheduler_set_unavailable"] is False

    def test_the_scheduler_flag_follows_the_handback_turning_it_off(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        """After a handback the switch is off, and the download must say so.

        The read happens *before* the write (``probe_scheduler_support`` then
        ``set_scheduler_enabled(False)``), so a snapshot that only tracked
        reads would report ``enable: True`` for an inverter this integration
        had just released — the download contradicting the device.
        """
        foxess_sim.set(scheduler_enabled=True, scheduler_supported=True)
        inv, client = self._inverter(foxess_sim)
        inv.get_scheduler_flag()
        inv.set_scheduler_enabled(False)
        client.calls.clear()

        flag = _handback(_dd_with(handback=True), inverter=inv)["scheduler_flag"]

        assert client.paths() == []
        assert foxess_sim.state()["scheduler_enabled"] is False
        assert flag is not None and flag["enable"] is False, (
            "the download reports the Mode Scheduler master switch as still "
            f"on after the integration turned it off ({flag})"
        )

    def test_an_inverter_without_the_snapshot_reports_null(self) -> None:
        """A future adapter without the snapshot must not break the download.

        Stubbed with exactly the attributes the rest of the download already
        reads, so the only thing missing is the one under test.  (Anything
        less than this crashes on ``max_power_w``, which is pre-existing and
        not the handback section's business.)
        """
        stub = SimpleNamespace(
            max_power_w=3500,
            device_type="H3-8.0",
            declared_limits_snapshot=None,
            last_write_failure=None,
            last_write_ok_at=None,
        )
        section = _handback(_dd_with(handback=True), inverter=stub)
        assert section["scheduler_flag"] is None
