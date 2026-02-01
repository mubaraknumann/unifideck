"""RAWG metadata cache.

Stores RAWG API results keyed by game title (lowercase).
This lives in user data (~/.local/share/unifideck) so it survives plugin reinstalls.

NOTE: This module intentionally preserves the existing cache file format.
"""

import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

RAWG_METADATA_CACHE_FILE = "rawg_metadata_cache.json"


def get_rawg_metadata_cache_path() -> Path:
    """Get path to RAWG metadata cache file."""
    return Path.home() / ".local" / "share" / "unifideck" / RAWG_METADATA_CACHE_FILE


def load_rawg_metadata_cache() -> Dict[str, Dict]:
    """Load RAWG metadata cache. Returns {lowercase_title: rawg_data_dict}."""
    cache_path = get_rawg_metadata_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading RAWG metadata cache: {e}")
    return {}


def save_rawg_metadata_cache(cache: Dict[str, Dict]) -> bool:
    """Save RAWG metadata cache."""
    cache_path = get_rawg_metadata_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
        logger.info(f"Saved {len(cache)} RAWG metadata entries to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving RAWG metadata cache: {e}")
        return False
