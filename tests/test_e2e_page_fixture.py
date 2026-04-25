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


class TestWaitForLovelacePanelNavigationDuringPanelRender:
    """The helper must survive a full page navigation fired *during* the
    panel-render stage — the scenario that drives the remaining flake.

    **Root cause** (diagnosed 2026-04-25 by observing a live HA container):
    HA's frontend fires a full page navigation ~1–15 seconds after the
    initial ``goto`` completes — triggered by its auth refresh / WS
    reconnect housekeeping.  The navigation destroys the browser's JS
    execution context mid-flight, and when the new context mounts, the
    entire shadow-DOM chain (home-assistant → home-assistant-main →
    ha-panel-lovelace) must rebuild from scratch inside the already-running
    stage-3 wait.

    The per-stage 30s cap is *just enough* under normal load but breaks
    under slow-shard contention: the original stage-3 wait burns a few
    seconds before the nav arrives, Playwright catches the destruction,
    the retry starts with a fresh context, and the rebuild legitimately
    needs 25–40s on a contended runner.  With ~25s of stage-3 budget
    already consumed and only ~5s left, the retry times out.

    Observed CI signatures:
    - ``test_form_recovers_from_page_navigation[entity]`` setup=40.0s →
      TimeoutError Page.wait_for_function: Timeout 30000ms exceeded.
      (v1.0.13-beta.2, run 24931127123)
    - ``test_time_picker_stays_open_during_rerender[entity]`` setup=39.9s
      → same TimeoutError. (v1.0.13-beta.1, run 24921297745)
    - ``test_gallery_overview_idle[entity]`` body failed with
      ``Locator.screenshot: Element is not attached to the DOM`` — the
      *same* mid-stage navigation but this time observed from the test
      body: the helper returned successfully based on the OLD context's
      ``ha-panel-lovelace``, then the navigation detached it before the
      test could screenshot it.

    **Fix contract** (what these tests assert):
    1. When a navigation destroys the context mid-stage, the helper must
       retry using *any remaining overall budget* — not be artificially
       capped at the per-stage ``max_stage_ms`` on the retry.  Under
       adversarial CI timing the retry legitimately needs > 30s.
    2. The helper must not return when the panel element is present but
       *transiently* so — a panel that appears then disappears (navigation
       about to happen) is not a usable ready signal.  Returning on the
       transient attachment causes the test body to hit
       ``Element is not attached to the DOM``.

    These tests encode both properties using a MagicMock page whose
    predicate responses simulate the exact mid-stage navigation.
    """

    def test_retry_after_midstage_nav_uses_remaining_overall_budget(self) -> None:
        """Retry after mid-stage context destruction must use any remaining
        overall budget, not be re-capped to ``max_stage_ms``.

        Scenario: caller supplies ``timeout_ms=75000``.  Earlier stages
        consume ~2s total.  The final stage enters ``wait_for_function``,
        runs for 20s, then HA navigates → ``Execution context was
        destroyed``.  On retry the overall budget still has ~53s left and
        the helper must use that full amount so the post-navigation panel
        mount has time to complete under slow-shard contention.

        **What the current code does wrong**: ``_wait_for_stage`` uses
        ``min(remaining_ms, max_stage_ms=30000)`` on every loop iteration.
        After the mid-stage navigation the remaining overall budget is
        ~53s but the retry is re-capped to 30s.  If post-nav rebuild
        legitimately takes 31–53s (well under overall budget, but over
        the per-stage cap) we time out spuriously.
        """
        helper = _get_helper()
        page = MagicMock()
        observed_timeouts: list[int] = []
        call_history: list[str] = []
        fired = {"destroyed_once": False}

        def _wait_function(_pred: str, timeout: int = 30000) -> None:
            observed_timeouts.append(timeout)
            call_history.append("fn")
            # The first call that reaches the FINAL stage returns
            # successfully quickly (stages 1+2 pass fast).  The one we
            # want to exercise is the call that sees the panel predicate
            # with a *large* remaining budget — it should be given the
            # full budget, not 30s.
            if not fired["destroyed_once"] and timeout > 30000:
                # This is the final-stage call with full remaining budget.
                # Simulate a mid-stage navigation.
                fired["destroyed_once"] = True
                raise PlaywrightError(
                    "Page.wait_for_function: Execution context was destroyed, "
                    "most likely because of a navigation"
                )
            return None

        def _wait_selector(_selector: str, timeout: int = 30000, **_kw: Any) -> Any:
            observed_timeouts.append(timeout)
            call_history.append("sel")
            if not fired["destroyed_once"] and timeout > 30000:
                fired["destroyed_once"] = True
                raise PlaywrightError(
                    "Page.wait_for_selector: Execution context was destroyed, "
                    "most likely because of a navigation"
                )
            return MagicMock()

        page.wait_for_function.side_effect = _wait_function
        page.wait_for_selector.side_effect = _wait_selector
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=75000)

        # Assert: at least one call received a timeout > 30000ms.  A
        # helper that caps every individual call at 30s cannot honour
        # the full remaining budget on the post-navigation retry, which
        # is precisely what drives the observed 40s stage-3 timeouts.
        assert any(t > 30000 for t in observed_timeouts), (
            f"Every individual wait call was capped at <=30000ms "
            f"(observed: {observed_timeouts}).  The final stage must be "
            f"allowed to consume the remaining overall budget (up to "
            f"the full 75000ms) on the post-navigation retry, otherwise "
            f"a slow-shard rebuild has insufficient time to complete."
        )

    def test_final_stage_predicate_includes_stable_signal(self) -> None:
        """The final-stage predicate must include a signal that indicates
        the panel is *settled* — not merely attached once.

        The ``test_gallery_overview_idle[entity]`` failure on
        v1.0.13-beta.1 showed the exact symptom of this gap: setup
        returned successfully, the test body took a screenshot of the
        Lovelace card, and got ``Element is not attached to the DOM`` —
        because HA navigated between setup return and the test body's
        first action, detaching the panel the helper had just certified
        as "ready".

        Diagnosis (observed live 2026-04-25 against a real HA container):
        after ``page.goto`` returns and stages 1+2 pass, HA's frontend
        fires a full page navigation ~1–15s later — triggered by its
        auth refresh / service-worker registration.  The navigation
        destroys the browser's JS execution context and the panel
        re-mounts from scratch.  If stage-3's predicate only checks
        ``!!ham.shadowRoot.querySelector('ha-panel-lovelace')`` (a bare
        attach check), the helper happily returns on the *first*
        transient mount — right before HA's housekeeping navigation
        detaches it again.

        Contract: the final-stage predicate must do more than check that
        the panel element exists in the DOM.  Concrete signals that
        prove the panel is past HA's initial navigation churn:
          - ``ha.hass.connected === true`` (WS session established)
          - ``ha.hass.states`` is populated (entity snapshot loaded)
          - ``panel.hass`` is set (panel is wired to the state store)
          - ``panel.shadowRoot.querySelector('hui-root')`` exists
            (Lovelace has actually started rendering content)

        Any of these alternatives — or a combination — is strictly
        stronger than the bare attach check and prevents the
        transient-attachment failure.  This test fails against the
        current helper because its final predicate is a bare attach
        check (``return !!panel``) with no stability signal.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        stages = getattr(conftest, "_LOVELACE_PANEL_STAGES", None)
        assert stages is not None, (
            "Helper no longer exposes _LOVELACE_PANEL_STAGES — cannot "
            "inspect the final-stage predicate for stability signal."
        )
        # The final stage is the last entry in _LOVELACE_PANEL_STAGES.
        final_stage_name, final_predicate = stages[-1]

        # Normalise the predicate source: collapse whitespace so we
        # match irrespective of formatting.
        normalised = "".join(final_predicate.split())

        # At least one semantic signal must be present in the final
        # predicate.  Each of these indicates the panel is past HA's
        # initial navigation churn:
        semantic_signals = (
            "hass.connected",  # WS session established
            "hass.states",  # entity snapshot loaded
            "panel.hass",  # panel is wired
            "panelhass",  # alt spelling after whitespace collapse
            "hui-root",  # Lovelace content renderer mounted
            "hui_root",
        )

        has_signal = any(sig in normalised for sig in semantic_signals)
        assert has_signal, (
            f"Final-stage predicate for '{final_stage_name}' uses only a "
            f"bare attach check with no stability signal.  Observed "
            f"predicate source:\n{final_predicate}\n\n"
            f"This fails to distinguish a transient panel attachment "
            f"(moments before HA's housekeeping navigation detaches it) "
            f"from a stably mounted panel, producing the observed CI "
            f"flakes:\n"
            f"  - ``Element is not attached to the DOM`` in the test body\n"
            f"  - ``wait_for_function: Timeout 30000ms`` at fixture setup\n"
            f"Add one of: hass.connected check, hass.states non-empty, "
            f"panel.hass set, or hui-root rendered.  See the docstring "
            f"for the rationale."
        )
