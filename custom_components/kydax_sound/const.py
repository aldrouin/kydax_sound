"""Constants for the Kydax Sound integration."""

DOMAIN = "kydax_sound"

# Symetrix Jupiter control protocol default UDP port (see PROTOCOL.md)
DEFAULT_PORT = 48630

# MusiSelect music source device (see PROTOCOL.md); port is static in
# practice but kept configurable
CONF_MUSISELECT_HOST = "musiselect_host"
CONF_MUSISELECT_PORT = "musiselect_port"
DEFAULT_MUSISELECT_PORT = 2325

# how often to poll the appliance for current values (seconds)
POLL_SECONDS = 30

# options keys
# channels: [{number, name, volume_50, volume_100}]
# volume_50/volume_100 are the channel's calibrated dB at 50% and 100%;
# other percentages are interpolated linearly in dB.
CONF_CHANNELS = "channels"
CONF_LEVELS = "levels"  # [int] percentages, one button each
CONF_PAUSE_GROUPS = "pause_groups"  # [{id, name, channels: [int]}]
CONF_CHANNEL_GROUPS = "channel_groups"  # [{id, name, channels: [int]}]
CONF_EVENT_BUTTONS = "event_buttons"  # [{id, name, preset, command, delay, duration, return_preset}]

DEFAULT_LEVELS = [0, 50, 60, 70, 80, 90, 100]
DEFAULT_VOLUME_50 = -33.0
DEFAULT_VOLUME_100 = -11.0

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


def channel_db_for_pct(
    volume_50: float, volume_100: float, pct: float
) -> float | None:
    """The dB a channel should play at for a percentage level.

    None means off (controller position 0). 50-100% interpolates linearly in
    dB between the channel's two calibration points; below 50% the line
    continues down to the fader minimum at 0%.
    """
    if pct <= 0:
        return None
    pct = min(pct, 100)
    if pct >= 50:
        return volume_50 + (volume_100 - volume_50) * (pct - 50) / 50
    return FADER_MIN_DB + (volume_50 - FADER_MIN_DB) * pct / 50


def channel_pct_for_db(
    volume_50: float, volume_100: float, db: float
) -> float | None:
    """Inverse of channel_db_for_pct: the percentage a channel's current dB
    implies, given its calibration.

    None when the channel carries no information (flat calibration, where
    every level sounds the same). Clamped to 0-100.
    """
    if volume_100 == volume_50:
        return None
    if db >= volume_50:
        pct = 50 + 50 * (db - volume_50) / (volume_100 - volume_50)
    elif volume_50 == FADER_MIN_DB:
        pct = 0.0
    else:
        pct = 50 * (db - FADER_MIN_DB) / (volume_50 - FADER_MIN_DB)
    return max(0.0, min(100.0, pct))


def channel_position_for_pct(channel: dict, pct: float) -> int:
    """The controller position for a channel dict at a percentage level."""
    db = channel_db_for_pct(
        channel.get("volume_50", DEFAULT_VOLUME_50),
        channel.get("volume_100", DEFAULT_VOLUME_100),
        pct,
    )
    return 0 if db is None else db_to_position(db)


def signal_update(entry_id: str) -> str:
    """Dispatcher signal for entity state refreshes."""
    return f"{DOMAIN}_{entry_id}_update"
