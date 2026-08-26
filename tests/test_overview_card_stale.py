"""The overview card must look obviously stale when its data is not live.

Production report (2026-08-27): the dashboard showed ``API · 45m`` with
solar reading zero for 45 minutes while the integration was in fact
polling normally every 5 minutes — every poll ``success: True``, and the
server-side ``age_seconds`` never exceeded 300 s across 22 consecutive
intervals. The 45 minutes was the *browser*: the card computes its age as
``Date.now() - last_update`` client-side, so when the frontend loses its
WebSocket the page's ``last_update`` freezes while the clock keeps moving
and the displayed age grows without bound.

Two defects follow from that:

1. A disconnected frontend is presented as *data staleness*. The two need
   opposite responses — one is "check your browser/network", the other is
   "check the inverter or the cloud API" — and the card cannot tell them
   apart, so neither can the user.
2. The existing badge threshold is 30 s against a 300 s poll interval, so
   a perfectly healthy install renders the stale badge for roughly 90% of
   every interval. Crying wolf that reliably is the same as saying
   nothing: it is why ``API · 45m`` did not stand out from the ``API · 2m``
   that shows the rest of the time.

These tests drive the real card in a real Chromium DOM — the element is
registered by the shipped JS, fed a stub ``hass``, and inspected through
its shadow root — so they fail if the card's *behaviour* is wrong rather
than merely if its source text changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

_CARD_JS = Path("custom_components/foxess_control/www/foxess-overview-card.js")

# Poll cadences the integration actually runs at, so the thresholds under
# test are anchored to reality rather than to a number someone liked:
# REST polls every 300 s (DEFAULT_POLLING_INTERVAL), WS pushes every ~5 s.
_REST_INTERVAL = 300
_WS_INTERVAL = 5

_MOUNT = """
(args) => {
  const {connected, source, ageSeconds} = args;
  document.body.replaceChildren();
  const el = document.createElement("foxess-overview-card");
  el.setConfig({});
  // Pre-seed the entity map so the card never needs callWS: the setter
  // only fetches when the map is missing or empty.
  el._entityMap = {
    data_freshness: "sensor.foxess_data_freshness",
    solar_power: "sensor.foxess_solar_power",
    house_load: "sensor.foxess_house_load",
    battery_soc: "sensor.foxess_battery_soc",
  };
  const lastUpdate = new Date(Date.now() - ageSeconds * 1000).toISOString();
  el.hass = {
    connected: connected,
    language: "en",
    states: {
      "sensor.foxess_data_freshness": {
        state: source,
        attributes: {last_update: lastUpdate},
      },
      // Production polled sensors carry a data_source attribute, and that
      // is where the card reads the source from (_getDataSource walks the
      // role entities' attributes, not the freshness sensor's state).
      "sensor.foxess_solar_power": {state: "0.0", attributes: {data_source: source}},
      "sensor.foxess_house_load": {state: "2.9", attributes: {data_source: source}},
      "sensor.foxess_battery_soc": {state: "47", attributes: {data_source: source}},
    },
    callWS: async () => ({}),
  };
  document.body.appendChild(el);
  const root = el.shadowRoot;
  const card = root.querySelector("ha-card");
  const banner = root.querySelector(".stale-banner");
  const grid = root.querySelector(".flow-grid");
  return {
    cardClass: card ? card.getAttribute("class") || "" : null,
    stale: card ? card.classList.contains("stale") : null,
    bannerText: banner ? banner.textContent.trim() : null,
    gridOpacity: grid ? getComputedStyle(grid).opacity : null,
    gridFilter: grid ? getComputedStyle(grid).filter : null,
  };
}
"""


def _mount(
    page: Page, *, connected: bool, source: str = "api", age_seconds: int = 10
) -> dict[str, Any]:
    """Render the real card with a stub hass and report what it produced."""
    page.set_content("<html><body></body></html>")
    page.add_script_tag(path=str(_CARD_JS))
    result: dict[str, Any] = page.evaluate(
        _MOUNT,
        {"connected": connected, "source": source, "ageSeconds": age_seconds},
    )
    return result


class TestDisconnectedFrontendIsObviouslyStale:
    """``hass.connected === false`` must be shown, not silently aged."""

    def test_card_is_marked_stale(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60)
        assert r["stale"] is True, (
            "the frontend is disconnected, so every reading on the card is "
            f"frozen — the card must mark itself stale (class={r['cardClass']!r})"
        )

    def test_banner_says_the_connection_is_the_problem(self, page: Page) -> None:
        """The wording must send the user to the right place.

        A disconnected browser is not an inverter problem; telling the user
        their data is old would send them to check the wrong system.
        """
        r = _mount(page, connected=False, age_seconds=45 * 60)
        assert r["bannerText"], "no stale banner rendered while disconnected"
        text = r["bannerText"].lower()
        assert "connection" in text or "connected" in text, (
            f"banner must name the connection as the cause, got {r['bannerText']!r}"
        )

    def test_banner_still_reports_the_age(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60)
        assert "45" in (r["bannerText"] or ""), (
            f"banner should say how old the readings are, got {r['bannerText']!r}"
        )

    def test_disconnected_beats_a_fresh_age(self, page: Page) -> None:
        """Disconnected with a young age must still be stale.

        The age is computed from a value the page received *before* it lost
        contact, so a small number proves nothing.
        """
        r = _mount(page, connected=False, age_seconds=5)
        assert r["stale"] is True


class TestStaleTreatmentIsVisuallyObvious:
    """The requirement was CSS that makes staleness unmistakable."""

    def test_readings_are_dimmed_when_stale(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60)
        assert r["gridOpacity"] is not None, "no .flow-grid rendered"
        assert float(r["gridOpacity"]) < 0.8, (
            f"stale readings must be visibly dimmed, opacity={r['gridOpacity']}"
        )

    def test_readings_are_desaturated_when_stale(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60)
        assert r["gridFilter"] not in (None, "none"), (
            f"stale readings must be desaturated, filter={r['gridFilter']!r}"
        )

    def test_healthy_card_is_not_dimmed(self, page: Page) -> None:
        """The inverse: a live card must look completely normal."""
        r = _mount(page, connected=True, source="api", age_seconds=30)
        assert r["stale"] is False
        assert r["bannerText"] is None
        assert float(r["gridOpacity"]) == 1.0
        assert r["gridFilter"] in (None, "none")


class TestThresholdsMatchThePollCadence:
    """Stale must mean stale — not "a poll interval has elapsed"."""

    @pytest.mark.parametrize("age", [1, 60, _REST_INTERVAL, _REST_INTERVAL + 30])
    def test_api_mode_tolerates_a_normal_poll_interval(
        self, page: Page, age: int
    ) -> None:
        """This is the cry-wolf fix.

        With a 300 s poll interval, ages up to one interval (plus slack for
        an 8 s fetch) are entirely normal. The old 30 s threshold marked
        them stale, which is why the badge was ignored.
        """
        r = _mount(page, connected=True, source="api", age_seconds=age)
        assert r["stale"] is False, (
            f"age {age}s is normal for a {_REST_INTERVAL}s poll interval, but "
            f"the card called it stale"
        )

    def test_api_mode_is_stale_after_several_missed_polls(self, page: Page) -> None:
        r = _mount(
            page, connected=True, source="api", age_seconds=3 * _REST_INTERVAL + 60
        )
        assert r["stale"] is True, "three missed polls must register as stale"
        text = (r["bannerText"] or "").lower()
        assert "connection" not in text, (
            f"connected but stale data must not blame the connection, "
            f"got {r['bannerText']!r}"
        )

    def test_ws_mode_threshold_is_tighter_than_rest(self, page: Page) -> None:
        """WS pushes every ~5 s, so minutes of silence is already wrong."""
        fresh = _mount(page, connected=True, source="ws", age_seconds=_WS_INTERVAL * 3)
        assert fresh["stale"] is False
        stale = _mount(page, connected=True, source="ws", age_seconds=180)
        assert stale["stale"] is True, (
            "3 minutes without a WS frame is stale even though it would be "
            "normal for the REST poll"
        )
