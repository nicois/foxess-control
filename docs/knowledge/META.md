---
project: FoxESS Control
created: 2026-04-14
last_updated: 2026-04-19
last_reflection: 2026-04-19T15:47:00+10:00
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
