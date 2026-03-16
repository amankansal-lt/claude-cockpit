#!/usr/bin/env python3
"""Gatekeeper for auto-memory hooks.

Checks cockpit-settings.json for the auto_memory toggle.
Also throttles to once per 5 minutes to avoid expensive
API calls on every Claude response.

Exit 0 = enabled (allow subsequent hooks to run).
Exit 2 = disabled (block subsequent hooks).
"""

import json
import sys
import time
from pathlib import Path

SETTINGS_FILE = Path.home() / ".claude" / "cockpit-settings.json"
THROTTLE_FILE = Path.home() / ".claude" / "cockpit-hooks" / ".last_run"
THROTTLE_SECONDS = 300  # 5 minutes between runs


def is_enabled() -> bool:
    if not SETTINGS_FILE.exists():
        return True  # Default to enabled for plugin users
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        return bool(data.get("auto_memory", True))
    except (json.JSONDecodeError, OSError):
        return True


def is_throttled() -> bool:
    """Check if we ran recently. Prevents API calls on every response."""
    if not THROTTLE_FILE.exists():
        return False
    try:
        last_run = float(THROTTLE_FILE.read_text().strip())
        return (time.time() - last_run) < THROTTLE_SECONDS
    except (ValueError, OSError):
        return False


def update_throttle():
    """Record current time as last run."""
    try:
        THROTTLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        THROTTLE_FILE.write_text(str(time.time()))
    except OSError:
        pass


if __name__ == "__main__":
    if not is_enabled():
        sys.exit(2)

    if is_throttled():
        sys.exit(2)

    update_throttle()
    sys.exit(0)
