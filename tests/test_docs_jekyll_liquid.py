"""Jekyll/Liquid compatibility tests for the ``docs/`` tree.

GitHub Pages is enabled on this repo (legacy mode, branch ``main``,
path ``/docs``). On every push to ``main``, the
``pages-build-deployment`` workflow runs ``github-pages`` (Jekyll
3.10.0 + Liquid 4.0.4) against ``docs/`` and publishes the result.

Jekyll pre-processes every ``.md`` file through Liquid *before* the
markdown renderer. Liquid does NOT understand Jinja2 tags such as
``{% set %}``, ``{% for %}``, ``{% if %}`` — these are legitimate
Home Assistant template syntax that users copy/paste into Lovelace
markdown cards, but they blow up the Pages build with::

    Liquid Exception: Liquid syntax error (line 23): Unknown tag 'set' ...

Liquid *also* parses ``{{ ... }}`` as variable interpolation, and
raises a ``Variable '...' was not properly terminated with regexp:
/\\}\\}/`` error when the token it finds inside the braces isn't a
valid Liquid expression — which is exactly what happens when an
author embeds a Python f-string (e.g. a triple-quoted f-string
whose body contains ``{{ ... }}`` as a brace escape) or a
JavaScript object literal in a code block. GH CI run 25199109037
(2026-04-30) caught this on
``docs/superpowers/plans/2026-04-20-overview-card-customisation.md``
line 543::

    Liquid Exception: Liquid syntax error (line 543): Variable
    '{{ {_JS_FIND_OVERVIEW_CARD}' was not properly terminated with
    regexp: /\\}\\}/ ...

Two escape hatches were historically considered:

1. Wrap the Jinja region(s) in ``{% raw %}...{% endraw %}`` — Liquid
   emits the content literally and skips tag parsing. **This is
   the only mechanism that works on GitHub Pages today.**
2. Add ``render_with_liquid: false`` to the YAML frontmatter of the
   file. This is a **Jekyll 4.0+** feature
   (https://jekyllrb.com/news/2020/03/31/announcing-jekyll-4/).
   The ``github-pages`` gem (v232) is pinned to Jekyll 3.10.0, so
   this flag is silently ignored in production. GH CI run
   25195304627 (2026-04-30) demonstrated this: ``06-tests.md`` had
   ``render_with_liquid: false`` in its frontmatter AND still blew
   up with ``Unknown tag 'set'`` at line 432.

This test therefore enforces a stricter rule than the first
iteration: every Jinja-style ``{% ... %}`` tag AND every
``{{ ... }}`` variable interpolation in any ``docs/**/*.md`` file
must be inside a ``{% raw %}...{% endraw %}`` envelope —
*regardless* of whether the file claims ``render_with_liquid:
false`` in its frontmatter, and *regardless* of whether the token
sits inside a markdown backtick/code-fence (Liquid runs before the
markdown pass, so backticks and fences mean nothing to it).

Without this guard, a doc author adding HA template examples to
the docs tree will silently break the Pages deployment on the next
push to ``main``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "docs"

# Liquid 4.0.4 (the version github-pages bundles) understands this set
# of block tags. Any other ``{% tagname ... %}`` will raise
# ``Liquid::SyntaxError: Unknown tag``. Home Assistant's Jinja2
# templates commonly use ``set``, ``for``, ``if``, ``elif``, ``else``,
# ``endif``, ``endfor``, ``macro``, ``endmacro`` — all of which
# collide with or are unknown to Liquid.
#
# We key off the opening ``{%`` rather than trying to enumerate every
# incompatible tag: anything at all between ``{%`` and ``%}`` that is
# not known-safe-for-Liquid is a risk. The safest enforcement is
# "every ``{% ... %}`` in the document body must be inside a
# ``{% raw %}...{% endraw %}`` envelope" — ``render_with_liquid:
# false`` in frontmatter is NOT a valid escape because it's a
# Jekyll-4.0+ feature and github-pages is still pinned to Jekyll
# 3.10.
#
# Liquid tag tokens that ARE legal at document level (outside raw
# blocks) and therefore should NOT trigger the guard:
_LIQUID_SAFE_TAGS = frozenset(
    {
        "raw",
        "endraw",
        "comment",
        "endcomment",
        "include",
    }
)

# Matches ``{% <word> ...`` — captures the first token after ``{%``
# so we can tell whether it's a Liquid-safe tag or not.
_TAG_OPEN_RE = re.compile(r"\{%-?\s*(\w+)")
# Matches ``{{`` — Liquid variable interpolation. ANY occurrence
# outside a ``{% raw %}`` envelope will be parsed by Liquid as a
# variable and will raise
# ``Variable '...' was not properly terminated with regexp: /\}\}/``
# if the inner tokens aren't valid Liquid. This is exactly the
# failure mode GH CI run 25199109037 hit on the Python f-string
# ``f"""() => {{ ... }}"""`` in the overview-card plan.
_VAR_OPEN_RE = re.compile(r"\{\{")
# Matches ``{% raw %}`` and ``{% endraw %}`` (with optional whitespace
# trimmers ``-``).
_RAW_OPEN_RE = re.compile(r"\{%-?\s*raw\s*-?%\}")
_RAW_CLOSE_RE = re.compile(r"\{%-?\s*endraw\s*-?%\}")

# Frontmatter fence: the ``---``-fenced YAML block at the top of a
# Jekyll document. We skip *that block* when scanning for Liquid
# tokens (tokens quoted inside frontmatter comments such as
# ``# Disable Liquid on {% set %}`` are never passed to the Liquid
# parser — Jekyll strips the frontmatter before handing the body to
# Liquid). Everything *after* the second ``---`` is fair game.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _docs_markdown_files() -> list[Path]:
    """Return all ``.md`` files under ``docs/`` (the Jekyll source)."""
    # Exclude Jekyll's build output if a local build has produced one.
    return sorted(p for p in DOCS_ROOT.rglob("*.md") if "_site" not in p.parts)


def _body_and_frontmatter_lines(text: str) -> tuple[str, int]:
    """Return ``(body_text, frontmatter_line_count)``.

    Jekyll treats the ``---``-fenced block at the very top as YAML
    metadata and does not pass it to Liquid. Anything in the body
    *is* passed to Liquid — including content inside backticks and
    fenced code blocks (Liquid runs before the markdown pass, so it
    has no concept of either).

    Liquid numbers lines starting at 1 within the body (i.e. after
    the frontmatter strip). GH CI run 25195304627 confirms this:
    the ``{% set %}`` on source line 443 of ``06-tests.md`` was
    reported by Liquid as line 432 — which is exactly 443 minus
    the 11-line frontmatter block. Returning the frontmatter line
    count lets callers report errors in either coordinate system.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text, 0
    frontmatter_text = text[: match.end()]
    # ``---\n...\n---\n`` — count newlines to get the line count.
    frontmatter_lines = frontmatter_text.count("\n")
    body = text[match.end() :]
    return body, frontmatter_lines


def _unescaped_jinja_tags(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, token)`` pairs for every Liquid-
    incompatible token that sits outside a
    ``{% raw %}...{% endraw %}`` envelope.

    Two classes of offender are reported:

    * ``{% <tag> ... %}`` where ``<tag>`` is not in
      ``_LIQUID_SAFE_TAGS`` — reported as the tag token
      (``"set"``, ``"for"``, etc.).
    * Any ``{{`` — Liquid parses it as variable interpolation and
      blows up if the inner tokens aren't a valid Liquid
      expression. Reported as the literal ``"{{"``.

    Lines are 1-based to match Liquid's error reporting.
    """
    offenders: list[tuple[int, str]] = []
    in_raw = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Walk the line character-by-character so that ``{% raw %}``
        # and the offending token on the same line are handled in
        # order.  (In practice, our docs put each token on its own
        # line, but a robust scanner handles both.)
        idx = 0
        while idx < len(line):
            remainder = line[idx:]
            if in_raw:
                close = _RAW_CLOSE_RE.search(remainder)
                if close is None:
                    break
                in_raw = False
                idx += close.end()
                continue
            # Not in raw — look for the next ``{%`` or ``{{``,
            # whichever comes first.
            tag_open = _TAG_OPEN_RE.search(remainder)
            var_open = _VAR_OPEN_RE.search(remainder)
            if tag_open is None and var_open is None:
                break
            # Pick whichever is earlier in the remainder.
            if tag_open is not None and (
                var_open is None or tag_open.start() < var_open.start()
            ):
                # ``{% ... %}`` branch.
                if _RAW_OPEN_RE.match(remainder, tag_open.start()):
                    in_raw = True
                    idx += tag_open.end()
                    continue
                tag = tag_open.group(1)
                if tag not in _LIQUID_SAFE_TAGS:
                    offenders.append((lineno, tag))
                idx += tag_open.end()
            else:
                # ``{{`` variable-interpolation branch. Any
                # occurrence outside a raw block is a risk —
                # Liquid will try to parse the inside as a
                # variable expression.
                assert var_open is not None  # for type-checkers
                offenders.append((lineno, "{{"))
                idx += var_open.end()
    return offenders


@pytest.mark.parametrize(
    "md_file",
    _docs_markdown_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_docs_markdown_is_jekyll_liquid_safe(md_file: Path) -> None:
    """Every ``docs/**/*.md`` file must survive the Jekyll Liquid pass.

    A file fails this test when it contains a Jinja2-style tag such
    as ``{% set %}`` or ``{% for %}`` in the document body that sits
    outside a ``{% raw %}...{% endraw %}`` envelope.

    Note: ``render_with_liquid: false`` in frontmatter is a
    Jekyll-4.0+ feature and is silently ignored by the ``github-
    pages`` gem (Jekyll 3.10). It is therefore *not* accepted as an
    opt-out by this test. Backticks and fenced code blocks are also
    not accepted — Liquid runs before the markdown pass and cannot
    see them. The only reliable escape is ``{% raw %}...{% endraw %}``.

    GH CI run 25195304627 (2026-04-30) demonstrated both failure
    modes simultaneously: ``docs/knowledge/06-tests.md`` had
    ``render_with_liquid: false`` set AND its offending ``{% set %}``
    token sat inside a markdown backtick — yet Jekyll still blew up
    with ``Unknown tag 'set'`` at line 432.

    The GitHub Pages build (which publishes ``docs/`` on every push
    to ``main``) will fail with a ``Liquid syntax error`` exactly
    when this guard fires — catch it in CI, not after merge.
    """
    text = md_file.read_text(encoding="utf-8")
    body, frontmatter_lines = _body_and_frontmatter_lines(text)

    offenders = _unescaped_jinja_tags(body)
    rel = md_file.relative_to(REPO_ROOT)

    def _render(ln: int, token: str) -> str:
        # Report both Liquid's body-relative line number (what CI
        # logs) and the absolute source line (what editors jump to).
        absolute = ln + frontmatter_lines
        if token == "{{":
            display = "{{ ... }} (variable interpolation)"
        else:
            display = f"{{% {token} ... %}}"
        return f"  body line {ln} (source line {absolute}): {display}"

    assert not offenders, (
        f"{rel} contains Liquid-incompatible tokens that will break the "
        f"Jekyll/GitHub-Pages build:\n"
        + "\n".join(_render(ln, token) for ln, token in offenders)
        + "\n\nFix options:\n"
        "  (a) wrap the Jinja region(s) in {% raw %}...{% endraw %}\n"
        "      (this is the ONLY mechanism that works on the\n"
        "      github-pages gem — Jekyll 3.10 — today)\n"
        "  (b) rephrase to avoid the Jinja-style tokens entirely\n"
        "  (c) delete the file from docs/ if it should not be published\n"
        "\n"
        "NOTE: ``render_with_liquid: false`` in frontmatter is a\n"
        "Jekyll-4.0+ feature and is silently ignored by github-pages\n"
        "(Jekyll 3.10). It is NOT a valid escape here."
    )
