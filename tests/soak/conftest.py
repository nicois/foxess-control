"""Soak test fixtures: long-running real-time scenario simulations.

Reuses E2E infrastructure (simulator + HA container) but runs full
charge/discharge sessions in real time across realistic multi-hour
windows. Artifacts (SoC trajectory, logs, invariant checks) are
saved for post-run analysis.

Run:
    pytest tests/soak/ -m soak --tb=short
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import requests

from .results_db import save_run

if TYPE_CHECKING:
    from collections.abc import Generator

from tests.e2e.conftest import (
    CONTAINER_IMAGE,
    E2E_TOKEN,
    HA_CONFIG_SEED,
    REPO_ROOT,
    SimulatorHandle,
    _build_container_once,
    _find_free_port,
    _kill_process,
    _stop_container,
    _worker_id,
)
from tests.e2e.ha_client import HAClient

_log = logging.getLogger("soak")


def _artifact_dir() -> Path:
    env = os.environ.get("SOAK_ARTIFACT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "test-artifacts" / "soak"


ARTIFACT_DIR = _artifact_dir()


def pytest_configure(config: Any) -> None:
    config.addinivalue_line("markers", "soak: long-running scenario simulation")


@dataclass
class SoakSample:
    """A single observation during a soak test."""

    elapsed_s: float
    wall_time: str
    soc: float
    state: str
    power_w: float
    grid_import_kw: float
    grid_export_kw: float
    solar_kw: float
    load_kw: float
    bat_charge_kw: float
    bat_discharge_kw: float
    sim_time: str = ""


@dataclass
class InvariantViolation:
    """A recorded invariant violation."""

    elapsed_s: float
    rule: str
    detail: str


@dataclass
class SoakRecorder:
    """Collects samples and invariant violations during a soak test."""

    test_name: str
    samples: list[SoakSample] = field(default_factory=list)
    violations: list[InvariantViolation] = field(default_factory=list)
    _start: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def record(self, sample: SoakSample) -> None:
        self.samples.append(sample)

    def violate(self, rule: str, detail: str) -> None:
        v = InvariantViolation(self.elapsed(), rule, detail)
        self.violations.append(v)
        _log.warning("VIOLATION [%s] %s: %s", self.test_name, rule, detail)

    def save(self, artifact_dir: Path | None = None) -> Path:
        out_dir = artifact_dir or ARTIFACT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = self.test_name.replace("/", "_").replace("::", "__")
        base = out_dir / f"{safe_name}_{ts}"

        csv_path = base.with_suffix(".csv")
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "elapsed_s",
                    "wall_time",
                    "soc",
                    "state",
                    "power_w",
                    "grid_import_kw",
                    "grid_export_kw",
                    "solar_kw",
                    "load_kw",
                    "bat_charge_kw",
                    "bat_discharge_kw",
                    "sim_time",
                ]
            )
            for s in self.samples:
                writer.writerow(
                    [
                        f"{s.elapsed_s:.1f}",
                        s.wall_time,
                        f"{s.soc:.1f}",
                        s.state,
                        f"{s.power_w:.0f}",
                        f"{s.grid_import_kw:.3f}",
                        f"{s.grid_export_kw:.3f}",
                        f"{s.solar_kw:.3f}",
                        f"{s.load_kw:.3f}",
                        f"{s.bat_charge_kw:.3f}",
                        f"{s.bat_discharge_kw:.3f}",
                        s.sim_time,
                    ]
                )

        if self.violations:
            viol_path = base.with_name(base.stem + "_violations.json")
            with open(viol_path, "w") as f:
                json.dump(
                    [
                        {"elapsed_s": v.elapsed_s, "rule": v.rule, "detail": v.detail}
                        for v in self.violations
                    ],
                    f,
                    indent=2,
                )

        summary = base.with_name(base.stem + "_summary.txt")
        buf = io.StringIO()
        buf.write(f"Test: {self.test_name}\n")
        buf.write(f"Samples: {len(self.samples)}\n")
        buf.write(f"Violations: {len(self.violations)}\n")
        if self.samples:
            socs = [s.soc for s in self.samples]
            buf.write(f"SoC range: {min(socs):.1f}% - {max(socs):.1f}%\n")
            buf.write(f"Duration: {self.samples[-1].elapsed_s:.0f}s\n")
        for v in self.violations:
            buf.write(f"  [{v.elapsed_s:.0f}s] {v.rule}: {v.detail}\n")
        with open(summary, "w") as f:
            f.write(buf.getvalue())

        return csv_path


@dataclass
class LoadProfile:
    """Time-varying household load."""

    base_kw: float = 0.5
    spikes: list[tuple[float, float, float]] = field(default_factory=list)

    def at(self, elapsed_minutes: float) -> float:
        load = self.base_kw
        for start_min, kw, duration_min in self.spikes:
            if start_min <= elapsed_minutes < start_min + duration_min:
                load = kw
        return load


@dataclass
class SolarProfile:
    """Time-varying solar generation (bell curve approximation)."""

    peak_kw: float = 0.0
    sunrise_hour: float = 6.0
    sunset_hour: float = 18.0

    def at(self, hour_of_day: float) -> float:
        if self.peak_kw <= 0 or hour_of_day < self.sunrise_hour:
            return 0.0
        if hour_of_day > self.sunset_hour:
            return 0.0
        mid = (self.sunrise_hour + self.sunset_hour) / 2.0
        half_span = (self.sunset_hour - self.sunrise_hour) / 2.0
        if half_span <= 0:
            return 0.0
        x = (hour_of_day - mid) / half_span
        return self.peak_kw * max(0.0, 1.0 - x * x)


@dataclass
class ScenarioConfig:
    """Full configuration for a soak test scenario."""

    name: str
    session_type: str  # "charge" or "discharge"
    window_minutes: int = 240
    initial_soc: float = 50.0
    target_soc: int = 80  # target for charge, min_soc for discharge
    battery_capacity_kwh: float = 10.0
    max_power_w: int = 10500
    load: LoadProfile = field(default_factory=LoadProfile)
    solar: SolarProfile = field(default_factory=SolarProfile)
    charge_taper_soc: float = 90.0
    battery_temperature: float = 25.0
    efficiency: float = 1.0


def _sample_from_ha_and_sim(
    ha: HAClient,
    sim: SimulatorHandle,
    elapsed_s: float,
) -> SoakSample:
    """Capture a sample from HA sensors and simulator state."""
    sim_state = sim.state()
    try:
        soc = float(ha.get_state("sensor.foxess_battery_soc"))
    except (ValueError, TypeError):
        soc = sim_state.get("soc", 0.0)
    try:
        ha_state = ha.get_state("sensor.foxess_smart_operations")
    except (requests.RequestException, KeyError, ValueError):
        ha_state = "unknown"
    try:
        attrs = ha.get_attributes("sensor.foxess_smart_operations")
        power_w = float(attrs.get("power_w", 0))
    except (requests.RequestException, KeyError, ValueError, TypeError):
        power_w = 0.0

    return SoakSample(
        elapsed_s=elapsed_s,
        wall_time=datetime.datetime.now().strftime("%H:%M:%S"),
        soc=soc,
        state=ha_state,
        power_w=power_w,
        grid_import_kw=sim_state.get("grid_import_kw", 0.0),
        grid_export_kw=sim_state.get("grid_export_kw", 0.0),
        solar_kw=sim_state.get("solar_kw", 0.0),
        load_kw=sim_state.get("load_kw", 0.0),
        bat_charge_kw=sim_state.get("bat_charge_kw", 0.0),
        bat_discharge_kw=sim_state.get("bat_discharge_kw", 0.0),
        sim_time=sim_state.get("sim_time", ""),
    )


def run_scenario(
    ha: HAClient,
    sim: SimulatorHandle,
    config: ScenarioConfig,
    recorder: SoakRecorder,
    poll_interval_s: float = 10.0,
) -> None:
    """Execute a soak scenario in real time.

    Starts a charge/discharge session, then polls HA + simulator at
    *poll_interval_s* intervals, updating the simulator's load/solar
    profiles as time progresses. Runs until the session ends or the
    window expires.
    """
    sim.set(
        soc=config.initial_soc,
        solar_kw=config.solar.at(6.0),
        load_kw=config.load.base_kw,
        battery_capacity_kwh=config.battery_capacity_kwh,
        max_power_w=config.max_power_w,
        charge_taper_soc=config.charge_taper_soc,
        battery_temperature=config.battery_temperature,
        efficiency=config.efficiency,
        fuzzing=False,
    )

    now = datetime.datetime.now(tz=datetime.UTC)
    now_min = now.hour * 60 + now.minute
    start_min = max(0, now_min - 2)
    end_min = start_min + config.window_minutes
    if end_min > 23 * 60 + 59:
        end_min = 23 * 60 + 59
        start_min = max(0, end_min - config.window_minutes)
    start_time = f"{start_min // 60:02d}:{start_min % 60:02d}:00"
    end_time = f"{end_min // 60:02d}:{end_min % 60:02d}:00"

    if config.session_type == "charge":
        service, service_data = (
            "smart_charge",
            {
                "start_time": start_time,
                "end_time": end_time,
                "target_soc": config.target_soc,
            },
        )
        expected_active = "charging"
    else:
        service, service_data = (
            "smart_discharge",
            {
                "start_time": start_time,
                "end_time": end_time,
                "min_soc": config.target_soc,
            },
        )
        expected_active = "discharging"

    for attempt in range(3):
        try:
            ha.call_service("foxess_control", service, service_data)
            break
        except RuntimeError:
            if attempt == 2:
                raise
            _log.warning("Service call failed (attempt %d), retrying...", attempt + 1)
            time.sleep(5)

    # Session may start deferred or go straight to active.
    # Charge uses "deferred", discharge uses "discharge_deferred".
    valid_states = {expected_active, "deferred", "discharge_deferred"}
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = ha.get_state("sensor.foxess_smart_operations")
        if state in valid_states:
            break
        if state in ("error", "unavailable"):
            raise RuntimeError(
                f"Session reached fatal state '{state}' "
                f"while waiting for {valid_states}"
            )
        time.sleep(1)
    else:
        raise TimeoutError(
            f"sensor.foxess_smart_operations did not reach "
            f"{valid_states} within 180s "
            f"(last: '{ha.get_state('sensor.foxess_smart_operations')}')"
        )

    total_wall_seconds = config.window_minutes * 60
    wall_start = time.monotonic()

    while (time.monotonic() - wall_start) < total_wall_seconds:
        elapsed_s = time.monotonic() - wall_start
        elapsed_min = elapsed_s / 60.0

        # Update load/solar profiles based on current time
        sim_state = sim.state()
        sim_time_str = sim_state.get("sim_time", "")
        if sim_time_str:
            try:
                st = datetime.datetime.fromisoformat(sim_time_str)
                hour = st.hour + st.minute / 60.0
            except ValueError:
                hour = 12.0
        else:
            hour = 12.0

        new_load = config.load.at(elapsed_min)
        new_solar = config.solar.at(hour)
        sim.set(load_kw=new_load, solar_kw=new_solar)

        time.sleep(poll_interval_s)

        sample = _sample_from_ha_and_sim(ha, sim, time.monotonic() - wall_start)
        recorder.record(sample)

        if sample.state in ("idle", "error", "unavailable"):
            break

    time.sleep(5)
    final_sample = _sample_from_ha_and_sim(ha, sim, time.monotonic() - wall_start)
    recorder.record(final_sample)


def check_charge_invariants(recorder: SoakRecorder, config: ScenarioConfig) -> None:
    """Verify charge session invariants from recorded samples."""
    for s in recorder.samples:
        if s.state in ("charging", "deferred") and s.soc > config.target_soc + 2:
            recorder.violate(
                "CHARGE_OVERSHOOT",
                f"SoC {s.soc:.1f}% > target {config.target_soc}% + 2%",
            )

    charging_samples = [s for s in recorder.samples if s.state == "charging"]
    if len(charging_samples) >= 3:
        start_soc = charging_samples[0].soc
        end_soc = charging_samples[-1].soc
        if end_soc < start_soc - 1:
            recorder.violate(
                "CHARGE_REGRESSION",
                f"SoC went from {start_soc:.1f}% to {end_soc:.1f}%",
            )

    if recorder.samples:
        final = recorder.samples[-1]
        if final.soc < config.target_soc - 5 and final.state not in (
            "deferred",
            "idle",
        ):
            recorder.violate(
                "CHARGE_TARGET_MISSED",
                f"Final SoC {final.soc:.1f}% < target {config.target_soc}% - 5%",
            )


def check_discharge_invariants(recorder: SoakRecorder, config: ScenarioConfig) -> None:
    """Verify discharge session invariants from recorded samples."""
    for s in recorder.samples:
        if s.state == "discharging" and s.soc < config.target_soc - 2:
            recorder.violate(
                "DISCHARGE_BELOW_MIN",
                f"SoC {s.soc:.1f}% < min_soc {config.target_soc}% - 2%",
            )

    discharging_samples = [s for s in recorder.samples if s.state == "discharging"]
    if len(discharging_samples) >= 3:
        start_soc = discharging_samples[0].soc
        end_soc = discharging_samples[-1].soc
        if end_soc > start_soc + 1:
            recorder.violate(
                "DISCHARGE_REGRESSION",
                f"SoC went from {start_soc:.1f}% to {end_soc:.1f}%",
            )


def check_grid_import_during_discharge(recorder: SoakRecorder) -> None:
    """C-001 derivative: no sustained grid import during discharge."""
    consecutive_import = 0
    for s in recorder.samples:
        if s.state == "discharging" and s.grid_import_kw > 0.1:
            consecutive_import += 1
            if consecutive_import >= 3:
                recorder.violate(
                    "GRID_IMPORT_DURING_DISCHARGE",
                    f"Grid importing {s.grid_import_kw:.2f}kW during discharge "
                    f"for 3+ consecutive samples at {s.elapsed_s:.0f}s",
                )
                consecutive_import = 0
        else:
            consecutive_import = 0


@pytest.fixture(scope="session")
def _container_built() -> None:
    _build_container_once()


@pytest.fixture(scope="session")
def _worker_ports() -> dict[str, int]:
    return {"sim": _find_free_port(), "ha": _find_free_port()}


@pytest.fixture
def foxess_sim(
    _worker_ports: dict[str, int],
) -> Generator[SimulatorHandle, None, None]:
    port = _worker_ports["sim"]
    proc = subprocess.Popen(
        ["python", "-m", "simulator", "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"http://localhost:{port}/sim/state", timeout=1)
            if r.status_code == 200:
                break
        except requests.ConnectionError:
            pass
        time.sleep(0.2)
    else:
        _kill_process(proc)
        raise RuntimeError("Simulator did not start")

    yield SimulatorHandle(f"http://localhost:{port}")
    _kill_process(proc)


@pytest.fixture
def ha_e2e(
    foxess_sim: SimulatorHandle,
    _worker_ports: dict[str, int],
    _container_built: None,
) -> Generator[HAClient, None, None]:
    ha_port = _worker_ports["ha"]
    wid = _worker_id()
    name = f"ha-soak-{os.getpid()}-{wid}"
    _stop_container(name)

    tmpdir = tempfile.mkdtemp(prefix="ha-soak-")
    shutil.copytree(str(HA_CONFIG_SEED), tmpdir, dirs_exist_ok=True)
    os.chmod(tmpdir, 0o777)
    for root, dirs, _files in os.walk(tmpdir):
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o777)

    sim_port = foxess_sim.url.rsplit(":", 1)[1]
    proc = subprocess.Popen(
        [
            "podman",
            "run",
            "--rm",
            "--name",
            name,
            "-p",
            f"{ha_port}:8123",
            "--add-host=host.containers.internal:host-gateway",
            "-v",
            f"{REPO_ROOT}/custom_components/foxess_control"
            f":/config/custom_components/foxess_control:ro,z",
            "-v",
            f"{tmpdir}:/config:Z",
            "-e",
            f"FOXESS_SIMULATOR_URL=http://host.containers.internal:{sim_port}",
            CONTAINER_IMAGE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        ha = HAClient(f"http://localhost:{ha_port}", E2E_TOKEN)
        ha.wait_ready(timeout_s=120)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                ha.get_state("sensor.foxess_battery_soc")
                break
            except requests.RequestException:
                time.sleep(2)
        else:
            try:
                logs = subprocess.run(
                    ["podman", "logs", "--tail", "200", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if logs.stdout:
                    print(logs.stdout[-3000:])
            except (subprocess.SubprocessError, OSError):
                pass
            raise TimeoutError("Integration entities not created within 120s")
    except BaseException:
        _stop_container(name)
        _kill_process(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    yield ha

    try:
        logs = subprocess.run(
            ["podman", "logs", "--tail", "500", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if logs.stdout:
            out_dir = ARTIFACT_DIR
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = out_dir / f"ha_logs_{wid}_{ts}.txt"
            with open(log_path, "w") as f:
                f.write(logs.stdout)
    except (subprocess.SubprocessError, OSError):
        pass
    _stop_container(name)
    _kill_process(proc)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def soak_recorder(
    request: pytest.FixtureRequest,
) -> Generator[SoakRecorder, None, None]:
    started_at = datetime.datetime.now(tz=datetime.UTC).isoformat()
    recorder = SoakRecorder(test_name=request.node.nodeid)
    yield recorder
    path = recorder.save()
    _log.warning("Soak artifacts saved to %s", path)
    db_path = ARTIFACT_DIR / "soak_results.db"
    scenario = request.node.name
    passed = len(recorder.violations) == 0
    run_id = save_run(db_path, recorder, scenario, started_at, passed)
    _log.info(
        "Soak DB: run_id=%d, scenario=%s, passed=%s",
        run_id,
        scenario,
        passed,
    )
