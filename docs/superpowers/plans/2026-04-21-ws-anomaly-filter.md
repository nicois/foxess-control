# WS Anomaly Filter: Move to Mapping Layer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the WS aberrant-message filter from `coordinator.inject_realtime_data()` into `FoxESSRealtimeWS`, comparing all power keys against the last accepted message so no aberrant values reach HA entity history.

**Architecture:** `FoxESSRealtimeWS` gains a `_last_accepted` dict tracking the most recent plausible mapped message. A pure function `_is_plausible(candidate, reference)` compares all power keys (battery, grid, solar, load) using the existing >10x ratio heuristic. The coordinator's divergence filter is removed — data quality is enforced at the source.

**Tech Stack:** Python, pytest, aiohttp

**Evidence from logs:** Aberrant WS messages have simultaneously wrong battery AND grid power (e.g. `batDischargePower` 0.53 vs 5.5, `feedinPower` 0.07 vs 5.02). `loadsPower` is coincidentally similar because real house load is ~0.46kW in both. Grid power fields are sometimes entirely missing in aberrant messages (gridStatus=4). SoC is never wrong.

---

### Task 1: Add `_is_plausible()` pure function + tests

**Files:**
- Modify: `custom_components/foxess_control/foxess/realtime_ws.py:27-28` (add function after `_to_kw`)
- Test: `tests/test_realtime_ws.py`

The function compares a candidate mapped dict against a reference (last accepted). It checks all power keys for >10x divergence. Same edge-case carve-outs as the existing coordinator filter: near-zero reference values (<=0.1kW) accept any change, zero candidate values always pass (genuine stop).

- [ ] **Step 1: Write failing tests for `_is_plausible`**

Add a new test class `TestIsPlausible` in `tests/test_realtime_ws.py`:

```python
from custom_components.foxess_control.foxess.realtime_ws import (
    FoxESSRealtimeWS,
    _is_plausible,
    map_ws_to_coordinator,
)
```

```python
class TestIsPlausible:
    """Plausibility filter: reject WS messages where any power key diverges >10x."""

    NORMAL = {
        "SoC": 83.0,
        "batChargePower": 0.0,
        "batDischargePower": 5.5,
        "pvPower": 0.0,
        "loadsPower": 0.48,
        "gridConsumptionPower": 0.0,
        "feedinPower": 5.02,
    }

    def test_similar_values_accepted(self) -> None:
        candidate = {**self.NORMAL, "batDischargePower": 5.49, "feedinPower": 5.01}
        assert _is_plausible(candidate, self.NORMAL) is True

    def test_aberrant_battery_rejected(self) -> None:
        """Battery power drops 10x while other fields look normal."""
        candidate = {**self.NORMAL, "batDischargePower": 0.53, "feedinPower": 0.07}
        assert _is_plausible(candidate, self.NORMAL) is False

    def test_aberrant_feedin_only_rejected(self) -> None:
        """Only feedinPower is aberrant — still rejected."""
        candidate = {**self.NORMAL, "feedinPower": 0.05}
        assert _is_plausible(candidate, self.NORMAL) is False

    def test_near_zero_reference_accepts_any(self) -> None:
        """When reference power is <=0.1kW, any candidate value is accepted (ramp-up)."""
        ref = {**self.NORMAL, "batDischargePower": 0.05}
        candidate = {**self.NORMAL, "batDischargePower": 5.5}
        assert _is_plausible(candidate, ref) is True

    def test_zero_candidate_always_accepted(self) -> None:
        """Candidate with zero power passes — genuine stop."""
        candidate = {**self.NORMAL, "batDischargePower": 0.0, "feedinPower": 0.0}
        assert _is_plausible(candidate, self.NORMAL) is True

    def test_no_reference_always_accepted(self) -> None:
        """First message (no reference) is always accepted."""
        assert _is_plausible(self.NORMAL, None) is True

    def test_empty_reference_always_accepted(self) -> None:
        """Reference with no power keys → accept."""
        assert _is_plausible(self.NORMAL, {"SoC": 80.0}) is True

    def test_missing_candidate_key_accepted(self) -> None:
        """If candidate lacks a power key present in reference, that's fine.

        The aberrant messages sometimes omit grid fields entirely (gridStatus=4).
        Missing keys should not be compared — only present keys can diverge.
        However, a message with MISSING grid fields alongside a wrong battery
        value will still be caught by the battery divergence.
        """
        candidate = {"SoC": 83.0, "batDischargePower": 5.5, "loadsPower": 0.48}
        assert _is_plausible(candidate, self.NORMAL) is True

    def test_charge_anomaly_rejected(self) -> None:
        """Same filter applies to charging direction."""
        ref = {**self.NORMAL, "batChargePower": 3.8, "batDischargePower": 0.0}
        candidate = {**ref, "batChargePower": 0.35}
        assert _is_plausible(candidate, ref) is False

    def test_solar_anomaly_rejected(self) -> None:
        """pvPower is also checked."""
        ref = {**self.NORMAL, "pvPower": 4.0}
        candidate = {**ref, "pvPower": 0.3}
        assert _is_plausible(candidate, ref) is False

    def test_load_anomaly_rejected(self) -> None:
        """loadsPower checked too — though in practice it's rarely aberrant."""
        ref = {**self.NORMAL, "loadsPower": 5.0}
        candidate = {**ref, "loadsPower": 0.4}
        assert _is_plausible(candidate, ref) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_realtime_ws.py::TestIsPlausible -v`
Expected: ImportError — `_is_plausible` doesn't exist yet.

- [ ] **Step 3: Implement `_is_plausible`**

Add this function in `realtime_ws.py` between `_to_kw` (line 58) and `map_ws_to_coordinator` (line 61), in the "Data mapping — pure function, no I/O" section:

```python
_POWER_KEYS = (
    "batChargePower",
    "batDischargePower",
    "pvPower",
    "loadsPower",
    "gridConsumptionPower",
    "feedinPower",
)


def _is_plausible(
    candidate: dict[str, Any], reference: dict[str, Any] | None
) -> bool:
    """Return False if any power key in *candidate* diverges >10x from *reference*.

    Edge cases preserved from the original coordinator-level filter:
    - *reference* is ``None`` or missing the key → accept (first message).
    - Reference value <= 0.1 kW → accept (ramp-up from near-zero).
    - Candidate value == 0 → accept (genuine stop).
    """
    if reference is None:
        return True
    for key in _POWER_KEYS:
        cand_val = candidate.get(key)
        ref_val = reference.get(key)
        if (
            cand_val is not None
            and ref_val is not None
            and ref_val > 0.1
            and cand_val > 0
            and (cand_val / ref_val > 10 or ref_val / cand_val > 10)
        ):
            _LOGGER.warning(
                "WS %s diverges >10x: candidate=%.4f, "
                "last_accepted=%.4f — dropping anomalous message",
                key,
                cand_val,
                ref_val,
            )
            return False
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_realtime_ws.py::TestIsPlausible -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/foxess/realtime_ws.py tests/test_realtime_ws.py
git commit -m "feat: add _is_plausible() WS anomaly filter function

Pure function that compares all power keys (battery, grid, solar, load)
against the last accepted message for >10x divergence. Logs evidence
from production: aberrant messages have simultaneously wrong battery
AND grid power."
```

---

### Task 2: Wire `_is_plausible` into `FoxESSRealtimeWS._listen_loop`

**Files:**
- Modify: `custom_components/foxess_control/foxess/realtime_ws.py:159-300` (class `FoxESSRealtimeWS`)
- Test: `tests/test_realtime_ws.py`

Add `_last_accepted` state to the WS client. Apply the plausibility check in `_listen_loop` after `map_ws_to_coordinator()` and before `_on_data()`. Reset `_last_accepted` on reconnect.

- [ ] **Step 1: Write failing test for WS-level filtering**

Add to `tests/test_realtime_ws.py`:

```python
class TestWsPlausibilityFilter:
    """FoxESSRealtimeWS drops aberrant messages before calling on_data."""

    @staticmethod
    def _make_ws_msg(
        discharge: float = 5500.0,
        feedin: float = 5000.0,
        load: float = 480.0,
        soc: int = 83,
        grid_status: int = 1,
    ) -> aiohttp.WSMessage:
        import json

        return aiohttp.WSMessage(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps(
                {
                    "errno": 0,
                    "result": {
                        "node": {
                            "solar": {"power": {"value": "0"}},
                            "grid": {
                                "power": {"value": str(feedin + load)},
                                "gridStatus": grid_status,
                            },
                            "bat": {
                                "power": {"value": str(discharge)},
                                "soc": soc,
                                "charge": 0,
                            },
                            "load": {"power": {"value": str(load)}},
                        },
                        "timeDiff": 5,
                    },
                }
            ),
            extra=None,
        )

    @pytest.mark.asyncio
    async def test_aberrant_message_not_forwarded(self) -> None:
        """Aberrant WS message (10x lower power) must not reach on_data."""
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")

        ws = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)

        messages = [
            self._make_ws_msg(discharge=5500, feedin=5000),   # normal — accepted
            self._make_ws_msg(discharge=530, feedin=70),       # aberrant — dropped
            self._make_ws_msg(discharge=5490, feedin=5010),   # normal — accepted
            aiohttp.WSMessage(type=aiohttp.WSMsgType.CLOSED, data=None, extra=None),
        ]

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(side_effect=messages)
        mock_ws.closed = True

        ws._ws = mock_ws
        ws._connected = True
        ws._stop_event.clear()

        with patch.object(ws, "_try_reconnect", new_callable=AsyncMock) as mock_reconnect:
            async def _fail_reconnect() -> None:
                ws._connected = False
            mock_reconnect.side_effect = _fail_reconnect
            await ws._listen_loop()

        assert on_data.call_count == 2, (
            f"Expected 2 calls (aberrant dropped), got {on_data.call_count}"
        )

    @pytest.mark.asyncio
    async def test_first_message_always_accepted(self) -> None:
        """First message after connect has no reference — must be accepted."""
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")

        ws = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)

        messages = [
            self._make_ws_msg(discharge=530, feedin=70),  # would be aberrant, but first msg
            aiohttp.WSMessage(type=aiohttp.WSMsgType.CLOSED, data=None, extra=None),
        ]

        mock_ws = AsyncMock()
        mock_ws.receive = AsyncMock(side_effect=messages)
        mock_ws.closed = True

        ws._ws = mock_ws
        ws._connected = True
        ws._stop_event.clear()

        with patch.object(ws, "_try_reconnect", new_callable=AsyncMock) as mock_reconnect:
            async def _fail_reconnect() -> None:
                ws._connected = False
            mock_reconnect.side_effect = _fail_reconnect
            await ws._listen_loop()

        assert on_data.call_count == 1

    @pytest.mark.asyncio
    async def test_reconnect_resets_reference(self) -> None:
        """After reconnect, _last_accepted is reset so first message is accepted."""
        on_data = AsyncMock()
        on_disconnect = MagicMock()
        web_session = AsyncMock()
        web_session.async_ensure_token = AsyncMock(return_value="tok")

        ws = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)
        # Simulate a prior accepted message
        ws._last_accepted = {
            "batDischargePower": 5.5,
            "feedinPower": 5.02,
            "loadsPower": 0.48,
        }

        # After reconnect, _last_accepted should be None
        # Simulate reconnect by checking the __init__ state
        ws2 = FoxESSRealtimeWS("plant1", web_session, on_data, on_disconnect)
        assert ws2._last_accepted is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_realtime_ws.py::TestWsPlausibilityFilter -v`
Expected: FAIL — `_last_accepted` attribute doesn't exist, aberrant messages still forwarded.

- [ ] **Step 3: Add `_last_accepted` and wire into `_listen_loop`**

In `FoxESSRealtimeWS.__init__` (line 178), add:

```python
        self._last_accepted: dict[str, Any] | None = None
```

In `_listen_loop`, replace the block at lines 293-299:

```python
            mapped = map_ws_to_coordinator(data)
            if mapped:
                self._last_useful_data = asyncio.get_event_loop().time()
                try:
                    await self._on_data(mapped)
                except Exception:
                    _LOGGER.debug("Error in WebSocket data callback", exc_info=True)
```

with:

```python
            mapped = map_ws_to_coordinator(data)
            if mapped:
                if not _is_plausible(mapped, self._last_accepted):
                    continue
                self._last_accepted = mapped
                self._last_useful_data = asyncio.get_event_loop().time()
                try:
                    await self._on_data(mapped)
                except Exception:
                    _LOGGER.debug("Error in WebSocket data callback", exc_info=True)
```

Also reset `_last_accepted` in `_do_connect` (after line 214, before the log line) so reconnections start fresh:

```python
        self._last_accepted = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_realtime_ws.py::TestWsPlausibilityFilter tests/test_realtime_ws.py::TestIsPlausible tests/test_realtime_ws.py::TestStaleness -v`
Expected: all PASS (including existing staleness tests still working).

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/foxess/realtime_ws.py tests/test_realtime_ws.py
git commit -m "feat: wire plausibility filter into WS listen loop

FoxESSRealtimeWS tracks _last_accepted and runs _is_plausible() before
forwarding to on_data. Aberrant messages are dropped before they reach
the coordinator. _last_accepted resets on reconnect."
```

---

### Task 3: Remove coordinator-level divergence filter + migrate tests

**Files:**
- Modify: `custom_components/foxess_control/coordinator.py:502-524` (remove divergence block)
- Modify: `tests/test_coordinator.py:1117-1307` (remove `TestWsDivergenceFiltering` class)

The coordinator no longer needs its own filter — `FoxESSRealtimeWS` handles data quality before data reaches the coordinator.

- [ ] **Step 1: Verify WS-level filter covers all coordinator test cases**

Cross-reference: the 6 coordinator tests map to WS-level tests as follows:
- `test_anomalous_ws_message_is_dropped` → `TestIsPlausible::test_aberrant_battery_rejected` + `TestWsPlausibilityFilter::test_aberrant_message_not_forwarded`
- `test_normal_ws_message_is_applied` → `TestIsPlausible::test_similar_values_accepted`
- `test_legitimate_large_change_accepted_when_both_sides_small` → `TestIsPlausible::test_near_zero_reference_accepts_any`
- `test_legitimate_stop_accepted_when_ws_value_is_zero` → `TestIsPlausible::test_zero_candidate_always_accepted`
- `test_charge_anomaly_also_dropped` → `TestIsPlausible::test_charge_anomaly_rejected`
- `test_divergence_warning_still_logged` → implicit in `_is_plausible` (it logs WARNING)

All cases are covered.

- [ ] **Step 2: Remove the divergence block from `coordinator.py`**

Remove lines 502-524 in `coordinator.py` (the `for key in ("batChargePower", ...):` block and its comment). The code after it (`# Skip if nothing actually changed`) remains.

The result should flow directly from line 500 (`ws_data["_data_last_update"] = ...`) to the change-detection gate (currently line 526).

- [ ] **Step 3: Remove `TestWsDivergenceFiltering` from `test_coordinator.py`**

Delete the entire class `TestWsDivergenceFiltering` (lines 1117-1307 inclusive).

- [ ] **Step 4: Run all tests**

Run: `pytest tests/test_coordinator.py tests/test_realtime_ws.py -v`
Expected: all PASS, no references to the removed coordinator filter remain.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/coordinator.py tests/test_coordinator.py
git commit -m "refactor: remove coordinator-level WS divergence filter

Now handled by FoxESSRealtimeWS._is_plausible() at the source, covering
all power keys (battery, grid, solar, load) instead of just battery."
```

---

### Task 4: Run full test suite + pre-commit

**Files:** none (verification only)

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -m "not slow" --tb=short`
Expected: all PASS.

- [ ] **Step 2: Run pre-commit**

Run: `pre-commit run --all-files`
Expected: all PASS (ruff, mypy, semgrep, module size).

- [ ] **Step 3: Fix any issues**

If anything fails, fix and re-run.
