"""Steam metadata cache.

Stores Steam API game details for store patching.
This lives in user data (~/.local/share/unifideck) so it survives plugin reinstalls.

NOTE: This module intentionally preserves the existing cache file format.
"""

import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

STEAM_METADATA_CACHE_FILE = "steam_metadata_cache.json"


def get_steam_metadata_cache_path() -> Path:
    """Get path to Steam metadata cache file."""
    return Path.home() / ".local" / "share" / "unifideck" / STEAM_METADATA_CACHE_FILE


def load_steam_metadata_cache() -> Dict[int, Dict]:
    """Load Steam metadata cache. Returns {steam_appid: metadata_dict}."""
    cache_path = get_steam_metadata_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, "r") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.error(f"Error loading steam metadata cache: {e}")
    return {}


def save_steam_metadata_cache(cache: Dict[int, Dict]) -> bool:
    """Save Steam metadata cache."""
    cache_path = get_steam_metadata_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
        logger.info(f"Saved {len(cache)} Steam metadata entries to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving steam metadata cache: {e}")
        return False
