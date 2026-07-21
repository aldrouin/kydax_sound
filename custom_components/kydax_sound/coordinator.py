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
    CONF_CHANNEL_GROUPS,
    CONF_EVENT_BUTTONS,
    CONF_LANGUAGES,
    CONF_LEVELS,
    CONF_MUSISELECT_HOST,
    CONF_MUSISELECT_PORT,
    CONF_PAUSE_GROUPS,
    DEFAULT_LEVELS,
    DEFAULT_MUSISELECT_PORT,
    DEFAULT_PORT,
    FADER_MIN_DB,
    POSITION_MAX,
    POLL_SECONDS,
    channel_db_for_level,
    channel_fraction_for_db,
    channel_level_for_db,
    channel_max_db,
    channel_position_for_fraction,
    channel_position_for_level,
    db_to_position,
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
        # last known controller position per channel (from polls and writes)
        self.channel_positions: dict[int, int] = {}
        self.pause_groups: dict[str, dict] = {
            group["id"]: group for group in entry.options.get(CONF_PAUSE_GROUPS, [])
        }
        self.channel_groups: dict[str, dict] = {
            group["id"]: group
            for group in entry.options.get(CONF_CHANNEL_GROUPS, [])
        }
        self.event_buttons: dict[str, dict] = {
            event["id"]: event
            for event in entry.options.get(CONF_EVENT_BUTTONS, [])
        }
        self.event_runs: dict[str, EventRun] = {}
        # MusiSelect programs offered globally and the one currently chosen
        self.languages: list[dict] = entry.options.get(CONF_LANGUAGES, [])
        self.language: str | None = None
        # chosen preset label per event (e.g. which zone it plays in)
        self.event_preset_selection: dict[str, str] = {}
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

    # --- writing to the appliance --------------------------------------------

    async def _async_write(self, number: int, position: int) -> int:
        """Send one channel position, never above the channel's ceiling.

        Every write in this integration goes through here, so the volume
        configured as a channel's 100% is a hard limit no matter which path
        asked for the change - level, slider, service or pause restore.
        """
        position = int(round(position))  # the protocol takes whole numbers
        channel = self.channels.get(number)
        if channel is not None:
            ceiling = db_to_position(channel_max_db(channel))
            if position > ceiling:
                _LOGGER.warning(
                    "Channel %s: %s exceeds its maximum, capped to %s",
                    number,
                    position,
                    ceiling,
                )
                position = ceiling
        position = max(0, min(POSITION_MAX, position))
        await self.symetrix.async_set(number, position)
        self.channel_positions[number] = position
        return position

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
                await self._async_write(channel, 0)
                muted.append(channel)
        except SymetrixError as err:
            for channel in muted:  # best-effort rollback of a partial pause
                try:
                    await self._async_write(channel, saved[channel])
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
                await self._async_write(channel, state.saved[channel])
            except SymetrixError as err:
                raise HomeAssistantError(f"Resume failed: {err}") from err
            state.saved.pop(channel)
        state.is_on = False

    # --- volume levels -------------------------------------------------------

    def level_values(self, level: int) -> dict[str, float | str]:
        """The dB each channel plays at for a level, keyed by channel name."""
        values: dict[str, float | str] = {}
        for channel in self.channels.values():
            db = channel_db_for_level(channel, level)
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
        written: dict[int, int] = {}
        try:
            for number, channel in self.channels.items():
                if number in paused:
                    skipped.append(number)
                    continue
                position = await self._async_write(
                    number, channel_position_for_level(channel, level)
                )
                written[number] = position
        except SymetrixError as err:
            raise HomeAssistantError(f"Volume level failed: {err}") from err
        if skipped:
            _LOGGER.info(
                "Level %s%% skipped paused channels: %s", level, skipped
            )
        self.active_level = level
        self._dispatch()

    # --- per-channel volume (media players) -----------------------------------

    def channel_pct(self, number: int) -> float | None:
        """The channel's current volume as a percentage of its level table.

        None until the position is known. Channels whose levels are all the
        same dB fall back to their position relative to that level.
        """
        position = self.channel_positions.get(number)
        channel = self.channels.get(number)
        if position is None or channel is None:
            return None
        if position == 0:
            return 0.0
        db = position_to_db(position)
        pct = channel_level_for_db(channel, db)
        if pct is not None:
            return pct
        maximum = channel_max_db(channel)
        if maximum <= FADER_MIN_DB:
            return 0.0
        span = maximum - FADER_MIN_DB
        return max(0.0, min(100.0, (db - FADER_MIN_DB) / span * 100))

    def channel_db(self, number: int) -> float | None:
        """The channel's current volume in dB, None while unknown."""
        position = self.channel_positions.get(number)
        if position is None:
            return None
        return position_to_db(position)

    def channel_fraction(self, number: int) -> float | None:
        """The channel's slider position, 0.0-1.0 of its capped range."""
        db = self.channel_db(number)
        channel = self.channels.get(number)
        if db is None or channel is None:
            return None
        return channel_fraction_for_db(channel, db)

    async def async_set_channels_fraction(
        self, numbers: list[int], fraction: float
    ) -> None:
        """Move channels' sliders to a fraction of their capped range.

        Smooth, unlike the percentage levels, which use each channel's
        configured dB per level.
        """
        unknown = [n for n in numbers if n not in self.channels]
        if unknown:
            raise HomeAssistantError(f"Unknown channel(s): {unknown}")
        paused = self.paused_channels
        written: dict[int, int] = {}
        try:
            for number in numbers:
                if number in paused:
                    continue
                written[number] = await self._async_write(
                    number,
                    channel_position_for_fraction(self.channels[number], fraction),
                )
        except SymetrixError as err:
            raise HomeAssistantError(f"Volume change failed: {err}") from err
        self._update_level_from_positions(written)
        self._dispatch()

    async def async_set_channels_db(
        self, numbers: list[int], db: float
    ) -> None:
        """Set channels to an explicit dB, never above their configured max.

        This is the old set_db behaviour: you give the dB, that dB is what
        the appliance plays - clamped to the channel's 100% level so a
        speaker cannot be over-driven.
        """
        unknown = [n for n in numbers if n not in self.channels]
        if unknown:
            raise HomeAssistantError(f"Unknown channel(s): {unknown}")
        paused = self.paused_channels
        written: dict[int, int] = {}
        try:
            for number in numbers:
                if number in paused:
                    continue
                channel = self.channels[number]
                capped = min(db, channel_max_db(channel))
                if capped < db:
                    _LOGGER.info(
                        "Channel %s capped at its maximum %.1f dB (asked %.1f)",
                        number,
                        capped,
                        db,
                    )
                written[number] = await self._async_write(
                    number, db_to_position(capped)
                )
        except SymetrixError as err:
            raise HomeAssistantError(f"Volume change failed: {err}") from err
        self._update_level_from_positions(written)
        self._dispatch()

    async def async_set_channels_pct(
        self, numbers: list[int], pct: float
    ) -> None:
        """Set several channels to a percentage in one go.

        Each channel uses its own calibration; paused channels are skipped
        rather than raising, so a group call still serves the others.
        """
        paused = self.paused_channels
        unknown = [n for n in numbers if n not in self.channels]
        if unknown:
            raise HomeAssistantError(f"Unknown channel(s): {unknown}")
        skipped: list[int] = []
        written: dict[int, int] = {}
        try:
            for number in numbers:
                if number in paused:
                    skipped.append(number)
                    continue
                written[number] = await self._async_write(
                    number, channel_position_for_level(self.channels[number], pct)
                )
        except SymetrixError as err:
            raise HomeAssistantError(f"Volume change failed: {err}") from err
        if skipped:
            _LOGGER.info("Skipped paused channels: %s", skipped)
        self._update_level_from_positions(written)
        self._dispatch()

    async def async_set_channel_pct(self, number: int, pct: float) -> None:
        """Set one channel's volume by percentage (through its calibration)."""
        channel = self.channels.get(number)
        if channel is None:
            raise HomeAssistantError(f"Unknown channel {number}")
        if number in self.paused_channels:
            raise HomeAssistantError(
                "This channel is paused (ce canal est en pause)"
            )
        try:
            position = await self._async_write(
                number, channel_position_for_level(channel, pct)
            )
        except SymetrixError as err:
            raise HomeAssistantError(f"Volume change failed: {err}") from err
        # refresh the derived active level with the new position included
        self._update_level_from_positions({number: position})
        self._dispatch()

    # --- event buttons -------------------------------------------------------

    def is_event_running(self, event_id: str) -> bool:
        return event_id in self.event_runs

    @property
    def any_event_running(self) -> bool:
        return bool(self.event_runs)

    @property
    def language_labels(self) -> list[str]:
        return [language["label"] for language in self.languages]

    @property
    def selected_language(self) -> str | None:
        """The chosen MusiSelect program, defaulting to the first one."""
        labels = self.language_labels
        if not labels:
            return None
        return self.language if self.language in labels else labels[0]

    @callback
    def set_language(self, label: str) -> None:
        if label in self.language_labels:
            self.language = label
            self._dispatch()

    def _event_command(self, event: dict) -> str | None:
        """The MusiSelect command to send.

        An event's own command wins; otherwise the globally selected
        program (the birthday song language) is used.
        """
        if event.get("command"):
            return event["command"]
        label = self.selected_language
        for language in self.languages:
            if language["label"] == label:
                return language.get("command")
        return None

    def event_preset_labels(self, event_id: str) -> list[str]:
        event = self.event_buttons.get(event_id, {})
        return [option["label"] for option in event.get("preset_options", [])]

    def selected_event_preset(self, event_id: str) -> str | None:
        """The chosen preset label (e.g. the zone), defaulting to the first."""
        labels = self.event_preset_labels(event_id)
        if not labels:
            return None
        chosen = self.event_preset_selection.get(event_id)
        return chosen if chosen in labels else labels[0]

    @callback
    def set_event_preset(self, event_id: str, label: str) -> None:
        if label in self.event_preset_labels(event_id):
            self.event_preset_selection[event_id] = label
            self._dispatch()

    def _event_preset(self, event: dict) -> int | None:
        """The Symetrix preset to load.

        Events may offer a choice of presets - one per zone - so the same
        event can play in a different area depending on the selection.
        """
        options = event.get("preset_options")
        if options:
            label = self.selected_event_preset(event["id"])
            for option in options:
                if option["label"] == label:
                    return option["preset"]
            return options[0]["preset"]
        return event.get("preset")

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
        if not self._event_preset(event) and not self._event_command(event):
            raise HomeAssistantError(
                f"Event '{event['name']}' has nothing to do for the selected "
                "choice"
            )
        if event_id in self.event_runs:
            return  # already running
        if self.event_runs:
            other = next(iter(self.event_runs))
            other_name = self.event_buttons.get(other, {}).get("name", other)
            raise HomeAssistantError(
                f"Event '{other_name}' is already running; cancel it first "
                f"(l'événement '{other_name}' est déjà en cours; annulez-le "
                "d'abord)"
            )
        if self.paused_channels:
            raise HomeAssistantError(
                "A pause is active; event not triggered "
                "(une pause est active; événement non déclenché)"
            )
        if self._event_command(event) and self.musiselect is None:
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
            preset = self._event_preset(event)
            command = self._event_command(event)
            if preset:
                await self.symetrix.async_load_preset(preset)
            if command:
                await self.musiselect.async_send(command)
            if event.get("duration"):
                await asyncio.sleep(event["duration"])
                if event.get("return_preset"):
                    await self.symetrix.async_load_preset(event["return_preset"])
            details = [
                label
                for label in (
                    self.selected_event_preset(event["id"]),
                    None if event.get("command") else self.selected_language,
                )
                if label
            ]
            _LOGGER.info(
                "Event '%s'%s finished",
                event["name"],
                f" ({', '.join(details)})" if details else "",
            )
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
        self.channel_positions.update(positions)
        paused = self.paused_channels
        implied: list[float] = []
        detail: list[str] = []
        for number, position in positions.items():
            channel = self.channels.get(number)
            if channel is None:
                continue
            name = channel.get("name", number)
            if number in paused:
                detail.append(f"{name}: paused, ignored")
                continue
            # A channel whose levels are all the same volume - typically
            # -72 dB everywhere for a zone that is not used - says nothing
            # about the active level, so it must not weigh on the average.
            pct = channel_level_for_db(channel, position_to_db(position))
            if pct is None:
                detail.append(f"{name}: same volume at every level, ignored")
                continue
            implied.append(pct)
            detail.append(
                f"{name}: {position_to_db(position):.1f} dB -> {pct:.1f}%"
            )
        if implied:
            average = sum(implied) / len(implied)
            # +0.01 absorbs the 16-bit position quantization so an exactly
            # applied level (e.g. 70) never floors down to 69
            self.active_level = math.floor(average + 0.01)
            _LOGGER.debug(
                "Active level %s%% from the average of %.2f%% | %s",
                self.active_level,
                average,
                "; ".join(detail),
            )
            if self.active_level not in self.levels:
                _LOGGER.info(
                    "No level toggle matches %s%%: the channels are not all "
                    "at the same level (%s)",
                    self.active_level,
                    "; ".join(detail),
                )

    # --- helpers -----------------------------------------------------------

    @callback
    def _dispatch(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
