"""Test-as-policy: every direct ``page.evaluate`` / ``page.wait_for_function``
/ ``page.locator(...).first.screenshot`` call in ``tests/e2e/*.py`` MUST live
inside one of the dedicated retry helpers, never in a test body.

The four module-level helpers in ``tests/e2e/test_ui.py``
(``_safe_evaluate``, ``_safe_screenshot``, ``_find_card``,
``_wait_for_card_hass``) and the two helpers in
``tests/e2e/conftest.py`` (``_wait_for_lovelace_panel``,
``_wait_for_stage``) wrap these Playwright primitives with retry on
``PlaywrightError("Execution context was destroyed, most likely
because of a navigation")``.  HA fires background navigations
(WebSocket reconnect, dashboard auto-refresh, auth refresh ~1-15s
after initial load) that destroy the JS execution context mid-call.
Without the retry layer, any direct call hits a hard failure on the
race.

This was the root cause of the v1.0.17-beta.4 release-blocker on
``TestControlCard::test_safety_floor_row_appears_when_tracked[cloud]``
(GH Actions run id 26137320539): the test body's
``page.evaluate(...)`` at ``tests/e2e/test_ui.py:1528`` bypassed the
existing ``_safe_evaluate`` retry helper and failed immediately on the
navigation race with::

    playwright._impl._errors.Error: Page.evaluate: Execution context was
    destroyed, most likely because of a navigation

The fix is to route every call through the matching helper.  This
test enforces the contract structurally so the next direct call
introduced surfaces at unit-test time rather than as a CI flake.

Pattern follows ``tests/test_e2e_test_ui_helpers.py`` (the bounded-
settle structural budget tests landed in commit ``a47677b``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Functions where direct calls to the Playwright primitives are
# permitted.  These ARE the helpers — they implement the retry layer
# so test bodies don't have to.  Module-qualified by their containing
# file (so a same-named helper in a different file is not silently
# excluded).
_HELPER_FUNCTIONS: dict[str, frozenset[str]] = {
    "tests/e2e/test_ui.py": frozenset(
        {
            "_safe_evaluate",
            "_safe_screenshot",
            "_find_card",
            "_wait_for_card_hass",
        }
    ),
    "tests/e2e/conftest.py": frozenset(
        {
            "_wait_for_lovelace_panel",
            "_wait_for_stage",
        }
    ),
}

# Names of the Playwright methods we forbid in test bodies.
_GUARDED_METHODS = frozenset({"evaluate", "wait_for_function"})


def _e2e_test_files() -> list[Path]:
    """Collect every Python file under ``tests/e2e/``."""
    repo_root = Path(__file__).resolve().parent.parent
    e2e_dir = repo_root / "tests" / "e2e"
    return sorted(p for p in e2e_dir.glob("*.py") if p.name != "__init__.py")


def _is_page_evaluate_call(node: ast.Call) -> bool:
    """Return True if ``node`` is ``page.evaluate(...)`` or
    ``page.wait_for_function(...)`` (i.e. the Playwright primitives
    we want to force through the retry helpers)."""
    fn = node.func
    if not isinstance(fn, ast.Attribute):
        return False
    if fn.attr not in _GUARDED_METHODS:
        return False
    # We only flag the bare-``page`` form.  Class-scoped helpers like
    # ``self._safe_evaluate(page, ...)`` route through the retry layer
    # by construction and don't show up as ``page.<method>``.
    return isinstance(fn.value, ast.Name) and fn.value.id == "page"


def _is_locator_screenshot_call(node: ast.Call) -> bool:
    """Return True if ``node`` is ``page.locator(...).first.screenshot(...)``
    or ``page.locator(...).screenshot(...)``.

    This is the third primitive that's vulnerable to context destruction —
    the locator handle resolves lazily and the element it points to may
    be detached by the time ``.screenshot()`` runs.
    """
    fn = node.func
    if not isinstance(fn, ast.Attribute):
        return False
    if fn.attr != "screenshot":
        return False
    target = fn.value
    # Strip an optional ``.first`` accessor.
    if isinstance(target, ast.Attribute) and target.attr == "first":
        target = target.value
    # The target should be a ``page.locator(...)`` call.
    if not isinstance(target, ast.Call):
        return False
    inner = target.func
    return (
        isinstance(inner, ast.Attribute)
        and inner.attr == "locator"
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "page"
    )


def _walk_with_function_stack(
    tree: ast.Module,
) -> list[tuple[ast.Call, tuple[str, ...]]]:
    """Yield every ``ast.Call`` paired with the chain of enclosing
    function/method names (outermost first).
    """
    found: list[tuple[ast.Call, tuple[str, ...]]] = []

    def _visit(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                _visit(child, stack + (child.name,))
            elif isinstance(child, ast.Call):
                found.append((child, stack))
                _visit(child, stack)
            else:
                _visit(child, stack)

    _visit(tree, ())
    return found


def _collect_offences(path: Path, repo_root: Path) -> list[str]:
    """Return human-readable offence descriptions for ``path``."""
    rel = path.relative_to(repo_root).as_posix()
    helpers = _HELPER_FUNCTIONS.get(rel, frozenset())
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    offences: list[str] = []
    for call, stack in _walk_with_function_stack(tree):
        # If any enclosing function is one of the helpers, this call
        # IS part of the retry layer — that's where the primitives
        # legitimately live.
        if any(name in helpers for name in stack):
            continue
        is_evaluate = _is_page_evaluate_call(call)
        is_screenshot = _is_locator_screenshot_call(call)
        if not (is_evaluate or is_screenshot):
            continue
        method = call.func.attr if isinstance(call.func, ast.Attribute) else "<unknown>"
        kind = "page.locator(...).screenshot" if is_screenshot else f"page.{method}"
        scope = ".".join(stack) if stack else "<module>"
        offences.append(f"{rel}:{call.lineno}: {kind} in {scope}")
    return offences


class TestE2EHelperUsagePolicy:
    """Every direct ``page.evaluate`` / ``page.wait_for_function`` /
    ``page.locator(...).screenshot`` call in ``tests/e2e/*.py`` MUST live
    inside one of the dedicated retry helpers (``_safe_evaluate``,
    ``_safe_screenshot``, ``_find_card``, ``_wait_for_card_hass``,
    ``_wait_for_lovelace_panel``, ``_wait_for_stage``).
    """

    def test_no_direct_page_evaluate_in_test_bodies(self) -> None:
        """Walks the AST of every ``tests/e2e/*.py`` file and asserts
        no test-body code calls the guarded Playwright primitives
        outside the retry helpers.

        Reproduces the v1.0.17-beta.4 release-blocker structurally:
        the failing call at ``tests/e2e/test_ui.py:1528`` (and every
        other unwrapped call) shows up in the offences list.

        Failure output lists every offending line so callers can
        wrap each one in the matching helper.
        """
        repo_root = Path(__file__).resolve().parent.parent
        files = _e2e_test_files()
        assert files, "no tests/e2e/*.py files found — repo layout changed?"

        all_offences: list[str] = []
        for path in files:
            all_offences.extend(_collect_offences(path, repo_root))

        if all_offences:
            offences_str = "\n  ".join(all_offences)
            pytest.fail(
                "Direct page.evaluate / page.wait_for_function / "
                "page.locator(...).screenshot calls in test bodies bypass "
                "the context-destruction retry helpers (_safe_evaluate, "
                "_safe_screenshot, _find_card, _wait_for_card_hass, "
                "_wait_for_lovelace_panel, _wait_for_stage).  HA's "
                "background navigations destroy the JS execution context "
                "mid-call (release-blocker on v1.0.17-beta.4 against "
                "test_safety_floor_row_appears_when_tracked[cloud], run id "
                "26137320539).  Route each call through the matching "
                f"helper.  Offences:\n  {offences_str}"
            )

    def test_helper_definitions_are_recognised(self) -> None:
        """Sanity check: every name in ``_HELPER_FUNCTIONS`` actually
        resolves to a function/method definition in its declared file.

        Catches the silent-failure mode where a helper was renamed
        or moved and the policy stops protecting it.
        """
        repo_root = Path(__file__).resolve().parent.parent
        for rel, helpers in _HELPER_FUNCTIONS.items():
            path = repo_root / rel
            assert path.exists(), f"helper file {rel} missing"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            defined = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            missing = helpers - defined
            assert not missing, (
                f"helper(s) {sorted(missing)} declared for {rel} "
                f"but not defined there — rename or move?"
            )
