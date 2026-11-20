"""The "Schedule write API" option (issue #28, D-068).

``auto`` (the default) writes schedules via v0 and retries once via v1 when
v0 is refused with errno 40257; ``v0`` / ``v1`` pin one version.  The write
behaviour itself is tested against the simulator in
``test_scheduler_device_limits.py::TestScheduleApiVersion``; this module
covers the option: its default, where it is offered, and its translations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.foxess_control.const import (
    BACKEND_LOCAL,
    CONF_API_KEY,
    CONF_BACKEND,
    CONF_SCHEDULE_API_VERSION,
    SCHEDULE_API_VERSION_OPTIONS,
)
from custom_components.foxess_control.domain_data import build_config
from custom_components.foxess_control.foxess.inverter import (
    SCHEDULE_API_VERSIONS,
    Inverter,
)

from .test_handback_options import _LOCALE_FILES, _init_schema, _options_flow

_CLOUD_ENTRY = {CONF_API_KEY: "key"}


class TestConfig:
    def test_default_is_auto(self) -> None:
        assert build_config({}).schedule_api_version == "auto"

    @pytest.mark.parametrize("value", ["auto", "v0", "v1"])
    def test_saved_value_is_used(self, value: str) -> None:
        assert (
            build_config({CONF_SCHEDULE_API_VERSION: value}).schedule_api_version
            == value
        )

    def test_unrecognised_value_falls_back_to_auto(self) -> None:
        config = build_config({CONF_SCHEDULE_API_VERSION: "v9"})
        assert config.schedule_api_version == "auto"

    def test_option_values_match_the_inverter(self) -> None:
        """The option and the Inverter must agree on the vocabulary."""
        assert tuple(SCHEDULE_API_VERSION_OPTIONS) == SCHEDULE_API_VERSIONS

    def test_inverter_treats_an_unknown_version_as_auto(self) -> None:
        inv = Inverter(MagicMock(), "SN", schedule_api_version="v9")
        assert inv.schedule_api_version == "auto"


class TestOptionsForm:
    @pytest.mark.asyncio
    async def test_offered_to_cloud_entries_defaulting_to_auto(self) -> None:
        schema = await _init_schema(_options_flow(entry_data=_CLOUD_ENTRY))
        assert CONF_SCHEDULE_API_VERSION in {str(k) for k in schema.schema}
        assert schema({})[CONF_SCHEDULE_API_VERSION] == "auto"

    @pytest.mark.asyncio
    async def test_defaults_to_the_saved_value(self) -> None:
        """Pressing Submit must never change the saved version."""
        flow = _options_flow({CONF_SCHEDULE_API_VERSION: "v1"}, entry_data=_CLOUD_ENTRY)
        schema = await _init_schema(flow)
        assert schema({})[CONF_SCHEDULE_API_VERSION] == "v1"

    @pytest.mark.asyncio
    async def test_not_offered_to_local_entries(self) -> None:
        flow = _options_flow(entry_data={CONF_BACKEND: BACKEND_LOCAL})
        schema = await _init_schema(flow)
        assert CONF_SCHEDULE_API_VERSION not in {str(k) for k in schema.schema}

    @pytest.mark.asyncio
    async def test_not_offered_without_an_api_key(self) -> None:
        """Entity mode (foxess_modbus) never writes through the Open API."""
        schema = await _init_schema(_options_flow(entry_data={}))
        assert CONF_SCHEDULE_API_VERSION not in {str(k) for k in schema.schema}


class TestTranslations:
    @staticmethod
    def _load(locale_path: str) -> dict[str, Any]:
        path = Path(locale_path)
        assert path.is_file(), f"locale file missing: {locale_path}"
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    @pytest.mark.parametrize("locale_path", _LOCALE_FILES)
    def test_label_and_description(self, locale_path: str) -> None:
        step = self._load(locale_path)["options"]["step"]["init"]
        assert step["data"].get(CONF_SCHEDULE_API_VERSION)
        assert step["data_description"].get(CONF_SCHEDULE_API_VERSION)

    @pytest.mark.parametrize("locale_path", _LOCALE_FILES)
    def test_every_choice_is_labelled(self, locale_path: str) -> None:
        options = self._load(locale_path)["selector"][CONF_SCHEDULE_API_VERSION][
            "options"
        ]
        assert set(options) == set(SCHEDULE_API_VERSION_OPTIONS)
        assert all(options.values())
