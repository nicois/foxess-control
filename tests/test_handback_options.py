"""The opt-in ``scheduler_handback`` toggle in the options flow.

Task 5 of the scheduler-handback work (issues #16, #4).  The constant,
the ``IntegrationConfig`` field and the capture machinery already exist;
what is tested here is the *only* way a user can reach the feature — the
options dialog — plus the two things that make that dialog trustworthy:

1. **The default is OFF, and it stays OFF for entries that predate the
   feature.**  Hundreds of installs will pick this version up.  HA
   replaces the whole options dict with whatever the flow returns, and
   the value it returns for an untouched field is the *schema default*.
   A default of ``True`` would therefore enable handback — and with it
   writes to the user's own Min SoC register — for anyone who so much as
   opens the dialog and presses Submit.

2. **Saving the dialog disturbs nothing else.**  Same mechanism, same
   hazard, wider blast radius: any field whose default fails to echo the
   saved value is silently destructive.

The captured Min SoC is surfaced beside the toggle via
``description_placeholders`` (C-020): the captured value is
authoritative and never re-read, so a user who changes their floor in
the FoxESS app while handback is on has it reverted.  The remedy is to
toggle the option off and on, which re-captures — and a remedy nobody
can discover is not a remedy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import voluptuous as vol

from custom_components.foxess_control.config_flow import FoxessControlOptionsFlow
from custom_components.foxess_control.const import (
    CONF_ADDITIONAL_PV_POWER_VARIABLE,
    CONF_API_MIN_SOC,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BMS_POLLING_INTERVAL,
    CONF_GRID_EXPORT_LIMIT,
    CONF_MIN_POWER_CHANGE,
    CONF_MIN_SOC_ON_GRID,
    CONF_POLLING_INTERVAL,
    CONF_SCHEDULER_HANDBACK,
    CONF_SMART_HEADROOM,
    CONF_WEB_USERNAME,
    CONF_WS_MODE,
    DOMAIN,
    WS_MODE_ALWAYS,
)
from custom_components.foxess_control.domain_data import (
    FoxESSControlData,
    build_config,
)


def _options_flow(
    options: dict[str, Any] | None = None,
    *,
    entry_data: dict[str, Any] | None = None,
    captured_min_soc: int | None = None,
    domain_data_loaded: bool = True,
) -> FoxessControlOptionsFlow:
    """An options flow with *real* dicts for ``options`` and ``data``.

    ``test_config_flow._make_options_flow`` leaves ``config_entry.data`` a
    ``MagicMock``, whose ``.get()`` is truthy for every key.  That is fine
    for tests that only care about submission, but it makes any assertion
    about *which* fields the schema contains meaningless.  Both sides are
    real here.
    """
    config_entry = MagicMock()
    config_entry.options = dict(options or {})
    config_entry.data = dict(entry_data or {})

    hass = MagicMock()
    # No foxess_modbus: the init step then finishes the flow itself rather
    # than continuing to the entity-mapping step.
    hass.config_entries.async_entries = MagicMock(return_value=[])
    if domain_data_loaded:
        dd = FoxESSControlData()
        dd.captured_min_soc_on_grid = captured_min_soc
        hass.data = {DOMAIN: dd}
    else:
        hass.data = {}

    flow = FoxessControlOptionsFlow(config_entry)
    flow.hass = hass
    return flow


async def _init_schema(flow: FoxessControlOptionsFlow) -> vol.Schema:
    """Render the init step and return the schema it was shown with."""
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_init(None)
    schema = flow.async_show_form.call_args.kwargs["data_schema"]
    assert isinstance(schema, vol.Schema)
    return schema


async def _placeholders(flow: FoxessControlOptionsFlow) -> dict[str, str]:
    """Render the init step and return its description placeholders."""
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_init(None)
    placeholders = flow.async_show_form.call_args.kwargs.get("description_placeholders")
    assert placeholders is not None, (
        "the init step was shown without description_placeholders, so the "
        "captured Min SoC cannot appear anywhere in the dialog (C-020)"
    )
    return dict(placeholders)


# A deliberately non-default value for every option the init step owns, so
# "the save preserved it" cannot pass by coincidentally matching a
# built-in default.
_RICH_OPTIONS: dict[str, Any] = {
    CONF_MIN_SOC_ON_GRID: 17,
    CONF_BATTERY_CAPACITY_KWH: 10.4,
    CONF_MIN_POWER_CHANGE: 350,
    CONF_API_MIN_SOC: 4,
    CONF_POLLING_INTERVAL: 170,
    CONF_BMS_POLLING_INTERVAL: 900,
    CONF_SMART_HEADROOM: 7,
    CONF_GRID_EXPORT_LIMIT: 4600,
    CONF_WS_MODE: WS_MODE_ALWAYS,
    CONF_ADDITIONAL_PV_POWER_VARIABLE: "meterPower2",
}


class TestSchedulerHandbackInOptionsSchema:
    """The toggle exists, is a boolean, and remembers its saved value."""

    @pytest.mark.asyncio
    async def test_option_appears_in_schema(self) -> None:
        schema = await _init_schema(_options_flow({}))
        assert CONF_SCHEDULER_HANDBACK in {str(k) for k in schema.schema}

    @pytest.mark.asyncio
    async def test_option_is_a_boolean_field(self) -> None:
        """A bool validates; a non-bool is rejected.

        Guards against the field being declared ``str`` or ``vol.Any``,
        which would let ``"false"`` through and then read as truthy.
        """
        schema = await _init_schema(_options_flow({}))
        assert schema({CONF_SCHEDULER_HANDBACK: True})[CONF_SCHEDULER_HANDBACK] is True
        with pytest.raises(vol.Invalid):
            schema({CONF_SCHEDULER_HANDBACK: "not a bool"})

    @pytest.mark.asyncio
    async def test_defaults_to_false_when_option_absent(self) -> None:
        """An entry that predates the feature must read as OFF.

        The schema default is exactly what HA submits for a field the
        user did not touch, so this *is* the upgrade-safety guarantee.
        """
        schema = await _init_schema(_options_flow({}))
        assert schema({})[CONF_SCHEDULER_HANDBACK] is False

    @pytest.mark.asyncio
    async def test_pre_feature_entry_builds_a_config_with_handback_off(self) -> None:
        """Options dict → schema default → ``IntegrationConfig``, all OFF."""
        schema = await _init_schema(_options_flow(dict(_RICH_OPTIONS)))
        assert build_config(schema({})).scheduler_handback is False

    @pytest.mark.asyncio
    async def test_default_reflects_saved_true(self) -> None:
        """Reopening the dialog shows the toggle already on."""
        schema = await _init_schema(_options_flow({CONF_SCHEDULER_HANDBACK: True}))
        assert schema({})[CONF_SCHEDULER_HANDBACK] is True

    @pytest.mark.asyncio
    async def test_default_reflects_saved_false(self) -> None:
        schema = await _init_schema(_options_flow({CONF_SCHEDULER_HANDBACK: False}))
        assert schema({})[CONF_SCHEDULER_HANDBACK] is False


class TestSchedulerHandbackRoundTrip:
    """Saving the dialog reaches ``IntegrationConfig.scheduler_handback``."""

    @pytest.mark.asyncio
    async def test_saving_true_round_trips(self) -> None:
        flow = _options_flow(dict(_RICH_OPTIONS))
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})

        schema = await _init_schema(flow)
        await flow.async_step_init(dict(schema({CONF_SCHEDULER_HANDBACK: True})))

        saved = flow.async_create_entry.call_args.kwargs["data"]
        assert saved[CONF_SCHEDULER_HANDBACK] is True
        assert build_config(saved).scheduler_handback is True

    @pytest.mark.asyncio
    async def test_saving_false_round_trips(self) -> None:
        flow = _options_flow({CONF_SCHEDULER_HANDBACK: True})
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})

        schema = await _init_schema(flow)
        await flow.async_step_init(dict(schema({CONF_SCHEDULER_HANDBACK: False})))

        saved = flow.async_create_entry.call_args.kwargs["data"]
        assert saved[CONF_SCHEDULER_HANDBACK] is False
        assert build_config(saved).scheduler_handback is False


class TestOptionsFlowPreservesOtherOptions:
    """Adding a field must not make the dialog destructive.

    The user's route when they only want to flip the handback toggle is
    "open, toggle, Submit"; every other field is submitted at its schema
    default.  An options flow that silently reset a user's polling
    interval, export limit or WebSocket mode would be far worse than this
    feature is valuable.
    """

    @pytest.mark.asyncio
    async def test_schema_defaults_echo_every_saved_option(self) -> None:
        flow = _options_flow(
            dict(_RICH_OPTIONS), entry_data={CONF_WEB_USERNAME: "someone"}
        )
        defaults = (await _init_schema(flow))({})
        for key, value in _RICH_OPTIONS.items():
            assert defaults[key] == value, (
                f"option {key!r} would be reset from {value!r} to "
                f"{defaults.get(key)!r} just by opening and saving the dialog"
            )

    @pytest.mark.asyncio
    async def test_toggling_handback_preserves_every_other_option(self) -> None:
        flow = _options_flow(
            dict(_RICH_OPTIONS), entry_data={CONF_WEB_USERNAME: "someone"}
        )
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})

        schema = await _init_schema(flow)
        await flow.async_step_init(dict(schema({CONF_SCHEDULER_HANDBACK: True})))

        saved = flow.async_create_entry.call_args.kwargs["data"]
        for key, value in _RICH_OPTIONS.items():
            assert saved[key] == value, (
                f"option {key!r} was reset from {value!r} to {saved.get(key)!r} "
                "by a save that only meant to enable scheduler handback"
            )

    @pytest.mark.asyncio
    async def test_runtime_config_survives_a_no_op_save(self) -> None:
        """The strongest form: not "the keys are there" but "nothing moved"."""
        flow = _options_flow(
            dict(_RICH_OPTIONS), entry_data={CONF_WEB_USERNAME: "someone"}
        )
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})

        schema = await _init_schema(flow)
        await flow.async_step_init(dict(schema({})))
        saved = flow.async_create_entry.call_args.kwargs["data"]

        assert build_config(saved) == build_config(dict(_RICH_OPTIONS))


class TestCapturedMinSocIsVisible:
    """C-020: the remembered floor must be readable from the dialog.

    Without it, "toggle the option off and on to re-capture" is advice
    about a value the user cannot see.
    """

    @pytest.mark.asyncio
    async def test_captured_value_is_offered_as_a_placeholder(self) -> None:
        placeholders = await _placeholders(_options_flow({}, captured_min_soc=15))
        assert "15" in placeholders["captured_min_soc"]

    @pytest.mark.asyncio
    async def test_zero_percent_is_shown_not_swallowed(self) -> None:
        """0 % is a real captured value — issue #4 is precisely about it."""
        placeholders = await _placeholders(_options_flow({}, captured_min_soc=0))
        assert "0" in placeholders["captured_min_soc"]

    @pytest.mark.asyncio
    async def test_nothing_captured_is_not_reported_as_a_number(self) -> None:
        shown = (await _placeholders(_options_flow({}, captured_min_soc=None)))[
            "captured_min_soc"
        ]
        assert not any(ch.isdigit() for ch in shown), (
            f"nothing has been captured, but the dialog would show {shown!r} — "
            "a number here is a claim about the user's inverter that this "
            "integration has not actually read"
        )

    @pytest.mark.asyncio
    async def test_dialog_opens_when_domain_data_is_absent(self) -> None:
        """Setup may have failed; the options dialog must still open."""
        flow = _options_flow({}, domain_data_loaded=False)
        flow.async_show_form = MagicMock(return_value={"type": "form"})

        await flow.async_step_init(None)

        flow.async_show_form.assert_called_once()


_LOCALE_FILES = [
    "custom_components/foxess_control/strings.json",
    "custom_components/foxess_control/translations/en.json",
    "custom_components/foxess_control/translations/de.json",
    "custom_components/foxess_control/translations/es.json",
    "custom_components/foxess_control/translations/fr.json",
    "custom_components/foxess_control/translations/it.json",
    "custom_components/foxess_control/translations/ja.json",
    "custom_components/foxess_control/translations/nl.json",
    "custom_components/foxess_control/translations/pl.json",
    "custom_components/foxess_control/translations/pt.json",
    "custom_components/foxess_control/translations/zh-Hans.json",
]


class TestSchedulerHandbackTranslations:
    """Every shipped locale must carry the new option's strings.

    ``strings.json`` alone is not enough: HA serves options-flow labels
    from ``translations/<lang>.json``, so a locale missing the key
    renders the raw option name.  ``tests/test_card_translations.py``
    enforces the equivalent guarantee for the Lovelace card — note that
    it covers the card's JS tables only, not these JSON files.
    """

    @staticmethod
    def _init_step(locale_path: str) -> dict[str, Any]:
        path = Path(locale_path)
        assert path.is_file(), f"locale file missing: {locale_path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        step: dict[str, Any] = data["options"]["step"]["init"]
        return step

    @pytest.mark.parametrize("locale_path", _LOCALE_FILES)
    def test_locale_has_label(self, locale_path: str) -> None:
        labels = self._init_step(locale_path).get("data", {})
        assert labels.get(CONF_SCHEDULER_HANDBACK), (
            f"{locale_path} has no options.step.init.data."
            f"{CONF_SCHEDULER_HANDBACK} — users in this locale would see the "
            "raw option name on a toggle that rewrites their Min SoC"
        )

    @pytest.mark.parametrize("locale_path", _LOCALE_FILES)
    def test_locale_has_description(self, locale_path: str) -> None:
        descriptions = self._init_step(locale_path).get("data_description", {})
        assert descriptions.get(CONF_SCHEDULER_HANDBACK), (
            f"{locale_path} has no options.step.init.data_description."
            f"{CONF_SCHEDULER_HANDBACK}"
        )

    @pytest.mark.parametrize("locale_path", _LOCALE_FILES)
    def test_locale_description_shows_the_captured_value(
        self, locale_path: str
    ) -> None:
        """C-020 in every language, not only English.

        The placeholder is what makes the remembered floor visible; a
        translation that drops it turns the field back into an opaque
        toggle for that locale.
        """
        text = self._init_step(locale_path)["data_description"][CONF_SCHEDULER_HANDBACK]
        assert "{captured_min_soc}" in text, (
            f"{locale_path} description for {CONF_SCHEDULER_HANDBACK} does not "
            "interpolate {captured_min_soc}"
        )
