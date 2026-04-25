# FoxESS Control

## Build / Test / Lint

```bash
pytest tests/ --tb=short                    # all tests (unit + E2E)
pytest tests/ -m "not slow" --tb=short      # unit tests only (skip E2E)
pre-commit run --all-files                  # ruff + mypy + semgrep + module size
```

## Priorities (strict order — lower ID wins when goals conflict)

- **P-001**: No grid import during forced discharge
- **P-002**: Respect minimum state of charge
- **P-003**: Meet the user's energy target
- **P-004**: Maximise feed-in revenue
- **P-005**: Operational transparency
- **P-006**: Brand portability
- **P-007**: Engineering process integrity

See `docs/knowledge/01-vision.md` for full P-NNN definitions. Every
C-NNN cites the P-NNN it enforces; every D-NNN cites the P-NNN it
serves and any lower-priority goal it trades against.

## Key Constraints

### Safety
- **C-001**: Discharge power floored at peak_consumption x 1.5 to prevent grid import
- **C-002**: Discharge suspends at or below min SoC
- **C-017**: End-of-discharge guard: suspend when energy can't sustain safety floor for 10 min
- **C-024**: Safe state on failure: 3 consecutive adapter errors open circuit breaker (hold position). 5 more ticks without recovery → abort session → self-use
- **C-025**: Session boundary cleanliness: all overrides removed before new session starts
- **C-027**: Schedule end time set to safe horizon (SoC/rate/safety_factor), not full window
- **C-037**: Grid export limit awareness: deferral caps effective export rate at the configured hardware limit; active discharge requests max power and lets the hardware actuator enforce (D-047)

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
- **C-038**: Sensor display formulas must call the same algorithm functions as listeners, with the same parameter lists (prevents UI/listener phase divergence)

### FoxESS API
- **C-008**: FoxESS API: fdSoc >= 11 and minSocOnGrid <= fdSoc
- **C-009**: FoxESS API: schedule windows must not cross midnight
- **C-018**: Refuse to modify schedule when unmanaged modes (e.g. Backup) are present
- **C-019**: Discharge SoC unavailability aborts session after 3 checks (matching charge C-012)

### Architecture
- **C-021**: Brand-agnostic code belongs in `smart_battery/`; brand-specific code must not live there
- **C-039**: `smart_battery/` must not import from brand-layer modules — dependency inversion via the `InverterAdapter` Protocol (semgrep-enforced). See `docs/knowledge/03-architecture.md` §Adding a New Brand.
- **C-040**: Brand-agnostic code has brand-agnostic tests — use `smart_battery.testing.FakeAdapter` (or the `fake_adapter` fixture), never a brand-specific adapter. See `tests/test_smart_battery_agnostic.py`.
- **C-034**: No `.py` file in `custom_components/foxess_control/` exceeds 2000 lines
- **C-035**: Config via `IntegrationConfig` / `_cfg(hass)`, not raw `entry.options`
- **C-036**: Domain data via `_dd(hass)`, not raw `hass.data[DOMAIN]`

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
- **New features / parameters**: invoke `superpowers:test-driven-development` BEFORE writing implementation code. New parameters added to existing tested functions must have corresponding test cases in the same commit. If the function already has a test class, add cases there; if not, create one.
- **smart_battery/ edits**: ONLY edit the canonical root `smart_battery/`. Never edit the vendored copy under `custom_components/foxess_control/smart_battery/` directly — the pre-commit hook syncs it automatically.
- **Releases**: update `CHANGELOG.md` BEFORE bumping the version. The release workflow uses the changelog for release notes — an empty changelog means empty release notes.

## Architecture

Two-layer design: brand-agnostic `smart_battery/` core (pure pacing algorithms,
session management, sensors) + FoxESS-specific `foxess/` client (cloud API,
WebSocket, WASM signatures). `InverterAdapter` protocol is the abstraction boundary.

## Project Knowledge

See [`docs/knowledge/`](docs/knowledge/) for the full project knowledge tree:
vision, constraints, architecture, design decisions, test coverage, and gap analysis.
