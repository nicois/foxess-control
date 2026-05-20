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
