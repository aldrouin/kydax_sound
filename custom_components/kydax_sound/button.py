"""Button platform for Kydax Sound.

- one button per volume level percentage (like the old switchson_* buttons)
- reset-to-default-volumes button
- diagnostic flash-LEDs button

Events are switches (switch.py) so their running state is visible.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KydaxSoundConfigEntry
from .const import CONF_CHANNELS
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buttons."""
    hub = entry.runtime_data
    entities: list[ButtonEntity] = [FlashUnitButton(hub)]
    if entry.options.get(CONF_CHANNELS):
        entities.append(ResetVolumesButton(hub))
        entities.extend(VolumeLevelButton(hub, level) for level in hub.levels)
    async_add_entities(entities)


class FlashUnitButton(KydaxSoundEntity, ButtonEntity):
    """Flashes the appliance's front panel LEDs — a visible comms test."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "flash_unit"

    def __init__(self, hub: KydaxSoundHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry.entry_id}_flash_unit"

    async def async_press(self) -> None:
        await self._hub.symetrix.async_flash()


class ResetVolumesButton(KydaxSoundEntity, ButtonEntity):
    """Sets every configured channel to its default volume percentage."""

    _attr_translation_key = "reset_volumes"
    _attr_icon = "mdi:volume-equal"

    def __init__(self, hub: KydaxSoundHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry.entry_id}_reset_volumes"

    async def async_press(self) -> None:
        await self._hub.async_reset_volumes()


class VolumeLevelButton(KydaxSoundEntity, ButtonEntity):
    """Sets every channel to its calibrated volume for this percentage.

    Paused channels are skipped.
    """

    _attr_translation_key = "volume_level"
    _attr_icon = "mdi:volume-medium"

    def __init__(self, hub: KydaxSoundHub, level: int) -> None:
        super().__init__(hub)
        self._level = level
        self._attr_unique_id = f"{hub.entry.entry_id}_level_{level}"
        self._attr_translation_placeholders = {"level": str(level)}

    async def async_press(self) -> None:
        await self._hub.async_apply_level(self._level)
