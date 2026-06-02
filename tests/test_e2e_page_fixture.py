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

**Surface migration (2026-06-03)**: ``_wait_for_lovelace_panel`` →
``_wait_for_stage`` now delegate to ``conftest.wait_for_condition``,
which polls ``page.evaluate(predicate)`` in a loop rather than calling
``page.wait_for_function`` / ``page.wait_for_selector``.  These tests
drive the new surface: behavioural tests configure ``page.evaluate``;
budget/staging tests spy on ``conftest.wait_for_condition`` to observe
the per-stage ``timeout_ms`` the helper computes (no longer a Playwright
call argument).  Every original contract is preserved; only the mocked
surface and (where the helper now raises a typed error) the expected
exception type change.

Symptoms reproduced:
- ``PlaywrightError: Execution context was destroyed`` raised by
  ``page.evaluate`` during navigation churn.
- ``E2EConditionTimeout`` (the typed error wait_for_condition raises)
  when a stage predicate never becomes truthy within its bounded budget
  — the staged equivalent of the old monolithic 30s ``wait_for_function``
  timeout on slow shards.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError

from tests.e2e.conftest import E2EConditionTimeout


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


def _is_fail_check(expr: str) -> bool:
    """True if ``expr`` is the lovelace HA-error ``fail_check`` predicate.

    ``wait_for_condition`` polls the ``fail_check`` (the shared
    ``_LOVELACE_FAIL_CHECK``, which scans for ``ha-panel-error`` /
    ``hui-error-card``) BEFORE the stage ``pass_check`` on every
    iteration.  A MagicMock ``page.evaluate`` that returns truthy for
    *everything* would trip the fail_check and raise ``E2EConditionFailed``
    spuriously.  Tests that drive ``page.evaluate`` must therefore return
    falsy for the fail_check (no error panel rendered) and the intended
    value for the stage pass_check.  The fail_check is the only predicate
    that references the HA error elements.
    """
    return "ha-panel-error" in expr or "hui-error-card" in expr


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

        The helper polls ``page.evaluate(predicate)`` through
        ``wait_for_condition``: the first ``evaluate`` call raises a
        context-destroyed PlaywrightError, the next returns truthy — the
        helper recovers and the stage completes.

        Common contract (unchanged): at least one retry happens AND
        ``wait_for_load_state`` is used to settle between attempts.
        """
        helper = _get_helper()
        page = MagicMock()
        fired = {"destroyed": 0, "success": 0}

        def _maybe_destroy(expr: str) -> object:
            # The fail_check (HA-error scan) must stay falsy — an error
            # panel is NOT present — else wait_for_condition aborts early.
            if _is_fail_check(expr):
                return False
            # First stage pass_check loses its execution context, then
            # recovers on the retry.
            if fired["destroyed"] == 0:
                fired["destroyed"] += 1
                raise PlaywrightError(
                    "Page.evaluate: Execution context was destroyed, "
                    "most likely because of a navigation"
                )
            fired["success"] += 1
            return True

        # The helper now drives stages via page.evaluate(predicate)
        # through wait_for_condition; inject context destruction there.
        page.evaluate.side_effect = _maybe_destroy
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=5000)

        # Context-destruction was injected at least once and the helper
        # continued past it (recovered with >=1 successful pass_check).
        assert fired["destroyed"] >= 1 and fired["success"] >= 1, (
            f"Expected >=1 context-destroyed + >=1 success; "
            f"destroyed={fired['destroyed']}, success={fired['success']}"
        )
        # wait_for_load_state was used to settle after the context loss.
        assert page.wait_for_load_state.called

    def test_retries_on_navigating_error(self) -> None:
        """Helper retries when Playwright reports it is mid-navigation."""
        helper = _get_helper()
        page = MagicMock()
        fired = {"destroyed": 0, "success": 0}

        def _maybe_navigating(expr: str) -> object:
            if _is_fail_check(expr):
                return False
            if fired["destroyed"] == 0:
                fired["destroyed"] += 1
                raise PlaywrightError(
                    "Page.evaluate: frame was detached while navigating"
                )
            fired["success"] += 1
            return True

        page.evaluate.side_effect = _maybe_navigating
        page.wait_for_load_state.return_value = None

        helper(page, timeout_ms=5000)

        assert fired["destroyed"] >= 1 and fired["success"] >= 1, (
            f"Expected >=1 navigating + >=1 success; "
            f"destroyed={fired['destroyed']}, success={fired['success']}"
        )

    def test_genuine_timeout_propagates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """A predicate that never becomes truthy must still raise.

        We simulate the *first* stage never satisfying its predicate:
        ``page.evaluate`` always returns falsy, so ``wait_for_condition``
        exhausts the (short) per-stage budget and raises
        ``E2EConditionTimeout``.  The helper must propagate (not swallow)
        it.

        **Expected-exception update (contract, not weakening)**: the old
        monolithic helper bubbled Playwright's ``TimeoutError``; the
        staged helper now raises the typed ``E2EConditionTimeout``
        (an ``AssertionError`` subclass that ``wait_for_condition``
        raises after capturing the DOM).  Asserting the new type still
        asserts "a genuine timeout propagates and is not swallowed."
        """
        # Redirect failure-capture artifacts to tmp_path; the timeout
        # path calls _capture_failure (page.evaluate / content /
        # screenshot on the mock, all suppressed).
        from tests.e2e import conftest  # noqa: PLC0415

        monkeypatch.setattr(conftest, "_failure_capture_dir", lambda: tmp_path)
        helper = _get_helper()
        page = MagicMock()
        # Predicate never satisfied; fail_check also falsy so the stage
        # runs to its deadline and raises E2EConditionTimeout.
        page.evaluate.return_value = False
        page.wait_for_load_state.return_value = None
        page.content.return_value = "<html></html>"
        page.screenshot.return_value = None

        with pytest.raises(E2EConditionTimeout):
            helper(page, timeout_ms=200)

    def test_unrelated_playwright_error_propagates(self) -> None:
        """Non-context-destroyed PlaywrightErrors must not be swallowed.

        ``page.evaluate`` raises a PlaywrightError whose message does NOT
        match the context-destroyed signals; ``wait_for_condition`` must
        re-raise it unchanged — never retried, never converted into an
        ``E2EConditionTimeout``.
        """
        helper = _get_helper()
        page = MagicMock()
        page.evaluate.side_effect = PlaywrightError("Some other playwright failure")

        with pytest.raises(PlaywrightError) as exc_info:
            helper(page, timeout_ms=5000)
        # Must propagate unchanged — not swallowed into a timeout.
        assert not isinstance(exc_info.value, E2EConditionTimeout)
        assert "Some other playwright failure" in str(exc_info.value)


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

    def test_staged_wait_succeeds_when_panel_boot_exceeds_30s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Helper succeeds when panel boot takes ~45s across multiple stages.

        A monolithic 30s wait would time out.  A staged helper issues
        one ``wait_for_condition`` call per DOM milestone, each with its
        own bounded budget; each succeeds, so the overall helper succeeds.

        **Surface migration**: the per-stage budget is no longer a
        Playwright-call argument — it is the ``timeout_ms`` the helper
        passes to ``wait_for_condition`` (the internal poll deadline).
        We spy on ``conftest.wait_for_condition`` and sum the
        ``timeout_ms`` it receives.  Contract unchanged: >=2 staged
        milestones, total budget > 30000ms.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        stage_timeouts: list[int] = []

        def _spy(_page: Any, _pass: str, *, timeout_ms: int, **_kw: Any) -> object:
            stage_timeouts.append(timeout_ms)
            return True

        monkeypatch.setattr(conftest, "wait_for_condition", _spy)
        helper = _get_helper()
        page = MagicMock()

        # 90s budget reflects slow-shard worst case (one shard ran 90.9s).
        helper(page, timeout_ms=90000)

        # The helper must stage the wait — at least 2 distinct DOM
        # milestones checked (ideally 3, but 2 is the minimum that is
        # materially better than the monolithic single call).
        assert len(stage_timeouts) >= 2, (
            f"Expected staged wait with >=2 milestones; got "
            f"{len(stage_timeouts)} wait_for_condition calls."
        )

        total_budget_ms = sum(stage_timeouts)
        # Total budget across stages must exceed the old 30s cap —
        # otherwise we have not materially improved the worst case.
        assert total_budget_ms > 30000, (
            f"Staged helper total budget {total_budget_ms}ms does not exceed "
            f"the old monolithic 30000ms cap — slow shards will still fail."
        )

    def test_staged_wait_checks_progressive_dom_milestones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Helper checks distinct DOM milestones, not one monolithic predicate.

        A monolithic predicate (a single traversal of
        ``home-assistant >>> home-assistant-main >>> ha-panel-lovelace``)
        cannot distinguish between:
        (a) the root element hasn't attached yet (HA still booting),
        (b) the main layout hasn't rendered yet, or
        (c) the Lovelace panel hasn't mounted yet.

        Staging the wait produces diagnostic evidence of which layer is
        stuck.  **Surface migration**: each stage now flows through a
        separate ``wait_for_condition`` call carrying that stage's
        ``pass_check`` predicate.  We spy on ``conftest.wait_for_condition``
        and assert >=2 *distinct* predicates are polled.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        seen_predicates: list[str] = []

        def _spy(_page: Any, pass_check: str, **_kw: Any) -> object:
            seen_predicates.append(pass_check)
            return True

        monkeypatch.setattr(conftest, "wait_for_condition", _spy)
        helper = _get_helper()
        page = MagicMock()

        helper(page, timeout_ms=60000)

        # We expect at least 2 *distinct* milestone predicates.
        # A monolithic single-predicate helper would only have 1 entry.
        assert len(seen_predicates) >= 2, (
            f"Expected >=2 distinct DOM-milestone waits, got "
            f"{len(seen_predicates)}: {seen_predicates}"
        )
        # The individual waits must probe *different* targets — a helper
        # that polls the same predicate twice is not staging, it's
        # retrying a monolithic check.
        distinct = {"".join(p.split()) for p in seen_predicates}
        assert len(distinct) >= 2, (
            f"All wait calls used the same predicate — no staging: {seen_predicates}"
        )

    def test_first_stage_timeout_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each stage's timeout must be bounded to prevent runaway waits.

        No single stage should exceed the caller's total timeout budget.

        **Surface migration**: the per-stage budget is the ``timeout_ms``
        passed to ``wait_for_condition`` (no longer a Playwright-call
        argument), so we spy on it.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        observed_timeouts: list[int] = []

        def _spy(_page: Any, _pass: str, *, timeout_ms: int, **_kw: Any) -> object:
            observed_timeouts.append(timeout_ms)
            return True

        monkeypatch.setattr(conftest, "wait_for_condition", _spy)
        helper = _get_helper()
        page = MagicMock()

        helper(page, timeout_ms=60000)

        assert observed_timeouts, "Helper made no staged wait calls at all"
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

    def test_retry_after_midstage_nav_uses_remaining_overall_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retry after mid-stage context destruction must use any remaining
        overall budget, not be re-capped to ``max_stage_ms``.

        Scenario: caller supplies ``timeout_ms=75000``.  Earlier stages
        consume a small amount.  The final stage polls its predicate; if
        HA navigates mid-stage, the post-navigation retry must keep using
        the full remaining overall budget so the panel mount has time to
        complete under slow-shard contention.

        **Contract location**: ``_wait_for_stage`` sets
        ``max_stage_ms=None`` for the final stage, so its
        ``timeout_ms = remaining_ms`` (the full remaining overall budget),
        NOT ``min(remaining_ms, 30000)``.  Were the final stage re-capped
        to 30s, a post-nav rebuild taking 31–53s (well under the overall
        budget but over the per-stage cap) would time out spuriously.

        **Surface migration**: the per-stage budget is the ``timeout_ms``
        the helper hands ``wait_for_condition`` (no longer a Playwright-
        call argument), so we spy on it directly.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        observed_timeouts: list[int] = []

        # Spy on wait_for_condition: record the per-stage budget the
        # helper computes.  The final stage's max_stage_ms is None, so
        # _wait_for_stage gives it the full remaining overall budget —
        # NOT a 30s re-cap.  Earlier stages return instantly here, so
        # the final stage sees nearly the entire 75000ms left.
        def _spy(_page: Any, _pass: str, *, timeout_ms: int, **_kw: Any) -> object:
            observed_timeouts.append(timeout_ms)
            return True

        monkeypatch.setattr(conftest, "wait_for_condition", _spy)
        helper = _get_helper()
        page = MagicMock()

        helper(page, timeout_ms=75000)

        # Assert: at least one stage received a budget > 30000ms.  A
        # helper that caps every stage at 30s cannot honour the full
        # remaining budget on the post-navigation retry, which is
        # precisely what drives the observed 40s stage-3 timeouts.  The
        # contract lives in _wait_for_stage's max_stage_ms=None for the
        # final stage (stage_ms = remaining_ms, no 30s re-cap).
        assert any(t > 30000 for t in observed_timeouts), (
            f"Every staged wait budget was capped at <=30000ms "
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


class TestWaitForLovelacePanelCloudVariantSignalStability:
    """The final-stage predicate must be *robust* across HA's async
    panel-lifecycle wiring order — including the [cloud] variant.

    **Background** (diagnosed 2026-04-26 from Flaky Test Detection run
    24956110840 for v1.0.13): after the beta.3 fix landed, a *new* flake
    signature emerged — different from beta.3's 30s-per-stage cap:

        playwright._impl._errors.TimeoutError:
        Page.wait_for_function: Timeout 74958ms exceeded.

    Note the ``74958ms``: virtually the entire 75000ms overall budget
    was consumed by the final stage without converging.  The victim was
    ``test_gallery_control_charging[cloud]`` — a ``cloud`` variant, not
    an ``entity`` variant.

    **Root-cause diagnosis** (which signal is slow under cloud):
    Stage 3 currently requires THREE signals to be simultaneously true:
      (a) ``main.hass.connected === true`` — HA WS session established.
      (b) ``ha-panel-lovelace`` attached inside the main shadow root.
      (c) ``panel.hass`` set — partial-panel-resolver wired the panel.

    Signal (c) has a **timing vulnerability**: it is set by HA's
    ``partial-panel-resolver`` between the panel's ``connectedCallback``
    and its first render.  Under adversarial CI timing (12 xdist workers,
    cloud config doing additional async integration startup work), a
    navigation can destroy the context between panel mount and the
    ``panel.hass`` assignment — cycling the helper through retries that
    never simultaneously see all three signals true.

    **Fix contract** (what this test asserts): an observable
    *DOM-level* stability signal — specifically ``hui-root`` mounted
    inside ``panel.shadowRoot`` — is strictly stronger than
    ``panel.hass`` and must be used in place of (or as an alternative
    to) ``panel.hass`` in the final-stage predicate:

      - ``hui-root`` cannot exist unless the panel's render cycle
        completed at least once.
      - A render cycle requires ``panel.hass`` to have been assigned
        *and* still be set at render time.
      - So ``hui-root`` implies ``panel.hass`` (the DOM is the proof),
        but ``hui-root`` also survives the wire-up race because its
        presence is a synchronous DOM fact, not a transient JS property.

    The predicate can legitimately fall back to ``panel.hass`` when
    ``hui-root`` has not yet mounted (panels that show loading
    spinners), but ``hui-root`` being present must always be sufficient
    to return — never gated by ``panel.hass`` also being truthy.
    """

    def test_hui_root_presence_is_sufficient_for_settled_signal(self) -> None:
        """If ``hui-root`` is mounted inside ``panel.shadowRoot``, the
        final-stage predicate must reference it as *executable code*,
        not merely in comments.

        Why this matters: ``hui-root`` is rendered *by* the panel only
        after ``panel.hass`` has been assigned at least once and a Lit
        render cycle has completed.  Its presence proves the panel
        passed through the wired state.  But ``panel.hass`` can briefly
        read as ``null`` mid-navigation when HA's
        ``partial-panel-resolver`` is swapping panels — and we do not
        want the predicate to go false on that transient blip.

        The check strips JS comments before scanning, so mentions of
        ``hui-root`` in rationale comments do not satisfy the
        assertion — only executable ``querySelector('hui-root')`` (or
        an equivalent DOM reference) counts.
        """
        import re  # noqa: PLC0415

        from tests.e2e import conftest  # noqa: PLC0415

        stages = getattr(conftest, "_LOVELACE_PANEL_STAGES", None)
        assert stages is not None, "Helper missing _LOVELACE_PANEL_STAGES"
        _final_stage_name, final_predicate = stages[-1]

        # Strip JS line comments (// ...) and block comments (/* */)
        # so mentions of hui-root in rationale prose do not satisfy the
        # assertion — only references in executable code count.
        stripped = re.sub(r"//[^\n]*", "", final_predicate)
        stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
        normalised = "".join(stripped.split())

        has_hui_root = "hui-root" in normalised or "hui_root" in normalised
        assert has_hui_root, (
            "Final-stage predicate does not reference hui-root in "
            "executable code.  Observed predicate (with comments "
            f"stripped):\n{stripped}\n\n"
            "The [cloud] variant flake (run 24956110840, timeout "
            "74958ms) is driven by ``panel.hass`` being transiently "
            "unset during HA's navigation churn.  ``hui-root`` is a "
            "synchronous DOM fact that is strictly stronger and does "
            "not suffer the wire-up race.  Add ``hui-root`` as an "
            "alternative path in the predicate, or replace panel.hass "
            "with it entirely."
        )

    def test_predicate_succeeds_when_hui_root_present_even_if_panel_hass_null(
        self,
    ) -> None:
        """Evaluate the final-stage predicate's JavaScript source
        against a simulated DOM snapshot (``hui-root`` mounted,
        ``panel.hass`` ``null``) and confirm it returns truthy.

        Rationale: a purely-syntactic test (checking the predicate
        source contains ``hui-root``) can be satisfied by a comment or
        by a gated AND-clause that never actually accepts
        ``hui-root``-alone.  This test exercises the actual JavaScript
        semantics via a minimal JS interpreter (Node if available, or
        a textual simulation as fallback) to guarantee the predicate
        *behaviourally* accepts ``hui-root`` as sufficient proof.
        """
        import json  # noqa: PLC0415
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        from tests.e2e import conftest  # noqa: PLC0415

        stages = getattr(conftest, "_LOVELACE_PANEL_STAGES", None)
        assert stages is not None, "Helper missing _LOVELACE_PANEL_STAGES"
        _final_stage_name, final_predicate = stages[-1]

        # Require node.js for the DOM-behavioural check.  If not
        # available in the CI environment, the syntactic check in the
        # sibling test is the fall-back guard.
        node_bin = shutil.which("node")
        if node_bin is None:
            pytest.skip("node.js unavailable — cannot execute predicate JS")

        # Build a DOM mock that represents the observed adversarial
        # cloud-variant snapshot:
        #   - document.querySelector('home-assistant') resolves.
        #   - main.shadowRoot.querySelector('home-assistant-main') resolves.
        #   - ham.shadowRoot.querySelector('ha-panel-lovelace') resolves.
        #   - main.hass.connected === true.
        #   - main.hass.states populated.
        #   - panel.shadowRoot.querySelector('hui-root') resolves.
        #   - panel.hass === null (the transient race state).
        # Under this snapshot, a predicate that REQUIRES ``panel.hass``
        # will return false; a predicate that accepts ``hui-root`` as
        # alternative proof will return true.
        dom_setup = r"""
        const panel_shadow = {
            querySelector: (sel) => (
                sel === 'hui-root' ? {nodeName: 'HUI-ROOT'} : null
            ),
        };
        const panel = { shadowRoot: panel_shadow, hass: null };
        const ham_shadow = {
            querySelector: (sel) => (
                sel === 'ha-panel-lovelace' ? panel : null
            ),
        };
        const ham = { shadowRoot: ham_shadow };
        const main_shadow = {
            querySelector: (sel) => (
                sel === 'home-assistant-main' ? ham : null
            ),
        };
        const main = {
            shadowRoot: main_shadow,
            hass: { connected: true, states: { 'sensor.x': {} } },
        };
        global.document = {
            querySelector: (sel) => (
                sel === 'home-assistant' ? main : null
            ),
        };
        """
        # Strip the trailing semicolon safety — the predicate is the
        # arrow expression ``() => { ... }``; we invoke it with ``()``.
        js_source = (
            dom_setup
            + "\n"
            + "const predicate = "
            + final_predicate
            + ";\n"
            + "console.log(JSON.stringify({ result: Boolean(predicate()) }));"
        )

        completed = subprocess.run(  # noqa: S603
            [node_bin, "-e", js_source],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, (
            f"Node evaluation failed: {completed.stderr}\n"
            f"Predicate source:\n{final_predicate}"
        )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload["result"] is True, (
            "Final-stage predicate returned FALSE when given a DOM "
            "snapshot representing a stable Lovelace panel (hui-root "
            "mounted inside panel.shadowRoot, main.hass.connected "
            "true) BUT with panel.hass transiently null — the exact "
            "cloud-variant race that drove the 74958ms timeout on run "
            "24956110840.\n\nThe predicate is over-constraining: it "
            "requires panel.hass to be truthy, but hui-root being "
            "mounted already proves the panel passed through the wired "
            "state.  hui-root's presence is a synchronous DOM fact "
            "that survives the panel.hass wire-up race.\n\n"
            f"Predicate source:\n{final_predicate}"
        )


class TestWaitForLovelacePanelEntityModeInitRace:
    """The final-stage predicate must survive the entity-mode init race
    (C-031: no flaky tests).

    **Background** (diagnosed 2026-05-03 from Flaky Test Detection run,
    victim ``test_time_picker_stays_open_during_rerender[entity]``):
    the page fixture setup timed out at ``74951ms`` — virtually the
    entire 75000ms overall budget consumed without the final-stage
    predicate ever converging.  Same worker (gw2) succeeded for the
    ``[cloud]`` variant in 15.2s and failed for the ``[entity]``
    variant in 84.9s (75s wait + ~10s container init).

    **Root-cause diagnosis (what entity-mode does that cloud doesn't)**:
    The stage-3 predicate requires THREE signals to be *simultaneously*
    true on a single poll:

      (a) ``main.hass.connected === true`` — HA frontend WS session.
      (b) ``ha-panel-lovelace`` attached inside the main shadow root.
      (c) ``hui-root`` mounted inside ``panel.shadowRoot``.

    Signal (a) is a live JS property that reflects the *current* state
    of HA's WebSocket connection.  Under entity-mode configuration the
    YAML seed adds multiple ``input_number`` / ``input_select`` /
    ``input_boolean`` helpers.  Their registration and initial state
    burst (plus the foxess ``EntityCoordinator``'s first refresh
    reading each mapped entity) produces a heavier state-change
    stream than cloud mode's single REST poll.  That churn can cause
    HA's frontend WS session to transiently drop ``connected = false``
    for long enough to miss several Playwright polls — while signals
    (b) and (c) are stable synchronous DOM facts that persist through
    the churn.

    The failure mode: by the time entity-mode init has fully settled
    (``connected`` stably true again), the Playwright poll budget has
    been consumed scanning transient-falsy snapshots.  ``hui-root``'s
    presence is strictly stronger proof than ``hass.connected``: the
    Lit render that produced ``hui-root`` required ``hass.connected``
    to be true at render time, so ``hui-root`` implies the connection
    was live when the panel rendered — the runtime transient flip does
    not retroactively invalidate the render.

    **Fix contract** (what these tests assert):
    1. When ``hui-root`` is mounted inside ``panel.shadowRoot`` AND
       the rest of the shadow-DOM chain resolves, the predicate must
       return truthy even if ``main.hass.connected`` is transiently
       ``false`` at poll time.  This is the entity-mode race.
    2. The cloud-mode-shaped snapshot (all signals stably truthy) must
       continue to succeed — the fix must not regress the working
       variant.
    """

    def _run_predicate(self, js_source: str) -> bool:
        """Execute the predicate JS via node and return its boolean result.

        Raises pytest.skip if node is unavailable (CI environment
        guard — the syntactic check in the sibling test is the
        fallback).
        """
        import json  # noqa: PLC0415
        import shutil  # noqa: PLC0415
        import subprocess  # noqa: PLC0415

        node_bin = shutil.which("node")
        if node_bin is None:
            pytest.skip("node.js unavailable — cannot execute predicate JS")

        completed = subprocess.run(  # noqa: S603
            [node_bin, "-e", js_source],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail(
                f"Node evaluation failed: {completed.stderr}\nsource:\n{js_source}"
            )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        return bool(payload["result"])

    def test_predicate_truthy_when_hui_root_present_despite_connected_false(
        self,
    ) -> None:
        """Predicate must return truthy when ``hui-root`` is mounted
        even if ``main.hass.connected`` is transiently false.

        This is the exact entity-mode race: by the time the final
        stage's ``wait_for_function`` is polling, the Lovelace panel
        has rendered (``hui-root`` is in the DOM), but the heavy
        state-change churn from entity mode's input-helper bursts
        has briefly flipped ``main.hass.connected`` to false.

        A predicate that gates on ``main.hass.connected`` rejects
        this snapshot and keeps polling.  If the churn outlasts the
        budget (74951ms observed), the whole fixture times out.

        The fix: ``hui-root`` being mounted is itself proof the
        connection was established at render time.  The predicate
        should accept it without also requiring the live
        ``connected`` flag to be true at poll time.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        stages = getattr(conftest, "_LOVELACE_PANEL_STAGES", None)
        assert stages is not None, "Helper missing _LOVELACE_PANEL_STAGES"
        _final_stage_name, final_predicate = stages[-1]

        # Adversarial entity-mode snapshot: every DOM signal resolves,
        # hui-root is mounted, BUT main.hass.connected is transiently
        # false (WS reconnect during input-helper state-burst).
        dom_setup = r"""
        const panel_shadow = {
            querySelector: (sel) => (
                sel === 'hui-root' ? {nodeName: 'HUI-ROOT'} : null
            ),
        };
        const panel = {
            shadowRoot: panel_shadow,
            hass: { connected: true, states: { 'sensor.x': {} } },
        };
        const ham_shadow = {
            querySelector: (sel) => (
                sel === 'ha-panel-lovelace' ? panel : null
            ),
        };
        const ham = { shadowRoot: ham_shadow };
        const main_shadow = {
            querySelector: (sel) => (
                sel === 'home-assistant-main' ? ham : null
            ),
        };
        const main = {
            shadowRoot: main_shadow,
            // The entity-mode race: connected is transiently false
            // while HA's WS reconnects during input-helper state flux,
            // even though hui-root has already rendered.
            hass: { connected: false, states: { 'sensor.x': {} } },
        };
        global.document = {
            querySelector: (sel) => (
                sel === 'home-assistant' ? main : null
            ),
        };
        """
        js_source = (
            dom_setup
            + "\nconst predicate = "
            + final_predicate
            + ";\nconsole.log(JSON.stringify({ result: Boolean(predicate()) }));"
        )
        result = self._run_predicate(js_source)
        assert result is True, (
            "Final-stage predicate returned FALSE when given the "
            "entity-mode adversarial snapshot: hui-root mounted inside "
            "panel.shadowRoot, but main.hass.connected transiently "
            "false.\n\n"
            "This is the exact race that drove the 74951ms timeout on "
            "test_time_picker_stays_open_during_rerender[entity] — "
            "entity mode's additional input-helper registrations plus "
            "EntityCoordinator first-refresh state bursts cause HA's "
            "frontend WS to flap connected=true→false→true, and a "
            "predicate that requires connected=true at poll time "
            "cannot converge if the churn outlasts the budget.\n\n"
            "hui-root being mounted is STRICTLY STRONGER: the Lit "
            "render that produced it required hass.connected to be "
            "true at render time, so hui-root's DOM presence is proof "
            "the connection was established — the transient flap does "
            "not retroactively invalidate the render.\n\n"
            f"Predicate source:\n{final_predicate}"
        )

    def test_predicate_still_truthy_on_cloud_happy_path(self) -> None:
        """Neighbourhood guard: the cloud-mode-shaped snapshot (every
        signal stably truthy) must continue to succeed.

        This ensures the fix for entity-mode does not regress the
        cloud variant, which today succeeds in ~15s.  If the fix
        accidentally weakened the predicate to always-true, this test
        would still pass; the guard against that is the sibling
        ``test_predicate_falsy_when_panel_not_mounted`` below.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        stages = getattr(conftest, "_LOVELACE_PANEL_STAGES", None)
        assert stages is not None, "Helper missing _LOVELACE_PANEL_STAGES"
        _final_stage_name, final_predicate = stages[-1]

        dom_setup = r"""
        const panel_shadow = {
            querySelector: (sel) => (
                sel === 'hui-root' ? {nodeName: 'HUI-ROOT'} : null
            ),
        };
        const panel = {
            shadowRoot: panel_shadow,
            hass: { connected: true, states: { 'sensor.x': {} } },
        };
        const ham_shadow = {
            querySelector: (sel) => (
                sel === 'ha-panel-lovelace' ? panel : null
            ),
        };
        const ham = { shadowRoot: ham_shadow };
        const main_shadow = {
            querySelector: (sel) => (
                sel === 'home-assistant-main' ? ham : null
            ),
        };
        const main = {
            shadowRoot: main_shadow,
            hass: { connected: true, states: { 'sensor.x': {} } },
        };
        global.document = {
            querySelector: (sel) => (
                sel === 'home-assistant' ? main : null
            ),
        };
        """
        js_source = (
            dom_setup
            + "\nconst predicate = "
            + final_predicate
            + ";\nconsole.log(JSON.stringify({ result: Boolean(predicate()) }));"
        )
        assert self._run_predicate(js_source) is True, (
            "Cloud-happy-path snapshot (every signal stably truthy) "
            "failed — fix regressed the working variant."
        )

    def test_predicate_falsy_when_panel_not_mounted(self) -> None:
        """Negative guard: when ``ha-panel-lovelace`` has NOT yet
        attached, the predicate must return falsy — regardless of
        whether ``main.hass.connected`` is true.

        This prevents the fix from degenerating to "always return
        true": the predicate still needs to wait for the panel to
        actually mount before reporting ready.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        stages = getattr(conftest, "_LOVELACE_PANEL_STAGES", None)
        assert stages is not None, "Helper missing _LOVELACE_PANEL_STAGES"
        _final_stage_name, final_predicate = stages[-1]

        # Panel has NOT yet mounted — stage-3 early-boot snapshot.
        dom_setup = r"""
        const ham_shadow = {
            querySelector: (sel) => null, // panel not yet attached
        };
        const ham = { shadowRoot: ham_shadow };
        const main_shadow = {
            querySelector: (sel) => (
                sel === 'home-assistant-main' ? ham : null
            ),
        };
        const main = {
            shadowRoot: main_shadow,
            hass: { connected: true, states: { 'sensor.x': {} } },
        };
        global.document = {
            querySelector: (sel) => (
                sel === 'home-assistant' ? main : null
            ),
        };
        """
        js_source = (
            dom_setup
            + "\nconst predicate = "
            + final_predicate
            + ";\nconsole.log(JSON.stringify({ result: Boolean(predicate()) }));"
        )
        assert self._run_predicate(js_source) is False, (
            "Predicate returned TRUE when ha-panel-lovelace was not "
            "mounted — fix weakened the predicate to always-true, "
            "which would cause the fixture to return before the "
            "panel is actually ready."
        )

    def test_predicate_falsy_when_hui_root_not_mounted(self) -> None:
        """Negative guard: when ``hui-root`` has NOT yet rendered,
        the predicate must return falsy.

        Rationale: without ``hui-root``, there is no proof the panel
        has completed a render cycle.  Even a live
        ``main.hass.connected`` + attached panel is insufficient —
        the test body would still race the first render on entry.
        This ensures the fix does not accept a bare-attach snapshot.
        """
        from tests.e2e import conftest  # noqa: PLC0415

        stages = getattr(conftest, "_LOVELACE_PANEL_STAGES", None)
        assert stages is not None, "Helper missing _LOVELACE_PANEL_STAGES"
        _final_stage_name, final_predicate = stages[-1]

        # Panel mounted but hui-root has NOT yet rendered inside it.
        dom_setup = r"""
        const panel_shadow = {
            querySelector: (sel) => null, // hui-root not yet rendered
        };
        const panel = {
            shadowRoot: panel_shadow,
            hass: { connected: true, states: { 'sensor.x': {} } },
        };
        const ham_shadow = {
            querySelector: (sel) => (
                sel === 'ha-panel-lovelace' ? panel : null
            ),
        };
        const ham = { shadowRoot: ham_shadow };
        const main_shadow = {
            querySelector: (sel) => (
                sel === 'home-assistant-main' ? ham : null
            ),
        };
        const main = {
            shadowRoot: main_shadow,
            hass: { connected: true, states: { 'sensor.x': {} } },
        };
        global.document = {
            querySelector: (sel) => (
                sel === 'home-assistant' ? main : null
            ),
        };
        """
        js_source = (
            dom_setup
            + "\nconst predicate = "
            + final_predicate
            + ";\nconsole.log(JSON.stringify({ result: Boolean(predicate()) }));"
        )
        assert self._run_predicate(js_source) is False, (
            "Predicate returned TRUE when hui-root was not mounted — "
            "fix weakened the predicate to always-true or dropped "
            "the hui-root check, which would cause the fixture to "
            "return before the panel has completed a render cycle."
        )


class TestWaitForLovelacePanelSustainedRetryBudget:
    """The helper must survive a SUSTAINED context-destruction storm
    without bleeding the overall budget on per-retry settle waits
    (C-031: no flaky tests).

    **Background** (diagnosed 2026-05-19 from Flaky Test Detection on
    v1.0.17-beta.2, victim ``test_card_renders[entity]``): the page
    fixture timed out at ``74969ms`` — virtually the entire 75000ms
    overall budget consumed.  Container ready in 8.5s, so the gap is
    in the predicate-settle phase.  Earlier hardenings already settled
    the final-stage predicate on the synchronous DOM fact ``hui-root``
    (commit 5cc74bf) — yet the predicate still failed to converge.

    **Root-cause diagnosis**: the per-retry settle inside
    ``_wait_for_stage`` calls
    ``page.wait_for_load_state("networkidle", timeout=min(_, 15000))``.
    Under sustained CI churn (entity-mode WS state-burst from input-
    helper registrations + EntityCoordinator first refresh), HA's
    frontend can fire several navigation events back-to-back.  Each
    one destroys the JS execution context, our retry loop catches it,
    and we sit in ``wait_for_load_state`` for its full timeout because
    ``networkidle`` may never fire under sustained traffic — the
    suppressed PlaywrightError consumes the cap on every retry.

    Five such retries × 15000ms each = 75000ms — fully consumes the
    overall budget without ever giving ``wait_for_function`` time to
    actually observe ``hui-root`` once the churn subsides.  The
    container reported ready 67s before the timeout fired, so HA had
    plenty of wall-clock to render the panel — the helper just never
    reached a stable poll because the budget was burned on settle
    waits between retries.

    **Fix contract** (what these tests assert):
    1. The per-retry ``wait_for_load_state`` cap must be modest
       (≤ 5000ms), not 15000ms.  All we need from settle is a brief
       moment for the new context to attach — a settle signal never
       firing under sustained traffic must not eat the budget.
    2. The total time the helper can spend in settle calls across all
       retries must be bounded so a sustained retry storm cannot
       dominate the overall budget — at least half of the overall
       budget must remain available for the actual predicate polls
       that observe the predicate becoming truthy.

    **Surface migration (2026-06-03)**: the retry/settle loop now lives
    in ``conftest.wait_for_condition``, which polls ``page.evaluate`` and
    settles a context-destroyed navigation with
    ``page.wait_for_load_state("domcontentloaded", timeout=min(remaining,
    3000))``.  These tests drive ``page.evaluate`` (not
    ``page.wait_for_function``) to raise the context-destroyed error; the
    settle-cap and total-settle contracts are unchanged.  The implemented
    cap is 3000ms (well within the ≤ 5000ms contract); a regression to an
    unbounded 15000ms cap would fail test 1 here.
    """

    def test_per_retry_settle_is_bounded_to_modest_cap(self) -> None:
        """Each ``wait_for_load_state`` call after a context destruction
        must be capped at ≤ 5000ms.

        Why: ``networkidle`` may never fire under sustained WS / HTTP
        traffic.  A 15000ms cap on each settle means 5 retries consume
        the entire 75000ms budget before any successful predicate poll
        can occur.  A 5000ms cap gives the new context time to attach
        without dominating the budget.

        This test injects three successive context-destruction errors
        on ``page.evaluate`` and inspects the timeouts the helper
        requested for ``wait_for_load_state``.  Every observed call
        must be ≤ 5000ms.  A helper using the unbounded 15000ms cap
        will fail this assertion.
        """
        helper = _get_helper()
        page = MagicMock()
        settle_timeouts: list[int] = []
        fired = {"count": 0}
        target_destructions = 3

        def _evaluate(expr: str) -> object:
            # The fail_check must stay falsy (no HA error panel) so the
            # stage does not abort early.
            if _is_fail_check(expr):
                return False
            # Simulate sustained context destruction: raise on the first
            # ``target_destructions`` pass_check evaluations (each drives
            # one settle), then let the predicate succeed.
            if fired["count"] < target_destructions:
                fired["count"] += 1
                raise PlaywrightError(
                    "Page.evaluate: Execution context was destroyed, "
                    "most likely because of a navigation"
                )
            return True

        def _wait_load_state(state: str, timeout: int = 30000, **_kw: Any) -> None:  # noqa: ARG001
            settle_timeouts.append(timeout)
            return None

        page.evaluate.side_effect = _evaluate
        page.wait_for_load_state.side_effect = _wait_load_state

        helper(page, timeout_ms=75000)

        assert settle_timeouts, (
            "Expected helper to invoke wait_for_load_state at least once "
            "during the context-destruction retry path; got no calls."
        )
        # The per-call cap must be modest — large enough for the new
        # context to attach (~1-3s typical), small enough that several
        # back-to-back retries cannot consume the overall budget.
        for t in settle_timeouts:
            assert t <= 5000, (
                f"wait_for_load_state was called with timeout={t}ms.  Each "
                f"per-retry settle must be capped at ≤ 5000ms — under "
                f"sustained CI churn (entity-mode WS state-burst on a slow "
                f"shard), networkidle may never fire and a 15000ms cap "
                f"means 5 retries consume the entire 75000ms overall "
                f"budget before any successful predicate poll occurs.  "
                f"This is the v1.0.17-beta.2 flake on "
                f"test_card_renders[entity] (74969ms timeout, container "
                f"ready in 8.5s).  Observed all settle timeouts: "
                f"{settle_timeouts}"
            )

    def test_total_settle_budget_does_not_dominate_overall(self) -> None:
        """Across a sustained retry storm, the total time the helper
        commits to ``wait_for_load_state`` settle calls must leave the
        majority of the overall budget for the actual predicate polls.

        Concretely: with ``timeout_ms=75000`` and 5 successive context-
        destruction events, the SUM of per-call ``wait_for_load_state``
        timeouts must not exceed ~50% of the overall budget.

        This is a structural assertion — independent of how the fix is
        shaped (per-call cap vs. cumulative cap vs. shorter ``state``
        argument), the observable contract is "settle calls cannot
        dominate the budget."
        """
        helper = _get_helper()
        page = MagicMock()
        settle_timeouts: list[int] = []
        fired = {"count": 0}
        target_destructions = 5

        def _evaluate(expr: str) -> object:
            if _is_fail_check(expr):
                return False
            if fired["count"] < target_destructions:
                fired["count"] += 1
                raise PlaywrightError(
                    "Page.evaluate: Execution context was destroyed, "
                    "most likely because of a navigation"
                )
            return True

        def _wait_load_state(state: str, timeout: int = 30000, **_kw: Any) -> None:  # noqa: ARG001
            settle_timeouts.append(timeout)
            return None

        page.evaluate.side_effect = _evaluate
        page.wait_for_load_state.side_effect = _wait_load_state

        overall_budget_ms = 75000
        helper(page, timeout_ms=overall_budget_ms)

        assert settle_timeouts, (
            "Expected helper to invoke wait_for_load_state during the "
            "context-destruction retry path."
        )
        total_settle_ms = sum(settle_timeouts)
        # The settle commitments across all retries must not exceed
        # half of the overall budget — otherwise predicate polling has
        # insufficient time to observe convergence.
        assert total_settle_ms <= overall_budget_ms // 2, (
            f"Helper committed {total_settle_ms}ms across "
            f"{len(settle_timeouts)} wait_for_load_state calls — that is "
            f"more than half of the {overall_budget_ms}ms overall budget. "
            f"Under sustained CI churn (where networkidle may never fire "
            f"and each settle consumes its full cap), this means the "
            f"helper cannot give wait_for_function enough time to observe "
            f"hui-root after the churn subsides.  Observed settle "
            f"timeouts: {settle_timeouts}"
        )
