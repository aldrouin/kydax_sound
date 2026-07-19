"""Button platform for Kydax Sound."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KydaxSoundConfigEntry
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buttons."""
    async_add_entities([FlashUnitButton(entry.runtime_data)])


class FlashUnitButton(KydaxSoundEntity, ButtonEntity):
    """Flashes the appliance's front panel LEDs — a visible comms test."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "flash_unit"

    def __init__(self, hub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry.entry_id}_flash_unit"

    async def async_press(self) -> None:
        await self._hub.symetrix.async_flash()
