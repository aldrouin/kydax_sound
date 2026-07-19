"""Constants for the Kydax Sound integration."""

DOMAIN = "kydax_sound"

# Symetrix Jupiter control protocol default UDP port (see PROTOCOL.md)
DEFAULT_PORT = 48630

# how often to poll the appliance for current values (seconds)
POLL_SECONDS = 30

# options keys
CONF_PAUSE_GROUPS = "pause_groups"  # [{id, name, channels: [int]}]
CONF_VOLUME_SCENES = "volume_scenes"  # [{id, name, levels: {"7122": -24.0}}]

# Standard Jupiter volume fader range (PROTOCOL.md). Controller position 0
# maps to the minimum (OFF) and 65535 to the maximum.
FADER_MIN_DB = -72.0
FADER_MAX_DB = 12.0
POSITION_MAX = 65535


def db_to_position(db: float) -> int:
    """Convert a fader dB value to a 16-bit controller position."""
    span = FADER_MAX_DB - FADER_MIN_DB
    position = round((db - FADER_MIN_DB) / span * POSITION_MAX)
    return max(0, min(POSITION_MAX, position))


def position_to_db(position: int) -> float:
    """Convert a 16-bit controller position to a fader dB value."""
    span = FADER_MAX_DB - FADER_MIN_DB
    return FADER_MIN_DB + span * (position / POSITION_MAX)


def signal_update(entry_id: str) -> str:
    """Dispatcher signal for entity state refreshes."""
    return f"{DOMAIN}_{entry_id}_update"
