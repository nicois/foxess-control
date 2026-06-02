"""Tests for structured operational-error recording."""

from __future__ import annotations

from collections import deque

from smart_battery.domain_data import SmartBatteryDomainData


def test_domain_data_has_bounded_recent_errors_buffer() -> None:
    dd = SmartBatteryDomainData()
    assert isinstance(dd.recent_errors, deque)
    assert dd.recent_errors.maxlen == 30
    dd2 = SmartBatteryDomainData()
    dd.recent_errors.append({"x": 1})
    assert len(dd2.recent_errors) == 0  # no shared mutable default
