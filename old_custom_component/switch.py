import asyncio
from homeassistant.components.switch import SwitchEntity

from .const import DOMAIN, VOLUME_VALUES, PAUSE_CHANNELS


class SymetrixPauseSwitch(SwitchEntity):
    """Class to create mute buttons"""

    def __init__(self, name, channels) -> None:
        self._name = name
        self._value = False
        self._attr_unique_id = f"symetrix_pause_switch_{name}"
        self._channels = channels

    @property
    def name(self):
        return self._name

    @property
    def is_on(self):
        return self._value

    async def async_turn_on(self, **kwargs):
        self._value = True
        for entity in self.hass.data[DOMAIN]["channels"]:
            if entity.channel in self._channels:
                entity.set_paused(self._value)

        await self.async_update_ha_state(force_refresh=True)

    async def async_turn_off(self, **kwargs):
        self._value = False
        for entity in self.hass.data[DOMAIN]["channels"]:
            if entity.channel in self._channels:
                entity.set_paused(self._value)

        await self.async_update_ha_state(force_refresh=True)


class SymetrixVolumeSwitch(SwitchEntity):
    """Class to create volume buttons"""

    def __init__(self, name, value) -> None:
        self._name = name
        self._attr_unique_id = f"symetrix_volume_switch_{name}"
        self._value = value

    @property
    def name(self):
        return self._name

    @property
    def is_on(self):
        return self.hass.data[DOMAIN]["volume_switch_state"] == self._value

    async def async_turn_on(self, **kwargs):
        if self._value != self.hass.data[DOMAIN]["volume_switch_state"]:
            for entity in self.hass.data[DOMAIN]["channels"]:
                if self._value in VOLUME_VALUES:
                    if entity.channel in VOLUME_VALUES[self._value]:
                        entity.set_db(VOLUME_VALUES[self._value][entity.channel])

        self.hass.data[DOMAIN]["volume_switch_state"] = self._value
        await self.async_update_ha_state(force_refresh=True)
        await self.update_other_switches()

    async def update_other_switches(self):
        """Update other switches"""
        for entity in self.hass.data[DOMAIN]["volume_switch"]:
            if entity != self:
                await entity.async_update_ha_state(force_refresh=True)

    async def async_turn_off(self, **kwargs):
        await self.async_turn_on()


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Init platform for switches"""
    if DOMAIN not in hass.data:
        hass.data.setdefault(DOMAIN, {})

    volume_switches = [
        SymetrixVolumeSwitch("switchson_0", 0),
        SymetrixVolumeSwitch("switchson_50", 50),
        SymetrixVolumeSwitch("switchson_60", 60),
        SymetrixVolumeSwitch("switchson_70", 70),
        SymetrixVolumeSwitch("switchson_80", 80),
        SymetrixVolumeSwitch("switchson_90", 90),
        SymetrixVolumeSwitch("switchson_100", 100),
    ]

    pause_switches = [SymetrixPauseSwitch(f"{s[0]}", s[1]) for s in PAUSE_CHANNELS]

    hass.data[DOMAIN]["volume_switch_state"] = 0
    hass.data[DOMAIN]["volume_switch"] = []
    hass.data[DOMAIN]["pause_switch"] = []
    hass.data[DOMAIN]["volume_switch"].extend(volume_switches)
    hass.data[DOMAIN]["pause_switch"].extend(pause_switches)
    async_add_entities(volume_switches)
    async_add_entities(pause_switches)
