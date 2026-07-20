"""Button platform for Kydax Sound: the diagnostic flash-LEDs button.

Volume levels and events are switches (switch.py) so their state is visible.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KydaxSoundConfigEntry
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buttons."""
    async_add_entities([FlashUnitButton(entry.runtime_data)])


class FlashUnitButton(KydaxSoundEntity, ButtonEntity):
    """Flashes the appliance's front panel LEDs — a visible comms test.

    Hidden by default: pressable from the integration's device page, but
    kept off dashboards.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False
    _attr_translation_key = "flash_unit"

    def __init__(self, hub: KydaxSoundHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry.entry_id}_flash_unit"

    async def async_press(self) -> None:
        await self._hub.symetrix.async_flash()
