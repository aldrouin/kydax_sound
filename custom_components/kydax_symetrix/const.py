"""Constants for the Kydax Symetrix integration."""

DOMAIN = "kydax_symetrix"

# Symetrix control protocol default TCP port
DEFAULT_PORT = 48631

# how often to poll the appliance for current values (seconds)
POLL_SECONDS = 30


def signal_update(entry_id: str) -> str:
    """Dispatcher signal for entity state refreshes."""
    return f"{DOMAIN}_{entry_id}_update"
