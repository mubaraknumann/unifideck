"""
Centralized path utilities for game installation directories.

This module provides consistent path handling across all store connectors,
ensuring SD cards and external drives are properly detected.
"""
import os
import logging
from typing import List

logger = logging.getLogger(__name__)

# Default install directories per store
DEFAULT_PATHS = {
    'epic': os.path.expanduser("~/Games/Epic"),
    'gog': os.path.expanduser("~/GOG Games"),
    'amazon': os.path.expanduser("~/Games/Amazon"),
}

# games.map location (user data, survives plugin reinstall)
GAMES_MAP_PATH = os.path.expanduser("~/.local/share/unifideck/games.map")


def get_all_game_directories() -> List[str]:
    """
    Get all possible game installation directories.
    
    Scans:
    - Default install paths (~/Games/*, ~/GOG Games)
    - All mounted SD cards/USB drives (/run/media/*/*)
    
    Returns:
        List of existing directory paths
    """
    paths = []
    
    # Add default paths
    for path in DEFAULT_PATHS.values():
        if os.path.isdir(path):
            paths.append(path)
    
    # Also add ~/Games directly (for subdirs we might not know about)
    games_home = os.path.expanduser("~/Games")
    if os.path.isdir(games_home):
        paths.append(games_home)
    
    # Add ~/GOG Games if not already added
    gog_home = os.path.expanduser("~/GOG Games")
    if os.path.isdir(gog_home) and gog_home not in paths:
        paths.append(gog_home)
    
    # Scan mounted media (SD cards, USB drives)
    media_base = "/run/media"
    if os.path.exists(media_base):
        try:
            for user_or_mount in os.listdir(media_base):
                user_path = os.path.join(media_base, user_or_mount)
                if os.path.isdir(user_path):
                    # Check if this level has Games directly (/run/media/Games - unlikely but possible)
                    games_direct = os.path.join(user_path, "Games")
                    if os.path.isdir(games_direct):
                        paths.append(games_direct)
                    
                    # Check for GOG Games at this level
                    gog_direct = os.path.join(user_path, "GOG Games")
                    if os.path.isdir(gog_direct):
                        paths.append(gog_direct)
                    
                    # Scan subdirectories (for /run/media/deck/SDCARD/Games)
                    try:
                        for mount in os.listdir(user_path):
                            mount_path = os.path.join(user_path, mount)
                            if os.path.isdir(mount_path):
                                # Check for Games folder
                                games_path = os.path.join(mount_path, "Games")
                                if os.path.isdir(games_path):
                                    paths.append(games_path)
                                
                                # Check for GOG Games folder
                                gog_path = os.path.join(mount_path, "GOG Games")
                                if os.path.isdir(gog_path):
                                    paths.append(gog_path)
                    except PermissionError:
                        pass  # Skip directories we can't access
        except Exception as e:
            logger.warning(f"[Paths] Error scanning media paths: {e}")
    
    # Deduplicate while preserving order
    seen = set()
    unique_paths = []
    for p in paths:
        try:
            real_path = os.path.realpath(p)
            if real_path not in seen:
                seen.add(real_path)
                unique_paths.append(p)
        except Exception:
            # If realpath fails, just use the original path
            if p not in seen:
                seen.add(p)
                unique_paths.append(p)
    
    logger.debug(f"[Paths] Found {len(unique_paths)} game directories: {unique_paths}")
    return unique_paths


def get_games_map_path() -> str:
    """Get the canonical path to games.map"""
    return GAMES_MAP_PATH


def ensure_games_map_dir() -> None:
    """Ensure the games.map directory exists"""
    os.makedirs(os.path.dirname(GAMES_MAP_PATH), exist_ok=True)
