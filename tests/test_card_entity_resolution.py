"""Lovelace cards must resolve their sensor entity-id through the
``foxess_control/entity_map`` WebSocket command, not via a hardcoded
English default.

Background
----------
Home Assistant derives the entity_id from the *translated* friendly
name at entity-creation time.  A German-locale user sees
``sensor.foxess_intelligente_steuerung`` rather than
``sensor.foxess_smart_operations`` — the card's default fallback
silently misses the real entity and the user sees "Keine aktiven
Vorgänge" (no active operations) even during an active session, a
C-020 (operational transparency) violation.

The integration already exposes ``foxess_control/entity_map`` for
role-based discovery (see ``__init__.py::ws_entity_map``); both
forecast-card and history-card already use it via a ``_resolve(key)``
helper.  The control-card and taper-card must do the same.

Test strategy
-------------
Drive the actual browser DOM — these bugs live in the card's
shadow-DOM rendering path.  We start chromium via
``pytest-playwright``'s default ``page`` fixture (no HA container
needed), inject the card JS with ``add_script_tag``, and assign a
synthetic ``hass`` that mimics a DE-locale install:

* ``hass.states`` only contains
  ``sensor.foxess_intelligente_steuerung`` with
  ``charge_active: true, charge_phase: scheduled``.
* ``hass.callWS`` resolves ``foxess_control/entity_map`` to
  ``{smart_operations: "sensor.foxess_intelligente_steuerung"}``.

If the card resolves through ``_entityMap`` the shadow DOM shows
"Laden geplant" ("Charge Scheduled").  If it falls back to the
hardcoded English default, the shadow DOM shows "Keine aktiven
Vorgänge" — the user-observed bug.

Neighbourhood cases
-------------------
1. ``test_control_card_uses_entity_map_when_default_missing``:
   renamed entity, no config override → must render active content.
2. ``test_control_card_respects_explicit_config_override``:
   user sets ``operations_entity: sensor.my_custom`` → card uses it
   even if ``_entityMap`` has a different mapping (backwards
   compat).
3. ``test_control_card_falls_back_to_english_default_when_no_map``:
   ``callWS`` throws → card still falls back to
   ``sensor.foxess_smart_operations`` (legacy behaviour, no
   regression for English installs).
4. ``test_taper_card_uses_entity_map_when_default_missing``:
   taper card mirror of case 1.
5. ``test_taper_card_respects_explicit_config_override``:
   taper card mirror of case 2.

C-020 compliance
----------------
Users must determine system state from the UI alone.  The current
behaviour hides an active charge session behind a hardcoded-English
entity lookup — the user cannot tell from the UI whether a session
is running, and must inspect the HA entity registry to diagnose.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

_WWW_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "foxess_control"
    / "www"
)
_CONTROL_CARD_JS = _WWW_DIR / "foxess-control-card.js"
_TAPER_CARD_JS = _WWW_DIR / "foxess-taper-card.js"


# Synthetic DE-locale hass stub with an active charge session under
# the renamed entity_id.  callWS resolves the entity_map WS command
# unless the test overrides the behaviour.
_HASS_STUB_JS = r"""
window.__wsCalls = [];
window.makeHassDE = function(entityMap, stateOverride) {
    return {
        language: "de",
        states: {
            "sensor.foxess_intelligente_steuerung": stateOverride || {
                entity_id: "sensor.foxess_intelligente_steuerung",
                state: "scheduled",
                attributes: {
                    charge_active: true,
                    charge_phase: "scheduled",
                    charge_target_soc: 90,
                    charge_current_soc: 42,
                    charge_start_soc: 42,
                    charge_window: "11:00 – 13:59",
                    charge_remaining: "starts in 1h 23m",
                    taper_profile: {
                        charge: [{soc: 95, ratio: 0.6, count: 5}],
                        discharge: [],
                    },
                },
            },
        },
        callWS: function(msg) {
            window.__wsCalls.push(msg);
            if (msg && msg.type === "foxess_control/entity_map") {
                if (entityMap === null) {
                    return Promise.reject(new Error("WS failed"));
                }
                return Promise.resolve(entityMap || {});
            }
            return Promise.resolve({});
        },
        callService: function() { return Promise.resolve(); },
        callApi: function() { return Promise.resolve([]); },
    };
};
"""


def _inject_card(page: Page, card_js_path: Path) -> None:
    """Serve a blank page and inject the card JS + hass stub."""
    page.set_content(
        "<!doctype html><html><head><meta charset='utf-8'></head>"
        "<body><div id='root'></div></body></html>",
        wait_until="load",
    )
    page.add_script_tag(content=_HASS_STUB_JS)
    page.add_script_tag(content=card_js_path.read_text(encoding="utf-8"))


def _shadow_text(page: Page, selector: str) -> str:
    """Return the card element's shadow-root text content."""
    result = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el || !el.shadowRoot) return "__NO_SHADOW__";
            return el.shadowRoot.textContent || "";
        }""",
        selector,
    )
    return str(result)


# ---------------------------------------------------------------------------
# Control card
# ---------------------------------------------------------------------------


class TestControlCardEntityResolution:
    def test_control_card_uses_entity_map_when_default_missing(
        self, page: Page
    ) -> None:
        """DE-locale install: the real entity is
        ``sensor.foxess_intelligente_steuerung``.  With no user-set
        ``operations_entity``, the card must resolve via
        ``_entityMap`` and render the scheduled-charge section —
        *not* the "Keine aktiven Vorgänge" idle panel.
        """
        _inject_card(page, _CONTROL_CARD_JS)
        page.evaluate(
            """async () => {
                const entityMap = {
                    smart_operations: "sensor.foxess_intelligente_steuerung",
                };
                const card = document.createElement("foxess-control-card");
                card.setConfig({});  // NO operations_entity — the bug case
                document.getElementById("root").appendChild(card);
                card.hass = window.makeHassDE(entityMap);
                // Wait for callWS promise to resolve + re-render.
                await new Promise((r) => setTimeout(r, 50));
            }"""
        )
        text = _shadow_text(page, "foxess-control-card")
        assert "Keine aktiven Vorgänge" not in text, (
            "Card rendered idle panel — it ignored the entity_map and fell "
            f"back to the hardcoded English default.\nShadow text: {text!r}"
        )
        assert "Laden geplant" in text, (
            "Card should render the scheduled-charge section in German.\n"
            f"Shadow text: {text!r}"
        )

    def test_control_card_respects_explicit_config_override(self, page: Page) -> None:
        """Backwards-compatibility: an explicit ``operations_entity``
        in the YAML takes precedence over ``_entityMap``.  Covers the
        neighbourhood case opposite to the main bug.
        """
        _inject_card(page, _CONTROL_CARD_JS)
        result = page.evaluate(
            """async () => {
                // User configures a custom entity (perhaps a template sensor).
                const hass = {
                    language: "en",
                    states: {
                        "sensor.my_custom_ops": {
                            entity_id: "sensor.my_custom_ops",
                            state: "charging",
                            attributes: {
                                charge_active: true,
                                charge_phase: "active",
                                charge_target_soc: 80,
                                charge_current_soc: 50,
                                charge_window: "12:00 – 13:00",
                                charge_remaining: "0h 30m",
                                charge_power_w: 2500,
                            },
                        },
                        // Add entity_map target — but config override
                        // must win over it.
                        "sensor.foxess_smart_operations": {
                            entity_id: "sensor.foxess_smart_operations",
                            state: "idle",
                            attributes: {},
                        },
                    },
                    callWS: () =>
                        Promise.resolve({
                            smart_operations: "sensor.foxess_smart_operations",
                        }),
                    callService: () => Promise.resolve(),
                };
                const card = document.createElement("foxess-control-card");
                card.setConfig({operations_entity: "sensor.my_custom_ops"});
                document.getElementById("root").appendChild(card);
                card.hass = hass;
                await new Promise((r) => setTimeout(r, 50));
                const txt = card.shadowRoot.textContent || "";
                return {
                    has_idle: txt.includes("No active operations"),
                    has_active:
                        txt.includes("Charging")
                        || txt.includes("Smart Charge"),
                };
            }"""
        )
        assert not result["has_idle"], (
            "Card rendered idle — explicit operations_entity config was ignored."
        )
        assert result["has_active"], (
            "Card should render the active charge content for the "
            "explicitly-configured entity."
        )

    def test_control_card_falls_back_to_english_default_when_no_map(
        self, page: Page
    ) -> None:
        """Graceful degradation: if ``entity_map`` WS call fails AND
        no override is configured, the card should still use the
        historical default ``sensor.foxess_smart_operations``.  This
        preserves behaviour for English installs whose integration
        version predates the WS command or whose registry is
        transiently unavailable.
        """
        _inject_card(page, _CONTROL_CARD_JS)
        result = page.evaluate(
            """async () => {
                const hass = {
                    language: "en",
                    states: {
                        "sensor.foxess_smart_operations": {
                            entity_id: "sensor.foxess_smart_operations",
                            state: "charging",
                            attributes: {
                                charge_active: true,
                                charge_phase: "active",
                                charge_target_soc: 80,
                                charge_current_soc: 50,
                                charge_window: "12:00 – 13:00",
                                charge_remaining: "0h 30m",
                                charge_power_w: 2500,
                            },
                        },
                    },
                    callWS: () => Promise.reject(new Error("not registered")),
                    callService: () => Promise.resolve(),
                };
                const card = document.createElement("foxess-control-card");
                card.setConfig({});
                document.getElementById("root").appendChild(card);
                card.hass = hass;
                await new Promise((r) => setTimeout(r, 50));
                return card.shadowRoot.textContent || "";
            }"""
        )
        assert "No active operations" not in result, (
            "Card should fall back to sensor.foxess_smart_operations "
            f"when entity_map fails. Got shadow text: {result!r}"
        )


# ---------------------------------------------------------------------------
# Taper card
# ---------------------------------------------------------------------------


class TestTaperCardEntityResolution:
    def test_taper_card_uses_entity_map_when_default_missing(self, page: Page) -> None:
        """Same DE-locale scenario for the taper card: the taper
        profile data lives on ``sensor.foxess_intelligente_steuerung``.
        A broken resolver shows "Noch keine Beobachtungen" even when
        the sensor carries a populated ``taper_profile``.
        """
        _inject_card(page, _TAPER_CARD_JS)
        page.evaluate(
            """async () => {
                const entityMap = {
                    smart_operations: "sensor.foxess_intelligente_steuerung",
                };
                const card = document.createElement("foxess-taper-card");
                card.setConfig({});  // NO entity — the bug case
                document.getElementById("root").appendChild(card);
                card.hass = window.makeHassDE(entityMap);
                await new Promise((r) => setTimeout(r, 50));
            }"""
        )
        text = _shadow_text(page, "foxess-taper-card")
        # The stub's taper_profile has one charge bin.  If the card
        # resolves to the right entity it renders the "Laden" section;
        # otherwise it shows the "Noch keine Beobachtungen" empty state.
        assert "Noch keine Beobachtungen" not in text, (
            "Taper card rendered the empty-observation state — it ignored "
            "the entity_map and fell back to the hardcoded English default.\n"
            f"Shadow text: {text!r}"
        )
        assert "Laden" in text, (
            "Taper card should render the Laden (charge) section when "
            f"taper_profile has charge bins.\nShadow text: {text!r}"
        )

    def test_taper_card_respects_explicit_config_override(self, page: Page) -> None:
        """Backwards-compatibility: an explicit ``entity`` in the YAML
        config takes precedence over the entity_map.
        """
        _inject_card(page, _TAPER_CARD_JS)
        result = page.evaluate(
            """async () => {
                const hass = {
                    language: "en",
                    states: {
                        "sensor.my_taper_source": {
                            entity_id: "sensor.my_taper_source",
                            state: "idle",
                            attributes: {
                                taper_profile: {
                                    charge: [{soc: 75, ratio: 0.8, count: 10}],
                                    discharge: [],
                                },
                            },
                        },
                    },
                    callWS: () =>
                        Promise.resolve({
                            smart_operations: "sensor.something_else",
                        }),
                };
                const card = document.createElement("foxess-taper-card");
                card.setConfig({entity: "sensor.my_taper_source"});
                document.getElementById("root").appendChild(card);
                card.hass = hass;
                await new Promise((r) => setTimeout(r, 50));
                const sr = card.shadowRoot;
                const txt = sr.textContent || "";
                // The SoC-label proves the charge bin rendered from
                // the *configured* entity, not the empty fallback.
                const socLabels = Array.from(
                    sr.querySelectorAll(".soc-label")
                ).map((n) => n.textContent);
                return {text: txt, socLabels: socLabels};
            }"""
        )
        # The explicit entity has a charge bin at SoC 75.  If the card
        # used the (non-existent) entity_map target
        # sensor.something_else, no SoC labels render at all.
        assert "75%" in result["socLabels"], (
            "Taper card should render the SoC-75 charge bin from the "
            f"explicitly-configured entity. Got labels: {result['socLabels']!r}\n"
            f"Shadow text: {result['text']!r}"
        )
        assert "Charge" in result["text"], (
            "Taper card should render the Charge section title. "
            f"Got: {result['text']!r}"
        )


# ---------------------------------------------------------------------------
# Static-source belt-and-braces: guarantees against regression
# ---------------------------------------------------------------------------


def test_control_card_does_not_read_config_operations_entity_directly() -> None:
    """Source-level regression guard.  The card must never read
    ``this._config.operations_entity`` directly at runtime — all
    resolution must go through ``_resolve("operations_entity")``
    (or an equivalent helper that consults ``_entityMap``).

    The *editor* class uses the raw config for input-value
    hydration, which is fine.  We restrict the check to
    ``FoxESSControlCard`` — the runtime class.
    """
    src = _CONTROL_CARD_JS.read_text(encoding="utf-8")
    # Slice out the editor class — anything before
    # `class FoxESSControlCardEditor` is the runtime card.
    editor_idx = src.find("class FoxESSControlCardEditor")
    assert editor_idx > 0, "could not find editor class marker"
    runtime_src = src[:editor_idx]
    # Forbidden pattern: direct dereference of config.operations_entity
    # inside the runtime class (read path).
    bad_lines = [
        (lineno, line)
        for lineno, line in enumerate(runtime_src.splitlines(), 1)
        if "this._config.operations_entity" in line
    ]
    assert not bad_lines, (
        "Control card runtime code reads this._config.operations_entity "
        "directly — use _resolve('operations_entity') so the entity_map "
        "fallback applies for non-English installs:\n"
        + "\n".join(f"  line {n}: {L.strip()}" for n, L in bad_lines)
    )


def test_taper_card_does_not_read_config_entity_directly() -> None:
    """Source-level regression guard for the taper card."""
    src = _TAPER_CARD_JS.read_text(encoding="utf-8")
    bad_lines = [
        (lineno, line)
        for lineno, line in enumerate(src.splitlines(), 1)
        if "this._config.entity" in line
    ]
    assert not bad_lines, (
        "Taper card reads this._config.entity directly — use "
        "_resolve('entity') so the entity_map fallback applies for "
        "non-English installs:\n"
        + "\n".join(f"  line {n}: {L.strip()}" for n, L in bad_lines)
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
