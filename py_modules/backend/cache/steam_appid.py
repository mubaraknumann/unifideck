"""Steam appid mapping cache.

Stores mapping of Unifideck shortcut appid -> Steam appid.
This lives in user data (~/.local/share/unifideck) so it survives plugin reinstalls.

NOTE: This module intentionally preserves the existing cache file format.
"""

import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

STEAM_APPID_CACHE_FILE = "steam_appid_cache.json"


def get_steam_appid_cache_path() -> Path:
    """Get path to steam_app_id cache file (in user data, not plugin dir)."""
    return Path.home() / ".local" / "share" / "unifideck" / STEAM_APPID_CACHE_FILE


def load_steam_appid_cache() -> Dict[int, int]:
    """Load steam_app_id mappings from cache file. Returns {shortcut_appid: steam_appid}."""
    cache_path = get_steam_appid_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, "r") as f:
                data = json.load(f)
                # Cache is stored with string keys; convert to int keys.
                return {int(k): int(v) for k, v in data.items()}
    except Exception as e:
        logger.error(f"Error loading steam_app_id cache: {e}")
    return {}


def save_steam_appid_cache(cache: Dict[int, int]) -> bool:
    """Save steam_app_id mappings to cache file."""
    cache_path = get_steam_appid_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve existing on-disk format: string keys.
        serializable = {str(k): int(v) for k, v in cache.items()}
        with open(cache_path, "w") as f:
            json.dump(serializable, f, indent=2)
        logger.info(f"Saved {len(cache)} steam_app_id mappings to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving steam_app_id cache: {e}")
        return False
