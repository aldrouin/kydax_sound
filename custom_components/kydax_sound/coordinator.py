"""Hub for Kydax Sound: connection to the appliances and runtime state."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import timedelta

from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHANNELS,
    CONF_EVENT_BUTTONS,
    CONF_LEVELS,
    CONF_MUSISELECT_HOST,
    CONF_MUSISELECT_PORT,
    CONF_PAUSE_GROUPS,
    DEFAULT_LEVELS,
    DEFAULT_MUSISELECT_PORT,
    DEFAULT_PORT,
    DEFAULT_VOLUME_50,
    DEFAULT_VOLUME_100,
    POLL_SECONDS,
    channel_db_for_pct,
    channel_pct_for_db,
    channel_position_for_pct,
    position_to_db,
    signal_update,
)
from .musiselect import MusiSelectClient, MusiSelectError
from .symetrix import SymetrixClient, SymetrixError

_LOGGER = logging.getLogger(__name__)


@dataclass
class PauseState:
    """Runtime state of one pause group."""

    is_on: bool = False
    # positions each channel had before being muted, restored on resume
    saved: dict[int, int] = field(default_factory=dict)


@dataclass
class EventRun:
    """Runtime state of one running event."""

    task: asyncio.Task
    finishes_at: datetime | None = None


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
        musiselect_host = entry.options.get(CONF_MUSISELECT_HOST)
        self.musiselect: MusiSelectClient | None = (
            MusiSelectClient(
                musiselect_host,
                entry.options.get(CONF_MUSISELECT_PORT, DEFAULT_MUSISELECT_PORT),
            )
            if musiselect_host
            else None
        )
        # True once the appliance has answered; drives entity availability.
        # None until the first poll so the first failure is logged too.
        self.available: bool | None = None

        self.channels: dict[int, dict] = {
            channel["number"]: channel
            for channel in entry.options.get(CONF_CHANNELS, [])
        }
        self.levels: list[int] = entry.options.get(CONF_LEVELS, DEFAULT_LEVELS)
        self.pause_groups: dict[str, dict] = {
            group["id"]: group for group in entry.options.get(CONF_PAUSE_GROUPS, [])
        }
        self.event_buttons: dict[str, dict] = {
            event["id"]: event
            for event in entry.options.get(CONF_EVENT_BUTTONS, [])
        }
        self.event_runs: dict[str, EventRun] = {}
        self.pause_state: dict[str, PauseState] = {
            group_id: PauseState() for group_id in self.pause_groups
        }
        self.active_level: int | None = None

    # --- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        """Connect and start polling."""
        _LOGGER.info(
            "Starting: Symetrix at %s:%s, MusiSelect %s, %d channel(s), "
            "%d level(s), %d pause group(s), %d event(s)",
            self.entry.options[CONF_HOST],
            self.entry.options.get(CONF_PORT, DEFAULT_PORT),
            f"at {self.entry.options[CONF_MUSISELECT_HOST]}:"
            f"{self.entry.options.get(CONF_MUSISELECT_PORT, DEFAULT_MUSISELECT_PORT)}"
            if self.musiselect is not None
            else "not configured",
            len(self.channels),
            len(self.levels),
            len(self.pause_groups),
            len(self.event_buttons),
        )
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
        for run in self.event_runs.values():
            run.task.cancel()
        self.event_runs.clear()
        self.symetrix.disconnect()
        if self.musiselect is not None:
            self.musiselect.disconnect()

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

    # --- volume levels -------------------------------------------------------

    def level_values(self, level: int) -> dict[str, float | str]:
        """The dB each channel plays at for a level, keyed by channel name."""
        values: dict[str, float | str] = {}
        for channel in self.channels.values():
            db = channel_db_for_pct(
                channel.get("volume_50", DEFAULT_VOLUME_50),
                channel.get("volume_100", DEFAULT_VOLUME_100),
                level,
            )
            values[channel["name"]] = "off" if db is None else round(db, 1)
        return values

    @callback
    def seed_level(self, level: int) -> None:
        """Remember the active level after an HA restart, without writes."""
        if level in self.levels:
            self.active_level = level

    async def async_apply_level(self, level: int) -> None:
        """Set every channel to its calibrated volume for a percentage level.

        Each channel's dB is interpolated from its volume_50/volume_100
        calibration (see const.channel_db_for_pct); paused channels are
        skipped.
        """
        paused = self.paused_channels
        skipped: list[int] = []
        try:
            for number, channel in self.channels.items():
                if number in paused:
                    skipped.append(number)
                    continue
                await self.symetrix.async_set(
                    number, channel_position_for_pct(channel, level)
                )
        except SymetrixError as err:
            raise HomeAssistantError(f"Volume level failed: {err}") from err
        if skipped:
            _LOGGER.info(
                "Level %s%% skipped paused channels: %s", level, skipped
            )
        self.active_level = level
        self._dispatch()

    # --- event buttons -------------------------------------------------------

    def is_event_running(self, event_id: str) -> bool:
        return event_id in self.event_runs

    def event_finishes_at(self, event_id: str) -> datetime | None:
        run = self.event_runs.get(event_id)
        return run.finishes_at if run else None

    async def async_trigger_event(self, event_id: str) -> None:
        """Start an event: Symetrix preset, settle delay, MusiSelect command,
        then after the configured duration, the return preset.

        Blocked while any pause group is active, because a preset load on the
        Jupiter would override the paused channels.
        """
        event = self.event_buttons.get(event_id)
        if event is None:
            raise HomeAssistantError(f"Unknown event button {event_id}")
        if event_id in self.event_runs:
            return  # already running
        if self.paused_channels:
            raise HomeAssistantError(
                "A pause is active; event not triggered "
                "(une pause est active; événement non déclenché)"
            )
        if event.get("command") and self.musiselect is None:
            raise HomeAssistantError(
                "No MusiSelect device configured "
                "(aucun appareil MusiSelect configuré)"
            )

        finishes_at: datetime | None = None
        if event.get("duration"):
            finishes_at = dt_util.utcnow() + timedelta(seconds=event["duration"])
        task = self.entry.async_create_background_task(
            self.hass, self._async_run_event(event), f"kydax_sound event {event_id}"
        )
        self.event_runs[event_id] = EventRun(task=task, finishes_at=finishes_at)
        self._dispatch()

    async def _async_run_event(self, event: dict) -> None:
        """The event sequence; runs as a background task.

        Preset and MusiSelect command go out back-to-back (matching the old
        deployment); the duration is the only wait before the return preset.
        """
        try:
            preset = event.get("preset")
            command = event.get("command")
            if preset:
                await self.symetrix.async_load_preset(preset)
            if command:
                await self.musiselect.async_send(command)
            if event.get("duration"):
                await asyncio.sleep(event["duration"])
                if event.get("return_preset"):
                    await self.symetrix.async_load_preset(event["return_preset"])
            _LOGGER.info("Event '%s' finished", event["name"])
        except (SymetrixError, MusiSelectError) as err:
            _LOGGER.warning("Event '%s' failed: %s", event["name"], err)
        finally:
            self.event_runs.pop(event["id"], None)
            self._dispatch()

    async def async_cancel_event(self, event_id: str) -> None:
        """Stop a running event early and load its return preset, if any."""
        run = self.event_runs.pop(event_id, None)
        if run is None:
            return
        run.task.cancel()
        event = self.event_buttons.get(event_id)
        if event and event.get("return_preset"):
            try:
                await self.symetrix.async_load_preset(event["return_preset"])
            except SymetrixError as err:
                raise HomeAssistantError(
                    f"Return preset failed: {err}"
                ) from err
            finally:
                self._dispatch()
        else:
            self._dispatch()

    # --- polling -----------------------------------------------------------

    async def _async_poll(self, _now) -> None:
        """Read the appliance state; drives availability and the active level.

        Reading the channels also detects manual volume changes: each
        channel's dB implies a percentage through its calibration, and the
        floored average of those becomes the active level.
        """
        try:
            if self.channels:
                positions = await self._async_read_positions()
                self._update_level_from_positions(positions)
            else:
                await self.symetrix.async_ping()
        except SymetrixError as err:
            # also logs on the very first failed poll (available is None)
            if self.available is not False:
                _LOGGER.warning("Symetrix appliance is unreachable: %s", err)
            self.available = False
        else:
            if not self.available:
                _LOGGER.info("Symetrix appliance is reachable")
            self.available = True
        self._dispatch()

    async def _async_read_positions(self) -> dict[int, int]:
        """Read the current position of every configured channel."""
        numbers = sorted(self.channels)
        span = numbers[-1] - numbers[0] + 1
        if span <= 256:
            block = await self.symetrix.async_get_block(numbers[0], span)
            return {n: block[n] for n in numbers if n in block}
        positions: dict[int, int] = {}
        for number in numbers:
            positions[number] = await self.symetrix.async_get(number)
        return positions

    @callback
    def _update_level_from_positions(self, positions: dict[int, int]) -> None:
        """Derive the active level from actual channel volumes.

        Paused channels are excluded (they are muted on purpose), and so are
        flat-calibrated channels (same dB at every level, no information).
        """
        paused = self.paused_channels
        implied: list[float] = []
        for number, position in positions.items():
            channel = self.channels.get(number)
            if channel is None or number in paused:
                continue
            if position == 0:
                implied.append(0.0)
                continue
            pct = channel_pct_for_db(
                channel.get("volume_50", DEFAULT_VOLUME_50),
                channel.get("volume_100", DEFAULT_VOLUME_100),
                position_to_db(position),
            )
            if pct is not None:
                implied.append(pct)
        if implied:
            # +0.01 absorbs the 16-bit position quantization so an exactly
            # applied level (e.g. 70) never floors down to 69
            self.active_level = math.floor(
                sum(implied) / len(implied) + 0.01
            )

    # --- helpers -----------------------------------------------------------

    @callback
    def _dispatch(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
