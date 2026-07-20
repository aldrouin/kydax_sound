"""Media player platform for Kydax Sound: one per channel.

Each channel gets a volume slider and a mute button. The slider maps
through the channel's calibration, so a slider at 70% equals the channel's
volume-70 level. Group several channels with Home Assistant's Group helper
(type: media player) to control them with a single slider.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
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
    """Set up one media player per configured channel."""
    hub = entry.runtime_data
    async_add_entities(
        KydaxSoundChannelPlayer(hub, channel)
        for channel in entry.options.get(CONF_CHANNELS, [])
    )


class KydaxSoundChannelPlayer(KydaxSoundEntity, MediaPlayerEntity):
    """Volume slider + mute for one channel."""

    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(self, hub: KydaxSoundHub, channel: dict) -> None:
        super().__init__(hub)
        self._channel = channel
        self._number = channel["number"]
        self._attr_name = channel["name"]
        self._attr_unique_id = f"{hub.entry.entry_id}_channel_{self._number}"
        # restored on unmute; refreshed from every non-zero reading
        self._last_nonzero_pct = 70.0

    @property
    def state(self) -> MediaPlayerState:
        return MediaPlayerState.ON

    @property
    def volume_level(self) -> float | None:
        pct = self._hub.channel_pct(self._number)
        if pct is None:
            return None
        if pct > 0:
            self._last_nonzero_pct = pct
        return min(1.0, max(0.0, pct / 100))

    @property
    def is_volume_muted(self) -> bool | None:
        pct = self._hub.channel_pct(self._number)
        return None if pct is None else pct == 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"controller": self._number}
        position = self._hub.channel_positions.get(self._number)
        if position is not None:
            attrs["position"] = position
        if self._number in self._hub.paused_channels:
            attrs["paused"] = True
        return attrs

    async def async_set_volume_level(self, volume: float) -> None:
        await self._hub.async_set_channel_pct(self._number, volume * 100)

    async def async_mute_volume(self, mute: bool) -> None:
        if mute:
            pct = self._hub.channel_pct(self._number)
            if pct:
                self._last_nonzero_pct = pct
            await self._hub.async_set_channel_pct(self._number, 0)
        else:
            await self._hub.async_set_channel_pct(
                self._number, self._last_nonzero_pct
            )
