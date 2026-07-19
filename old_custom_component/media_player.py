import logging
import socket

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerState,
    MediaPlayerEntityFeature,
)

from .const import DOMAIN, CHANNELS, UDP_IP, UDP_PORT

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_MUTE
)

_LOGGER = logging.getLogger(__name__)
# SCAN_INTERVAL = timedelta(seconds=2)
entities = []


def setup_platform(hass, config, add_entities, discovery_info=None) -> None:
    """Set up the custom component."""

    for channel in CHANNELS:
        entities.append(SymetrixMediaPlayer(hass, channel[0], channel[1]))

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["channels"] = []
    hass.data[DOMAIN]["channels"].extend(entities)

    add_entities(entities, True)

    def handle_set_db(call):
        entity_id = call.data.get("entity_id")
        db = call.data.get("db")
        _LOGGER.info(call)
        _LOGGER.info(entity_id)
        _LOGGER.info(db)
        for entity in entities:
            if entity.entity_id == entity_id:
                entity.set_db(db)

    def handle_set_paused(call):
        entity_id = call.data.get("entity_id")
        paused = call.data_get("paused")
        paused = bool(paused)
        for entity in entities:
            if entity.entity_id == entity_id:
                entity.set_paused = paused

    hass.services.register(DOMAIN, "set_db", handle_set_db)
    hass.services.register(DOMAIN, "set_paused", handle_set_paused)

    # Schedule an update every `SCAN_INTERVAL` seconds
    # async_track_time_interval(hass, lambda now: asyncio.create_task(symetrix.async_update()), SCAN_INTERVAL)
    return None


class SymetrixMediaPlayer(MediaPlayerEntity):
    def __init__(self, hass, channel, max_volume):
        """Initialize the media player."""
        self._hass = hass
        self._name = f"symetrix_{UDP_IP}_{channel}"
        self._channel = channel
        self._volume = 0
        self._is_muted = False
        self._max_volume = max_volume
        self._is_paused = False
        self._volume_when_muted = 0
        self._unique_id = f"symetrix_{UDP_IP}_{channel}"
        self._host = UDP_IP
        self._port = UDP_PORT
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # async def async_update(self):
    #    loop = asyncio.get_running_loop()
    #    loop.sock_sendto(self.sock, command, (self._host, self._port))
    #    await self.do_update()

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def state(self) -> MediaPlayerState:
        """State of the player."""
        return MediaPlayerState.ON

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def channel(self):
        return self._channel

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return self._volume

    @property
    def is_volume_muted(self):
        """Boolean if volume is currently muted."""
        return self._is_muted

    @property
    def is_paused(self):
        return self._is_paused

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Flag media player features that are supported."""
        return SUPPORTED_FEATURES

    def set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        if not self._is_paused:
            if volume >= self._max_volume:
                volume = self._max_volume

            if volume == 0:
                self._volume_when_muted = self._volume
                self._is_muted = True
                command_volume = 0
            else:
                self._is_muted = False
                command_volume = round(volume * 65535)

            command = f"CS {self._channel} {command_volume}\r"
            self.send_command(command)

    def set_paused(self, is_paused: bool) -> None:
        if is_paused:
            self.mute_volume(True)
            self._is_paused = is_paused
        else:
            self._is_paused = is_paused
            self.mute_volume(False)

    def set_db(self, db_value: float) -> None:
        set_value = round((float(db_value) + 72) / 84, 2)
        if set_value >= self._max_volume:
            set_value = self._max_volume

        self.set_volume_level(set_value)

    def mute_volume(self, mute: bool):
        """Mute the volume."""
        if mute:
            self._volume_when_muted = self._volume
            self._is_muted = True
            self.set_volume_level(0)
        else:
            self._is_muted = False
            self.set_volume_level(self._volume_when_muted)
            self._volume_when_muted = 0

    def update(self) -> None:
        self.do_update()

    def do_update(self) -> bool:
        """Fetch new state data for this player."""

        controllerPosition = self.send_command(f"GS {self._channel}\r")
        controllerPosition = controllerPosition.replace("\r", "")
        controllerPositionInt = int(controllerPosition)

        if controllerPositionInt:
            if controllerPositionInt == 0:
                if self._volume != 0:
                    self._volume_when_muted = self._volume
                self._is_muted = True
            else:
                self._is_muted = False

            self._volume = round((controllerPositionInt / 65535), 2)
        return True

    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self._unique_id)},
            "name": self._name,
            "manufacturer": "Symetrix",
            "model": "Jupiter 8",
        }

    def send_command(self, command):
        """Sends a UDP command and returns the response."""
        _LOGGER.info(f"Command : {command}")

        self._sock.sendto(command.encode("ASCII"), (self._host, self._port))

        received_data, addr = self._sock.recvfrom(1024)

        decoded_data = received_data.decode("ASCII")
        _LOGGER.info(f"Response : {decoded_data}")
        return decoded_data