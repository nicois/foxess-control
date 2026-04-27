"""Tests for scripts/collect_ha_session.py locale-aware entity resolution.

The collector script must discover FoxESS entity_ids via the
integration's ``foxess_control/entity_map`` WS command rather than
assuming English friendly-name derivation.  A German-locale install
has ``sensor.foxess_intelligente_steuerung`` not
``sensor.foxess_smart_operations`` — a hardcoded list silently
collects nothing for renamed entities.

These tests cover:
1. ``resolve_default_sensors`` substitutes real entity_ids for every
   role found in the map.
2. ``resolve_default_sensors`` falls back to the English default when
   the WS command is unavailable (old integration, network failure,
   auth failure) or when a specific role is missing from the map.
3. ``_ENTITY_ROLES`` in the integration covers every role the script
   needs — a contract check that prevents the two from drifting.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "collect_ha_session.py"


@pytest.fixture(scope="module")
def collect_module() -> Any:
    """Load scripts/collect_ha_session.py as a module without executing
    its ``if __name__ == '__main__'`` guard."""
    spec = importlib.util.spec_from_file_location("collect_ha_session", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["collect_ha_session"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestResolveDefaultSensors:
    def test_resolves_roles_via_entity_map(self, collect_module: Any) -> None:
        """Every role present in the entity_map is substituted — this
        is the DE-locale path."""
        client = MagicMock()
        client.resolve_entity_map.return_value = {
            "battery_soc": "sensor.foxess_batterie_soc",
            "smart_operations": "sensor.foxess_intelligente_steuerung",
            "override_status": "sensor.foxess_status",
            "smart_charge_active": "binary_sensor.foxess_intelligentes_laden_aktiv",
        }

        resolved = collect_module.resolve_default_sensors(client)

        assert "sensor.foxess_batterie_soc" in resolved
        assert "sensor.foxess_intelligente_steuerung" in resolved
        assert "binary_sensor.foxess_intelligentes_laden_aktiv" in resolved
        # Roles not in the map fall back to English.
        assert "sensor.foxess_house_load" in resolved

    def test_empty_map_falls_back_to_english(self, collect_module: Any) -> None:
        """WS unavailable / old integration / auth failure: every role
        falls back to its English default."""
        client = MagicMock()
        client.resolve_entity_map.return_value = {}

        resolved = collect_module.resolve_default_sensors(client)

        english_defaults = [fallback for _, fallback in collect_module.DEFAULT_ROLES]
        assert resolved == english_defaults

    def test_preserves_order_of_default_roles(self, collect_module: Any) -> None:
        """Output ordering must match DEFAULT_ROLES declaration order
        so the JSONL timeline is stable across runs."""
        client = MagicMock()
        client.resolve_entity_map.return_value = {
            "battery_soc": "sensor.x_soc",
            "house_load": "sensor.x_load",
        }

        resolved = collect_module.resolve_default_sensors(client)

        # DEFAULT_ROLES starts battery_soc, house_load, ... — same order
        # must appear in output.
        assert resolved[0] == "sensor.x_soc"
        assert resolved[1] == "sensor.x_load"


class TestEntityRolesContract:
    """Every role the script requests must be present in the
    integration's ``_ENTITY_ROLES`` map.  Otherwise the WS command
    returns nothing for that role and we silently fall back to
    English on non-English installs — the whole point of this work."""

    def test_every_script_role_is_registered_by_integration(
        self, collect_module: Any
    ) -> None:
        from custom_components.foxess_control import _ENTITY_ROLES

        script_roles = {role for role, _ in collect_module.DEFAULT_ROLES}
        registered_roles = set(_ENTITY_ROLES.keys())
        missing = script_roles - registered_roles

        assert not missing, (
            f"scripts/collect_ha_session.py requests roles that are not "
            f"registered in custom_components/foxess_control/__init__.py "
            f"_ENTITY_ROLES: {sorted(missing)}. Without registration the "
            f"foxess_control/entity_map WS command returns no entity_id "
            f"for these roles and the script silently falls back to the "
            f"English default — defeating the locale-aware lookup."
        )
