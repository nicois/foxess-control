# Inverter Capability Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Probe the inverter's full capability surface once at first run (and after
each integration upgrade), classify the install as usable / degraded / unusable,
surface a degraded install to the user once via a Repair issue, and put the probe
result into diagnostics so a user can hand back a scrubbed hardware report in one
click.

**Architecture:** A brand-agnostic `CapabilityReport` dataclass plus a pure
classifier live in `smart_battery/capabilities.py`. `InverterAdapter` gains an
`async_probe_capabilities()` method; the FoxESS cloud adapter implements it with
three **read-only** API calls (`real/query` with `variables: []` for the complete
device variable set, `device/detail` for model/capacity, `scheduler/get` for the
control plane). The report is persisted in the existing `Store` and re-probed only
when there is no stored report, when the integration version has changed, or when
the user asks for a rescan via a new action. Classification drives a Repair issue;
the report is embedded in diagnostics.

**Tech Stack:** Python 3.13+, Home Assistant custom integration, `pytest`,
`aiohttp`-based simulator (`simulator/`), Playwright + containerised HA for E2E.

---

## Why this exists (evidence)

Probing `POST /op/v0/device/real/query` with `variables: []` against a live KH10 on
2026-08-26 returned **64** variables. `POLLED_VARIABLES` requests **27**. Every
polled variable was present, and **37** were available but unpolled:

```
PVEnergyTotal  RPower  RemainingPowerCapability  ResidualEnergy  SOH
batChargeEnergyToTal  batCycleCount  batDischargeEnergyToTal  batStatus
batStatusV2  currentFault  currentFaultCount  epsCurrentR  epsPowerR  epsVoltR
feedin2  gridConsumption2  invBatCurrent  invBatPower  invBatVolt
maxChargeCurrent  maxDischargeCurrent  meterPower2  pv1Current  pv1Volt
pv2Current  pv2Volt  pv3Current  pv3Power  pv3Volt  pv4Current  pv4Power
pv4Volt  remainCapacity  runningState  totalDischargeAh  totalDischargeKW
```

`PVEnergyTotal` in that list is the true PV-yield counter whose absence caused
issue #6 (six reporters, four months). `pv3Power`/`pv4Power` are issue #15.
`currentFault` / `runningState` / `SOH` are unexploited observability. This is the
concrete argument for probing everything rather than asking about one variable at
a time.

Do **not** probe `GET /op/v0/device/variable/get`. It returns the API's generic
dictionary — 72 PV entries covering `pv1`…`pv24` — not device capability. Only
`real/query` with an empty variable list reports what the device actually has.

## Explicitly out of scope

- **Narrowing `POLLED_VARIABLES` to the probed set.** The report records
  `unpolled_available` so this can be decided later. Whether an unsupported
  variable causes the whole `real/query` to be rejected is being investigated
  separately (the API-40257 work); until that lands, changing what we request
  risks breaking polling for existing users.
- **Adding sensors for any newly discovered variable.** Separate change, separate
  tests.
- **A button entity for rescan.** The action is enough; add a button later if
  users ask.

---

## File Structure

**Create:**
- `smart_battery/capabilities.py` — `CapabilityReport` + pure `classify()`. Brand-agnostic; no HA imports at runtime.
- `custom_components/foxess_control/foxess/probe.py` — FoxESS probe: three read-only calls → `CapabilityReport`.
- `tests/test_capabilities.py` — classifier unit tests.
- `tests/test_probe_foxess.py` — probe against the simulator.
- `tests/e2e/test_capability_probe.py` — Repair issue + rescan action through real HA.
- `.github/ISSUE_TEMPLATE/hardware-support.yml` — asks for the diagnostics download.

**Modify:**
- `smart_battery/adapter.py` — `InverterAdapter.async_probe_capabilities()` + `EntityAdapter` default.
- `custom_components/foxess_control/foxess_adapter.py` — cloud adapter implementation.
- `custom_components/foxess_control/const.py` — required-variable tuples, issue IDs, service name, store key.
- `custom_components/foxess_control/__init__.py` — probe trigger policy inside `async_setup_entry` (around the `dd.store` creation at line 1330).
- `custom_components/foxess_control/_services.py` — register `rescan_capabilities`.
- `custom_components/foxess_control/services.yaml` — declare the action.
- `custom_components/foxess_control/strings.json` + `translations/en.json` (and the other locales) — action text and Repair issue text.
- `custom_components/foxess_control/diagnostics.py` — `capabilities` section.
- `simulator/model.py` — honour `variables: []` as "all", add an `omit_variables` knob.
- `docs/knowledge/02-constraints.md` — new constraint (see Task 10).
- `docs/api/foxess-cloud-api.md` — document the empty-variable-list probe and the catalogue-vs-capability distinction.
- `CHANGELOG.md`.

**Never edit** `custom_components/foxess_control/smart_battery/` — the pre-commit hook syncs it from the root copy (C-015).

---

### Task 1: Simulator honours `variables: []` as "all"

The real API returns every supported variable when the list is empty. The
simulator returns nothing, so nothing downstream is testable until this is fixed.

**Files:**
- Modify: `simulator/model.py:372-410`
- Test: `tests/test_simulator_fidelity.py` (create if absent)

- [ ] **Step 1: Write the failing test**

```python
"""Simulator must match the real API's empty-variable-list behaviour."""

from simulator.model import InverterModel


class TestEmptyVariableList:
    """`variables: []` means "everything" on the real FoxESS API."""

    def test_empty_list_returns_all_variables(self) -> None:
        model = InverterModel()
        response = model.get_real_time_response([])
        datas = response[0]["datas"]
        names = {row["variable"] for row in datas}
        assert len(names) > 20, (
            f"empty variables list must return the full set, got {len(names)}: "
            f"{sorted(names)}"
        )
        assert "SoC" in names
        assert "pvPower" in names

    def test_explicit_list_still_filters(self) -> None:
        model = InverterModel()
        response = model.get_real_time_response(["SoC", "pvPower"])
        names = {row["variable"] for row in response[0]["datas"]}
        assert names == {"SoC", "pvPower"}

    def test_unknown_variable_is_omitted_not_errored(self) -> None:
        model = InverterModel()
        response = model.get_real_time_response(["SoC", "nonsenseVariable"])
        names = {row["variable"] for row in response[0]["datas"]}
        assert names == {"SoC"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_simulator_fidelity.py -v`
Expected: `test_empty_list_returns_all_variables` FAILS with
`empty variables list must return the full set, got 0: []`.

- [ ] **Step 3: Implement**

In `simulator/model.py`, replace the return block of `get_real_time_response`:

```python
        requested = variables or list(var_map)
        datas = []
        for v in requested:
            if v in var_map and v not in self.omit_variables:
                datas.append({"variable": v, "value": var_map[v]})
        return [{"datas": datas, "deviceSN": self.device_sn}]
```

And add the knob to the model's attribute set (alongside `active_fault`), so a
test can simulate hardware that lacks a variable:

```python
    #: Variables this simulated device does not report at all.  Lets tests
    #: reproduce hardware whose firmware omits a capability.
    omit_variables: set[str] = field(default_factory=set)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_simulator_fidelity.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add simulator/model.py tests/test_simulator_fidelity.py
git commit -m "test: simulator returns all variables for an empty request list

Matches the real FoxESS API, verified against a live KH10 (64 variables).
Adds an omit_variables knob so tests can simulate missing capabilities."
```

---

### Task 2: `CapabilityReport` and the pure classifier

**Files:**
- Create: `smart_battery/capabilities.py`
- Test: `tests/test_capabilities.py`

- [ ] **Step 1: Write the failing test**

```python
"""Capability classification is pure and brand-agnostic."""

from smart_battery.capabilities import (
    CapabilityStatus,
    classify,
)


class TestClassify:
    """Status derivation from a probed variable set and control plane."""

    def _vars(self, *names: str) -> dict[str, str]:
        return {n: "kW" for n in names}

    def test_all_present_and_control_ok_is_usable(self) -> None:
        result = classify(
            variables=self._vars(
                "SoC", "batChargePower", "batDischargePower", "loadsPower", "pvPower"
            ),
            control_ok=True,
            model="KH10",
        )
        assert result.status is CapabilityStatus.USABLE
        assert result.missing_required == []
        assert result.missing_optional == []

    def test_missing_soc_is_unusable(self) -> None:
        result = classify(
            variables=self._vars("batChargePower", "loadsPower"),
            control_ok=True,
            model="KH10",
        )
        assert result.status is CapabilityStatus.UNUSABLE
        assert "SoC" in result.missing_required

    def test_control_plane_failure_is_degraded_not_unusable(self) -> None:
        result = classify(
            variables=self._vars(
                "SoC", "batChargePower", "batDischargePower", "loadsPower", "pvPower"
            ),
            control_ok=False,
            model=None,
        )
        assert result.status is CapabilityStatus.DEGRADED
        assert result.control_ok is False

    def test_missing_optional_variable_is_degraded(self) -> None:
        result = classify(
            variables=self._vars(
                "SoC", "batChargePower", "batDischargePower", "loadsPower"
            ),
            control_ok=True,
            model="KH10",
        )
        assert result.status is CapabilityStatus.DEGRADED
        assert result.missing_optional == ["pvPower"]

    def test_unknown_model_is_recorded_but_not_fatal(self) -> None:
        result = classify(
            variables=self._vars(
                "SoC", "batChargePower", "batDischargePower", "loadsPower", "pvPower"
            ),
            control_ok=True,
            model=None,
        )
        assert result.status is CapabilityStatus.DEGRADED
        assert any("model" in note for note in result.notes)

    def test_empty_probe_is_unusable(self) -> None:
        result = classify(variables={}, control_ok=False, model=None)
        assert result.status is CapabilityStatus.UNUSABLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capabilities.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'smart_battery.capabilities'`.

- [ ] **Step 3: Implement**

```python
"""Inverter capability probing — brand-agnostic report and classification.

A brand adapter probes its inverter and returns a :class:`CapabilityReport`.
The policy that turns a probe result into a user-facing status lives here so
every brand classifies identically (C-021, C-039).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

#: Without these the algorithms cannot run at all.
REQUIRED_VARIABLES: tuple[str, ...] = (
    "SoC",
    "batChargePower",
    "batDischargePower",
    "loadsPower",
)

#: Absence degrades behaviour but leaves the integration useful.  ``pvPower``
#: is optional because AC-coupled installs report generation on a separate
#: meter channel instead.
OPTIONAL_VARIABLES: tuple[str, ...] = ("pvPower",)


class CapabilityStatus(str, Enum):
    """How much of the integration this hardware supports."""

    USABLE = "usable"
    DEGRADED = "degraded"
    UNUSABLE = "unusable"


@dataclass(frozen=True)
class CapabilityReport:
    """The outcome of probing one inverter."""

    status: CapabilityStatus
    variables: dict[str, str] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    unpolled_available: list[str] = field(default_factory=list)
    control_ok: bool = False
    model: str | None = None
    notes: list[str] = field(default_factory=list)
    probed_at: str | None = None
    integration_version: str | None = None
    signature: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialise for persistence and diagnostics."""
        return {
            "status": self.status.value,
            "variables": dict(self.variables),
            "missing_required": list(self.missing_required),
            "missing_optional": list(self.missing_optional),
            "unpolled_available": list(self.unpolled_available),
            "control_ok": self.control_ok,
            "model": self.model,
            "notes": list(self.notes),
            "probed_at": self.probed_at,
            "integration_version": self.integration_version,
            "signature": self.signature,
        }


def capability_signature(variables: dict[str, str], model: str | None) -> str:
    """Stable fingerprint of a hardware capability surface.

    A change between probes means the hardware or its firmware changed, which
    is worth telling the user about.
    """
    payload = "|".join(sorted(variables)) + f"#{model or ''}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def classify(
    *,
    variables: dict[str, str],
    control_ok: bool,
    model: str | None,
    polled: tuple[str, ...] = (),
) -> CapabilityReport:
    """Derive a :class:`CapabilityReport` from a probe result.

    ``variables`` maps variable name to unit, as reported by the device.
    ``control_ok`` is True when the brand layer verified it can read the
    control surface (for FoxESS: the schedule).  ``polled`` is what the
    integration currently requests, used to compute ``unpolled_available``.
    """
    missing_required = [v for v in REQUIRED_VARIABLES if v not in variables]
    missing_optional = [v for v in OPTIONAL_VARIABLES if v not in variables]
    unpolled = sorted(set(variables) - set(polled)) if polled else []

    notes: list[str] = []
    if model is None:
        notes.append("inverter model could not be determined")
    if not control_ok:
        notes.append("control surface could not be read — control may be unavailable")

    if missing_required or not variables:
        status = CapabilityStatus.UNUSABLE
    elif missing_optional or notes:
        status = CapabilityStatus.DEGRADED
    else:
        status = CapabilityStatus.USABLE

    return CapabilityReport(
        status=status,
        variables=dict(variables),
        missing_required=missing_required,
        missing_optional=missing_optional,
        unpolled_available=unpolled,
        control_ok=control_ok,
        model=model,
        notes=notes,
        signature=capability_signature(variables, model),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capabilities.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add smart_battery/capabilities.py tests/test_capabilities.py
git commit -m "feat: brand-agnostic capability report and classifier"
```

---

### Task 3: `InverterAdapter.async_probe_capabilities()`

**Files:**
- Modify: `smart_battery/adapter.py` (Protocol at line 22; `EntityAdapter` at line 114)
- Test: `tests/test_smart_battery_agnostic.py`

Per C-040 this must be tested through `smart_battery.testing.FakeAdapter` (the
`fake_adapter` fixture), never a brand adapter.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_smart_battery_agnostic.py`:

```python
class TestCapabilityProbeProtocol:
    """Every adapter answers a capability probe, brand-agnostically."""

    async def test_default_adapter_reports_usable(self, fake_adapter) -> None:
        from smart_battery.capabilities import CapabilityStatus

        report = await fake_adapter.async_probe_capabilities(None)
        assert report.status in tuple(CapabilityStatus)

    async def test_entity_adapter_default_is_not_unusable(self) -> None:
        """Entity-mode brands have no cloud variable surface to probe.

        They must not be reported as broken just because they cannot
        enumerate variables — control still works through HA entities.
        """
        from smart_battery.adapter import EntityAdapter
        from smart_battery.capabilities import CapabilityStatus
        from smart_battery.types import WorkMode

        adapter = EntityAdapter(
            mode_map={WorkMode.SELF_USE: "Self Use"},
            work_mode_entity="select.work_mode",
        )
        report = await adapter.async_probe_capabilities(None)
        assert report.status is not CapabilityStatus.UNUSABLE
        assert report.control_ok is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_smart_battery_agnostic.py -k CapabilityProbe -v`
Expected: FAIL — `AttributeError: 'EntityAdapter' object has no attribute 'async_probe_capabilities'`.

- [ ] **Step 3: Implement**

Add to the `InverterAdapter` Protocol in `smart_battery/adapter.py`:

```python
    async def async_probe_capabilities(
        self,
        hass: HomeAssistant | None,
    ) -> CapabilityReport:
        """Enumerate what this inverter supports.

        Called once at first setup and after an integration upgrade — never
        on every restart, because probing costs API calls against
        rate-limited cloud endpoints.

        Implementations MUST be read-only: a probe must never change inverter
        state.  Implementations MUST NOT raise; on failure return a report
        with ``status=UNUSABLE`` and an explanatory note, so a probe failure
        degrades the install rather than blocking setup.

        Refs C-026 (persistent errors surfaced via state, not logs), C-039
        (dependency inversion via this Protocol).
        """
        ...
```

with the import at the top:

```python
from .capabilities import CapabilityReport, CapabilityStatus
```

And the `EntityAdapter` default:

```python
    async def async_probe_capabilities(
        self,
        hass: HomeAssistant | None,
    ) -> CapabilityReport:
        """Entity-mode adapters control via HA entities, not a variable API.

        There is nothing to enumerate, and nothing is broken — report usable
        with an empty variable set.
        """
        return CapabilityReport(
            status=CapabilityStatus.USABLE,
            control_ok=True,
            notes=["entity mode — no cloud variable surface to probe"],
        )
```

Add the same default to `smart_battery/testing.py`'s `FakeAdapter` if it does not
inherit from `EntityAdapter`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_smart_battery_agnostic.py -k CapabilityProbe -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add smart_battery/adapter.py smart_battery/testing.py tests/test_smart_battery_agnostic.py
git commit -m "feat: async_probe_capabilities on the InverterAdapter protocol"
```

---

### Task 4: FoxESS probe implementation

Three read-only calls. `real/query` with `variables: []` for the device's whole
variable surface; `device/detail` for the model; `scheduler/get` for the control
plane.

**Files:**
- Create: `custom_components/foxess_control/foxess/probe.py`
- Modify: `custom_components/foxess_control/foxess_adapter.py`
- Test: `tests/test_probe_foxess.py`

- [ ] **Step 1: Write the failing test**

```python
"""FoxESS capability probe, exercised against the simulator (C-028)."""

import pytest

from smart_battery.capabilities import CapabilityStatus


class TestFoxESSProbe:
    """The probe reports what the simulated device actually offers."""

    async def test_probe_enumerates_full_variable_set(self, simulator) -> None:
        from custom_components.foxess_control.foxess.probe import probe_capabilities

        report = probe_capabilities(simulator.inverter)
        assert report.status is CapabilityStatus.USABLE
        assert "SoC" in report.variables
        assert "pvPower" in report.variables
        assert len(report.variables) > 20

    async def test_probe_records_unpolled_variables(self, simulator) -> None:
        from custom_components.foxess_control.const import POLLED_VARIABLES
        from custom_components.foxess_control.foxess.probe import probe_capabilities

        report = probe_capabilities(simulator.inverter)
        assert set(report.unpolled_available).isdisjoint(POLLED_VARIABLES)

    async def test_missing_required_variable_is_unusable(self, simulator) -> None:
        from custom_components.foxess_control.foxess.probe import probe_capabilities

        simulator.model.omit_variables = {"SoC"}
        report = probe_capabilities(simulator.inverter)
        assert report.status is CapabilityStatus.UNUSABLE
        assert "SoC" in report.missing_required

    async def test_missing_pv_power_is_degraded(self, simulator) -> None:
        from custom_components.foxess_control.foxess.probe import probe_capabilities

        simulator.model.omit_variables = {"pvPower"}
        report = probe_capabilities(simulator.inverter)
        assert report.status is CapabilityStatus.DEGRADED
        assert report.missing_optional == ["pvPower"]

    async def test_api_failure_degrades_and_never_raises(self, simulator) -> None:
        """A probe must not block setup when the cloud is unreachable."""
        from custom_components.foxess_control.foxess.probe import probe_capabilities

        simulator.model.active_fault = "unreachable"
        simulator.model.fault_remaining = 99
        report = probe_capabilities(simulator.inverter)
        assert report.status is CapabilityStatus.UNUSABLE
        assert report.notes, "a failed probe must explain itself"
```

Adapt `simulator` fixture usage to whatever the existing fixtures in
`tests/conftest.py` provide (see `tests/test_alternate_solar_source.py` for the
established pattern of driving a per-test simulator instance).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_probe_foxess.py -v`
Expected: FAIL — `ModuleNotFoundError: ...foxess.probe`.

- [ ] **Step 3: Implement**

```python
"""Read-only capability probe for FoxESS cloud devices.

Three calls, none of which mutate inverter state:

* ``POST /op/v0/device/real/query`` with ``variables: []`` — the device's
  complete reported variable set.  Verified against a live KH10 on
  2026-08-26: 64 variables, versus 27 in ``POLLED_VARIABLES``.
* ``GET /op/v0/device/detail`` — model and capacity.
* ``POST /op/v0/device/scheduler/get`` — can we read the control surface?

Do NOT use ``GET /op/v0/device/variable/get``: it returns the API's generic
dictionary (``pv1``…``pv24``), not this device's capability.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from smart_battery.capabilities import CapabilityReport, CapabilityStatus, classify

from ..const import POLLED_VARIABLES

if TYPE_CHECKING:
    from .inverter import Inverter

_LOGGER = logging.getLogger(__name__)


def probe_capabilities(inverter: Inverter) -> CapabilityReport:
    """Probe *inverter* read-only and classify the result.

    Never raises: a probe failure yields an UNUSABLE report with a note, so
    setup continues and the user sees an explanation instead of a traceback.
    """
    variables: dict[str, str] = {}
    model: str | None = None
    control_ok = False
    notes: list[str] = []

    try:
        variables = {
            name: "" for name in inverter.get_real_time([])
        }
    except Exception as exc:  # noqa: BLE001 — probe must not raise
        notes.append(f"variable probe failed: {type(exc).__name__}")
        _LOGGER.debug("Capability probe: real/query failed", exc_info=True)

    try:
        model = inverter.device_type
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Capability probe: device detail failed", exc_info=True)

    try:
        inverter.get_schedule()
        control_ok = True
    except Exception as exc:  # noqa: BLE001
        notes.append(f"schedule read failed: {type(exc).__name__}")
        _LOGGER.debug("Capability probe: scheduler/get failed", exc_info=True)

    report = classify(
        variables=variables,
        control_ok=control_ok,
        model=model,
        polled=tuple(POLLED_VARIABLES),
    )
    combined = [*report.notes, *notes]
    from dataclasses import replace

    return replace(
        report,
        notes=combined,
        probed_at=dt_util.utcnow().isoformat(),
        status=(
            CapabilityStatus.UNUSABLE if not variables else report.status
        ),
    )
```

Then wire it onto the cloud adapter in `foxess_adapter.py`:

```python
    async def async_probe_capabilities(
        self,
        hass: HomeAssistant | None,
    ) -> CapabilityReport:
        """Probe via the executor — the FoxESS client is blocking."""
        from .foxess.probe import probe_capabilities

        if hass is None:
            return probe_capabilities(self._inverter)
        return await hass.async_add_executor_job(
            probe_capabilities, self._inverter
        )
```

Use whatever attribute the adapter already holds the `Inverter` on; read the
constructor before writing this.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_probe_foxess.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/foxess/probe.py \
        custom_components/foxess_control/foxess_adapter.py \
        tests/test_probe_foxess.py
git commit -m "feat: read-only FoxESS capability probe"
```

---

### Task 5: Persistence and the trigger policy

Probe on first run and after an upgrade. Never on a plain restart.

**Files:**
- Modify: `custom_components/foxess_control/const.py`
- Modify: `custom_components/foxess_control/__init__.py` (near `dd.store` creation, line 1330)
- Test: `tests/test_capability_trigger.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""When the probe runs, and when it must not."""

from custom_components.foxess_control.capability_state import should_probe


class TestShouldProbe:
    """Probe on first run and after upgrade only."""

    def test_no_stored_report_probes(self) -> None:
        assert should_probe(stored=None, current_version="1.0.23") is True

    def test_same_version_does_not_probe(self) -> None:
        stored = {"integration_version": "1.0.23", "status": "usable"}
        assert should_probe(stored=stored, current_version="1.0.23") is False

    def test_version_change_probes(self) -> None:
        stored = {"integration_version": "1.0.22", "status": "usable"}
        assert should_probe(stored=stored, current_version="1.0.23") is True

    def test_degraded_install_still_does_not_renag_on_restart(self) -> None:
        """The nag is once per probe, not once per startup."""
        stored = {"integration_version": "1.0.23", "status": "degraded"}
        assert should_probe(stored=stored, current_version="1.0.23") is False

    def test_missing_version_in_stored_report_probes(self) -> None:
        assert should_probe(stored={"status": "usable"}, current_version="1.0.23") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capability_trigger.py -v`
Expected: FAIL — `ModuleNotFoundError: ...capability_state`.

- [ ] **Step 3: Implement**

Create `custom_components/foxess_control/capability_state.py`:

```python
"""Persistence and trigger policy for the capability probe."""

from __future__ import annotations

from typing import Any

#: Key inside the integration's existing Store payload.
CAPABILITY_STORE_KEY = "capabilities"


def should_probe(
    *,
    stored: dict[str, Any] | None,
    current_version: str | None,
) -> bool:
    """Whether to probe now.

    True on first run (nothing stored) and after an integration upgrade.
    False otherwise — a probe costs rate-limited API calls, and a degraded
    install must not re-nag on every restart.  The user can always force one
    with the ``rescan_capabilities`` action.
    """
    if not stored:
        return True
    return stored.get("integration_version") != current_version
```

In `__init__.py`, after `dd.store` is created (line 1330), load the stored report
and probe if needed. Keep it non-blocking for setup — schedule it rather than
awaiting it inline, and never let a failure abort setup:

```python
    async def _run_capability_probe(force: bool = False) -> None:
        """Probe capabilities if policy says so; surface the outcome."""
        from .capability_state import CAPABILITY_STORE_KEY, should_probe
        from .capability_issues import sync_capability_issue

        try:
            payload = await dd.store.async_load() or {}
            stored = payload.get(CAPABILITY_STORE_KEY)
            version = _integration_version()
            if not force and not should_probe(
                stored=stored, current_version=version
            ):
                dd.capabilities = stored
                return

            report = await dd.adapter.async_probe_capabilities(hass)
            record = report.to_dict()
            record["integration_version"] = version
            previous_signature = (stored or {}).get("signature")
            if previous_signature and previous_signature != record.get("signature"):
                record.setdefault("notes", []).append(
                    "hardware capability surface changed since the last probe"
                )
            dd.capabilities = record
            payload[CAPABILITY_STORE_KEY] = record
            await dd.store.async_save(payload)
            sync_capability_issue(hass, DOMAIN, record)
        except Exception:  # noqa: BLE001 — probing must never block setup
            _LOGGER.debug("Capability probe failed (non-critical)", exc_info=True)

    hass.async_create_task(_run_capability_probe())
    dd.run_capability_probe = _run_capability_probe
```

Add `capabilities: dict[str, Any] | None = None` and
`run_capability_probe: Callable[..., Any] | None = None` to `FoxESSControlData` in
`domain_data.py`. Reuse the module-level `_integration_version()` helper already
present in `diagnostics.py:28` — move it to `_helpers.py` and import it in both
places rather than duplicating it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capability_trigger.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/capability_state.py \
        custom_components/foxess_control/__init__.py \
        custom_components/foxess_control/domain_data.py \
        custom_components/foxess_control/_helpers.py \
        custom_components/foxess_control/diagnostics.py \
        tests/test_capability_trigger.py
git commit -m "feat: persist capability report; probe on first run and upgrade only"
```

---

### Task 6: Repair issue surfacing

Follows the established pattern at `foxess_adapter.py:161-192`
(`_create_schedule_not_applied_issue`).

**Files:**
- Create: `custom_components/foxess_control/capability_issues.py`
- Modify: `custom_components/foxess_control/strings.json`, `translations/en.json` (+ other locales)
- Test: `tests/test_capability_issues.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""A degraded probe raises exactly one Repair issue; recovery clears it."""

from unittest.mock import patch

from custom_components.foxess_control.capability_issues import sync_capability_issue


class TestCapabilityIssue:
    def test_degraded_creates_issue(self, hass) -> None:
        with patch(
            "custom_components.foxess_control.capability_issues.async_create_issue"
        ) as create:
            sync_capability_issue(
                hass,
                "foxess_control",
                {"status": "degraded", "missing_optional": ["pvPower"], "notes": []},
            )
        assert create.called
        assert create.call_args.kwargs["translation_key"] == "capability_degraded"

    def test_unusable_creates_error_severity_issue(self, hass) -> None:
        from homeassistant.helpers.issue_registry import IssueSeverity

        with patch(
            "custom_components.foxess_control.capability_issues.async_create_issue"
        ) as create:
            sync_capability_issue(
                hass,
                "foxess_control",
                {"status": "unusable", "missing_required": ["SoC"], "notes": []},
            )
        assert create.call_args.kwargs["severity"] is IssueSeverity.ERROR

    def test_usable_clears_any_existing_issue(self, hass) -> None:
        with patch(
            "custom_components.foxess_control.capability_issues.async_delete_issue"
        ) as delete:
            sync_capability_issue(hass, "foxess_control", {"status": "usable"})
        assert delete.called

    def test_placeholders_name_the_missing_capabilities(self, hass) -> None:
        with patch(
            "custom_components.foxess_control.capability_issues.async_create_issue"
        ) as create:
            sync_capability_issue(
                hass,
                "foxess_control",
                {
                    "status": "degraded",
                    "missing_optional": ["pvPower"],
                    "notes": ["inverter model could not be determined"],
                },
            )
        placeholders = create.call_args.kwargs["translation_placeholders"]
        assert "pvPower" in placeholders["details"]
        assert "model" in placeholders["details"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capability_issues.py -v`
Expected: FAIL — `ModuleNotFoundError: ...capability_issues`.

- [ ] **Step 3: Implement**

```python
"""Surface capability-probe outcomes as Home Assistant Repair issues (C-020, C-026)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_ISSUE_ID = "capability_probe"
_LEARN_MORE = (
    "https://github.com/nicois/foxess-control/issues/new"
    "?template=hardware-support.yml"
)


def sync_capability_issue(
    hass: HomeAssistant,
    domain: str,
    report: dict[str, Any] | None,
) -> None:
    """Create, update or clear the capability Repair issue for *report*."""
    if not report:
        return
    status = report.get("status")
    try:
        if status in (None, "usable"):
            async_delete_issue(hass, domain, _ISSUE_ID)
            return

        details = ", ".join(
            [
                *report.get("missing_required", []),
                *report.get("missing_optional", []),
                *report.get("notes", []),
            ]
        )
        async_create_issue(
            hass,
            domain,
            _ISSUE_ID,
            is_fixable=False,
            severity=(
                IssueSeverity.ERROR if status == "unusable" else IssueSeverity.WARNING
            ),
            translation_key=(
                "capability_unusable" if status == "unusable" else "capability_degraded"
            ),
            translation_placeholders={
                "details": details or "unknown",
                "model": str(report.get("model") or "unknown"),
            },
            learn_more_url=_LEARN_MORE,
        )
    except Exception:  # noqa: BLE001 — Repair surfacing is best-effort
        _LOGGER.debug("Failed to sync capability issue (non-critical)")
```

Add to `strings.json` and `translations/en.json` under `issues`:

```json
    "capability_degraded": {
      "title": "Some FoxESS features are unavailable on this inverter",
      "description": "Your inverter ({model}) does not report everything this integration uses, so some features are degraded: {details}.\n\nEverything else keeps working. To help add full support for your hardware, open the integration's ⋮ menu, choose **Download diagnostics**, and attach the file to a hardware-support report. The file is scrubbed of your API key, serial number and password automatically.\n\nIf you have changed hardware, run the **Rescan capabilities** action to re-check."
    },
    "capability_unusable": {
      "title": "FoxESS Control cannot control this inverter",
      "description": "Your inverter ({model}) is not reporting values this integration requires: {details}.\n\nSensors may still work, but charge and discharge control will not. Please open the integration's ⋮ menu, choose **Download diagnostics**, and attach the file to a hardware-support report — it is scrubbed of your API key, serial number and password automatically."
    }
```

Mirror the same two keys into every other file in `translations/`, translated.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_capability_issues.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/capability_issues.py \
        custom_components/foxess_control/strings.json \
        custom_components/foxess_control/translations/ \
        tests/test_capability_issues.py
git commit -m "feat: surface capability status as a Repair issue with diagnostics instructions"
```

---

### Task 7: `rescan_capabilities` action

**Files:**
- Modify: `custom_components/foxess_control/const.py` (add `SERVICE_RESCAN_CAPABILITIES = "rescan_capabilities"`)
- Modify: `custom_components/foxess_control/_services.py`
- Modify: `custom_components/foxess_control/services.yaml`
- Modify: `strings.json` + `translations/*.json` (`services` section)
- Test: `tests/test_rescan_service.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""The rescan action forces a probe regardless of the trigger policy."""


class TestRescanAction:
    async def test_action_is_registered(self, hass, setup_integration) -> None:
        assert hass.services.has_service("foxess_control", "rescan_capabilities")

    async def test_rescan_forces_a_probe_when_policy_would_skip(
        self, hass, setup_integration, simulator
    ) -> None:
        """Same version stored, so should_probe() is False — rescan overrides."""
        calls: list[bool] = []

        dd = hass.data["foxess_control"]
        original = dd.run_capability_probe

        async def _spy(force: bool = False) -> None:
            calls.append(force)
            await original(force)

        dd.run_capability_probe = _spy
        await hass.services.async_call(
            "foxess_control", "rescan_capabilities", {}, blocking=True
        )
        assert calls == [True], "rescan must force the probe"

    async def test_rescan_updates_stored_report(
        self, hass, setup_integration, simulator
    ) -> None:
        simulator.model.omit_variables = {"pvPower"}
        await hass.services.async_call(
            "foxess_control", "rescan_capabilities", {}, blocking=True
        )
        dd = hass.data["foxess_control"]
        assert dd.capabilities["status"] == "degraded"
        assert "pvPower" in dd.capabilities["missing_optional"]
```

Use the existing integration-setup fixture from `tests/conftest.py`; read a
neighbouring service test (e.g. any test that calls
`foxess_control.clear_overrides`) for the established fixture names before
writing this.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rescan_service.py -v`
Expected: FAIL — `test_action_is_registered` asserts False.

- [ ] **Step 3: Implement**

In `_services.py`, inside `_register_services`:

```python
    async def _handle_rescan_capabilities(call: ServiceCall) -> None:
        """Re-probe the inverter's capabilities on demand.

        Users need this when they change hardware or firmware, and when they
        are collecting a hardware-support report for a GitHub issue.
        """
        dd = _dd(hass)
        runner = getattr(dd, "run_capability_probe", None)
        if runner is None:
            _LOGGER.warning("Capability probe unavailable — is setup complete?")
            return
        await runner(True)

    hass.services.async_register(
        DOMAIN, SERVICE_RESCAN_CAPABILITIES, _handle_rescan_capabilities
    )
```

In `services.yaml`:

```yaml
rescan_capabilities:
  description: >-
    Re-check which features this inverter supports. Use after changing
    hardware or firmware, or when collecting a hardware-support report.
  fields: {}
```

And the matching `services.rescan_capabilities.name` / `.description` entries in
`strings.json` and every `translations/*.json`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rescan_service.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/_services.py \
        custom_components/foxess_control/services.yaml \
        custom_components/foxess_control/const.py \
        custom_components/foxess_control/strings.json \
        custom_components/foxess_control/translations/ \
        tests/test_rescan_service.py
git commit -m "feat: rescan_capabilities action to force a capability probe"
```

---

### Task 8: Diagnostics section

**Files:**
- Modify: `custom_components/foxess_control/diagnostics.py:99-119`
- Test: `tests/test_diagnostics.py` (extend; create if absent)

- [ ] **Step 1: Write the failing test**

```python
class TestCapabilityDiagnostics:
    """The probe result is the hardware-support payload — it must be present."""

    async def test_capabilities_section_present(
        self, hass, setup_integration
    ) -> None:
        from custom_components.foxess_control.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        entry = hass.config_entries.async_entries("foxess_control")[0]
        result = await async_get_config_entry_diagnostics(hass, entry)
        assert "capabilities" in result
        assert "variables" in result["capabilities"]
        assert "unpolled_available" in result["capabilities"]

    async def test_capabilities_section_carries_no_secrets(
        self, hass, setup_integration
    ) -> None:
        from custom_components.foxess_control.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        entry = hass.config_entries.async_entries("foxess_control")[0]
        result = await async_get_config_entry_diagnostics(hass, entry)
        blob = repr(result["capabilities"])
        for secret in ("api_key", "web_password", "token"):
            assert secret not in blob
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diagnostics.py -k Capability -v`
Expected: FAIL — `KeyError`/assert on `"capabilities" in result`.

- [ ] **Step 3: Implement**

In the returned dict of `async_get_config_entry_diagnostics`, add:

```python
            "capabilities": getattr(domain_data, "capabilities", None),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diagnostics.py -k Capability -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/foxess_control/diagnostics.py tests/test_diagnostics.py
git commit -m "feat: include the capability report in diagnostics"
```

---

### Task 9: E2E coverage

HA-visible behaviour (a Repair issue and a new action) needs E2E coverage.

**Files:**
- Create: `tests/e2e/test_capability_probe.py`

- [ ] **Step 1: Write the failing test**

```python
"""Capability probe through a real HA instance (C-029)."""

import pytest

pytestmark = pytest.mark.slow


class TestCapabilityProbeE2E:
    async def test_degraded_hardware_raises_repair_issue(
        self, ha, simulator, page
    ) -> None:
        """A device missing pvPower must tell the user, via the UI alone (C-020)."""
        simulator.set_omit_variables(["pvPower"])
        await ha.reload_integration()

        issues = await ha.wait_for_condition(
            lambda: ha.get_repair_issues(domain="foxess_control"),
            description="capability repair issue to appear",
        )
        assert any(i["issue_id"] == "capability_probe" for i in issues)

    async def test_rescan_clears_the_issue_once_hardware_reports_fully(
        self, ha, simulator, page
    ) -> None:
        simulator.set_omit_variables(["pvPower"])
        await ha.reload_integration()
        await ha.wait_for_condition(
            lambda: ha.get_repair_issues(domain="foxess_control"),
            description="issue present before recovery",
        )

        simulator.set_omit_variables([])
        await ha.call_service("foxess_control", "rescan_capabilities", {})

        await ha.wait_for_condition(
            lambda: not ha.get_repair_issues(domain="foxess_control"),
            description="capability issue to clear after rescan",
        )

    async def test_restart_does_not_reprobe_or_renag(
        self, ha, simulator, page
    ) -> None:
        """The nag is once per probe, not once per startup."""
        probe_count_before = simulator.count_requests("/op/v0/device/real/query", empty_variables=True)
        await ha.reload_integration()
        probe_count_after = simulator.count_requests("/op/v0/device/real/query", empty_variables=True)
        assert probe_count_after == probe_count_before, (
            "a reload at the same integration version must not re-probe"
        )
```

`simulator.set_omit_variables` and `simulator.count_requests` need adding to the
E2E simulator handle in `tests/e2e/conftest.py` (see the existing
`SimulatorHandle` around line 489) plus matching control endpoints in
`simulator/server.py`. Use `wait_for_condition` — never a sleep (C-031). Run E2E
with `-n auto`, never serial.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/e2e/test_capability_probe.py -n auto -v`
Expected: FAIL — no such repair issue / no such simulator control.

- [ ] **Step 3: Implement**

Add the simulator control endpoints and handle methods, then make the tests pass.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/e2e/test_capability_probe.py -n auto -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_capability_probe.py tests/e2e/conftest.py simulator/server.py
git commit -m "test: E2E coverage for capability probe, repair issue and rescan"
```

---

### Task 10: Issue template, docs, changelog

**Files:**
- Create: `.github/ISSUE_TEMPLATE/hardware-support.yml`
- Modify: `docs/api/foxess-cloud-api.md`
- Modify: `docs/knowledge/02-constraints.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Create the issue template**

```yaml
name: Hardware support report
description: Help add support for an inverter the integration does not fully handle
title: "[hardware] <inverter model>"
labels: ["enhancement"]
body:
  - type: markdown
    attributes:
      value: |
        Thanks for helping widen hardware support.

        The **diagnostics file** below contains everything needed: the full list
        of variables your inverter reports, its model, and what the integration
        could not determine. Your API key, serial number, password and battery
        IDs are removed automatically — you do not need to edit the file.
  - type: input
    id: model
    attributes:
      label: Inverter model
      placeholder: "H3-15.0-Smart"
    validations:
      required: true
  - type: textarea
    id: symptom
    attributes:
      label: What is not working?
    validations:
      required: true
  - type: markdown
    attributes:
      value: |
        ### Diagnostics file (required)

        1. Settings → Devices & Services → **FoxESS Control**
        2. The **⋮** menu → **Download diagnostics**
        3. Attach the downloaded file to this issue

        If the integration is set up but reporting a problem, run the
        **Rescan capabilities** action first so the report is current.
  - type: checkboxes
    id: attached
    attributes:
      label: Confirmation
      options:
        - label: I have attached the diagnostics file
          required: true
```

- [ ] **Step 2: Document the probe in the API contract**

Add to `docs/api/foxess-cloud-api.md`, in the `real/query` section:

```markdown
**Capability probing.** Sending `"variables": []` returns **every** variable the
device supports — 64 on a KH10 (verified 2026-08-26), against the 27 in
`POLLED_VARIABLES`. This is the only reliable capability probe.

Do **not** use `GET /op/v0/device/variable/get` for this: it returns the API's
generic dictionary, including `pv1`…`pv24` voltage/current/power entries that no
real device has. It describes the API, not the inverter.
```

- [ ] **Step 3: Add the constraint the bug revealed**

`C-041` was taken by the sensor-naming constraint that shipped with the
PVEnergyTotal fix on 2026-08-26. Use `C-042`, and re-check the highest
allocated ID in `docs/knowledge/02-constraints.md` before writing.

Add to `docs/knowledge/02-constraints.md` under Testing Infrastructure:

```markdown
- **C-042**: Simulator variables must model the *semantics* of the real API
  variable, not just its presence. `generationPower` was simulated as
  `solar_kw`, encoding the same misconception as the production code — so no
  test could catch the mislabelled solar sensor (issue #6, six reporters, four
  months). When a simulated variable stands for a physical quantity, the model
  must be able to produce a state where two related variables *diverge*
  (P-005, P-007).
```

- [ ] **Step 4: Changelog**

Add under a new Unreleased heading in `CHANGELOG.md`:

```markdown
### Added

- **Inverter capability probe** (C-020, C-026, D-0NN). On first run and after
  each upgrade the integration now asks the inverter what it actually supports
  (`real/query` with an empty variable list — 64 variables on a KH10 versus the
  27 we poll) and checks whether the control surface is readable. Installs
  that are degraded or unusable raise a Repair issue naming exactly what is
  missing, once per probe rather than on every restart, with instructions for
  submitting a scrubbed diagnostics file. New `rescan_capabilities` action
  forces a re-probe after a hardware or firmware change. The probe result is
  included in diagnostics.
```

Assign the real `D-NNN` by reading `docs/knowledge/04-design/` and add a design
note for the trigger policy while you are there.

- [ ] **Step 5: Commit**

```bash
git add .github/ISSUE_TEMPLATE/hardware-support.yml docs/ CHANGELOG.md
git commit -m "docs: capability probe contract, C-041, hardware-support template"
```

---

## Final verification

- [ ] `pytest tests/ -m "not slow" --tb=short` — all green
- [ ] `pytest tests/ --tb=short` — all green including E2E
- [ ] `pre-commit run --all-files` — ruff, mypy, semgrep, module-size, vendored-copy sync all clean
- [ ] Confirm no `.py` in `custom_components/foxess_control/` exceeds 2000 lines (C-034); `__init__.py` is at 1755 and this plan adds to it — if it crosses, extract the probe orchestration into `capability_state.py` rather than trimming elsewhere
- [ ] Confirm the vendored `custom_components/foxess_control/smart_battery/` copy was updated by the hook, not by hand (C-015)
- [ ] Do **not** bump the version or create a tag — releases are handled separately

## Self-review notes

- **Spec coverage:** probe-everything (Task 1, 4), nag at first run only (Tasks 5, 6, 9), user-triggerable rescan (Task 7), re-probe after upgrade (Task 5), scrubbed-report path (Tasks 8, 10). All four of the requested behaviours have a task.
- **Type consistency:** `CapabilityReport` / `CapabilityStatus` / `classify()` / `capability_signature()` / `should_probe()` / `sync_capability_issue()` / `probe_capabilities()` / `async_probe_capabilities()` are used with those exact names throughout.
- **Known ordering dependency:** Task 1 must land before Tasks 4 and 9 — the probe is untestable while the simulator returns nothing for an empty variable list.
- **Merge dependency:** Tasks 4, 5 and 8 touch `const.py`, `__init__.py` and `diagnostics.py`, which the in-flight PVEnergyTotal and API-40257 fixes also touch. Land those first.
