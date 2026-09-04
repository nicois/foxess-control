/**
 * Shared freshness / staleness logic for the FoxESS Lovelace cards.
 *
 * Imported by both `foxess-control-card.js` and `foxess-overview-card.js`.
 * The alternative was a copy of the threshold table in each card, which is
 * how the two would drift apart: the overview card was fixed in
 * 1.0.22-beta.5 and the control card was left with a 30 s threshold against
 * a 300 s poll interval for another release.
 *
 * Both importers are registered with HA as `res_type: "module"` and served
 * from the same static directory, so a relative sibling import resolves.
 * They import it *dynamically*, propagating their own `?v=` query
 * (`import.meta.url`), because HA serves these files with
 * `Cache-Control: public, max-age=2678400` — a query-less sibling would be
 * pinned in browser caches for a month after an upgrade while the
 * version-stamped card around it was refetched.
 *
 * Deliberately holds no translated strings.  Each card keeps its own
 * TRANSLATIONS table (ten locales, parity enforced by
 * tests/test_card_translations.py), so a card can word its banner for its
 * own context without the two having to agree.
 */

/**
 * How long data may go unrefreshed before a card calls itself stale, per
 * data source, in seconds.
 *
 * Anchored to the cadence each source actually runs at: the REST poll is
 * DEFAULT_POLLING_INTERVAL (300 s), so three missed polls is a real fault;
 * the WebSocket pushes every ~5 s, so a minute of silence already is. A
 * flat 30 s threshold marks a perfectly healthy REST install stale for
 * ~90% of every interval, which trains users to ignore the indicator
 * entirely — that is why the production report's "API - 45m" did not stand
 * out from the "API - 2m" the same card showed the rest of the time.
 */
export const STALE_AFTER = { ws: 60, api: 900, modbus: 900 };
export const STALE_AFTER_DEFAULT = 900;

/** Seconds since `lastUpdate`, or null if there is no usable timestamp. */
export function ageSeconds(lastUpdate, now) {
  if (!lastUpdate) return null;
  const then = new Date(lastUpdate).getTime();
  if (Number.isNaN(then)) return null;
  const ref = typeof now === "number" ? now : Date.now();
  return Math.max(0, Math.round((ref - then) / 1000));
}

/** "45s" / "12m" / "2h05m" — compact enough for a badge. */
export function formatAge(seconds) {
  if (typeof seconds !== "number") return "";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${m % 60}m`;
}

/** True once `age` has passed the threshold for `dataSource`. */
export function isStaleAge(dataSource, age) {
  if (typeof age !== "number") return false;
  const after = STALE_AFTER[dataSource] || STALE_AFTER_DEFAULT;
  return age > after;
}

/**
 * Why the card is not live: "connection", "data", or null when it is.
 *
 * The two need opposite responses — "check your browser/network" versus
 * "check the inverter or the cloud API" — and they are easy to confuse,
 * because the age a card displays is computed client-side against
 * Date.now().  A disconnected frontend therefore makes the age grow
 * without bound while every reading on the card is frozen, so reporting
 * that as data staleness sends the user to the wrong system.
 * `hass.connected` is the only thing that tells them apart.
 *
 * Production report 2026-08-27: the overview card showed "API - 45m" for
 * 45 minutes while the integration polled successfully every 5 minutes and
 * its server-side age never exceeded 300 s.
 */
export function staleReason({ connected, dataSource, age }) {
  if (connected === false) return "connection";
  return isStaleAge(dataSource, age) ? "data" : null;
}

/**
 * The stale banner's own styling.
 *
 * Full strength while the readings behind it are dimmed, so the one part
 * of the card that *is* current stays readable.  Theme variables only, so
 * it reads correctly in both light and dark themes.
 */
export const STALE_BANNER_CSS = `
  .stale-banner {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
    line-height: 1.3;
    color: var(--primary-text-color);
    background: rgba(var(--rgb-warning-color, 255, 152, 0), 0.28);
    border-bottom: 2px solid var(--warning-color, #ffa600);
  }
  .stale-icon {
    font-size: 13px;
  }
`;

/**
 * Dim and desaturate the given selectors, so a stale card cannot be
 * mistaken for a live one at a glance.  Each card passes the regions that
 * hold its *readings* — never the banner.
 */
export function staleDimCss(selectors) {
  return `
  ${selectors} {
    opacity: 0.55;
    filter: grayscale(1);
  }
`;
}
