"""Load the shipped Lovelace card JS in a real browser, the way HA does.

Why this exists
---------------
The cards are registered with Home Assistant as ``res_type: "module"``
(see ``__init__.py::_register_card_frontend``) and served from
``/foxess_control/<name>.js?v=<manifest version>``.  Loading them as
ES *modules over HTTP* is therefore the production configuration, and
it is the only configuration in which one card file can ``import``
a shared sibling module — which ``foxess-control-card.js`` and
``foxess-overview-card.js`` both do for the staleness logic they share
(``foxess-stale.js``).

The older technique — ``page.add_script_tag(path=...)`` — injects the
file as a *classic* script, where an ``import`` statement or top-level
``await`` is a syntax error.  Tests written that way would fail for a
reason that has nothing to do with the card's behaviour, and would
also have been silently testing a different module graph from the one
users run.

So the harness serves the real ``www/`` directory over a routed fake
origin and loads the card with ``type="module"``.  No port is bound and
no server process is started — ``page.route`` fulfils the requests from
disk — so nothing here is shared between concurrent pytest runs (C-043).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import Page, Route

WWW_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "foxess_control"
    / "www"
)

# Any origin works; a `.test` TLD is reserved by RFC 6761 so it can never
# escape to a real host even if routing were somehow bypassed.
ORIGIN = "http://foxess-cards.test"

_BLANK_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'></head>"
    "<body><div id='root'></div></body></html>"
)


def serve_cards(page: Page) -> list[str]:
    """Serve ``www/`` over :data:`ORIGIN` and open a blank page there.

    Returns a list that accumulates uncaught page errors.  Assert on it,
    or pass it to :func:`load_card`, so a module that fails to parse or
    whose sibling import 404s reports *that* rather than an opaque
    "custom element never defined" timeout.
    """

    def _handler(route: Route) -> None:
        # Strip the cache-busting query the cards propagate to their
        # sibling imports (`?v=1.0.22`) before hitting the filesystem.
        rel = urlsplit(route.request.url).path.lstrip("/")
        if rel in ("", "index.html"):
            route.fulfill(status=200, content_type="text/html", body=_BLANK_PAGE)
            return
        target = WWW_DIR / rel
        # Refuse anything that is not a plain file directly in www/, so a
        # traversal in a test URL cannot read the rest of the checkout.
        if target.is_file() and target.parent == WWW_DIR:
            route.fulfill(
                status=200,
                content_type="text/javascript; charset=utf-8",
                body=target.read_text(encoding="utf-8"),
            )
        else:
            route.fulfill(status=404, content_type="text/plain", body="not found")

    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.route(f"{ORIGIN}/**", _handler)
    page.goto(f"{ORIGIN}/index.html", wait_until="load")
    return errors


def load_card(
    page: Page,
    filename: str,
    tag: str,
    *,
    errors: list[str] | None = None,
    version: str = "1.0.0-test",
    timeout: int = 15000,
) -> None:
    """Load ``www/<filename>`` as a module and wait for ``tag`` to define.

    ``version`` is appended as a ``?v=`` query so the harness exercises
    the same URL shape HA registers, including the query the card
    propagates to its sibling imports.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        page.add_script_tag(url=f"/{filename}?v={version}", type="module")
        page.wait_for_function(
            "(t) => !!customElements.get(t)", arg=tag, timeout=timeout
        )
    except PlaywrightError as exc:  # pragma: no cover - diagnostic path
        detail = "; ".join(errors or []) or "no page errors captured"
        msg = f"{filename} did not define <{tag}>: {detail}"
        raise AssertionError(msg) from exc
