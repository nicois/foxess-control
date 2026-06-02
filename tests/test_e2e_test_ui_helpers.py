"""Regression tests for the four context-destruction retry helpers in
``tests/e2e/test_ui.py`` (C-031: no flaky tests — fix root causes).

**Background** (the same root cause that produced commit ``74d7f75``
in ``tests/e2e/conftest.py::_wait_for_stage`` on 2026-05-19): every
helper that retries after a Playwright "Execution context was
destroyed" error must NOT settle the new context with
``wait_for_load_state("networkidle", timeout=15000)``.  Under sustained
CI churn (entity-mode WebSocket state-burst from input-helper
registrations + EntityCoordinator first refresh), ``networkidle`` may
never fire — the suppressed ``PlaywrightError`` then consumes the
full 15000ms cap on EVERY retry.  Three to five back-to-back retries
exhaust the test's overall budget on settle waits alone, leaving the
actual predicate (``page.evaluate`` / ``card.screenshot`` /
``page.wait_for_function``) no time to converge once churn subsides.

The contract the helpers must obey:

- The per-retry ``wait_for_load_state`` cap MUST be modest
  (≤ 5000ms).  ``networkidle`` is not a reliable signal under
  sustained traffic; the helper should use a load-event signal that
  fires synchronously (``domcontentloaded``) and a cap small enough
  that several back-to-back retries cannot dominate the budget.

The helpers covered:

1. ``_safe_evaluate``  — wraps ``page.evaluate`` (retries=3).
2. ``_safe_screenshot`` — wraps ``locator.screenshot`` (retries=3).
3. ``_wait_for_card_hass`` — delegates to ``conftest.wait_for_condition``
   (which polls ``page.evaluate``) for ``_hass``-readiness (30s budget).
4. ``_find_card`` — delegates to ``conftest.wait_for_condition``
   (which polls ``page.evaluate``) for shadow-DOM element existence
   (30s budget).

Each test forces the helper down its retry path by raising
context-destroyed errors on the underlying Playwright call (``page.evaluate``
for helpers 3 & 4 since their 2026-06-03 migration to
``wait_for_condition``; the original call for 1 & 2), captures every
``wait_for_load_state`` invocation, and asserts that EVERY observed
timeout is ≤ 5000ms.  A helper still using the unbounded 15000ms cap
will fail with ``15000 > 5000``; ``wait_for_condition``'s cap is 3000ms.

Style follows
``tests/test_e2e_page_fixture.py::TestWaitForLovelacePanelSustainedRetryBudget``
— the structural-budget assertion that landed yesterday for
``_wait_for_stage``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError

if TYPE_CHECKING:
    from pathlib import Path

# Maximum acceptable per-retry settle cap (ms).  See module docstring.
_MAX_SETTLE_MS = 5000


def _ctx_destroyed(call: str) -> PlaywrightError:
    """Build a context-destruction error matching the production message."""
    return PlaywrightError(
        f"{call}: Execution context was destroyed, most likely because of a navigation"
    )


def _attached_to_dom() -> PlaywrightError:
    """Build a not-attached error matching the production message."""
    return PlaywrightError("Locator.screenshot: Element is not attached to the DOM")


class TestSafeEvaluateBoundedSettle:
    """``_safe_evaluate`` must not consume its retry budget on
    ``networkidle`` settles that never fire (C-031).
    """

    def test_per_retry_settle_capped_at_modest_budget(self) -> None:
        """Force every ``page.evaluate`` call to raise context-destroyed
        until the retry limit is reached, capture every
        ``wait_for_load_state`` timeout, and assert each is ≤ 5000ms.

        The helper retries ``retries=3`` times by default, so 3
        settles fire.  A helper using the unbounded 15000ms cap will
        fail this assertion immediately.
        """
        from tests.e2e.test_ui import _safe_evaluate  # noqa: PLC0415

        page = MagicMock()
        settle_timeouts: list[int] = []

        def _evaluate(_expr: str) -> object:
            raise _ctx_destroyed("Page.evaluate")

        def _wait_load_state(
            _state: str,  # noqa: ARG001
            timeout: int = 30000,
            **_kw: Any,
        ) -> None:
            settle_timeouts.append(timeout)

        page.evaluate.side_effect = _evaluate
        page.wait_for_load_state.side_effect = _wait_load_state

        with pytest.raises(PlaywrightError, match="Execution context was destroyed"):
            _safe_evaluate(page, "() => 1")

        assert settle_timeouts, (
            "Expected _safe_evaluate to invoke wait_for_load_state during "
            "the context-destruction retry path."
        )
        for t in settle_timeouts:
            assert t <= _MAX_SETTLE_MS, (
                f"_safe_evaluate called wait_for_load_state with "
                f"timeout={t}ms.  The per-retry settle MUST be capped at "
                f"≤ {_MAX_SETTLE_MS}ms — under sustained CI churn, "
                f"networkidle may never fire and 15000ms × 3 retries = "
                f"45000ms of dead time, dominating the test budget.  "
                f"Same root cause as the v1.0.17-beta.2 flake fixed in "
                f"_wait_for_stage by commit 74d7f75.  Observed settle "
                f"timeouts: {settle_timeouts}"
            )


class TestSafeScreenshotBoundedSettle:
    """``_safe_screenshot`` must not consume its retry budget on
    ``networkidle`` settles that never fire (C-031).
    """

    def test_per_retry_settle_capped_at_modest_budget(self, tmp_path: Path) -> None:
        """Force every ``locator.screenshot`` call to raise the
        not-attached error (one of the retryable signatures) until the
        retry limit is reached, capture every ``wait_for_load_state``
        timeout, and assert each is ≤ 5000ms.
        """
        from tests.e2e.test_ui import _safe_screenshot  # noqa: PLC0415

        page = MagicMock()
        settle_timeouts: list[int] = []

        def _next_locator(*_args: Any, **_kwargs: Any) -> MagicMock:
            locator = MagicMock()
            locator.first.screenshot.side_effect = _attached_to_dom()
            return locator

        def _wait_load_state(
            _state: str,  # noqa: ARG001
            timeout: int = 30000,
            **_kw: Any,
        ) -> None:
            settle_timeouts.append(timeout)

        page.locator.side_effect = _next_locator
        page.wait_for_load_state.side_effect = _wait_load_state

        with pytest.raises(PlaywrightError, match="not attached to the DOM"):
            _safe_screenshot(page, "foxess-control-card", tmp_path / "card.png")

        assert settle_timeouts, (
            "Expected _safe_screenshot to invoke wait_for_load_state "
            "during the context-destruction retry path."
        )
        for t in settle_timeouts:
            assert t <= _MAX_SETTLE_MS, (
                f"_safe_screenshot called wait_for_load_state with "
                f"timeout={t}ms.  The per-retry settle MUST be capped at "
                f"≤ {_MAX_SETTLE_MS}ms — under sustained CI churn, "
                f"networkidle may never fire and 15000ms × 3 retries = "
                f"45000ms of dead time, dominating the test budget.  "
                f"Observed settle timeouts: {settle_timeouts}"
            )


class TestWaitForCardHassBoundedSettle:
    """``_wait_for_card_hass`` must not consume its 30s budget on
    settles that never fire (C-031).

    **Surface migration (2026-06-03)**: ``_wait_for_card_hass`` now
    delegates to ``conftest.wait_for_condition``, which polls
    ``page.evaluate`` (not ``page.wait_for_function``) and, on a
    context-destroyed navigation error, settles the new context with
    ``page.wait_for_load_state("domcontentloaded", timeout=min(settle,
    3000))``.  The CONTRACT is unchanged — every per-retry settle must
    be capped at ≤ 5000ms so that a sustained churn storm cannot eat
    the budget — only the mocked surface moves from ``wait_for_function``
    to ``page.evaluate``.  The primitive's hard cap is 3000ms (< 5000ms);
    a regression to an unbounded 15000ms cap would still fail here.
    """

    def test_per_retry_settle_capped_at_modest_budget(self) -> None:
        """Force the first ``page.evaluate`` calls to raise context-
        destroyed.  The retry-loop's ``wait_for_load_state`` must use a
        modest cap, regardless of how much budget is remaining.
        """
        from tests.e2e.test_ui import _wait_for_card_hass  # noqa: PLC0415

        page = MagicMock()
        settle_timeouts: list[int] = []
        fired = {"count": 0}

        def _evaluate(_expr: str) -> object:
            # Raise context-destroyed twice; on the third call return
            # success so the helper exits cleanly.
            if fired["count"] < 2:
                fired["count"] += 1
                raise _ctx_destroyed("Page.evaluate")
            return True

        def _wait_load_state(
            _state: str,  # noqa: ARG001
            timeout: int = 30000,
            **_kw: Any,
        ) -> None:
            settle_timeouts.append(timeout)

        page.evaluate.side_effect = _evaluate
        page.wait_for_load_state.side_effect = _wait_load_state

        _wait_for_card_hass(page, "foxess-control-card", timeout_ms=30000)

        assert settle_timeouts, (
            "Expected _wait_for_card_hass to invoke wait_for_load_state "
            "during the context-destruction retry path."
        )
        for t in settle_timeouts:
            assert t <= _MAX_SETTLE_MS, (
                f"_wait_for_card_hass called wait_for_load_state with "
                f"timeout={t}ms.  The per-retry settle MUST be capped at "
                f"≤ {_MAX_SETTLE_MS}ms — under sustained CI churn a "
                f"never-firing settle that consumes a 15000ms cap on "
                f"every retry exhausts the 30s budget before the "
                f"predicate can converge.  wait_for_condition caps the "
                f"settle at min(remaining, 3000)ms.  Same root cause as "
                f"conftest._wait_for_stage prior to 74d7f75.  "
                f"Observed settle timeouts: {settle_timeouts}"
            )


class TestFindCardBoundedSettle:
    """``_find_card`` must not consume its 30s budget on settles that
    never fire (C-031).

    **Surface migration (2026-06-03)**: ``_find_card`` now delegates to
    ``conftest.wait_for_condition``, polling ``page.evaluate`` instead of
    ``page.wait_for_function``.  The settle-cap CONTRACT (≤ 5000ms per
    retry) is unchanged — only the mocked surface moves to
    ``page.evaluate``.  ``_find_card`` still returns ``True`` when the
    predicate eventually becomes truthy.
    """

    def test_per_retry_settle_capped_at_modest_budget(self) -> None:
        """Force the first ``page.evaluate`` calls to raise context-
        destroyed.  The retry-loop's ``wait_for_load_state`` must use a
        modest cap, regardless of how much budget is remaining.
        """
        from tests.e2e.test_ui import _find_card  # noqa: PLC0415

        page = MagicMock()
        settle_timeouts: list[int] = []
        fired = {"count": 0}

        def _evaluate(_expr: str) -> object:
            if fired["count"] < 2:
                fired["count"] += 1
                raise _ctx_destroyed("Page.evaluate")
            return True

        def _wait_load_state(
            _state: str,  # noqa: ARG001
            timeout: int = 30000,
            **_kw: Any,
        ) -> None:
            settle_timeouts.append(timeout)

        page.evaluate.side_effect = _evaluate
        page.wait_for_load_state.side_effect = _wait_load_state

        result = _find_card(page, "foxess-control-card", timeout=30000)

        # Helper returns True when the predicate eventually succeeds.
        assert result is True
        assert settle_timeouts, (
            "Expected _find_card to invoke wait_for_load_state during "
            "the context-destruction retry path."
        )
        for t in settle_timeouts:
            assert t <= _MAX_SETTLE_MS, (
                f"_find_card called wait_for_load_state with "
                f"timeout={t}ms.  The per-retry settle MUST be capped at "
                f"≤ {_MAX_SETTLE_MS}ms — a never-firing settle on a "
                f"15000ms cap eats the budget early; wait_for_condition "
                f"caps it at min(remaining_ms, 3000)ms.  Same dead-time "
                f"problem as conftest._wait_for_stage prior to 74d7f75.  "
                f"Observed settle timeouts: {settle_timeouts}"
            )
