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
