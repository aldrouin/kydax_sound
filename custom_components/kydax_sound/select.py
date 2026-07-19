"""Select platform for Kydax Sound: the active volume scene.

Picking a scene writes each configured channel's dB level to the appliance,
skipping channels locked by an active pause switch.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import KydaxSoundConfigEntry
from .const import CONF_VOLUME_SCENES
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the volume scene selector when scenes are configured."""
    if entry.options.get(CONF_VOLUME_SCENES):
        async_add_entities([KydaxSoundVolumeSceneSelect(entry.runtime_data)])


class KydaxSoundVolumeSceneSelect(KydaxSoundEntity, SelectEntity, RestoreEntity):
    """Shows the last applied volume scene; selecting one applies it."""

    _attr_translation_key = "volume_scene"
    _attr_icon = "mdi:volume-medium"

    def __init__(self, hub: KydaxSoundHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry.entry_id}_volume_scene"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            scene_id = self._scene_id_for_name(last.state)
            if scene_id is not None:
                self._hub.seed_scene(scene_id)

    def _scene_id_for_name(self, name: str) -> str | None:
        for scene_id, scene in self._hub.scenes.items():
            if scene["name"] == name:
                return scene_id
        return None

    @property
    def options(self) -> list[str]:
        return [scene["name"] for scene in self._hub.scenes.values()]

    @property
    def current_option(self) -> str | None:
        if self._hub.active_scene_id is None:
            return None
        scene = self._hub.scenes.get(self._hub.active_scene_id)
        return scene["name"] if scene else None

    async def async_select_option(self, option: str) -> None:
        scene_id = self._scene_id_for_name(option)
        if scene_id is not None:
            await self._hub.async_apply_scene(scene_id)
