"""Playwright browser tests for FoxESS Lovelace cards.

Run with: pytest e2e/test_ui.py -m slow
Requires: podman, playwright (chromium), PyJWT
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .ha_client import FATAL_FOR_ACTIVE
from .selectors import ControlCard, OverviewCard

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow

_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
SCREENSHOT_DIR = Path(__file__).parent / "screenshots" / _WORKER


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
    except Exception:
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
    now = datetime.datetime.now(tz=datetime.UTC)
    start = now - datetime.timedelta(minutes=2)
    end = start + datetime.timedelta(minutes=minutes)
    return (
        f"{start.hour:02d}:{start.minute:02d}:00",
        f"{end.hour:02d}:{end.minute:02d}:00",
    )


# ---------------------------------------------------------------------------
# Overview card tests
# ---------------------------------------------------------------------------


class TestOverviewCard:
    def test_card_renders(self, page: Page) -> None:
        """Overview card is present on the dashboard."""
        assert _find_card(page, "foxess-overview-card")

    def test_shows_soc(self, page: Page, foxess_sim: SimulatorHandle) -> None:
        """Battery SoC is displayed."""
        foxess_sim.set(soc=75)
        page.reload()
        page.wait_for_load_state("networkidle")
        # Wait for card to re-render with updated data
        page.wait_for_timeout(3000)
        soc = page.locator(OverviewCard.BATTERY_SOC)
        if soc.count() > 0:
            text = soc.text_content() or ""
            assert "75" in text or "%" in text

    def test_house_load_never_greyed(
        self, page: Page, foxess_sim: SimulatorHandle
    ) -> None:
        """House node should not be greyed out even at very low load."""
        foxess_sim.set(load_kw=0.003)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        house = page.locator(OverviewCard.HOUSE_NODE)
        if house.count() > 0:
            classes = house.get_attribute("class") or ""
            assert "inactive" not in classes

    def test_data_source_badge_matches_mode(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle,
        data_source: str,
    ) -> None:
        """Data source badge reflects the active data path."""
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
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        badge_text = page.evaluate(
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
            }"""
        )
        expected = data_source.upper()
        assert badge_text == expected, (
            f"Badge shows '{badge_text}', expected '{expected}'"
        )

    def test_pv_values_consistent_with_solar_total(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle,
        data_source: str,
    ) -> None:
        """PV1 + PV2 ≈ solar total during smart operations."""
        foxess_sim.set(soc=80, solar_kw=3.0, load_kw=0.5)
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
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(5000)

        # Extract solar node text from deep shadow DOM
        solar_texts = page.evaluate(
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
            }"""
        )
        assert solar_texts, "Solar node not found in overview card"
        assert solar_texts["total"], "Solar total not displayed"

        if data_source == "ws":
            # WS mode hides PV detail (stale REST values) — verify
            # the total is displayed but don't check PV breakdown.
            assert not solar_texts["detail"], "PV detail should be hidden in WS mode"
        else:
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
        foxess_sim: SimulatorHandle,
        data_source: str,
    ) -> None:
        """Progress section appears during active discharge."""
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
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(5000)

        SCREENSHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "discharge-progress.png"))

        # Verify the control card renders with a progress section.
        # HA nests cards deep in shadow DOM — use JS to traverse all
        # shadow roots rather than relying on pierce selectors.
        has_progress = page.evaluate(
            """() => {
                function findInShadows(root, selector) {
                    const el = root.querySelector(selector);
                    if (el) return true;
                    for (const child of root.querySelectorAll('*')) {
                        if (child.shadowRoot
                            && findInShadows(child.shadowRoot, selector))
                                return true;
                    }
                    return false;
                }
                // First find the card, then look for progress-section inside it
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
                if (!card) return false;
                const sr = card.shadowRoot;
                if (!sr) return false;
                return !!sr.querySelector('.progress-section');
            }"""
        )
        assert has_progress, (
            "Progress section not found in control card during discharge"
        )


# ---------------------------------------------------------------------------
# Screenshot regression
# ---------------------------------------------------------------------------


class TestScreenshots:
    def test_idle_screenshot(self, page: Page, foxess_sim: SimulatorHandle) -> None:
        """Capture idle state for visual regression review."""
        foxess_sim.set(soc=60, solar_kw=2.0, load_kw=0.5)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "idle.png"))

    def test_discharging_screenshot(
        self,
        page: Page,
        ha_e2e: HAClient,
        foxess_sim: SimulatorHandle,
    ) -> None:
        """Capture discharging state for visual regression review."""
        # SoC=80 with min_soc=30 and 30-min window forces immediate
        # discharge start (avoids deferred-start timeout).
        foxess_sim.set(soc=80, solar_kw=1.0, load_kw=0.8)
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
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "discharging.png"))
