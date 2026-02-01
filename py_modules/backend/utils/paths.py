"""Unifideck file path constants and utilities."""

import os
from pathlib import Path


# Unifideck data directory
UNIFIDECK_DATA_DIR = os.path.expanduser("~/.local/share/unifideck")

# Cache and data files
GAMES_MAP_PATH = os.path.join(UNIFIDECK_DATA_DIR, "games.map")
GAMES_REGISTRY_PATH = os.path.join(UNIFIDECK_DATA_DIR, "games_registry.json")
GAME_SIZES_CACHE_PATH = os.path.join(UNIFIDECK_DATA_DIR, "game_sizes.json")
SHORTCUTS_REGISTRY_PATH = os.path.join(UNIFIDECK_DATA_DIR, "shortcuts_registry.json")
DOWNLOAD_QUEUE_PATH = os.path.join(UNIFIDECK_DATA_DIR, "download_queue.json")
DOWNLOAD_SETTINGS_PATH = os.path.join(UNIFIDECK_DATA_DIR, "download_settings.json")
SETTINGS_PATH = os.path.join(UNIFIDECK_DATA_DIR, "settings.json")

# Store config directories
LEGENDARY_CONFIG_DIR = os.path.expanduser("~/.config/legendary")
LEGENDARY_USER_JSON = os.path.join(LEGENDARY_CONFIG_DIR, "user.json")

GOG_CONFIG_DIR = os.path.expanduser("~/.config/unifideck")
GOG_TOKEN_JSON = os.path.join(GOG_CONFIG_DIR, "gog_token.json")

NILE_CONFIG_DIR = os.path.expanduser("~/.config/nile")
NILE_USER_JSON = os.path.join(NILE_CONFIG_DIR, "user.json")

# Default game install locations
DEFAULT_GOG_GAMES_PATH = os.path.expanduser("~/GOG Games")


def get_prefix_path(game_id: str) -> str:
    """Get Wine prefix path for a game.
    
    Args:
        game_id: Store-specific game ID
        
    Returns:
        Full path to game's Wine prefix directory
    """
    return os.path.expanduser(f"~/.local/share/unifideck/prefixes/{game_id}")


def ensure_unifideck_dir() -> None:
    """Ensure the unifideck data directory exists."""
    os.makedirs(UNIFIDECK_DATA_DIR, exist_ok=True)


def is_safe_delete_path(path: str) -> bool:
    """Check if a path is safe to delete (not system critical).
    
    Args:
        path: Path to validate
        
    Returns:
        True if safe to delete
    """
    # Safety check: ensure we're deleting from expected locations
    # Only delete if path contains "Games", "Epic", "GOG", or "unifideck"
    # and is NOT root or home root
    safe_keywords = ['/Games/', '/Epic', '/GOG', 'unifideck']
    is_safe = any(k in path for k in safe_keywords)
    
    home_dir = os.path.expanduser("~")
    games_dir = os.path.join(home_dir, "Games")
    not_root = path not in ['/', home_dir, games_dir]
    
    return is_safe and not_root and os.path.exists(path)
