"""A failed Lovelace *panel module* load must be detected, diagnosed and
recovered — not waited out for 75 seconds.

The failure
-----------
``Flaky Test Detection`` has never passed (every release back to
``v1.0.21-beta.2``).  Its signature is always an **error at setup** of
the E2E ``page`` fixture::

    tests.e2e.conftest.E2EConditionTimeout: lovelace-stage:ha-panel-lovelace:
    pass_check not satisfied within 74991ms. capture #1
    | elements present: ['home-assistant', 'home-assistant-main']

The captured PNG (run 32953128115, artifact ``e2e-results-3``) shows a
fully-rendered Home Assistant — sidebar, logged-in user, page ``<title>``
already updated to ``Overview – Home Assistant`` — with a red alert in the
content area reading **"Error while loading page lovelace."** and a *Back*
link.  The dashboard did not render slowly; it had already failed.

Why waiting can never help
--------------------------
``/lovelace/0`` renders through HA's ``hass-router-page``.  The route's
``load()`` for the ``lovelace`` panel is a ``Promise.all`` over **51**
separate lazily-imported webpack chunks (verified against the shipped
bundle: ``lovelace:()=>Promise.all([a.e(4801),a.e(44947),...])`` — 51
``a.e(...)`` entries).  If any single one of those asset requests fails,
``hass-router-page`` runs::

    i.catch(e => { console.error("Error loading page", o, e);
                   ...
                   this.appendChild(this.createErrorScreen(
                       `Error while loading page ${o}.`)) })

and ``createErrorScreen`` appends ``<hass-error-screen>``.  The rejected
import is never re-issued for the life of that document, so the document
is *terminally* broken: ``hui-root`` will never appear, no matter how long
the fixture polls.  A 75s budget is not merely generous here, it is
misdirected — and the error it finally reports ("pass_check not satisfied")
misdiagnoses a dead page as a slow one.

Two defects, both in the harness
--------------------------------
1. **No detection.**  ``_LOVELACE_FAIL_CHECK`` fails fast on
   ``ha-panel-error`` / ``hui-error-card``.  HA uses neither for a panel
   *module* load failure — it uses ``hass-error-screen`` — so the
   fail-fast path never fired and every occurrence burned the full budget
   (observed setup times 89-95s).
2. **No diagnosis.**  The browser logged the underlying cause
   (``console.error("Error loading page", "lovelace", err)``), and the
   failing asset request raised a Playwright ``requestfailed`` event.  The
   harness recorded neither, so three months of CI artefacts contain the
   symptom and none of the cause.  ``_DOM_SUMMARY_JS`` could not even
   report the alert text: it looked for the wrong tags *and* used a
   non-recursive ``document.querySelector``, while the element lives two
   shadow roots deep.

Recovery: re-navigation, not a longer wait
------------------------------------------
There is no precondition to wait on.  The missing thing is an HTTP
response that was already refused; nothing in HA or in the harness will
ask for it again in this document.  Re-navigating is the *only* mechanism
that re-issues the panel import.  The recovery is therefore a **bounded
re-navigation triggered by a positively-identified terminal state** — not
a blind retry and not a tuned timeout.  A plain timeout (no error screen)
must still fail on the first attempt, which
``test_plain_timeout_is_not_retried`` pins down.

Test shape
----------
* The detection tests drive a **real Chromium DOM** (same approach as
  ``tests/test_card_entity_resolution.py``): they build HA's exact
  shadow-DOM shape at the moment of failure and evaluate the *actual*
  predicate strings the fixture uses.  That is a much stronger guard than
  asserting on the JS source text.
* The recovery tests drive ``open_lovelace_dashboard`` with a mocked page,
  because the trigger (a lost asset request on a starved runner) cannot be
  produced deterministically.

Refs C-031 (no flaky tests — root cause, no masking), C-029.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from tests.e2e import conftest as e2e_conftest

if TYPE_CHECKING:
    from playwright.sync_api import Page


def _surface(name: str) -> Any:
    """Fetch a ``tests.e2e.conftest`` surface by name.

    The indirection exists for the same reason as
    ``tests/test_e2e_page_fixture.py::_get_helper``: it keeps this module
    importable and type-checkable while the surface it demands does not yet
    exist (tests land before the fix), and turns a later removal into a
    legible assertion instead of an ``AttributeError`` mid-test.
    """
    try:
        return getattr(e2e_conftest, name)
    except AttributeError as exc:  # pragma: no cover - fix not applied yet
        msg = (
            f"tests.e2e.conftest.{name} is not defined. The E2E harness must "
            "detect HA's terminal panel-load error screen, recover from it by "
            "re-navigating, and record the browser-side cause."
        )
        raise AssertionError(msg) from exc


# ---------------------------------------------------------------------------
# Real-DOM builders — HA's shadow-DOM shape, reproduced exactly.
#
#   <home-assistant>            (document body)
#     #shadow-root
#       <home-assistant-main>
#         #shadow-root
#           <partial-panel-resolver>
#             <hass-error-screen error="Error while loading page lovelace.">
#               #shadow-root
#                 <ha-alert>Error while loading page lovelace.</ha-alert>
#
# ``hass-error-screen`` is a *light-DOM child* of the resolver, which
# itself lives inside ``home-assistant-main``'s shadow root — two shadow
# hops from the document.  ``error`` is a Lit property, not a reflected
# attribute, so the message is only reachable via the JS property or by
# descending into the element's own shadow root.
# ---------------------------------------------------------------------------

_PANEL_LOAD_ERROR_TEXT = "Error while loading page lovelace."

_BUILD_PANEL_LOAD_ERROR_DOM = """(msg) => {
    document.body.replaceChildren();
    const ha = document.createElement('home-assistant');
    document.body.appendChild(ha);
    const haRoot = ha.attachShadow({mode: 'open'});
    const main = document.createElement('home-assistant-main');
    haRoot.appendChild(main);
    const mainRoot = main.attachShadow({mode: 'open'});
    const resolver = document.createElement('partial-panel-resolver');
    mainRoot.appendChild(resolver);
    const err = document.createElement('hass-error-screen');
    err.error = msg;
    const errRoot = err.attachShadow({mode: 'open'});
    const alert = document.createElement('ha-alert');
    alert.setAttribute('alert-type', 'error');
    alert.textContent = msg;
    errRoot.appendChild(alert);
    resolver.appendChild(err);
}"""

_BUILD_HEALTHY_DASHBOARD_DOM = """() => {
    document.body.replaceChildren();
    const ha = document.createElement('home-assistant');
    document.body.appendChild(ha);
    const haRoot = ha.attachShadow({mode: 'open'});
    const main = document.createElement('home-assistant-main');
    haRoot.appendChild(main);
    const mainRoot = main.attachShadow({mode: 'open'});
    const panel = document.createElement('ha-panel-lovelace');
    mainRoot.appendChild(panel);
    const panelRoot = panel.attachShadow({mode: 'open'});
    const huiRoot = document.createElement('hui-root');
    panelRoot.appendChild(huiRoot);
}"""


class TestPanelLoadErrorIsDetectedInTheDom:
    """The fail-fast predicate must recognise HA's panel-load error screen.

    Evaluated against a real browser DOM, so the test fails if the
    predicate's *behaviour* is wrong — not merely if its source text
    changed.
    """

    def test_fail_check_trips_on_hass_error_screen(self, page: Page) -> None:
        """``hass-error-screen`` two shadow hops deep must trip the check.

        This is the exact DOM the CI capture shows.  Against the
        pre-fix predicate (``ha-panel-error, hui-error-card``) the check
        stays falsy, which is why the wait burned the whole 75s budget
        instead of aborting in the first poll.
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(_BUILD_PANEL_LOAD_ERROR_DOM, _PANEL_LOAD_ERROR_TEXT)

        tripped = page.evaluate(e2e_conftest._LOVELACE_FAIL_CHECK)

        assert tripped, (
            "_LOVELACE_FAIL_CHECK must trip on <hass-error-screen> — the "
            "element HA appends when the Lovelace panel's 51-chunk module "
            "import rejects. Without this the page fixture polls a "
            "terminally-broken document for the full 75s and reports a "
            "misleading timeout."
        )

    def test_fail_check_silent_on_healthy_dashboard(self, page: Page) -> None:
        """Negative control: a healthy dashboard must NOT trip the check.

        Guards against 'fix' by over-broad selector — a fail_check that
        matched anything would abort every E2E test at setup.
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(_BUILD_HEALTHY_DASHBOARD_DOM)

        assert not page.evaluate(e2e_conftest._LOVELACE_FAIL_CHECK)
        assert page.evaluate(e2e_conftest._STAGE_HA_PANEL_LOVELACE), (
            "sanity: the healthy DOM must satisfy the stage-3 predicate"
        )

    def test_dom_summary_reports_the_panel_load_error_message(self, page: Page) -> None:
        """The failure capture must name the error, not just list elements.

        The CI summary read ``elements present: ['home-assistant',
        'home-assistant-main']`` — technically true and diagnostically
        useless.  ``_DOM_SUMMARY_JS`` must (a) list
        ``hass-error-screen`` and (b) surface its ``error`` property,
        which needs a *recursive* shadow-DOM search (the pre-fix code
        used a flat ``document.querySelector``).
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(_BUILD_PANEL_LOAD_ERROR_DOM, _PANEL_LOAD_ERROR_TEXT)

        info = page.evaluate(e2e_conftest._DOM_SUMMARY_JS)

        assert "hass-error-screen" in info["tags"], (
            f"_DOM_SUMMARY_JS must report hass-error-screen; got {info['tags']}"
        )
        assert info["error_text"] and _PANEL_LOAD_ERROR_TEXT in info["error_text"], (
            "_DOM_SUMMARY_JS must surface the error-screen message so the CI "
            f"log says *why* the panel failed; got {info['error_text']!r}"
        )


class TestPanelLoadErrorRecovery:
    """A terminal panel-load error must be re-navigated, boundedly."""

    @staticmethod
    def _page(
        *,
        error_screen_until_goto: int = 0,
        panel_ready_after_goto: int = 1,
        never_ready: bool = False,
    ) -> tuple[Any, dict[str, int]]:
        """Fake page whose DOM state depends on how often ``goto`` ran.

        ``error_screen_until_goto``: the error screen is present while
        ``goto`` has been called at most this many times.
        ``panel_ready_after_goto``: stage predicates go truthy once
        ``goto`` has been called at least this many times.
        """
        page = MagicMock()
        state = {"gotos": 0}

        def _goto(*_a: object, **_k: object) -> None:
            state["gotos"] += 1

        page.goto.side_effect = _goto

        def _evaluate(expr: str, *_a: object, **_k: object) -> Any:
            if "hass-error-screen" in expr:
                return state["gotos"] <= error_screen_until_goto
            if never_ready:
                return False
            return state["gotos"] >= panel_ready_after_goto

        page.evaluate.side_effect = _evaluate
        return page, state

    def test_terminal_panel_error_raises_a_recoverable_error_type(self) -> None:
        """``_wait_for_stage`` must raise the dedicated panel-load type.

        The caller has to tell "HA rendered an error screen — re-navigate"
        apart from "the predicate never became true — give up".  A shared
        ``E2EConditionFailed`` cannot express that.
        """
        page, _ = self._page(error_screen_until_goto=99)

        with pytest.raises(_surface("E2EPanelLoadError")):
            e2e_conftest._wait_for_lovelace_panel(page, timeout_ms=500)

    def test_renavigates_once_after_a_panel_load_error(self) -> None:
        """First load poisoned, second load clean → the fixture succeeds."""
        page, state = self._page(error_screen_until_goto=1, panel_ready_after_goto=2)

        _surface("open_lovelace_dashboard")(page, 8123, timeout_ms=2000)

        assert state["gotos"] == 2, (
            "expected exactly one re-navigation after the panel-load error, "
            f"got {state['gotos']} navigations"
        )

    def test_gives_up_after_bounded_attempts(self) -> None:
        """A permanently broken dashboard must fail, not loop for ever."""
        page, state = self._page(error_screen_until_goto=99)

        with pytest.raises(_surface("E2EPanelLoadError")):
            _surface("open_lovelace_dashboard")(page, 8123, timeout_ms=500, attempts=3)

        assert state["gotos"] == 3, (
            f"recovery must be bounded to 3 navigations, got {state['gotos']}"
        )

    def test_plain_timeout_is_not_retried(self) -> None:
        """No error screen → no re-navigation.

        This is what keeps the fix from degenerating into a blind retry
        (C-031): only a positively-identified terminal error screen earns
        a second navigation.  A slow or genuinely hung dashboard still
        fails on the first attempt.
        """
        page, state = self._page(never_ready=True)

        with pytest.raises(e2e_conftest.E2EConditionTimeout):
            _surface("open_lovelace_dashboard")(page, 8123, timeout_ms=500)

        assert state["gotos"] == 1, (
            f"a plain timeout must not be retried; got {state['gotos']} navigations"
        )


class TestBrowserDiagnosticsAreCaptured:
    """The failure capture must include *why* the browser failed."""

    def test_capture_failure_includes_recorded_browser_diagnostics(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Console errors / failed requests must reach the CI artefact.

        HA logs ``console.error("Error loading page", "lovelace", err)``
        with the underlying reason, and Playwright raises
        ``requestfailed`` for the lost asset.  Both were discarded, which
        is why this went undiagnosed for three months.
        """
        monkeypatch.setattr(e2e_conftest, "_failure_capture_dir", lambda: tmp_path)
        page = MagicMock()
        page.evaluate.return_value = {"tags": [], "error_text": None}
        page.content.return_value = "<html></html>"

        diag = _surface("attach_page_diagnostics")(page)
        diag.append("console[error] Error loading page lovelace TypeError: boom")
        diag.append(
            "requestfailed http://localhost:1/frontend_latest/x.js "
            ":: net::ERR_CONNECTION_RESET"
        )

        summary = e2e_conftest._capture_failure(page, "lovelace-stage:x")

        assert "ERR_CONNECTION_RESET" in summary, (
            f"the failure summary must name the browser-side cause; got {summary!r}"
        )
        logs = list(tmp_path.glob("*.log"))
        assert logs, "the full browser diagnostic log must be written as an artefact"
        assert "Error loading page lovelace" in logs[0].read_text()

    def test_attach_registers_listeners_for_the_relevant_events(self) -> None:
        """Recording must cover console, page errors and failed requests."""
        page = MagicMock()

        _surface("attach_page_diagnostics")(page)

        events = {call.args[0] for call in page.on.call_args_list}
        assert {"console", "pageerror", "requestfailed"} <= events, (
            f"missing diagnostic listeners; registered {events}"
        )


class TestPageFixtureRoutesThroughRecovery:
    """Structural guard: the fixture must not hand-roll the navigation.

    Mirrors ``tests/test_e2e_helper_usage_policy.py`` — if a future edit
    reinstates a bare ``goto`` + ``_wait_for_lovelace_panel`` in the
    fixture, the flake returns silently.  This surfaces it at unit-test
    time.
    """

    @staticmethod
    def _page_fixture_body() -> ast.FunctionDef:
        src = Path(e2e_conftest.__file__).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "page":
                return node
        pytest.fail("tests/e2e/conftest.py no longer defines a `page` fixture")

    def _fixture_calls(self) -> set[str]:
        return {
            n.func.id
            for n in ast.walk(self._page_fixture_body())
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    def test_page_fixture_calls_open_lovelace_dashboard(self) -> None:
        called = self._fixture_calls()
        assert "open_lovelace_dashboard" in called, (
            "the `page` fixture must open the dashboard through "
            "open_lovelace_dashboard so a poisoned document is recovered"
        )
        assert "attach_page_diagnostics" in called, (
            "the `page` fixture must attach the browser diagnostic recorder "
            "so a failure capture can report the cause"
        )

    def test_page_fixture_does_not_wait_for_the_panel_directly(self) -> None:
        assert "_wait_for_lovelace_panel" not in self._fixture_calls(), (
            "the `page` fixture must not call _wait_for_lovelace_panel "
            "directly — that path has no recovery for a poisoned document"
        )
