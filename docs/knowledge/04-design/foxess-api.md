---
project: FoxESS Control
level: 4
feature: FoxESS Cloud API Integration
last_verified: 2026-04-18
traces_up: [../02-constraints.md, ../03-architecture.md]
traces_down: [../05-coverage.md, ../06-tests.md]
---
# Design: FoxESS Cloud API Integration

## Overview

The FoxESS Cloud API is the control plane for inverter mode management.
It has numerous undocumented behaviours and quirks that require careful
handling. These are documented in `API_DEVIATIONS.md` and encoded as
constraints and sanitisation logic.

## Design Decisions

### D-014: Schedule group sanitisation on read-before-write
**Decision**: Before writing schedule groups back to the API, strip
unknown fields, filter placeholders, clamp `fdSoc >= 11`, and ensure
`minSocOnGrid <= fdSoc`.
**Context**: The API's read endpoint returns groups with extra fields
and sometimes-invalid values that the write endpoint rejects.
**Rationale**: Defensive programming against an inconsistent API.
Without sanitisation, common operations (set force charge) fail silently.
**Alternatives considered**:
- Build groups from scratch: rejected because existing non-conflicting
  groups must be preserved
- Cache last-written state: rejected because the app/web portal may
  modify schedules between reads
**Traces**: C-008, C-010, C-011;
`tests/test_init.py::TestSanitizeGroup`,
`tests/test_init.py::TestMergeWithExisting`

### D-015: WASM signature generation
**Decision**: Use a pre-built WASM module (reverse-engineered from the
FoxESS web portal JavaScript) for request signing, loaded via `wasmtime`.
**Context**: The FoxESS web portal API requires a specific signature
header. The algorithm is obfuscated in the portal's JavaScript.
**Rationale**: WASM is the only reliable way to reproduce the exact
signature algorithm. Pure Python re-implementation would be fragile
against portal updates.
**Alternatives considered**:
- Pure Python port: rejected as too fragile; algorithm is obfuscated
- Headless browser: rejected as too heavy for HA environment
**Traces**: `tests/test_realtime_ws.py::TestGenerateSignature`

### D-016: Unmanaged mode protection
**Decision**: Service calls check for unmanaged modes (e.g., Backup) in
the existing schedule and raise a validation error rather than
overwriting them.
**Context**: Users may configure Backup mode via the FoxESS app for
outage protection. The integration assumes SelfUse as baseline.
**Rationale**: Silently overwriting a Backup schedule could leave the
home unprotected during an outage.
**Alternatives considered**:
- Force-overwrite with warning: rejected because the consequence
  (no backup during outage) is too severe
- Manage all modes: rejected as scope creep
**Traces**: `tests/test_init.py::TestMergeWithExisting::test_rejects_schedule_with_backup_mode`

## Key Behaviours

- Rate limit handling: errno 40400 retried up to `RATE_LIMIT_RETRIES`
  times with backoff.
- Transient HTTP errors (502, 503) retried up to `TRANSIENT_RETRIES`.
- Minimum request interval: 5 seconds between API calls.
- Device capacity cached after first query (avoids repeated API calls).

## Edge Cases

- **Null schedule response**: Some inverter modes (set via app) return
  null from `scheduler/get`. Normalised to empty list.
- **Past groups retained**: Groups with `endHour` in the past are kept
  because FoxESS schedules recur daily.
- **Full-day SelfUse baseline**: A 00:00-23:59 SelfUse group (default
  schedule) is dropped to make room for force actions.
