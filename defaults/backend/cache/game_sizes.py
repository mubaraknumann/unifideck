"""
Game size cache helpers.

Stores download sizes for instant button loading.
"""
import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Game Size Cache - stores download sizes for instant button loading
# Pre-populated during sync, read during get_game_info
GAME_SIZES_CACHE_FILE = "game_sizes.json"

# In-memory cache for game sizes (avoids disk I/O on every get_game_info call)
_game_sizes_mem_cache: Optional[Dict[str, Dict]] = None
_game_sizes_mem_cache_time: float = 0
GAME_SIZES_MEM_CACHE_TTL = 60.0  # 60 seconds (sizes change rarely)


def get_game_sizes_cache_path() -> Path:
    """Get path to game sizes cache file (in user data, not plugin dir)"""
    return Path.home() / ".local" / "share" / "unifideck" / GAME_SIZES_CACHE_FILE


def _invalidate_game_sizes_mem_cache():
    """Invalidate in-memory game sizes cache"""
    global _game_sizes_mem_cache, _game_sizes_mem_cache_time
    _game_sizes_mem_cache = None
    _game_sizes_mem_cache_time = 0


def load_game_sizes_cache() -> Dict[str, Dict]:
    """Load game sizes cache with in-memory caching. Returns {store:game_id: {size_bytes, updated}}"""
    global _game_sizes_mem_cache, _game_sizes_mem_cache_time

    # Check in-memory cache first
    now = time.time()
    if _game_sizes_mem_cache is not None and (now - _game_sizes_mem_cache_time) < GAME_SIZES_MEM_CACHE_TTL:
        return _game_sizes_mem_cache

    # Cache miss - read from disk
    cache_path = get_game_sizes_cache_path()
    result = {}
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                result = json.load(f)
    except Exception as e:
        logger.error(f"Error loading game sizes cache: {e}")

    # Update in-memory cache
    _game_sizes_mem_cache = result
    _game_sizes_mem_cache_time = now
    return result


def save_game_sizes_cache(cache: Dict[str, Dict]) -> bool:
    """Save game sizes cache to file and update in-memory cache"""
    global _game_sizes_mem_cache, _game_sizes_mem_cache_time

    cache_path = get_game_sizes_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)
        logger.debug(f"Saved {len(cache)} entries to game sizes cache")

        # Update in-memory cache immediately
        _game_sizes_mem_cache = cache
        _game_sizes_mem_cache_time = time.time()
        return True
    except Exception as e:
        logger.error(f"Error saving game sizes cache: {e}")
        _invalidate_game_sizes_mem_cache()  # Invalidate on error
        return False


def cache_game_size(store: str, game_id: str, size_bytes: int) -> bool:
    """Cache a game's download size"""
    cache = load_game_sizes_cache()
    cache_key = f"{store}:{game_id}"
    cache[cache_key] = {
        'size_bytes': size_bytes,
        'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    return save_game_sizes_cache(cache)


def get_cached_game_size(store: str, game_id: str) -> Optional[int]:
    """Get cached game size, or None if not cached"""
    cache = load_game_sizes_cache()  # Uses in-memory cache
    cache_key = f"{store}:{game_id}"
    entry = cache.get(cache_key)
    return entry.get('size_bytes') if entry else None
