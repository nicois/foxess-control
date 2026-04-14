# FoxESS Control

## Build / Test / Lint

```bash
pytest tests/ --tb=short
pre-commit run --all-files      # ruff + mypy
```

## Key Constraints

- **C-001**: Discharge power floored at peak_consumption x 1.5 to prevent grid import
- **C-002**: Discharge suspends at or below min SoC
- **C-003**: Session identity tokens prevent stale callback races
- **C-004**: WebSocket sends watts (strings); coordinator uses kW — divide by 1000
- **C-005**: WebSocket messages with timeDiff > 30s are stale — discard them
- **C-008**: FoxESS API: fdSoc >= 11 and minSocOnGrid <= fdSoc
- **C-009**: FoxESS API: schedule windows must not cross midnight
- **C-014**: Taper profiles auto-reset if plausibility check fails (median ratio <= 0.1)
- **C-015**: Vendored smart_battery/ must be byte-identical to canonical root copy

## Architecture

Two-layer design: brand-agnostic `smart_battery/` core (pure pacing algorithms,
session management, sensors) + FoxESS-specific `foxess/` client (cloud API,
WebSocket, WASM signatures). `InverterAdapter` protocol is the abstraction boundary.

## Project Knowledge

See [`docs/knowledge/`](docs/knowledge/) for the full project knowledge tree:
vision, constraints, architecture, design decisions, test coverage, and gap analysis.
