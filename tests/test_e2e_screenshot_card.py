"""Tests for the E2E gallery-screenshot helper's detached-DOM retry.

``TestGalleryScreenshots._screenshot_card`` captures individual cards
for README visual regression.  The helper's three-step sequence is:

1. ``page.wait_for_function`` — confirm the element is in the shadow-DOM
   tree.
2. ``page.locator(f"{tag} >>> ha-card").first`` — resolve to a locator.
3. ``card.screenshot(path=...)`` — capture the bytes.

Between step 2 and step 3, HA's Lovelace can re-render (WebSocket update,
auth refresh, dashboard router churn) and detach the ``ha-card`` handle
that ``.screenshot()`` then tries to use.  Playwright surfaces this as
``PlaywrightError("Locator.screenshot: Element is not attached to the
DOM")`` — observed on Flaky Test Detection run 24994690563 against
``test_gallery_control_charging[entity]``.

The fix is a retry wrapper: on ``Element is not attached to the DOM`` /
``Execution context was destroyed`` / ``Target closed``, wait for
``networkidle``, re-resolve the locator, and retry — mirroring
``_safe_evaluate`` and ``_find_card``.

These tests avoid a full Playwright browser: they stub ``page.locator``
and the returned ``Locator.screenshot`` to simulate the CI race
deterministically.

Symptoms reproduced:

- ``PlaywrightError: Locator.screenshot: Element is not attached to the
  DOM`` raised by the first ``.screenshot()`` call, recovered by a
  fresh locator on the second attempt.
- ``PlaywrightError: Execution context was destroyed`` and ``Target
  closed`` — other flavours of the same navigation-induced race.
- Unrelated Playwright errors (e.g. a generic ``Timeout``) must
  propagate without being retried.
- Boundary: when all retries are exhausted, the last detach error must
  propagate with its original message preserved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError

if TYPE_CHECKING:
    from pathlib import Path


def _get_helper() -> Any:
    """Import the module-level helper.  Raises ImportError if not yet defined.

    Keeping the import inside a function avoids a module-level
    ImportError that would break test collection entirely — we want a
    clean per-test failure when the helper is missing.

    Uses ``getattr`` (not attribute access) so mypy does not fail
    pre-commit while the helper is being introduced in a separate
    commit from the tests.
    """
    from tests.e2e import test_ui  # noqa: PLC0415

    _sentinel = object()
    helper = getattr(test_ui, "_safe_screenshot", _sentinel)
    if helper is _sentinel:
        msg = (
            "tests.e2e.test_ui._safe_screenshot is not defined. "
            "The _screenshot_card helper's screenshot step must be "
            "extracted into a module-level wrapper with "
            "retry-on-detached-element semantics."
        )
        raise ImportError(msg)
    return helper


def _make_page(screenshot_side_effects: list[Any]) -> MagicMock:
    """Build a MagicMock Page whose ``locator(...).first.screenshot(...)``
    returns/raises the supplied side-effects in order.

    Each call to ``page.locator()`` returns a fresh locator whose
    ``.first.screenshot`` pops the next side-effect.  This lets tests
    assert that the helper *re-resolves the locator* (rather than
    caching a stale handle) between retries.
    """
    page = MagicMock()
    # Index into the shared side_effects list, so successive locators
    # return the next value.
    page._screenshot_idx = 0

    def _next_locator(*_args: Any, **_kwargs: Any) -> MagicMock:
        locator = MagicMock()

        def _screenshot(*_a: Any, **_k: Any) -> None:
            idx = page._screenshot_idx
            page._screenshot_idx = idx + 1
            if idx >= len(screenshot_side_effects):
                msg = (
                    f"Unexpected screenshot call #{idx + 1}; "
                    f"only {len(screenshot_side_effects)} side-effects queued"
                )
                raise AssertionError(msg)
            effect = screenshot_side_effects[idx]
            if isinstance(effect, BaseException):
                raise effect
            # Success — return nothing (matches Playwright API).

        locator.first.screenshot.side_effect = _screenshot
        return locator

    page.locator.side_effect = _next_locator
    page.wait_for_load_state.return_value = None
    return page


class TestSafeScreenshotRetries:
    """The helper must retry when the element detaches between
    locator-resolve and screenshot.

    Under CI load, HA fires background re-renders that replace the
    ``ha-card`` element between the two steps.  Without retry, a single
    such re-render during gallery capture fails the test — even though
    the next re-resolve would succeed immediately.
    """

    def test_retries_on_detached_element(self, tmp_path: Path) -> None:
        """First screenshot raises "not attached"; second succeeds.

        The helper must (a) catch the detach error, (b) re-resolve the
        locator via a second ``page.locator(...)`` call, and (c) retry
        the screenshot.
        """
        helper = _get_helper()
        page = _make_page(
            [
                PlaywrightError(
                    "Locator.screenshot: Element is not attached to the DOM"
                ),
                None,  # success
            ]
        )

        helper(page, "foxess-control-card", tmp_path / "card.png")

        # Detach-and-recover: the helper called screenshot twice AND
        # re-resolved the locator each time (not cached once).
        assert page._screenshot_idx == 2, (
            f"Expected 2 screenshot attempts (1 detach + 1 success); "
            f"got {page._screenshot_idx}"
        )
        assert page.locator.call_count >= 2, (
            f"Expected helper to re-resolve the locator between retries; "
            f"page.locator called {page.locator.call_count} times"
        )
        # wait_for_load_state('networkidle') used to settle post-detach
        # before the retry — matches _safe_evaluate's recovery pattern.
        assert page.wait_for_load_state.called, (
            "Expected wait_for_load_state('networkidle') between retries"
        )

    def test_retries_on_execution_context_destroyed(self, tmp_path: Path) -> None:
        """Context-destroyed is another flavour of the same race.

        When HA navigates mid-screenshot, Playwright raises "Execution
        context was destroyed" instead of "not attached" — same root
        cause (DOM swap), different error string.  Must retry.
        """
        helper = _get_helper()
        page = _make_page(
            [
                PlaywrightError(
                    "Locator.screenshot: Execution context was destroyed, "
                    "most likely because of a navigation"
                ),
                None,  # success
            ]
        )

        helper(page, "foxess-overview-card", tmp_path / "overview.png")

        assert page._screenshot_idx == 2
        assert page.wait_for_load_state.called

    def test_retries_on_target_closed(self, tmp_path: Path) -> None:
        """Target-closed = tab-level navigation swap.  Must retry.

        Observed less often but same root cause: the Playwright "target"
        (the tab/frame) is torn down mid-operation.  Retry after
        networkidle re-establishes a live target.
        """
        helper = _get_helper()
        page = _make_page(
            [
                PlaywrightError("Locator.screenshot: Target closed"),
                None,
            ]
        )

        helper(page, "foxess-control-card", tmp_path / "card.png")

        assert page._screenshot_idx == 2

    def test_unrelated_playwright_error_propagates(self, tmp_path: Path) -> None:
        """Non-race Playwright errors (e.g. Timeout) must not be retried.

        A genuine timeout or selector mismatch is a real bug in the
        test — swallowing it would mask legitimate failures.
        """
        helper = _get_helper()
        page = _make_page(
            [
                PlaywrightError("Locator.screenshot: Timeout 30000ms exceeded"),
                # No second side-effect; we expect NO retry.
            ]
        )

        with pytest.raises(PlaywrightError, match="Timeout"):
            helper(page, "foxess-control-card", tmp_path / "card.png")

        assert page._screenshot_idx == 1, (
            f"Timeout must not trigger retry; screenshot called "
            f"{page._screenshot_idx} times"
        )

    def test_retries_exhausted_preserves_last_error(self, tmp_path: Path) -> None:
        """If every retry hits the same race, the last detach error
        must propagate — with its message intact so debugging is possible.
        """
        helper = _get_helper()
        detach_err = PlaywrightError(
            "Locator.screenshot: Element is not attached to the DOM"
        )
        # Seed the helper with as many detaches as it will possibly try.
        # The helper caps retries; any count large enough guarantees
        # exhaustion.  We use 10 which exceeds any sane retry budget.
        page = _make_page([detach_err] * 10)

        with pytest.raises(PlaywrightError, match="not attached to the DOM"):
            helper(page, "foxess-control-card", tmp_path / "card.png")

        # At least 2 attempts (initial + >=1 retry).
        assert page._screenshot_idx >= 2, (
            f"Expected helper to retry at least once before giving up; "
            f"got {page._screenshot_idx} attempts"
        )

    def test_success_on_first_attempt_no_retry(self, tmp_path: Path) -> None:
        """Happy path: single screenshot call, no settle, no retry.

        Guards against regression where the helper retries
        unconditionally and multiplies screenshot time.
        """
        helper = _get_helper()
        page = _make_page([None])

        helper(page, "foxess-control-card", tmp_path / "card.png")

        assert page._screenshot_idx == 1
        assert not page.wait_for_load_state.called, (
            "wait_for_load_state must only fire between retries, not on the happy path"
        )


class TestScreenshotCardDelegates:
    """``TestGalleryScreenshots._screenshot_card`` must delegate the
    actual screenshot to ``_safe_screenshot`` so all four gallery tests
    benefit from the retry path in one place.
    """

    def test_screenshot_card_recovers_from_detach(self, tmp_path: Path) -> None:
        """End-to-end test of the class helper: presence check + detach
        on first screenshot + success on retry.

        This mirrors the exact CI failure mode: ``wait_for_function``
        passes, ``locator(...).first`` resolves, then
        ``card.screenshot`` hits "Element is not attached to the DOM"
        on the first attempt.  The fix must let the capture succeed
        on the retry.
        """
        from tests.e2e.test_ui import TestGalleryScreenshots  # noqa: PLC0415

        # Point the class gallery dir at tmp so we don't litter the repo.
        instance = TestGalleryScreenshots()
        instance.GALLERY_DIR = tmp_path

        page = _make_page(
            [
                PlaywrightError(
                    "Locator.screenshot: Element is not attached to the DOM"
                ),
                None,
            ]
        )
        page.wait_for_function.return_value = None

        instance._screenshot_card(page, "foxess-control-card", "card.png")

        assert page._screenshot_idx == 2, (
            f"_screenshot_card must retry on detach; got "
            f"{page._screenshot_idx} attempts"
        )
