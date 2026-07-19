"""Base entity for Kydax Sound."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, signal_update
from .coordinator import KydaxSoundHub


class KydaxSoundEntity(Entity):
    """Entity attached to the Kydax Sound hub device, push-updated."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: KydaxSoundHub) -> None:
        self._hub = hub
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub.entry.entry_id)},
            name="Kydax Sound",
            manufacturer="Symetrix",
            model="Jupiter 8",
        )

    @property
    def available(self) -> bool:
        return self._hub.available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._hub.entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
