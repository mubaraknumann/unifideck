"""Shortcuts registry cache.

Stores mapping of game launch options -> appid for reconciliation after plugin reinstall.
This lives in user data (~/.local/share/unifideck) so it survives plugin reinstalls.

The registry tracks which non-Steam shortcuts were created by Unifideck, allowing
proper reconciliation when the plugin is reinstalled or Steam restarts.
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

SHORTCUTS_REGISTRY_FILE = "shortcuts_registry.json"


def get_shortcuts_registry_path() -> Path:
    """Get path to shortcuts registry file (in user data, not plugin dir)."""
    return Path.home() / ".local" / "share" / "unifideck" / SHORTCUTS_REGISTRY_FILE


def load_shortcuts_registry() -> Dict[str, Dict]:
    """Load shortcuts registry. Returns {launch_options: {appid, appid_unsigned, title, created}}."""
    registry_path = get_shortcuts_registry_path()
    try:
        if registry_path.exists():
            with open(registry_path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading shortcuts registry: {e}")
    return {}


def save_shortcuts_registry(registry: Dict[str, Dict]) -> bool:
    """Save shortcuts registry to file."""
    registry_path = get_shortcuts_registry_path()
    try:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        with open(registry_path, "w") as f:
            json.dump(registry, f, indent=2)
        logger.info(f"Saved {len(registry)} entries to shortcuts registry")
        return True
    except Exception as e:
        logger.error(f"Error saving shortcuts registry: {e}")
        return False


def register_shortcut(launch_options: str, appid: int, title: str) -> bool:
    """Register a shortcut's appid for future reconciliation."""
    registry = load_shortcuts_registry()

    # Calculate unsigned appid for logging/debugging
    appid_unsigned = appid if appid >= 0 else appid + 2**32

    registry[launch_options] = {
        "appid": appid,
        "appid_unsigned": appid_unsigned,
        "title": title,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    logger.debug(
        f"Registered shortcut: {launch_options} -> appid={appid} (unsigned={appid_unsigned})"
    )
    return save_shortcuts_registry(registry)


def get_registered_appid(launch_options: str) -> Optional[int]:
    """Get the registered appid for a game, or None if not registered."""
    registry = load_shortcuts_registry()
    entry = registry.get(launch_options)
    return entry.get("appid") if entry else None
