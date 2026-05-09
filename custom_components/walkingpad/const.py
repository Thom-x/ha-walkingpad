"""Constants for the WalkingPad integration."""
from __future__ import annotations

DOMAIN = "walkingpad"

DEFAULT_NAME = "WalkingPad"

POLL_INTERVAL_SECONDS = 2
RECONNECT_BACKOFF_SECONDS = 5.0
# If the pad doesn't send any status notification for this many seconds while
# we believe we're connected, we consider the link dead.
STATUS_TIMEOUT_SECONDS = 10.0

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "walkingpad_totals"
