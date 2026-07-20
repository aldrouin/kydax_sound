"""Media player platform for Kydax Sound.

- one per channel: a volume slider and mute for that channel
- one per configured channel group: a single slider driving every channel
  in the group

The slider moves smoothly from silent up to the channel's configured 100%,
so dragging feels natural; it deliberately does not snap to the per-level
volumes, which are what the percentage toggles use. Its top end is the
channel's 100%, so a channel can never be driven past that - the speakers
are protected.
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
from .const import CONF_CHANNEL_GROUPS, CONF_CHANNELS, channel_max_db
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one media player per channel and per channel group."""
    hub = entry.runtime_data
    entities: list[MediaPlayerEntity] = [
        KydaxSoundChannelPlayer(hub, channel)
        for channel in entry.options.get(CONF_CHANNELS, [])
    ]
    entities.extend(
        KydaxSoundGroupPlayer(hub, group)
        for group in entry.options.get(CONF_CHANNEL_GROUPS, [])
    )
    async_add_entities(entities)


class _BasePlayer(KydaxSoundEntity, MediaPlayerEntity):
    """Shared volume/mute behavior over a set of channels."""

    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(self, hub: KydaxSoundHub) -> None:
        super().__init__(hub)
        # restored on unmute; refreshed from every non-zero reading
        self._last_nonzero = 0.7

    @property
    def _numbers(self) -> list[int]:
        raise NotImplementedError

    def _current_fraction(self) -> float | None:
        """Average slider position of the members; None while unknown."""
        values = [
            fraction
            for fraction in (
                self._hub.channel_fraction(n) for n in self._numbers
            )
            if fraction is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)

    @property
    def state(self) -> MediaPlayerState:
        return MediaPlayerState.ON

    @property
    def volume_level(self) -> float | None:
        fraction = self._current_fraction()
        if fraction is None:
            return None
        if fraction > 0:
            self._last_nonzero = fraction
        return fraction

    @property
    def is_volume_muted(self) -> bool | None:
        fraction = self._current_fraction()
        return None if fraction is None else fraction == 0

    async def async_set_volume_level(self, volume: float) -> None:
        await self._hub.async_set_channels_fraction(self._numbers, volume)

    async def async_mute_volume(self, mute: bool) -> None:
        if mute:
            fraction = self._current_fraction()
            if fraction:
                self._last_nonzero = fraction
            await self._hub.async_set_channels_fraction(self._numbers, 0.0)
        else:
            await self._hub.async_set_channels_fraction(
                self._numbers, self._last_nonzero
            )


class KydaxSoundChannelPlayer(_BasePlayer):
    """Volume slider + mute for one channel."""

    def __init__(self, hub: KydaxSoundHub, channel: dict) -> None:
        super().__init__(hub)
        self._number = channel["number"]
        self._attr_name = channel["name"]
        self._attr_unique_id = f"{hub.entry.entry_id}_channel_{self._number}"

    @property
    def _numbers(self) -> list[int]:
        return [self._number]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        channel = self._hub.channels.get(self._number, {})
        attrs: dict[str, Any] = {
            "controller": self._number,
            # the slider's 100% - the channel can never be driven past it
            "max_db": round(channel_max_db(channel), 1) if channel else None,
        }
        db = self._hub.channel_db(self._number)
        if db is not None:
            attrs["db"] = round(db, 1)
        position = self._hub.channel_positions.get(self._number)
        if position is not None:
            attrs["position"] = position
        if self._number in self._hub.paused_channels:
            attrs["paused"] = True
        return attrs


class KydaxSoundGroupPlayer(_BasePlayer):
    """One slider driving every channel of a configured group.

    Each member still gets its own calibrated dB, so the group slider at
    70% means "every zone at its level-70 volume", not one flat value.
    """

    _attr_icon = "mdi:speaker-multiple"

    def __init__(self, hub: KydaxSoundHub, group: dict) -> None:
        super().__init__(hub)
        self._group_id = group["id"]
        self._attr_name = group["name"]
        self._attr_unique_id = f"{hub.entry.entry_id}_group_{group['id']}"

    @property
    def _numbers(self) -> list[int]:
        group = self._hub.channel_groups.get(self._group_id, {})
        return [n for n in group.get("channels", []) if n in self._hub.channels]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        paused = self._hub.paused_channels
        return {
            "channels": self._numbers,
            "paused_channels": [n for n in self._numbers if n in paused],
        }
