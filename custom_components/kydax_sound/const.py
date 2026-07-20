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
# channels: [{number, name, levels: {"50": -30.0, "60": -28.0, ...}}]
# Every percentage level has its own dB per channel: the levels are what
# guests hear, not a formula, so nothing is inferred between them.
CONF_CHANNELS = "channels"
CONF_LEVELS = "levels"  # [int] percentages offered, one toggle each
CONF_PAUSE_GROUPS = "pause_groups"  # [{id, name, channels: [int]}]
CONF_CHANNEL_GROUPS = "channel_groups"  # [{id, name, channels: [int]}]
CONF_EVENT_BUTTONS = "event_buttons"  # [{id, name, preset, command, ...}]
# MusiSelect programs offered globally, e.g. the birthday song languages:
# [{label, command}]. One selector applies to every event that has no
# command of its own.
CONF_LANGUAGES = "languages"
# optional custom names for the entities whose label comes from a
# translation; empty values keep the translated default.
# {"volume_level": "Volume {level} %", "volume_select": ..., "language": ...,
#  "event_end": "Fin de {event}"}
CONF_LABELS = "labels"
LABEL_VOLUME_LEVEL = "volume_level"
LABEL_VOLUME_SELECT = "volume_select"
LABEL_LANGUAGE = "language"
LABEL_EVENT_END = "event_end"


def custom_label(options: dict, key: str, **placeholders) -> str | None:
    """A configured label with its placeholders filled in, if one is set."""
    template = (options.get(CONF_LABELS) or {}).get(key)
    if not template:
        return None
    try:
        return template.format(**placeholders)
    except (KeyError, IndexError, ValueError):
        return template

DEFAULT_LEVELS = [0, 50, 60, 70, 80, 90, 100]
DEFAULT_LEVEL_DB = -30.0

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


def channel_level_table(channel: dict) -> dict[int, float]:
    """The channel's {level percentage: dB} table, sanitised."""
    table: dict[int, float] = {}
    for level, db in (channel.get("levels") or {}).items():
        try:
            table[int(level)] = float(db)
        except (TypeError, ValueError):
            continue
    return {level: db for level, db in table.items() if level > 0}


def channel_max_db(channel: dict) -> float:
    """The loudest dB this channel may ever play: the value set for its
    highest level (normally 100%).

    Everything goes through this ceiling - the volume slider, the level
    toggles and the set_channel_db service - so a channel can never be
    driven past the volume configured as its maximum, even if a lower level
    was mistyped louder than the top one.
    """
    table = channel_level_table(channel)
    if not table:
        return FADER_MIN_DB
    return table[max(table)]


def channel_db_for_level(channel: dict, level: float) -> float | None:
    """The dB this channel plays at a percentage level.

    Configured levels are used exactly as given. A percentage that is not
    configured (only reachable through a service call or a slider drag)
    falls between its neighbours; above the highest level the value is
    capped there. None means off.
    """
    if level <= 0:
        return None
    table = channel_level_table(channel)
    if not table:
        return None
    ceiling = channel_max_db(channel)
    if level in table:
        return min(table[level], ceiling)
    points = sorted(table.items())
    if level >= points[-1][0]:
        return points[-1][1]  # never louder than the configured maximum
    if level <= points[0][0]:
        low_level, low_db = points[0]
        return FADER_MIN_DB + (low_db - FADER_MIN_DB) * (level / low_level)
    for (l1, d1), (l2, d2) in zip(points, points[1:]):
        if l1 <= level <= l2:
            return d1 + (d2 - d1) * (level - l1) / (l2 - l1)
    return points[-1][1]


def channel_position_for_level(channel: dict, level: float) -> int:
    """The controller position for a channel at a percentage level."""
    db = channel_db_for_level(channel, level)
    return 0 if db is None else db_to_position(db)


def channel_level_for_db(channel: dict, db: float) -> float | None:
    """Inverse: the percentage a channel's current dB corresponds to.

    None when the channel carries no information (every level configured to
    the same dB, so its volume says nothing about the active level).
    """
    table = channel_level_table(channel)
    if not table:
        return None
    points = sorted(table.items())
    if len({d for _, d in points}) == 1:
        return None
    if db >= points[-1][1]:
        return float(points[-1][0])
    if db <= FADER_MIN_DB:
        return 0.0
    if db <= points[0][1]:
        low_level, low_db = points[0]
        if low_db <= FADER_MIN_DB:
            return 0.0
        return low_level * (db - FADER_MIN_DB) / (low_db - FADER_MIN_DB)
    for (l1, d1), (l2, d2) in zip(points, points[1:]):
        if d1 <= db <= d2:
            if d2 == d1:
                return float(l1)
            return l1 + (l2 - l1) * (db - d1) / (d2 - d1)
    return float(points[-1][0])


def channel_fraction_for_db(channel: dict, db: float) -> float:
    """Where a dB sits on the channel's slider, 0.0 to 1.0.

    The slider is a smooth range from the fader minimum up to the channel's
    configured 100% - it deliberately does not follow the per-level table,
    which would make dragging feel like it jumps between steps.
    """
    ceiling = channel_max_db(channel)
    if ceiling <= FADER_MIN_DB:
        return 0.0
    fraction = (db - FADER_MIN_DB) / (ceiling - FADER_MIN_DB)
    return max(0.0, min(1.0, fraction))


def channel_position_for_fraction(channel: dict, fraction: float) -> int:
    """The controller position for a slider at a fraction of its range."""
    fraction = max(0.0, min(1.0, fraction))
    ceiling = channel_max_db(channel)
    if ceiling <= FADER_MIN_DB:
        return 0
    return db_to_position(FADER_MIN_DB + fraction * (ceiling - FADER_MIN_DB))


def signal_update(entry_id: str) -> str:
    """Dispatcher signal for entity state refreshes."""
    return f"{DOMAIN}_{entry_id}_update"
