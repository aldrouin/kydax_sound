"""Select platform for Kydax Sound: the active volume level.

Shows which percentage level was applied last; picking one applies it (each
channel gets its calibrated dB for that percentage, paused channels are
skipped).
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import KydaxSoundConfigEntry
from .const import CONF_CHANNELS
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the volume level selector when channels and levels exist."""
    hub = entry.runtime_data
    if entry.options.get(CONF_CHANNELS) and hub.levels:
        async_add_entities([KydaxSoundVolumeLevelSelect(hub)])


class KydaxSoundVolumeLevelSelect(KydaxSoundEntity, SelectEntity, RestoreEntity):
    """Shows the last applied volume level; selecting one applies it."""

    _attr_translation_key = "volume_level"
    _attr_icon = "mdi:volume-medium"

    def __init__(self, hub: KydaxSoundHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry.entry_id}_volume_level"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state.isdigit():
            self._hub.seed_level(int(last.state))

    @property
    def options(self) -> list[str]:
        return [str(level) for level in self._hub.levels]

    @property
    def current_option(self) -> str | None:
        if self._hub.active_level is None:
            return None
        return str(self._hub.active_level)

    async def async_select_option(self, option: str) -> None:
        await self._hub.async_apply_level(int(option))
