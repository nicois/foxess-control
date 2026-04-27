# Changelog

## 1.0.14-beta.1

### Fixed
- **Control and taper cards showed "No active operations" on non-English Home Assistant installs (C-020 violation)**: during an active smart charge session, a user running HA in German saw "Keine aktiven Vorgänge" on the control card despite `charge_active=true` and `charge_phase=scheduled` on the backing sensor. The cards hardcoded `sensor.foxess_smart_operations` as the `operations_entity` default, but HA derives entity_ids from the translated friendly name at entity-creation time — in DE the real entity is `sensor.foxess_intelligente_steuerung`, in FR `sensor.foxess_operations_intelligentes`, etc. The integration already exposes `foxess_control/entity_map` for role-based discovery, and the forecast/history cards already consult it via a `_resolve(key)` helper; the control and taper cards ignored it. Users had no way to determine system state from the UI alone, requiring entity-registry inspection — a direct C-020 violation. Fixed by routing both cards through a `_resolve(key)` helper matching the forecast/history pattern: explicit `operations_entity:` config > `_entityMap["smart_operations"]` from the WS command > hardcoded English default as last-resort fallback. The taper card previously didn't fetch the entity map at all; it now does. Backwards-compatible for users with explicit dashboard YAML overrides. Seven-test regression suite (`tests/test_card_entity_resolution.py`) drives the card JS in a Playwright-chromium stub, reproducing the exact DE-locale symptom — four cases fail against pre-fix card code, three passed throughout (backwards-compat + graceful-degradation + source-level guards that catch any future regression into direct `this._config.operations_entity` reads).
- **`logs.txt` added to `.gitignore`**: ad-hoc HA log captures from live debugging sessions should not be tracked.

## 1.0.13

### Added
- **Pacing-transparency UI on the control card (UX #4 / #6 / #8)** — four new data-surface attributes on `sensor.foxess_smart_operations` (`discharge_deferred_reason` + `charge_deferred_reason`, `discharge_safety_floor_w` + `discharge_peak_consumption_kw` + `discharge_paced_target_w`, `discharge_grid_export_limit_w` + `discharge_clamp_active`) are rendered directly as rows on `foxess-control-card`:
  - **Deferred reason** (UX #4) — both charge and discharge sections render a wide row with the explanatory text produced by `_explain_*_deferral()` whenever the deferred phase is active. Users finally see *why* pacing is holding back (e.g. "Feed-in limit reached — holding until more solar is available", "Export clamp slack 5.5 kW exceeds projected peak 3.0 kW; single headroom applied"). Longer strings wrap via new `.detail-row-wide` / `.detail-value-wrap` styling.
  - **Safety floor** (UX #6) — a `safety_floor` row appears whenever `discharge_safety_floor_w > 0`. An upward-arrow icon (`mdi:arrow-up-bold`) surfaces when the paced target is below the floor (the C-001 floor is actively raising paced power). The row is click-expandable: tapping it reveals a translation-aware explainer with the *actual* peak value interpolated (e.g. *"Minimum discharge power. Computed as peak household load (1.0 kW) × 1.5, so the battery can cover sudden load spikes without pulling from the grid."*). Mobile-friendly — no hover needed.
  - **Export clamp split** (UX #8) — when `grid_export_limit` is configured, the discharge power row splits into an *inverter / export* pair separated by `/`. The export side takes the warning colour and shows a `mdi:fence` icon when `discharge_clamp_active` is true, so the user can see immediately that hardware clamping is capping their export. Unchanged for sites without an export limit.
- **Standalone `foxess-taper-card` (UX #5)** rendering the BMS acceptance-ratio histogram from the new `taper_profile` attribute on `sensor.foxess_smart_operations` (both charge and discharge histograms as chart-friendly `{soc, ratio, count}` lists). The card shows per-SoC-bin horizontal bars with observation count annotation and a low-confidence marker (`·`) for bins with fewer than 3 observations. Users opt in by adding `type: custom:foxess-taper-card` to their dashboard; an ApexCharts variant is also covered as a user template in `docs/lovelace-examples.md`.
- **Card translation coverage test** (`tests/test_card_translations.py`): parses each card's `TRANSLATIONS` table and asserts every non-English locale carries every English key. Structurally prevents the class of bug that left the `info_log` sensor ID broken in beta.7 — a locale missing a key silently falls back to the raw key name.
- **E2E card-wiring tests**: 7 new `TestControlCard` tests (split power row, clamp-active toggle, safety-floor row + expandable explainer, charge/discharge deferred-reason rows, charge section title phase distinction) and 3 new `TestTaperCard` tests (card mount, empty state, seeded-profile bar widths). Taper card added to the E2E Lovelace dashboard.
- **D-051 "Transparency attributes surfaced via card rows, not tooltips"** captures the design decision — visible rows over hover tooltips / debug panels / consolidated toggles — in `docs/knowledge/04-design/lovelace-cards.md`, tracing to C-020 and C-034.

### Fixed
- **Structured events silently dropped when a user set a per-module log level**: `emit_event` used `logger.info(message, extra=...)`, which Python's logging framework evaluates against `Logger.isEnabledFor()` *before* propagation to ancestor handlers. On a live HA (v1.0.12) with a `logger:` YAML config pinning `custom_components.foxess_control.foxess.inverter` above INFO, every `SCHEDULE_WRITE` event emitted from `Inverter._post_schedule()` was dropped at the child's level check — never reaching the debug-log sensor attached to the parent `custom_components.foxess_control` logger. Fixed by changing `emit_event` to build the `LogRecord` via `logger.makeRecord()` and dispatch via `logger.handle()`, bypassing `isEnabledFor()` while still running the logger's filter chain and every handler's level/filter chain. Structured events are telemetry the integration emits on its own behalf; visibility is controlled at the handler level, not at the logger level. Applies to all six event types (`ALGO_DECISION`, `TICK_SNAPSHOT`, `SCHEDULE_WRITE`, `TAPER_UPDATE`, `SERVICE_CALL`, `SESSION_TRANSITION`). Three-test regression suite `TestInverterScheduleWriteReachesParentHandler` covers the exact production symptom (child at WARNING) + default-levels baseline + session-context-survives-child-override neighbourhood.
- **Page-fixture flake in E2E CI caused by HA's housekeeping navigation** (Flaky Test Detection surfaced this on four consecutive tag runs v1.0.11–v1.0.13-beta.2; live-diagnosed by observing a real HA container). Two compounding bugs in the staged Lovelace-panel wait (`tests/e2e/conftest.py`): (a) the final stage's predicate was a bare `!!panel` attach check, which could return truthy moments before HA's service-worker/auth-refresh navigation at t≈12s detached the panel — surfacing either as a 30s `wait_for_function` timeout or, in the test body, as `Locator.screenshot: Element is not attached to the DOM`; (b) `_wait_for_stage` re-capped every retry at `max_stage_ms=30000` even when 50+ seconds of overall budget remained, so a post-navigation rebuild under slow-shard CPU contention had insufficient time to complete. Stage-3 predicate now requires three *settled* signals (`hass.connected === true`, `ha-panel-lovelace` mounted, `panel.hass` wired) — proving the panel is past HA's initial navigation churn, not merely attached; and a new `_LOVELACE_STAGE_TIMEOUTS_MS` mapping lets the final stage use the full remaining overall budget on retries while keeping the tight 30s cap on stages 1+2 (shadow-root attachment is synchronous and catastrophic failures must still time out fast).
- **SoakRecorder dropped invariant-violation detail when persisting run results**: `save_run` in `tests/soak/results_db.py` wrote a `violations` count to the `runs` row but never inserted corresponding `events` rows, so the rule name and detail message were lost. Observed on a 2026-04-23 nightly when `test_charge_solar_then_spike` recorded `violations=3` with zero persisted detail — making post-mortem diagnosis impossible and hollowing out the C-020 observability value of the recorder. Each `InvariantViolation` now emits an `event_type='violation'` row with `{rule}: {detail}` as the event detail, anchored to the nearest sample's SoC/power/state so cross-run queries (`SELECT * FROM events WHERE event_type='violation'`) can correlate violations back to system state.

## 1.0.12

### Fixed
- **Every FoxESS sensor silently freezes during a charge session's pre-window phase** (surfaced 2026-04-25 by the new `collect_ha_session.py` trace collector). `SmartOperationsSensor.native_value` can return `"scheduled"` — a value added in 1.0.11-beta.9 to distinguish the charge card's pre-window state from the deferred state — but the sensor's `_attr_options` list was not updated when that phase value was introduced. HA's sensor base class rejects any state not in `_attr_options` and raises `ValueError` from `async_write_ha_state()`. Because `DataUpdateCoordinator.async_update_listeners()` iterates listeners sequentially and the exception was uncaught, **every listener registered after `SmartOperationsSensor` stopped receiving updates**. Observed blast radius on a live instance: coordinator polling succeeded every 5 minutes (`success: True` in the debug log), yet SoC, house load, generation, work mode, charge rate and every other FoxESS sensor had `last_reported`/`last_changed` frozen at their pre-session values for 50+ minutes. The `smart_operations` sensor itself stuck at `idle` (its last valid state) while the *binary*-sensor-class `smart_charge_active` — which registers earlier in the listener iteration — correctly showed `on`. Fixed by adding `"scheduled"` to `_attr_options`. Regression test `TestSmartOperationsSensorOptionsCoverage` parametrises over every string `native_value` can return and asserts each is in options — structurally prevents the class of defect.

### Added
- **Repair surface for sensor state-write failures (C-026)**: the "scheduled-options" freeze above was invisible to the user until live tracing found it — the `ValueError` was log-only, and no Repair issue was raised while every sensor silently stopped updating for 50+ minutes. Defence-in-depth hardening: sensor listener callbacks now route through `_safe_write_ha_state()`, which wraps `async_write_ha_state()` in a narrow `(ValueError, RuntimeError)` try/except. On failure it logs, creates an HA Repair issue naming the offending entity and the error, and crucially **does NOT re-raise** — so listener iteration continues and other sensors still update. On the next successful write for that sensor, the Repair is dismissed automatically. Applied to `SmartOperationsOverviewSensor` and `InverterOverrideStatusSensor` (the two listener callbacks in `smart_battery/sensor_base.py`); the helper pattern scales to any future listener. New `sensor_write_failed` translation in `strings.json` provides the Repair card text. Seven-test regression suite (`TestSensorListenerFailureSurfacesRepair`) covers: Repair created with entity_id, recovery dismisses Repair, repeated failures don't spam (idempotent), listener iteration continues when one sensor raises, pattern applies to multiple sensor classes, helper logs the exception, happy-path is invisible.
- **`schedule_write` structured event emission**: `smart_battery/events.py` has defined `SCHEDULE_WRITE` since 1.0.11-beta.7 but no call site was emitting it — grep showed the constant declared but never referenced. Live trace collection confirmed the gap: a 15-minute charge session captured `tick_snapshot` + `algo_decision` events but zero `schedule_write` records, despite confirmed inverter schedule writes. `Inverter._post_schedule()` now funnels every `/op/v0/device/scheduler/enable` call through a single path that emits one `SCHEDULE_WRITE` event per write (payload: `groups` list + API `response` + `endpoint` + `call_site`). Both `Inverter.set_schedule` and `Inverter.set_work_mode` route through it, so every code path that writes an inverter schedule (direct writes from `_services.py` / `foxess_adapter.py`, or `self_use`/`force_charge`/`force_discharge` via `set_work_mode`) produces exactly one event. Payload is JSON-serialisable so the replay harness can reconstruct exact API state changes; committed sample trace `tests/replay_traces/sample_schedule_write.jsonl` is picked up by the parametrised replay-regression test.
- **`scripts/collect_ha_session.py` — merged event + observation timeline collector**: produces a single JSONL per session capturing both what the integration *decided* (algorithm decisions, schedule writes, session transitions, tick snapshots) **and** what the real inverter + house were *doing* at the same moments (SoC, house load, PV, grid flows, BMS temperature, work mode). This pairing is what simulator validation needs — feed the exogenous observations into the simulator, replay the decisions, and assert the simulator's resulting state agrees with HA's. Two modes: `live` (polls every 5 s, appends per-session files, auto-detects the log sensor across integration versions) and `history` (reconstructs a past time range via `/api/history/period/`). Paired systemd service `systemd/foxess-collect-ha.service` runs it continuously with `Restart=always` / 10-minute backoff between attempts (survives sustained HA outages without API-hammering). Credentials live in `~/.config/foxess-collect-ha.env` (not in repo); `systemd/foxess-collect-ha.env.example` ships the template.
- **Soak-artefact run-directory collision fix**: `systemd/foxess-soak.service` inlined its ExecStart script; systemd's own `%`/`$`-substitution layer swallowed `${TS}_${SHORT}` and `$$` before bash saw them, so every nightly run wrote into `runs/_/` — overwriting the prior night's artefacts. Extracted to `systemd/foxess-soak-run.sh` so bash expands variables normally; runs now land in per-timestamped directories (`runs/20260425_080536_d6dc5bc/...`) as intended, enabling cross-run comparison.

## 1.0.11

### Added
- **Smart discharge via hardware export-limit actuator** (`SmartDischargeExportLimitSensor`, `discharge_export_limit_w` attribute, new **Max Grid Export Limit Entity** mapping in entity mode): when a `foxess_modbus` "Max Grid Export Limit" number entity is mapped, smart discharge modulates feed-in by writing the inverter's hardware export cap each tick instead of mutating the cloud schedule's `fdPwr`. The cloud schedule is pinned at inverter max; the actuator is written with the paced target, clamped to `[peak_consumption × 1.5, Grid Export Limit]` — the C-001 safety floor is the lower bound. Sub-threshold deltas are suppressed to avoid chatty modbus writes. Every session exit (timer expire, circuit-breaker abort, SoC threshold, feed-in limit, manual cancel) reverts the entity to the configured maximum. Opt-in: unchanged when no actuator entity is mapped. Simulator gained `max_grid_export_limit_w` with curtailment physics for E2E coverage.
- **Deferred-phase slack on the control card**: while a smart charge or discharge session is in the `deferred` phase (window open, pacing algorithm waiting to act), the card shows a "slack" row giving the algorithm's live countdown to forced action — the same `deferred_start − now` the listener recomputes each tick, so solar surplus grows slack and load spikes shrink it. New `charge_time_slack_s` / `discharge_time_slack_s` sensor attributes (integer seconds, `null` outside the deferred phase). Translated into all 10 card languages.
- **Charge re-deferral when ahead of schedule**: during the active-charge phase, if solar pushes SoC ahead of the pacing trajectory, the listener re-enters the deferred phase (stops forced charging) and re-evaluates each tick; it only resumes forced charging when the deadline requires it. Previously, once `charging_started`, the listener only adjusted power (min 100 W) and could not revert to self-use — causing the target to be reached 30+ minutes early, wasting cheap-rate self-use time.
- **Unreachable charge target surfaced as an HA Repair issue**: when the pacing algorithm detects the target cannot be reached in the remaining window, an HA Repair is raised (dismissed automatically once reachable again or when the session ends). Paired with an outlier-robust feasibility check that blends the taper-integrated estimate with a median-ratio linear estimate — prevents false alarms when a few isolated low taper observations skew the integrated estimate, while still catching genuine unreachability.
- **Distinct `scheduled` / `deferred` / `charging` phases on the charge side** (to match the discharge side's `scheduled` / `deferred` / `suspended` / `discharging`): the UI no longer conflates "window not yet open" with "window open, pacing deferred". `charge_phase` now reports `scheduled` before the window opens and `deferred` during the window when forced charging has been pushed later; new `charge_deferred` translation key wired into all 10 card languages.
- **Structured event emission + replay harness** for pacing-algorithm regression testing: algorithm decisions, tick snapshots, service calls, session transitions, schedule writes, and taper updates are emitted through the logging pipeline as JSON-serialisable records (`smart_battery/events.py`). `smart_battery/replay.py` re-invokes each `algo_decision` with its recorded inputs and flags divergences; `tests/test_replay.py::test_committed_trace_replays_clean` parametrises over any JSONL in `tests/replay_traces/`, making traces executable regression protection against algorithm drift. The existing info/debug log sensors capture events for out-of-process collection via `scripts/collect_events.py`.
- **Nightly soak test suite** (`tests/soak/`, 19 scenarios: basic charge/discharge, solar interaction, spiky load, BMS high-SoC taper, cold battery, large battery, tight windows, extreme taper, very-cold current limiting, combined charge-then-discharge cycle, solar-exceeds-load/target, near-min-SoC, heavy-load-during-deferral): runs full sessions through containerised HA + simulator, verifying SoC-overshoot, no-import, and target-reach invariants. Simulator auto-tick loop (5 s steps) advances the model in real time so sessions progress without explicit fast-forward. Results land in a SQLite `soak_results.db` with state transitions, SoC direction changes (2% deadband), and power-step changes as discrete events — enabling cross-run comparison between tags. Systemd timer runs the latest tagged release nightly.
- **Grid Export Limit configuration option** (default 5 kW, 0–20 kW): net export cap set on the inverter (DNO / firmware limit). When non-zero, smart discharge deferral treats it as the effective maximum export rate and the active-discharge phase requests inverter max power.

### Fixed
- **Deferred-start feed-in headroom over-defers on export-limited sites** (beta.14, surfaced by live session monitoring): `calculate_discharge_deferred_start` previously applied the doubled feed-in headroom (up to 40%) unconditionally whenever `feedin_energy_limit_kwh` was set. The doubled margin is justified when household load volatility can erode effective export, but when **Grid Export Limit** is configured well below inverter max power, load must exceed `max_power − grid_export_limit` before net export degrades at all — so on many sites the doubled buffer was protecting against a scenario the hardware made physically impossible, eating ~3 min of self-use time per session. Now conditional on whether `max(net_consumption_kw, consumption_peak_kw)` exceeds the clamp slack: if the clamp actively shields export rate, single headroom (10%) is used; otherwise doubled headroom still applies. Six-test regression suite `TestFeedinHeadroomAccountsForExportClamp` covers live-session parameters plus neighbourhood (unlimited export, boundary `limit == max_power`, peak above/below slack, net-consumption variant, C-001 floor preservation).
- **Discharge pacing wasted energy when export-limited**: when a **Grid Export Limit** was configured, power pacing reduced discharge power below inverter maximum even though firmware already caps grid export, under-utilising the battery's contribution to house load. Now uses maximum inverter power during active discharge and relies on deferral timing for energy management.
- **Taper profile blind to BMS curtailment during paced charging** (beta.1): `_record_taper_observation` used the paced power request as the denominator instead of inverter max. When pacing reduced the request below the BMS limit (e.g. 4552 W paced vs 6380 W accepted at 81% SoC), the ratio exceeded 1.0, was clamped to 1.0, and the profile silently recorded "no taper" at high SoC. Subsequent sessions had no taper data for the 80–100% band, producing inaccurate time estimates and deferred start calculations.
- **Spurious "unreachable charge target" repair issue caused by outlier taper observations** (beta.10): taper-integrated estimate summed isolated outliers (bins 81: 0.05, 83: 0.41, 85: 0.16, 90: 0.21 surrounded by 0.87–1.0 neighbours) to produce 1.04 h of taper-weighted charge hours, pushed over 1.09 h by the 10% headroom buffer, and fired a false Repair. Fixed by blending taper-integrated with median-ratio linear estimate and taking the minimum — genuine unreachability still flagged because the median is also low in that case.
- **Sensor countdown / phase labels mis-matched listener decisions** (beta.10 / beta.12): the sensor-side deferred start estimate used a simplified formula without headroom, taper profile, net consumption, or BMS temperature, so the card showed wrong countdown times and wrong phase labels ("Charge Scheduled" when charging had started; "0 m" or "39 m" instead of ~24 m). `is_effectively_charging()`, `estimate_charge_remaining()`, and `estimate_discharge_remaining()` now call the same algorithm with the same parameters the listeners use (C-038 sensor-listener parameter parity).
- **Deferred-phase charge card SoC hidden at high precision** (beta.11): during the `deferred` charge phase the progress bar is suppressed, leaving only the `target` row. That row previously rendered `Math.round(current) → target`, hiding the two-decimal interpolated value. Now uses two-decimal rendering matching the progress bar once the interpolated value drifts from the last confirmed integer.
- **Info log and init debug log entity IDs** (beta.7 regression, beta.9 fix): removing `_attr_name` left these sensors relying on translation-driven naming, but the translation keys (`info_log`, `init_debug_log`) were only present in `strings.json` — not `translations/en.json` — so HA fell back to device-name and registered them as `sensor.foxess_2` / `sensor.foxess_3`. Added the missing translation entries plus a regression test that asserts every log sensor with `_attr_has_entity_name = True` has either an explicit `_attr_name` or a resolvable translation key.
- **Overview card crash on corrupted box entries**: card threw an uncaught `TypeError` when `_boxes` contained entries with unexpected shape (e.g. `{flow_from: [...]}` from energy-dashboard config patterns or corrupted state). `_renderBox()` now skips null/undefined/typeless entries, and `_render()` catches exceptions with a graceful error fallback UI.
- **Poll timer not reset on deferred session creation**: creating a deferred charge or discharge session didn't trigger a coordinator refresh, leaving the next poll up to 300 s away. Now calls `async_request_refresh()` immediately so the UI updates within seconds.
- **Session context missing from debug log records** (beta.8): `SessionContextFilter` was installed on the `custom_components.foxess_control` logger, but Python's logging module does not run parent-logger filters on records emitted from child loggers (e.g. `smart_battery.listeners`). Beta.7's new structured events landed in the debug-log sensors with `session: None`. Fixed by attaching the filter at the handler level so every record seen by the debug-log handlers is enriched with the current session context regardless of emitting logger.
- **Division-by-zero guards** in `calculate_deferred_start`, `is_charge_target_reachable`, and `calculate_discharge_deferred_start` for edge cases where effective charge/export power or the headroom denominator is zero.
- **Production `assert` statements replaced with `RuntimeError`** (10 occurrences across `listeners.py`, `config_flow.py`, `services.py`), preventing silent `AssertionError` in optimised builds.
- **Narrowed exception handling in `config_flow.py`**: three `except Exception` catches narrowed to `(OSError, requests.RequestException)` or `(FoxESSApiError, requests.RequestException, OSError)` so unexpected errors are no longer masked.
- **Cross-field config validation**: discharge now rejects start when current SoC is at or below min SoC, with a descriptive `ServiceValidationError`.
- **REST poll cadence preserved during WS injections**: WebSocket-driven data updates no longer reset the coordinator's REST poll timer, so scheduled polls fire on their intended cadence regardless of WS activity.

### Improved
- **Flaky Test Detection workflow triggers on tag push**: the workflow previously ran on `workflow_run` of whatever pushed to `main`, so nightly detection repeatedly exercised stale commits from before the session. Switching to `push: tags: [v*]` makes the workflow run against each released prerelease.
- **E2E page fixture tolerates navigation churn and slow containers**: the `page` fixture's wait for `ha-panel-lovelace` was failing on GH-hosted runners because either (a) HA's frontend navigations destroyed the JS execution context mid-poll (beta.12: deadline-bounded retry on `Execution context was destroyed` / `navigating` only), or (b) the panel legitimately took longer than the 30 s monolithic cap to boot (beta.13: split `_wait_for_lovelace_panel` into three staged waits — `home-assistant` → `home-assistant-main` → `ha-panel-lovelace` — each retrying on context destruction with `min(remaining_budget, 30 s)` per stage, total budget 75 s).
- **Soak test container name collision**: concurrent soak runs (nightly timer firing while a manual run is still active) now use PID-prefixed container names for isolation.

### Documentation
- **Priority hierarchy (`P-001`…`P-007`) across the knowledge tree** (beta.14): `docs/knowledge/01-vision.md` now has an ordered `## Priorities` section (no grid import > min SoC > energy target > feed-in > operational transparency > brand portability > engineering process integrity). Every C-NNN cites the P-NNN it enforces; every D-NNN cites the P-NNN it serves, any lower-priority goal it trades against, and a safety/pacing/other classification. `05-coverage.md` gained a priority-enforcement matrix and trade-off audit that made one pre-existing priority inversion visible (D-005 corrected). `CLAUDE.md` surfaces the priority list as a top-level section.
- **D-047 Hardware export-limit actuator** documented with the two-channel control scheme (cloud schedule at max; actuator modulated each tick with threshold-gated writes, clamped to C-001 floor). `03-architecture.md` InverterAdapter section documents `set_export_limit_w` / `get_export_limit_w`; structural repair of a prior edit that had inserted Soak Test Infrastructure mid-External-Dependencies table; BMS-temperature endpoint reference corrected to `/dew/v0/device/detail`.
- **README**: new **Grid Export Limit** option row, **Max Grid Export Limit Entity** mapping, `sensor.foxess_discharge_export_limit`, `charge_time_slack_s` / `discharge_time_slack_s` attributes, expanded `charge_phase` / `discharge_phase` enum, charge re-deferral paragraph, and unreachable-target Repair blurb.

## 1.0.10

### Added
- **Grid export limit configuration**: new integration option (default 5 kW) for the net export cap set on the inverter. When configured, discharge deferral accounts for the capped export rate, and discharge power always uses maximum inverter power (firmware handles export capping). Set to 0 for legacy power-pacing behaviour.

### Fixed
- **Card deferred countdown wrong for charge and discharge**: the sensor-side deferred start estimate used a simplified formula without headroom, taper profile, net consumption, or BMS temperature. The listener used the full algorithm with all parameters, causing the card to show wrong countdown times and wrong phase labels ("Charge Scheduled" when charging had started, "0m" or "39m" instead of ~24m for discharge deferral). Both `is_effectively_charging()`, `estimate_charge_remaining()`, and `estimate_discharge_remaining()` now call the same algorithm with the same parameters as their respective listeners (C-020).
- **Overview card crash on corrupted box entries**: card threw an uncaught `TypeError` when the internal `_boxes` array contained entries with unexpected shape (e.g. `{flow_from: [...]}` from energy-dashboard config patterns or corrupted state). `_renderBox()` now skips null/undefined/typeless entries, and `_render()` catches exceptions with a graceful error fallback UI.
- **Poll timer not reset on deferred session creation**: creating a deferred charge or discharge session didn't trigger a coordinator refresh, leaving the next poll up to 300s away. Now calls `async_request_refresh()` immediately so the UI updates within seconds.
- **Discharge power unnecessarily paced when export-limited**: when a grid export limit is configured, power pacing reduced discharge power below the inverter maximum even though the firmware already caps grid export. Now uses maximum power and relies on deferral timing for energy management.

## 1.0.9

### Fixed
- **Feedin-limited discharge started immediately instead of deferring**: large batteries with small feedin limits (e.g. 42 kWh battery, 1 kWh feedin, 51 min window) started forced discharge immediately at low paced power (~1.5 kW) for the entire window, creating sustained grid import risk. Now defers until the feedin deadline and discharges at full power, maximising headroom above household load (D-005, C-001).
- **Discharge session lost after HA restart during deferred phase**: when HA restarted before a scheduled discharge window opened, session recovery looked for a ForceDischarge schedule on the inverter. Since the schedule isn't written until the window opens, recovery found nothing and discarded the valid session. Now correctly re-creates the session in deferred state, matching charge recovery behaviour (C-024, D-002).
- **WebSocket connected before discharge window opened**: calling `smart_discharge` (or `smart_charge`) before the window start time caused WebSocket to connect immediately during the "scheduled" phase. WS now waits until the window actually opens. Also fixed the same issue for charge sessions in `smart_sessions` mode.
- **Discharge deferred countdown not shown on card**: the badge next to "Discharge Deferred" showed a bare duration (e.g. "2h 15m") without indicating it was a countdown to discharge start. Now shows "discharges in 2h 15m" (localised in all 10 languages).

## 1.0.8

### Added
- **Entity-mode dashboard support**: four new optional entity mappings — battery charge power, battery discharge power, grid consumption power, and grid feed-in power — populate the overview card's grid and battery sections in entity/modbus mode.
- **Automatic unit conversion in entity mode**: the entity coordinator reads `unit_of_measurement` from each source entity and converts to the expected coordinator unit (e.g. W→kW, Wh→kWh) using HA's built-in `PowerConverter`, `EnergyConverter`, and `TemperatureConverter`.

### Changed
- **Force operations unified with smart sessions**: `force_charge` and `force_discharge` now create smart sessions internally with a `full_power` flag, gaining circuit breaker protection, restart recovery, UI state, and sensor visibility. The `power` parameter has been removed — force operations always charge/discharge at maximum inverter power.

### Fixed
- **Grid direction swap with external generation** (issue #3): installations with additional solar inverters not visible to FoxESS could show grid consumption and feed-in swapped. Now falls back to `gridStatus` when the power balance diverges >3× from the actual grid reading.
- **Discharge deferred start with feedin target**: the feedin energy cap caused incorrect deferred start timing in two scenarios — starting too early (full SoC energy used instead of feedin drain time) and staying deferred too long in tight windows (feedin cap over-deferring when the SoC deadline already exceeded the window).
- **Force operation premature WebSocket connection**: force ops opened the WS connection at service call time, before the schedule was applied. Now starts through the smart session listener.
- **`clear_overrides` 30s timeout**: WS linger dispatched as background task to avoid blocking the service call.

### Improved
- **WebSocket plausibility filter**: anomalous WS messages (power values diverging >10× from the last accepted value) now filtered at the WS layer rather than the coordinator. Keeps data-source-specific logic out of the brand-agnostic coordinator.
- **Architectural lint enforcement**: semgrep rules and pre-commit hooks enforce module size budget (2000 lines), typed config access (`IntegrationConfig`), typed domain data access (`_dd(hass)`), and brand-import boundaries.
- **E2E timing-based worker balancing**: greedy bin-packing distributes tests across CI workers by estimated time, and new pushes cancel in-progress E2E workflows.

## 1.0.8-beta.7

### Improved
- **WebSocket mapping diagnostics**: the WS debug log now includes the raw `node` dict alongside the derived mapped data, so correctness of the field mapping can be verified from logs alone.

## 1.0.8-beta.6

### Fixed
- **WebSocket power value jumping**: the coordinator now drops anomalous WebSocket messages (typically `gridStatus=3`) where battery power diverges >10x from the current value, instead of just logging a warning and applying the bad data. Prevents `sensor.foxess_discharge_rate` from jumping between ~5.5kW and ~0.5kW every few seconds.

### Added
- **Entity-mode Modbus diagnostics**: one-shot logging for entity-mode (Modbus) users. Logs the full entity mapping at startup, then INFO on first successful read/write per entity and WARNING on first failure. All messages surface in the debug log sensor for remote troubleshooting without SSH.

### Improved
- **Flaky test detection**: 20 runs with random half-selection per run — each test averages ~10 runs alongside varying combinations, improving cross-test interaction coverage. Removed unit-test job (deterministic tests don't need flake detection).

## 1.0.8-beta.5

### Added
- **Temperature-aware taper profiles**: the adaptive BMS taper model now learns temperature-dependent charge/discharge curtailment independently of SoC effects. Uses a multiplicative decomposition (`effective_ratio = soc_ratio × temp_factor`) with integer-°C-indexed bins, EMA-smoothed. A 10-minute stability gate filters transient power reductions. Gracefully degrades to SoC-only profiling when BMS cell temperature is unavailable.
- **Simulator cold-temperature taper**: the inverter simulator now models BMS cold-temperature charge curtailment (linear 1.0→0.5 from 15°C→0°C), enabling temperature-aware taper learning in tests.

### Removed
- **Cold-temperature charge clamp** (`_apply_cold_temp_limit`): the binary 16°C step function that pre-capped requested power has been removed. The BMS enforces its own limits; pre-capping prevented the taper model from observing real curtailment and made learned data one-directional.

## 1.0.8-beta.4

### Improved
- **Typed domain data migration**: replaced untyped `hass.data[DOMAIN]` dict with `FoxESSControlData` dataclass and frozen `IntegrationConfig`, eliminating runtime key errors and enabling IDE support across all accessors.
- **Service handler extraction**: moved all service handlers from `__init__.py` into `_services.py`, reducing `__init__.py` scope and improving maintainability.
- **Circuit breaker extraction**: shared `_with_circuit_breaker` function replaces duplicated charge/discharge circuit breaker logic.
- **Discharge listener decomposition**: 395-line `_check_discharge_soc_inner` split into 7 focused closures (deferred start, feed-in limit, SoC unavailable, suspend/resume, power pacing, SoC threshold) with a slim orchestrator.
- **Helper deduplication**: extracted shared helpers, deduplicated types, cleaned up dead code across the codebase.

### Simulator
- **Battery efficiency factor**: configurable round-trip efficiency (charge stores less, discharge draws more from internal capacity).
- **MD5 signature validation**: optional FoxESS Open API signature checking for testing auth flows.
- **Per-endpoint rate limiting**: configurable autonomous rate limiting with FoxESS-style 41807 error responses.
- **null_schedule fault injection**: simulates real API returning `{"result": null}` when no scheduler is configured.
- **fdSoc enforcement**: simulator validates fdSoc >= 11 and minSocOnGrid <= fdSoc, matching real API constraints.

### Fixed
- **E2E race: overview card click test**: `test_node_click_opens_more_info` and `test_sub_link_click_opens_more_info` used immediate `page.evaluate()` instead of `wait_for_function()`, causing "Overview card not found" flakes under parallel CI load.

## 1.0.8-beta.3

### Added
- **Taper-aware forecast curve**: the forecast card now uses the adaptive BMS taper profile to vary the SoC rate per bucket, producing a realistic curve that bends at high SoC (charge) or low SoC (discharge) instead of a misleading straight line. Edge extrapolation applies the nearest recorded ratio beyond observed data.
- **Configurable BMS polling interval**: BMS cell temperature fetch frequency is now adjustable (60–3600s, default 300s) in the integration options, replacing the hardcoded 5-minute interval.
- **Control card show_cancel option**: cancel button visibility is now configurable via the card editor.
- **README gallery screenshots**: generated from E2E simulator for reproducible, up-to-date documentation images.

### Fixed
- **BMS temperature freezes after HA restart**: the battery compound ID (needed for BMS temperature fetch) was only held in memory and lost on restart. The one-shot discovery task had no retry, so the sensor froze at its last value. Now retries discovery with 300s backoff on every poll cycle until the ID is recovered.
- **Control card form inputs reset during typing**: state updates triggered full card re-renders, clearing user input mid-edit. Now preserves form DOM and restores values post-render using real-time `input` event listeners.
- **E2E `_robust_reload` navigation race**: retry `page.goto()` could overlap with the browser's navigation teardown, causing "interrupted by another navigation" flakes. Added `wait_for_load_state("load")` between failed navigation and retry.

## 1.0.7

### Added
- **Overview card box customisation**: show/hide, reorder, relabel, and re-icon boxes (solar, house, grid, battery) via the card editor or YAML `boxes` config. Responsive grid adapts layout for 1, 3, or 4 visible boxes.
- **Overview card click-to-history**: tapping any energy flow node opens the HA entity history dialog. Sub-details (cell temperature, PV strings, grid voltage/frequency, residual energy) are individually clickable for granular history access.
- **Cold-temperature BMS charge curtailment**: when BMS battery temperature is below 16°C, the maximum charge power is capped at 80A × live battery voltage (~4kW at 50V). The BMS physically limits charge current — the system now anticipates this to avoid over-requesting. Exposed via `charge_effective_max_power_w` sensor attribute.
- **BMS temperature on overview card**: shows "Cell 15.5°C · Inv 25.3°C" in the battery node, clearly distinguishing BMS cell temperature from the inverter sensor temperature.
- **Two-tier circuit breaker** (C-024): 3 consecutive adapter errors open the circuit breaker and hold position; 5 more ticks without recovery abort the session to self-use.
- **Automatic session replay after outage**: when the circuit breaker aborts a session and the time window is still open, the integration probes the API every 5 minutes and restarts on recovery (up to 6 attempts).
- **Proactive schedule conflict detection**: periodic check warns about unmanaged schedule modes (e.g. Backup) before they block a session start.
- **Forecast chart card** (`foxess-forecast-card`): SVG-based card showing projected SoC trajectory with actual history overlay, target/min SoC markers.
- **Session history timeline card** (`foxess-history-card`): 24h horizontal timeline with coloured session bars and SoC trace overlay.
- **Action buttons on control card**: cancel (with double-tap confirmation), charge, and discharge buttons with inline parameter forms.
- **Visual card editors**: both control and overview cards support HA's visual card editor.
- **Init debug log sensor** (`sensor.foxess_init_debug_log`): non-wrapping buffer preserving the first 75 log messages after startup, complementing the rolling debug log.
- **INFO-level rolling log sensor** (`sensor.foxess_info_log`): captures only INFO+ messages in a rolling buffer of 75 entries, retaining operational context much longer than the DEBUG log.
- **BMS battery temperature sensor** (`sensor.foxess_bms_battery_temperature`): exposes the min cell temperature from the BMS via the FoxESS web portal API.
- **Troubleshooting guide**, **Contributing guide**, and **FAQ** documentation.
- **Performance regression gate**: CI job testing algorithm calculation time and AST scan for sync I/O in async functions.

### Fixed
- **BMS temperature reliability**: resolved multiple issues preventing BMS temperature from working — correct endpoint discovery (`GET /dew/v0/device/detail`), compound battery ID persistence, web session token handling, fetch timing during WebSocket operation, and value preservation on transient server failures.
- **Discharge power sensor oscillates**: `get_actual_discharge_power_w` now correctly returns 0 when the battery isn't discharging (solar > load) instead of falling back to the target power.
- **Discharge target_power_w missing until first tick**: attribute now set at session creation and deferred start.
- **Control card form inputs reset during typing**: state updates triggered full card re-renders, clearing user input. Now uses real-time `input` event listeners with post-render DOM restore.
- **Discharge circuit breaker unreliable**: pacing loop now calls the adapter every tick, matching the charge path, so the circuit breaker can detect steady-state failures.
- **Log sensor entities unnamed**: added explicit `_attr_name` so entities register with correct names.
- **Blocking I/O on event loop**: WASM bytes pre-read at module import time; Module constructed from memory.
- **Web session token expiry**: `async_get` and `async_post` detect auth errors (errno 41808/41809) and retry once after re-authenticating.

### Improved
- **HA Integration Quality Scale**: Bronze 18/18, Silver 10/10, Gold 19/21, Platinum 3/3. Includes `ConfigEntryNotReady`, repair issues, diagnostics, entity categories, display precision, reauthentication, `icons.json`, `entry.runtime_data`, and `py.typed`.
- **Typed runtime data**: `entry.runtime_data` stores `FoxESSEntryData` instead of untyped dict. `FoxESSControlData` bridge layer preserves backward compatibility.
- **HA-managed aiohttp session**: web portal operations use HA's shared HTTP session for proper SSL, proxy, and lifecycle management.
- **E2E test infrastructure**: deterministic waits replace all sleeps, robust page reload, expanded cloud/entity parametrization, fault recovery tests, 796 total tests (670 unit + 126 E2E).
- **Migration guide rewritten**: recommends clean install with guidance for cleaning up orphaned entities.

## 1.0.6-beta.2

### Added
- **Graceful setup retry on cloud outage**: raises `ConfigEntryNotReady` when the FoxESS Cloud is unreachable during startup, so HA retries with exponential backoff instead of failing permanently.
- **PARALLEL_UPDATES = 0**: sensor and binary_sensor platforms declare no parallel updates since all data comes from the coordinator.
- **Repair issues for actionable errors**: unmanaged work mode (C-018) and session aborts now surface in HA's Repairs panel instead of just logs. Issues auto-clear when the problem is resolved or a new session starts.
- **Unrecorded attributes on high-churn sensors**: `SmartOperationsOverviewSensor`, `OverrideStatusSensor`, and `BatteryForecastSensor` mark frequently-changing attributes as unrecorded to prevent database bloat.
- **Clean removal**: `async_remove_entry` deletes the session Store file when the integration is removed entirely, preventing stale data if re-added later.
- **Diagnostics platform**: "Download Diagnostics" button in the integration page exports coordinator data, session state, WebSocket status, taper profile, and config — with API keys and credentials redacted.
- **Entity categories**: diagnostic-only sensors (temperatures, voltages, currents, grid frequency, EPS, throughput) marked as `DIAGNOSTIC` so they don't clutter default dashboards.
- **Disabled by default**: rarely-used sensors (PV1/PV2, battery voltage/current, ambient/inverter temp, grid current/frequency, EPS, throughput) disabled by default — users can enable them as needed.
- **Display precision**: all polled sensors set `suggested_display_precision` (0 for SoC, 2 for kW/kWh, 1 for °C/V/A/Hz) for clean dashboard values.
- **Enriched DeviceInfo**: device page shows inverter model name (from API device detail) and links to FoxESS Cloud portal.
- **Reauthentication flow**: when the FoxESS API key expires or becomes invalid, HA shows a "Reconfigure" prompt instead of silently failing. Users can enter a new key without removing and re-adding the integration.
- **Service action error handling**: API errors and network failures in service calls (force charge, smart discharge, etc.) now surface as user-friendly HA error toasts instead of generic "An error occurred".
- **icons.json**: all sensors, binary sensors, and service actions have Material Design Icons defined via `icons.json`. Smart Operations sensor uses state-aware icons (charging, discharging, deferred, etc.).

## 1.0.6-beta.1

### Fixed
- **Session recovery fails when schedule uses horizon end time**: after HA restart, `_has_matching_schedule_group` compared the session window end (e.g. 20:01) against the inverter schedule's safe horizon end (e.g. 19:24, set by C-027). Mismatch caused the session to be discarded while the inverter continued discharging. Now matches on work mode only, since any active ForceDischarge/ForceCharge group confirms the session is still live.
- **Debug log sensor exceeds recorder attribute limit**: reduced buffer from 200 to 75 entries and marked `entries` attribute as `_unrecorded_attributes` so the recorder doesn't attempt to persist the large debug payload.

## 1.0.5

### Added
- **WebSocket mode selector** (`ws_mode`): replaced the boolean `ws_all_sessions` toggle with a 3-state dropdown — **Auto** (WS only during paced forced discharge), **All smart sessions** (any smart session or force op), **Always connected** (WS preferred at all times with watchdog recovery). Existing configurations migrate automatically.
- **Data freshness sensor**: `sensor.foxess_data_freshness` exposes the current data source (`ws`, `api`, or `modbus`) as its state, with `last_update` and `age_seconds` attributes for staleness detection.
- **Data staleness indicator on Lovelace cards**: both cards compute data age client-side; badge turns red with elapsed time (e.g. "API · 2m") when data exceeds 30 seconds old.
- **Structured session logging**: session context (ID, type, SoC, power) enriched via `logging.Filter`; debug log sensor exposes structured data for E2E tests and power users.
- **Target power display**: Lovelace card shows current vs target discharge rate when they differ during feed-in pacing.
- **Entity-mode E2E tests**: input helpers simulate modbus entities; `connection_mode` fixture parametrizes cloud vs entity modes with function-scoped containers for full isolation.
- **Reconfigure flow**: add or update web portal credentials without re-creating the config entry. Accepts both raw password and pre-computed MD5 hash.
- **Structural tests**: AST-based verification of synchronous cancel functions (C-016) and brand import boundary (C-021).

### Changed
- **SoC display precision matches confidence**: Lovelace card shows integer SoC until the first confirmed integer change (e.g. 93→92), then switches to 2 decimal places. Before the first change, interpolation is just an estimate; after, the real SoC is known to be near X.5, making interpolation meaningful.
- **Interpolated SoC stored at full float precision**: rounding applied only for change detection (2dp gate to prevent entity update storms), not storage.
- **Session construction via factory functions**: `create_charge_session()` and `create_discharge_session()` ensure consistent field defaults and reduce duplication across callers.
- **Min SoC floor lowered to 0%**: `min_soc` and `min_soc_on_grid` now accept 0, removing the previous 5% floor.
- **Unified cancel functions**: `_cancel_smart_charge` and `_cancel_smart_discharge` replaced with delegates to brand-agnostic `cancel_smart_session`, ensuring the `_on_session_cancel` hook fires from all cancel paths.
- **Simulator fidelity**: charge taper above 90% SoC, discharge taper below 15% SoC, per-app state isolation, stale-stream behaviour matching FoxESS cloud.
- **E2E config uses production defaults**: tests needing non-default options set them explicitly via the options flow, matching real user setup.
- **GitHub Actions updated**: checkout v4→v6, setup-python v5→v6, upload-artifact v4→v7, download-artifact v4→v8.

### Fixed
- **WS linger race captured stale forced-discharge data (D-009)**: `_on_session_cancel` now returns the WS stop coroutine; callers await it after override removal completes, so the linger captures post-session self-use data. Also fixed the `clear_overrides` service path which used fire-and-forget.
- **Entity-mode service domain detection**: `apply_mode()` used hardcoded `"select"` and `"number"` domains, breaking `input_select`/`input_number` entities from foxess_modbus. Added `_entity_service_domain()` helper to derive the correct domain from the entity ID prefix.
- **Charge fdSoc regression**: listener must pass `fd_soc=100` to prevent FoxESS API validation failure (C-008).
- **Smart discharge starting before scheduled window**: deferred discharge listener omitted the `start=` parameter, bypassing the floor clamp. The inverter received `fdPwr=0` but ignored it and discharged at full power.
- **Force operations not cancelling opposite smart session**: both force operations now cancel both session types, preventing leftover listeners from fighting the schedule.
- **WS not connecting after deferred discharge start**: timer now fires the WS-aware wrapper, so WebSocket connects as soon as forced discharge begins.
- **WS reconnect during smart charge**: charge listener wrapper wasn't triggering `_maybe_start_realtime_ws`.
- **Session sensors delayed by ~30s**: now subscribe via `coordinator.async_add_listener` for instant state propagation.
- **Stale work mode badge after failed cleanup**: override removal retries on each successful REST poll; cancel paths clear `_work_mode` immediately.
- **Smart sessions survive transient API errors**: errors retried on next timer tick; only 3 consecutive failures trigger abort. Previously any transient cloud outage killed a multi-hour session.
- **SoC interpolation overshooting entity value**: clamp tightened to `[tick − 0.5, tick + 0.44]` so the rounded display always matches the entity.
- **REST poll starvation from SoC extrapolation**: now updates entity data directly without resetting the poll timer.
- **Feed-in pacing stuck at initial power**: switches to self-use when target is below `min_power_change` threshold, enabling ramp-up on the next tick.
- **Feed-in energy inflated at session start**: baseline deferred to the listener's first tick when fresh data is available.
- **Schedule horizon not set on immediate discharge start**: computed inline before state dict creation.
- **WS data_source badge stuck during stale stream**: coordinator notified immediately when reconnecting, so badge shows "API" until fresh WS data resumes.
- **WS stream-stolen recovery**: tracks last useful data timestamp; reconnects after 30s of only stale frames. Token invalidated on handshake error, forcing fresh login.
- **SoC interpolation stuck between ticks**: timestamp set after integration, made unconditional.
- **SoC clamp rounding**: upper bound changed to 0.94 to prevent displayed value exceeding authoritative tick.
- **Progress bar start SoC wrong after deferral**: updated to actual SoC when session begins.

## 1.0.4

### Added
- **Progressive schedule extension**: discharge schedule end time is set to a dynamically computed safe horizon based on current SoC, discharge rate, and safety factor (1.5×). If HA loses connectivity, the inverter's schedule expires and reverts to self-use — battery protected without HA intervention. Horizon shown on Lovelace card time progress bar as a vertical marker.
- **SoC interpolation in REST-only mode**: coordinator integrates battery power between REST polls for sub-percent SoC estimates, eliminating staircase progress bars when WebSocket is not active.
- **FoxESS simulator** (`simulator/`): standalone aiohttp server with REST API, WebSocket, web auth, and backchannel endpoints for testing. Supports fault injection, fast-forward, and fuzzing (±2% jitter). Unit tests migrated from mock library to simulator.
- **Containerised E2E tests** (`tests/e2e/`): real HA instance in Podman container with Playwright browser automation. 20 tests covering card rendering, discharge/charge lifecycle, PV consistency, data source badge (API and WS modes), schedule horizon marker, and screenshot regression. Runs in ~70s with 10 parallel workers.
- **`_on_session_cancel` hook**: WebSocket stops through all cancel paths (timer, SoC abort, exception, clear_overrides, target reached), and work mode clears immediately for the Lovelace card.

### Changed
- **Stale REST values hidden in WS mode**: overview card suppresses PV1/PV2 detail, grid voltage/frequency, battery temperature, and residual energy when WebSocket is the active data source — these values only update on REST polls and would be misleadingly stale.

### Fixed
- **Work mode label stuck after session ends**: overview card showed "Force Discharge" for minutes after the window finished. Now cleared immediately via `_on_session_cancel`.
- **WS not stopping when session ends via timer**: the brand-agnostic `cancel_smart_session` didn't trigger WebSocket shutdown.
- **WASM signature test ordering dependency**: module singleton heap state caused non-deterministic output across test runs.

## 1.0.3

### Added
- **SoC interpolation between integer ticks**: the coordinator integrates battery power over time to maintain a sub-percent SoC estimate for display. Progress bars and forecasts update smoothly between the ~6-minute integer SoC ticks instead of appearing stuck. Resyncs to the authoritative value on each tick change and REST poll. Algorithm decisions continue to use raw integer SoC.
- **Two-zone SoC progress bar**: solid fill for the inverter-confirmed SoC + semi-transparent extension for the power-integrated estimate. When the next SoC tick arrives, the solid fill catches up to the projected zone.

### Fixed
- **WebSocket not connecting during discharge**: the adapter extraction removed inline `_maybe_start_realtime_ws` calls from the listener code. The discharge callback is now wrapped to trigger WS lifecycle after each check.
- **WebSocket not connecting during smart charge**: the charge listener wrapper didn't trigger `_maybe_start_realtime_ws` when `ws_all_sessions` was enabled.
- **Persistent notifications lost on schedule conflict**: restored `pn_create` for unmanaged work mode detection by pre-checking schedule safety from the async context in the cloud adapter.

## 1.0.1

### Added
- **WebSocket real-time data**: optional ~5-second power data from the FoxESS Cloud WebSocket during smart sessions, reducing grid import risk from load spikes between 5-minute REST polls. Requires web portal credentials (optional config flow step). Connects automatically during paced discharge; `ws_all_sessions` toggle extends to all sessions.
- **Data source indicator on Lovelace cards**: badge shows "WS", "API", or "Modbus" when multiple data sources are configured, so users know which source is driving displayed values. Immediately switches on WS connect/disconnect.
- **Deferred self-use for smart discharge**: stays in self-use mode until a deadline calculation requires forced discharge, preventing grid import from low paced power
- **Peak consumption tracking**: exponential decay (~4.3 min half-life at 1-min ticks), floors discharge power at peak × 1.5 to absorb inter-poll load spikes
- **End-of-discharge guard**: suspends forced discharge ~10 min before window end when paced power would drop below house load
- **Discharge SoC unavailability abort**: discharge sessions now abort after 3 consecutive SoC-unavailable checks, matching charge path behaviour
- **Safe state on failure**: listener callbacks catch unexpected exceptions, cancel the session, and revert to self-use
- **Unreachable charge target detection**: `charge_target_reachable` sensor attribute warns when even max power can't reach the target SoC in remaining time
- **Proactive error surfacing**: session errors surfaced via sensor attributes (`has_error`, `last_error`, `last_error_at`, `error_count`) instead of log-only
- **Reconfigure flow**: add or update web portal credentials without re-creating the config entry. Accepts both raw password and pre-computed MD5 hash.
- **Feed-in energy integration from WebSocket**: trapezoidal integration between REST polls for more accurate cumulative energy tracking
- **Feed-in early-stop**: schedules a one-shot stop based on observed export rate to prevent overshooting the feed-in energy limit

### Changed
- **Session orchestration via adapter pattern**: `__init__.py` reduced from 3056 to ~2030 lines (-34%). FoxESS-specific schedule merging encapsulated in `FoxESSCloudAdapter` and `FoxESSEntityAdapter`, delegating to brand-agnostic `smart_battery/listeners.py`.
- **WebSocket per-field unit handling**: respects the `unit` property on each power field (some sent as kW, others as W within the same message) instead of assuming uniform units
- **REST fallback on poll failure**: coordinator keeps last-known data when REST fails, preventing all entities flashing unavailable

### Fixed
- **Taper-path consumption bypass**: deferred start taper paths now account for household consumption, matching the linear path
- **WebSocket stale data filter**: messages with `timeDiff > 30` seconds are discarded
- **WebSocket grid direction**: power-balance-derived direction replaces unreliable `gridStatus` field
- **Taper profile corruption**: minimum actual power guard (50W) and plausibility check auto-reset corrupted profiles
- **Progress bars during inactive phases**: hidden during charge "deferred" and discharge "scheduled" phases
- **House load greyed out at low values**: overview card always shows actual value at full opacity
- **Reconfigure password whitespace**: `ensure_password_hash` strips trailing newlines from pasted hashes
- **Session recovery `start_soc`**: persisted for accurate progress bars after HA restart

## 1.0.0

### Added
- **Adaptive BMS taper model**: learns actual charge/discharge acceptance at each SoC level via exponential moving average, improving time estimates and power pacing at high/low SoC where BMS limits throughput
- **Full i18n support** (10 languages): English, German, French, Dutch, Spanish, Italian, Polish, Portuguese, Simplified Chinese, Japanese — covering entity names, service descriptions, config UI, Lovelace card labels, durations, and status text
- **Overview Lovelace card** (`custom:foxess-overview-card`): live 2×2 energy flow display with solar, battery, grid, and house nodes, auto-discovered via WebSocket entity map
- **EXAMPLES.md**: quick-start guide with copy-pasteable automations and dashboard setup
- Forecast chart: actual SoC history overlay, locked to configured interval, dashed line for past data
- Discharge progress bar shows energy schedule disparity when feed-in limit is set
- Entity names use HA `translation_key` for native localisation instead of hardcoded English strings
- CI: release gate on lint/test/hassfest/hacs, pre-push hook blocks tags without passing CI

### Changed
- Smart charge/discharge service descriptions now accurately describe rate pacing and deferred start behaviour
- Extracted shared `smart_battery/` library (algorithms, sensors, listeners) for multi-brand reuse
- GoodWe Battery Control moved to its own repository ([goodwe-control](https://github.com/nicois/goodwe-control))
- Refactored Lovelace card progress bar rendering

### Fixed
- SoC progress bar showing `?%` when `start_soc` is unavailable (sessions started before the field existed)
- Dark theme: progress bar tracks and time fills now use HA CSS variables instead of hardcoded rgba
- mypy compatibility between CI and pre-commit environments
