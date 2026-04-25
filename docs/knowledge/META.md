---
project: FoxESS Control
created: 2026-04-14
last_updated: 2026-04-25
last_reflection: 2026-04-25T14:00:00+10:00
---
# Knowledge Tree Meta

## Discovery Notes

- Constraints were most reliably discovered from CHANGELOG.md "Fixed"
  entries — each fix implies a constraint that was previously violated.
- Algorithm docstrings in `smart_battery/algorithms.py` are the best
  source for pacing rationale (they explain the "why" well).
- `API_DEVIATIONS.md` is essential reading for FoxESS API constraints —
  the code comments reference it but don't duplicate the full context.
- The `tests/test_init.py` file conflates schedule merging, power
  calculation, and session management tests. Domain grouping required
  reading every test.

## Interview Notes

- **Vision**: Multi-brand only. Tariff optimisation, solar forecasting,
  and grid services are explicit non-goals — they belong in external HA
  automations.
- **Constraints**: Owner flagged the initial constraint list as needing
  correction but did not elaborate. The 16 constraints listed are
  preliminary and should be reviewed. Particular attention needed on
  whether "no grid import" is truly absolute P1.
- **Architecture**: `__init__.py` at 112K is mixed — some should be
  extracted (FoxESS session orchestration) but HA setup/service
  registration genuinely belongs there.
- **Design parameters**: Priorities are correct but specific values
  (1.5x safety factor, 0.3 EMA alpha, 0.85 decay) are empirical
  and open to refinement with more data.

## Structure Refinements

- The `04-design/` split by feature works well for this project.
  FoxESS API quirks warranted their own design doc despite being
  "just" API workarounds — the decisions are non-obvious.
- `06-tests.md` could benefit from automated generation (parsing
  pytest output) rather than manual curation.

## Reflection Log

### 2026-04-14 — Initial generation
- **What was hard**: Mapping tests to constraints. Many tests verify
  algorithm correctness without a clear "which invariant does this
  protect?" answer. The 93 sensor tests especially were hard to trace.
- **What was wrong**: Owner indicated constraints need correction but
  did not specify what. The constraint list is preliminary.
- **What could be better**: The interview could be more targeted —
  presenting constraints one at a time for yes/no would be faster than
  asking about all 26 at once. Also, showing the actual test-to-constraint
  mapping during the interview would help the owner spot missing traces.
  Additionally, the user provided some answers which were lost due to the
  way in which the questions were asked. Instead of asking a large battery
  of questions, perhaps a more iterative approach would avoid this, as well
  as allowing some questions to naturally lead to others, making the process
  more like a real interview or dialogue.

### 2026-04-14 — Reconciliation pass
- **What was wrong**:
  - C-002 conflated two mechanisms (pure function vs listener counter)
  - D-004 half-life wrong: ~4.3 min at 1-min ticks, not ~21 min at 5-min
  - D-007 incomplete: taper path skips consumption headroom (likely a bug)
  - D-008 incomplete: omitted entity-mode exclusion and WS debounce
  - C-014 boundary imprecise: 0.10 itself fails plausibility
  - C-012 falsely marked as GAP (3 tests exist in test_services.py)
  - Test count was 519, not ~378 (test_services.py 79 tests nearly absent)
  - 3 constraints missing: end-guard, unmanaged mode, discharge SoC gap
- **What was found**:
  - C-019: discharge path has no SoC-unavailability abort (code gap)
  - Taper-aware deferred start ignores consumption (D-007, potential bug)
- **What could be better**: The initial analysis agents should run
  `pytest --co -q` for authoritative test counts rather than estimating
  from file reads. Constraint-test mapping should cross-reference
  test_services.py more thoroughly — it's the largest integration test
  file and was largely missed.

### 2026-04-14 — Update pass (D-007 fix + trace integrity failure)
- **What was fixed**: D-007 taper-path consumption bypass — both charge
  and discharge deferred start now account for consumption in the taper
  path. 4 new tests added (523 total). Test counts corrected across
  06-tests.md and 05-coverage.md (multiple files had stale counts).
  D-006 test trace pointed to wrong class name (fixed).
- **What was wrong**: D-008 lists `ws_all_sessions` as a rejected
  alternative, but the code implements it as a supported configuration
  toggle that fundamentally changes the WebSocket activation conditions.
  This is an **UNDOCUMENTED code path** — it has no upward trace through
  the knowledge tree. The update verification agents missed it because
  they only checked top-down (does doc match code?) and never bottom-up
  (does code match doc?). The "Alternatives considered" framing caused
  the agent to treat implemented behaviour as historical context.
- **Skill improvement**: Added bidirectional trace integrity checking
  to the skill (Check step 5, Update after-step, Coverage gap types).
  Verification must now walk code → design → constraint, not just the
  reverse. "Alternatives considered" entries are audited to confirm
  they are genuinely rejected, not implemented.

### 2026-04-17 — Update pass (ws_mode + structured logging + ID collision)
- **Changes detected**: 7 commits since last update (6d5443d..bd0b44a) plus
  uncommitted `ws_mode` feature. Source files: `__init__.py`, `const.py`,
  `config_flow.py`, `smart_battery/logging.py`, `smart_battery/listeners.py`,
  `smart_battery/sensor_base.py`, `tests/e2e/test_e2e.py`.
- **D-NNN ID collision fixed**: D-023 was assigned to both "Progressive
  schedule extension" (smart-discharge.md) and "Transient adapter error
  resilience" (session-management.md). Renumbered session-management entries
  to D-025 (transient errors) and D-026 (pending override cleanup). All
  cross-references in 02-constraints.md and 05-coverage.md updated.
- **D-008 rewritten**: The former "Conditional WebSocket activation" listed
  "always-on WebSocket" as a rejected alternative, but `ws_mode=always` is
  now implemented. Rewritten as "WebSocket activation modes (ws_mode)" with
  three documented states (auto/smart_sessions/always), migration from the
  old boolean, and watchdog for always mode. This repeats the exact pattern
  flagged in the 2026-04-14 reflection — implemented behaviour described
  as a rejected alternative.
- **D-027 added**: Structured session logging via `logging.Filter`.
  12 new unit tests in `test_structured_logging.py`. Traces to C-020.
- **Test counts updated**: 570 unit + 66 E2E = 636 total (was 557 + 19).
  E2E expanded via cloud/entity parametrization and new data source tests.
- **C-024/C-025 promoted**: Both now have design docs (D-025/D-026), moving
  from PARTIAL to COVERED in the coverage matrix.

### 2026-04-18 — Flaky test investigation + D-009 fix + session monitoring
- **Flaky test root cause**: Unit tests using the FoxESS simulator
  (`tests/test_client.py`, `tests/test_inverter.py`) failed intermittently
  at ~0.5% rate under parallel execution. Three distinct test failures
  observed across ~350 runs: `test_get_soc` (SoC=50 instead of 75),
  `test_real_query` (SoC=50 instead of 75), `test_rate_limit_exhausts_retries`
  (fault not raised). Root cause: `simulator/server.py` stored
  `InverterModel` and `_ws_clients` as **module-level singletons**.
  With `pytest-xdist -n auto`, each worker process has its own module
  globals, but within a single worker, function-scoped fixtures create
  new simulator apps (own port, own event loop thread) that all share
  the same `_model`. Daemon threads from previous tests' simulators
  could process `reset()` requests from teardown AFTER the next test
  had already `set()` its state, clobbering the new test's setup.
  Fix: moved `_model` and `_ws_clients` to per-app state
  (`app["model"]`, `app["ws_clients"]`). Each `create_app()` now gets
  a fully isolated `InverterModel`. 0/200 failures after fix vs 1/200
  before.
- **Troubleshooting methodology**: 5 initial runs didn't reproduce.
  Scaled to 50 runs (1 failure), then 100 (0), then 200 with explicit
  `-n 16` (1). The ~0.5% rate requires 200+ runs to reliably observe.
  Key insight: examine ALL observed failure messages across runs — the
  different test names but same symptom (wrong default values) pointed
  to shared mutable state rather than a test-specific bug.
- **D-009 WS linger race fixed**: E2E test
  `test_ws_linger_captures_post_discharge_data` written first (test
  before fix). Test confirmed failure on CI: `discharge_rate` stuck at
  0.496 kW after session end. Fix: `_on_session_cancel` returns the WS
  stop coroutine instead of scheduling via `async_create_task`; all 10
  cancel→override paths in `listeners.py` await it after override
  removal. CI confirmed fix passes.
- **SoC precision**: Coordinator rounded `_soc_interpolated` to 1dp
  before storing in coordinator data, losing precision before the
  Lovelace card could display it. Card's `toFixed(2)` showed trailing
  zero. Fix: store full float, round only for change detection (2dp
  gate to prevent entity update storms).
- **Live session monitoring**: Discharge session 09:00–09:11 monitored
  in real-time via HA REST API. Verified: WS active, power pacing down
  from 4.9kW to 0.5kW, feed-in tracking (deferred baseline working —
  no phantom jump), schedule horizon set, session ended cleanly at
  feed-in limit (1.0 kWh), no errors.
- **Skill improvement opportunity**: The flaky test investigation
  exposed a gap in C-033 (minimise simulator–production deviations) and
  C-028 (simulator over mocks). The constraint says "use simulator",
  but doesn't capture "simulator instances must be isolated". A new
  constraint or an amendment to C-028 should state: **each test must
  have an independent simulator instance with no shared mutable state**.
  The module-level singleton pattern violated this implicitly — it
  worked when tests ran serially but broke under parallelism. The
  skill's staleness detection (monitoring `conftest.py` and test
  infrastructure) would have flagged this if it checked for module-level
  state in simulator code.

### 2026-04-19 — Automated reflection (149 interactions, 2026-04-17 to 2026-04-19)
- **HA best practices audit**: 16 items implemented across 3 sessions,
  covering HA 2024.x+ patterns: `ConfigEntryNotReady`, `PARALLEL_UPDATES`,
  repair issues, unrecorded attributes, clean removal, diagnostics, entity
  categories, display precision, enriched DeviceInfo, reauthentication, error
  handling, `icons.json`, HA-managed aiohttp session, named `async_create_task`,
  `serial_number` in DeviceInfo, `Platform` enum, and `entry.runtime_data`.
  The `entry.runtime_data` migration introduced `FoxESSControlData` and
  `FoxESSEntryData` typed dataclasses with a bridge layer for backward
  compatibility. Architecture doc updated to reflect the new data flow.
- **BMS battery temperature sensor**: User identified that the Open API's
  `batTemperature` reports the inverter's sensor, not the BMS cell
  temperature. Low BMS temps (e.g. 14.9°C) inhibit charge rate despite
  the API reporting ~22°C. New sensor added via web portal scraping.
  This is operationally critical context that pacing algorithms don't yet
  account for. Memory saved in `project_bms_temperature.md`.
- **CI workflow restructuring**: E2E tests moved under `tests/`, default
  pytest now runs all tests (workflows opt out via `-m "not slow"`), release
  workflow depends on E2E `results` job. Duplicate CI runs on develop+main
  discussed — workflows now trigger only on develop; main uses branch
  protection requiring status checks from the develop run.
- **Session recovery E2E**: HA restart identified as an under-tested area.
  E2E tests added for session recovery (adapter group persistence, store
  flush on unload, overview sensor restore).
- **Test counts updated**: 589 unit + 88 E2E = 677 total (was 587 + 74
  = 661). Growth from E2E parametrisation (cloud/entity) and new
  restart recovery tests.
- **Knowledge tree updates applied**:
  - `03-architecture.md`: Added `FoxESSControlData`/`FoxESSEntryData`
    typed domain data section, updated data flow diagram.
  - `06-tests.md`: Test counts updated to 589 + 88 = 677.
- **Stale areas not yet updated** (recommend `/project-overview update`):
  - `02-constraints.md`: No new C-NNN for HA best practices patterns
    (e.g. "use `entry.runtime_data` for typed per-entry data", "surface
    errors via HA Repairs panel"). These may warrant constraints if the
    project mandates these patterns going forward.
  - `04-design/`: No D-NNN for the runtime_data migration, HA session
    management, or BMS temperature sensor. These are significant design
    decisions that should be documented.
  - `05-coverage.md`: Matrix stale — new tests and design decisions not
    cross-referenced.
- **Process observation**: The user emphasised running E2E tests after
  each incremental change ("these failing tests would be much easier to
  fix if you had checked e2e tests as each item was done"). This is
  already captured in memory (`feedback_e2e_coverage.md`) but the
  knowledge tree doesn't have a constraint for it. C-029 says "E2E for
  HA-dependent behaviour" but doesn't capture the incremental testing
  discipline.

### 2026-04-20 — Automated reflection (214 interactions, 2026-04-19 to 2026-04-20)
- **Test count growth**: 670 unit + 126 E2E = 796 total (was 603 + 88
  = 691). Growth from: overview card E2E tests (click-to-history, box
  customisation, sub-links), control card form input persistence tests,
  cold-temperature curtailment tests, BMS temperature preservation tests.
  `06-tests.md` updated.
- **New design doc**: `04-design/lovelace-cards.md` created with 4
  decisions: D-035 (click-to-history), D-036 (box customisation),
  D-037 (cold-temp curtailment), D-038 (BMS temp preservation).
  These features were implemented across beta.23–beta.29 without
  design documentation.
- **E2E test hardening pattern**: A recurring theme across 3 sessions
  was replacing one-shot `page.evaluate()` with `wait_for_function()`
  in Playwright tests. The root cause is consistent: HA custom card
  shadow DOM renders asynchronously after the `hass` property is set,
  and under CI load the 2s settle time after reload isn't enough.
  This pattern should be a documented testing constraint — all
  Playwright assertions on shadow DOM content must use polling waits,
  never one-shot evaluate. Recommend adding to C-031 or as new
  constraint.
- **Regression-test skill improvements**: User flagged that fix agents
  were writing tests with plain dicts instead of production types,
  and accepting fixes without proper root-cause investigation. Skill
  updated with production-type verification and root-cause requirements.
  The `test_before_fix` pattern was also added to CLAUDE.md.
- **Stale areas not updated this pass** (recommend
  `/project-overview update`):
  - `05-coverage.md`: Matrix needs D-035 through D-038 added
  - `02-constraints.md`: No constraint for "Playwright assertions
    must poll, never one-shot evaluate" pattern
  - `03-architecture.md`: No mention of Lovelace card architecture
    (shadow DOM, Web Components, static JS resources)

### 2026-04-21 — Update pass (show_cancel + form DOM preservation)
- **Changes detected**: 2 commits since last update (c36f293..407388b).
  Source files: `foxess-control-card.js`, `tests/e2e/test_ui.py`.
- **D-039 added**: Control card `show_cancel` config option — hides
  cancel button during active sessions. Traces to C-020.
- **D-040 added**: Targeted DOM updates when form overlay is present.
  Header/content/action-row updated selectively; form overlay left
  intact to preserve native time picker state and focus. Traces to
  C-020. The rejected alternative (save/restore form values after full
  innerHTML replacement) was the previous implementation — it failed
  because native picker popup state cannot be saved/restored.
- **Test counts updated**: 670 unit + 128 E2E = 798 total (was 670 +
  126 = 796). Growth from `test_time_picker_stays_open_during_rerender`
  × 2 connection modes.
- **Coverage matrix updated**: D-035–D-040 added to C-020 and C-001
  rows. Unit/E2E counts corrected (were 598/88, now 670/128).
- **README updated**: Documented all 4 Lovelace cards, `show_cancel`,
  box customisation, click-to-history, BMS temperature, debug log
  sensors, cold-temp curtailment, session resilience, diagnostics,
  repair issues, reauthentication.
- **Stale areas resolved**: `05-coverage.md` D-035–D-038 gap (flagged
  in 2026-04-20 reflection) now addressed.

### 2026-04-21 — Automated reflection (110 interactions, 2026-04-21)
- **Architecture remediation completed**: 5-phase plan executed across
  multiple sessions. `__init__.py` reduced from ~2500 to ~1600 lines via
  extraction of `_services.py` (~800 lines) and `_helpers.py` (~340 lines).
  `FoxESSControlData` bridge layer (`__getitem__`/`__contains__`/`get`)
  fully removed — all access now via typed attributes. Config accessors
  consolidated into frozen `IntegrationConfig` dataclass. Circuit breaker
  extracted to shared function. Architecture doc updated to reflect new
  module boundaries.
- **Temperature-aware taper model**: Already documented (D-014, D-015 in
  `taper-model.md` from commit 1c505df). Multiplicative SoC × temperature
  decomposition with 10-minute stability gate. Cold-temperature limit
  (`_apply_cold_temp_limit`) removed in favour of data-driven model.
  `test_cold_temp_limit.py` deleted; replaced by `tests/test_taper.py`
  temperature tests (67 total taper tests).
- **Simulator enhancements**: 5 new features added: battery efficiency
  factor, MD5 signature validation, per-endpoint rate limiting,
  null_schedule fault injection, fdSoc enforcement. New test file
  `test_simulator_model.py` (22 tests). Architecture doc updated.
- **Modbus debugging instrumentation**: First-read/write logging for
  entity-mode, raw WS node in mapping diagnostics. Helps remote users
  debug Modbus entity mapping without SSH access. Traces to C-020.
- **Anker Solix X1 assessment**: User evaluated multi-brand candidate.
  Third-Party Controlled Modbus mode maps to InverterAdapter. Memory
  already saved in `project_anker_solix_x1.md`.
- **Discharge sim improvements** (`deferred-discharge-sim.html`): Peak
  decay fix, BMS taper model, solar generation model, safe horizon
  visualisation, suspension reason display, priority board, 13 presets.
  Not tracked in knowledge tree (standalone documentation tool).
- **WS divergence fix**: Anomalous WS messages now dropped instead of
  just logging a warning. 193 new coordinator tests added.
- **CI changes**: E2E parallel workers increased from 12 to 20. Flaky
  test workflow changed to 20 runs with random half-selection per run
  (each test run ~10 times on average alongside random others).
- **Test counts updated**: 719 unit + 140 E2E = 859 total (was 670 +
  128 = 798). Growth from: simulator model tests (22), taper temperature
  tests, coordinator WS divergence tests (193), E2E parametrization
  expansion. Coverage matrix updated.
- **Knowledge tree updates applied**:
  - `03-architecture.md`: Module boundaries updated for `_services.py`,
    `_helpers.py` extraction, bridge layer removal, `IntegrationConfig`.
    Simulator fidelity section expanded with 5 new features.
  - `05-coverage.md`: D-014/D-015 added to C-014 row, test counts
    updated to 719/140/859.
  - `06-tests.md`: Test counts updated, simulator model section added,
    taper model section expanded with temperature tests.
- **Stale areas not updated this pass** (recommend
  `/project-overview update`):
  - `02-constraints.md`: No new C-NNN for WS message anomaly dropping
    (may warrant extending C-004 or C-005 to cover "drop anomalous
    messages where power values diverge significantly from preceding
    values"). No constraint for entity-mode first-read/write
    instrumentation (informational, not an invariant).
  - `04-design/`: No D-NNN for `IntegrationConfig` consolidation,
    `_services.py` extraction, or circuit breaker extraction. These are
    architecture decisions that reduce coupling and improve
    maintainability but don't implement a specific constraint. Consider
    whether they warrant documentation under a principle like C-021
    (code organisation) or are sufficiently captured by the architecture
    doc updates.
  - No D-NNN for simulator enhancements (MD5, rate limiting, fault
    injection, efficiency). These serve C-033 (simulator fidelity) but
    are infrastructure improvements rather than design decisions with
    alternatives considered.

### 2026-04-21 — Update pass (architectural lint + WS plausibility refactor)
- **Changes detected**: 6 commits since last update (637be15..fa8ffc4).
  Source files: `coordinator.py`, `realtime_ws.py`, `.semgrep/`,
  `.githooks/check-module-size`, `.pre-commit-config.yaml`, `CLAUDE.md`,
  `domain_data.py`, `sensor.py`.
- **C-034, C-035, C-036 added**: Architectural constraints enforced by
  automated tooling (semgrep + pre-commit hooks). Module size budget
  (2000 lines), typed config access (`IntegrationConfig`), typed domain
  data access (`_dd(hass)`). All three are ACCEPTED in coverage matrix
  (process/infrastructure constraints without D-NNN).
- **D-041 added**: WS anomaly plausibility filter. Originally at
  coordinator level (03d0c5f), refactored to `realtime_ws.py` (fa8ffc4)
  to keep data-source-specific logic in the WS module. Traces to C-004,
  C-005. 14 tests (11 unit `TestIsPlausible` + 3 integration
  `TestWsPlausibilityFilter`).
- **Semgrep integration**: 4 rules added to `.semgrep/foxess-architecture.yaml`
  (brand imports, raw hass.data, raw entry.options, service handlers in
  init). Existing violations fixed in `sensor.py` and `coordinator.py`.
  `bms_polling_interval` added to `IntegrationConfig`.
- **Test counts updated**: 727 unit + 140 E2E = 867 total (was 719 +
  140 = 859). Net +8 unit: 193 coordinator WS divergence tests removed
  (moved to WS layer), 197 realtime_ws plausibility tests added, plus
  4 from semgrep-related test changes.
- **Coverage matrix updated**: C-034–C-036 added as ACCEPTED. D-041
  added to C-004 and C-005 rows. Total constraints 36 (35 active + 1
  proposed). Total design decisions 35.
- **Stale areas resolved from prior reflection**:
  - WS anomaly dropping now documented as D-041 (was flagged as missing
    D-NNN for "WS message anomaly dropping").
  - Architecture constraints now formal C-NNN entries (was flagged as
    "consider whether they warrant documentation under a principle like
    C-021").

### 2026-04-21 — Update pass (stale design docs + D-042)
- **Stale docs refreshed**: 4 design docs updated:
  - `session-management.md`: D-025 rewritten from "transient error
    resilience" to "two-tier circuit breaker" — now documents hold
    phase, replay mechanism, `_with_circuit_breaker` shared function,
    and `_notify_replay` callback. D-031 updated to reflect bridge
    layer removal (migration complete). Key behaviours updated.
  - `foxess-api.md`: D-033 endpoint corrected from
    `/generic/v0/device/battery/info` (via device UUID) to
    `/dew/v0/device/detail?id=<compound_id>&category=battery` (via
    WebSocket-discovered compound ID). Evolution history documented.
    D-042 added: auth retry on errno 41808/41809 with executor-wrapped
    WASM signatures.
  - `smart-charge.md`: Key behaviours updated with temperature-aware
    time estimates (bms_temp_c passthrough), cold-temp curtailment
    cross-reference (D-037), circuit breaker cross-reference (D-025).
  - `smart-discharge.md`: D-004 clarified peak update formula.
    Key behaviours updated with temperature-aware deferred start and
    circuit breaker cross-reference.
- **Coverage matrix**: D-042 added to C-024 row. Design decision count
  updated to 36 (was 35). D-020 reclassified note added.
- **Stale areas resolved from check report**:
  - D-031 bridge layer text (check item 2) — updated to reflect removal
  - D-033 endpoint reference (check item 3) — corrected
  - D-025 circuit breaker description (check item 4) — rewritten

### 2026-04-22 — Update pass (entity-mode dashboard + feedin deferred fix)
- **Changes detected**: 10 commits since last update (eb5b98e..e81850a).
  Key changes: entity-mode dashboard support (4 new entity mappings +
  automatic unit conversion), feedin deferred start over-deferring fix
  (tight windows), force op premature WS fix, E2E CI timing balancing.
- **D-005 updated**: Feed-in energy budget spreading decision expanded
  to document the tight-window guard for deferred start. The feedin cap
  is now skipped when the uncapped SoC deadline falls before the window
  start, preventing over-deferral. New test
  `test_tight_window_feedin_does_not_over_defer` added to traces.
- **Architecture updated**: Entity mode section expanded with 9
  coordinator variable mappings via `_ENTITY_VAR_MAP` 3-tuples and
  automatic unit conversion using HA's `PowerConverter`,
  `EnergyConverter`, `TemperatureConverter`.
- **Test counts updated**: 736 unit + 140 E2E = 876 total (was 727 +
  140 = 867). Growth from entity-mode unit conversion tests (+4) and
  feedin deferred start regression test (+1), plus build_entity_map
  updates (+4).
- **E2E CI timing observation**: Greedy bin-packing for E2E worker
  distribution (beta.11) is working correctly when timings are
  available, but falls back to count-based splitting when the artifact
  download fails (`workflow_conclusion: success` filter skips failed
  runs). Two inherently slow tests (~7-8 min each:
  `test_ws_connects_after_deferred_start` at ~471s,
  `test_api_down_during_discharge_opens_circuit_breaker` at ~417s)
  dominate wall time. Both are justified: the deferred start test
  requires real-time deferral (~4 min) because HA's
  `async_track_time_interval` cannot be accelerated; the circuit
  breaker test requires 3×60s discharge ticks to trip the breaker
  (C-024).

### 2026-04-24 — Automated reflection (170 interactions, 2026-04-22 to 2026-04-24)

**Major work streams across 5 sessions:**

- **Force→smart unification (v1.0.8)**: Force charge/discharge operations
  now delegate to smart session internals with `full_power=True`. `power`
  parameter removed from force ops. `_start_force_op_ws` infrastructure
  deleted. Full release v1.0.8 created.

- **Production bug fixes (v1.0.9–v1.0.10)**:
  - WS connecting during `discharge_scheduled` phase before window opens.
    Fixed by adding `now < start` guard in `_should_start_realtime_ws`.
  - Session recovery discarding deferred sessions after HA restart.
    Recovery code required a matching schedule group on the inverter,
    but during deferred phase no schedule exists yet. Fixed by mirroring
    charge recovery pattern: `if has_group or not discharging_started`.
  - Feedin-limited discharge starting immediately instead of deferring.
    Root cause: tight-window guard in D-005 skipped feedin cap when
    uncapped SoC deadline fell before window start. With 42kWh battery
    and 1kWh feedin limit, this caused 51-min active discharge at 1469W
    instead of deferring to ~7 min before end. Fixed by always applying
    feedin cap. D-005 updated.
  - Overview card crash on corrupted `box_config` entries (`flow_from`
    undefined TypeError).

- **Grid export limit feature (v1.0.9-beta.8+)**: User identified that
  a 5kW net grid export limit wasn't factored into deferral timing.
  New `grid_export_limit` config option (W) added across config flow,
  algorithms, listeners, services, and UI. When configured: deferral
  uses export-limited rate for timing, discharge always requests max
  inverter power (firmware handles export capping), pacing skipped.
  Key insight from user: "the export limit is nett: a 5kW export limit
  and a 2kW household load implies a 7kW feed in limit."

- **LitElement migration attempt and revert (v1.0.9)**: Card migrated
  from vanilla HTMLElement to LitElement to solve form value preservation.
  Failed because HA bundles Lit into hashed webpack chunks with no import
  map — `import("lit")` fails, prototype extraction can't reach
  `html`/`css`/`nothing` (module-scope exports, not class properties).
  Reverted. D-041 documents the vanilla HTMLElement constraint and upgrade
  paths (bundled Lit via Rollup, or morphdom). D-040 expanded with
  `_formValues` snapshot mechanism as the working solution.

- **Charge re-deferral (D-043, v1.0.11-beta.2)**: User identified that
  smart charge reached target 30+ minutes early. Root cause: once
  `charging_started=True`, listener only adjusts power (min 100W), never
  switches back to self-use. User corrected my initial analysis that
  solar needed to be in pacing calculation — the real fix is re-evaluating
  deferral each tick and reverting to self-use when ahead of schedule.
  Implemented with `calculate_deferred_start()` check after charging
  starts.

- **Taper recording denominator fix (v1.0.11-beta.1)**: User observed
  taper profile recorded ratio 1.0 at 81% SoC despite BMS limiting
  charge to 6380W. Root cause: `_record_taper_observation` used paced
  `last_power_w` (4552W) as denominator instead of `max_power_w`
  (10500W). Ratio 6380/4552=1.40 clamped to 1.0, making real taper
  invisible.

- **Sensor-listener parameter parity fixes (v1.0.9-beta.9+)**: Discharge
  deferral countdown and charge "Scheduled" display used simplified
  formulas in `sensor_base.py` that didn't match the full algorithm in
  listeners. Fixed by using `calculate_deferred_start()` /
  `calculate_discharge_deferred_start()` with all parameters.

- **Soak test suite (v1.0.11-beta.3+)**: 17 real-time charge/discharge
  scenarios through containerised HA + simulator. Simulator auto-tick
  added (`_auto_tick_loop`, 5s). PID-prefixed container names for
  concurrent run isolation. SQLite inflection-point store for cross-run
  comparison (state transitions, SoC direction changes, power steps).
  Monotonic energy counters (`grid_consumption_total_kwh`) for charge
  overshoot detection. Systemd timer/service added to repo.

- **Regression-test skill improvements**: User strongly corrected
  dismissal of CI failures as intermittent ("no flake is acceptable!!!").
  Skill updated with: Phase 1 hard stop (no source code reading before
  reporting), Phase 2→3 gate (dispatch immediately after reporting),
  two-commit verification (test-only must fail, fix makes it pass),
  production type verification, knowledge tree compliance check.
  CLAUDE.md updated with TDD process rule and `superpowers:test-driven-
  development` skill created.

**User corrections captured:**
1. "the force operations should no longer allow a power limit to be
   specified; it is not compatible with the concept of forcing full
   power" — removed `power` param from force ops entirely.
2. "why does solar need to be incorporated into the pacing calculation?"
   — corrected my analysis; re-deferral is the right approach, not
   solar-aware pacing.
3. "wait shouldn't it be checking energy and not power?" — shifted
   charge overshoot check from instantaneous power to accumulated energy.
4. "does the foxess simulator expose the same variables as the real
   inverter? there are monotonic variables" — led to using
   `grid_consumption_total_kwh` for exact energy measurement.
5. "no flake is acceptable. remember this!!!" — saved as memory; never
   dismiss CI failures as intermittent.
6. "the regression test skill is supposed to ensure tests get written
   first. The parent agent is supposed to review the result. Neither of
   those things happened properly" — led to skill hardening with
   concrete verification steps.

**Test counts updated**: 786 unit + 130 E2E + 17 soak = 933 total
(was 736 + 140 = 876). Growth from: taper observation tests, session
recovery tests, charge re-deferral tests, sensor parity tests,
soak scenarios (17), inflection-point DB tests (6). E2E count dropped
from 140 to 130 (deselection of invalid parametrisation combos).

**Knowledge tree updates needed** (recommend `/project-overview update`):
- `02-constraints.md`: No C-NNN for grid export limit awareness, no
  C-NNN for sensor-listener parameter parity.
- `04-design/smart-discharge.md`: D-005 needs the tight-window guard
  REMOVAL documented (previous update added it; it was then removed).
  Grid export limit interaction needs D-NNN.
- `04-design/smart-charge.md`: D-043 was added but may need traces
  updated after implementation.
- `06-tests.md`: Test counts stale (933 vs documented ~876). Soak
  test section missing entirely. Inflection-point DB tests missing.
- `05-coverage.md`: Matrix stale — D-043, taper denominator fix,
  grid export limit feature not cross-referenced.

### 2026-04-24 — Priorities component introduced (P-NNN)

**Trigger**: Live smart-discharge session monitoring revealed that
`calculate_discharge_deferred_start` applied a doubled feed-in
headroom (`min(headroom*2, 0.40) = 0.20`) even on sites where the
configured `grid_export_limit_w` (5 kW) was well below the inverter
max power (10.5 kW). On such sites, load volatility below the
export clamp threshold has zero effect on effective export rate, so
the doubled headroom was protecting against a physically impossible
degradation — eating ~3 min of self-use time to guard feed-in target.

**Structural gap this exposed**: The doubled-headroom line was
described in D-044's rationale with safety-flavoured language
("all export must come from forced discharge, so we start earlier to
absorb load spikes"). But the thing it actually protects is
**feed-in target achievement** — a lower-priority goal than self-use
/ no-import. Because the knowledge tree had no formal priority
register, nobody traced the decision back to the priority it
actually served, and the trade-off (self-use time for feed-in
margin) was invisible.

**Action taken** (this pass):
- Added `## Priorities` section to `01-vision.md` with P-001..P-006:
  P-001 no grid import, P-002 min SoC, P-003 energy target, P-004
  feed-in, P-005 operational transparency, P-006 brand portability.
  Order derived from user memory `feedback_discharge_priorities.md`
  and extended with operational/architectural goals already implicit
  in the tree.
- Backfilled `Priority enforced` on all 38 C-NNN in
  `02-constraints.md`.
- Backfilled `Priority served`, `Trades against`, and
  `Classification` (safety/pacing/other) on all 45 D-NNN across
  seven design files.
- Updated D-044 Decision / Key Behaviours to reflect today's fix
  (`2dee100`): the doubled headroom is now conditional on whether
  peak load can breach the export-clamp slack.
- Regenerated `05-coverage.md` with a priority → enforcement matrix
  and a trade-off audit table; mapped the new
  `TestFeedinHeadroomAccountsForExportClamp` tests.

**Skill update**: The `project-overview` skill itself was updated
first to define P-NNN, the trade-off audit, and the safety/pacing
classification. Templates for `01-vision.md`, `02-constraints.md`,
`04-design.md`, and `05-coverage.md` updated so future `init`/
`update` runs ask for priorities during the interview and wire
them through automatically.

**Rank-change policy**: Priority IDs are stable labels, not rank
indicators. If P-005 needed to rank above P-003 in a future
revision, it would be moved earlier in the list in `01-vision.md`
but keep its ID. Record such rank changes here when they happen;
none so far.

**Lessons captured** (for future reflection):
- Buffers and margins commonly attract safety-flavoured rationale
  even when they serve pacing goals. The `Classification` field
  forces that distinction to be explicit.
- Live session monitoring is a productive way to surface priority
  inversions — the doubled-headroom bug was invisible in review but
  obvious when watching the system defer for too long on a calm
  evening.
- The fix commit pair (`ad624da` Test + `2dee100` Fix) is the
  concrete example of how a priority-inversion discovery leads to
  tightened tests and a tighter algorithm.

### 2026-04-24 — Update pass (export-limit actuator + stale architecture)

**Changes detected** (post-priority-introduction `/project-overview update`):
- Source commits since `c0b1327` (last 03-architecture touch)
  introduced an export-limit actuator on the `InverterAdapter`
  protocol (`set_export_limit_w` / `get_export_limit_w`, commits
  5861af7..5195a64, d2fcd19, 52f4ea0) and the 2026-04-24
  export-limit-clamp headroom fix (`2dee100`). Test count grew
  786 → 863 unit, 130 → 136 E2E, 17 → 19 soak = 1018 total.
- `03-architecture.md` had a structural defect: a "Soak Test
  Infrastructure" block was inserted mid-External Dependencies
  table, breaking the markdown. The BMS-temperature endpoint
  reference on line 175 was also stale (`/generic/v0/device/
  battery/info` instead of `/dew/v0/device/detail`).

**Actions taken**:
- `03-architecture.md`: repaired the External Dependencies table,
  moved Soak Test Infrastructure to its own section, corrected the
  BMS endpoint, added `set_export_limit_w` / `get_export_limit_w`
  to the InverterAdapter method list, and extended the data-flow
  diagram to show the export-limit write path.
- `04-design/smart-discharge.md`: added **D-047 Hardware
  export-limit actuator for discharge pacing**, classified safety
  (serves P-001 via C-001 floor enforcement). Describes the
  two-channel control scheme (cloud schedule at max, hardware
  actuator modulated each tick with threshold-gated writes).
- `06-tests.md`: refreshed counts to 863 / 136 / 19 / 1018; added
  sections for the `TestFeedinHeadroomAccountsForExportClamp` (6)
  and `tests/test_export_limit.py::*` (24) groups.
- `05-coverage.md`: added D-047 to P-001, C-001, C-020, C-037
  rows; updated summary counts; corrected classification tallies
  to measured values (15 safety / 13 pacing / 21 other — the
  previous ~18/~13/~14 estimate was a hand-wave).

**Observations**:
- The `/update` workflow correctly surfaced the
  `set_export_limit_w` adapter change as an architecture gap and
  flagged the D-044 textual staleness — both are exactly the kinds
  of issues the tree is meant to catch.
- The Soak Test Infrastructure insertion bug illustrates why
  architecture docs benefit from headings (`## Soak Test
  Infrastructure`) rather than being slipped into mid-table. A
  future skill-level improvement could add a markdown structural
  sanity check (tables intact, no orphan rows after blank lines).

### 2026-04-25 — Live-monitoring bug cascade + three D-NNN additions

**Context**: Setting up the `collect_ha_session.py` trace collector
against the author's live HA surfaced a real production bug that
had been silently affecting every smart_charge session: SoC / house
load / generation / work mode all froze for 50+ minutes while
coordinator polls succeeded. The "sensor frozen" symptom led to an
investigation that exposed three separate structural gaps.

**Chain of discoveries:**

1. `SmartOperationsOverviewSensor.native_value` returned `"scheduled"`
   (a value added in 1.0.11-beta.9 to distinguish charge-side
   pre-window state) that was missing from `_attr_options`. HA's
   sensor base class raised `ValueError`, and because
   `async_update_listeners()` iterates sequentially without
   per-listener exception handling, every listener registered after
   the failing one stopped updating. Fixed in 1.0.12 with a
   one-line options addition (`9b95dec`). Regression test
   (`TestSmartOperationsSensorOptionsCoverage`) is parametrised over
   every value `native_value` can return — structurally prevents
   recurrence.

2. The freeze was invisible from the user's perspective: no Repair,
   no persistent notification, no error attribute, no nothing. This
   surfaced a gap in C-026's enforcement: the session-error state
   dict (D-029) is written TO the `SmartOperationsOverviewSensor`
   itself, so if that sensor is the one failing, the error surface
   is circular. The Repair-issue path is orthogonal and
   listener-agnostic. Documented as **D-048** (sensor-listener
   write failures surface as HA Repair, not silent freeze), fixed
   in 1.0.12.

3. Trace collection found zero `SCHEDULE_WRITE` events from
   `foxess.inverter` across a 2-hour session, despite the v1.0.12
   fix adding emission at `Inverter._post_schedule`. Root-caused
   to **`emit_event`'s use of `logger.info()`** — Python evaluates
   `Logger.isEnabledFor()` BEFORE propagation, so a per-module
   level override (from HA's `logger:` YAML or
   `logger.set_level` service) silently drops INFO records at the
   child. Fixed in 1.0.13-beta.1 by changing dispatch to
   `logger.makeRecord()` + `logger.handle()`, bypassing the
   logger-level check while keeping every filter/handler chain.
   Documented as **D-050** (emit_event bypasses logger-level
   filter).

**Architectural finding**: The v1.0.12 fix emitted
`SCHEDULE_WRITE` at the FoxESS API layer (`Inverter._post_schedule`)
in parallel with existing listener-layer emissions in
`smart_battery/listeners.py` and `smart_battery/services.py` via
`emit_schedule_write()`. That dual-layer arrangement wasn't
obviously intentional when viewed from the v1.0.12 fix alone — it
looked like a near-duplicate. After review: the two layers
capture fundamentally different information (intent vs wire; D-014
sanitises between them), and simulator validation needs both.
Documented as **D-049** (dual-layer SCHEDULE_WRITE emission) so
future contributors don't collapse it back to a single emission.

**Knowledge-tree updates (this pass)**:
- `04-design/session-management.md`: added **D-048** and **D-050**.
- `04-design/foxess-api.md`: added **D-049**.
- `02-constraints.md`: C-026 Traces line now cites D-029 and D-048,
  plus the new regression test suite.
- `06-tests.md`: 863→890 unit (authoritative), 1018→1045 total.
  Added three new sections: "Structured Events — Emission Paths and
  Propagation (D-049, D-050)", "Sensor-Listener Write Safety
  (D-048)", "SmartOperations Sensor Options Coverage (C-038)".
- `03-architecture.md`: added "Live HA Session Collector" section
  covering `scripts/collect_ha_session.py` + paired systemd service;
  expanded Soak Test Infrastructure to note the ExecStart script
  extraction.

**Lessons captured**:
- **Live-tracing infrastructure pays for itself.** The
  `collect_ha_session.py` work that preceded the bug discovery
  was the only reason any of this was found — the symptom was
  invisible on the dashboard and absent from the debug log for
  the failing sensor path. Continuing to run the collector in
  production accrues value every session.
- **Enum options and their producers drift apart silently.**
  Parametrised "every value returned by `native_value` is in
  `_attr_options`" tests for every HA enum sensor are cheap
  insurance against this class of defect. Consider a
  knowledge-tree practice: any D-NNN adding a new enum value to
  a sensor must cite the matching options update as part of the
  fix.
- **Logger-level overrides can silently break integration
  telemetry.** Future integrations that emit structured events
  should follow the D-050 pattern from the start (use
  `logger.handle()` rather than `logger.info()` for telemetry).
  Candidate for promotion to a constraint if this pattern recurs.
- **The "single emission per write" intuition is sometimes
  wrong.** Replay/simulator-validation may need both intent and
  wire records. Document the split explicitly (D-049) so future
  refactors don't collapse it.

### 2026-04-25 — Decision: no string-level brand-reference semgrep rule

**Question raised**: now that C-021 (where brand-agnostic code
lives), C-039 (no brand-layer imports), and C-040 (brand-agnostic
tests) are invariants, should there be a complementary rule
forbidding textual references to "foxess" / "FoxESS" in
`smart_battery/` docstrings, comments, and string literals?

**Inventory at the time of the question**: 8 references in
`smart_battery/` — all in docstrings or comments. Every one is a
legitimate use of `foxess_control` / `FoxESS` as the canonical
example of a brand implementation (e.g. `testing.py` explains
*why* the `FakeAdapter` exists: to decouple from the FoxESS
adapter used by today's sole production integration). Zero
references are in actual code paths; zero couple the module to
FoxESS types or behaviour.

**Decision**: **No new rule.** C-039's import-direction invariant
catches the real coupling risk; docstring references do not
execute and cannot break a second-brand integration. The cost of
adding a rule would be:
- High false-positive rate. Legitimate descriptive uses
  (``"e.g. foxess_control wraps ..."``) would need rewording to
  something less informative (``"e.g. <brand>_control wraps ..."``).
  The `testing.py` rationale specifically names FoxESS because
  that IS the brand the FakeAdapter currently stands in for.
- Pre-optimisation. When the second brand arrives, these
  docstrings will be touched naturally during the "describe the
  dependency-inversion pattern in brand-neutral terms" pass.
  Grepping for `foxess` in `smart_battery/` at that point is a
  one-liner.
- Semgrep text rules are noisy: any commit message, any new
  reference-to-the-canonical-brand-example, any paste from the
  FoxESS adapter would trip the rule.

**What stays on watch instead**: the existing
`grep -ir foxess smart_battery/` command — run it when adding a
new brand, expect to generalise the docstrings that match, and
that's enough.

**Meta-observation**: the case for the rule was weak because
the rule the community actually wants is not "no mention of
FoxESS" but "no dependency on FoxESS", and the second is
already covered by C-039 + C-040. Rules should enforce
invariants, not house style — a house-style check is better done
by a brand-portability review when the second brand begins, not
by a bot.
