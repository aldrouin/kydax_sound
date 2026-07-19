"""Hub for Kydax Sound: connection to the appliances and runtime state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PAUSE_GROUPS,
    CONF_VOLUME_SCENES,
    DEFAULT_PORT,
    POLL_SECONDS,
    db_to_position,
    signal_update,
)
from .symetrix import SymetrixClient, SymetrixError

_LOGGER = logging.getLogger(__name__)


@dataclass
class PauseState:
    """Runtime state of one pause group."""

    is_on: bool = False
    # positions each channel had before being muted, restored on resume
    saved: dict[int, int] = field(default_factory=dict)


class KydaxSoundHub:
    """Holds the appliance connections and all runtime state.

    Same pattern as kydax_light's KydaxEngine: a poll loop instead of a
    heartbeat, entities subscribe via the dispatcher signal and never poll.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsubs: list[CALLBACK_TYPE] = []
        self.symetrix = SymetrixClient(
            entry.options[CONF_HOST], entry.options.get(CONF_PORT, DEFAULT_PORT)
        )
        # True once the appliance has answered; drives entity availability.
        self.available = False

        self.pause_groups: dict[str, dict] = {
            group["id"]: group for group in entry.options.get(CONF_PAUSE_GROUPS, [])
        }
        self.scenes: dict[str, dict] = {
            scene["id"]: scene for scene in entry.options.get(CONF_VOLUME_SCENES, [])
        }
        self.pause_state: dict[str, PauseState] = {
            group_id: PauseState() for group_id in self.pause_groups
        }
        self.active_scene_id: str | None = None

    # --- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        """Connect and start polling."""
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_poll, timedelta(seconds=POLL_SECONDS)
            )
        )
        await self._async_poll(dt_util.now())

    @callback
    def async_stop(self) -> None:
        """Tear down listeners and the connection."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.symetrix.disconnect()

    # --- pause groups ------------------------------------------------------

    @property
    def paused_channels(self) -> set[int]:
        """Channels locked by an active pause group; scene writes skip them."""
        channels: set[int] = set()
        for group_id, state in self.pause_state.items():
            if state.is_on:
                channels.update(self.pause_groups[group_id]["channels"])
        return channels

    def is_paused(self, group_id: str) -> bool:
        state = self.pause_state.get(group_id)
        return state is not None and state.is_on

    def saved_positions(self, group_id: str) -> dict[int, int]:
        state = self.pause_state.get(group_id)
        return dict(state.saved) if state else {}

    @callback
    def seed_pause(self, group_id: str, saved: dict[int, int]) -> None:
        """Mark a group paused after an HA restart, without touching the device."""
        if (state := self.pause_state.get(group_id)) is not None:
            state.is_on = True
            state.saved = dict(saved)

    async def async_set_pause(self, group_id: str, on: bool) -> None:
        """Pause (mute + lock) or resume a group of channels."""
        group = self.pause_groups.get(group_id)
        state = self.pause_state.get(group_id)
        if group is None or state is None:
            raise HomeAssistantError(f"Unknown pause group {group_id}")

        if on and not state.is_on:
            await self._async_pause_channels(group["channels"], state)
        elif not on and state.is_on:
            await self._async_resume_channels(state)
        self._dispatch()

    async def _async_pause_channels(
        self, channels: list[int], state: PauseState
    ) -> None:
        saved: dict[int, int] = {}
        muted: list[int] = []
        try:
            for channel in channels:
                saved[channel] = await self.symetrix.async_get(channel)
            for channel in channels:
                await self.symetrix.async_set(channel, 0)
                muted.append(channel)
        except SymetrixError as err:
            for channel in muted:  # best-effort rollback of a partial pause
                try:
                    await self.symetrix.async_set(channel, saved[channel])
                except SymetrixError:
                    _LOGGER.warning("Rollback failed for channel %s", channel)
            raise HomeAssistantError(f"Pause failed: {err}") from err
        state.saved = saved
        state.is_on = True

    async def _async_resume_channels(self, state: PauseState) -> None:
        # Restored channels are popped as they succeed, so retrying after a
        # partial failure only touches the remaining ones.
        for channel in list(state.saved):
            try:
                await self.symetrix.async_set(channel, state.saved[channel])
            except SymetrixError as err:
                raise HomeAssistantError(f"Resume failed: {err}") from err
            state.saved.pop(channel)
        state.is_on = False

    # --- volume scenes -----------------------------------------------------

    @callback
    def seed_scene(self, scene_id: str) -> None:
        """Remember the active scene after an HA restart, without writes."""
        if scene_id in self.scenes:
            self.active_scene_id = scene_id

    async def async_apply_scene(self, scene_id: str) -> None:
        """Write every channel level of a scene, skipping paused channels."""
        scene = self.scenes.get(scene_id)
        if scene is None:
            raise HomeAssistantError(f"Unknown volume scene {scene_id}")
        paused = self.paused_channels
        skipped: list[int] = []
        try:
            for channel_str, level_db in scene["levels"].items():
                channel = int(channel_str)
                if channel in paused:
                    skipped.append(channel)
                    continue
                await self.symetrix.async_set(channel, db_to_position(level_db))
        except SymetrixError as err:
            raise HomeAssistantError(f"Volume scene failed: {err}") from err
        if skipped:
            _LOGGER.info(
                "Scene %s skipped paused channels: %s", scene["name"], skipped
            )
        self.active_scene_id = scene_id
        self._dispatch()

    # --- polling -----------------------------------------------------------

    async def _async_poll(self, _now) -> None:
        """Verify the appliance is reachable; drives entity availability."""
        try:
            await self.symetrix.async_ping()
        except SymetrixError as err:
            if self.available:
                _LOGGER.warning("Symetrix appliance is unreachable: %s", err)
            self.available = False
        else:
            if not self.available:
                _LOGGER.info("Symetrix appliance is reachable")
            self.available = True
        self._dispatch()

    # --- helpers -----------------------------------------------------------

    @callback
    def _dispatch(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
