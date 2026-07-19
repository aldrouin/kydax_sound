"""Button platform for Kydax Sound.

- one button per volume scene (like the old switchson_* buttons)
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
from .const import CONF_CHANNELS, CONF_VOLUME_SCENES
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
    entities.extend(
        VolumeSceneButton(hub, scene)
        for scene in entry.options.get(CONF_VOLUME_SCENES, [])
    )
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


class VolumeSceneButton(KydaxSoundEntity, ButtonEntity):
    """Applies one volume scene (paused channels are skipped)."""

    _attr_icon = "mdi:volume-medium"

    def __init__(self, hub: KydaxSoundHub, scene: dict) -> None:
        super().__init__(hub)
        self._scene = scene
        self._attr_name = scene["name"]
        self._attr_unique_id = f"{hub.entry.entry_id}_scene_{scene['id']}"

    async def async_press(self) -> None:
        await self._hub.async_apply_scene(self._scene["id"])
