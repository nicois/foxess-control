"""Unit test: every i18n key defined in the English table exists
in every other locale table.

Prevents translation drift — the card falls back to the key name
if a locale is missing an entry, which looks broken to the user.
A beta.7-era bug (log-sensor entity ID) was caused by this class
of gap; a unit test rather than a reviewer's eye is the right
defence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CARD_FILES = [
    "custom_components/foxess_control/www/foxess-control-card.js",
    "custom_components/foxess_control/www/foxess-taper-card.js",
]


# Matches a locale header like `en: {` or `"zh-hans": {` on its own
# line starting with two spaces.  Greedy on whitespace so we also
# match potential edge cases.
_LOCALE_HEADER_RE = re.compile(
    r'^  (?:([a-z]{2,3}(?:-[a-z]+)?)|"([a-z]{2,3}(?:-[a-z]+)?)"):\s*\{',
    re.M,
)

# Matches a key line inside a locale body: `  foo: "bar",` (indented,
# 4-space).  Quoted keys aren't used in the card tables so we don't
# match `"foo"` — if that ever changes, extend the pattern.
_KEY_RE = re.compile(r"^\s{4}([a-z][A-Za-z0-9_]*)\s*:", re.M)


def _parse_i18n_tables(js: str) -> dict[str, set[str]]:
    """Extract ``{locale: set(key)}`` from the ``TRANSLATIONS = {...}``
    block of a card JS file.

    Walks the locale headers in source order, then slices each block
    from its opening brace to the next header (or end of the
    ``TRANSLATIONS`` object).
    """
    trans_idx = js.find("TRANSLATIONS = {")
    if trans_idx < 0:
        return {}
    region = js[trans_idx:]
    # Find end of the TRANSLATIONS object: first `^};` at column 0.
    end_match = re.search(r"^\};", region, re.M)
    region = region[: end_match.start()] if end_match else region

    headers: list[tuple[str, int]] = []
    for m in _LOCALE_HEADER_RE.finditer(region):
        name = m.group(1) or m.group(2)
        assert name is not None
        headers.append((name, m.end()))

    tables: dict[str, set[str]] = {}
    for idx, (name, start) in enumerate(headers):
        end = headers[idx + 1][1] if idx + 1 < len(headers) else len(region)
        block = region[start:end]
        # The block contains `key: "value",` entries; extract keys.
        tables[name] = set(_KEY_RE.findall(block))
    return tables


@pytest.mark.parametrize("card_file", _CARD_FILES)
def test_all_locales_cover_english_keys(card_file: str) -> None:
    """Every non-English locale table has every key from the English
    table — otherwise the card's ``_t()`` falls back to the key name
    and the user sees broken rendering in their language.
    """
    path = Path(card_file)
    assert path.is_file(), f"card file missing: {card_file}"
    tables = _parse_i18n_tables(path.read_text())
    assert "en" in tables, f"no English table parsed from {card_file}"
    english_keys = tables["en"]
    assert english_keys, f"English table parsed as empty for {card_file}"
    drift: dict[str, set[str]] = {}
    for locale, keys in tables.items():
        if locale == "en":
            continue
        missing = english_keys - keys
        if missing:
            drift[locale] = missing
    assert not drift, (
        f"{card_file} locales are missing keys present in English:\n"
        + "\n".join(f"  {loc}: {sorted(ks)}" for loc, ks in drift.items())
    )


def test_parser_finds_expected_locales() -> None:
    """Sanity: the parser picks up all 10 locales in the control card.

    Guards the parser itself; if a new locale is added (or the
    existing ones are restructured), this test flags the parser
    change rather than silently dropping locales from coverage.
    """
    js = Path(_CARD_FILES[0]).read_text()
    tables = _parse_i18n_tables(js)
    expected = {"en", "de", "fr", "nl", "es", "it", "pl", "pt", "zh-hans", "ja"}
    assert set(tables) == expected, (
        f"control-card locale set changed: got {sorted(tables)}, "
        f"expected {sorted(expected)}"
    )


def test_new_keys_present_in_every_locale() -> None:
    """Regression guard for the 2026-04-25 UX #4/#6/#8 additions.

    Every locale must carry the four new keys; otherwise users
    running the card in a non-English language will see the raw
    key name on their dashboard.
    """
    js = Path(_CARD_FILES[0]).read_text()
    tables = _parse_i18n_tables(js)
    new_keys = {
        "deferred_reason",
        "safety_floor",
        "floor_clamping_tooltip",
        "clamp_active_tooltip",
    }
    missing_by_locale: dict[str, set[str]] = {}
    for locale, keys in tables.items():
        missing = new_keys - keys
        if missing:
            missing_by_locale[locale] = missing
    assert not missing_by_locale, "UX #4/#6/#8 keys missing from locales: " + ", ".join(
        f"{loc}({sorted(ks)})" for loc, ks in missing_by_locale.items()
    )
