# FoxESS Control

## Build / Test / Lint

```bash
pytest tests/ --tb=short                    # all tests (unit + E2E)
pytest tests/ -m "not slow" --tb=short      # unit tests only (skip E2E)
pre-commit run --all-files                  # ruff + mypy
```

## Key Constraints

### Safety
- **C-001**: Discharge power floored at peak_consumption x 1.5 to prevent grid import
- **C-002**: Discharge suspends at or below min SoC
- **C-017**: End-of-discharge guard: suspend when energy can't sustain safety floor for 10 min
- **C-024**: Safe state on failure: 3 consecutive adapter errors abort session → self-use
- **C-025**: Session boundary cleanliness: all overrides removed before new session starts
- **C-027**: Schedule end time set to safe horizon (SoC/rate/safety_factor), not full window

### Data Integrity
- **C-003**: Session identity tokens prevent stale callback races
- **C-004**: WebSocket sends watts (strings); coordinator uses kW — divide by 1000
- **C-005**: WebSocket messages with timeDiff > 30s are stale — discard them
- **C-014**: Taper profiles auto-reset if plausibility check fails (median ratio <= 0.10)
- **C-015**: Vendored smart_battery/ must be byte-identical to canonical root copy

### Observability
- **C-020**: User must determine system state from UI alone — no log inspection required
- **C-022**: Unreachable charge target surfaced to user
- **C-026**: Persistent errors surfaced via sensor state, not just logs

### FoxESS API
- **C-008**: FoxESS API: fdSoc >= 11 and minSocOnGrid <= fdSoc
- **C-009**: FoxESS API: schedule windows must not cross midnight
- **C-018**: Refuse to modify schedule when unmanaged modes (e.g. Backup) are present
- **C-019**: Discharge SoC unavailability aborts session after 3 checks (matching charge C-012)

### Testing Infrastructure
- **C-028**: Use simulator over mocks; each test gets independent simulator instance
- **C-029**: E2E tests for HA-dependent behaviour (containerised HA + Playwright)
- **C-031**: No flaky tests — investigate root cause, don't skip/xfail/tune params

## Test Quality

- **No hardcoded sleeps in tests.** Use `wait_for_state()`, `wait_for_attribute()`, `wait_for_numeric_state()`, or Playwright's `expect()` instead of `time.sleep()` or `page.wait_for_timeout()`. If a deterministic wait is impossible, document why.
- **No blind exception swallowing in tests.** `except Exception: pass` hides real failures and makes flakes undiagnosable. Catch specific types, or at minimum log the exception.
- **No bare `page.reload()` in Playwright tests.** Use `_robust_reload()` (goto + networkidle) to avoid `net::ERR_ABORTED` races.
- **Prefer element waits over sleeps.** After reload or state change, wait for the specific DOM element or HA entity to update — not a fixed number of seconds.

## Process

- **Bug fixes**: invoke `/regression-test` BEFORE writing any fix. The test must fail against the current code before the fix is applied.
- **smart_battery/ edits**: ONLY edit the canonical root `smart_battery/`. Never edit the vendored copy under `custom_components/foxess_control/smart_battery/` directly — the pre-commit hook syncs it automatically.
- **Releases**: update `CHANGELOG.md` BEFORE bumping the version. The release workflow uses the changelog for release notes — an empty changelog means empty release notes.

## Architecture

Two-layer design: brand-agnostic `smart_battery/` core (pure pacing algorithms,
session management, sensors) + FoxESS-specific `foxess/` client (cloud API,
WebSocket, WASM signatures). `InverterAdapter` protocol is the abstraction boundary.

## Project Knowledge

See [`docs/knowledge/`](docs/knowledge/) for the full project knowledge tree:
vision, constraints, architecture, design decisions, test coverage, and gap analysis.
