# Scheduler Handback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the integration *release* the inverter when no session is
active — Mode Scheduler off, work mode set directly, the user's own Min SoC
restored — so users who drive the inverter by other means (local Modbus, the
FoxESS app) get it back in the state they left it.

**Architecture:** Sessions keep using the cloud scheduler, because forced
charge and discharge exist nowhere else. Handback governs only the *idle*
state between sessions. It is opt-in (default off), and it never chooses a
Min SoC of its own: it captures `MinSocOnGrid` before a session and restores
that exact value afterwards. Four new read/write surfaces go on `Inverter`;
the policy lives in the FoxESS layer because the scheduler is brand-specific.

**Tech Stack:** Python 3.13+, Home Assistant custom integration, `pytest`,
`aiohttp` simulator (`simulator/`), Playwright + containerised HA for E2E.

---

## Why this exists (evidence)

Two open issues are the same feature:

- **#16** (sofapi) — `clear_overrides` removes every schedule group but leaves
  the **Mode Scheduler master switch on**. FoxCloud still shows the inverter
  as scheduler-controlled, and their local Modbus writes to Self Use stay
  ineffective until they disable it by hand in FoxCloud. They asked for
  either `clear_overrides` to disable it, or a `disable_scheduler` action.
- **#4** (maciej84321) — firmware permits `minSOC = 0%` **only** outside the
  scheduler. Turn the scheduler on and it reverts to 10%, the schedule
  minimum, so the bottom of the battery becomes unusable. A commenter
  (dogz85) has already built a working "Scheduling Mode" switch locally.

Probed read-only against the owner's KH10 on 2026-08-26, which settles the
feasibility questions:

```
POST /op/v1/device/scheduler/get/flag   → {"enable": true, "support": true}
POST /op/v0/device/setting/get WorkMode → enumList [PeakShaving, Feedin, Backup, SelfUse]
                                          value "SelfUse"
POST /op/v0/device/setting/get MinSoc        → range 0-100, value "0"
POST /op/v0/device/setting/get MinSocOnGrid  → range 0-100, value "11"
```

Three consequences drive the whole design:

1. The master switch is readable and writable, so #16 is straightforward.
2. Work mode **is** directly settable off-scheduler, and `MinSocOnGrid`
   accepts **0** there. #4's premise is correct: the 10% floor is a
   *scheduler* restriction (the device declares `minsocongrid.range.min = 10`
   inside a schedule group — see C-042), not a hardware limit.
3. **The direct work-mode enum has no ForceCharge/ForceDischarge.** Smart
   charge and discharge therefore *must* keep using the scheduler. "Direct
   work mode instead of the scheduler" is only possible for the idle state.

## Decisions already made (do not relitigate)

- **Opt-in, default off.** Existing installs must behave exactly as today.
  The integration has hundreds of users; a behaviour change on upgrade that
  writes to their inverter is not acceptable as a default.
- **Restore-only Min SoC.** Capture `MinSocOnGrid` before a session, restore
  that value afterwards. The integration never *writes a floor of its own
  choosing*, so P-002 is never traded against — a user who sets 0% in the
  FoxESS app keeps it, and we cannot lower anyone's floor by accident. This
  is the same bug class as the shipped defect where an adapter wrote a
  session value into the persistent floor and never restored it, causing
  post-session grid import.

## Explicitly out of scope

- Changing how sessions themselves work. Charge/discharge keep writing
  schedule groups; only teardown and the idle state change.
- Entity mode. There is no cloud scheduler to hand back — skip cleanly.
- Exposing `MinSoc` (the off-grid/EPS reserve). Only `MinSocOnGrid` matters
  for #4, and touching the EPS reserve has safety implications nobody asked
  for.
- A `PeakShaving` / `Backup` idle mode. Self-use is what both issues want;
  more modes can come later behind the same option.

---

## File Structure

**Create:**
- `custom_components/foxess_control/handback.py` — the policy: decide whether
  to hand back, in what order, and what to restore. Pure functions plus one
  orchestrator, so the decision logic is testable without HTTP.
- `tests/test_handback_policy.py` — pure-policy unit tests.
- `tests/test_handback_foxess.py` — against the simulator, through the real
  `Inverter`.
- `tests/e2e/test_handback.py` — HA-visible behaviour.

**Modify:**
- `custom_components/foxess_control/foxess/inverter.py` — four new API
  methods (see Task 2). `get_min_soc` / `set_min_soc` already exist at lines
  321 and 328 and are reused for capture/restore.
- `custom_components/foxess_control/const.py` — `CONF_SCHEDULER_HANDBACK`,
  `DEFAULT_SCHEDULER_HANDBACK = False`, `SERVICE_DISABLE_SCHEDULER`.
- `custom_components/foxess_control/domain_data.py` — `scheduler_handback`
  on `IntegrationConfig`; `captured_min_soc_on_grid` on `FoxESSControlData`.
- `custom_components/foxess_control/config_flow.py` — the option.
- `custom_components/foxess_control/__init__.py` — call handback from
  `_async_remove_override` (line 237) and re-enable the scheduler before
  session writes.
- `custom_components/foxess_control/_services.py` + `services.yaml` — the
  `disable_scheduler` action.
- `custom_components/foxess_control/diagnostics.py` — handback state.
- `custom_components/foxess_control/strings.json` + `translations/*.json`.
- `simulator/model.py`, `simulator/server.py` — the flag and settings
  endpoints, and the master-switch semantics.
- `docs/api/foxess-cloud-api.md`, `docs/knowledge/`, `CHANGELOG.md`.

**Never edit** `custom_components/foxess_control/smart_battery/` — the
pre-commit hook syncs it (C-015).

---

### Task 1: Establish the master-switch semantics (do this first)

**The correctness risk in this whole feature.** If handback disables the
Mode Scheduler, the *next* session must be able to write schedule groups and
have them take effect. Does `POST /op/v0/device/scheduler/enable` turn the
master switch back on by itself, or does it write groups that sit inert
behind a disabled switch? Everything downstream depends on the answer, and
guessing it wrong means smart charge silently stops working for anyone who
enables the option.

Evidence so far: #16 reports the flag stays *on* after groups are removed,
so writing groups does not turn it off. Whether it turns it *on* from off is
unverified.

**Do not resolve this by writing to the live inverter.** The owner's device
is a production home battery.

- [ ] **Step 1: Model both behaviours in the simulator**

In `simulator/model.py`, add the master switch and make its coupling to
`scheduler/enable` a knob, so tests can pin *both* readings:

```python
    #: Mode Scheduler master switch, as reported by
    #: ``/op/v1/device/scheduler/get/flag``.
    scheduler_enabled: bool = True
    #: Whether the device supports the scheduler at all (``support`` in the
    #: flag response).  A batteryless micro-inverter reports False.
    scheduler_supported: bool = True
    #: Whether ``POST /op/v0/device/scheduler/enable`` implicitly turns the
    #: master switch on.  The real API's behaviour is unverified, so tests
    #: pin both: the integration must work either way.
    scheduler_enable_implies_on: bool = True
```

- [ ] **Step 2: Write the failing test**

```python
"""The integration must not depend on unverified API coupling."""


class TestSchedulerMasterSwitch:
    """Writing groups must work whether or not the write flips the switch."""

    def test_session_write_works_when_enable_is_implicit(self, foxess_sim) -> None:
        foxess_sim.set(scheduler_enabled=False, scheduler_enable_implies_on=True)
        inv = _make_inv(foxess_sim)
        inv.force_discharge(min_soc=20)
        assert foxess_sim.state()["scheduler_enabled"] is True
        assert _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)

    def test_session_write_works_when_enable_is_not_implicit(self, foxess_sim) -> None:
        """The dangerous case: groups written behind a disabled switch.

        If the real API behaves this way and the integration does not
        explicitly enable the switch, smart charge writes a schedule that
        never fires and the user sees nothing happen at all.
        """
        foxess_sim.set(scheduler_enabled=False, scheduler_enable_implies_on=False)
        inv = _make_inv(foxess_sim)
        inv.force_discharge(min_soc=20)
        assert foxess_sim.state()["scheduler_enabled"] is True, (
            "the integration must explicitly enable the Mode Scheduler before "
            "writing groups; otherwise a handback that disabled it leaves "
            "every later session inert"
        )
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/test_handback_foxess.py -k MasterSwitch -v`
Expected: the second test FAILS — nothing enables the switch today.

- [ ] **Step 4: Implement**

In `Inverter._post_schedule` (the single choke point every schedule write
already passes through — see C-042), ensure the switch is on before posting:

```python
        # A handback may have disabled the Mode Scheduler.  Whether
        # scheduler/enable implicitly re-enables it is not documented and
        # not verified against real hardware, so do it explicitly: the call
        # is idempotent and one extra request per session is cheap next to
        # a session that silently never fires.
        self.set_scheduler_enabled(True)
```

- [ ] **Step 5: Run to verify both pass**

Run: `pytest tests/test_handback_foxess.py -k MasterSwitch -v`
Expected: 2 passed.

- [ ] **Step 6: Document what is and isn't known**

Add to `docs/api/foxess-cloud-api.md`, near the `scheduler/enable` section:

```markdown
**Master switch.** `POST /op/v1/device/scheduler/get/flag` returns
`{"enable": bool, "support": bool}` — whether Mode Scheduler is on, and
whether the device supports it at all. `POST /op/v0/device/scheduler/set`
with `{"deviceSN": …, "enable": 0|1}` sets it.

Removing every group does **not** turn the switch off (issue #16: FoxCloud
still showed the inverter as scheduler-controlled with no groups left).
Whether `scheduler/enable` turns the switch *on* from off is **unverified**
against real hardware — so the integration enables it explicitly before
writing groups rather than relying on the coupling.
```

- [ ] **Step 7: Commit**

```bash
git add simulator/ tests/test_handback_foxess.py \
        custom_components/foxess_control/foxess/inverter.py \
        docs/api/foxess-cloud-api.md
git commit -m "fix: enable Mode Scheduler explicitly before writing groups

Schedule writes must not depend on undocumented coupling between
scheduler/enable and the master switch."
```

---

### Task 2: The four new `Inverter` API surfaces

**Files:**
- Modify: `custom_components/foxess_control/foxess/inverter.py`
- Modify: `simulator/server.py`, `simulator/model.py`
- Test: `tests/test_handback_foxess.py`

- [ ] **Step 1: Write the failing test**

```python
class TestSchedulerFlag:
    def test_reads_the_master_switch(self, foxess_sim) -> None:
        foxess_sim.set(scheduler_enabled=True, scheduler_supported=True)
        inv = _make_inv(foxess_sim)
        flag = inv.get_scheduler_flag()
        assert flag == {"enable": True, "support": True}

    def test_reports_unsupported_hardware(self, foxess_sim) -> None:
        """A device with no scheduler must be detectable, not assumed."""
        foxess_sim.set(scheduler_supported=False)
        assert inv_flag(foxess_sim)["support"] is False

    def test_disables_the_master_switch(self, foxess_sim) -> None:
        foxess_sim.set(scheduler_enabled=True)
        inv = _make_inv(foxess_sim)
        inv.set_scheduler_enabled(False)
        assert foxess_sim.state()["scheduler_enabled"] is False


class TestDirectSettings:
    def test_reads_work_mode_and_its_enumeration(self, foxess_sim) -> None:
        inv = _make_inv(foxess_sim)
        setting = inv.get_setting("WorkMode")
        assert setting["value"] == "SelfUse"
        assert "SelfUse" in setting["enumList"]
        assert "ForceDischarge" not in setting["enumList"], (
            "the direct work-mode enum has no forced modes; if this ever "
            "changes, sessions could stop needing the scheduler"
        )

    def test_sets_work_mode_directly(self, foxess_sim) -> None:
        inv = _make_inv(foxess_sim)
        inv.set_work_mode_direct("SelfUse")
        assert foxess_sim.state()["work_mode_direct"] == "SelfUse"

    def test_refuses_a_mode_the_device_does_not_declare(self, foxess_sim) -> None:
        """Never write a mode outside the declared enumeration (C-042 spirit)."""
        inv = _make_inv(foxess_sim)
        with pytest.raises(ValueError, match="ForceDischarge"):
            inv.set_work_mode_direct("ForceDischarge")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_handback_foxess.py -v`
Expected: FAIL — `AttributeError: 'Inverter' object has no attribute 'get_scheduler_flag'`.

- [ ] **Step 3: Implement**

```python
    # --- Mode Scheduler master switch ---

    def get_scheduler_flag(self) -> dict[str, bool]:
        """Whether Mode Scheduler is enabled, and whether it is supported.

        ``support`` is False on hardware with no scheduler at all (e.g. a
        batteryless micro-inverter), which is worth knowing before trying
        to control it.
        """
        result: dict[str, Any] = self.client.post(
            "/op/v1/device/scheduler/get/flag", {"deviceSN": self.sn}
        )
        return {
            "enable": bool(result.get("enable")),
            "support": bool(result.get("support")),
        }

    def set_scheduler_enabled(self, enable: bool) -> None:
        """Turn the Mode Scheduler master switch on or off.

        Off hands the inverter back to whatever else drives it — the FoxESS
        app, local Modbus — and is the only way `MinSocOnGrid` below the
        schedule minimum can hold (issues #16, #4).
        """
        self.client.post(
            "/op/v0/device/scheduler/set",
            {"deviceSN": self.sn, "enable": 1 if enable else 0},
        )

    # --- Direct settings (off-scheduler) ---

    def get_setting(self, key: str) -> dict[str, Any]:
        """Read one inverter setting, with its declared range/enumeration."""
        result: dict[str, Any] = self.client.post(
            "/op/v0/device/setting/get", {"sn": self.sn, "key": key}
        )
        return result

    def set_setting(self, key: str, value: str) -> None:
        """Write one inverter setting."""
        self.client.post(
            "/op/v0/device/setting/set",
            {"sn": self.sn, "key": key, "value": value},
        )

    def set_work_mode_direct(self, mode: str) -> None:
        """Set the work mode *without* the scheduler.

        Deliberately named to be impossible to confuse with
        :meth:`set_work_mode`, which writes a schedule group.  The direct
        enumeration has no forced modes, so this can only govern the idle
        state.
        """
        declared = self.get_setting("WorkMode").get("enumList") or []
        if declared and mode not in declared:
            raise ValueError(
                f"work mode {mode!r} is not in the device's declared "
                f"enumeration {sorted(declared)}"
            )
        self.set_setting("WorkMode", mode)
```

Add the matching simulator endpoints (`/op/v1/device/scheduler/get/flag`,
`/op/v0/device/scheduler/set`, `/op/v0/device/setting/get`,
`/op/v0/device/setting/set`) with `work_mode_direct` and
`min_soc_on_grid_setting` on the model, and make `setting/get` return the
declared ranges the live device does (`WorkMode` enumList, `MinSocOnGrid`
range 0-100).

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_handback_foxess.py -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/foxess/inverter.py simulator/ tests/test_handback_foxess.py
git commit -m "feat: scheduler-flag and direct-setting API surfaces on Inverter"
```

---

### Task 3: The handback policy, as pure functions

**Files:**
- Create: `custom_components/foxess_control/handback.py`
- Test: `tests/test_handback_policy.py`

- [ ] **Step 1: Write the failing test**

```python
"""When to hand back, and what to restore.

Pure decision logic, so every guard is testable without HTTP or HA.
"""

from custom_components.foxess_control.handback import (
    HandbackPlan,
    plan_handback,
)


class TestPlanHandback:
    def test_disabled_by_default_does_nothing(self) -> None:
        plan = plan_handback(
            enabled=False, entity_mode=False, session_active=False,
            unmanaged_modes=[], scheduler_supported=True,
            captured_min_soc_on_grid=11,
        )
        assert plan == HandbackPlan(act=False, reason="option disabled")

    def test_hands_back_when_idle_and_enabled(self) -> None:
        plan = plan_handback(
            enabled=True, entity_mode=False, session_active=False,
            unmanaged_modes=[], scheduler_supported=True,
            captured_min_soc_on_grid=0,
        )
        assert plan.act is True
        assert plan.disable_scheduler is True
        assert plan.work_mode == "SelfUse"
        assert plan.restore_min_soc_on_grid == 0

    def test_never_during_an_active_session(self) -> None:
        """Disabling the scheduler mid-session would strand the override."""
        plan = plan_handback(
            enabled=True, entity_mode=False, session_active=True,
            unmanaged_modes=[], scheduler_supported=True,
            captured_min_soc_on_grid=11,
        )
        assert plan.act is False
        assert "session" in plan.reason

    def test_never_with_unmanaged_modes_present(self) -> None:
        """C-018: do not touch a schedule carrying modes we do not manage."""
        plan = plan_handback(
            enabled=True, entity_mode=False, session_active=False,
            unmanaged_modes=["Backup"], scheduler_supported=True,
            captured_min_soc_on_grid=11,
        )
        assert plan.act is False
        assert "Backup" in plan.reason

    def test_skips_entity_mode(self) -> None:
        plan = plan_handback(
            enabled=True, entity_mode=True, session_active=False,
            unmanaged_modes=[], scheduler_supported=True,
            captured_min_soc_on_grid=11,
        )
        assert plan.act is False

    def test_skips_hardware_without_a_scheduler(self) -> None:
        plan = plan_handback(
            enabled=True, entity_mode=False, session_active=False,
            unmanaged_modes=[], scheduler_supported=False,
            captured_min_soc_on_grid=11,
        )
        assert plan.act is False

    def test_never_invents_a_min_soc(self) -> None:
        """Nothing captured means restore nothing — never guess a floor.

        The integration must not pick a floor of its own: that is how a
        previous defect wrote a session value into the persistent floor and
        caused post-session grid import.
        """
        plan = plan_handback(
            enabled=True, entity_mode=False, session_active=False,
            unmanaged_modes=[], scheduler_supported=True,
            captured_min_soc_on_grid=None,
        )
        assert plan.act is True
        assert plan.restore_min_soc_on_grid is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_handback_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: ...handback`.

- [ ] **Step 3: Implement**

```python
"""Policy for releasing the inverter when no session is active.

Sessions require the cloud scheduler — the direct work-mode enumeration has
no forced modes — so this governs only the idle state between sessions.
Refs issues #16 and #4, C-018 (unmanaged modes), C-025 (session boundary
cleanliness), P-002 (respect minimum state of charge).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HandbackPlan:
    """What handback should do, and why."""

    act: bool
    reason: str = ""
    disable_scheduler: bool = False
    work_mode: str | None = None
    restore_min_soc_on_grid: int | None = None


def plan_handback(
    *,
    enabled: bool,
    entity_mode: bool,
    session_active: bool,
    unmanaged_modes: list[str],
    scheduler_supported: bool,
    captured_min_soc_on_grid: int | None,
) -> HandbackPlan:
    """Decide whether to hand the inverter back, and what to restore."""
    if not enabled:
        return HandbackPlan(act=False, reason="option disabled")
    if entity_mode:
        return HandbackPlan(act=False, reason="entity mode has no cloud scheduler")
    if session_active:
        return HandbackPlan(
            act=False,
            reason="a session is active — disabling the scheduler would strand it",
        )
    if unmanaged_modes:
        return HandbackPlan(
            act=False,
            reason=(
                "unmanaged modes present, refusing to modify the schedule "
                f"(C-018): {', '.join(sorted(unmanaged_modes))}"
            ),
        )
    if not scheduler_supported:
        return HandbackPlan(act=False, reason="device does not support the scheduler")
    return HandbackPlan(
        act=True,
        reason="idle and handback enabled",
        disable_scheduler=True,
        work_mode="SelfUse",
        # None means "we captured nothing, so restore nothing".  Never
        # substitute a default: choosing a floor is the user's business.
        restore_min_soc_on_grid=captured_min_soc_on_grid,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_handback_policy.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/handback.py tests/test_handback_policy.py
git commit -m "feat: handback policy with session, C-018 and capability guards"
```

---

### Task 4: Capture and restore the user's Min SoC

**Files:**
- Modify: `custom_components/foxess_control/handback.py`, `domain_data.py`, `__init__.py`
- Test: `tests/test_handback_foxess.py`

- [ ] **Step 1: Write the failing test**

```python
class TestMinSocCaptureRestore:
    """The user's own floor must survive a session unchanged."""

    def test_users_zero_floor_survives_a_session(self, foxess_sim) -> None:
        """Issue #4: a 0% floor set in the FoxESS app must come back.

        Today the session writes the schedule minimum and leaves it there,
        so the bottom of the battery becomes permanently unusable.
        """
        foxess_sim.set(min_soc_on_grid_setting=0)
        captured = capture_min_soc_on_grid(_make_inv(foxess_sim))
        assert captured == 0
        # ...session runs, writing the schedule minimum...
        _make_inv(foxess_sim).force_discharge(min_soc=20)
        assert foxess_sim.state()["min_soc_on_grid_setting"] != 0
        restore_min_soc_on_grid(_make_inv(foxess_sim), captured)
        assert foxess_sim.state()["min_soc_on_grid_setting"] == 0

    def test_restore_is_a_noop_when_nothing_was_captured(self, foxess_sim) -> None:
        foxess_sim.set(min_soc_on_grid_setting=11)
        restore_min_soc_on_grid(_make_inv(foxess_sim), None)
        assert foxess_sim.state()["min_soc_on_grid_setting"] == 11

    def test_capture_failure_does_not_break_the_session(self, foxess_sim) -> None:
        """Capture is best-effort: a failed read must not abort a session."""
        foxess_sim.model_fault("unreachable", count=99)
        assert capture_min_soc_on_grid(_make_inv(foxess_sim)) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_handback_foxess.py -k MinSoc -v`
Expected: FAIL — the helpers do not exist.

- [ ] **Step 3: Implement**

Add to `handback.py` (reusing the existing `get_min_soc` / `set_min_soc` at
`inverter.py:321,328`):

```python
def capture_min_soc_on_grid(inverter: Any) -> int | None:
    """Read the user's on-grid floor before a session overwrites it.

    Best-effort: returns None on any failure, and None means "restore
    nothing later" rather than "restore a default".
    """
    try:
        return int(inverter.get_min_soc()["minSocOnGrid"])
    except Exception:  # noqa: BLE001 — capture must never abort a session
        _LOGGER.debug("Could not capture minSocOnGrid", exc_info=True)
        return None


def restore_min_soc_on_grid(inverter: Any, value: int | None) -> None:
    """Put the captured floor back.  None is a deliberate no-op."""
    if value is None:
        return
    inverter.set_min_soc(min_soc_on_grid=value)
```

Persist the captured value in the existing `Store` (`dd.store`, created in
`__init__.py:1330`) so it survives a restart mid-session, and add
`captured_min_soc_on_grid: int | None = None` to `FoxESSControlData`.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_handback_foxess.py -k MinSoc -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/ tests/test_handback_foxess.py
git commit -m "feat: capture and restore the user's own minSocOnGrid"
```

---

### Task 5: The opt-in option

**Files:**
- Modify: `const.py`, `domain_data.py`, `config_flow.py`, `strings.json`, `translations/*.json`
- Test: `tests/test_handback_policy.py`, plus the existing options-flow test module

- [ ] **Step 1: Write the failing test**

```python
class TestHandbackOption:
    def test_defaults_to_off(self) -> None:
        from custom_components.foxess_control.domain_data import build_config

        assert build_config({}).scheduler_handback is False

    def test_reads_the_option(self) -> None:
        from custom_components.foxess_control.const import CONF_SCHEDULER_HANDBACK
        from custom_components.foxess_control.domain_data import build_config

        cfg = build_config({CONF_SCHEDULER_HANDBACK: True})
        assert cfg.scheduler_handback is True

    def test_option_appears_in_the_options_flow(self) -> None:
        """Users must be able to find it without editing YAML (C-020)."""
        from custom_components.foxess_control.config_flow import _options_schema
        from custom_components.foxess_control.const import CONF_SCHEDULER_HANDBACK

        keys = {str(k) for k in _options_schema({}).schema}
        assert CONF_SCHEDULER_HANDBACK in keys
```

Read `config_flow.py` first — use whatever the module actually names its
schema builder rather than the placeholder above.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_handback_policy.py -k Option -v`
Expected: FAIL — `AttributeError: ... 'scheduler_handback'`.

- [ ] **Step 3: Implement**

`const.py`:

```python
CONF_SCHEDULER_HANDBACK = "scheduler_handback"
DEFAULT_SCHEDULER_HANDBACK = False
SERVICE_DISABLE_SCHEDULER = "disable_scheduler"
```

Add `scheduler_handback: bool` to `IntegrationConfig`, read it in
`build_config`, add a `vol.Optional` boolean to the options schema, and add
the label/description to `strings.json` plus every `translations/*.json`:

```json
"scheduler_handback": "Release the inverter when idle",
"scheduler_handback_description": "When no smart session is running, turn off the inverter's Mode Scheduler, set Self Use directly, and restore the minimum SoC you had configured. Enable this if you also control the inverter another way (the FoxESS app, local Modbus), or if you want a minimum SoC below the 10% the scheduler enforces. Off by default; smart charge and discharge still use the scheduler while they run."
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_handback_policy.py -k Option -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/ tests/
git commit -m "feat: scheduler_handback option, default off"
```

---

### Task 6: Wire handback into session teardown

**Files:**
- Modify: `custom_components/foxess_control/__init__.py` (`_async_remove_override`, line 237)
- Test: `tests/test_handback_foxess.py`

- [ ] **Step 1: Write the failing test**

```python
class TestTeardownHandsBack:
    def test_teardown_disables_the_scheduler_when_enabled(self, foxess_sim) -> None:
        """Issue #16: after clear_overrides the switch must be off."""
        ...  # drive the real teardown path with the option on
        assert foxess_sim.state()["scheduler_enabled"] is False
        assert foxess_sim.state()["work_mode_direct"] == "SelfUse"

    def test_teardown_leaves_the_switch_alone_when_disabled(self, foxess_sim) -> None:
        """Default installs must behave exactly as they do today."""
        ...
        assert foxess_sim.state()["scheduler_enabled"] is True

    def test_handback_failure_does_not_break_teardown(self, foxess_sim) -> None:
        """C-025: the override must still come off if handback fails.

        Handback is a convenience; leaving a forced-discharge group in place
        because the extra write failed would be a safety regression.
        """
        ...
        assert not _written_groups(foxess_sim, WorkMode.FORCE_DISCHARGE)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_handback_foxess.py -k Teardown -v`

- [ ] **Step 3: Implement**

In `_async_remove_override`, after the existing group removal, run handback
in the executor. Order matters and must be exactly: remove groups → disable
the switch → set work mode → restore the floor. Removing groups first means
a failure part-way through never leaves a *forced* mode active behind a
disabled scheduler. Wrap the handback portion so a failure logs and records
an operational error (D-059, `record_operational_error`) without propagating.

- [ ] **Step 4: Run to verify it passes**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: hand the inverter back at session teardown (issues #16, #4)"
```

---

### Task 7: The `disable_scheduler` action

Issue #16 asked for this explicitly, and it gives users a manual lever
regardless of the option.

**Files:**
- Modify: `_services.py`, `services.yaml`, `strings.json`, `translations/*.json`
- Test: `tests/test_handback_foxess.py`

- [ ] **Step 1: Write the failing test**

```python
class TestDisableSchedulerAction:
    async def test_action_is_registered(self, hass, setup_integration) -> None:
        assert hass.services.has_service("foxess_control", "disable_scheduler")

    async def test_action_disables_the_switch(self, hass, setup_integration, foxess_sim) -> None:
        await hass.services.async_call(
            "foxess_control", "disable_scheduler", {}, blocking=True
        )
        assert foxess_sim.state()["scheduler_enabled"] is False

    async def test_action_refuses_during_a_session(self, hass, setup_integration, foxess_sim) -> None:
        """Same guard as automatic handback — no stranded overrides."""
        ...
        assert foxess_sim.state()["scheduler_enabled"] is True
```

- [ ] **Step 2-5:** RED, implement (`hass.services.async_register`, the
`services.yaml` entry with no fields, translations), GREEN, commit.

The action must apply the same `plan_handback` guards rather than writing
directly — one policy, one place.

---

### Task 8: Diagnostics

**Files:**
- Modify: `custom_components/foxess_control/diagnostics.py`
- Test: `tests/test_diagnostics.py`

- [ ] **Step 1: Write the failing test**

```python
    async def test_handback_section_present(self, hass, setup_integration) -> None:
        result = await async_get_config_entry_diagnostics(hass, entry)
        hb = result["handback"]
        assert "enabled" in hb
        assert "scheduler_flag" in hb
        assert "captured_min_soc_on_grid" in hb
```

- [ ] **Step 2-5:** RED, add a `handback` section (option state, last-known
flag, captured floor, last action and outcome), GREEN, commit.

Without this, a "my scheduler keeps turning off" report is undiagnosable.

---

### Task 9: E2E coverage

**Files:**
- Create: `tests/e2e/test_handback.py`

- [ ] **Step 1: Write the failing test**

```python
pytestmark = pytest.mark.slow


class TestHandbackE2E:
    async def test_idle_release_after_a_discharge_session(
        self, ha, simulator, connection_mode
    ) -> None:
        """The user-visible contract for issues #16 and #4."""
        if connection_mode != "cloud":
            pytest.skip("handback is a cloud-scheduler concept")
        simulator.set(min_soc_on_grid_setting=0)
        await ha.set_option("scheduler_handback", True)
        ...  # run a short smart_discharge to completion
        await ha.wait_for_condition(
            lambda: simulator.state()["scheduler_enabled"] is False,
            description="Mode Scheduler to be released after the session",
        )
        assert simulator.state()["min_soc_on_grid_setting"] == 0, (
            "the user's 0% floor must be restored, not left at the "
            "schedule minimum"
        )

    async def test_next_session_still_works_after_a_handback(
        self, ha, simulator, connection_mode
    ) -> None:
        """The regression that would matter most: sessions must still fire."""
        ...
```

Use `wait_for_condition` — no sleeps (C-031). Run `-n auto`.

- [ ] **Step 2-5:** RED, implement any missing simulator/HA-client helpers,
GREEN, commit.

---

### Task 10: Docs, constraint, changelog

- [ ] **Step 1:** `docs/api/foxess-cloud-api.md` — the settings endpoints
  and their declared ranges; that the direct work-mode enum has no forced
  modes, which is *why* sessions need the scheduler.
- [ ] **Step 2:** `docs/control/smart-discharge-contract.md` and the charge
  contract — note that teardown may release the scheduler.
- [ ] **Step 3:** Propose a constraint (check the highest allocated ID —
  C-043 is taken):

```markdown
- **C-0NN**: Handback restores captured user settings only — the integration
  never writes a `MinSocOnGrid` of its own choosing. A value that was not
  captured is not restored. Enforces P-002 without trading against it: we
  cannot lower a user's floor, and cannot leave a session's floor behind.
```

  Add a coverage row in `docs/knowledge/05-coverage.md` and a test-inventory
  section in `06-tests.md`.
- [ ] **Step 4:** README — the option, what it does, and who wants it.
- [ ] **Step 5:** `CHANGELOG.md` under `## Unreleased` (create the heading;
  do not rename an existing release). Do **not** bump the version or tag.
- [ ] **Step 6:** Commit.

---

## Final verification

- [ ] `pytest tests/ -m "not slow" --tb=short` — green (baseline 1448 at `67fd3f9`)
- [ ] `pytest tests/e2e/ -m slow -n auto` — green (baseline 131 passed / 39 skipped)
- [ ] `pre-commit run --all-files` — 13 hooks
- [ ] Confirm no `.py` in `custom_components/foxess_control/` exceeds 2000 lines (C-034); `__init__.py` was 1755 before this work
- [ ] Confirm the vendored `smart_battery/` copy was synced by the hook, not by hand (C-015)
- [ ] **Default installs unchanged:** with the option off, no new API write occurs at teardown. Assert this explicitly — it is the upgrade-safety guarantee for hundreds of users.
- [ ] Do not bump the version or create a tag

## Self-review notes

- **Spec coverage:** #16 is closed by Tasks 6 and 7; #4 by Tasks 4 and 6
  (its 0% floor survives, because handback leaves the scheduler off and
  restores the captured value). Opt-in is Task 5; restore-only is Task 4.
- **Type consistency:** `HandbackPlan`, `plan_handback()`,
  `capture_min_soc_on_grid()`, `restore_min_soc_on_grid()`,
  `get_scheduler_flag()`, `set_scheduler_enabled()`, `get_setting()`,
  `set_setting()`, `set_work_mode_direct()` are used with those exact names
  throughout.
- **Ordering dependency:** Task 1 must land first. Everything else risks
  making sessions inert if the master-switch semantics are wrong.
- **Naming hazard:** `set_work_mode` (existing) writes a *schedule group*;
  `set_work_mode_direct` (new) writes the *setting*. Confusing them would
  silently move control between the two systems, which is the whole subject
  of this feature — keep the names far apart and say so in the docstrings.
- **Merge dependency:** Tasks 5, 6 and 8 touch `const.py`, `__init__.py`,
  `diagnostics.py` and the options flow, which the capability-probe plan
  (`docs/plans/2026-08-26-capability-probe.md`) also touches. Land one
  feature fully before starting the other.
