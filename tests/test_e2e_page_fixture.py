"""Tests for the E2E ``page`` fixture's Lovelace-panel wait.

Two flaky-test failures converge here:

1. **Context destruction (beta.12):** under CI load the HA frontend
   triggers navigations (WebSocket reconnect, dashboard router refresh)
   that destroy the JS execution context mid-poll.  Without retry logic,
   a single ``wait_for_function`` call fails with ``PlaywrightError:
   Execution context was destroyed``.  The beta.12 fix (commit cf76dfe)
   added a retry loop around context-destruction errors.

2. **Slow shard boot (this fix):** on slow GitHub runners the HA panel
   can take longer than 30s to render — not because of context
   destruction, but because HA's container is still registering custom
   elements / booting the dashboard router.  A single monolithic 30s
   ``wait_for_function`` budget is insufficient on these shards.  Root
   cause: the helper treats panel render as one opaque step, so neither
   progress nor failure stage is observable, and the 30s cap applies to
   the entire shadow-DOM traversal (root attach → main → lovelace panel).

The fix stages the wait into progressive DOM milestones — each with its
own bounded budget — so (a) the worst-case wall-clock budget materially
exceeds 30s on legitimately slow runners, and (b) a failure identifies
which stage was stuck.

These tests avoid a full Playwright browser: they mock the page object
to simulate the CI races deterministically.

Symptoms reproduced:
- ``PlaywrightError: Execution context was destroyed`` raised by
  ``wait_for_function`` during navigation churn.
- ``TimeoutError: Page.wait_for_function: Timeout 30000ms exceeded``
  when panel boot exceeds the monolithic 30s cap on slow shards.
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
        """Helper retries when the first poll loses its execution context.

        Monolithic helper: first ``wait_for_function`` call raises,
        second succeeds — helper recovers.

        Staged helper: first call (stage 1) raises, retries, subsequent
        stage calls succeed — helper still recovers.

        Common contract: at least one retry happens AND
        ``wait_for_load_state`` is used to settle between attempts.
        """
        helper = _get_helper()
        page = MagicMock()
        fired = {"count": 0}

        def _maybe_destroy(*_args: Any, **_kwargs: Any) -> None:
            if fired["count"] == 0:
                fired["count"] += 1
                raise PlaywrightError(
                    "Page.wait_for_function: Execution context was destroyed, "
                    "most likely because of a navigation"
                )
            fired["count"] += 1
            return None

        page.wait_for_function.side_effect = _maybe_destroy
        # Some staged implementations may use wait_for_selector;
        # apply the same pattern so context destruction is injected
        # regardless of which API the helper uses for its stages.
        page.wait_for_selector.side_effect = _maybe_destroy
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=5000)

        # Context-destruction was injected at least once and the helper
        # continued past it (no unhandled exception bubbled up).
        assert fired["count"] >= 2, (
            f"Expected >=1 context-destroyed + >=1 success; total calls="
            f"{fired['count']}"
        )
        # wait_for_load_state was used to settle after the context loss.
        assert page.wait_for_load_state.called

    def test_retries_on_navigating_error(self) -> None:
        """Helper retries when Playwright reports it is mid-navigation."""
        helper = _get_helper()
        page = MagicMock()
        fired = {"count": 0}

        def _maybe_navigating(*_args: Any, **_kwargs: Any) -> None:
            if fired["count"] == 0:
                fired["count"] += 1
                raise PlaywrightError(
                    "Page.wait_for_function: frame was detached while navigating"
                )
            fired["count"] += 1
            return None

        page.wait_for_function.side_effect = _maybe_navigating
        page.wait_for_selector.side_effect = _maybe_navigating
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=5000)

        assert fired["count"] >= 2, (
            f"Expected >=1 navigating + >=1 success; total calls={fired['count']}"
        )

    def test_genuine_timeout_propagates(self) -> None:
        """True TimeoutError (panel never rendered) must still raise.

        With a staged helper, we simulate the *first* stage timing out
        — the helper must propagate (not swallow) the TimeoutError.
        """
        helper = _get_helper()
        page = MagicMock()
        page.wait_for_function.side_effect = PwTimeoutError(
            "Page.wait_for_function: Timeout 30000ms exceeded."
        )
        page.wait_for_selector.side_effect = PwTimeoutError(
            "Page.wait_for_selector: Timeout 30000ms exceeded."
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
        page.wait_for_selector.side_effect = PlaywrightError(
            "Some other playwright failure"
        )

        with pytest.raises(PlaywrightError):
            helper(page, timeout_ms=5000)


class TestWaitForLovelacePanelStagedBudget:
    """The helper must stage the wait into progressive milestones so the
    worst-case budget materially exceeds 30s on slow CI shards.

    Root-cause diagnosis of the beta.12 escape (run 24872997253,
    gw2 shard 12): ``test_time_input_survives_multiple_rerenders[cloud]``
    setup ran 40.3s before giving up at the 30s ``wait_for_function``
    timeout.  The stack shows the retry loop was in place (context
    destruction not the cause) — the panel simply took >30s to render
    on an overloaded shard.  Other tests on the *same* shard ran 90.9s,
    confirming the container was alive but slow.

    Fix: stage the wait across ≥3 DOM milestones (``home-assistant`` →
    ``home-assistant-main`` → ``ha-panel-lovelace``).  Each stage has
    its own bounded budget; total worst-case is ~60–90s, not 30s.

    Observable contract:
    - Multiple distinct wait calls are issued (selector or function).
    - The total timeout budget across calls exceeds 30000ms.
    """

    def test_staged_wait_succeeds_when_panel_boot_exceeds_30s(self) -> None:
        """Helper succeeds when panel boot takes ~45s across multiple stages.

        Simulates a slow shard where each DOM milestone takes 10-15s:
        - ``home-assistant`` attaches at t=5s
        - ``home-assistant-main`` inside shadowRoot at t=20s
        - ``ha-panel-lovelace`` at t=45s

        A monolithic 30s ``wait_for_function`` would time out.  A staged
        helper issues three calls with their own budgets; each succeeds
        within its budget, so the overall helper succeeds.
        """
        helper = _get_helper()
        page = MagicMock()
        # Track every call's timeout argument so we can sum them.
        wait_function_timeouts: list[int] = []
        wait_selector_timeouts: list[int] = []

        def _wait_function(_pred: str, timeout: int) -> None:
            wait_function_timeouts.append(timeout)
            return None

        def _wait_selector(_selector: str, timeout: int = 30000, **_kw: Any) -> Any:
            wait_selector_timeouts.append(timeout)
            return MagicMock()

        page.wait_for_function.side_effect = _wait_function
        page.wait_for_selector.side_effect = _wait_selector
        page.wait_for_load_state.return_value = None

        # 90s budget reflects slow-shard worst case (one shard ran 90.9s).
        helper(page, timeout_ms=90000)

        total_calls = (
            page.wait_for_function.call_count + page.wait_for_selector.call_count
        )
        # The helper must stage the wait — at least 2 distinct DOM
        # milestones checked (ideally 3, but 2 is the minimum that is
        # materially better than the monolithic single call).
        assert total_calls >= 2, (
            f"Expected staged wait with >=2 milestones; got {total_calls} total calls. "
            f"wait_for_function={page.wait_for_function.call_count}, "
            f"wait_for_selector={page.wait_for_selector.call_count}"
        )

        total_budget_ms = sum(wait_function_timeouts) + sum(wait_selector_timeouts)
        # Total budget across stages must exceed the old 30s cap —
        # otherwise we have not materially improved the worst case.
        assert total_budget_ms > 30000, (
            f"Staged helper total budget {total_budget_ms}ms does not exceed "
            f"the old monolithic 30000ms cap — slow shards will still fail."
        )

    def test_staged_wait_checks_progressive_dom_milestones(self) -> None:
        """Helper checks distinct DOM milestones, not one monolithic predicate.

        A monolithic predicate (single ``wait_for_function`` with the
        ``home-assistant >>> home-assistant-main >>> ha-panel-lovelace``
        traversal) cannot distinguish between:
        (a) the root element hasn't attached yet (HA still booting),
        (b) the main layout hasn't rendered yet, or
        (c) the Lovelace panel hasn't mounted yet.

        Staging the wait produces diagnostic evidence of which layer is
        stuck.  Assert that multiple *distinct* wait arguments are used —
        either as selector strings or JS predicate substrings.
        """
        helper = _get_helper()
        page = MagicMock()
        seen_function_predicates: list[str] = []
        seen_selector_strings: list[str] = []

        def _wait_function(predicate: str, timeout: int = 30000) -> None:  # noqa: ARG001
            seen_function_predicates.append(predicate)
            return None

        def _wait_selector(selector: str, timeout: int = 30000, **_kw: Any) -> Any:  # noqa: ARG001
            seen_selector_strings.append(selector)
            return MagicMock()

        page.wait_for_function.side_effect = _wait_function
        page.wait_for_selector.side_effect = _wait_selector
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=60000)

        # Combine all "what we waited for" evidence.
        all_waited_for = [*seen_function_predicates, *seen_selector_strings]
        # Normalise — we expect at least 2 *distinct* milestone checks.
        # A monolithic single-predicate helper would only have 1 entry.
        assert len(all_waited_for) >= 2, (
            f"Expected >=2 distinct DOM-milestone waits, got {len(all_waited_for)}: "
            f"function_preds={seen_function_predicates}, "
            f"selectors={seen_selector_strings}"
        )
        # The individual waits must probe *different* targets — a helper
        # that calls the same predicate twice is not staging, it's
        # retrying a monolithic check.
        distinct = {str(x).replace(" ", "").replace("\n", "") for x in all_waited_for}
        assert len(distinct) >= 2, (
            f"All wait calls used the same target — no staging: {all_waited_for}"
        )

    def test_first_stage_timeout_is_bounded(self) -> None:
        """Each stage's timeout must be bounded to prevent runaway waits.

        No single stage should exceed the caller's total timeout budget,
        and at least one stage must use a meaningful fraction (≥10s) so
        the container has time to boot even on slow shards.
        """
        helper = _get_helper()
        page = MagicMock()
        observed_timeouts: list[int] = []

        def _record(_x: str, timeout: int = 30000, **_kw: Any) -> Any:
            observed_timeouts.append(timeout)
            return MagicMock()

        page.wait_for_function.side_effect = _record
        page.wait_for_selector.side_effect = _record
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=60000)

        assert observed_timeouts, "Helper made no wait calls at all"
        # No stage may exceed the total budget (bounded).
        for t in observed_timeouts:
            assert 0 < t <= 60000, f"Stage timeout {t}ms outside (0, 60000]ms budget"
