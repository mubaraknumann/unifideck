"""
Steam User Detection Utilities

Provides reliable detection of the currently logged-in Steam user by parsing
Steam's loginusers.vdf file instead of relying on directory modification times.
"""

import os
import logging
from typing import Optional

try:
    import vdf
    VDF_AVAILABLE = True
except ImportError:
    VDF_AVAILABLE = False

logger = logging.getLogger(__name__)


def get_logged_in_steam_user(steam_path: Optional[str] = None) -> Optional[str]:
    """
    Get the currently logged-in Steam user's account ID (userdata folder name).
    
    Uses loginusers.vdf with MostRecent flag as primary source,
    falls back to mtime-based detection (excluding user 0).
    
    Args:
        steam_path: Path to Steam installation (auto-detected if None)
        
    Returns:
        Account ID string (the folder name in userdata/) or None
    """
    if steam_path is None:
        steam_path = _find_steam_path()
    
    if not steam_path:
        logger.warning("[SteamUser] Could not find Steam installation path")
        return None
    
    # Try loginusers.vdf first (most reliable)
    user_id = _get_user_from_loginusers(steam_path)
    if user_id:
        logger.info(f"[SteamUser] Found logged-in user from loginusers.vdf: {user_id}")
        return user_id
    
    # Fallback to mtime-based detection (excluding user 0)
    user_id = _get_user_from_mtime(steam_path)
    if user_id:
        logger.info(f"[SteamUser] Fallback: Using mtime-based user detection: {user_id}")
        return user_id
    
    logger.error("[SteamUser] Could not detect logged-in Steam user")
    return None


def _find_steam_path() -> Optional[str]:
    """Find Steam installation directory"""
    possible_paths = [
        os.path.expanduser("~/.steam/steam"),
        os.path.expanduser("~/.local/share/Steam"),
    ]

    for path in possible_paths:
        if os.path.exists(os.path.join(path, "steamapps")):
            return path

    return None


def _get_user_from_loginusers(steam_path: str) -> Optional[str]:
    """
    Get the logged-in user from loginusers.vdf
    
    The file contains Steam64IDs, which we convert to account IDs (userdata folder names).
    """
    if not VDF_AVAILABLE:
        logger.debug("[SteamUser] vdf module not available, skipping loginusers.vdf")
        return None
    
    loginusers_path = os.path.join(steam_path, "config", "loginusers.vdf")
    
    if not os.path.exists(loginusers_path):
        logger.debug(f"[SteamUser] loginusers.vdf not found at {loginusers_path}")
        return None
    
    try:
        with open(loginusers_path, 'r', encoding='utf-8', errors='ignore') as f:
            data = vdf.load(f)
        
        users = data.get('users', {})
        
        # Find the user with MostRecent = "1"
        for steam64_id_str, user_info in users.items():
            if user_info.get('MostRecent') == '1':
                # Convert Steam64ID to account ID (lower 32 bits)
                try:
                    steam64_id = int(steam64_id_str)
                    account_id = steam64_id & 0xFFFFFFFF
                    
                    # Validate that this userdata folder actually exists
                    userdata_path = os.path.join(steam_path, "userdata", str(account_id))
                    if os.path.exists(userdata_path):
                        return str(account_id)
                    else:
                        logger.warning(f"[SteamUser] MostRecent user {account_id} folder doesn't exist")
                except ValueError:
                    logger.warning(f"[SteamUser] Invalid Steam64ID: {steam64_id_str}")
                    continue
        
        logger.debug("[SteamUser] No MostRecent user found in loginusers.vdf")
        
    except Exception as e:
        logger.warning(f"[SteamUser] Error reading loginusers.vdf: {e}")
    
    return None


def _get_user_from_mtime(steam_path: str) -> Optional[str]:
    """
    Fallback: Get the most recently active user by directory mtime.
    
    EXPLICITLY EXCLUDES user 0 which is a meta-directory.
    """
    userdata_path = os.path.join(steam_path, "userdata")
    
    if not os.path.exists(userdata_path):
        return None
    
    user_dirs = []
    for d in os.listdir(userdata_path):
        # Skip non-numeric directories
        if not d.isdigit():
            continue
        
        # CRITICAL: Skip user 0 - it's a meta-directory, not a real user
        if d == '0':
            logger.debug("[SteamUser] Skipping user 0 (meta-directory)")
            continue
        
        dir_path = os.path.join(userdata_path, d)
        if os.path.isdir(dir_path):
            mtime = os.path.getmtime(dir_path)
            user_dirs.append((d, mtime))
    
    if not user_dirs:
        return None
    
    # Sort by mtime descending, return most recent
    user_dirs.sort(key=lambda x: x[1], reverse=True)
    return user_dirs[0][0]


def validate_user_id(steam_path: str, user_id: str) -> bool:
    """
    Validate that a user ID has a valid userdata directory with shortcuts config.
    
    Args:
        steam_path: Path to Steam installation
        user_id: The account ID to validate
        
    Returns:
        True if the user has a valid config directory
    """
    if user_id == '0':
        return False
    
    config_path = os.path.join(steam_path, "userdata", user_id, "config")
    return os.path.exists(config_path)


def migrate_user0_to_logged_in_user(steam_path: Optional[str] = None) -> dict:
    """
    Migrate shortcuts and artwork from user 0 to the logged-in user.
    
    This function:
    1. Detects the logged-in user
    2. Checks if user 0 has any shortcuts.vdf or grid artwork
    3. Merges user 0's shortcuts into the logged-in user's shortcuts.vdf
    4. Copies user 0's grid artwork to the logged-in user's grid folder
    5. Optionally cleans up user 0's data after successful migration
    
    Returns:
        dict with migration results: {
            'success': bool,
            'shortcuts_migrated': int,
            'artwork_migrated': int,
            'errors': list
        }
    """
    import shutil
    
    result = {
        'success': False,
        'shortcuts_migrated': 0,
        'artwork_migrated': 0,
        'errors': []
    }
    
    if steam_path is None:
        steam_path = _find_steam_path()
    
    if not steam_path:
        result['errors'].append("Could not find Steam path")
        return result
    
    # Get logged-in user
    logged_in_user = get_logged_in_steam_user(steam_path)
    if not logged_in_user:
        result['errors'].append("Could not determine logged-in user")
        return result
    
    if logged_in_user == '0':
        result['errors'].append("Logged-in user detected as 0 - cannot migrate to self")
        return result
    
    userdata_path = os.path.join(steam_path, "userdata")
    user0_path = os.path.join(userdata_path, "0")
    target_user_path = os.path.join(userdata_path, logged_in_user)
    
    # Check if user 0 folder exists
    if not os.path.exists(user0_path):
        logger.info("[Migration] User 0 folder does not exist, nothing to migrate")
        result['success'] = True
        return result
    
    logger.info(f"[Migration] Starting migration from user 0 to user {logged_in_user}")
    
    # === MIGRATE SHORTCUTS.VDF ===
    user0_shortcuts = os.path.join(user0_path, "config", "shortcuts.vdf")
    target_shortcuts = os.path.join(target_user_path, "config", "shortcuts.vdf")
    
    if os.path.exists(user0_shortcuts):
        try:
            result['shortcuts_migrated'] = _migrate_shortcuts(user0_shortcuts, target_shortcuts)
            logger.info(f"[Migration] Migrated {result['shortcuts_migrated']} shortcuts")
        except Exception as e:
            result['errors'].append(f"Shortcuts migration error: {e}")
            logger.error(f"[Migration] Shortcuts error: {e}")
    
    # === MIGRATE GRID ARTWORK ===
    user0_grid = os.path.join(user0_path, "config", "grid")
    target_grid = os.path.join(target_user_path, "config", "grid")
    
    if os.path.exists(user0_grid):
        try:
            result['artwork_migrated'] = _migrate_grid_artwork(user0_grid, target_grid)
            logger.info(f"[Migration] Migrated {result['artwork_migrated']} artwork files")
        except Exception as e:
            result['errors'].append(f"Artwork migration error: {e}")
            logger.error(f"[Migration] Artwork error: {e}")
    
    result['success'] = len(result['errors']) == 0
    
    if result['success'] and (result['shortcuts_migrated'] > 0 or result['artwork_migrated'] > 0):
        logger.info(f"[Migration] Complete: {result['shortcuts_migrated']} shortcuts, {result['artwork_migrated']} artwork files")
    
    return result


def _migrate_shortcuts(source_path: str, target_path: str) -> int:
    """
    Merge shortcuts from source into target shortcuts.vdf
    
    Returns number of shortcuts migrated.
    """
    if not VDF_AVAILABLE:
        logger.warning("[Migration] vdf module not available, cannot migrate shortcuts")
        return 0
    
    # Load source shortcuts (user 0)
    try:
        with open(source_path, 'rb') as f:
            import vdf
            source_data = vdf.binary_loads(f.read())
    except Exception as e:
        logger.error(f"[Migration] Could not read source shortcuts: {e}")
        return 0
    
    source_shortcuts = source_data.get('shortcuts', {})
    if not source_shortcuts:
        logger.debug("[Migration] No shortcuts in user 0's shortcuts.vdf")
        return 0
    
    # Load target shortcuts (logged-in user)
    target_data = {'shortcuts': {}}
    if os.path.exists(target_path):
        try:
            with open(target_path, 'rb') as f:
                import vdf
                target_data = vdf.binary_loads(f.read())
        except Exception as e:
            logger.warning(f"[Migration] Could not read target shortcuts, will create new: {e}")
    
    target_shortcuts = target_data.get('shortcuts', {})
    
    # Build set of existing launch options to avoid duplicates
    existing_launch_opts = {
        s.get('LaunchOptions') for s in target_shortcuts.values() if s.get('LaunchOptions')
    }
    
    # Find next available index in target
    existing_indices = [int(k) for k in target_shortcuts.keys() if str(k).isdigit()]
    next_index = max(existing_indices, default=-1) + 1
    
    # Migrate shortcuts that don't already exist
    migrated = 0
    for shortcut in source_shortcuts.values():
        launch_opts = shortcut.get('LaunchOptions', '')
        
        # Skip if already exists in target
        if launch_opts in existing_launch_opts:
            logger.debug(f"[Migration] Skipping duplicate: {shortcut.get('AppName')}")
            continue
        
        # Add to target
        target_shortcuts[str(next_index)] = shortcut
        existing_launch_opts.add(launch_opts)
        next_index += 1
        migrated += 1
        logger.info(f"[Migration] Migrated shortcut: {shortcut.get('AppName')}")
    
    # Save merged shortcuts
    if migrated > 0:
        target_data['shortcuts'] = target_shortcuts
        
        # Ensure target directory exists
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        
        try:
            import vdf
            with open(target_path, 'wb') as f:
                f.write(vdf.binary_dumps(target_data))
            logger.info(f"[Migration] Saved {len(target_shortcuts)} shortcuts to {target_path}")
        except Exception as e:
            logger.error(f"[Migration] Failed to save target shortcuts: {e}")
            raise
    
    return migrated


def _migrate_grid_artwork(source_dir: str, target_dir: str) -> int:
    """
    Copy grid artwork files from source to target directory.
    
    Does not overwrite existing files.
    Returns number of files copied.
    """
    import shutil
    
    if not os.path.isdir(source_dir):
        return 0
    
    os.makedirs(target_dir, exist_ok=True)
    
    copied = 0
    for filename in os.listdir(source_dir):
        source_file = os.path.join(source_dir, filename)
        target_file = os.path.join(target_dir, filename)
        
        # Skip directories
        if not os.path.isfile(source_file):
            continue
        
        # Skip if target already exists (don't overwrite)
        if os.path.exists(target_file):
            logger.debug(f"[Migration] Skipping existing artwork: {filename}")
            continue
        
        try:
            shutil.copy2(source_file, target_file)
            copied += 1
            logger.debug(f"[Migration] Copied artwork: {filename}")
        except Exception as e:
            logger.warning(f"[Migration] Failed to copy {filename}: {e}")
    
    return copied

