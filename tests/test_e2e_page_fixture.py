"""Tests for the E2E ``page`` fixture's Lovelace-panel wait.

Regression for a flaky-test failure in the ``page`` fixture's
``wait_for_function`` call: under CI load the HA frontend can trigger
navigation events (WebSocket reconnect, dashboard router refresh) that
destroy the JS execution context mid-poll.  The single ``wait_for_function``
call does not retry, causing ``TimeoutError: Timeout 30000ms exceeded``
whenever a re-navigation burst exceeds the 30s budget.

The same pattern was fixed for ``_find_card`` in commit aa25b10 (retry
after context-destruction errors with the remaining budget).  This test
verifies the *helper* that the page fixture uses has equivalent retry
semantics.

The tests avoid a full Playwright browser: they mock
``Page.wait_for_function`` to simulate the CI race deterministically.

Symptom reproduced:
    ``playwright._impl._errors.TimeoutError: Page.wait_for_function:
    Timeout 30000ms exceeded`` at the fixture's ``wait_for_function``
    call, caused by repeated "Execution context was destroyed" errors
    during navigation churn.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright._impl._errors import TimeoutError as PwTimeoutError


def _get_helper() -> Any:
    """Import the helper.  Raises ImportError if not yet defined.

    Keeping the import inside a function avoids a module-level
    ImportError that would break test collection entirely — we want a
    clean per-test skip/fail when the helper is missing.
    """
    from tests.e2e import conftest  # noqa: PLC0415

    try:
        return conftest._wait_for_lovelace_panel
    except AttributeError as exc:
        msg = (
            "tests.e2e.conftest._wait_for_lovelace_panel is not defined. "
            "The page fixture's wait-for-panel logic must be extracted "
            "into a helper with retry-on-context-destruction semantics."
        )
        raise ImportError(msg) from exc


class TestWaitForLovelacePanelRetries:
    """The helper must retry when the JS execution context is destroyed.

    Under CI load, HA's frontend triggers navigations during the initial
    page load (WS reconnect, panel router rebuild, sidebar load).  Each
    navigation destroys the browser's JS execution context, causing
    ``wait_for_function`` to raise ``PlaywrightError: Execution context
    was destroyed``.  Without retry logic, a single such burst during
    the 30s budget fails the fixture — even though the panel does render
    a few seconds later once navigation settles.
    """

    def test_retries_on_execution_context_destroyed(self) -> None:
        """Helper retries when the first poll loses its execution context."""
        helper = _get_helper()
        page = MagicMock()
        # First call: navigation-induced context destruction (should retry).
        # Second call: succeeds.
        page.wait_for_function.side_effect = [
            PlaywrightError(
                "Page.wait_for_function: Execution context was destroyed, "
                "most likely because of a navigation"
            ),
            None,  # success on retry
        ]
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=5000)

        # Two attempts: one that raised, one that succeeded.
        assert page.wait_for_function.call_count == 2
        # Between retries, helper waits for the navigation to settle.
        assert page.wait_for_load_state.called

    def test_retries_on_navigating_error(self) -> None:
        """Helper retries when Playwright reports it is mid-navigation."""
        helper = _get_helper()
        page = MagicMock()
        page.wait_for_function.side_effect = [
            PlaywrightError(
                "Page.wait_for_function: frame was detached while navigating"
            ),
            None,
        ]
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=5000)

        assert page.wait_for_function.call_count == 2

    def test_genuine_timeout_propagates(self) -> None:
        """True TimeoutError (panel never rendered) must still raise."""
        helper = _get_helper()
        page = MagicMock()
        page.wait_for_function.side_effect = PwTimeoutError(
            "Page.wait_for_function: Timeout 5000ms exceeded."
        )

        with pytest.raises(PwTimeoutError):
            helper(page, timeout_ms=5000)

    def test_unrelated_playwright_error_propagates(self) -> None:
        """Non-context-destroyed PlaywrightErrors must not be swallowed."""
        helper = _get_helper()
        page = MagicMock()
        page.wait_for_function.side_effect = PlaywrightError(
            "Some other playwright failure"
        )

        with pytest.raises(PlaywrightError):
            helper(page, timeout_ms=5000)

    def test_success_on_first_attempt_does_not_wait_for_load_state(self) -> None:
        """No retry machinery engages when wait_for_function succeeds."""
        helper = _get_helper()
        page = MagicMock()
        page.wait_for_function.return_value = None

        helper(page, timeout_ms=5000)

        assert page.wait_for_function.call_count == 1
        assert not page.wait_for_load_state.called
