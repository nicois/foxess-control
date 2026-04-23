# Architectural Lint Enforcement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent tech debt recurrence by encoding architectural constraints as automated lint rules that fail CI — no human memory required.

**Architecture:** Three enforcement layers: (1) ruff TID251 for import bans (fast, built-in), (2) semgrep for structural patterns (AST-aware, custom rules), (3) a simple pre-commit hook for module size budget. All run via pre-commit and CI.

**Tech Stack:** ruff (already in pre-commit), semgrep (installed, needs pre-commit hook), bash (module size script)

---

## Background

The recent 5-phase architecture remediation extracted `_services.py`, `_helpers.py`, consolidated `IntegrationConfig`, and removed the bridge layer. Without enforcement, the same drift will recur: new code added to `__init__.py` instead of the right module, raw `entry.options` access bypassing `IntegrationConfig`, brand-specific imports leaking into `smart_battery/`.

The goal is zero CLAUDE.md rules that depend on Claude or the developer remembering them — every enforceable rule should fail `pre-commit run`.

## What Gets Enforced and How

| Constraint | Tool | Rationale |
|---|---|---|
| `smart_battery/` never imports brand-specific code | semgrep | AST-aware, catches `from custom_components.foxess_control.foxess...` and similar |
| No raw `hass.data[DOMAIN]` outside `__init__.py`, `_helpers.py`, `domain_data.py` | semgrep | Forces use of `_dd(hass)` typed accessor |
| No raw `entry.options` outside `__init__.py`, `domain_data.py`, `config_flow.py` | semgrep | Forces use of `IntegrationConfig` via `_cfg(hass)` |
| Module size budget: no `.py` file > 2000 lines | pre-commit script | Catches `__init__.py` growth before it becomes a problem again |
| No service handler definitions in `__init__.py` | semgrep | New service handlers must go in `_services.py` |

### What NOT to enforce (and why)

- **Import ordering within modules**: ruff `I` already handles this.
- **File placement for new modules**: too many false positives; naming conventions are better enforced by code review.
- **Test patterns** (simulator over mocks, no sleeps): too context-dependent for static analysis; stays in CLAUDE.md.
- **`entry.options` in `smart_battery/`**: the shared core legitimately reads options for `battery_capacity_kwh` and `smart_headroom` in `sensor_base.py` and `listeners.py`. These should eventually be passed as constructor arguments, but banning them now would require a larger refactor. Flag as a future migration, not an immediate lint rule.

---

## File Structure

| File | Responsibility |
|---|---|
| `.semgrep/foxess-architecture.yaml` | All semgrep rules for this project |
| `.githooks/check-module-size` | Line-count check for `.py` files (called by pre-commit) |
| `pyproject.toml` | ruff TID251 config additions |
| `.pre-commit-config.yaml` | semgrep + module-size hooks added |

---

### Task 1: Semgrep Rules

**Files:**
- Create: `.semgrep/foxess-architecture.yaml`

- [ ] **Step 1: Create semgrep rules file**

```yaml
rules:
  # C-021: smart_battery/ must not import from brand-specific modules.
  # The existing test (test_smart_battery_has_no_brand_imports) catches this
  # at test time; this rule catches it at commit time before tests run.
  - id: no-brand-imports-in-smart-battery
    patterns:
      - pattern-either:
          - pattern: from custom_components.foxess_control.foxess import ...
          - pattern: from custom_components.foxess_control.foxess.$MOD import ...
          - pattern: from custom_components.foxess_control.coordinator import ...
          - pattern: from custom_components.foxess_control.foxess_adapter import ...
          - pattern: from custom_components.foxess_control._services import ...
          - pattern: from custom_components.foxess_control._helpers import ...
          - pattern: import custom_components.foxess_control.foxess
          - pattern: import custom_components.foxess_control.coordinator
    paths:
      include:
        - "smart_battery/"
        - "custom_components/foxess_control/smart_battery/"
    message: >
      smart_battery/ must not import from brand-specific modules (C-021).
      This package is brand-agnostic — move brand-specific logic to the
      integration layer or pass it via the InverterAdapter protocol.
    languages: [python]
    severity: ERROR

  # Typed domain data: use _dd(hass) instead of raw hass.data[DOMAIN].
  # Only __init__.py (setup/teardown), _helpers.py (_dd definition),
  # and domain_data.py (type definition) may access hass.data[DOMAIN].
  - id: no-raw-hass-data-access
    pattern: hass.data[$KEY]
    paths:
      include:
        - "custom_components/foxess_control/"
      exclude:
        - "custom_components/foxess_control/__init__.py"
        - "custom_components/foxess_control/_helpers.py"
        - "custom_components/foxess_control/domain_data.py"
    message: >
      Use _dd(hass) from _helpers.py instead of raw hass.data[DOMAIN].
      Direct dict access bypasses typed domain data and was the source
      of the bridge-layer tech debt.
    languages: [python]
    severity: ERROR

  # Typed config: use _cfg(hass) instead of raw entry.options access.
  # Only __init__.py (builds IntegrationConfig), domain_data.py
  # (build_config), config_flow.py (reads options for UI), and
  # diagnostics.py (dumps raw options) may access entry.options.
  - id: no-raw-entry-options
    pattern: entry.options
    paths:
      include:
        - "custom_components/foxess_control/"
      exclude:
        - "custom_components/foxess_control/__init__.py"
        - "custom_components/foxess_control/domain_data.py"
        - "custom_components/foxess_control/config_flow.py"
        - "custom_components/foxess_control/diagnostics.py"
    message: >
      Use _cfg(hass) from _helpers.py instead of raw entry.options.
      Config values should come from IntegrationConfig, which is built
      once at setup time and cached in domain data. If the field you
      need isn't in IntegrationConfig, add it to domain_data.py first.
    languages: [python]
    severity: ERROR

  # Service handlers belong in _services.py, not __init__.py.
  # Detect service-handler-shaped functions: async functions that take
  # a ServiceCall parameter, defined in __init__.py.
  - id: no-service-handlers-in-init
    pattern: |
      async def $FUNC(..., call: ServiceCall, ...):
          ...
    paths:
      include:
        - "custom_components/foxess_control/__init__.py"
    message: >
      Service handlers must be defined in _services.py, not __init__.py.
      The _register_services() function in _services.py is the single
      registration point for all HA service handlers.
    languages: [python]
    severity: ERROR
```

- [ ] **Step 2: Run semgrep to verify rules parse and check current violations**

Run: `semgrep --config .semgrep/ custom_components/foxess_control/ --verbose 2>&1 | tail -30`

Expected: violations in `sensor.py` (raw hass.data), `coordinator.py` / `sensor_base.py` / `listeners.py` / `services.py` (raw entry.options). No violations for brand-imports or service-handlers-in-init.

- [ ] **Step 3: Fix the `sensor.py` hass.data violation**

`sensor.py:220` accesses `hass.data[DOMAIN].debug_log_handlers`. Change it to use `_dd(hass)`:

```python
# In sensor.py, replace:
hass.data[DOMAIN].debug_log_handlers.extend(handlers)
# With:
from ._helpers import _dd
_dd(hass).debug_log_handlers.extend(handlers)
```

- [ ] **Step 4: Fix `entry.options` violations in `coordinator.py`**

`coordinator.py` reads `entry.options` for `battery_capacity_kwh`, `CONF_SMART_HEADROOM`, and `min_soc_on_grid`. These are all fields already in `IntegrationConfig`. Replace with `_cfg(hass)` access:

```python
# In coordinator.py, replace each entry.options.get(CONF_...) with
# the equivalent IntegrationConfig field via _cfg(hass).
# battery_capacity_kwh → _cfg(hass).battery_capacity_kwh
# CONF_SMART_HEADROOM  → _cfg(hass).smart_headroom (already 0-1 float)
# min_soc_on_grid      → _cfg(hass).min_soc_on_grid
```

Note: `smart_headroom` in `IntegrationConfig` is already divided by 100 (a float 0-1), so callers that previously did `pct / 100` must use the value directly.

- [ ] **Step 5: Fix `entry.options` violations in `smart_battery/sensor_base.py` and `smart_battery/listeners.py`**

These are in the `smart_battery/` shared package and cannot import `_cfg(hass)` from the brand-specific layer (C-021). The correct fix is to pass these values as constructor arguments or via the existing `InverterAdapter` protocol, not to add a semgrep exception.

For now, add `smart_battery/` paths to the `no-raw-entry-options` exclude list with a comment explaining the future migration:

```yaml
      exclude:
        - "custom_components/foxess_control/__init__.py"
        - "custom_components/foxess_control/domain_data.py"
        - "custom_components/foxess_control/config_flow.py"
        - "custom_components/foxess_control/diagnostics.py"
        # smart_battery/ reads entry.options directly because it can't
        # import IntegrationConfig (C-021). Future: pass config values
        # via constructor args or InverterAdapter protocol.
        - "custom_components/foxess_control/smart_battery/"
```

Also exclude `smart_battery/services.py` at root level:

```yaml
        - "smart_battery/"
```

- [ ] **Step 6: Fix `entry.options` violation in `foxess_adapter.py`**

Check what `foxess_adapter.py` reads from `entry.options`. If the fields are in `IntegrationConfig`, switch to `_cfg(hass)`. If not, add them to `IntegrationConfig`.

- [ ] **Step 7: Re-run semgrep and confirm zero violations**

Run: `semgrep --config .semgrep/ custom_components/foxess_control/ 2>&1 | tail -10`
Expected: `0 findings`

- [ ] **Step 8: Run full test suite to confirm no regressions**

Run: `pytest tests/ -m "not slow" --tb=short -q`
Expected: all pass (719 tests)

- [ ] **Step 9: Commit**

```bash
git add .semgrep/ custom_components/foxess_control/sensor.py \
  custom_components/foxess_control/coordinator.py \
  custom_components/foxess_control/foxess_adapter.py
git commit -m "Add semgrep architectural lint rules; fix existing violations"
```

---

### Task 2: Module Size Budget Hook

**Files:**
- Create: `.githooks/check-module-size`
- Modify: `.pre-commit-config.yaml`

- [ ] **Step 1: Create the size check script**

```bash
#!/usr/bin/env bash
# Enforce module size budget: no single .py file in
# custom_components/foxess_control/ exceeds MAX_LINES.
# Keeps __init__.py from growing back to 2500+ lines.
set -euo pipefail

MAX_LINES=2000
EXIT_CODE=0

for f in custom_components/foxess_control/*.py; do
    [ -f "$f" ] || continue
    lines=$(wc -l < "$f")
    if [ "$lines" -gt "$MAX_LINES" ]; then
        echo "ERROR: $f is $lines lines (max $MAX_LINES). Extract code to a dedicated module."
        EXIT_CODE=1
    fi
done

exit $EXIT_CODE
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x .githooks/check-module-size`

- [ ] **Step 3: Test it passes on current codebase**

Run: `bash .githooks/check-module-size`
Expected: no output, exit 0 (largest file is `__init__.py` at ~1588 lines)

- [ ] **Step 4: Commit**

```bash
git add .githooks/check-module-size
git commit -m "Add module size budget check (2000-line limit per .py file)"
```

---

### Task 3: Pre-commit Integration

**Files:**
- Modify: `.pre-commit-config.yaml`
- Modify: `pyproject.toml` (optional ruff TID251 addition)

- [ ] **Step 1: Add semgrep and module-size hooks to `.pre-commit-config.yaml`**

Add after the existing `ruff` hooks:

```yaml
  - repo: local
    hooks:
      - id: semgrep-architecture
        name: architectural lint (semgrep)
        language: system
        entry: semgrep --config .semgrep/ --error custom_components/foxess_control/ smart_battery/
        pass_filenames: false
        files: ^(custom_components/foxess_control/|smart_battery/).*\.py$
        types: [python]

      - id: check-module-size
        name: module size budget
        language: script
        entry: .githooks/check-module-size
        pass_filenames: false
        files: ^custom_components/foxess_control/.*\.py$
```

- [ ] **Step 2: Run pre-commit to verify integration**

Run: `pre-commit run semgrep-architecture --all-files`
Expected: `Passed`

Run: `pre-commit run check-module-size --all-files`
Expected: `Passed`

- [ ] **Step 3: (Optional) Add ruff TID251 ban for direct foxess imports in smart_battery/**

In `pyproject.toml`, add to the ruff config:

```toml
[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM", "TCH", "S110", "S112", "BLE001", "B904", "TID251"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"custom_components.foxess_control.foxess".msg = "smart_battery/ must not import brand-specific modules (C-021)"
"custom_components.foxess_control.coordinator".msg = "smart_battery/ must not import brand-specific modules (C-021)"
"custom_components.foxess_control._services".msg = "smart_battery/ must not import brand-specific modules (C-021)"
"custom_components.foxess_control._helpers".msg = "smart_battery/ must not import brand-specific modules (C-021)"
```

Note: TID251 is a global ban — it blocks these imports in ALL files, not just `smart_battery/`. This is actually desirable for the foxess→smart_battery direction (no file should import foxess internals from smart_battery), but may be too broad if brand-layer files legitimately cross-import. Test carefully.

If TID251 produces false positives, skip this step — semgrep already covers it with path-scoped rules.

- [ ] **Step 4: Run full pre-commit suite**

Run: `pre-commit run --all-files`
Expected: all hooks pass

- [ ] **Step 5: Commit**

```bash
git add .pre-commit-config.yaml pyproject.toml
git commit -m "Wire semgrep + module size checks into pre-commit"
```

---

### Task 4: Update Constraints and CLAUDE.md

**Files:**
- Modify: `docs/knowledge/02-constraints.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add new constraints to `02-constraints.md`**

Add under **Invariants — Testing** (or create a new **Invariants — Architecture** section if preferred):

```markdown
### C-034: Module size budget
**Statement**: No single `.py` file in `custom_components/foxess_control/`
may exceed 2000 lines. When a module approaches this limit, extract
cohesive functionality into a dedicated module.
**Rationale**: `__init__.py` grew to ~2500 lines by accretion before the
2026-04-21 remediation. Automated enforcement prevents recurrence.
**Violation consequence**: Pre-commit hook fails; code cannot be committed.
**Traces**: `.githooks/check-module-size`

### C-035: Typed config access
**Statement**: Config values must be read via `IntegrationConfig`
(accessed through `_cfg(hass)` in the brand layer), not via raw
`entry.options` access. New config fields must be added to
`IntegrationConfig` in `domain_data.py` before use.
**Rationale**: Raw `entry.options` access scatters default values and
type conversions across multiple files, creating inconsistency. The
`IntegrationConfig` frozen dataclass provides a single source of truth,
built once at setup time.
**Violation consequence**: Semgrep rule `no-raw-entry-options` fails
pre-commit.
**Traces**: `.semgrep/foxess-architecture.yaml::no-raw-entry-options`

### C-036: Typed domain data access
**Statement**: Runtime state in `hass.data[DOMAIN]` must be accessed via
the `_dd(hass)` helper, not by raw dict lookup. Only `__init__.py`
(setup/teardown), `_helpers.py` (helper definition), and `domain_data.py`
(type definition) may reference `hass.data[DOMAIN]` directly.
**Rationale**: Raw dict access was the source of the bridge-layer tech
debt. Typed access via `_dd()` catches key typos at lint time and
provides IDE autocomplete.
**Violation consequence**: Semgrep rule `no-raw-hass-data-access` fails
pre-commit.
**Traces**: `.semgrep/foxess-architecture.yaml::no-raw-hass-data-access`
```

- [ ] **Step 2: Update CLAUDE.md with key constraint references**

Add under **Key Constraints** after the existing sections:

```markdown
### Architecture
- **C-034**: No `.py` file in `custom_components/foxess_control/` exceeds 2000 lines
- **C-035**: Config via `IntegrationConfig` / `_cfg(hass)`, not raw `entry.options`
- **C-036**: Domain data via `_dd(hass)`, not raw `hass.data[DOMAIN]`
```

- [ ] **Step 3: Commit**

```bash
git add docs/knowledge/02-constraints.md CLAUDE.md
git commit -m "Add architectural constraints C-034, C-035, C-036 with automated enforcement"
```

---

### Task 5: Verify End-to-End

- [ ] **Step 1: Intentionally break each rule and confirm it's caught**

Create a temporary test file to verify each rule:

```python
# /tmp/test_lint_violations.py — DO NOT COMMIT
# Test 1: brand import in smart_battery
# Copy to smart_battery/test_violation.py, run semgrep, confirm ERROR

# Test 2: raw hass.data in sensor.py
# Add hass.data[DOMAIN] to sensor.py, run semgrep, confirm ERROR

# Test 3: module size
# Confirm .githooks/check-module-size correctly flags files over 2000 lines
```

Verify each violation is caught, then revert the test changes.

- [ ] **Step 2: Run the full test suite one final time**

Run: `pytest tests/ -m "not slow" --tb=short -q`
Expected: all pass

- [ ] **Step 3: Run pre-commit one final time**

Run: `pre-commit run --all-files`
Expected: all hooks pass

---

## Future Migrations (Not In Scope)

These are known gaps that the new rules intentionally don't enforce yet:

1. **`smart_battery/` entry.options access**: `sensor_base.py`, `listeners.py`, and `services.py` read `entry.options` directly because `smart_battery/` can't import `IntegrationConfig` from the brand layer (C-021). Fix: pass config values via constructor args when extracting `smart_battery` to a standalone package.

2. **Service handler shape detection**: The semgrep rule catches `async def f(..., call: ServiceCall, ...)` but not callback-style handlers that don't take `ServiceCall` directly. Acceptable — the naming convention and code review cover the edge cases.

3. **Cross-module dependency graph**: No enforcement that `_services.py` doesn't import from `coordinator.py` or vice versa. Could be added with `import-linter` if the dependency graph becomes complex enough to warrant it.
