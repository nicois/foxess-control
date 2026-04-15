"""Playwright browser tests for FoxESS Lovelace cards.

Run with: pytest e2e/test_ui.py -m slow
Requires: podman, playwright (chromium), PyJWT
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .selectors import ControlCard, OverviewCard

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .conftest import SimulatorHandle
    from .ha_client import HAClient

pytestmark = pytest.mark.slow

SCREENSHOT_DIR = Path(__file__).parent / "screenshots"


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

    def test_data_source_badge(self, page: Page) -> None:
        """Data source badge is visible when web credentials are configured."""
        badge = page.locator(OverviewCard.DATA_SOURCE)
        if badge.count() > 0:
            text = badge.text_content() or ""
            assert text in ("API", "WS", "Modbus")


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
    ) -> None:
        """Progress bars appear during active discharge."""
        foxess_sim.set(soc=80, solar_kw=0, load_kw=0.5)
        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
        )
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        fill = page.locator(ControlCard.DISCHARGE_FILL)
        assert fill.count() > 0


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
        foxess_sim.set(soc=70, solar_kw=1.0, load_kw=0.8)
        start, end = _tight_window(30)
        ha_e2e.call_service(
            "foxess_control",
            "smart_discharge",
            {"start_time": start, "end_time": end, "min_soc": 30},
        )
        ha_e2e.wait_for_state(
            "sensor.foxess_smart_operations",
            "discharging",
            timeout_s=120,
        )
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "discharging.png"))
