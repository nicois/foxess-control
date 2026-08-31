"""A failure capture must be able to explain a *card* failure.

The failure
-----------
CI run 33380962649 (``develop`` @ ``9d67d15``), E2E shard 7::

    FAILED tests/e2e/test_ui.py::TestFormInputPersistence::
    test_time_input_survives_rerender[entity]
        assert _find_card(page, "foxess-control-card")
    E   playwright._impl._errors.TimeoutError:
        Page.wait_for_function: Timeout 10000ms exceeded.

The artefact held ``form-input--wait-for-form-1.png`` and
``...-1.html`` — and nothing else.  The PNG shows the dashboard fully
rendered with the FoxESS control card present in its *collapsed* state
("No active operations", Charge/Discharge buttons, no form).  So the
card existed; the form never appeared.

Two diagnosability defects, both in ``tests/e2e/conftest.py``
------------------------------------------------------------
1. **The ``.html`` artefact cannot contain any card.**  It is written
   from ``page.content()``, i.e. ``document.documentElement.outerHTML``,
   and ``outerHTML`` **does not serialise shadow roots**.  Every FoxESS
   card lives three-plus shadow hops deep inside HA
   (``home-assistant`` → ``home-assistant-main`` →
   ``ha-panel-lovelace`` → … → ``foxess-control-card``), so *none* of
   the content the screenshot plainly shows appears in the dump.
   Grepping the real artefact for ``ha-textfield``, ``form-start`` and
   ``No active operations`` returns nothing at all.

   ``_DEEP_DOM_JS`` already taught the DOM *summary* to pierce shadow
   roots (the Lovelace panel-load fix), but the raw HTML dump still
   stops at the first shadow boundary.

2. **No ``.log`` was written.**  ``_capture_failure`` writes the
   browser-diagnostics log only ``if diagnostics:`` — i.e. only when the
   recorder buffer is non-empty.  The ``page`` fixture *does* attach the
   recorder (``attach_page_diagnostics``), so for this failure the
   buffer was simply empty: HA emitted no ``console.error`` /
   ``console.warning``, no ``pageerror``, and no ``requestfailed``.

   The gate is silent, so an absent ``.log`` is ambiguous between three
   very different states — "no recorder was attached", "a recorder was
   attached and the browser said nothing", and "the write itself
   failed".  The first is a harness bug, the second is *evidence* (it
   rules console errors out as the cause), the third is a lost artefact.
   Only an unconditional write can tell them apart.

Test shape
----------
The serialisation tests drive a **real Chromium DOM** — the same
approach as ``tests/test_e2e_lovelace_panel_load_error.py`` — building
HA's actual nesting around a ``foxess-control-card`` and asserting on
the *bytes written to the artefact*.  They assert behaviour, never the
serialiser's source text.

Four negative controls keep the tests from passing on a blind dump:

* ``test_shadow_and_light_dom_are_distinguishable`` — the shadow marker
  must land *inside* its host's shadow delimiters and the light-DOM
  marker *outside* them.  A structure-free "concatenate everything"
  dump fails this.
* ``test_hosts_are_not_duplicated`` — each host element is serialised
  once.  A dump that walks every element and appends its ``outerHTML``
  repeats every ancestor and fails this.
* ``test_closed_shadow_root_content_is_absent`` — a ``mode: 'closed'``
  root is unreachable and must not appear.
* ``test_absent_marker_stays_absent`` — a string that is nowhere in the
  document must be nowhere in the dump.

Refs C-031 (no flaky tests — diagnose the root cause, never tune the
trigger away), C-029, C-020.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from tests.e2e import conftest as e2e_conftest

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page


def _surface(name: str) -> Any:
    """Fetch a ``tests.e2e.conftest`` surface by name.

    Same indirection as ``tests/test_e2e_lovelace_panel_load_error.py``:
    it keeps this module importable and type-checkable while the surface
    it demands does not yet exist (tests land before the fix), and turns
    a later removal into a legible assertion rather than an
    ``AttributeError`` mid-test.
    """
    try:
        return getattr(e2e_conftest, name)
    except AttributeError as exc:  # pragma: no cover - fix not applied yet
        msg = (
            f"tests.e2e.conftest.{name} is not defined.  A failure capture "
            "must serialise open shadow roots into the .html artefact, "
            "otherwise no HA card can ever appear in it."
        )
        raise AssertionError(msg) from exc


# ---------------------------------------------------------------------------
# Markers.  Distinctive enough that a substring search cannot collide with
# anything HA or Playwright emits.
# ---------------------------------------------------------------------------

SHADOW_MARKER = "MARKER-SHADOW-7f3a91"
DEEP_MARKER = "MARKER-DEEPSHADOW-7f3a91"
LIGHT_MARKER = "MARKER-LIGHTDOM-7f3a91"
CLOSED_MARKER = "MARKER-CLOSEDROOT-7f3a91"
ABSENT_MARKER = "MARKER-NOWHERE-7f3a91"


# HA's real nesting around a FoxESS card, reproduced exactly:
#
#   <home-assistant>                      (document body)
#     #shadow-root
#       <home-assistant-main>
#         #shadow-root
#           <ha-panel-lovelace>
#             #shadow-root
#               <foxess-control-card>
#                 #shadow-root
#                   <div class="form">      <- SHADOW_MARKER
#                     <input id="form-start">
#                   <x-secret>              closed root -> CLOSED_MARKER
#                 <span>                    light-DOM child -> LIGHT_MARKER
#
# The card is four shadow hops from the document, and it has *both* a
# shadow child and a light-DOM child so the two can be told apart.
_BUILD_CARD_DOM = """(m) => {
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

    const card = document.createElement('foxess-control-card');
    panelRoot.appendChild(card);
    const cardRoot = card.attachShadow({mode: 'open'});

    const form = document.createElement('div');
    form.className = 'form';
    form.textContent = m.shadow;
    cardRoot.appendChild(form);
    const input = document.createElement('input');
    input.id = 'form-start';
    input.type = 'time';
    form.appendChild(input);

    // A nested card inside the card's shadow root: a card can contain
    // cards, so the walk must recurse rather than stop at depth 1.
    const inner = document.createElement('foxess-inner-card');
    cardRoot.appendChild(inner);
    const innerRoot = inner.attachShadow({mode: 'open'});
    const deep = document.createElement('p');
    deep.textContent = m.deep;
    innerRoot.appendChild(deep);

    // Unreachable: a closed root is not exposed to script at all.
    const secret = document.createElement('x-secret');
    cardRoot.appendChild(secret);
    secret.attachShadow({mode: 'closed'})
          .appendChild(document.createTextNode(m.closed));

    // Light-DOM child of the card (slotted content), NOT shadow content.
    const slotted = document.createElement('span');
    slotted.textContent = m.light;
    card.appendChild(slotted);
}"""


# A shadow chain far deeper than anything HA builds, to prove the walk is
# depth-bounded rather than merely recursive.
#
# The depth is chosen empirically: an otherwise-identical walk with the
# depth cap removed raises ``RangeError: Maximum call stack size
# exceeded`` at 4000 levels (it survives 2000).  Because
# ``_capture_failure`` wraps the HTML write in
# ``contextlib.suppress(Exception)``, that RangeError does not surface as
# a test error — it silently degrades the artefact.  So the assertion has
# to be that content from *inside* the chain is present, which the
# ``page.content()`` fallback cannot provide.
_DEEP_CHAIN_LEVELS = 4000

_BUILD_DEEP_CHAIN_DOM = """(n) => {
    document.body.replaceChildren();
    let host = document.createElement('x-level-0');
    document.body.appendChild(host);
    for (let i = 1; i < n; i++) {
        const root = host.attachShadow({mode: 'open'});
        const next = document.createElement('x-level-' + i);
        root.appendChild(next);
        host = next;
    }
}"""


def _capture_html(page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the production capture against ``page``; return the .html text."""
    monkeypatch.setattr(e2e_conftest, "_failure_capture_dir", lambda: tmp_path)
    e2e_conftest._capture_failure(page, "form-input: wait_for_form")
    written = sorted(tmp_path.glob("*.html"))
    assert written, (
        "_capture_failure wrote no .html artefact at all.  Every capture "
        "must leave a DOM dump behind; the CI artefact for run 33380962649 "
        "had one, and it must not regress to none."
    )
    return written[-1].read_text()


class TestFailureCaptureSerialisesShadowContent:
    """The ``.html`` artefact must contain the cards the PNG shows.

    Driven against a real browser DOM, so these fail if the capture's
    *behaviour* is wrong — not merely if its source text changed.
    """

    def test_playwright_page_content_cannot_see_shadow_content(
        self, page: Page
    ) -> None:
        """Characterisation, and the premise the rest of the class rests on.

        ``page.content()`` is ``document.documentElement.outerHTML``, and
        ``outerHTML`` does not serialise shadow roots.  If this ever
        starts passing, the built DOM is not actually shadow-hidden and
        every other test here proves nothing.
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(
            _BUILD_CARD_DOM,
            {
                "shadow": SHADOW_MARKER,
                "deep": DEEP_MARKER,
                "light": LIGHT_MARKER,
                "closed": CLOSED_MARKER,
            },
        )

        raw = page.content()

        assert SHADOW_MARKER not in raw, (
            "premise broken: page.content() unexpectedly serialised shadow "
            "content, so this module's DOM builder is not exercising the gap"
        )
        assert "foxess-control-card" not in raw, (
            "premise broken: the card must be shadow-hidden from "
            f"page.content(); got {raw!r}"
        )

    def test_capture_html_contains_shadow_dom_content(
        self, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The card, its form and a nested card must all reach the artefact.

        This is the whole point: the CI ``.html`` for the form-wait
        failure could not be grepped for ``form-start`` or for the card
        tag, so it could not distinguish "the form never opened" from
        "the form opened and was re-rendered away".
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(
            _BUILD_CARD_DOM,
            {
                "shadow": SHADOW_MARKER,
                "deep": DEEP_MARKER,
                "light": LIGHT_MARKER,
                "closed": CLOSED_MARKER,
            },
        )

        html = _capture_html(page, tmp_path, monkeypatch)

        assert "foxess-control-card" in html, (
            "the capture must name the card element; without it the "
            f"artefact cannot describe a card failure.  Got:\n{html}"
        )
        assert SHADOW_MARKER in html, (
            "content one shadow hop inside the card is missing — the walk "
            f"is not descending into open shadow roots.  Got:\n{html}"
        )
        assert 'id="form-start"' in html, (
            "the artefact must show whether the form input existed; that is "
            f"the exact question the failing test asked.  Got:\n{html}"
        )
        assert DEEP_MARKER in html, (
            "content inside a card *within* the card's shadow root is "
            "missing — the walk must recurse, not stop at the first "
            f"shadow boundary.  Got:\n{html}"
        )
        assert LIGHT_MARKER in html, (
            "light-DOM (slotted) children must survive; expanding shadow "
            f"roots must not drop the light tree.  Got:\n{html}"
        )

    def test_shadow_and_light_dom_are_distinguishable(
        self, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader must be able to tell shadow content from light DOM.

        Negative control against a structure-free dump: the shadow
        marker must fall *between* the card's shadow-root delimiters and
        the light-DOM marker *outside* them.  A capture that simply
        concatenated every root's ``innerHTML`` would contain both
        markers and fail here.
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(
            _BUILD_CARD_DOM,
            {
                "shadow": SHADOW_MARKER,
                "deep": DEEP_MARKER,
                "light": LIGHT_MARKER,
                "closed": CLOSED_MARKER,
            },
        )

        html = _capture_html(page, tmp_path, monkeypatch)

        # The delimiter must mention "shadow-root" so it is self-describing
        # to someone reading the artefact for the first time.
        bounds = [m.start() for m in re.finditer(r"shadow-root", html, re.IGNORECASE)]
        assert len(bounds) >= 2, (
            "shadow content must be delimited by a self-describing marker "
            "(open and close) so a reader can tell it from light DOM; found "
            f"{len(bounds)} occurrences of 'shadow-root' in:\n{html}"
        )

        card_at = html.index("foxess-control-card")
        shadow_at = html.index(SHADOW_MARKER)
        light_at = html.index(LIGHT_MARKER)

        # Delimiters that belong to the card's own shadow root: the first
        # 'shadow-root' mention after the card's open tag, and the last
        # one before its light-DOM child.
        opens = [b for b in bounds if b > card_at]
        assert opens, f"no shadow-root delimiter after the card tag:\n{html}"
        first_open = opens[0]

        assert first_open < shadow_at, (
            "the card's shadow content appears before any shadow-root "
            f"delimiter, so it reads as light DOM.  Got:\n{html}"
        )
        assert light_at > shadow_at, (
            "the card's light-DOM child must be serialised after its shadow "
            f"root, mirroring the DOM order.  Got:\n{html}"
        )
        closing = [b for b in bounds if shadow_at < b < light_at]
        assert closing, (
            "the shadow block must be *closed* before the light-DOM child "
            "is emitted, otherwise a reader cannot tell where shadow "
            f"content ends.  Got:\n{html}"
        )

    def test_hosts_are_not_duplicated(
        self, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: each element is serialised exactly once.

        The cheapest wrong implementation walks every element and appends
        its ``outerHTML`` plus every shadow root's ``innerHTML``.  That
        contains all the markers — and repeats each host once per
        ancestor, producing an unreadable dump many times the size of the
        document.  Counting open tags catches it.
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(
            _BUILD_CARD_DOM,
            {
                "shadow": SHADOW_MARKER,
                "deep": DEEP_MARKER,
                "light": LIGHT_MARKER,
                "closed": CLOSED_MARKER,
            },
        )

        html = _capture_html(page, tmp_path, monkeypatch)

        for tag in (
            "home-assistant-main",
            "ha-panel-lovelace",
            "foxess-control-card",
            "foxess-inner-card",
        ):
            opens = len(re.findall(rf"<{tag}[\s>]", html))
            assert opens == 1, (
                f"<{tag}> was serialised {opens} times; each element must "
                "appear exactly once or the artefact is a duplicated mess "
                f"rather than a readable tree.  Got:\n{html}"
            )
        assert html.count(SHADOW_MARKER) == 1, (
            "shadow content must be emitted once, not once per enclosing "
            f"root.  Got:\n{html}"
        )

    def test_closed_shadow_root_content_is_absent(
        self, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: only *open* roots are reachable.

        A ``mode: 'closed'`` root is not exposed to script; a capture
        claiming to have serialised one is reporting something it
        invented.
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(
            _BUILD_CARD_DOM,
            {
                "shadow": SHADOW_MARKER,
                "deep": DEEP_MARKER,
                "light": LIGHT_MARKER,
                "closed": CLOSED_MARKER,
            },
        )

        html = _capture_html(page, tmp_path, monkeypatch)

        assert "x-secret" in html, (
            "sanity: the closed root's *host* element is light DOM and must "
            f"still appear.  Got:\n{html}"
        )
        assert CLOSED_MARKER not in html, (
            "content of a closed shadow root cannot be read from script, so "
            f"it must not appear in the dump.  Got:\n{html}"
        )

    def test_absent_marker_stays_absent(
        self, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: the dump reflects this document only."""
        page.set_content("<html><body></body></html>")
        page.evaluate(
            _BUILD_CARD_DOM,
            {
                "shadow": SHADOW_MARKER,
                "deep": DEEP_MARKER,
                "light": LIGHT_MARKER,
                "closed": CLOSED_MARKER,
            },
        )

        html = _capture_html(page, tmp_path, monkeypatch)

        assert ABSENT_MARKER not in html, (
            f"a string absent from the document appeared in the dump:\n{html}"
        )

    def test_deep_shadow_chain_is_bounded_and_says_so(
        self, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pathologically deep chain must be capped, visibly.

        With the depth cap removed the same walk raises ``RangeError:
        Maximum call stack size exceeded`` at this depth.  Because
        ``_capture_failure`` suppresses exceptions around the write, that
        does not fail loudly — it quietly falls back to the shallow
        ``page.content()`` dump, i.e. back to the original defect.  So
        the assertions are that content from well inside the chain is
        present (which the fallback cannot supply) *and* that the cap
        announces itself, so a truncated artefact is never mistaken for a
        complete one.
        """
        page.set_content("<html><body></body></html>")
        page.evaluate(_BUILD_DEEP_CHAIN_DOM, _DEEP_CHAIN_LEVELS)

        html = _capture_html(page, tmp_path, monkeypatch)

        assert "x-level-0" in html, (
            f"the top of the chain must be serialised.  Got:\n{html[:2000]}"
        )
        assert "x-level-50" in html, (
            "the walk must descend well past the first few levels; a shallow "
            "dump (the page.content() fallback, i.e. what happens when an "
            "unbounded walk blows the JS stack) stops at level 0.  Got:\n"
            f"{html[:2000]}"
        )
        assert f"x-level-{_DEEP_CHAIN_LEVELS - 1}" not in html, (
            "a chain this deep must be cut short — an unbounded walk raises "
            "RangeError here, which _capture_failure would swallow.  Got "
            f"{len(html)} chars"
        )
        assert re.search(r"depth\s+limit", html, re.IGNORECASE), (
            "the depth cap must announce itself in the artefact, otherwise a "
            "truncated dump reads as a complete one and the reader draws the "
            f"wrong conclusion.  Got tail:\n{html[-2000:]}"
        )


class TestFailureCaptureIsRobust:
    """The capture must degrade, never vanish."""

    def test_html_falls_back_when_deep_serialisation_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the deep walk raises, the plain dump must still be written.

        Otherwise a serialiser bug removes the one artefact the previous
        implementation always produced.
        """
        monkeypatch.setattr(e2e_conftest, "_failure_capture_dir", lambda: tmp_path)
        page = MagicMock()

        def _evaluate(expr: str, *_a: object, **_k: object) -> Any:
            if "tags" in expr:  # the DOM summary
                return {"tags": [], "error_text": None}
            raise RuntimeError("deep walk exploded")

        page.evaluate.side_effect = _evaluate
        page.content.return_value = f"<html><body>{LIGHT_MARKER}</body></html>"

        e2e_conftest._capture_failure(page, "form-input: wait_for_form")

        written = sorted(tmp_path.glob("*.html"))
        assert written, (
            "a failing deep serialisation must fall back to page.content(), "
            "not silently drop the .html artefact"
        )
        assert LIGHT_MARKER in written[-1].read_text(), (
            "the fallback must contain the plain page content"
        )


class TestBrowserDiagnosticsLogIsAlwaysWritten:
    """``no .log`` must stop being an ambiguous non-signal.

    For the run-33380962649 failure the recorder *was* attached (the
    ``page`` fixture calls ``attach_page_diagnostics``), so the buffer
    was empty and the ``if diagnostics:`` gate skipped the write.  An
    absent file cannot be told apart from a harness bug that forgot to
    attach the recorder — so the file must always exist and must say
    which state it is describing.
    """

    @staticmethod
    def _page() -> Any:
        page = MagicMock()
        page.evaluate.return_value = {"tags": [], "error_text": None}
        page.content.return_value = "<html></html>"
        page.url = "http://localhost:8123/lovelace/0"
        return page

    def _capture(self, page: Any, tmp_path: Path, monkeypatch: Any) -> str:
        monkeypatch.setattr(e2e_conftest, "_failure_capture_dir", lambda: tmp_path)
        summary: str = e2e_conftest._capture_failure(page, "form-input: wait_for_form")
        logs = sorted(tmp_path.glob("*.log"))
        assert logs, (
            "no .log was written.  This is exactly what the CI artefact for "
            "run 33380962649 looked like, and it is why the browser's own "
            "account of the failure is unrecoverable.  Every capture must "
            f"write a diagnostics log.  Summary was: {summary!r}"
        )
        return logs[-1].read_text()

    def test_log_written_when_the_recorder_recorded_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attached recorder, empty buffer — the common case, and evidence.

        "The browser reported no errors" positively rules out a console
        error or a lost asset as the cause.  Today that conclusion is
        indistinguishable from a missing recorder.
        """
        page = self._page()
        _surface("attach_page_diagnostics")(page)

        text = self._capture(page, tmp_path, monkeypatch)

        assert "0" in text, (
            f"the log must state how many entries were recorded; got {text!r}"
        )
        lowered = text.lower()
        assert "no " in lowered or "empty" in lowered or "none" in lowered, (
            "the log must say in words that nothing was recorded, so a "
            f"reader is not left guessing; got {text!r}"
        )

    def test_log_distinguishes_a_missing_recorder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No recorder attached is a *harness bug* and must read differently.

        If the two states produced the same text the log would be no
        better than the absent file it replaces.
        """
        unattached = self._page()
        attached = self._page()
        _surface("attach_page_diagnostics")(attached)

        without = self._capture(unattached, tmp_path, monkeypatch)
        with_recorder = self._capture(attached, tmp_path, monkeypatch)

        assert without != with_recorder, (
            "'no recorder attached' (a harness bug) and 'recorder attached, "
            "nothing recorded' (evidence) must not produce identical logs — "
            f"both read {without!r}"
        )
        assert "attach" in without.lower() or "not attached" in without.lower(), (
            "the log must name the missing recorder so the harness bug is "
            f"actionable; got {without!r}"
        )

    def test_log_still_carries_recorded_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for the Lovelace panel-load work.

        The unconditional write must not displace the recorded entries
        themselves, which is what makes a panel-load failure diagnosable.
        """
        page = self._page()
        diag = _surface("attach_page_diagnostics")(page)
        diag.append("console[error] Error loading page lovelace TypeError: boom")

        text = self._capture(page, tmp_path, monkeypatch)

        assert "Error loading page lovelace" in text, (
            f"recorded diagnostics must still reach the artefact; got {text!r}"
        )


class TestNavigationsAreRecorded:
    """A navigation on the page under test must be stated, not inferred.

    Reproducing the run-33380962649 failure locally (once in ~1200 replays
    of the body, under deliberate CPU load) produced a capture whose only
    unexplained content was two aborted asset loads::

        requestfailed .../static/translations/en-GB-<hash>.json
            :: net::ERR_ABORTED
        requestfailed .../frontend_latest/80125.<hash>.js
            :: net::ERR_ABORTED

    ``net::ERR_ABORTED`` on two in-flight frontend assets *hints* that the
    document was replaced mid-load, but it does not say so, and the
    difference decides the diagnosis.  A navigation that lands between the
    click and the wait gives a brand-new card with no form state and
    raises no "Execution context was destroyed" — so the retry paths never
    engage and the wait polls a healthy idle card for its whole budget.
    That is exactly the failure the capture showed, and
    ``_set_form_value`` already carries a recovery branch for it ("Form was
    closed (e.g. by a navigation that completed without triggering a
    context error)"), so it is a mode this harness has met before.

    Recording navigations turns that inference into a fact for the next
    occurrence.  This is deliberately a *diagnostic* addition, not a fix:
    1600 instrumented replays showed the click always landing and always
    rendering the form, so no branch has yet been shown to need changing.
    """

    def test_navigations_reach_the_diagnostics_log(
        self, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both documents in a two-navigation page must be named."""
        _surface("attach_page_diagnostics")(page)
        page.goto("data:text/html,<p>MARKER-FIRST-DOC</p>")
        page.goto("data:text/html,<p>MARKER-SECOND-DOC</p>")

        monkeypatch.setattr(e2e_conftest, "_failure_capture_dir", lambda: tmp_path)
        e2e_conftest._capture_failure(page, "form-input: wait_for_form")
        logs = sorted(tmp_path.glob("*.log"))
        assert logs, "no diagnostics log written"
        text = logs[-1].read_text()

        assert "MARKER-FIRST-DOC" in text and "MARKER-SECOND-DOC" in text, (
            "the log must name every document this page navigated to — a "
            "navigation between the click and the wait is the difference "
            "between a lost click and a replaced card, and the capture "
            f"currently reports neither.  Got:\n{text}"
        )
        assert "navigat" in text.lower(), (
            "the entries must say in words that they are navigations, not "
            f"leave a reader to recognise a bare URL.  Got:\n{text}"
        )

    def test_a_page_that_never_navigated_claims_no_navigation(
        self, page: Page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: no navigation, no navigation entry.

        Without this the requirement could be met by emitting a navigation
        line unconditionally, which would assert the very thing the next
        diagnosis needs to rule out.
        """
        _surface("attach_page_diagnostics")(page)

        monkeypatch.setattr(e2e_conftest, "_failure_capture_dir", lambda: tmp_path)
        e2e_conftest._capture_failure(page, "form-input: wait_for_form")
        logs = sorted(tmp_path.glob("*.log"))
        assert logs, "no diagnostics log written"
        text = logs[-1].read_text()

        navigations = [
            line for line in text.splitlines() if line.lower().startswith("navigat")
        ]
        assert not navigations, (
            "a page that never navigated must not report a navigation; "
            f"got {navigations}"
        )


class TestWaitForFormFailureIsSelfExplanatory:
    """The raised message must name the capture it just wrote.

    The CI log for run 33380962649 contained a bare
    ``Page.wait_for_function: Timeout 10000ms exceeded`` — no capture
    number, no file names, no element list.  ``_wait_for_form`` calls
    ``_capture_failure`` and *discards* its return value, so the summary
    that ``wait_for_condition`` puts in its own message is thrown away
    here.  A reader of the CI log alone cannot even tell that artefacts
    exist.
    """

    @staticmethod
    def _instance() -> Any:
        from tests.e2e.test_ui import TestFormInputPersistence  # noqa: PLC0415

        obj = TestFormInputPersistence()
        obj._current_form_action = "charge"
        return obj

    def test_timeout_message_names_the_capture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from playwright._impl._errors import TimeoutError as PwTimeout  # noqa: PLC0415

        monkeypatch.setattr(e2e_conftest, "_failure_capture_dir", lambda: tmp_path)
        page = MagicMock()
        page.evaluate.return_value = {
            "tags": ["foxess-control-card"],
            "error_text": None,
        }
        page.content.return_value = "<html></html>"
        page.wait_for_function.side_effect = PwTimeout(
            "Page.wait_for_function: Timeout 10000ms exceeded."
        )

        with pytest.raises(PwTimeout) as caught:
            self._instance()._wait_for_form(page)

        message = str(caught.value)
        assert "capture" in message, (
            "the raised error must name the capture written alongside it, so "
            "the CI log alone tells a reader that artefacts exist and which "
            f"ones.  Got: {message!r}"
        )
        assert ".html" in message and ".png" in message, (
            f"the message must name the artefact files; got {message!r}"
        )
