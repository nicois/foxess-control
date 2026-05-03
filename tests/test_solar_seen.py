"""Tests for the "solar seen" runtime flag and its dashboard behaviour.

Feature
-------
Installations without solar panels (AC-coupled inverters, battery-only
inverters, or any setup where the PV fields are permanently zero)
should not render a perpetually-stuck "0.0 kW" solar reading on the
overview card — that is noise.  Instead, the card should show the
house/generator load ("Gen Load") in the solar box until a non-zero
``pvPower`` value has been observed at runtime.

Behaviour contract
------------------
1. **Fresh start** — a newly-initialised coordinator has never seen a
   non-zero ``pvPower``, so the public attribute ``solar_seen`` is
   ``False`` and the card renders "Gen Load" with ``loadsPower`` in
   place of the solar value and icon.
2. **Solar observed** — once any WS or REST update carries
   ``pvPower`` above the noise threshold (``SOLAR_SEEN_THRESHOLD_KW``,
   default 0.05 kW / 50 W), the flag flips to ``True`` and the last-
   seen timestamp is refreshed (exposed on the ``pv_power`` sensor as
   the ``solar_seen`` attribute).  The card reverts to the normal
   solar rendering.
3. **Sticky within window** — subsequent zero / sub-threshold readings
   keep the flag ``True`` as long as the most recent positive
   observation is less than ``SOLAR_SEEN_TIMEOUT_MIN`` minutes old
   (default 20 min).  This absorbs brief cloud dips and WS flicker
   without flapping the display.
4. **Reverts after timeout** — if no reading above threshold has been
   seen for ``SOLAR_SEEN_TIMEOUT_MIN`` minutes, the flag goes back to
   ``False`` and the card switches back to Gen Load.  This is what
   makes the display honest overnight / on permanently-unwired PV
   installs that previously saw a transient during the day.
5. **Re-flips on new positive reading** — a subsequent above-threshold
   reading flips the flag back to ``True`` and refreshes the
   timestamp, following the same rule as step 2.

A fresh start (integration reload) begins again in "not yet seen"
mode; the flag must not persist across restarts.

Defensive cases
---------------
- A tiny positive reading below the noise threshold (e.g. 0.01 kW)
  does NOT count — sensor noise near dawn/dusk shouldn't flap the
  display.
- ``None``, missing key, and negative readings do NOT count — the
  detector is defensive against garbage data.

C-020/C-026 — Operational transparency: the user must determine
"are my solar readings real?" from the UI alone.  Rather than surface
a flag the user has to interpret, we hide the stuck-zero reading and
show a useful load value in its place, with a descriptive label.

Storage — runtime state only.  The flag lives on the coordinator, not
a HA sensor entity (the user requirement explicitly forbids a new
sensor) and does not persist across restarts.  This is consistent
with the existing ``_data_source`` / ``_data_last_update`` convention
(underscore-prefixed keys in ``coordinator.data`` that are not
sensors but are read by sensor ``extra_state_attributes``).
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.foxess_control.coordinator import (
    SOLAR_SEEN_THRESHOLD_KW,
    SOLAR_SEEN_TIMEOUT_MIN,
    FoxESSDataCoordinator,
)
from custom_components.foxess_control.foxess.inverter import Inverter, WorkMode
from custom_components.foxess_control.sensor import (
    POLLED_SENSOR_DESCRIPTIONS,
    FoxESSPolledSensor,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator(
    inverter: Inverter | None = None,
    update_interval: int = 300,
) -> Any:
    """Build a coordinator with a real dict ``hass.data`` (matches
    production — MagicMock's truthy ``.get()`` triggers side paths
    that don't fire in the real code).

    Return type is :class:`Any` rather than
    :class:`FoxESSDataCoordinator` because these tests assert on
    attributes (``solar_seen``) introduced by the feature under test;
    a concrete annotation would mask real attribute errors behind
    mypy's static-type check before the feature ships.
    """
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.data = {}
    if inverter is None:
        inverter = MagicMock(spec=Inverter)
    with patch("homeassistant.helpers.frame.report_usage"):
        coord = FoxESSDataCoordinator(hass, inverter, update_interval)
    return coord


def _pv_power_descriptor() -> Any:
    """Return the ``pvPower`` polled-sensor descriptor."""
    for desc in POLLED_SENSOR_DESCRIPTIONS:
        if desc.variable == "pvPower":
            return desc
    msg = "pvPower descriptor missing from POLLED_SENSOR_DESCRIPTIONS"
    raise RuntimeError(msg)


def _make_entry(entry_id: str = "entry1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    # Always expose a web_username so extra_state_attributes populates.
    entry.data = {"web_username": "user@example.com"}
    return entry


# ---------------------------------------------------------------------------
# Coordinator-level tests: the flag lifecycle
# ---------------------------------------------------------------------------


class TestCoordinatorSolarSeenFlag:
    """The coordinator tracks whether pvPower > 0 has ever been seen."""

    def test_fresh_coordinator_solar_not_yet_seen(self) -> None:
        """A newly-initialised coordinator has never seen solar."""
        coord = _make_coordinator()
        # Public state via coordinator.data.  Freshly constructed the
        # coordinator has no data yet; but the flag query must work.
        assert coord.solar_seen is False

    @pytest.mark.asyncio
    async def test_rest_poll_with_zero_pv_keeps_flag_false(self) -> None:
        """A REST poll that returns pvPower=0 must NOT flip the flag."""
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0, "pvPower": 0.0}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        coord = _make_coordinator(inverter=inv)

        data = await coord._async_update_data()

        assert coord.solar_seen is False
        assert data["_solar_seen"] is False

    @pytest.mark.asyncio
    async def test_rest_poll_with_positive_pv_flips_flag(self) -> None:
        """A REST poll carrying pvPower>0 flips the flag permanently."""
        inv = MagicMock(spec=Inverter)
        inv.get_real_time.return_value = {"SoC": 50.0, "pvPower": 3.2}
        inv.get_current_mode.return_value = WorkMode.SELF_USE
        coord = _make_coordinator(inverter=inv)

        data = await coord._async_update_data()

        assert coord.solar_seen is True
        assert data["_solar_seen"] is True

    def test_ws_injection_with_zero_pv_keeps_flag_false(self) -> None:
        """WS update carrying pvPower=0 must NOT flip the flag."""
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "pvPower": 0.0, "_solar_seen": False}

        coord.inject_realtime_data({"SoC": 55.0, "pvPower": 0.0})

        assert coord.solar_seen is False
        assert coord.data is not None
        assert coord.data["_solar_seen"] is False

    def test_ws_injection_with_positive_pv_flips_flag(self) -> None:
        """WS update carrying any pvPower>0 flips the flag."""
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "pvPower": 0.0, "_solar_seen": False}

        coord.inject_realtime_data({"SoC": 55.0, "pvPower": 2.5})

        assert coord.solar_seen is True
        assert coord.data is not None
        assert coord.data["_solar_seen"] is True

    def test_flag_is_sticky_within_timeout_window(self) -> None:
        """Within the timeout window, subsequent pvPower=0 keeps True."""
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        # Flip it on
        coord.inject_realtime_data({"SoC": 50.0, "pvPower": 2.0})
        assert coord.solar_seen is True

        # A handful of zero readings arriving back-to-back (seconds
        # apart) must NOT revert — that'd flap the display on every
        # cloud dip.
        for _ in range(5):
            coord.inject_realtime_data({"SoC": 50.0, "pvPower": 0.0})
        assert coord.solar_seen is True
        assert coord.data is not None
        assert coord.data["_solar_seen"] is True

    def test_flag_reverts_after_timeout(self) -> None:
        """After SOLAR_SEEN_TIMEOUT_MIN with no reading above threshold,
        the flag MUST revert to False — otherwise the display keeps
        claiming the inverter saw solar on a permanently-dark site.
        """
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        now = datetime.datetime(2026, 5, 3, 12, 0, tzinfo=datetime.UTC)
        coord._observe_pv_power(2.5, now=now)
        # Verify at the same simulated moment (the ``solar_seen`` property
        # uses live UTC, so we call the explicit ``_solar_seen_at`` form
        # to keep the assertion time-consistent with ``_observe_pv_power``).
        assert coord._solar_seen_at(now=now) is True

        # Just before timeout — still True.
        nearly = now + datetime.timedelta(minutes=SOLAR_SEEN_TIMEOUT_MIN - 1)
        assert coord._solar_seen_at(now=nearly) is True

        # At / past timeout with no refresh — reverts to False.
        past = now + datetime.timedelta(minutes=SOLAR_SEEN_TIMEOUT_MIN + 1)
        assert coord._solar_seen_at(now=past) is False

    def test_positive_reading_after_timeout_re_flips_flag(self) -> None:
        """A fresh positive reading after the flag reverted flips it
        back on and refreshes the timestamp — same rule as startup.
        """
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        now = datetime.datetime(2026, 5, 3, 12, 0, tzinfo=datetime.UTC)
        coord._observe_pv_power(2.5, now=now)

        # Fast-forward past the timeout — flag has effectively reverted.
        later = now + datetime.timedelta(minutes=SOLAR_SEEN_TIMEOUT_MIN + 5)
        assert coord._solar_seen_at(now=later) is False

        # New positive reading at `later` — flips back to True.
        coord._observe_pv_power(1.0, now=later)
        assert coord._solar_seen_at(now=later) is True

        # And stays True within the new window anchored to `later`.
        later_plus_10 = later + datetime.timedelta(minutes=10)
        assert coord._solar_seen_at(now=later_plus_10) is True

    def test_zero_reading_does_not_refresh_timestamp(self) -> None:
        """A zero reading within the window must NOT refresh the
        last-seen timestamp — otherwise the timeout never triggers on
        installs that see transient positive readings then go quiet.
        """
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        t0 = datetime.datetime(2026, 5, 3, 12, 0, tzinfo=datetime.UTC)
        coord._observe_pv_power(2.5, now=t0)

        # Many zero readings well within the window.
        t1 = t0 + datetime.timedelta(minutes=5)
        for _ in range(10):
            coord._observe_pv_power(0.0, now=t1)

        # Timestamp is anchored at t0, not t1 — so past t0+timeout
        # the flag reverts even though a zero arrived at t1.
        past = t0 + datetime.timedelta(minutes=SOLAR_SEEN_TIMEOUT_MIN + 1)
        assert coord._solar_seen_at(now=past) is False

    def test_sub_threshold_positive_does_not_flip_flag(self) -> None:
        """A reading below SOLAR_SEEN_THRESHOLD_KW (noise) must NOT
        flip the flag.  The threshold exists so that sensor noise near
        dawn/dusk does not keep the solar display alive on sites with
        no real generation.
        """
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        # 0.01 kW = 10 W, below the 50 W threshold.
        coord.inject_realtime_data({"SoC": 50.0, "pvPower": 0.01})
        assert coord.solar_seen is False

        # At the threshold: also does NOT count (strict greater-than).
        coord.inject_realtime_data({"SoC": 50.0, "pvPower": SOLAR_SEEN_THRESHOLD_KW})
        assert coord.solar_seen is False

    def test_above_threshold_positive_flips_flag(self) -> None:
        """A reading strictly above SOLAR_SEEN_THRESHOLD_KW counts."""
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        coord.inject_realtime_data(
            {"SoC": 50.0, "pvPower": SOLAR_SEEN_THRESHOLD_KW + 0.001}
        )
        assert coord.solar_seen is True

    def test_negative_pv_does_not_flip_flag(self) -> None:
        """Defensive: negative pvPower (garbage) must NOT count."""
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        coord.inject_realtime_data({"SoC": 50.0, "pvPower": -0.5})

        assert coord.solar_seen is False

    def test_missing_pv_does_not_flip_flag(self) -> None:
        """Defensive: absence of pvPower key does NOT count."""
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        coord.inject_realtime_data({"SoC": 50.0})

        assert coord.solar_seen is False

    def test_none_pv_does_not_flip_flag(self) -> None:
        """Defensive: explicit None pvPower does NOT count."""
        coord = _make_coordinator()
        coord.data = {"SoC": 50.0, "_solar_seen": False}

        coord.inject_realtime_data({"SoC": 50.0, "pvPower": None})

        assert coord.solar_seen is False


# ---------------------------------------------------------------------------
# Sensor attribute: the card reads `solar_seen` from the pv_power sensor
# ---------------------------------------------------------------------------


class TestPvPowerSensorSolarSeenAttribute:
    """The pv_power sensor surfaces the flag so the card can read it."""

    def test_solar_seen_false_on_fresh_sensor(self) -> None:
        """On a fresh coordinator, pv_power sensor reports solar_seen=False."""
        coordinator = MagicMock()
        coordinator.data = {"pvPower": 0.0, "_solar_seen": False}
        sensor = FoxESSPolledSensor(coordinator, _make_entry(), _pv_power_descriptor())

        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["solar_seen"] is False

    def test_solar_seen_true_after_flag_flip(self) -> None:
        """After flag flip, pv_power sensor reports solar_seen=True."""
        coordinator = MagicMock()
        coordinator.data = {"pvPower": 2.5, "_solar_seen": True}
        sensor = FoxESSPolledSensor(coordinator, _make_entry(), _pv_power_descriptor())

        attrs = sensor.extra_state_attributes
        assert attrs is not None
        assert attrs["solar_seen"] is True

    def test_solar_seen_absent_on_non_pv_sensor(self) -> None:
        """solar_seen is a pv_power-specific attribute; other polled
        sensors (SoC, load) do not expose it — avoids attribute
        pollution on unrelated entities.
        """
        soc_desc = POLLED_SENSOR_DESCRIPTIONS[0]  # SoC
        assert soc_desc.variable == "SoC"
        coordinator = MagicMock()
        coordinator.data = {"SoC": 75.0, "_solar_seen": True, "_data_source": "ws"}
        sensor = FoxESSPolledSensor(coordinator, _make_entry(), soc_desc)

        attrs = sensor.extra_state_attributes
        # data_source is always exposed in multi-source; solar_seen must not be.
        assert attrs is not None
        assert "solar_seen" not in attrs


# ---------------------------------------------------------------------------
# Playwright card test: the overview card swaps the solar box for gen-load
# ---------------------------------------------------------------------------


_WWW_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "foxess_control"
    / "www"
)
_OVERVIEW_CARD_JS = _WWW_DIR / "foxess-overview-card.js"


# Hass stub — configurable pvPower / loadsPower values and solar_seen
# attribute on the pv_power entity.  Mimics a minimal entity map with
# only the entities the solar/house boxes touch.
_HASS_STUB_JS = r"""
window.makeHass = function(opts) {
    const pv = opts.pvPower;
    const load = opts.loadsPower;
    const solarSeen = opts.solarSeen;
    // kW floats stringified — HA stores states as strings.
    const toState = (v) => (v === null || v === undefined) ? "unknown" : String(v);
    return {
        language: "en",
        states: {
            "sensor.foxess_pv_power": {
                entity_id: "sensor.foxess_pv_power",
                state: toState(pv),
                attributes: {
                    unit_of_measurement: "kW",
                    solar_seen: solarSeen,
                    data_source: "ws",
                },
            },
            "sensor.foxess_loads_power": {
                entity_id: "sensor.foxess_loads_power",
                state: toState(load),
                attributes: {unit_of_measurement: "kW"},
            },
        },
        callWS: function(msg) {
            if (msg && msg.type === "foxess_control/entity_map") {
                return Promise.resolve({
                    solar_power: "sensor.foxess_pv_power",
                    house_load: "sensor.foxess_loads_power",
                });
            }
            return Promise.resolve({});
        },
        callService: function() { return Promise.resolve(); },
    };
};
"""


def _inject_overview_card(page: Page) -> None:
    """Serve a blank page + inject the overview card JS + hass stub."""
    page.set_content(
        "<!doctype html><html><head><meta charset='utf-8'></head>"
        "<body><div id='root'></div></body></html>",
        wait_until="load",
    )
    page.add_script_tag(content=_HASS_STUB_JS)
    page.add_script_tag(content=_OVERVIEW_CARD_JS.read_text(encoding="utf-8"))


def _mount_overview(
    page: Page,
    *,
    pv_power: float | None,
    loads_power: float | None,
    solar_seen: bool,
    boxes: list[Any] | None = None,
) -> dict[str, Any]:
    """Mount the card with a fresh hass and return the solar node's
    rendered text + label + value for assertion (or ``solarRendered=False``
    when the solar box has been hidden)."""
    result = page.evaluate(
        """async (opts) => {
            const card = document.createElement("foxess-overview-card");
            const config = {};
            if (opts.boxes !== null) {
                config.boxes = opts.boxes;
            }
            card.setConfig(config);
            document.getElementById("root").appendChild(card);
            card.hass = window.makeHass(opts);
            // entity_map fetch is async — wait a tick then reassign hass
            // so the newly-populated map triggers a re-render.
            await new Promise((r) => setTimeout(r, 50));
            card.hass = window.makeHass(opts);
            await new Promise((r) => setTimeout(r, 20));
            const sr = card.shadowRoot;
            const solarNode = sr.querySelector(".node.solar");
            return {
                fullText: sr.textContent || "",
                solarRendered: solarNode !== null,
                solarLabel: solarNode
                    ? (solarNode.querySelector(".node-label")?.textContent || "")
                    : "",
                solarValue: solarNode
                    ? (solarNode.querySelector(".node-value")?.textContent || "")
                    : "",
                solarIcon: solarNode
                    ? (solarNode.querySelector(".node-icon")?.textContent || "")
                    : "",
            };
        }""",
        {
            "pvPower": pv_power,
            "loadsPower": loads_power,
            "solarSeen": solar_seen,
            "boxes": boxes,
        },
    )
    return dict(result)


class TestOverviewCardSolarHiddenMode:
    """Overview card hides the solar box while the coordinator flag is
    False — any default 'gen load' rendering duplicates the House box
    and is not useful.  Power users can restore / repurpose the slot
    via the existing ``boxes:`` config (D-036).
    """

    def test_fresh_start_hides_solar_box(self, page: Page) -> None:
        """solar_seen=False with default config → solar box is NOT
        rendered at all.  The card's responsive grid handles the 3-box
        layout (House / Grid / Battery) via D-036.
        """
        _inject_overview_card(page)
        result = _mount_overview(
            page,
            pv_power=0.0,
            loads_power=1.2,
            solar_seen=False,
        )
        assert result["solarRendered"] is False, (
            "Solar box must be hidden when solar_seen=False with default "
            "config — showing the house load under a different label "
            "duplicates the existing House box.  "
            f"Got label: {result['solarLabel']!r}, value: {result['solarValue']!r}"
        )

    def test_explicit_solar_box_config_renders_even_when_hidden(
        self, page: Page
    ) -> None:
        """solar_seen=False BUT the user has listed a custom solar box
        with label/icon overrides in ``boxes:`` → the box renders using
        the user's config.  This is the escape hatch for users who
        want to repurpose the solar slot (e.g. for a generator sensor).
        """
        _inject_overview_card(page)
        result = _mount_overview(
            page,
            pv_power=0.0,
            loads_power=1.2,
            solar_seen=False,
            boxes=[
                {"type": "solar", "label": "Generator", "icon": "⚙"},
                "house",
                "grid",
                "battery",
            ],
        )
        assert result["solarRendered"] is True, (
            "Solar box must render when the user has explicitly "
            "overridden it in boxes: config, even if solar_seen=False."
        )
        assert "Generator" in result["solarLabel"], (
            "User-supplied label must be honoured.  "
            f"Got label: {result['solarLabel']!r}"
        )

    def test_after_solar_seen_shows_normal_solar(self, page: Page) -> None:
        """solar_seen=True → solar box renders normally with pvPower."""
        _inject_overview_card(page)
        result = _mount_overview(
            page,
            pv_power=3.5,
            loads_power=1.2,
            solar_seen=True,
        )
        assert "Solar" in result["solarLabel"], (
            "Solar box should show 'Solar' label when solar_seen=True. "
            f"Got label: {result['solarLabel']!r}"
        )
        assert "Gen Load" not in result["solarLabel"], (
            "Solar box should NOT show 'Gen Load' label after flag flip. "
            f"Got label: {result['solarLabel']!r}"
        )
        assert "3.50 kW" in result["solarValue"], (
            "Solar box should show the pvPower value (3.5 kW) in normal mode. "
            f"Got value: {result['solarValue']!r}"
        )

    def test_sticky_zero_pv_after_flag_shows_normal_solar(self, page: Page) -> None:
        """Once solar_seen=True, even a later pvPower=0 still renders
        the solar box normally (flag is sticky — user's flag contract
        is tested on the coordinator side; this verifies the card
        honours it).
        """
        _inject_overview_card(page)
        result = _mount_overview(
            page,
            pv_power=0.0,
            loads_power=2.5,
            solar_seen=True,
        )
        assert "Solar" in result["solarLabel"], (
            "Solar box should show 'Solar' even with pv=0 when solar_seen=True. "
            f"Got label: {result['solarLabel']!r}"
        )
        assert "Gen Load" not in result["solarLabel"], (
            "Solar box must not flip back to Gen Load once solar_seen=True. "
            f"Got label: {result['solarLabel']!r}"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
