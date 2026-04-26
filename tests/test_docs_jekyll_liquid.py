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

Two escape hatches exist:

1. Wrap the Jinja region(s) in ``{% raw %}...{% endraw %}`` — Liquid
   emits the content literally and skips tag parsing.
2. Add ``render_with_liquid: false`` to the YAML frontmatter of the
   file — Jekyll skips Liquid entirely for that file (but still
   renders markdown).

This test enforces that any ``docs/**/*.md`` file containing a
Jinja-style ``{% ... %}`` tag is either wrapped in ``{% raw %}`` or
has ``render_with_liquid: false`` in its frontmatter. Without this
guard, a doc author adding HA template examples to the docs tree
will silently break the Pages deployment on the next push to
``main``.
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
# "every ``{% ... %}`` must be inside a ``{% raw %}...{% endraw %}``
# envelope, or the file must have ``render_with_liquid: false``".
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
# Matches ``{% raw %}`` and ``{% endraw %}`` (with optional whitespace
# trimmers ``-``).
_RAW_OPEN_RE = re.compile(r"\{%-?\s*raw\s*-?%\}")
_RAW_CLOSE_RE = re.compile(r"\{%-?\s*endraw\s*-?%\}")

# Frontmatter opt-out: ``render_with_liquid: false`` anywhere in the
# leading ``---``-fenced YAML block.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _docs_markdown_files() -> list[Path]:
    """Return all ``.md`` files under ``docs/`` (the Jekyll source)."""
    # Exclude Jekyll's build output if a local build has produced one.
    return sorted(p for p in DOCS_ROOT.rglob("*.md") if "_site" not in p.parts)


def _frontmatter_opts_out(text: str) -> bool:
    """True if the file frontmatter sets ``render_with_liquid: false``.

    Jekyll treats any ``---``-fenced YAML block at the start of the
    file as frontmatter. ``render_with_liquid: false`` inside it tells
    Jekyll to skip the Liquid pass entirely for this file — exactly
    what we want for files full of HA Jinja templates.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return False
    fm = match.group(1)
    # Loose regex match — we're not a YAML parser, just checking for
    # the opt-out flag. A stricter test would import yaml, but that
    # pulls in a dep for something a one-line regex handles cleanly.
    return bool(re.search(r"^\s*render_with_liquid\s*:\s*false\s*$", fm, re.MULTILINE))


def _unescaped_jinja_tags(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, tag_token)`` pairs for every Liquid-
    incompatible ``{% ... %}`` tag that sits outside a
    ``{% raw %}...{% endraw %}`` envelope.

    Lines are 1-based to match Liquid's error reporting.
    """
    offenders: list[tuple[int, str]] = []
    in_raw = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Walk the line character-by-character so that ``{% raw %}``
        # and the offending tag on the same line are handled in
        # order.  (In practice, our docs put each tag on its own
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
            # Not in raw — look for the next ``{%``.
            open_ = _TAG_OPEN_RE.search(remainder)
            if open_ is None:
                break
            # Is this ``{% raw %}``?
            if _RAW_OPEN_RE.match(remainder, open_.start()):
                in_raw = True
                idx += open_.end()
                continue
            tag = open_.group(1)
            if tag not in _LIQUID_SAFE_TAGS:
                offenders.append((lineno, tag))
            idx += open_.end()
    return offenders


@pytest.mark.parametrize(
    "md_file",
    _docs_markdown_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_docs_markdown_is_jekyll_liquid_safe(md_file: Path) -> None:
    """Every ``docs/**/*.md`` file must survive the Jekyll Liquid pass.

    A file fails this test when it contains a Jinja2-style tag such as
    ``{% set %}`` or ``{% for %}`` that sits outside a
    ``{% raw %}...{% endraw %}`` envelope AND does not have
    ``render_with_liquid: false`` in its frontmatter.

    The GitHub Pages build (which publishes ``docs/`` on every push
    to ``main``) will fail with a ``Liquid syntax error`` exactly
    when this guard fires — catch it in CI, not after merge.
    """
    text = md_file.read_text(encoding="utf-8")

    if _frontmatter_opts_out(text):
        # File has opted out of Liquid processing — no need to check.
        return

    offenders = _unescaped_jinja_tags(text)
    rel = md_file.relative_to(REPO_ROOT)
    assert not offenders, (
        f"{rel} contains Liquid-incompatible tags that will break the "
        f"Jekyll/GitHub-Pages build:\n"
        + "\n".join(f"  line {ln}: {{% {tag} ... %}}" for ln, tag in offenders)
        + "\n\nFix options:\n"
        "  (a) wrap the Jinja region(s) in {% raw %}...{% endraw %}\n"
        "  (b) add ``render_with_liquid: false`` to the frontmatter\n"
        "  (c) delete the file from docs/ if it should not be published"
    )
