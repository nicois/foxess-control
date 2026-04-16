"""Inverter adapter protocol and entity-mode base implementation.

Brand integrations implement :class:`InverterAdapter` to provide
the control primitives that smart charge/discharge algorithms need.
Most brands will use :class:`EntityAdapter` directly, which controls
the inverter through standard HA ``select`` and ``number`` entities.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from .types import WorkMode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class InverterAdapter(Protocol):
    """Interface that brand integrations implement for inverter control.

    All methods accept *hass* so adapters can make HA service calls
    (entity mode) or run blocking API calls via the executor (cloud mode).
    """

    async def apply_mode(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
        power_w: int | None = None,
        fd_soc: int = 11,
    ) -> None:
        """Set the inverter to *mode* with optional power and SoC target."""
        ...

    async def remove_override(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
    ) -> None:
        """Remove an active override, reverting to self-use."""
        ...

    def get_max_power_w(self) -> int:
        """Return the inverter's maximum power in watts."""
        ...


class EntityAdapter:
    """Control an inverter via HA select/number entities.

    This is the primary adapter for most brands.  It maps
    :class:`WorkMode` values to the brand's entity option strings
    and writes power/SoC via ``number.set_value`` service calls.

    Subclass and override :meth:`pre_mode_change` for brands that
    need additional entity writes before a mode switch (e.g. Huawei
    TOU slot configuration).
    """

    def __init__(
        self,
        mode_map: dict[WorkMode, str],
        work_mode_entity: str,
        charge_power_entity: str | None = None,
        discharge_power_entity: str | None = None,
        min_soc_entity: str | None = None,
        max_power_w: int = 12000,
    ) -> None:
        self._mode_map = mode_map
        self._work_mode_entity = work_mode_entity
        self._charge_power_entity = charge_power_entity
        self._discharge_power_entity = discharge_power_entity
        self._min_soc_entity = min_soc_entity
        self._max_power_w = max_power_w

    def get_max_power_w(self) -> int:
        return self._max_power_w

    async def pre_mode_change(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
        power_w: int | None = None,
        fd_soc: int = 11,
    ) -> None:
        """Hook for brands that need extra entity writes before mode switch.

        Default implementation does nothing.  Override in subclass for
        brands like Huawei that need TOU slot configuration.
        """

    @staticmethod
    def _service_domain(entity_id: str, default: str) -> str:
        """Derive the service domain from the entity ID prefix.

        HA's ``select.select_option`` only works for platform-backed
        ``select`` entities.  ``input_select`` entities require the
        ``input_select`` service domain.  Same for ``number`` vs
        ``input_number``.
        """
        prefix = entity_id.split(".", 1)[0]
        if prefix.startswith("input_"):
            return prefix
        return default

    async def apply_mode(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
        power_w: int | None = None,
        fd_soc: int = 11,
    ) -> None:
        """Set inverter mode by writing to external HA entities."""
        _LOGGER.debug(
            "Entity adapter: setting mode=%s power=%s fd_soc=%d",
            mode,
            f"{power_w}W" if power_w is not None else "unchanged",
            fd_soc,
        )

        await self.pre_mode_change(hass, mode, power_w, fd_soc)

        mode_option = self._mode_map.get(mode)
        if mode_option:
            domain = self._service_domain(self._work_mode_entity, "select")
            await hass.services.async_call(
                domain,
                "select_option",
                {"entity_id": self._work_mode_entity, "option": mode_option},
            )

        if power_w is not None and mode in (
            WorkMode.FORCE_CHARGE,
            WorkMode.FORCE_DISCHARGE,
        ):
            power_entity = (
                self._charge_power_entity
                if mode == WorkMode.FORCE_CHARGE
                else self._discharge_power_entity
            )
            if power_entity:
                domain = self._service_domain(power_entity, "number")
                await hass.services.async_call(
                    domain,
                    "set_value",
                    {"entity_id": power_entity, "value": power_w},
                )

        if self._min_soc_entity and mode == WorkMode.FORCE_DISCHARGE:
            domain = self._service_domain(self._min_soc_entity, "number")
            await hass.services.async_call(
                domain,
                "set_value",
                {"entity_id": self._min_soc_entity, "value": fd_soc},
            )

    async def remove_override(
        self,
        hass: HomeAssistant,
        mode: WorkMode,
    ) -> None:
        """Revert to self-use mode."""
        await self.apply_mode(hass, WorkMode.SELF_USE)
