---
project: FoxESS Control
created: 2026-04-14
last_updated: 2026-04-14
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
