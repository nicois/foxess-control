"""Regression tests: HA runtime-loaded translations must cover every
``translation_key`` the integration passes to ``async_create_issue``
(and the equivalent ``HomeAssistantError`` exception keys).

Live failure (2026-05-18, v1.0.17-beta.2): the HA Repairs UI displayed
the raw key ``foxess_control: charge_target_unreachable`` instead of
the friendly title and interpolated description.

Root cause: HA loads runtime UI translations from
``<integration>/translations/<lang>.json`` — NOT from
``strings.json``.  The previous fix (e7a6a97) added the
``charge_target_unreachable`` block to ``strings.json`` only.  The
runtime ``translations/en.json`` had no ``issues`` key at all, so HA's
translation cache had no entry to render and fell back to the bare key.

Reference: ``homeassistant.helpers.translation._async_get_component_strings``
loads ``integration.file_path / "translations" / file_name`` (e.g.
``en.json``) — confirmed by reading
``site-packages/homeassistant/helpers/translation.py`` line 102-110.

These tests guard the runtime translation surface — both the
existence of the entries that today's listener code needs, AND the
sync between every ``translation_key=`` call site and the runtime
file.  A fix that only updates ``strings.json`` will fail these
tests.

Constraint references: C-020 (UI must self-explain — no log inspection
required), C-022 (unreachable target surfaced to user), C-026
(persistent errors via sensor/Repair state, not just logs).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_INTEGRATION = _ROOT / "custom_components" / "foxess_control"
_RUNTIME_EN = _INTEGRATION / "translations" / "en.json"
_STRINGS = _INTEGRATION / "strings.json"

# Source trees that may raise issues / exceptions with translation_key.
_SOURCE_DIRS = [
    _INTEGRATION,
    _ROOT / "smart_battery",
]

# Pattern: ``translation_key="foo"`` as a kwarg (not ``_attr_translation_key``
# attribute assignment, which entities use for their entity-name
# lookup under ``entity.<platform>.<key>`` — that's a different
# category and is covered by HA's built-in entity translation flow).
#
# We anchor on a non-word char before ``translation_key`` to exclude
# ``self._attr_translation_key`` and ``foo.translation_key``.
_KWARG_KEY_RE = re.compile(r'(?<![\w.])translation_key\s*=\s*[\'"]([A-Za-z0-9_]+)[\'"]')

# Files where translation_key= appears as part of unrelated symbol
# names (e.g. dataclass field defaults, comments, the helper helpers
# that re-pass keys).  Empty by default — extend if you find one.
_EXCLUDE_FILES: set[str] = set()


def _collect_translation_keys() -> dict[str, set[str]]:
    """Walk source trees and return ``{file_path: {key, ...}}`` for every
    ``translation_key=...`` kwarg literal (``async_create_issue``,
    ``HomeAssistantError``, etc.) found.

    These are the keys HA will look up at runtime when the Repair issue
    or exception fires — and therefore the keys that MUST exist in
    ``translations/en.json``.

    Excludes ``self._attr_translation_key = "..."`` style entity
    attribute assignments (those resolve under ``entity.<platform>.<key>``
    and are covered by the existing entity translation entries).
    """
    found: dict[str, set[str]] = {}
    for src in _SOURCE_DIRS:
        for path in src.rglob("*.py"):
            # Skip vendored copies and tests / __pycache__
            parts = set(path.parts)
            if "__pycache__" in parts or "tests" in parts:
                continue
            # Skip the vendored smart_battery copy under
            # custom_components/foxess_control/smart_battery/ — the
            # canonical root copy is already covered.
            if (
                "custom_components" in parts
                and "foxess_control" in parts
                and "smart_battery" in parts
            ):
                continue
            if str(path) in _EXCLUDE_FILES:
                continue
            text = path.read_text()
            keys = set(_KWARG_KEY_RE.findall(text))
            if keys:
                found[str(path.relative_to(_ROOT))] = keys
    return found


def _all_callsites_keys() -> set[str]:
    """Flat set of every ``translation_key=`` literal across the codebase."""
    out: set[str] = set()
    for keys in _collect_translation_keys().values():
        out.update(keys)
    return out


def _runtime_translations() -> dict[str, Any]:
    """Load the runtime English translations file used by HA at runtime."""
    data: dict[str, Any] = json.loads(_RUNTIME_EN.read_text())
    return data


def _flatten_section(section: dict[str, Any], base: str = "") -> dict[str, str]:
    """Flatten the nested issues/exceptions block to dotted paths."""
    out: dict[str, str] = {}
    for key, value in section.items():
        path = f"{base}.{key}" if base else key
        if isinstance(value, dict):
            out.update(_flatten_section(value, path))
        else:
            out[path] = value
    return out


# ---------------------------------------------------------------------------
# The core regression test for the live bug.
# ---------------------------------------------------------------------------


class TestRuntimeTranslationsCoverIssueKeys:
    """C-020 / C-022 / C-026: runtime ``translations/en.json`` must
    contain a renderable ``issues.<key>.{title,description}`` for every
    ``translation_key`` that the integration passes to
    ``async_create_issue``.  Otherwise HA renders the bare key.
    """

    def test_charge_target_unreachable_has_runtime_translation(self) -> None:
        """The exact failure from the live 2026-05-18 report.

        The user's HA showed ``foxess_control: charge_target_unreachable``
        instead of "Smart charge can't reach target SoC".  HA looks up
        ``component.foxess_control.issues.charge_target_unreachable.title``
        in the runtime cache, which is populated from
        ``translations/<lang>.json`` — not ``strings.json``.

        With the entry missing from the runtime file, the fallback is
        the raw translation key.
        """
        en = _runtime_translations()
        issues = en.get("issues", {})
        assert "charge_target_unreachable" in issues, (
            "C-020 violation: translations/en.json has no "
            "issues.charge_target_unreachable entry — HA will render "
            "the bare translation key in the Repairs UI.  The "
            "previous fix added the entry to strings.json, but HA "
            "loads runtime translations from translations/<lang>.json "
            "(see homeassistant.helpers.translation, "
            "_async_get_component_strings)."
        )
        entry = issues["charge_target_unreachable"]
        assert isinstance(entry.get("title"), str) and entry["title"].strip(), (
            "issues.charge_target_unreachable.title must be a non-empty string"
        )
        assert (
            isinstance(entry.get("description"), str) and entry["description"].strip()
        ), "issues.charge_target_unreachable.description must be a non-empty string"

    def test_charge_target_unreachable_description_uses_all_placeholders(self) -> None:
        """The listener passes four placeholders; the rendered
        description must reference each, otherwise the user sees a
        message that doesn't name the actual gap.

        Placeholders set by ``smart_battery.listeners._create_unreachable_issue``:
        ``current_soc``, ``target_soc``, ``remaining_hours``, ``max_power_w``.
        """
        en = _runtime_translations()
        desc = (
            en.get("issues", {})
            .get("charge_target_unreachable", {})
            .get("description", "")
        )
        required = ("current_soc", "target_soc", "remaining_hours", "max_power_w")
        missing = [ph for ph in required if "{" + ph + "}" not in desc]
        assert not missing, (
            f"issues.charge_target_unreachable.description must "
            f"interpolate every placeholder the listener passes — "
            f"missing references: {missing}.  Description was: {desc!r}"
        )

    def test_every_callsite_key_has_a_runtime_translation_entry(self) -> None:
        """Sync test: every ``translation_key="foo"`` literal in the
        source tree must resolve to either ``issues.foo`` or
        ``exceptions.foo`` in the runtime translations file.

        This catches future drift — adding a new ``translation_key=``
        without adding the entry to ``translations/en.json`` would
        silently render the bare key on the user's UI.

        Neighbourhood coverage: when ``charge_target_unreachable``
        broke, three other live keys (``unmanaged_work_mode``,
        ``session_aborted``, ``sensor_write_failed``) had the same
        flaw.  This test catches the whole class.
        """
        callsite_keys = _all_callsites_keys()
        assert callsite_keys, (
            "Sanity: at least one translation_key= literal must be "
            "found in the source tree"
        )

        en = _runtime_translations()
        runtime_issue_keys = set(en.get("issues", {}).keys())
        runtime_exception_keys = set(en.get("exceptions", {}).keys())
        runtime_keys = runtime_issue_keys | runtime_exception_keys

        missing = sorted(callsite_keys - runtime_keys)
        assert not missing, (
            "C-020 violation: translation_key callsites with no "
            "matching entry in translations/en.json (issues/ or "
            "exceptions/).  These render as the bare key in the HA "
            "UI:\n"
            + "\n".join(f"  - {k}" for k in missing)
            + "\n\nAdd entries to custom_components/foxess_control/"
            "translations/en.json (and ideally to strings.json for "
            "Lokalise sync)."
        )


class TestStringsAndRuntimeTranslationsAgree:
    """Hardening: ``strings.json`` (developer source) and
    ``translations/en.json`` (runtime) must agree on the issue/exception
    keys.  Drift between them is what caused the live bug — the
    developer added to one and not the other.
    """

    def test_runtime_issues_match_strings_issues(self) -> None:
        """Every ``issues.*`` key in ``strings.json`` must appear in
        ``translations/en.json`` with title and description.
        """
        strings = json.loads(_STRINGS.read_text())
        runtime = _runtime_translations()
        strings_keys = set(strings.get("issues", {}).keys())
        runtime_keys = set(runtime.get("issues", {}).keys())
        missing = sorted(strings_keys - runtime_keys)
        assert not missing, (
            f"strings.json declares issues.{missing} but "
            f"translations/en.json does not.  HA loads the runtime "
            f"file — the entries in strings.json alone do not reach "
            f"the user."
        )

    def test_runtime_exceptions_match_strings_exceptions(self) -> None:
        """Same contract for ``exceptions.*`` — HomeAssistantError
        instances raised with ``translation_key`` look up under
        ``component.<domain>.exceptions.<key>.message``.
        """
        strings = json.loads(_STRINGS.read_text())
        runtime = _runtime_translations()
        strings_keys = set(strings.get("exceptions", {}).keys())
        runtime_keys = set(runtime.get("exceptions", {}).keys())
        missing = sorted(strings_keys - runtime_keys)
        assert not missing, (
            f"strings.json declares exceptions.{missing} but "
            f"translations/en.json does not.  HA-rendered error "
            f"messages will fall back to bare keys."
        )


@pytest.mark.parametrize(
    "key",
    [
        "charge_target_unreachable",
        "unmanaged_work_mode",
        "session_aborted",
        "sensor_write_failed",
    ],
)
def test_known_issue_keys_render_non_empty_title(key: str) -> None:
    """Neighbourhood test: every issue key the integration raises today
    must have a non-empty rendered title.  Catches the class of bug,
    not just the one symptom from the live report.
    """
    en = _runtime_translations()
    title = en.get("issues", {}).get(key, {}).get("title", "")
    assert isinstance(title, str) and title.strip(), (
        f"issues.{key}.title in translations/en.json must be a "
        f"non-empty string — got {title!r}.  HA will render the bare "
        f"key on the Repairs UI."
    )


# ---------------------------------------------------------------------------
# Locale coverage regression: shipped in 1.0.17-beta.3, en.json gained
# ``issues`` + ``exceptions`` blocks but the nine other locales did not.
# HA's translation loader falls back to en.json for missing keys, so a
# German user currently sees the English Repair messages.  These tests
# guard the contract that every shipped locale carries the same set of
# Repair / exception keys with preserved ``{placeholder}`` tokens.
# ---------------------------------------------------------------------------


_PLACEHOLDER_RE = re.compile(r"\{\w+\}")

_NON_EN_LOCALES = (
    "de",
    "es",
    "fr",
    "it",
    "ja",
    "nl",
    "pl",
    "pt",
    "zh-Hans",
)

# Strong English markers — words / phrases unlikely to appear verbatim
# in a faithfully translated description.  If any one of these survives
# in a localised description it is a strong signal that the EN block
# was pasted in unchanged.  The list is intentionally short and uses
# only words rare across the target locales' shared vocabulary.
_EN_SENTINELS = (
    "smart charge",
    "sensor",
    "unable",
    "continue",
    "before the window",
    "schedule",
)


def _load_locale(locale: str) -> dict[str, Any]:
    path = _INTEGRATION / "translations" / f"{locale}.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text))


class TestNonEnglishLocalesMirrorIssueKeys:
    """C-020: every shipped locale must carry the same Repair issue
    catalogue as ``en.json``.  HA falls back to ``en.json`` for missing
    keys, which means a non-English user sees English text — a
    regression in operational transparency for non-English deployments.
    """

    @pytest.mark.parametrize("locale", _NON_EN_LOCALES)
    def test_locale_has_issues_block_with_all_en_keys(self, locale: str) -> None:
        en = _runtime_translations()
        en_keys = set(en.get("issues", {}).keys())
        data = _load_locale(locale)
        locale_issues = data.get("issues", {})
        missing = sorted(en_keys - set(locale_issues.keys()))
        assert not missing, (
            f"C-020 violation: locale {locale!r} is missing issues "
            f"entries present in en.json: {missing}.  HA will render "
            f"the English fallback for these keys, breaking "
            f"operational transparency for non-English users."
        )

    @pytest.mark.parametrize("locale", _NON_EN_LOCALES)
    def test_locale_has_exceptions_block_with_all_en_keys(self, locale: str) -> None:
        en = _runtime_translations()
        en_keys = set(en.get("exceptions", {}).keys())
        data = _load_locale(locale)
        locale_exceptions = data.get("exceptions", {})
        missing = sorted(en_keys - set(locale_exceptions.keys()))
        assert not missing, (
            f"C-020 violation: locale {locale!r} is missing exceptions "
            f"entries present in en.json: {missing}.  HomeAssistantError "
            f"messages raised by the integration will fall back to "
            f"English."
        )

    @pytest.mark.parametrize("locale", _NON_EN_LOCALES)
    def test_locale_issue_titles_and_descriptions_non_empty(self, locale: str) -> None:
        en = _runtime_translations()
        en_keys = set(en.get("issues", {}).keys())
        data = _load_locale(locale)
        locale_issues = data.get("issues", {})
        for key in sorted(en_keys):
            entry = locale_issues.get(key, {})
            title = entry.get("title", "")
            desc = entry.get("description", "")
            assert isinstance(title, str) and title.strip(), (
                f"locale {locale!r}: issues.{key}.title must be a "
                f"non-empty string — got {title!r}"
            )
            assert isinstance(desc, str) and desc.strip(), (
                f"locale {locale!r}: issues.{key}.description must be a "
                f"non-empty string — got {desc!r}"
            )

    @pytest.mark.parametrize("locale", _NON_EN_LOCALES)
    def test_locale_exception_messages_non_empty(self, locale: str) -> None:
        en = _runtime_translations()
        en_keys = set(en.get("exceptions", {}).keys())
        data = _load_locale(locale)
        locale_exceptions = data.get("exceptions", {})
        for key in sorted(en_keys):
            message = locale_exceptions.get(key, {}).get("message", "")
            assert isinstance(message, str) and message.strip(), (
                f"locale {locale!r}: exceptions.{key}.message must be a "
                f"non-empty string — got {message!r}"
            )

    @pytest.mark.parametrize("locale", _NON_EN_LOCALES)
    def test_locale_issue_descriptions_preserve_all_placeholders(
        self, locale: str
    ) -> None:
        """Every ``{name}`` token in the EN description must appear
        verbatim in the locale's description.  HA passes those
        placeholders by name at render time — losing one means the
        user sees a literal ``{current_soc}`` or a missing slot.
        """
        en = _runtime_translations()
        en_issues = en.get("issues", {})
        data = _load_locale(locale)
        locale_issues = data.get("issues", {})
        for key, en_entry in en_issues.items():
            en_desc = en_entry.get("description", "")
            en_title = en_entry.get("title", "")
            locale_entry = locale_issues.get(key, {})
            locale_desc = locale_entry.get("description", "")
            locale_title = locale_entry.get("title", "")

            # Description placeholders.
            en_desc_phs = _placeholders(en_desc)
            missing_desc = sorted(ph for ph in en_desc_phs if ph not in locale_desc)
            assert not missing_desc, (
                f"locale {locale!r}: issues.{key}.description has lost "
                f"placeholder(s) {missing_desc} relative to en.json — "
                f"HA will render an empty slot or literal token.\n"
                f"EN  description: {en_desc!r}\n"
                f"got description: {locale_desc!r}"
            )

            # Title placeholders too — some titles interpolate
            # ``{session_type}`` / ``{entity_id}``.
            en_title_phs = _placeholders(en_title)
            missing_title = sorted(ph for ph in en_title_phs if ph not in locale_title)
            assert not missing_title, (
                f"locale {locale!r}: issues.{key}.title has lost "
                f"placeholder(s) {missing_title} relative to en.json.\n"
                f"EN  title: {en_title!r}\n"
                f"got title: {locale_title!r}"
            )

    @pytest.mark.parametrize("locale", _NON_EN_LOCALES)
    def test_locale_exception_messages_preserve_all_placeholders(
        self, locale: str
    ) -> None:
        en = _runtime_translations()
        en_exceptions = en.get("exceptions", {})
        data = _load_locale(locale)
        locale_exceptions = data.get("exceptions", {})
        for key, en_entry in en_exceptions.items():
            en_msg = en_entry.get("message", "")
            locale_msg = locale_exceptions.get(key, {}).get("message", "")
            en_phs = _placeholders(en_msg)
            missing = sorted(ph for ph in en_phs if ph not in locale_msg)
            assert not missing, (
                f"locale {locale!r}: exceptions.{key}.message has lost "
                f"placeholder(s) {missing} relative to en.json.\n"
                f"EN  message: {en_msg!r}\n"
                f"got message: {locale_msg!r}"
            )

    @pytest.mark.parametrize("locale", _NON_EN_LOCALES)
    def test_locale_issue_descriptions_are_not_english(self, locale: str) -> None:
        """Best-effort sentinel check: a faithfully translated
        description should not contain any of the strong English
        markers used by the EN copy.  Catches a contributor accidentally
        pasting the EN block verbatim into a non-EN file.

        This is intentionally lenient — it only asserts on a small
        list of conspicuous English-only markers.  If a marker appears
        legitimately in a target locale (e.g. ``sensor`` is an English
        loanword in some locales), drop it from ``_EN_SENTINELS``
        rather than weaken the test.
        """
        en = _runtime_translations()
        en_issues = en.get("issues", {})
        data = _load_locale(locale)
        locale_issues = data.get("issues", {})
        for key in en_issues:
            locale_desc = locale_issues.get(key, {}).get("description", "")
            lowered = locale_desc.lower()
            hits = [m for m in _EN_SENTINELS if m in lowered]
            # If every sentinel that appears in the EN description also
            # appears in the locale description, the block is almost
            # certainly untranslated.
            en_desc_lower = en_issues[key].get("description", "").lower()
            en_hits = [m for m in _EN_SENTINELS if m in en_desc_lower]
            if not en_hits:
                # EN copy doesn't itself contain any sentinel for this
                # key — nothing to assert.
                continue
            assert hits != en_hits, (
                f"locale {locale!r}: issues.{key}.description contains "
                f"every English sentinel from the EN copy ({en_hits}) "
                f"— it appears to be the EN text pasted verbatim.\n"
                f"description: {locale_desc!r}"
            )
