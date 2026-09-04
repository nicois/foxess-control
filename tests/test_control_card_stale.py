"""The control card must say when its view is not live, and must not
invite the user to act on a view that cannot reach Home Assistant.

Production report (1.0.22-beta.5)
---------------------------------
A user clicked **Cancel** on a smart charge session, confirmed it, and
the card kept showing a live charging session "even after a long time".
They concluded cancel was broken.

Their instance says otherwise: the session went ``idle`` at 12:20:01 and
again at 12:21:22 and stayed idle for 45 minutes, until one of their own
automations re-armed it. The integration did exactly what it was asked.
What failed was the browser — it had stopped receiving state updates —
and then the card, which gave the user nothing to see that with.

Two defects follow, and both are C-020 (state determinable from the UI
alone) failures:

1. **No staleness indication at all.** 1.0.22-beta.5 added a staleness
   banner and dimming to ``foxess-overview-card.js``; the control card
   got nothing. A stale *overview* shows misleading numbers. A stale
   *control* card invites you to act on them: ``_handleAction`` calls
   ``callService`` regardless of how old the view is, so with a dead
   WebSocket the click is swallowed and the user is left believing they
   acted.
2. **Cancel has no acknowledgement.** The confirming click fired the
   service and re-rendered from unchanged state, so nothing at all
   changed on screen until HA pushed a new state. That is precisely why
   this user could not distinguish "my click didn't register" from "it
   worked and the session is ending".

Test strategy
-------------
Same technique as ``tests/test_overview_card_stale.py``: drive the real
shipped card in a real Chromium DOM, feed it a stub ``hass``, and inspect
its shadow root — so these fail when the card's *behaviour* is wrong, not
merely when its source text changed. Visual assertions read **computed**
style, never class names, because a class name proves nothing about what
the user sees.

Cards are loaded as ES modules over a routed origin (see
``tests/card_dom.py``) because that is how HA serves them and because the
staleness logic is a shared sibling module imported by both cards.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from .card_dom import WWW_DIR, load_card, serve_cards

if TYPE_CHECKING:
    from playwright.sync_api import Page

# Cadences the integration actually runs at, so the thresholds under test
# are anchored to reality rather than to a number someone liked: REST
# polls every 300 s (DEFAULT_POLLING_INTERVAL), WS pushes every ~5 s.
_REST_INTERVAL = 300
_WS_INTERVAL = 5

# Per-source staleness thresholds the card is required to use, mirroring
# the overview card: three missed REST polls, one minute of WS silence.
_STALE_AFTER = {"ws": 60, "api": 900, "modbus": 900}
_STALE_AFTER_DEFAULT = 900

_HARNESS_JS = r"""
window.__fx = {serviceCalls: []};

window.__fxMakeHass = function(opts) {
  const lastUpdate = new Date(Date.now() - opts.ageSeconds * 1000).toISOString();
  const attrs = opts.active ? {
    discharge_active: true,
    discharge_phase: "active",
    discharge_current_soc: 55,
    discharge_start_soc: 60,
    discharge_min_soc: 20,
    discharge_power_w: 3000,
    discharge_window: "12:00 - 13:00",
    discharge_remaining: "0h 30m",
  } : {};
  return {
    connected: opts.connected,
    language: "en",
    states: {
      "sensor.foxess_smart_operations": {
        entity_id: "sensor.foxess_smart_operations",
        state: opts.active ? "discharging" : "idle",
        attributes: attrs,
      },
      "sensor.foxess_battery_soc": {
        entity_id: "sensor.foxess_battery_soc",
        state: "55",
        attributes: opts.source ? {data_source: opts.source} : {},
      },
      "sensor.foxess_data_freshness": {
        entity_id: "sensor.foxess_data_freshness",
        state: opts.source || "unknown",
        attributes: {last_update: lastUpdate},
      },
    },
    callWS: async () => ({}),
    callService: function(domain, service, data) {
      window.__fx.serviceCalls.push({domain: domain, service: service, data: data});
      if (opts.serviceMode === "reject") {
        return Promise.reject(new Error("connection lost"));
      }
      if (opts.serviceMode === "hang") {
        return new Promise(() => {});
      }
      return Promise.resolve();
    },
  };
};

window.__fxMount = function(opts) {
  const root = document.getElementById("root");
  root.replaceChildren();
  window.__fx.serviceCalls = [];
  const el = document.createElement("foxess-control-card");
  el.setConfig({});
  // setConfig clears _entityMap, so seed it afterwards.  Seeding means the
  // card never needs callWS: its setter only fetches when the map is unset.
  el._entityMap = {
    smart_operations: "sensor.foxess_smart_operations",
    battery_soc: "sensor.foxess_battery_soc",
    data_freshness: "sensor.foxess_data_freshness",
  };
  if (opts.ackTimeoutMs) el._cancelAckTimeoutMs = opts.ackTimeoutMs;
  root.appendChild(el);
  window.__fxCard = el;
  el.hass = window.__fxMakeHass(opts);
  return window.__fxProbe();
};

/** Push a fresh hass object, as HA does on every state change. */
window.__fxPush = function(opts) {
  window.__fxCard.hass = window.__fxMakeHass(opts);
  return window.__fxProbe();
};

window.__fxProbe = function() {
  const r = window.__fxCard.shadowRoot;
  const card = r.querySelector("ha-card");
  const banner = r.querySelector(".stale-banner");
  const header = r.querySelector(".header");
  const content = r.querySelector(".content");
  const badge = r.querySelector(".data-source");
  const notice = r.querySelector(".action-notice");
  const cs = (el) => (el ? getComputedStyle(el) : null);
  const hs = cs(header);
  const ct = cs(content);
  const bn = cs(banner);
  return {
    stale: card ? card.classList.contains("stale") : null,
    cardClass: card ? (card.getAttribute("class") || "") : null,
    bannerText: banner ? banner.textContent.trim() : null,
    bannerOpacity: bn ? bn.opacity : null,
    bannerFilter: bn ? bn.filter : null,
    headerOpacity: hs ? hs.opacity : null,
    headerFilter: hs ? hs.filter : null,
    contentOpacity: ct ? ct.opacity : null,
    contentFilter: ct ? ct.filter : null,
    badgeClass: badge ? badge.className : null,
    badgeText: badge ? badge.textContent.trim() : null,
    noticeText: notice ? notice.textContent.trim() : null,
    noticeClass: notice ? notice.className : null,
    buttons: Array.from(r.querySelectorAll(".action-btn")).map((b) => ({
      action: b.dataset.action || "",
      text: b.textContent.trim(),
      disabled: b.disabled === true || b.hasAttribute("disabled"),
      title: b.getAttribute("title") || "",
    })),
    serviceCalls: window.__fx.serviceCalls.slice(),
  };
};

/** Click a button by data-action.  Uses .click(), which browsers make a
 *  no-op on a disabled control — so "disabled" is tested as the user
 *  experiences it, not as an attribute. */
window.__fxClick = function(action) {
  const b = window.__fxCard.shadowRoot.querySelector(
    '.action-btn[data-action="' + action + '"]');
  if (!b) return null;
  b.click();
  return window.__fxProbe();
};

/** Cancel needs two clicks (confirm-then-act).  Doing both inside one
 *  evaluate keeps the pair well inside the 3s confirm window regardless
 *  of how loaded the machine is. */
window.__fxConfirmCancel = function() {
  window.__fxClick("cancel");
  return window.__fxClick("cancel");
};
"""


def _prepare(page: Page) -> None:
    errors = serve_cards(page)
    page.add_script_tag(content=_HARNESS_JS)
    load_card(page, "foxess-control-card.js", "foxess-control-card", errors=errors)


def _mount(
    page: Page,
    *,
    connected: bool = True,
    source: str | None = "api",
    age_seconds: int = 30,
    active: bool = False,
    service_mode: str = "resolve",
    ack_timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Render the real card with a stub hass and report what it produced."""
    _prepare(page)
    return _opts_call(
        page,
        "__fxMount",
        connected=connected,
        source=source,
        age_seconds=age_seconds,
        active=active,
        service_mode=service_mode,
        ack_timeout_ms=ack_timeout_ms,
    )


def _push(
    page: Page,
    *,
    connected: bool = True,
    source: str | None = "api",
    age_seconds: int = 30,
    active: bool = False,
    service_mode: str = "resolve",
) -> dict[str, Any]:
    """Push a new hass object at an already-mounted card."""
    return _opts_call(
        page,
        "__fxPush",
        connected=connected,
        source=source,
        age_seconds=age_seconds,
        active=active,
        service_mode=service_mode,
    )


def _opts_call(page: Page, fn: str, **kwargs: Any) -> dict[str, Any]:
    opts = {
        "connected": kwargs["connected"],
        "source": kwargs["source"],
        "ageSeconds": kwargs["age_seconds"],
        "active": kwargs["active"],
        "serviceMode": kwargs["service_mode"],
        "ackTimeoutMs": kwargs.get("ack_timeout_ms"),
    }
    result: dict[str, Any] = page.evaluate(f"(o) => window.{fn}(o)", opts)
    return result


def _probe(page: Page) -> dict[str, Any]:
    result: dict[str, Any] = page.evaluate("() => window.__fxProbe()")
    return result


def _click(page: Page, action: str) -> dict[str, Any]:
    result = page.evaluate("(a) => window.__fxClick(a)", action)
    assert result is not None, f"no .action-btn[data-action={action!r}] to click"
    return dict(result)


def _confirm_cancel(page: Page) -> dict[str, Any]:
    result = page.evaluate("() => window.__fxConfirmCancel()")
    assert result is not None, "no cancel button to confirm"
    return dict(result)


def _button(probe: dict[str, Any], action: str) -> dict[str, Any] | None:
    for b in probe["buttons"]:
        if b["action"] == action:
            return dict(b)
    return None


def _require_button(probe: dict[str, Any], action: str) -> dict[str, Any]:
    btn = _button(probe, action)
    assert btn is not None, f"no {action!r} button rendered: {probe['buttons']!r}"
    return btn


def _wait_for_notice(page: Page, timeout: int = 5000) -> dict[str, Any]:
    """Wait for an action-row notice to appear.  No sleeps: the notice is
    produced by a settled promise or a bounded timer, both of which are
    observable events."""
    page.wait_for_function(
        "() => !!window.__fxCard.shadowRoot.querySelector('.action-notice')",
        timeout=timeout,
    )
    return _probe(page)


# ---------------------------------------------------------------------------
# 1. A disconnected frontend
# ---------------------------------------------------------------------------


class TestDisconnectedFrontendIsObvious:
    """``hass.connected === false`` froze every reading on this card."""

    def test_card_is_marked_stale(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60, active=True)
        assert r["stale"] is True, (
            "the frontend is disconnected, so every reading is frozen — the "
            f"card must mark itself stale (class={r['cardClass']!r})"
        )

    def test_banner_blames_the_connection_not_the_inverter(self, page: Page) -> None:
        """The wording must send the user to the right place.

        A disconnected browser is not an inverter problem; telling this
        user their inverter data was old would have sent them to check the
        cloud API when the fault was in their own browser session.
        """
        r = _mount(page, connected=False, age_seconds=45 * 60, active=True)
        assert r["bannerText"], "no stale banner rendered while disconnected"
        text = r["bannerText"].lower()
        assert "connection" in text or "connected" in text, (
            f"banner must name the connection as the cause, got {r['bannerText']!r}"
        )
        assert "inverter" not in text, (
            "a disconnected browser must not be reported as inverter data "
            f"staleness, got {r['bannerText']!r}"
        )

    def test_banner_reports_the_age(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60, active=True)
        assert "45" in (r["bannerText"] or ""), (
            f"banner should say how old the readings are, got {r['bannerText']!r}"
        )

    def test_disconnected_beats_a_fresh_age(self, page: Page) -> None:
        """A small age proves nothing: it was computed from a value the
        page received *before* it lost contact."""
        r = _mount(page, connected=False, age_seconds=5, active=True)
        assert r["stale"] is True


class TestStaleTreatmentIsVisuallyObvious:
    """Computed style, not class names — a class name is not a pixel."""

    def test_readings_are_dimmed(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60, active=True)
        assert r["contentOpacity"] is not None, "no .content rendered"
        assert float(r["contentOpacity"]) < 0.8, (
            f"stale readings must be visibly dimmed, "
            f"content opacity={r['contentOpacity']}"
        )
        assert float(r["headerOpacity"]) < 0.8, (
            f"the stale header (SoC, badge) must be dimmed too, "
            f"opacity={r['headerOpacity']}"
        )

    def test_readings_are_desaturated(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60, active=True)
        assert r["contentFilter"] not in (None, "none"), (
            f"stale readings must be desaturated, filter={r['contentFilter']!r}"
        )
        assert r["headerFilter"] not in (None, "none"), (
            f"stale header must be desaturated, filter={r['headerFilter']!r}"
        )

    def test_banner_itself_stays_at_full_strength(self, page: Page) -> None:
        """Dimming the explanation along with the readings would defeat
        the point — the banner is the one thing that *is* current."""
        r = _mount(page, connected=False, age_seconds=45 * 60, active=True)
        assert r["bannerOpacity"] is not None, "no banner rendered"
        assert float(r["bannerOpacity"]) == 1.0, (
            f"the banner must not be dimmed, opacity={r['bannerOpacity']}"
        )
        assert r["bannerFilter"] in (None, "none"), (
            f"the banner must not be desaturated, filter={r['bannerFilter']!r}"
        )


class TestDisconnectedDisablesTheControls:
    """An enabled button that cannot reach HA is a lie.

    With no WebSocket the service call cannot be delivered, so the click
    is swallowed and the user believes they acted — which is exactly what
    happened in the production report.
    """

    def test_cancel_is_disabled_during_a_session(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60, active=True)
        cancel = _button(r, "cancel")
        assert cancel is not None, "no cancel button rendered during a session"
        assert cancel["disabled"] is True, (
            "Cancel cannot reach HA while the frontend is disconnected, so it "
            f"must not look clickable, got {cancel!r}"
        )

    def test_charge_and_discharge_are_disabled_when_idle(self, page: Page) -> None:
        r = _mount(page, connected=False, age_seconds=45 * 60, active=False)
        for action in ("charge", "discharge"):
            btn = _button(r, action)
            assert btn is not None, f"no {action} button rendered"
            assert btn["disabled"] is True, (
                f"{action} cannot reach HA while disconnected, got {btn!r}"
            )

    def test_a_disabled_cancel_fires_no_service_call(self, page: Page) -> None:
        """The behavioural half of the assertion above."""
        _mount(page, connected=False, age_seconds=45 * 60, active=True)
        r = _confirm_cancel(page)
        assert r["serviceCalls"] == [], (
            "a disconnected card must not attempt clear_overrides, "
            f"got {r['serviceCalls']!r}"
        )

    def test_the_user_is_told_why_the_buttons_are_dead(self, page: Page) -> None:
        """C-020: a greyed-out button with no explanation is its own
        transparency failure."""
        r = _mount(page, connected=False, age_seconds=45 * 60, active=True)
        cancel = _button(r, "cancel")
        assert cancel is not None
        explained = (
            "connection" in (r["bannerText"] or "").lower() and bool(cancel["title"])
        ) or "connection" in cancel["title"].lower()
        assert explained, (
            "the card must state why the controls are unavailable — banner "
            f"{r['bannerText']!r}, button title {cancel['title']!r}"
        )


# ---------------------------------------------------------------------------
# 2. Connected and fresh — nothing must change
# ---------------------------------------------------------------------------


class TestHealthyCardIsUntouched:
    def test_no_banner_and_no_dimming(self, page: Page) -> None:
        r = _mount(page, connected=True, source="api", age_seconds=30, active=True)
        assert r["stale"] is False, f"healthy card marked stale: {r['cardClass']!r}"
        assert r["bannerText"] is None, (
            f"healthy card must show no banner, got {r['bannerText']!r}"
        )
        assert float(r["contentOpacity"]) == 1.0, (
            f"healthy readings must be at full strength, opacity={r['contentOpacity']}"
        )
        assert r["contentFilter"] in (None, "none")
        assert float(r["headerOpacity"]) == 1.0
        assert r["headerFilter"] in (None, "none")

    def test_buttons_are_enabled_during_a_session(self, page: Page) -> None:
        r = _mount(page, connected=True, source="api", age_seconds=30, active=True)
        cancel = _button(r, "cancel")
        assert cancel is not None
        assert cancel["disabled"] is False, f"healthy Cancel disabled: {cancel!r}"

    def test_buttons_are_enabled_when_idle(self, page: Page) -> None:
        r = _mount(page, connected=True, source="api", age_seconds=30, active=False)
        for action in ("charge", "discharge"):
            btn = _button(r, action)
            assert btn is not None, f"no {action} button rendered"
            assert btn["disabled"] is False, f"healthy {action} disabled: {btn!r}"

    def test_badge_does_not_cry_stale_at_a_normal_poll_age(self, page: Page) -> None:
        """The control card carried the same cry-wolf threshold the
        overview card was fixed for: 30 s against a 300 s poll, so a
        healthy install showed the stale badge ~90% of the time."""
        r = _mount(page, connected=True, source="api", age_seconds=120, active=True)
        assert r["badgeClass"] is not None, "no data-source badge rendered"
        assert "stale" not in r["badgeClass"], (
            f"120 s is normal for a {_REST_INTERVAL} s poll, but the badge "
            f"called it stale: {r['badgeClass']!r} / {r['badgeText']!r}"
        )


# ---------------------------------------------------------------------------
# 3. Connected but the data is old
# ---------------------------------------------------------------------------


class TestConnectedButStaleData:
    def test_banner_blames_the_data_not_the_connection(self, page: Page) -> None:
        r = _mount(
            page,
            connected=True,
            source="api",
            age_seconds=3 * _REST_INTERVAL + 60,
            active=True,
        )
        assert r["stale"] is True, "three missed polls must register as stale"
        text = (r["bannerText"] or "").lower()
        assert "connection" not in text, (
            f"connected but stale data must not blame the connection, "
            f"got {r['bannerText']!r}"
        )
        assert "stale" in text or "inverter" in text, (
            f"banner must name the data as the problem, got {r['bannerText']!r}"
        )

    def test_readings_are_dimmed(self, page: Page) -> None:
        r = _mount(
            page,
            connected=True,
            source="api",
            age_seconds=3 * _REST_INTERVAL + 60,
            active=True,
        )
        assert float(r["contentOpacity"]) < 0.8, (
            f"stale data must be visibly dimmed, opacity={r['contentOpacity']}"
        )

    def test_controls_stay_enabled(self, page: Page) -> None:
        """Deliberate: the user may be cancelling *because* they can see
        something is wrong.

        The service call reaches HA and HA acts on server-side truth, not
        on this browser's snapshot; ``clear_overrides`` is idempotent and
        strictly safety-increasing. Removing the one control that fixes
        the situation would be worse than the staleness it reacts to.
        """
        r = _mount(
            page,
            connected=True,
            source="api",
            age_seconds=3 * _REST_INTERVAL + 60,
            active=True,
        )
        cancel = _button(r, "cancel")
        assert cancel is not None
        assert cancel["disabled"] is False, (
            "stale readings are no reason to take Cancel away from a "
            f"connected user, got {cancel!r}"
        )

    def test_cancel_still_reaches_home_assistant(self, page: Page) -> None:
        _mount(
            page,
            connected=True,
            source="api",
            age_seconds=3 * _REST_INTERVAL + 60,
            active=True,
        )
        r = _confirm_cancel(page)
        assert [(c["domain"], c["service"]) for c in r["serviceCalls"]] == [
            ("foxess_control", "clear_overrides")
        ], f"stale-but-connected Cancel must still fire, got {r['serviceCalls']!r}"


class TestThresholdsMatchEachSourceCadence:
    """Stale must mean stale — not "a poll interval has elapsed"."""

    @pytest.mark.parametrize("source", ["ws", "api", "modbus"])
    def test_boundary_either_side(self, page: Page, source: str) -> None:
        limit = _STALE_AFTER[source]
        fresh = _mount(page, connected=True, source=source, age_seconds=limit)
        assert fresh["stale"] is False, (
            f"{source}: {limit}s is the threshold itself and must still count as live"
        )
        stale = _mount(page, connected=True, source=source, age_seconds=limit + 1)
        assert stale["stale"] is True, (
            f"{source}: {limit + 1}s is past the {limit}s threshold and must be stale"
        )

    @pytest.mark.parametrize("age", [1, 60, _REST_INTERVAL, _REST_INTERVAL + 30])
    def test_api_tolerates_a_normal_poll_interval(self, page: Page, age: int) -> None:
        r = _mount(page, connected=True, source="api", age_seconds=age)
        assert r["stale"] is False, (
            f"age {age}s is normal for a {_REST_INTERVAL}s poll interval, but "
            f"the card called it stale"
        )

    def test_ws_is_tighter_than_rest(self, page: Page) -> None:
        """WS pushes every ~5 s, so minutes of silence is already wrong —
        while the same age is unremarkable for the REST poll."""
        fresh = _mount(page, connected=True, source="ws", age_seconds=_WS_INTERVAL * 3)
        assert fresh["stale"] is False
        stale = _mount(page, connected=True, source="ws", age_seconds=180)
        assert stale["stale"] is True
        rest = _mount(page, connected=True, source="api", age_seconds=180)
        assert rest["stale"] is False, (
            "180 s is three WS thresholds but well inside one REST poll — the "
            "two sources must not share a threshold"
        )

    def test_unknown_source_falls_back_to_the_conservative_default(
        self, page: Page
    ) -> None:
        fresh = _mount(
            page, connected=True, source=None, age_seconds=_STALE_AFTER_DEFAULT
        )
        assert fresh["stale"] is False
        stale = _mount(
            page, connected=True, source=None, age_seconds=_STALE_AFTER_DEFAULT + 1
        )
        assert stale["stale"] is True


# ---------------------------------------------------------------------------
# 4. Cancel must acknowledge itself
# ---------------------------------------------------------------------------


class TestCancelAcknowledgement:
    """Fire-and-forget plus a re-render from unchanged state is silence."""

    def test_first_click_asks_for_confirmation(self, page: Page) -> None:
        _mount(page, connected=True, source="api", age_seconds=30, active=True)
        r = _click(page, "cancel")
        cancel = _button(r, "cancel")
        assert cancel is not None
        assert "confirm" in cancel["text"].lower(), (
            f"first click must ask to confirm, got {cancel['text']!r}"
        )
        assert r["serviceCalls"] == [], (
            f"the first click must not fire the service, got {r['serviceCalls']!r}"
        )

    def test_confirming_click_acknowledges_immediately(self, page: Page) -> None:
        """The heart of the report: the user must see *something* change
        the instant they confirm, before HA pushes any new state."""
        _mount(page, connected=True, source="api", age_seconds=30, active=True)
        r = _confirm_cancel(page)
        assert [(c["domain"], c["service"]) for c in r["serviceCalls"]] == [
            ("foxess_control", "clear_overrides")
        ]
        cancel = _button(r, "cancel")
        assert cancel is not None, "the cancel button vanished on confirm"
        assert cancel["disabled"] is True, (
            f"an in-flight cancel must not be re-clickable, got {cancel!r}"
        )
        label = cancel["text"].lower()
        assert "cancel" in label and label not in ("cancel", "confirm cancel?"), (
            "the button must acknowledge that the cancel is in flight rather "
            f"than reverting to its idle label, got {cancel['text']!r}"
        )

    def test_acknowledgement_clears_when_the_session_ends(self, page: Page) -> None:
        _mount(page, connected=True, source="api", age_seconds=30, active=True)
        pending = _confirm_cancel(page)
        assert _button(pending, "cancel") is not None

        # HA pushes the state the cancel produced: session over.
        r = _push(page, connected=True, source="api", age_seconds=5, active=False)
        assert _button(r, "cancel") is None, (
            "with the session ended the cancel button must be gone, not stuck "
            f"acknowledging: {r['buttons']!r}"
        )
        for action in ("charge", "discharge"):
            btn = _button(r, action)
            assert btn is not None, f"idle card must offer {action}: {r['buttons']!r}"
            assert btn["disabled"] is False
        assert r["noticeText"] is None, (
            f"a completed cancel must leave no notice behind, got {r['noticeText']!r}"
        )

    def test_acknowledgement_clears_when_the_phase_changes(self, page: Page) -> None:
        """Any genuine news clears the pending state — the card must not
        keep saying "cancelling" over a state it has just re-rendered."""
        _mount(page, connected=True, source="api", age_seconds=30, active=True)
        _confirm_cancel(page)
        page.evaluate(
            """() => {
                const h = window.__fxMakeHass(
                    {connected: true, source: "api", ageSeconds: 5, active: true});
                const a = h.states["sensor.foxess_smart_operations"].attributes;
                a.discharge_phase = "suspended";
                window.__fxCard.hass = h;
            }"""
        )
        r = _probe(page)
        cancel = _button(r, "cancel")
        assert cancel is not None
        assert cancel["disabled"] is False, (
            "the pending acknowledgement must clear once the session state "
            f"changes, got {cancel!r}"
        )

    def test_a_failed_call_is_surfaced_not_swallowed(self, page: Page) -> None:
        """If ``callService`` rejects, the user must be told — being left
        looking at an unchanged card is the bug we are fixing."""
        _mount(
            page,
            connected=True,
            source="api",
            age_seconds=30,
            active=True,
            service_mode="reject",
        )
        _confirm_cancel(page)
        r = _wait_for_notice(page)
        assert r["noticeText"], "a rejected cancel produced no visible notice"
        assert "cancel" in r["noticeText"].lower() or "fail" in (
            r["noticeText"].lower()
        ), f"the notice must name what failed, got {r['noticeText']!r}"
        cancel = _button(r, "cancel")
        assert cancel is not None
        assert cancel["disabled"] is False, (
            f"after a failure the user must be able to retry, got {cancel!r}"
        )

    def test_acknowledgement_does_not_stick_when_nothing_happens(
        self, page: Page
    ) -> None:
        """A permanently "cancelling..." card would be a new bug, not a fix.

        The service call never settles and no state ever arrives; the card
        must give up within its own bound and say so.
        """
        _mount(
            page,
            connected=True,
            source="api",
            age_seconds=30,
            active=True,
            service_mode="hang",
            ack_timeout_ms=200,
        )
        pending = _confirm_cancel(page)
        assert _require_button(pending, "cancel")["disabled"] is True

        r = _wait_for_notice(page)
        assert r["noticeText"], "the bounded wait expired with nothing shown"
        cancel = _button(r, "cancel")
        assert cancel is not None
        assert cancel["disabled"] is False, (
            "once the card has given up waiting, Cancel must be usable "
            f"again, got {cancel!r}"
        )

    def test_the_bound_is_not_reached_on_the_happy_path(self, page: Page) -> None:
        """The give-up notice must not fire when the cancel worked.

        Asserted deterministically on the timer handle rather than by
        waiting out the bound: clearing the pending state *must* also
        cancel the timer, or the notice would arrive later over a card
        that has already moved on. The short wait afterwards is the only
        way to assert the negative (that nothing appears), and it is
        bounded by the test-shortened 200 ms ack timeout rather than the
        15 s production one.
        """
        _mount(
            page,
            connected=True,
            source="api",
            age_seconds=30,
            active=True,
            ack_timeout_ms=200,
        )
        _confirm_cancel(page)
        _push(page, connected=True, source="api", age_seconds=5, active=False)
        page.wait_for_function(
            "() => window.__fxCard._cancelPending !== true", timeout=5000
        )
        assert page.evaluate("() => !window.__fxCard._cancelAckTimer") is True, (
            "clearing the pending cancel must also clear its give-up timer"
        )
        page.wait_for_timeout(400)
        r = _probe(page)
        assert r["noticeText"] is None, (
            f"a successful cancel must not raise a give-up notice, got "
            f"{r['noticeText']!r}"
        )

    def test_retry_after_failure_fires_a_second_call(self, page: Page) -> None:
        _mount(
            page,
            connected=True,
            source="api",
            age_seconds=30,
            active=True,
            service_mode="reject",
        )
        _confirm_cancel(page)
        _wait_for_notice(page)
        r = _confirm_cancel(page)
        assert len(r["serviceCalls"]) == 2, (
            f"retry must reach HA again, got {r['serviceCalls']!r}"
        )


# ---------------------------------------------------------------------------
# 5. The shared module must actually be reachable in production
# ---------------------------------------------------------------------------

# `import("./foo.js")` or `from "./foo.js"` inside a card, ignoring any query.
_SIBLING_IMPORT_RE = re.compile(r"""["'`]\./(?P<name>[\w-]+\.js)""")


class TestSharedModulesAreServed:
    """Sharing logic between cards has one failure mode, and it is total.

    A card whose sibling import 404s never reaches
    ``customElements.define``, so the user gets "Custom element doesn't
    exist" instead of a card. Every module a card imports must therefore be
    registered as a static path — and must *not* be registered as a Lovelace
    resource, since HA would try to load it as a card.
    """

    def test_every_sibling_import_is_registered_as_a_static_path(self) -> None:
        from custom_components.foxess_control import _CARD_URLS, _SUPPORT_URLS

        served = {url.rsplit("/", 1)[-1] for url in _CARD_URLS + _SUPPORT_URLS}
        missing: dict[str, set[str]] = {}
        for card in sorted(WWW_DIR.glob("*.js")):
            imported = {
                m.group("name")
                for m in _SIBLING_IMPORT_RE.finditer(card.read_text(encoding="utf-8"))
            }
            gap = {name for name in imported if name not in served}
            if gap:
                missing[card.name] = gap
        assert not missing, (
            "these sibling modules are imported but never served — the "
            "importing card will fail to define its element:\n"
            + "\n".join(f"  {c}: {sorted(g)}" for c, g in missing.items())
        )

    def test_every_sibling_import_exists_on_disk(self) -> None:
        missing: dict[str, set[str]] = {}
        for card in sorted(WWW_DIR.glob("*.js")):
            gap = {
                m.group("name")
                for m in _SIBLING_IMPORT_RE.finditer(card.read_text(encoding="utf-8"))
                if not (WWW_DIR / m.group("name")).is_file()
            }
            if gap:
                missing[card.name] = gap
        assert not missing, f"sibling imports with no file: {missing!r}"

    def test_support_modules_are_not_lovelace_resources(self) -> None:
        """``_SUPPORT_URLS`` define no custom element; registering one as a
        resource would make HA log a card-load failure on every dashboard."""
        from custom_components.foxess_control import _CARD_URLS, _SUPPORT_URLS

        overlap = set(_CARD_URLS) & set(_SUPPORT_URLS)
        assert not overlap, f"support modules registered as cards: {overlap!r}"
        for url in _SUPPORT_URLS:
            src = (WWW_DIR / url.rsplit("/", 1)[-1]).read_text(encoding="utf-8")
            assert "customElements.define" not in src, (
                f"{url} defines a custom element, so it belongs in _CARD_URLS"
            )
