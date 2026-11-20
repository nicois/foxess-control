"""Detecting a REST poll that returns a repeated cloud sample (issue #41).

With ``meterPower2`` configured, a user saw the 5-minute REST poll return the
same snapshot — values and sample ``time`` — for 30-55 minutes while the
integration presented it as fresh.  The coordinator now compares each
response's sample ``time`` with the last one, keeps the time a sample was
*first* received as ``_data_last_update`` (so the cards' existing staleness
threshold applies), records one Recent Errors entry per long repeat, and
keeps a short history of repeats for the diagnostics download.

Judged by how long a sample has been repeated, not by how many polls: the
datalogger uploads every few minutes, so two polls 60 s apart returning one
sample is normal.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from custom_components.foxess_control import coordinator as coordinator_mod
from custom_components.foxess_control.coordinator import FoxESSDataCoordinator
from custom_components.foxess_control.domain_data import build_config
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter

if TYPE_CHECKING:
    import pytest

    from .conftest import SimulatorHandle

_T0 = datetime.datetime(2026, 11, 18, 7, 46, tzinfo=datetime.UTC)


def _coordinator(
    monkeypatch: pytest.MonkeyPatch, extra_var: str | None = None
) -> tuple[FoxESSDataCoordinator, MagicMock, MagicMock]:
    options = {"additional_pv_power_variable": extra_var} if extra_var else {}
    cfg = build_config(options)
    monkeypatch.setattr(coordinator_mod, "_cfg", lambda _h: cfg)
    spy = MagicMock()
    monkeypatch.setattr(coordinator_mod, "record_operational_error", spy)
    inverter = MagicMock()
    inverter.get_real_time.return_value = {"pvPower": 1.0, "meterPower2": 0.8}
    inverter.get_current_mode.return_value = None
    with patch("homeassistant.helpers.frame.report_usage"):
        coord = FoxESSDataCoordinator(
            MagicMock(), inverter, update_interval_seconds=300
        )
    return coord, inverter, spy


def _poll(
    coord: FoxESSDataCoordinator, inverter: MagicMock, sample: str | None, at: float
) -> None:
    """One REST poll returning *sample*, *at* seconds after ``_T0``."""
    inverter.last_real_time_sample = sample
    now = _T0 + datetime.timedelta(seconds=at)
    with patch(
        "custom_components.foxess_control.coordinator.dt_util.utcnow",
        return_value=now,
    ):
        coord._fetch_all()


class TestDataAsOf:
    def test_fresh_samples_are_stamped_now(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coord, inv, _ = _coordinator(monkeypatch)
        _poll(coord, inv, "2026-11-18 07:46:00", 0)
        _poll(coord, inv, "2026-11-18 07:51:00", 300)
        assert coord.rest_data_as_of() == _T0 + datetime.timedelta(seconds=300)

    def test_a_repeated_sample_keeps_its_first_seen_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The card's age then grows with the repeat, and its existing
        15-minute threshold shows it as stale (C-020)."""
        coord, inv, _ = _coordinator(monkeypatch)
        _poll(coord, inv, "2026-11-18 07:46:00", 0)
        _poll(coord, inv, "2026-11-18 07:46:00", 300)
        _poll(coord, inv, "2026-11-18 07:46:00", 600)
        assert coord.rest_data_as_of() == _T0

    def test_no_sample_time_means_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A response without ``time`` cannot be judged; treat it as fresh."""
        coord, inv, _ = _coordinator(monkeypatch)
        _poll(coord, inv, None, 0)
        _poll(coord, inv, None, 300)
        assert coord.rest_data_as_of() == _T0 + datetime.timedelta(seconds=300)


class TestRecentErrors:
    def test_a_long_repeat_is_recorded_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coord, inv, spy = _coordinator(monkeypatch, extra_var="meterPower2")
        for minute in range(0, 35, 5):
            _poll(coord, inv, "2026-11-18 07:46:00", minute * 60)
        assert spy.call_count == 1
        kwargs = spy.call_args.kwargs
        assert kwargs["category"] == "stale_data"
        context = kwargs["context"]
        assert context["sample_time"] == "2026-11-18 07:46:00"
        assert context["additional_pv_power_variable"] == "meterPower2"
        assert context["seconds_repeated"] > coordinator_mod.REST_SAMPLE_STALE_AFTER_S

    def test_a_short_repeat_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One sample across two or three polls is normal datalogger cadence."""
        coord, inv, spy = _coordinator(monkeypatch)
        _poll(coord, inv, "2026-11-18 07:46:00", 0)
        _poll(coord, inv, "2026-11-18 07:46:00", 60)
        _poll(coord, inv, "2026-11-18 07:46:00", 120)
        _poll(coord, inv, "2026-11-18 07:51:00", 180)
        assert spy.call_count == 0

    def test_each_long_repeat_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coord, inv, spy = _coordinator(monkeypatch)
        for minute in range(0, 25, 5):
            _poll(coord, inv, "A", minute * 60)
        _poll(coord, inv, "B", 25 * 60)
        for minute in range(30, 55, 5):
            _poll(coord, inv, "B", minute * 60)
        assert spy.call_count == 2


class TestRepeatHistory:
    def test_history_records_each_repeat_with_its_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The diagnostics telemetry for #41: when, for how long, and
        whether ``meterPower2`` was in the request."""
        coord, inv, _ = _coordinator(monkeypatch, extra_var="meterPower2")
        for minute in range(0, 25, 5):
            _poll(coord, inv, "A", minute * 60)
        _poll(coord, inv, "B", 25 * 60)

        snapshot = coord.rest_freshness_snapshot()
        assert snapshot["sample_time"] == "B"
        (episode,) = snapshot["repeats"]
        assert episode["sample_time"] == "A"
        assert episode["polls"] == 5
        assert episode["first_seen_at"] == _T0.isoformat()
        assert episode["ended_at"] == (_T0 + datetime.timedelta(minutes=25)).isoformat()
        assert episode["stale"] is True
        assert episode["additional_pv_power_variable"] == "meterPower2"

    def test_an_ongoing_repeat_is_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        coord, inv, _ = _coordinator(monkeypatch)
        _poll(coord, inv, "A", 0)
        _poll(coord, inv, "A", 300)
        (episode,) = coord.rest_freshness_snapshot()["repeats"]
        assert episode["ended_at"] is None
        assert episode["polls"] == 2
        assert episode["stale"] is False

    def test_history_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        coord, inv, _ = _coordinator(monkeypatch)
        at = 0
        for i in range(30):
            _poll(coord, inv, f"S{i}", at)
            _poll(coord, inv, f"S{i}", at + 60)
            at += 120
        repeats = coord.rest_freshness_snapshot()["repeats"]
        assert len(repeats) == coordinator_mod.REST_REPEAT_HISTORY
        assert repeats[-1]["sample_time"] == "S29"


class TestAgainstTheSimulator:
    """The sample time is read from a real response, not only mocked."""

    def test_sample_time_is_read_from_the_response(
        self, foxess_sim: SimulatorHandle
    ) -> None:
        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        inv = Inverter(FoxESSClient("k", base_url=foxess_sim.url), "SIM0001")
        inv.get_real_time(["SoC"])
        first = inv.last_real_time_sample
        assert first is not None

        foxess_sim.set(real_time_frozen=True)
        foxess_sim.set(sim_time="2026-11-18T09:00:00+00:00")
        inv.get_real_time(["SoC"])
        assert inv.last_real_time_sample == first, "a frozen cloud repeats its time"

        foxess_sim.set(real_time_frozen=False)
        inv.get_real_time(["SoC"])
        assert inv.last_real_time_sample != first

    def test_the_sample_time_is_kept_raw(self, foxess_sim: SimulatorHandle) -> None:
        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        foxess_sim.set(sim_time="2026-11-18T07:46:12+00:00")
        inv = Inverter(FoxESSClient("k", base_url=foxess_sim.url), "SIM0001")
        inv.get_real_time(["SoC"])
        assert inv.last_real_time_sample == "2026-11-18 07:46:12 UTC+0000"


class TestDiagnostics:
    def test_the_download_carries_the_repeat_history(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from custom_components.foxess_control.diagnostics import _rest_freshness

        coord, inv, _ = _coordinator(monkeypatch, extra_var="meterPower2")
        _poll(coord, inv, "A", 0)
        _poll(coord, inv, "A", 300)
        section = _rest_freshness(coord)
        assert section is not None
        assert section["sample_time"] == "A"
        assert section["repeats"][0]["additional_pv_power_variable"] == "meterPower2"

    def test_absent_for_other_coordinators(self) -> None:
        """Entity mode and the local gateway have no cloud REST poll."""
        from custom_components.foxess_control.diagnostics import _rest_freshness

        assert _rest_freshness(object()) is None
        assert _rest_freshness(None) is None
