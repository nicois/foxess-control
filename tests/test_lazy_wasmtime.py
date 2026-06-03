"""Lazy-wasmtime import contract.

The integration must import (and register its config flow) WITHOUT importing
``wasmtime``. ``wasmtime`` has no wheel on some platforms (e.g. 32-bit ARM),
so an eager import would make the whole integration fail to load and never
appear in HA's "Add Integration" list. The WASM signature engine is only
needed when making an authenticated web-portal request, so it must be
deferred until the first ``generate_signature`` call.

See ``docs/wasm-signature.md`` ("If web credentials are not configured, the
WASM module is never loaded and wasmtime is never imported").
"""

from __future__ import annotations

import builtins
import importlib
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest


def test_web_session_imports_without_wasmtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the foxess package / web_session must not import wasmtime.

    Simulates a platform with no wasmtime wheel by blocking the import, then
    asserts the integration's import path still succeeds.
    """
    # Clean slate: drop cached foxess modules + wasmtime so the import is
    # actually re-executed under the block. Done FIRST so other tests are
    # unaffected (they re-import fresh, and monkeypatch restores __import__).
    for m in list(sys.modules):
        if (
            m.startswith("custom_components.foxess_control.foxess")
            or m == "wasmtime"
            or m.startswith("wasmtime.")
        ):
            sys.modules.pop(m, None)

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "wasmtime" or name.startswith("wasmtime."):
            raise ModuleNotFoundError(
                "No module named 'wasmtime' (simulated missing wheel)"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    # Both must succeed with wasmtime unavailable.
    importlib.import_module("custom_components.foxess_control.foxess.web_session")
    importlib.import_module("custom_components.foxess_control.foxess")

    # Sanity: wasmtime was genuinely never imported during the above.
    assert "wasmtime" not in sys.modules


def test_generate_signature_still_works_when_wasmtime_present() -> None:
    """The lazy deferral must not break signing when wasmtime IS available.

    Proves the deferred import resolves and the cached engine produces the
    known signature output.
    """
    import custom_components.foxess_control.foxess.signature as sig_mod

    # Reset the singleton so heap state is fresh (matches the existing
    # known-signature test's requirement).
    sig_mod._engine = None
    sig = sig_mod.generate_signature("/basic/v0/user/login", "", "en", "1776124242356")
    assert sig == "02ed69731394e020c1a7e28d56a51013.5245784"

    # Engine is cached, not rebuilt per call.
    engine_after_first = sig_mod._engine
    assert engine_after_first is not None
    sig_mod.generate_signature("/basic/v0/user/login", "", "en", "1776124300000")
    assert sig_mod._engine is engine_after_first
