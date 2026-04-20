"""Playwright browser tests for FoxESS Lovelace cards.

Run with: pytest tests/e2e/test_ui.py -m slow
Requires: podman, playwright (chromium), PyJWT
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError

from .conftest import set_inverter_state
from .ha_client import FATAL_FOR_ACTIVE
from .selectors import ControlCard, OverviewCard

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
SCREENSHOT_DIR = Path(__file__).parent / "screenshots" / _WORKER


def _robust_reload(page: Page, settle_ms: int = 1000) -> None:
    """Reload the page without the ``net::ERR_ABORTED`` race.

    ``page.reload()`` can throw when a previous navigation or frame
    detach is still in flight.  Using ``page.goto(page.url, ...)``
    is safer because it starts a *fresh* navigation and Playwright
    waits for the load event rather than racing against an existing
    one.  We also wait for ``networkidle`` so HA's WebSocket
    connection is re-established before tests interact with the DOM.
    """
    url = page.url
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except PlaywrightError:
        # Retry once — the first attempt can fail if a prior
        # navigation was still tearing down.
        page.goto(url, wait_until="networkidle", timeout=30000)
    if settle_ms > 0:
        page.wait_for_timeout(settle_ms)


def _wait_for_card_text(
    page: Page,
    locator: Locator,
    predicate: str,
    *,
    timeout: int = 15000,
) -> str:
    """Wait until *locator* has text matching *predicate* (substring).

    Returns the matched text content.  Raises on timeout.
    """
    from playwright.sync_api import expect

    expect(locator.first).to_contain_text(predicate, timeout=timeout)
    return locator.first.text_content() or ""


def _find_card(page: Page, tag: str, timeout: int = 30000) -> bool:
    """Check if a custom card element exists anywhere in the page DOM.

    HA nests custom cards deep inside shadow DOM hierarchies
    (home-assistant >>> home-assistant-main >>> ha-panel-lovelace >>>
    hui-root >>> hui-view >>> hui-card >>> {card}).
    Playwright's `>>>` pierce selector handles this.
    """
    try:
        page.wait_for_function(
            f"""() => {{
                // Deep search through all shadow roots
                function findInShadows(root, tag) {{
                    if (root.querySelector(tag)) return true;
                    for (const el of root.querySelectorAll('*')) {{
                        if (el.shadowRoot && findInShadows(el.shadowRoot, tag))
                            return true;
                    }}
                    return false;
                }}
                return findInShadows(document, '{tag}');
            }}""",
            timeout=timeout,
        )
        return True
    except (TimeoutError, PlaywrightError):
        return False


def _parse_power_kw(text: str) -> float:
    """Parse a formatted power string like '3.00 kW' or '500 W' to kW."""
    m = re.search(r"([\d.]+)\s*(kW|W)", text)
    if not m:
        msg = f"Cannot parse power from {text!r}"
        raise ValueError(msg)
    value = float(m.group(1))
    if m.group(2) == "W":
        value /= 1000
    return value


def _tight_window(minutes: int = 30) -> tuple[str, str]:
    """Return a tight window starting ~2 min before now (UTC).

    Avoids midnight crossings (C-009): clamps end to 23:59 and
    ensures start >= 00:00.  When ``now`` is near midnight the
    window shifts so the current minute always falls inside [start, end).
    """
    now = datetime.datetime.now(tz=datetime.UTC)
    now_min = now.hour * 60 + now.minute
    start_min = max(0, now_min - 2)
    end_min = start_min + minutes
    if end_min > 23 * 60 + 59:
        end_min = 23 * 60 + 59
        start_min = max(0, end_min - minutes)
    return (
        f"{start_min // 60:02d}:{start_min % 60:02d}:00",
        f"{end_min // 60:02d}:{end_min % 60:02d}:00",
    )


# ---------------------------------------------------------------------------
# Overview card tests
# ---------------------------------------------------------------------------


_JS_FIND_OVERVIEW_CARD = """
function findCard(root) {
    const c = root.querySelector('foxess-overview-card');
    if (c) return c;
    for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
            const f = findCard(el.shadowRoot);
            if (f) return f;
        }
    }
    return null;
}
"""


class TestOverviewCard:
    def test_card_renders(self, page: Page) -> None:
        """Overview card is present on the dashboard."""
        assert _find_card(page, "foxess-overview-card")

    def test_node_click_opens_more_info(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """Clicking a node fires hass-more-info with the correct entity."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
        _robust_reload(page, settle_ms=2000)

        results = page.evaluate(
            f"""() => {{
                {_JS_FIND_OVERVIEW_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const sr = card.shadowRoot;

                const captured = [];
                card.addEventListener('hass-more-info', (e) => {{
                    captured.push(e.detail.entityId);
                }});

                const nodes = sr.querySelectorAll('.node[data-entity]');
                const clicked = [];
                for (const node of nodes) {{
                    const entity = node.getAttribute('data-entity');
                    clicked.push(entity);
                    node.click();
                }}
                return {{ clicked, captured }};
            }}"""
        )
        assert results is not None, "Overview card not found"
        assert len(results["clicked"]) >= 3, (
            f"Expected at least 3 clickable nodes, got {len(results['clicked'])}"
        )
        assert results["clicked"] == results["captured"], (
            f"Events mismatch: clicked {results['clicked']}, "
            f"captured {results['captured']}"
        )

    def test_node_has_cursor_pointer(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """Clickable nodes show pointer cursor."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
        _robust_reload(page, settle_ms=2000)

        cursor = page.wait_for_function(
            f"""() => {{
                {_JS_FIND_OVERVIEW_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const node = card.shadowRoot.querySelector('.node[data-entity]');
                if (!node) return null;
                return getComputedStyle(node).cursor;
            }}""",
            timeout=10000,
        ).json_value()
        assert cursor == "pointer", f"Expected pointer cursor, got '{cursor}'"

    def test_sub_link_click_opens_more_info(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """Clicking a sub-detail (e.g. cell temp) opens that entity's history."""
        if connection_mode != "cloud":
            pytest.skip("sub-detail entities only present in cloud mode")
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
        _robust_reload(page, settle_ms=2000)

        results = page.evaluate(
            f"""() => {{
                {_JS_FIND_OVERVIEW_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const sr = card.shadowRoot;

                const captured = [];
                card.addEventListener('hass-more-info', (e) => {{
                    captured.push(e.detail.entityId);
                }});

                const links = sr.querySelectorAll('.sub-link[data-entity]');
                const clicked = [];
                for (const link of links) {{
                    const entity = link.getAttribute('data-entity');
                    clicked.push(entity);
                    link.click();
                }}
                return {{ clicked, captured }};
            }}"""
        )
        assert results is not None, "Overview card not found"
        assert len(results["clicked"]) >= 1, (
            f"Expected at least 1 clickable sub-link, got {len(results['clicked'])}"
        )
        assert results["clicked"] == results["captured"], (
            f"Sub-link events mismatch: clicked {results['clicked']}, "
            f"captured {results['captured']}"
        )

    def test_custom_boxes_hides_unconfigured(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """A card with boxes=[battery, solar] should not render grid or house."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
        _robust_reload(page, settle_ms=2000)

        node_types = page.wait_for_function(
            """() => {
                function findAllCards(root) {
                    const cards = [];
                    const c = root.querySelectorAll('foxess-overview-card');
                    cards.push(...c);
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            cards.push(...findAllCards(el.shadowRoot));
                        }
                    }
                    return cards;
                }
                const cards = findAllCards(document);
                if (cards.length < 2) return null;
                const card = cards[1];
                if (!card || !card.shadowRoot) return null;
                const nodes = card.shadowRoot.querySelectorAll('.node');
                const types = [];
                for (const node of nodes) {
                    if (node.classList.contains('battery')) types.push('battery');
                    else if (node.classList.contains('solar')) types.push('solar');
                    else if (node.classList.contains('house')) types.push('house');
                    else if (node.classList.contains('grid')) types.push('grid');
                }
                return types.length > 0 ? types : null;
            }""",
            timeout=10000,
        ).json_value()
        assert node_types is not None, "Second overview card not found"
        assert "battery" in node_types, "Battery node should be present"
        assert "solar" in node_types, "Solar node should be present"
        assert "house" not in node_types, "House node should not be present"
        assert "grid" not in node_types, "Grid node should not be present"

    def test_custom_boxes_label_override(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """A box with label override should display the custom label."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, solar_kw=1.0)
        _robust_reload(page, settle_ms=2000)

        label_text = page.wait_for_function(
            """() => {
                function findAllCards(root) {
                    const cards = [];
                    const c = root.querySelectorAll('foxess-overview-card');
                    cards.push(...c);
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            cards.push(...findAllCards(el.shadowRoot));
                        }
                    }
                    return cards;
                }
                const cards = findAllCards(document);
                if (cards.length < 2) return null;
                const card = cards[1];
                if (!card || !card.shadowRoot) return null;
                const solar = card.shadowRoot.querySelector('.node.solar .node-label');
                return solar ? solar.textContent : null;
            }""",
            timeout=10000,
        ).json_value()
        assert label_text is not None, "Solar node label not found"
        assert "PV" in label_text, f"Expected 'PV' in label, got '{label_text}'"

    def test_custom_boxes_order(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """Boxes render in the order specified in config (battery, then solar)."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
        _robust_reload(page, settle_ms=2000)

        order = page.wait_for_function(
            """() => {
                function findAllCards(root) {
                    const cards = [];
                    const c = root.querySelectorAll('foxess-overview-card');
                    cards.push(...c);
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            cards.push(...findAllCards(el.shadowRoot));
                        }
                    }
                    return cards;
                }
                const cards = findAllCards(document);
                if (cards.length < 2) return null;
                const card = cards[1];
                if (!card || !card.shadowRoot) return null;
                const nodes = card.shadowRoot.querySelectorAll('.node');
                const types = [];
                for (const node of nodes) {
                    if (node.classList.contains('battery')) types.push('battery');
                    else if (node.classList.contains('solar')) types.push('solar');
                    else if (node.classList.contains('house')) types.push('house');
                    else if (node.classList.contains('grid')) types.push('grid');
                }
                return types.length > 0 ? types : null;
            }""",
            timeout=10000,
        ).json_value()
        assert order is not None, "Second overview card not found"
        assert order == ["battery", "solar"], (
            f"Expected order ['battery', 'solar'], got {order}"
        )

    def test_default_config_renders_all_four_boxes(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """Card with no boxes config renders all four nodes in default order."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=60, load_kw=0.5)
        _robust_reload(page, settle_ms=2000)

        types = page.wait_for_function(
            f"""() => {{
                {_JS_FIND_OVERVIEW_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const nodes = card.shadowRoot.querySelectorAll('.node');
                if (nodes.length < 4) return null;
                const types = [];
                for (const node of nodes) {{
                    if (node.classList.contains('battery')) types.push('battery');
                    else if (node.classList.contains('solar')) types.push('solar');
                    else if (node.classList.contains('house')) types.push('house');
                    else if (node.classList.contains('grid')) types.push('grid');
                }}
                return types.length >= 4 ? types : null;
            }}""",
            timeout=10000,
        ).json_value()
        assert types is not None, "Overview card not found"
        assert types == ["solar", "house", "grid", "battery"], (
            f"Expected order ['solar', 'house', 'grid', 'battery'], got {types}"
        )

    def test_shows_soc(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """Battery SoC is displayed."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=75)
        _robust_reload(page)
        soc = page.locator(OverviewCard.BATTERY_SOC)
        if soc.count() > 0:
            _wait_for_card_text(page, soc, "75")
            text = soc.first.text_content() or ""
            assert "75" in text or "%" in text

    def test_house_load_never_greyed(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """House node should not be greyed out even at very low load."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, load_kw=0.003)
        _robust_reload(page, settle_ms=2000)
        house = page.locator(OverviewCard.HOUSE_NODE)
        if house.count() > 0:
            classes = house.get_attribute("class") or ""
            assert "inactive" not in classes

    def test_data_source_badge_matches_mode(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
        data_source: str,
    ) -> None:
        """Data source badge reflects the active data path (cloud only)."""
        if connection_mode != "cloud":
            pytest.skip("data source badge is cloud-specific")
        assert foxess_sim is not None
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)
        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        # WS needs time to: web login → discover plantId → connect
        # WS → receive first message → coordinator sets data_source.
        ha_e2e.wait_for_attribute(
            "sensor.foxess_solar_power",
            "data_source",
            data_source,
            timeout_s=90,
        )
        _robust_reload(page)

        badge_text = page.wait_for_function(
            """() => {
                function findCard(root) {
                    const c = root.querySelector(
                        'foxess-overview-card'
                    );
                    if (c) return c;
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            const f = findCard(el.shadowRoot);
                            if (f) return f;
                        }
                    }
                    return null;
                }
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const badge = card.shadowRoot.querySelector(
                    '.data-source'
                );
                return badge ? badge.textContent : null;
            }""",
            timeout=10000,
        ).json_value()
        expected = data_source.upper()
        assert badge_text is not None and badge_text.startswith(expected), (
            f"Badge shows '{badge_text}', expected to start with '{expected}'"
        )

    def test_stale_badge_shown_for_old_api_data(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        data_source: str,
        connection_mode: str,
    ) -> None:
        """Badge turns amber (stale class) when API data exceeds 10s age."""
        if connection_mode != "cloud":
            pytest.skip("data freshness badge is cloud-specific")
        if data_source != "api":
            pytest.skip("staleness indicator only relevant for API mode")
        assert foxess_sim is not None
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)
        ha_e2e.wait_for_attribute(
            "sensor.foxess_solar_power",
            "data_source",
            "api",
            timeout_s=60,
        )
        _robust_reload(page)
        # Genuinely time-dependent: card JS compares Date.now() to
        # last_updated — data must physically age past the 10s threshold.
        page.wait_for_timeout(32000)
        _robust_reload(page)

        badge_info = page.wait_for_function(
            """() => {
                function findCard(root) {
                    const c = root.querySelector('foxess-overview-card');
                    if (c) return c;
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            const f = findCard(el.shadowRoot);
                            if (f) return f;
                        }
                    }
                    return null;
                }
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const badge = card.shadowRoot.querySelector('.data-source');
                if (!badge) return null;
                return {
                    text: badge.textContent,
                    classes: badge.className,
                };
            }""",
            timeout=10000,
        ).json_value()
        assert badge_info is not None, "data-source badge not found"
        assert "stale" in badge_info["classes"], (
            f"Badge should have 'stale' class after 32s, got: {badge_info}"
        )
        assert "API" in badge_info["text"], (
            f"Badge should contain 'API', got: {badge_info['text']}"
        )

    def test_pv_values_consistent_with_solar_total(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        data_source: str,
        connection_mode: str,
    ) -> None:
        """PV1 + PV2 ≈ solar total on overview card (cloud only)."""
        if connection_mode != "cloud":
            pytest.skip("PV1/PV2 entities don't exist in entity mode")
        assert foxess_sim is not None
        # PV sensors are disabled by default — enable them and reload.
        for eid in ("sensor.foxess_pv1_power", "sensor.foxess_pv2_power"):
            ha_e2e.enable_entity(eid)
        foxess_sim.set(soc=80, solar_kw=3.0, load_kw=0.5)
        ha_e2e.reload_integration()
        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_solar_power", "ge", 2.5, timeout_s=120
        )
        _robust_reload(page, settle_ms=3000)

        # Extract solar node text from deep shadow DOM
        solar_texts = page.wait_for_function(
            """() => {
                function findCard(root) {
                    const card = root.querySelector(
                        'foxess-overview-card'
                    );
                    if (card) return card;
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            const found = findCard(el.shadowRoot);
                            if (found) return found;
                        }
                    }
                    return null;
                }
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const sr = card.shadowRoot;
                const solar = sr.querySelector('.node.solar');
                if (!solar) return null;
                const value = solar.querySelector('.node-value');
                const sub = solar.querySelector('.node-sub');
                return {
                    total: value ? value.textContent : null,
                    detail: sub ? sub.textContent : null,
                };
            }""",
            timeout=10000,
        ).json_value()
        assert solar_texts, "Solar node not found in overview card"
        assert solar_texts["total"], "Solar total not displayed"

        assert solar_texts["detail"], "PV detail not displayed"
        total_kw = _parse_power_kw(solar_texts["total"])
        pv_parts = solar_texts["detail"].split("·")
        pv_sum = sum(_parse_power_kw(part) for part in pv_parts)
        assert abs(pv_sum - total_kw) < 0.15, (
            f"PV sum {pv_sum:.3f} kW != solar total {total_kw:.3f} kW"
        )


# ---------------------------------------------------------------------------
# Control card tests
# ---------------------------------------------------------------------------


class TestControlCard:
    def test_card_renders(self, page: Page) -> None:
        """Control card is present on the dashboard."""
        assert _find_card(page, "foxess-control-card")

    def test_soc_displayed(self, page: Page) -> None:
        """SoC percentage is shown in the header."""
        soc = page.locator(ControlCard.SOC_TEXT)
        if soc.count() > 0:
            text = soc.text_content() or ""
            assert "%" in text

    def test_progress_hidden_when_idle(self, page: Page) -> None:
        """No progress section when no session is active."""
        progress = page.locator(ControlCard.PROGRESS_SECTION)
        # Should either not exist or not be visible
        assert progress.count() == 0 or not progress.is_visible()

    def test_progress_visible_during_discharge(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        data_source: str,
        connection_mode: str,
    ) -> None:
        """Progress section appears during active discharge."""
        set_inverter_state(connection_mode, foxess_sim, ha_e2e, soc=80, load_kw=0.5)
        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        _robust_reload(page, settle_ms=3000)

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "discharge-progress.png"))

        # Poll until the card renders a progress section — the card
        # may take several seconds to re-render after discharge starts,
        # especially in the WS data-source variant.
        page.wait_for_function(
            """() => {
                function findCard(root) {
                    const card = root.querySelector('foxess-control-card');
                    if (card) return card;
                    for (const child of root.querySelectorAll('*')) {
                        if (child.shadowRoot) {
                            const found = findCard(child.shadowRoot);
                            if (found) return found;
                        }
                    }
                    return null;
                }
                const card = findCard(document);
                if (!card || !card.shadowRoot) return false;
                return !!card.shadowRoot.querySelector('.progress-section');
            }""",
            timeout=30000,
        )

    def test_schedule_horizon_during_discharge(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Schedule horizon attribute is set and marker renders on card."""
        if connection_mode != "cloud":
            pytest.skip("progressive schedule extension is cloud-adapter only")
        # SoC=55/min=30 gives ~15 min discharge at max power (no deferral)
        # but horizon = 15/1.5 = ~10 min < 12 min window (marker visible).
        assert foxess_sim is not None
        set_inverter_state("cloud", foxess_sim, ha_e2e, soc=55, solar_kw=0, load_kw=0.5)
        ha_e2e.wait_for_numeric_state(
            "sensor.foxess_battery_soc", "le", 56, timeout_s=120
        )
        start, end = _tight_window(12)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )

        # Wait for the first listener tick to set the horizon
        # (apply_mode runs on power adjustments, ~60s tick interval)
        import time as _time

        deadline = _time.monotonic() + 90
        horizon = None
        while _time.monotonic() < deadline:
            attrs = ha_e2e.get_attributes("sensor.foxess_smart_operations")
            horizon = attrs.get("discharge_schedule_horizon")
            if horizon:
                break
            _time.sleep(2)
        assert horizon, "discharge_schedule_horizon not set within 90s"
        assert "T" in horizon, f"Expected ISO timestamp, got: {horizon}"

        # Verify the horizon is between now and the session end
        end_time = attrs.get("discharge_end_time")
        assert horizon < end_time, (
            f"Horizon {horizon} should be before session end {end_time}"
        )

        # Verify the marker renders on the card
        _robust_reload(page)

        has_marker = page.wait_for_function(
            """() => {
                function findCard(root) {
                    const card = root.querySelector(
                        'foxess-control-card'
                    );
                    if (card) return card;
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) {
                            const f = findCard(el.shadowRoot);
                            if (f) return f;
                        }
                    }
                    return null;
                }
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const marker = card.shadowRoot.querySelector(
                    '.horizon-marker'
                );
                if (!marker) return null;
                return {
                    left: marker.style.left,
                    visible: marker.offsetWidth > 0,
                };
            }""",
            timeout=10000,
        ).json_value()
        assert has_marker, "Horizon marker not found in control card"
        assert has_marker["left"], "Horizon marker has no position"


# ---------------------------------------------------------------------------
# Form input persistence tests (C-020: operational transparency)
# ---------------------------------------------------------------------------

_JS_FIND_CONTROL_CARD = """
function findCard(root) {
    const card = root.querySelector('foxess-control-card');
    if (card) return card;
    for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
            const f = findCard(el.shadowRoot);
            if (f) return f;
        }
    }
    return null;
}
"""


class TestFormInputPersistence:
    """Verify form inputs survive card re-renders (hass property updates)."""

    # Track which form type is expected, so _safe_evaluate can re-open
    # the form after a page navigation destroys the execution context.
    _current_form_action: str | None = None

    def _recover_form(self, page: Page, *, _depth: int = 0) -> None:
        """Wait for the page and card to be ready, then re-open the form.

        If a second navigation destroys the execution context during
        recovery (e.g. HA rapid-fire WebSocket reconnections), this
        method retries up to ``_MAX_RECOVER_DEPTH`` times.  Each retry
        waits for the *new* navigation to finish (``networkidle``)
        before attempting to find the card and re-open the form.
        """
        _MAX_RECOVER_DEPTH = 3
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
            _find_card(page, "foxess-control-card")
            if self._current_form_action:
                page.evaluate(
                    f"""() => {{
                        {_JS_FIND_CONTROL_CARD}
                        const card = findCard(document);
                        if (!card || !card.shadowRoot) return;
                        const btn = card.shadowRoot.querySelector(
                            '[data-action="{self._current_form_action}"]'
                        );
                        if (btn) btn.click();
                    }}"""
                )
        except PlaywrightError as exc:
            if (
                "Execution context was destroyed" not in str(exc)
                or _depth >= _MAX_RECOVER_DEPTH
            ):
                raise
            # Another navigation hit during recovery — recurse with
            # incremented depth to wait for the new page to settle.
            self._recover_form(page, _depth=_depth + 1)

    def _safe_evaluate(  # type: ignore[return]
        self, page: Page, expression: str, *, retries: int = 2
    ) -> object:
        """Run page.evaluate with retry on navigation-induced context destruction.

        HA can trigger a full page navigation (WebSocket reconnect, dashboard
        auto-refresh) between Playwright calls, destroying the JS execution
        context.  When that happens, wait for the page to settle, re-open
        the form if one was expected, and retry.
        """
        for attempt in range(retries + 1):
            try:
                return page.evaluate(expression)
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc):
                    raise
                if attempt == retries:
                    raise
                # Page navigated — wait for it to settle, then
                # re-open the form (navigation resets card state).
                self._recover_form(page)
                self._wait_for_form(page)

    def _open_form(self, page: Page, action: str) -> None:
        """Click a button on the control card to open the form overlay.

        Waits for the form element to appear after clicking, so callers
        do not need a separate ``_wait_for_form`` call.  If a navigation
        destroys the context between the click and the form appearing,
        the retry in ``_wait_for_form`` handles it.
        """
        self._current_form_action = action
        self._safe_evaluate(
            page,
            f"""() => {{
                {_JS_FIND_CONTROL_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return;
                const btn = card.shadowRoot.querySelector(
                    '[data-action="{action}"]'
                );
                if (btn) btn.click();
            }}""",
        )
        self._wait_for_form(page)

    def _get_form_values(self, page: Page) -> dict[str, str]:
        """Read form-start, form-end, form-soc values from the control card."""
        result: dict[str, str] = self._safe_evaluate(  # type: ignore[assignment]
            page,
            f"""() => {{
                {_JS_FIND_CONTROL_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const sr = card.shadowRoot;
                const start = sr.getElementById('form-start');
                const end = sr.getElementById('form-end');
                const soc = sr.getElementById('form-soc');
                return {{
                    start: start ? start.value : null,
                    end: end ? end.value : null,
                    soc: soc ? soc.value : null,
                }};
            }}""",
        )
        return result

    def _set_form_value(self, page: Page, input_id: str, value: str) -> None:
        """Set the value of a form input inside the control card shadow DOM.

        Returns after the value is confirmed set.  If the form is not
        open (e.g. after a page navigation closed it), re-opens the form
        and retries.
        """
        found = self._safe_evaluate(
            page,
            f"""() => {{
                {_JS_FIND_CONTROL_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return false;
                const input = card.shadowRoot.getElementById('{input_id}');
                if (!input) return false;
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeSetter.call(input, '{value}');
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}""",
        )
        if not found and self._current_form_action:
            # Form was closed (e.g. by a navigation that completed
            # without triggering a context error).  Re-open and retry.
            self._recover_form(page)
            self._wait_for_form(page)
            self._safe_evaluate(
                page,
                f"""() => {{
                    {_JS_FIND_CONTROL_CARD}
                    const card = findCard(document);
                    if (!card || !card.shadowRoot) return;
                    const input = card.shadowRoot.getElementById(
                        '{input_id}'
                    );
                    if (!input) return;
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeSetter.call(input, '{value}');
                    input.dispatchEvent(
                        new Event('input', {{ bubbles: true }})
                    );
                    input.dispatchEvent(
                        new Event('change', {{ bubbles: true }})
                    );
                }}""",
            )

    def _trigger_hass_update(self, page: Page) -> None:
        """Simulate HA pushing a state update by re-setting the hass property."""
        self._safe_evaluate(
            page,
            f"""() => {{
                {_JS_FIND_CONTROL_CARD}
                const card = findCard(document);
                if (card && card._hass) {{
                    card.hass = card._hass;
                }}
            }}""",
        )

    def _wait_for_form(self, page: Page) -> None:
        for attempt in range(3):
            try:
                page.wait_for_function(
                    f"""() => {{
                        {_JS_FIND_CONTROL_CARD}
                        const card = findCard(document);
                        if (!card || !card.shadowRoot) return false;
                        return !!card.shadowRoot.getElementById('form-start');
                    }}""",
                    timeout=10000,
                )
                return
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc):
                    raise
                if attempt == 2:
                    raise
                # Page navigated — recover card + form
                self._recover_form(page)

    def test_time_input_survives_rerender(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Time selector value persists after card re-render (C-020).

        Regression: the time <input type="time"> resets to empty when
        the card re-renders due to a hass property update (~5s with WS).
        """
        assert _find_card(page, "foxess-control-card")

        self._open_form(page, "charge")

        self._set_form_value(page, "form-start", "02:30")
        self._set_form_value(page, "form-end", "06:45")
        self._set_form_value(page, "form-soc", "85")

        vals_before = self._get_form_values(page)
        assert vals_before["start"] == "02:30", (
            f"Start time not set correctly: {vals_before}"
        )
        assert vals_before["end"] == "06:45", (
            f"End time not set correctly: {vals_before}"
        )

        self._trigger_hass_update(page)
        self._wait_for_form(page)

        vals_after = self._get_form_values(page)
        assert vals_after["start"] == "02:30", (
            f"Time input RESET after re-render: start was '02:30', "
            f"now '{vals_after['start']}'"
        )
        assert vals_after["end"] == "06:45", (
            f"Time input RESET after re-render: end was '06:45', "
            f"now '{vals_after['end']}'"
        )
        assert vals_after["soc"] == "85", (
            f"SoC input RESET after re-render: soc was '85', now '{vals_after['soc']}'"
        )

    def test_time_input_survives_multiple_rerenders(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Form values persist through multiple rapid state updates."""
        assert _find_card(page, "foxess-control-card")

        self._open_form(page, "discharge")

        self._set_form_value(page, "form-start", "14:00")
        self._set_form_value(page, "form-end", "18:30")
        self._set_form_value(page, "form-soc", "20")

        for _ in range(3):
            self._trigger_hass_update(page)

        self._wait_for_form(page)

        vals = self._get_form_values(page)
        assert vals["start"] == "14:00", (
            f"Start time lost after 3 re-renders: '{vals['start']}'"
        )
        assert vals["end"] == "18:30", (
            f"End time lost after 3 re-renders: '{vals['end']}'"
        )
        assert vals["soc"] == "20", f"SoC lost after 3 re-renders: '{vals['soc']}'"

    def test_rerender_between_field_edits(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """A re-render between editing different fields preserves earlier values.

        Scenario: user sets start time, hass update fires, user then sets
        end time — the start time must still be present.
        """
        assert _find_card(page, "foxess-control-card")

        self._open_form(page, "charge")

        # Set only start time
        self._set_form_value(page, "form-start", "03:15")

        # Re-render fires before user fills the next field
        self._trigger_hass_update(page)
        self._wait_for_form(page)

        # Now set end time
        self._set_form_value(page, "form-end", "07:00")

        # Another re-render
        self._trigger_hass_update(page)
        self._wait_for_form(page)

        vals = self._get_form_values(page)
        assert vals["start"] == "03:15", (
            f"Start time lost after interleaved re-render: '{vals['start']}'"
        )
        assert vals["end"] == "07:00", (
            f"End time lost after interleaved re-render: '{vals['end']}'"
        )

    def test_time_picker_stays_open_during_rerender(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Focused time input keeps focus and DOM identity through re-render.

        Regression: the form overlay was rebuilt via innerHTML on every hass
        update, destroying native time picker popups and losing focus.  With
        targeted DOM updates, the form-overlay element must survive intact.
        """
        assert _find_card(page, "foxess-control-card")

        self._open_form(page, "charge")
        self._set_form_value(page, "form-start", "02:30")

        # Mark the DOM element with a sentinel and focus it
        self._safe_evaluate(
            page,
            f"""() => {{
                {_JS_FIND_CONTROL_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return;
                const input = card.shadowRoot.getElementById('form-start');
                if (input) {{
                    input._sentinel = 'alive';
                    input.focus();
                }}
            }}""",
        )

        # Verify focus before the update
        focused_before = self._safe_evaluate(
            page,
            f"""() => {{
                {_JS_FIND_CONTROL_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return false;
                const input = card.shadowRoot.getElementById('form-start');
                return input === card.shadowRoot.activeElement;
            }}""",
        )
        assert focused_before, "form-start should be focused before hass update"

        # Trigger hass update (re-render)
        self._trigger_hass_update(page)
        self._wait_for_form(page)

        # DOM element must be the SAME node (sentinel survives)
        sentinel = self._safe_evaluate(
            page,
            f"""() => {{
                {_JS_FIND_CONTROL_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return null;
                const input = card.shadowRoot.getElementById('form-start');
                return input ? input._sentinel : null;
            }}""",
        )
        assert sentinel == "alive", (
            f"DOM element was destroyed and recreated (sentinel={sentinel})"
        )

        # Focus must be preserved
        focused_after = self._safe_evaluate(
            page,
            f"""() => {{
                {_JS_FIND_CONTROL_CARD}
                const card = findCard(document);
                if (!card || !card.shadowRoot) return false;
                const input = card.shadowRoot.getElementById('form-start');
                return input === card.shadowRoot.activeElement;
            }}""",
        )
        assert focused_after, "form-start lost focus after hass update"

        # Value must also survive
        vals = self._get_form_values(page)
        assert vals["start"] == "02:30", f"Start value lost: '{vals['start']}'"

    def test_form_recovers_from_page_navigation(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Form interaction recovers after a page navigation closes the form.

        Regression: HA can navigate the page (WebSocket reconnect, dashboard
        auto-refresh) between _open_form and _set_form_value, destroying the
        JS execution context.  The recovery infrastructure (_safe_evaluate,
        _recover_form, _set_form_value's ``if not found`` path) must detect
        the closed form, re-open it, and set values.

        This test opens the form, reloads the page (simulating HA
        navigation), then verifies _set_form_value recovers.  Without
        recovery, values silently fail to be set.
        """
        assert _find_card(page, "foxess-control-card")

        self._open_form(page, "discharge")

        # Simulate HA navigating: reload destroys the form overlay.
        _robust_reload(page)

        # _set_form_value must detect the form is closed, re-open it
        # via _recover_form, and set the value.
        self._set_form_value(page, "form-start", "14:00")
        self._set_form_value(page, "form-end", "18:30")
        self._set_form_value(page, "form-soc", "20")

        vals = self._get_form_values(page)
        assert vals["start"] == "14:00", (
            f"Start time not set after navigation recovery: '{vals['start']}'"
        )
        assert vals["end"] == "18:30", (
            f"End time not set after navigation recovery: '{vals['end']}'"
        )
        assert vals["soc"] == "20", (
            f"SoC not set after navigation recovery: '{vals['soc']}'"
        )


# ---------------------------------------------------------------------------
# Screenshot regression
# ---------------------------------------------------------------------------


class TestScreenshots:
    def test_idle_screenshot(
        self,
        page: Page,
        foxess_sim: SimulatorHandle | None,
        ha_e2e: HAClient,
        connection_mode: str,
    ) -> None:
        """Capture idle state for visual regression review."""
        set_inverter_state(
            connection_mode, foxess_sim, ha_e2e, soc=60, solar_kw=2.0, load_kw=0.5
        )
        _robust_reload(page, settle_ms=2000)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "idle.png"))

    def test_discharging_screenshot(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle | None,
        connection_mode: str,
    ) -> None:
        """Capture discharging state for visual regression review."""
        set_inverter_state(
            connection_mode, foxess_sim, ha_e2e, soc=80, solar_kw=1.0, load_kw=0.8
        )
        start, end = _tight_window(10)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
            fatal_states=FATAL_FOR_ACTIVE,
        )
        _robust_reload(page, settle_ms=2000)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "discharging.png"))
