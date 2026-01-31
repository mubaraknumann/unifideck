"""Compatibility cache helpers.

Moved from main.py to shrink the entrypoint.
Behavior should remain identical.

Cache file: ~/.local/share/unifideck/compat_cache.json
"""

import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# Compatibility Cache - stores ProtonDB tier and Steam Deck status for games
# Pre-populated during sync for fast "Great on Deck" filtering
COMPAT_CACHE_FILE = "compat_cache.json"

# ProtonDB tier types
PROTONDB_TIERS = ['platinum', 'gold', 'silver', 'bronze', 'borked', 'pending', 'native']

# Steam Deck compatibility categories from Steam API
DECK_CATEGORIES = {
    1: 'unknown',
    2: 'unsupported',
    3: 'playable',
    4: 'verified'
}

# User-Agent to avoid being blocked by APIs
COMPAT_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


def get_compat_cache_path() -> Path:
    """Get path to compatibility cache file"""
    return Path.home() / ".local" / "share" / "unifideck" / COMPAT_CACHE_FILE


def load_compat_cache() -> Dict[str, Dict]:
    """Load compatibility cache. Returns {normalized_title: {tier, deckVerified, steamAppId, timestamp}}"""
    cache_path = get_compat_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading compat cache: {e}")
    return {}


def save_compat_cache(cache: Dict[str, Dict]) -> bool:
    """Save compatibility cache to file"""
    cache_path = get_compat_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)
        logger.debug(f"Saved {len(cache)} entries to compat cache")
        return True
    except Exception as e:
        logger.error(f"Error saving compat cache: {e}")
        return False
