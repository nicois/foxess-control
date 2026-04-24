# Changelog

## 1.0.11-beta.14

### Fixed
- **Deferred-start feed-in headroom over-defers on export-limited sites** (surfaced by live session monitoring 2026-04-24): `calculate_discharge_deferred_start` applied the doubled feed-in headroom (`min(headroom × 2, 0.40) = 0.20`) unconditionally whenever a `feedin_energy_limit_kwh` was set. The doubled margin is justified when household load volatility can erode effective export — but when `grid_export_limit_w` is configured well below the inverter's max power, load must exceed `max_power_kw − grid_export_limit_kw` before net export degrades at all. On the example site (5 kW clamp, 10.5 kW inverter) that threshold is 5.5 kW — well above typical residential baseline — so the doubled buffer was protecting against a scenario the hardware made physically impossible, eating ~3 min of self-use time per session. Fixed by making the feed-in headroom conditional on whether `max(net_consumption_kw, consumption_peak_kw)` exceeds the clamp slack: when the clamp actively shields export rate from load volatility, single headroom (10%) is used; otherwise the doubled headroom still applies. Added six-test regression suite `TestFeedinHeadroomAccountsForExportClamp` covering the live-session parameters and neighbourhood cases (unlimited export, boundary `limit == max_power`, peak above/below slack, net-consumption variant, C-001 floor preservation). Traces C-001, C-037, D-044 (rationale updated).

### Documentation
- **Priority hierarchy introduced across the knowledge tree** (`docs/knowledge/`): added an ordered `## Priorities` section to `01-vision.md` with stable IDs P-001..P-007 (no grid import > min SoC > energy target > feed-in > operational transparency > brand portability > engineering process integrity). Every C-NNN now cites the P-NNN it enforces; every D-NNN cites the P-NNN it serves, any lower-priority goal it trades against, and a safety/pacing/other classification. This makes priority inversions visible at review time: the audit caught one pre-existing inversion (D-005 was claimed to serve P-004 while trading against P-003 — corrected to serve P-003 with no trade-off) and forced an honest re-examination of D-044's rationale. `05-coverage.md` gained a priority-enforcement matrix and trade-off audit table. `CLAUDE.md` surfaces the priority list as a top-level section.
- **D-047 Hardware export-limit actuator for discharge pacing**: documented the two-channel control scheme introduced in beta.11 (cloud schedule pinned at max; hardware export-limit actuator modulated each tick with threshold-gated writes, clamped to the C-001 safety floor). Traces to C-001, C-037 and 24 tests in `test_export_limit.py`. The `InverterAdapter` protocol section in `03-architecture.md` now documents `set_export_limit_w` / `get_export_limit_w`.
- **03-architecture.md structural repair**: a prior pass had inserted the Soak Test Infrastructure block mid-External-Dependencies table; the table is now intact and the soak section has its own heading. Also corrected a stale BMS-temperature endpoint reference (`/generic/v0/device/battery/info` → `/dew/v0/device/detail`).
- **06-tests.md and 05-coverage.md refreshed** to the authoritative `pytest --co -q` counts: 863 unit + 136 E2E + 19 soak = 1018 total.
- **CLAUDE.md surfaces the new C-037 (grid export limit awareness) and C-038 (sensor-listener parameter parity)** constraints, which had been added to `02-constraints.md` earlier but not yet propagated to the top-level entry file.

## 1.0.11-beta.13

### Fixed
- **E2E page fixture flake: stage Lovelace-panel wait on slow shards** (surfaced when the beta.12 trigger change made Flaky Test Detection actually run against each prerelease): beta.12's context-destruction retry was incomplete — it covered navigation-induced churn but not the "panel legitimately takes longer than 30s on a slow GH-runner" variant. Observed in run 24872997253 (gw2 shard 12, `test_time_input_survives_multiple_rerenders[cloud]`): setup spent 40.3s before the monolithic 30s `wait_for_function` cap fired, while another test on the same shard ran 90.9s — so the container was alive, just slow to boot. Fixed by splitting `_wait_for_lovelace_panel` into three staged waits with per-stage bounded timeouts: `home-assistant` attached → `home-assistant-main` inside its shadowRoot → `ha-panel-lovelace` inside main's shadowRoot. Each stage retries on context destruction (beta.12 retry semantics preserved), uses `min(remaining_budget, 30s)`, and logs a debug line on completion. Page fixture budget raised from 30s to 75s (justified by observed 90s test times on the same slow shard). Worst-case total remains bounded; stage names appear in logs so future flakes identify which DOM milestone stuck.

## 1.0.11-beta.12

### Fixed
- **E2E page fixture flake: retry on navigation-induced context destruction**: the nightly Flaky Test Detection workflow surfaced a ~29% failure rate on GH-hosted runners where the `page` fixture's `wait_for_function(...)` waiting for the `ha-panel-lovelace` shadow-DOM element intermittently timed out after 30s. Root cause: HA's frontend triggers navigations (WS reconnect, panel router rebuild, sidebar load) during the initial page boot; each navigation destroys the JS execution context mid-poll, and sustained navigation churn during the 30s budget exhausted Playwright's internal retry — producing a generic `TimeoutError` even though the panel would render a few seconds later. Extracted the wait into `_wait_for_lovelace_panel()` with a deadline-bounded retry loop that catches only `PlaywrightError` with "Execution context was destroyed" or "navigating" in the message, settles on `networkidle`, then retries with the remaining budget. Timeout stays at 30s (no timeout bump). Mirrors the prior `_find_card` fix (commit aa25b10). Traces C-031 (no flaky tests), C-029 (E2E).
- **REST poll cadence preserved during WS injections** (landed during the same work window): prevents WebSocket-driven data updates from resetting the coordinator's REST poll timer, so scheduled polls fire on their intended cadence regardless of WS activity.

### Improved
- **Flaky Test Detection workflow triggers on tag push**: the workflow previously ran on `workflow_run` of whatever pushed to `main`, so nightly detection repeatedly exercised stale commits from before the session. Switching to `push: tags: [v*]` makes the workflow run against each released prerelease instead.

### Testing
- **Discharge sensor countdown uses tracked peak consumption, not instant**: follow-up regression test covering the corner of the C-038 sensor-listener parameter-parity bug (fixed in `8e10b9a`) where tracked peak consumption differs substantially from the instantaneous load sample. The listener pessimistically sizes around peak to avoid grid import during spikes between polls, so a sensor that ignored `consumption_peak_kw` drifted from the listener's deferred start. Verified pre-fix: at `b8d1405` the sensor reports "defers 1h 33m" vs. the listener's "defers 1h 14m" — the exact mismatch `8e10b9a` fixed.

## 1.0.11-beta.11

### Improved
- **Deferred-phase charge card shows high-precision SoC** (user-reported 2026-04-24): during the `deferred` charge phase the progress bar is suppressed (no meaningful progress to display), so the only SoC visible on the card was the `target` row rendering `Math.round(current) → target`. This hid the two-decimal interpolated value that users rely on to see the battery climbing — a detail that matters especially when solar-only self-use is driving slow incremental progress. The target row now uses the same two-decimal rendering as the progress bar (`89.24% → 90%`) once the interpolated value has drifted from the last confirmed integer. Discharge was already showing the precise value via its own progress bar (which stays visible during deferred).

### Added
- **Export-limit actuator for smart discharge tapering**: when a `foxess_modbus` "Max Grid Export Limit" number entity is configured in the entity mapping, smart discharge now controls feed-in power by varying the inverter's hardware export cap instead of modulating the cloud schedule's `fdPwr`. The cloud schedule is pinned at hardware maximum; the export-limit entity is written each tick with the paced target, clamped to `[peak_consumption × 1.5, grid_export_limit]` — C-001's safety floor is preserved as the lower bound. Sub-threshold deltas are suppressed to avoid chatty modbus writes. The configured `grid_export_limit` acts as both the default (applied outside smart sessions) and the upper bound (smart discharge may only reduce it). Every session exit path (timer expire, circuit-breaker abort, SoC threshold, feed-in limit, manual cancel) reverts the entity to the configured maximum (C-024, C-025). Opt-in: when no export-limit entity is configured, behaviour is unchanged.
- **`SmartDischargeExportLimitSensor`** surfaces the current export-limit value with `{configured_max, modulated, entity}` attributes (C-020). The existing discharge overview sensor gains a `discharge_export_limit_w` attribute.
- **Simulator**: `max_grid_export_limit_w` field with curtailment physics (battery discharge backs off when the cap is hit; no dump-to-load) and a `number.set_value` backchannel for E2E tests.
- **Deferred-phase slack on the control card**: while a smart charge or discharge session is in the `deferred` phase (window open, pacing algorithm waiting to act), the card now shows a "slack" row giving the algorithm's internal countdown to forced action — the same `deferred_start − now` the listener recomputes each tick. Makes the threshold-crossing behaviour legible: solar surplus grows slack, load spikes shrink it. Surfaced via new `charge_time_slack_s` / `discharge_time_slack_s` sensor attributes (integer seconds, `None` outside the deferred phase). Translated into all 10 card languages.

## 1.0.11-beta.10

### Fixed
- **Spurious "unreachable charge target" repair issue caused by outlier taper observations** (user-reported live 2026-04-24): during a smart charge with plentiful solar surplus and ~1h 5m remaining for a 15% uplift on a 42 kWh battery, `is_charge_target_reachable` returned False, firing an HA Repair issue even though the BMS was empirically accepting ~10.2 kW (linear estimate: 40 min needed in 65 min window). The taper-integrated estimate summed several isolated outlier observations (bins 81:0.05 count=1, 83:0.41 count=3, 85:0.16 count=2, 90:0.21 count=7) surrounded by 0.87-1.0 neighbours, producing 1.04h of taper-weighted charge hours — pushed over 1.09h once the 10% headroom buffer was applied. Fixed by blending the taper-integrated estimate with a median-ratio linear estimate across the traversed SoC range and taking the minimum. The feasibility check (C-022) now returns False only when no plausible scenario reaches the target, not when a few outlier observations skew the integrated estimate. Uniformly low taper ratios (genuine unreachability) still correctly produce False because the median is also low. Added four regression tests in `TestIsChargeTargetReachable` covering the exact live inputs, a house-load variant, a short-window guard against over-fix, and a fresh-profile sanity check. Traces: C-022 (unreachable target surfaced), C-020 (no false alarms), D-028 (reachability check design).

## 1.0.11-beta.9

### Fixed
- **Info log and init debug log sensors registered under wrong entity IDs** (regression introduced in beta.7): removing `_attr_name` from `InfoLogSensor` and `InitDebugLogSensor` left them relying on translation-driven naming, but the corresponding translation keys (`info_log`, `init_debug_log`) were only present in `strings.json` — not in `translations/en.json`. HA therefore fell back to the device name and registered the sensors as `sensor.foxess_2` / `sensor.foxess_3` instead of `sensor.foxess_info_log` / `sensor.foxess_init_debug_log`, making them effectively invisible to users who searched for them by their expected names. Fixed by adding the missing translation entries to `translations/en.json`. Added a regression test that asserts every log sensor with `_attr_has_entity_name = True` has either an explicit `_attr_name` or a translation key present in `translations/en.json`.
- **Charge card conflated "Scheduled" (pre-window) with "Deferred" (within-window, waiting)** (user-reported live 2026-04-24): the discharge side already distinguished `scheduled`/`deferred`/`suspended`/`discharging` via `discharge_phase`, but the charge side only had two phases (`charging`/`deferred`), so the card displayed "Charge Scheduled" both before the window opened and during the window when the pacing algorithm had pushed forced charging later. Users saw "Charge Scheduled — starts in 1m" over an hour after the window had opened, unable to distinguish the scheduled/deferred states from the UI alone (C-020 violation). Additionally, `charge_remaining` displayed `"starts in 0m"` when the computed deferred start was within the current minute, because `format_duration` rounds sub-minute durations down to `0m`. Fixed by: (1) adding a `scheduled` value to `charge_phase` and to the overview sensor state (now `scheduled`/`deferred`/`charging`); (2) treating any sub-minute wait as "transition imminent" in `estimate_charge_remaining`, falling through to window-remaining display; (3) adding a `charge_deferred` translation key in all 10 card languages and wiring the three section-title labels in the card JS.

## 1.0.11-beta.8

### Fixed
- **Session context missing from debug log records** (pre-existing bug, uncovered during beta.7 live validation): `SessionContextFilter` was installed on the `custom_components.foxess_control` logger, but Python's logging module does not run parent-logger filters on records emitted from child loggers (e.g. `custom_components.foxess_control.smart_battery.listeners`). As a result, every record from the listeners — including all of beta.7's new structured events (`algo_decision`, `tick_snapshot`, `session_transition`, `schedule_write`, `taper_update`, `service_call`) — landed in the debug-log sensors with `session: None` instead of the active session context. Fixed by attaching the filter at the **handler** level in `setup_debug_log`, so every record seen by those handlers — regardless of its emitting logger — gets enriched with the current charge/discharge state. Added a regression test that exercises a child logger to prevent this from reappearing.

## 1.0.11-beta.7

### Added
- **Structured event emission for session replay** (`smart_battery/events.py`): algorithm decisions, tick snapshots, service calls, session transitions, schedule writes, and taper updates now emit structured records through the existing logging pipeline. Events carry normalised JSON-serialisable payloads (datetime, time, timedelta, and taper profile types round-trip cleanly), captured by the info/debug log sensors and surfaced via the HA REST API for out-of-process collection.
- **Replay harness** (`smart_battery/replay.py`): loads JSONL traces of recorded events, replays each `algo_decision` by re-invoking the named pure algorithm with its recorded inputs, and reports divergences when outputs do not match. The `_REPLAY_FUNCS` registry covers all six pacing algorithms (`calculate_discharge_power`, `calculate_charge_power`, `calculate_deferred_start`, `calculate_discharge_deferred_start`, `should_suspend_discharge`, `is_charge_target_reachable`).
- **Event collector script** (`scripts/collect_events.py`): external poller that reads the info-log sensor via the HA REST API, deduplicates events, and writes per-session JSONL files to `test-artifacts/traces/` — suitable for ad-hoc capture from live systems.
- **Nightly soak trace capture**: the soak test teardown now dumps accumulated events to `test-artifacts/soak/traces/` after each scenario, giving cross-run replay data alongside the existing `soak_results.db` inflection-point store.
- **Replay regression gate**: `tests/test_replay.py::test_committed_trace_replays_clean` parametrises over any JSONL file in `tests/replay_traces/` so hand-crafted or recorded traces become locked-in protection against algorithm output drift. A sample discharge trace is included.

### Improved
- **Info log and init debug log entity IDs** now follow the same naming pattern as the debug log sensor, resulting in stable `sensor.foxess_info_log` / `sensor.foxess_init_debug_log` entity IDs. The explicit `_attr_name` overrides were redundant with the translation-driven naming and caused inconsistent slugs. (Note: beta.7 shipped this change without the corresponding translation entries — see the beta.8 fix below.)

## 1.0.11-beta.6

### Added
- **Unreachable charge target repair issue (C-022)**: when the pacing algorithm detects the charge target cannot be reached in the remaining window, an HA Repair issue is created. Automatically dismissed when the target becomes reachable again, or when the session ends.
- **Named algorithm constants**: extracted magic numbers (`MIN_CHARGE_POWER_W`, `MIN_DISCHARGE_POWER_W`, `FEEDIN_FALLBACK_RATIO`, `MAX_FEEDIN_HEADROOM`) from inline literals in algorithms.py into `const.py`.
- **Soak tests**: `test_charge_extreme_taper` (92%→100% BMS taper region) and `test_charge_very_cold_battery` (8°C current-limiting).
- **E2E test**: `test_show_cancel_false_hides_button` verifies the cancel button is hidden when `show_cancel: false` is set.

### Fixed
- **Division-by-zero in deferred start**: zero-guards added to `calculate_deferred_start`, `is_charge_target_reachable`, and `calculate_discharge_deferred_start` for edge cases where effective charge/export power or headroom denominator is zero.
- **Production asserts replaced with RuntimeError**: 10 `assert` statements in listeners.py, config_flow.py, and services.py replaced with explicit `RuntimeError` raises or early returns, preventing silent `AssertionError` in optimised builds.
- **Narrowed exception handling in config_flow.py**: three `except Exception` catches narrowed to `(OSError, requests.RequestException)` or `(FoxESSApiError, requests.RequestException, OSError)` to avoid masking unexpected errors.
- **Cross-field config validation**: discharge now rejects start when current SoC is at or below min SoC, with a descriptive `ServiceValidationError`.
- **E2E event_stream fixture**: added `try/finally` to ensure WebSocket cleanup on test failure.

## 1.0.11-beta.5

### Added
- **Soak test suite**: real-time scenario simulations that run full charge/discharge sessions through the HA integration and simulator, verifying invariants (SoC overshoot, grid import during discharge, target reach). 17 scenarios covering basic charge/discharge, solar interaction, load spikes, BMS taper, cold battery, large battery, tight windows, and combined cycles.
- **Simulator auto-tick**: simulator now advances its model in real time (5s steps) via a background task, so SoC and power flows update without explicit fast-forward calls.
- **Nightly soak service uses latest tag**: the systemd soak timer now checks out the most recent git tag instead of HEAD, avoiding wasted runs against WIP code on develop.
- **SQLite inflection-point store**: soak test results are recorded in a `soak_results.db` with state transitions, SoC direction changes (2% deadband), and power step changes as discrete events, enabling cross-run comparison between tags.

### Fixed
- **Soak test container name collision**: concurrent soak runs (e.g. nightly timer firing while a manual run is still active) collided on container names. Now uses PID-prefixed names for isolation.

## 1.0.11-beta.2

### Added
- **Charge re-deferral when ahead of schedule (D-043)**: when solar generation supplements grid charging and pushes SoC ahead of the pacing trajectory, the charge listener now switches back to self-use and clears `charging_started`. Each subsequent tick re-evaluates `calculate_deferred_start()` and resumes forced charging only when the deadline requires it. Prevents reaching the target 30+ minutes early and wasting cheap-rate self-use time.

## 1.0.11-beta.1

### Fixed
- **Taper profile blind to BMS curtailment during paced charging**: `_record_taper_observation` used the paced power request (`last_power_w`) as the denominator instead of the inverter maximum (`max_power_w`). When pacing reduced the request below the BMS limit (e.g. 4552W paced vs 6380W actual at 81% SoC), the ratio exceeded 1.0, was clamped, and the profile recorded "no taper". Subsequent sessions had no taper data for high SoC, producing inaccurate time estimates and deferred start calculations. Now uses `max_power_w` so the profile correctly captures the BMS acceptance fraction (e.g. 6380/10500 = 0.607).

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
