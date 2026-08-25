"""PV-only energy sensor vs. inverter-output energy (Energy dashboard).

Production symptom (live KH10, 42 kWh battery, AEST, 2026-08-26): the
entity Home Assistant presents as *solar* energy — translation key
``generation_energy``, English "Solar Generation Energy", German
"Solarenergie" (``sensor.foxess_solarenergie``) — climbed all night
while the panels produced nothing.  It is fed by the FoxESS cloud
variable ``generation``, which is the inverter's cumulative AC **output**
energy and therefore includes everything the battery discharged.

The user had wired that entity as the HA Energy dashboard's *solar*
source and ``sensor.foxess_entladeenergie`` (battery discharge) as its
*battery* source, so battery discharge was counted twice: one night's
home consumption came out as 26.6 kWh against an actual house load of
7.2 kWh.

Raw cloud history for that night (167 samples, 18:00 → 08:00 AEST)::

    PVEnergyTotal   first= 2610.100 last= 2610.300 delta=  0.200
    generation      first= 4970.300 last= 4988.500 delta= 18.200
    pvPower         first=    0.000 last=    0.000 max=0.076 (dawn)
    generationPower first=    0.725 last=    1.652 max=6.729

``PVEnergyTotal`` is the genuine PV-only lifetime counter and was not
polled at all.  These tests drive the real REST path (FoxESSClient →
``/op/v0/device/real/query`` → Inverter → FoxESSDataCoordinator →
FoxESSPolledSensor) against the project simulator (C-028) and assert on
observable entity state plus the user-facing translation catalogue —
because the displayed name comes from ``translations/*.json``
(``_attr_translation_key``), not from the descriptor's ``name`` field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.foxess_control.const import POLLED_VARIABLES
from custom_components.foxess_control.coordinator import FoxESSDataCoordinator
from custom_components.foxess_control.domain_data import build_config
from custom_components.foxess_control.foxess.client import FoxESSClient
from custom_components.foxess_control.foxess.inverter import Inverter
from custom_components.foxess_control.sensor import (
    POLLED_SENSOR_DESCRIPTIONS,
    FoxESSPolledSensor,
)

if TYPE_CHECKING:
    from .conftest import SimulatorHandle

_INTEGRATION = Path("custom_components/foxess_control")
_LOCALE_FILES = [
    str(_INTEGRATION / "strings.json"),
    *(
        str(_INTEGRATION / "translations" / f"{loc}.json")
        for loc in (
            "en",
            "de",
            "es",
            "fr",
            "it",
            "ja",
            "nl",
            "pl",
            "pt",
            "zh-Hans",
        )
    ),
]

# Words that make a user read an entity name as "this is my solar yield".
# Deliberately latin-script only: applied to the English catalogue and to
# German, the locale the production report came from ("Solarenergie").
_SOLAR_WORDS = ("solar", "pv", "photovoltaic", "photovoltaik")

# Variables that genuinely carry photovoltaic-only measurements.  Anything
# else must not be presented to the user as solar.
_PV_ONLY_VARIABLES = frozenset({"pvPower", "pv1Power", "pv2Power", "PVEnergyTotal"})


def _claims_solar(name: str) -> bool:
    """True when *name* tells the user the entity measures solar yield."""
    low = name.lower()
    return any(word in low for word in _SOLAR_WORDS)


def _entity_names(locale_path: str) -> dict[str, str]:
    """translation key -> displayed sensor name for one locale file."""
    data = json.loads(Path(locale_path).read_text(encoding="utf-8"))
    sensors = data.get("entity", {}).get("sensor", {})
    return {key: str(val.get("name", "")) for key, val in sensors.items()}


def _desc(variable: str) -> Any:
    for d in POLLED_SENSOR_DESCRIPTIONS:
        if d.variable == variable:
            return d
    raise AssertionError(
        f"no polled sensor descriptor for FoxESS variable {variable!r}; "
        f"variables present: {sorted(d.variable for d in POLLED_SENSOR_DESCRIPTIONS)}"
    )


def _energy_keys_claiming_solar(locale_path: str) -> dict[str, str]:
    """Cumulative-energy sensors whose displayed name claims solar yield.

    Returns ``{translation key: displayed name}`` for every
    ``TOTAL_INCREASING`` energy descriptor whose name in *locale_path*
    reads as solar generation.
    """
    names = _entity_names(locale_path)
    found: dict[str, str] = {}
    for d in POLLED_SENSOR_DESCRIPTIONS:
        if d.device_class is not SensorDeviceClass.ENERGY:
            continue
        name = names.get(d.unique_id_suffix, "")
        if name and _claims_solar(name):
            found[d.unique_id_suffix] = name
    return found


class _Rig:
    """Real coordinator + real polled sensor entities over the simulator.

    Nothing is mocked between the simulator's HTTP API and the HA sensor
    entity: the production ``FoxESSClient`` signs and posts to
    ``/op/v0/device/real/query``, the production ``Inverter`` parses the
    ``datas`` array, the production ``FoxESSDataCoordinator`` builds the
    data dict, and production ``FoxESSPolledSensor`` entities render it.
    """

    def __init__(self, sim: SimulatorHandle, monkeypatch: pytest.MonkeyPatch) -> None:
        FoxESSClient.MIN_REQUEST_INTERVAL = 0.0
        # The coordinator reads IntegrationConfig off hass (C-035); a bare
        # MagicMock hass would hand back a MagicMock "additional PV
        # variable", so bind a real (empty) config.
        monkeypatch.setattr(
            "custom_components.foxess_control.coordinator._cfg",
            lambda _hass: build_config({}),
        )
        client = FoxESSClient("test-api-key", base_url=sim.url)
        self.inverter = Inverter(client, "SIM0001")
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.data = {}
        # DataUpdateCoordinator.__init__ calls frame.report_usage(), which
        # needs HA's frame helper initialised (same seam as
        # tests/test_coordinator.py).
        with patch("homeassistant.helpers.frame.report_usage"):
            self.coordinator = FoxESSDataCoordinator(
                MagicMock(), self.inverter, update_interval_seconds=300
            )
        self.sensors: dict[str, FoxESSPolledSensor] = {
            d.unique_id_suffix: FoxESSPolledSensor(self.coordinator, entry, d)
            for d in POLLED_SENSOR_DESCRIPTIONS
        }

    def poll(self) -> dict[str, Any]:
        """Run one coordinator refresh, exactly as the 5-minute tick does."""
        data = self.coordinator._fetch_all()
        self.coordinator.data = data
        return data

    def state(self, translation_key: str) -> float | None:
        """Observable entity state for the sensor with *translation_key*."""
        sensor = self.sensors.get(translation_key)
        assert sensor is not None, (
            f"no sensor entity with translation key {translation_key!r}; "
            f"present: {sorted(self.sensors)}"
        )
        return sensor.native_value


@pytest.fixture
def rig(foxess_sim: SimulatorHandle, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    return _Rig(foxess_sim, monkeypatch)


# ---------------------------------------------------------------------------
# The production symptom
# ---------------------------------------------------------------------------


class TestSolarEnergyEntityTracksPvOnly:
    """The entity HA presents as solar energy must measure PV, only PV."""

    def test_flat_overnight_while_battery_discharges(
        self, foxess_sim: SimulatorHandle, rig: _Rig
    ) -> None:
        """Zero sun, battery serving the house: solar energy must not move.

        This is the reported failure.  Before the fix the only cumulative
        energy sensor named as solar was ``generation_energy``, fed by the
        inverter's AC-output counter, so it rose by the battery's whole
        night of discharge — double-counting it on the Energy dashboard.
        """
        solar_keys = _energy_keys_claiming_solar(
            str(_INTEGRATION / "translations" / "en.json")
        )
        assert solar_keys, (
            "no cumulative-energy sensor is presented to the user as solar "
            "generation, so the HA Energy dashboard has no solar source to "
            "wire up"
        )

        foxess_sim.set(fuzzing=False, soc=90, solar_kw=0.0, load_kw=1.2)
        rig.poll()
        before = {key: rig.state(key) for key in solar_keys}
        output_before = rig.state("generation_energy")
        assert output_before is not None

        # One night: 8 h of house load carried entirely by the battery.
        foxess_sim.fast_forward(8 * 3600, step=900)
        rig.poll()
        after = {key: rig.state(key) for key in solar_keys}
        output_after = rig.state("generation_energy")

        # Sanity: the night really happened — the inverter put energy out
        # and the panels stayed dark.
        assert output_after is not None
        assert output_after > output_before + 1.0, (
            "simulator did not discharge: inverter output energy went from "
            f"{output_before} to {output_after} kWh"
        )
        assert rig.state("pv_power") == 0.0

        for key, name in solar_keys.items():
            assert before[key] is not None
            assert after[key] == pytest.approx(before[key], abs=1e-6), (
                f"{name!r} (translation key {key!r}) is presented to the "
                f"user as solar generation energy but rose "
                f"{(after[key] or 0) - (before[key] or 0):.3f} kWh overnight "
                f"with pvPower == 0 — it is tracking inverter output "
                f"(battery discharge), which double-counts the battery on "
                f"the HA Energy dashboard"
            )

    def test_rises_with_pv_and_stays_below_inverter_output(
        self, foxess_sim: SimulatorHandle, rig: _Rig
    ) -> None:
        """Panels producing while the battery also discharges.

        Solar energy must climb (it is real yield) but by strictly less
        than the inverter's output, which carries PV *plus* battery.
        """
        solar_keys = _energy_keys_claiming_solar(
            str(_INTEGRATION / "translations" / "en.json")
        )
        assert solar_keys

        foxess_sim.set(fuzzing=False, soc=80, solar_kw=3.0, load_kw=5.0)
        rig.poll()
        before = {key: rig.state(key) for key in solar_keys}
        output_before = rig.state("generation_energy")
        assert output_before is not None

        foxess_sim.fast_forward(2 * 3600, step=900)
        rig.poll()
        after = {key: rig.state(key) for key in solar_keys}
        output_after = rig.state("generation_energy")
        assert output_after is not None

        output_delta = output_after - output_before
        assert output_delta > 0
        for key, name in solar_keys.items():
            assert before[key] is not None
            assert after[key] is not None
            delta = (after[key] or 0.0) - (before[key] or 0.0)
            assert delta > 0.0, f"{name!r} did not rise with 3 kW of PV"
            assert delta < output_delta, (
                f"{name!r} rose {delta:.3f} kWh but inverter output rose only "
                f"{output_delta:.3f} kWh — with the battery discharging as "
                f"well, PV yield must be the smaller of the two, so this "
                f"sensor is not measuring PV"
            )

    def test_solar_energy_sensor_is_fed_by_a_pv_only_variable(self) -> None:
        """Whatever we call solar must come from a PV-only variable."""
        solar_keys = _energy_keys_claiming_solar(
            str(_INTEGRATION / "translations" / "en.json")
        )
        by_key = {d.unique_id_suffix: d for d in POLLED_SENSOR_DESCRIPTIONS}
        for key, name in solar_keys.items():
            variable = by_key[key].variable
            assert variable in _PV_ONLY_VARIABLES, (
                f"{name!r} (key {key!r}) is named as solar generation but is "
                f"fed by FoxESS variable {variable!r}, which is not a "
                f"PV-only measurement (PV-only: "
                f"{sorted(_PV_ONLY_VARIABLES)})"
            )

    def test_german_locale_solar_energy_is_pv_only(self) -> None:
        """The reported entity was German ``sensor.foxess_solarenergie``."""
        solar_keys = _energy_keys_claiming_solar(
            str(_INTEGRATION / "translations" / "de.json")
        )
        by_key = {d.unique_id_suffix: d for d in POLLED_SENSOR_DESCRIPTIONS}
        for key, name in solar_keys.items():
            variable = by_key[key].variable
            assert variable in _PV_ONLY_VARIABLES, (
                f"German name {name!r} (key {key!r}) reads as solar yield "
                f"but is fed by {variable!r} — this is the entity the user "
                f"wired into the Energy dashboard as their solar source"
            )


# ---------------------------------------------------------------------------
# The new PV-only sensor
# ---------------------------------------------------------------------------


class TestPvEnergySensor:
    def test_pv_energy_total_is_polled(self) -> None:
        """``PVEnergyTotal`` must be in the shared poll list.

        Safe for every model: a variable the device does not support is
        silently omitted from ``datas`` and the request still answers
        ``errno: 0`` (probed read-only against a live KH10, 2026-08-26).
        """
        assert "PVEnergyTotal" in POLLED_VARIABLES

    def test_descriptor_shape(self) -> None:
        """Energy, kWh, total_increasing, enabled — an Energy-dashboard
        source has to be all four."""
        desc = _desc("PVEnergyTotal")
        assert desc.unique_id_suffix == "pv_energy"
        assert desc.device_class is SensorDeviceClass.ENERGY
        assert desc.state_class is SensorStateClass.TOTAL_INCREASING
        assert desc.unit == "kWh"
        assert desc.enabled_default is True
        assert desc.entity_category is None

    def test_value_reaches_the_entity_over_the_real_rest_path(
        self, foxess_sim: SimulatorHandle, rig: _Rig
    ) -> None:
        foxess_sim.set(fuzzing=False, solar_kw=2.0, load_kw=0.5)
        foxess_sim.fast_forward(3600, step=900)
        data = rig.poll()

        assert data["PVEnergyTotal"] == pytest.approx(rig.state("pv_energy"), rel=1e-9)
        assert (rig.state("pv_energy") or 0.0) > 0.0

    def test_unsupported_on_this_model_degrades_gracefully(
        self, foxess_sim: SimulatorHandle, rig: _Rig
    ) -> None:
        """A model that does not report ``PVEnergyTotal``.

        The variable is simply absent from ``datas``; the poll must
        succeed, the PV energy entity must go unavailable (``None``) and
        every other sensor must be unaffected.
        """
        foxess_sim.set(
            fuzzing=False,
            soc=70,
            solar_kw=1.0,
            load_kw=0.5,
            unsupported_variables=["PVEnergyTotal"],
        )
        data = rig.poll()

        assert "PVEnergyTotal" not in data
        assert rig.state("pv_energy") is None
        # The rest of the poll is intact.
        for key in (
            "battery_soc",
            "pv_power",
            "loads_energy",
            "generation_energy",
            "feedin_energy",
            "discharge_energy_total",
        ):
            assert rig.state(key) is not None, f"{key} lost by the missing var"

        # A second poll must not accumulate damage either.
        assert rig.poll()["SoC"] is not None
        assert rig.state("pv_energy") is None

    def test_null_value_is_unavailable_not_zero(
        self, foxess_sim: SimulatorHandle, rig: _Rig
    ) -> None:
        """Present-but-null must read unavailable, never 0 kWh.

        A ``TOTAL_INCREASING`` sensor that reports 0 makes HA's statistics
        engine treat it as a meter reset and inject a spurious jump on the
        next real reading.
        """
        foxess_sim.set(fuzzing=False, solar_kw=1.0, load_kw=0.5)
        data = rig.poll()
        assert data["PVEnergyTotal"] is not None
        # coordinator.data is the plain dict production hands the entities
        # (REST poll and WS inject both write it directly).
        data["PVEnergyTotal"] = None
        self_check = rig.state("pv_energy")
        assert self_check is None, f"expected unavailable, got {self_check!r}"


# ---------------------------------------------------------------------------
# The renamed output sensors
# ---------------------------------------------------------------------------


class TestInverterOutputSensors:
    def test_output_energy_keeps_tracking_inverter_output(
        self, foxess_sim: SimulatorHandle, rig: _Rig
    ) -> None:
        """The relabelled sensor still reports what it always reported."""
        assert _desc("generation").unique_id_suffix == "generation_energy"

        foxess_sim.set(fuzzing=False, soc=90, solar_kw=0.0, load_kw=1.5)
        rig.poll()
        before = rig.state("generation_energy")
        assert before is not None

        foxess_sim.fast_forward(2 * 3600, step=900)
        rig.poll()
        after = rig.state("generation_energy")
        assert after is not None
        assert after > before + 1.0, (
            "inverter output energy must keep rising while the battery "
            "supplies the house — it measures AC output, not PV"
        )

    def test_output_power_tracks_inverter_output(
        self, foxess_sim: SimulatorHandle, rig: _Rig
    ) -> None:
        assert _desc("generationPower").unique_id_suffix == "generation_power"

        foxess_sim.set(fuzzing=False, soc=90, solar_kw=0.0, load_kw=1.5)
        rig.poll()

        assert rig.state("pv_power") == 0.0
        assert rig.state("generation_power") == pytest.approx(1.5, abs=0.01)

    @pytest.mark.parametrize("key", ["generation_energy", "generation_power"])
    def test_english_name_does_not_claim_solar(self, key: str) -> None:
        names = _entity_names(str(_INTEGRATION / "translations" / "en.json"))
        assert key in names
        assert not _claims_solar(names[key]), (
            f"{key!r} is named {names[key]!r} — it is fed by an "
            f"inverter-output variable, so a solar name invites users to "
            f"wire it into the Energy dashboard as their solar source "
            f"(the production bug)"
        )

    @pytest.mark.parametrize("key", ["generation_energy", "generation_power"])
    def test_german_name_does_not_claim_solar(self, key: str) -> None:
        names = _entity_names(str(_INTEGRATION / "translations" / "de.json"))
        assert key in names
        assert not _claims_solar(names[key]), (
            f"German {key!r} is named {names[key]!r}; "
            f"'Solarenergie' is exactly what misled the reporter"
        )

    @pytest.mark.parametrize("key", ["generation_energy", "generation_power"])
    def test_icon_is_not_a_solar_icon(self, key: str) -> None:
        """Output-side sensors must not carry a solar icon."""
        icons = json.loads((_INTEGRATION / "icons.json").read_text(encoding="utf-8"))
        icon = icons.get("entity", {}).get("sensor", {}).get(key, {}).get("default", "")
        assert icon, f"no icon declared for {key!r}"
        assert "solar" not in icon, f"{key!r} still uses solar icon {icon!r}"

        desc_icon = next(
            d.icon for d in POLLED_SENSOR_DESCRIPTIONS if d.unique_id_suffix == key
        )
        assert "solar" not in desc_icon, (
            f"{key!r} descriptor still uses solar icon {desc_icon!r}"
        )


# ---------------------------------------------------------------------------
# Upgrade safety and catalogue completeness
# ---------------------------------------------------------------------------


class TestUpgradeSafetyAndTranslations:
    def test_existing_translation_keys_are_unchanged(self) -> None:
        """Friendly-name changes only.

        ``unique_id`` is ``f"{entry_id}_{unique_id_suffix}"`` and the
        registry keeps the entity_id of an already-registered entity, so
        renaming is safe for the hundreds of existing installs — but only
        as long as the *keys* stay put.
        """
        suffixes = {d.unique_id_suffix for d in POLLED_SENSOR_DESCRIPTIONS}
        assert {"generation_energy", "generation_power"} <= suffixes
        assert _desc("generation").unique_id_suffix == "generation_energy"
        assert _desc("generationPower").unique_id_suffix == "generation_power"

    def test_no_duplicate_unique_id_suffixes(self) -> None:
        suffixes = [d.unique_id_suffix for d in POLLED_SENSOR_DESCRIPTIONS]
        assert len(suffixes) == len(set(suffixes))

    @pytest.mark.parametrize("locale_path", _LOCALE_FILES)
    def test_locale_declares_pv_energy(self, locale_path: str) -> None:
        """Without an entry the sensor renders as the raw key in that
        locale (same discipline as ``charge_slack``)."""
        names = _entity_names(locale_path)
        assert "pv_energy" in names, (
            f"{locale_path} has no entity.sensor.pv_energy entry; the new "
            f"PV energy sensor would display its raw translation key"
        )
        assert names["pv_energy"].strip(), f"{locale_path}: empty pv_energy name"

    @pytest.mark.parametrize("locale_path", _LOCALE_FILES)
    def test_locale_energy_names_are_distinct(self, locale_path: str) -> None:
        """PV energy and inverter-output energy must not share a name —
        indistinguishable names are how the sources got swapped."""
        names = _entity_names(locale_path)
        pv = names.get("pv_energy")
        out = names.get("generation_energy")
        assert pv and out
        assert pv != out, f"{locale_path}: pv_energy and generation_energy agree"

    def test_pv_energy_has_an_icon(self) -> None:
        icons = json.loads((_INTEGRATION / "icons.json").read_text(encoding="utf-8"))
        icon = (
            icons.get("entity", {})
            .get("sensor", {})
            .get("pv_energy", {})
            .get("default", "")
        )
        assert "solar" in icon, f"pv_energy should carry a solar icon, got {icon!r}"


# ---------------------------------------------------------------------------
# The same mislabelling elsewhere
# ---------------------------------------------------------------------------


class TestDocumentedSemantics:
    def test_api_doc_does_not_call_generation_solar(self) -> None:
        """``docs/api/foxess-cloud-api.md`` is where the mislabelling
        originated; a re-implementer reading it would repeat the bug."""
        doc = Path("docs/api/foxess-cloud-api.md").read_text(encoding="utf-8")
        rows = [
            line
            for line in doc.splitlines()
            if line.strip().startswith("| `generation`")
        ]
        assert rows, "no `generation` row in the cumulative-counter table"
        for row in rows:
            assert not _claims_solar(row), (
                f"docs/api/foxess-cloud-api.md still describes `generation` "
                f"as solar: {row.strip()!r}"
            )

    def test_api_doc_documents_pv_energy_total(self) -> None:
        doc = Path("docs/api/foxess-cloud-api.md").read_text(encoding="utf-8")
        assert "PVEnergyTotal" in doc, (
            "the PV-only lifetime counter is undocumented, which is why it "
            "was never polled"
        )
        assert "todayYield" in doc, (
            "todayYield reads 0.0 on KH10 — that trap must be documented "
            "so nobody reaches for it as the PV source"
        )
