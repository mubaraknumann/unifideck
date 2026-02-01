"""Steam shortcuts manager for non-Steam games.

Manages shortcuts.vdf file, games.map registry, and Steam appinfo injection.
"""

import os
import time
import logging
from typing import Dict, Any, Optional, List, Tuple

from backend.stores.base import Game
from backend.utils.steam_user import get_logged_in_steam_user
from backend.cache.shortcuts_registry import (
    load_shortcuts_registry,
    register_shortcut,
)
from backend.utils.vdf import load_shortcuts_vdf, save_shortcuts_vdf

logger = logging.getLogger(__name__)

# In-memory cache for games.map (avoids disk I/O on every get_game_info call)
_games_map_mem_cache: Optional[Dict[str, str]] = None  # key -> full line
_games_map_mem_cache_time: float = 0
GAMES_MAP_MEM_CACHE_TTL = 5.0  # 5 seconds

GAMES_MAP_PATH = os.path.expanduser("~/.local/share/unifideck/games.map")


def _invalidate_games_map_mem_cache():
    """Invalidate in-memory games.map cache"""
    global _games_map_mem_cache, _games_map_mem_cache_time
    _games_map_mem_cache = None
    _games_map_mem_cache_time = 0
    logger.debug("[GameMap] In-memory cache invalidated")


def _load_games_map_cached() -> Dict[str, str]:
    """Load games.map with in-memory caching. Returns {store:game_id: full_line}"""
    global _games_map_mem_cache, _games_map_mem_cache_time
    
    # Check in-memory cache first
    now = time.time()
    if _games_map_mem_cache is not None and (now - _games_map_mem_cache_time) < GAMES_MAP_MEM_CACHE_TTL:
        return _games_map_mem_cache
    
    # Cache miss - read from disk
    result = {}
    if os.path.exists(GAMES_MAP_PATH):
        try:
            with open(GAMES_MAP_PATH, 'r') as f:
                for line in f:
                    if '|' in line:
                        key = line.split('|')[0]
                        result[key] = line.strip()
        except Exception as e:
            logger.error(f"[GameMap] Error reading games.map: {e}")
    
    # Update in-memory cache
    _games_map_mem_cache = result
    _games_map_mem_cache_time = now
    return result


class ShortcutsManager:
    """Manages Steam's shortcuts.vdf file for non-Steam games"""
    
    # Shortcuts VDF in-memory cache TTL
    SHORTCUTS_CACHE_TTL = 5.0  # 5 seconds

    def __init__(self, steam_path: Optional[str] = None):
        self.steam_path = steam_path or self._find_steam_path()
        self.shortcuts_path = self._find_shortcuts_vdf()
        logger.info(f"Shortcuts path: {self.shortcuts_path}")
        
        # In-memory cache for shortcuts.vdf
        self._shortcuts_cache: Optional[Dict[str, Any]] = None
        self._shortcuts_cache_time: float = 0

    def _find_steam_path(self) -> Optional[str]:
        """Find Steam installation directory"""
        possible_paths = [
            os.path.expanduser("~/.steam/steam"),
            os.path.expanduser("~/.local/share/Steam"),
        ]

        for path in possible_paths:
            if os.path.exists(os.path.join(path, "steamapps")):
                return path

        return None

    def _find_shortcuts_vdf(self) -> Optional[str]:
        """Find shortcuts.vdf file for the logged-in Steam user.
        
        Uses loginusers.vdf to find the user with MostRecent=1, falling
        back to mtime-based detection while explicitly excluding user 0.
        """
        if not self.steam_path:
            return None

        userdata_path = os.path.join(self.steam_path, "userdata")
        if not os.path.exists(userdata_path):
            return None

        # Use the new robust user detection utility
        active_user = get_logged_in_steam_user(self.steam_path)
        
        if not active_user:
            logger.error("[ShortcutsManager] Could not determine logged-in Steam user")
            return None
        
        # Safety check: never use user 0
        if active_user == '0':
            logger.error("[ShortcutsManager] User 0 detected - this is a meta-directory, not a real user!")
            return None

        shortcuts_path = os.path.join(userdata_path, active_user, "config", "shortcuts.vdf")
        logger.info(f"[ShortcutsManager] Using shortcuts.vdf for user {active_user}: {shortcuts_path}")

        return shortcuts_path

    async def _update_game_map(self, store: str, game_id: str, exe_path: str, work_dir: str):
        """Update the dynamic games map file atomically
        
        FIX 4: Uses tempfile + atomic rename to prevent data corruption
        if power is lost or multiple processes write simultaneously.
        """
        import tempfile
        
        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
        dir_name = os.path.dirname(map_file)
        os.makedirs(dir_name, exist_ok=True)
        
        key = f"{store}:{game_id}"
        new_entry = f"{key}|{exe_path}|{work_dir}\n"
        
        logger.info(f"[GameMap] Updating {key}: exe_path='{exe_path}', work_dir='{work_dir}'")
        
        lines = []
        if os.path.exists(map_file):
            with open(map_file, 'r') as f:
                lines = f.readlines()
        
        # Remove existing entry for this key
        lines = [l for l in lines if not l.startswith(f"{key}|")]
        lines.append(new_entry)
        
        # Atomic write: write to temp file, sync to disk, then rename
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=dir_name, delete=False, 
                                              prefix='.games.map.', suffix='.tmp') as tmp:
                tmp.writelines(lines)
                tmp.flush()
                os.fsync(tmp.fileno())  # Ensure data is on disk
                tmp_path = tmp.name
            
            os.rename(tmp_path, map_file)  # Atomic on POSIX
            logger.info(f"[GameMap] Atomically updated {key}")
        except Exception as e:
            logger.error(f"[GameMap] Atomic write failed, falling back: {e}")
            # Fallback to direct write if atomic fails
            with open(map_file, 'w') as f:
                f.writelines(lines)
        
        # Invalidate in-memory cache
        _invalidate_games_map_mem_cache()
            
    async def _remove_from_game_map(self, store: str, game_id: str):
        """Remove entry from games map file"""
        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
        if not os.path.exists(map_file):
            return
            
        key = f"{store}:{game_id}"
        
        with open(map_file, 'r') as f:
            lines = f.readlines()
            
        new_lines = [l for l in lines if not l.startswith(f"{key}|")]
        
        if len(new_lines) != len(lines):
            with open(map_file, 'w') as f:
                f.writelines(new_lines)
            # Invalidate in-memory cache
            _invalidate_games_map_mem_cache()

    def _is_in_game_map(self, store: str, game_id: str) -> bool:
        """Check if game is registered in games.map AND the executable/directory exists.
        
        Uses in-memory cache for fast lookups.
        
        Args:
            store: Store name ('epic' or 'gog')
            game_id: Game ID
            
        Returns:
            True if game is in games.map AND files exist on disk
        """
        key = f"{store}:{game_id}"
        games_map = _load_games_map_cached()
        
        if key not in games_map:
            return False
        
        # Parse the cached entry to verify files exist
        line = games_map[key]
        parts = line.split('|')
        if len(parts) >= 3:
            exe_path = parts[1]
            work_dir = parts[2]
            path_to_check = exe_path if exe_path else work_dir
            if path_to_check and os.path.exists(path_to_check):
                return True
            else:
                # Stale entry detected - auto-cleanup
                logger.info(f"[GameMap] Entry {key} exists but path missing: {path_to_check} - removing stale entry")
                self._remove_from_game_map_sync(store, game_id)
                return False
        return True  # Malformed entry, assume installed

    def _remove_from_game_map_sync(self, store: str, game_id: str):
        """Synchronous version of _remove_from_game_map for use in sync contexts.
        
        Removes entry from games.map file immediately (no async overhead).
        """
        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
        if not os.path.exists(map_file):
            return
            
        key = f"{store}:{game_id}"
        
        try:
            with open(map_file, 'r') as f:
                lines = f.readlines()
                
            new_lines = [l for l in lines if not l.startswith(f"{key}|")]
            
            if len(new_lines) != len(lines):
                with open(map_file, 'w') as f:
                    f.writelines(new_lines)
                logger.info(f"[GameMap] Removed stale entry: {key}")
                # Invalidate in-memory cache
                _invalidate_games_map_mem_cache()
        except Exception as e:
            logger.error(f"[GameMap] Error removing stale entry {key}: {e}")

    def _has_game_map_entry(self, store: str, game_id: str) -> bool:
        """Check if game has ANY entry in games.map (regardless of path validity).
        
        Uses in-memory cache for fast lookups.
        
        Args:
            store: Store name ('epic' or 'gog')
            game_id: Game ID
            
        Returns:
            True if any entry exists in games.map for this game
        """
        key = f"{store}:{game_id}"
        games_map = _load_games_map_cached()
        return key in games_map

    def _get_install_dir_from_game_map(self, store: str, game_id: str) -> Optional[str]:
        """Get install directory from games.map.
        
        Uses in-memory cache for fast lookups.
        Returns the parent directory of the exe_path or work_dir.
        """
        key = f"{store}:{game_id}"
        games_map = _load_games_map_cached()
        
        if key not in games_map:
            return None
        
        try:
            line = games_map[key]
            parts = line.split('|')
            if len(parts) >= 2:
                exe_path = parts[1] if len(parts) > 1 else None
                work_dir = parts[2] if len(parts) > 2 else None
                
                # Find install dir (parent of executable's parent OR work_dir's parent)
                if work_dir and os.path.exists(work_dir):
                    # work_dir is usually game_root/subdir, so go up to get game root
                    # But for some games, work_dir IS the game root
                    # Return the top-level directory containing .unifideck-id or goggame files
                    path = work_dir
                    while path and path != '/':
                        if (os.path.exists(os.path.join(path, '.unifideck-id')) or 
                            any(f.startswith('goggame-') for f in os.listdir(path) if os.path.isfile(os.path.join(path, f)))):
                            return path
                        path = os.path.dirname(path)
                    # Fallback: return work_dir's parent
                    return os.path.dirname(work_dir)
                elif exe_path and os.path.exists(exe_path):
                    # Go up from exe to find game root
                    path = os.path.dirname(exe_path)
                    while path and path != '/':
                        if (os.path.exists(os.path.join(path, '.unifideck-id')) or 
                            any(f.startswith('goggame-') for f in os.listdir(path) if os.path.isfile(os.path.join(path, f)))):
                            return path
                        path = os.path.dirname(path)
                    # Fallback: return exe's grandparent
                    return os.path.dirname(os.path.dirname(exe_path))
        except Exception as e:
            logger.error(f"[GameMap] Error getting install dir for {key}: {e}")
        return None

    def reconcile_games_map(self) -> Dict[str, Any]:
        """
        Reconcile games.map by removing entries pointing to non-existent files.
        
        Called on plugin startup to handle games deleted externally (e.g., via file manager).
        Entries are removed if neither the executable nor work directory exists.
        
        Returns:
            dict: {'removed': int, 'kept': int, 'entries_removed': list}
        """
        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
        
        if not os.path.exists(map_file):
            logger.debug("[Reconcile] games.map not found, nothing to reconcile")
            return {'removed': 0, 'kept': 0, 'entries_removed': []}
        
        removed = 0
        kept = 0
        entries_removed = []
        valid_lines = []
        
        try:
            with open(map_file, 'r') as f:
                lines = f.readlines()
            
            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                    
                parts = line_stripped.split('|')
                if len(parts) < 3:
                    logger.warning(f"[Reconcile] Skipping malformed line: {line_stripped}")
                    continue
                
                key = parts[0]  # store:game_id
                exe_path = parts[1]
                work_dir = parts[2]
                
                # Check if executable exists (primary check)
                # If exe_path is empty, check work_dir instead
                path_to_check = exe_path if exe_path else work_dir
                
                if path_to_check and os.path.exists(path_to_check):
                    valid_lines.append(line)
                    kept += 1
                else:
                    removed += 1
                    entries_removed.append(key)
                    logger.info(f"[Reconcile] Removing orphaned entry: {key} (path missing: {path_to_check})")
            
            # Rewrite games.map with only valid entries
            if removed > 0:
                with open(map_file, 'w') as f:
                    f.writelines(valid_lines)
                logger.info(f"[Reconcile] Cleaned games.map: {kept} kept, {removed} removed")
                # Invalidate in-memory cache
                _invalidate_games_map_mem_cache()
            else:
                logger.debug(f"[Reconcile] No orphaned entries found: {kept} entries all valid")
        
        except Exception as e:
            logger.error(f"[Reconcile] Error reconciling games.map: {e}")
            return {'removed': 0, 'kept': kept, 'entries_removed': [], 'error': str(e)}
        
        return {'removed': removed, 'kept': kept, 'entries_removed': entries_removed}

    async def reconcile_games_map_from_installed(self, epic_client=None, gog_client=None, amazon_client=None) -> Dict[str, Any]:
        """
        Repair games.map for Unifideck shortcuts that are missing entries.
        
        This ONLY processes shortcuts that Unifideck created (LaunchOptions = store:game_id).
        It does NOT touch Heroic, Lutris, or other tool shortcuts.
        
        For each Unifideck shortcut missing from games.map:
        1. Check if game is installed via store API
        2. If yes, get install path and add to games.map
        
        Called during Force Sync to repair existing installations.
        
        Args:
            epic_client: EpicConnector instance for getting Epic install info
            gog_client: GOGAPIClient instance for getting GOG install info
            amazon_client: AmazonGamesClient instance for getting Amazon install info
            
        Returns:
            dict: {'added': int, 'already_mapped': int, 'skipped': int, 'errors': list}
        """
        added = 0
        already_mapped = 0
        skipped = 0
        errors = []
        
        logger.info("[ReconcileMap] Starting games.map reconciliation for Unifideck shortcuts")
        
        try:
            # Load current games.map entries
            map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
            existing_entries = set()
            
            if os.path.exists(map_file):
                with open(map_file, 'r') as f:
                    for line in f:
                        parts = line.strip().split('|')
                        if parts:
                            existing_entries.add(parts[0])  # store:game_id
            
            # Load shortcuts and find Unifideck shortcuts missing from games.map
            shortcuts_data = await self.read_shortcuts()
            shortcuts = shortcuts_data.get('shortcuts', {})
            
            # Pre-fetch installed games from stores (for efficiency)
            epic_installed = {}
            gog_installed = {}
            amazon_installed = {}
            
            if epic_client and epic_client.legendary_bin:
                try:
                    epic_installed = await epic_client.get_installed()
                except Exception as e:
                    errors.append(f"Epic fetch: {e}")
            
            if gog_client:
                try:
                    gog_list = await gog_client.get_installed()
                    # GOG returns list of IDs, convert to dict with info
                    for gid in gog_list:
                        info = gog_client.get_installed_game_info(gid)
                        if info:
                            gog_installed[gid] = info
                except Exception as e:
                    errors.append(f"GOG fetch: {e}")
            
            if amazon_client:
                try:
                    amazon_installed = await amazon_client.get_installed()
                except Exception as e:
                    errors.append(f"Amazon fetch: {e}")
            
            # Iterate over shortcuts and find Unifideck ones missing from games.map
            for idx, shortcut in shortcuts.items():
                launch_options = shortcut.get('LaunchOptions', '')
                
                # Check if this is a Unifideck shortcut (store:game_id format)
                # Skip if it's a Heroic/Lutris/other shortcut
                if ':' not in launch_options:
                    continue
                
                parts = launch_options.split(':', 1)
                store = parts[0]
                game_id = parts[1] if len(parts) > 1 else ''
                
                # Only process known stores
                if store not in ('epic', 'gog', 'amazon'):
                    continue
                
                key = f"{store}:{game_id}"
                
                # Check if already in games.map
                if key in existing_entries:
                    already_mapped += 1
                    continue
                
                # Not in games.map - check if installed and get path
                game_title = shortcut.get('AppName', game_id)
                
                try:
                    if store == 'epic' and game_id in epic_installed:
                        game_data = epic_installed[game_id]
                        install_info = game_data.get('install', {})
                        install_path = install_info.get('install_path', '')
                        executable = game_data.get('manifest', {}).get('launch_exe', '')
                        
                        if install_path and os.path.exists(install_path):
                            exe_path = os.path.join(install_path, executable) if executable else ''
                            await self._update_game_map('epic', game_id, exe_path, install_path)
                            added += 1
                            logger.info(f"[ReconcileMap] Added Epic '{game_title}' to games.map")
                        else:
                            skipped += 1
                            logger.debug(f"[ReconcileMap] Epic '{game_title}' not installed or path missing")
                    
                    elif store == 'gog' and game_id in gog_installed:
                        game_info = gog_installed[game_id]
                        install_path = game_info.get('install_path', '')
                        exe_path = game_info.get('executable', '')
                        
                        if install_path and os.path.exists(install_path):
                            await self._update_game_map('gog', game_id, exe_path or '', install_path)
                            added += 1
                            logger.info(f"[ReconcileMap] Added GOG '{game_title}' to games.map")
                        else:
                            skipped += 1
                            logger.debug(f"[ReconcileMap] GOG '{game_title}' not installed or path missing")
                    
                    elif store == 'amazon' and game_id in amazon_installed:
                        game_data = amazon_installed[game_id]
                        install_path = game_data.get('path', '')
                        executable = game_data.get('executable', '')
                        
                        if install_path and os.path.exists(install_path):
                            await self._update_game_map('amazon', game_id, executable or '', install_path)
                            added += 1
                            logger.info(f"[ReconcileMap] Added Amazon '{game_title}' to games.map")
                        else:
                            skipped += 1
                            logger.debug(f"[ReconcileMap] Amazon '{game_title}' not installed or path missing")
                    else:
                        skipped += 1
                        
                except Exception as e:
                    errors.append(f"{game_title}: {e}")
                    logger.error(f"[ReconcileMap] Error processing {game_title}: {e}")
            
            if added > 0:
                logger.info(f"[ReconcileMap] Added {added} missing entries to games.map")
            else:
                logger.debug(f"[ReconcileMap] No missing entries ({already_mapped} already mapped, {skipped} skipped)")
                
        except Exception as e:
            logger.error(f"[ReconcileMap] Error: {e}")
            errors.append(str(e))
        
        return {'added': added, 'already_mapped': already_mapped, 'skipped': skipped, 'errors': errors}

    def validate_gog_exe_paths(self, gog_client=None) -> Dict[str, Any]:
        """
        Validate and auto-correct GOG executable paths that point to installers.
        
        If a GOG game's exe_path looks like an installer file (large .sh, contains colon, etc.),
        this function re-runs the game executable detection and updates games.map.
        
        Args:
            gog_client: Reference to GOGAPIClient for exe detection
            
        Returns:
            dict: {'corrected': int, 'checked': int, 'corrections': list}
        """
        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
        
        if not os.path.exists(map_file):
            return {'corrected': 0, 'checked': 0, 'corrections': []}
        
        corrected = 0
        checked = 0
        corrections = []
        modified_lines = []
        
        try:
            with open(map_file, 'r') as f:
                lines = f.readlines()
            
            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    modified_lines.append(line)
                    continue
                    
                parts = line_stripped.split('|')
                if len(parts) < 3:
                    modified_lines.append(line)
                    continue
                
                key = parts[0]  # store:game_id
                exe_path = parts[1]
                work_dir = parts[2]
                
                # Only check GOG games
                if not key.startswith('gog:'):
                    modified_lines.append(line)
                    continue
                
                checked += 1
                
                # Check if exe_path looks like an installer
                is_likely_installer = False
                if exe_path and exe_path.endswith('.sh'):
                    try:
                        if os.path.exists(exe_path):
                            file_size = os.path.getsize(exe_path)
                            filename = os.path.basename(exe_path)
                            is_likely_installer = (
                                file_size > 50 * 1024 * 1024 or  # Over 50MB
                                filename.startswith('gog_') or
                                filename.startswith('setup_') or
                                ':' in filename  # Game title pattern
                            )
                    except Exception:
                        pass
                
                if is_likely_installer and gog_client:
                    logger.info(f"[ValidateGOG] Detected installer path for {key}: {exe_path}")
                    
                    # Get the install directory (parent of exe or work_dir)
                    install_dir = work_dir if work_dir else os.path.dirname(exe_path)
                    
                    if install_dir and os.path.exists(install_dir):
                        # Re-run executable detection
                        new_exe = gog_client._find_game_executable(install_dir)
                        
                        if new_exe and new_exe != exe_path:
                            logger.info(f"[ValidateGOG] Correcting path: {exe_path} -> {new_exe}")
                            
                            # Update the line
                            new_work_dir = os.path.dirname(new_exe)
                            parts[1] = new_exe
                            parts[2] = new_work_dir
                            corrected_line = '|'.join(parts) + '\n'
                            modified_lines.append(corrected_line)
                            
                            corrections.append({
                                'game_id': key,
                                'old_path': exe_path,
                                'new_path': new_exe
                            })
                            corrected += 1
                            continue
                
                # Keep original line if no correction needed
                modified_lines.append(line)
            
            # Write back if corrections were made
            if corrected > 0:
                with open(map_file, 'w') as f:
                    f.writelines(modified_lines)
                logger.info(f"[ValidateGOG] Corrected {corrected} installer paths in games.map")
            
        except Exception as e:
            logger.error(f"[ValidateGOG] Error: {e}")
            return {'corrected': 0, 'checked': checked, 'corrections': [], 'error': str(e)}
        
        return {'corrected': corrected, 'checked': checked, 'corrections': corrections}

    def repair_shortcuts_exe_path(self) -> Dict[str, Any]:
        """
        Repair shortcuts pointing to old plugin paths after reinstall.
        
        Called on plugin startup to fix shortcuts where the exe path
        no longer exists (e.g., after Decky reinstall moves the plugin dir).
        
        Returns:
            dict: {'repaired': int, 'checked': int, 'errors': list}
        """
        import re
        
        repaired = 0
        checked = 0
        errors = []
        
        # Get the CURRENT launcher path (this plugin's installation)
        current_launcher = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')
        
        if not os.path.exists(current_launcher):
            logger.error(f"[RepairExe] Current launcher not found: {current_launcher}")
            return {'repaired': 0, 'checked': 0, 'errors': ['Current launcher not found']}
        
        logger.info(f"[RepairExe] Current launcher path: {current_launcher}")
        
        try:
            shortcuts_data = load_shortcuts_vdf(self.shortcuts_path)
            shortcuts = shortcuts_data.get('shortcuts', {})
            modified = False
            
            for idx, shortcut in shortcuts.items():
                launch_opts = shortcut.get('LaunchOptions', '')
                
                # Only check Unifideck shortcuts (store:game_id format)
                if re.match(r'^(epic|gog|amazon):[a-zA-Z0-9_-]+$', launch_opts):
                    checked += 1
                    exe_path = shortcut.get('exe', '')
                    
                    # Remove quotes if present
                    exe_path_clean = exe_path.strip('"')
                    
                    # Check if exe points to unifideck-launcher but at a different (old) path
                    if 'unifideck-launcher' in exe_path_clean and exe_path_clean != current_launcher:
                        # Check if the current exe doesn't exist (stale path)
                        if not os.path.exists(exe_path_clean):
                            logger.info(f"[RepairExe] Repairing shortcut '{shortcut.get('AppName')}': {exe_path_clean} -> {current_launcher}")
                            shortcut['exe'] = f'"{current_launcher}"'
                            shortcut['StartDir'] = f'"{os.path.dirname(current_launcher)}"'
                            repaired += 1
                            modified = True
                        else:
                            logger.debug(f"[RepairExe] Shortcut '{shortcut.get('AppName')}' has valid exe at: {exe_path_clean}")
            
            # Write back if modified
            if modified:
                success = save_shortcuts_vdf(self.shortcuts_path, shortcuts_data)
                if success:
                    logger.info(f"[RepairExe] Updated shortcuts.vdf: {repaired} repairs")
                else:
                    errors.append('Failed to write shortcuts.vdf')
            
        except Exception as e:
            logger.error(f"[RepairExe] Error: {e}")
            errors.append(str(e))
        
        return {'repaired': repaired, 'checked': checked, 'errors': errors}


    def reconcile_shortcuts_from_games_map(self) -> Dict[str, Any]:
        """
        Ensure shortcuts exist for all installed games in games.map.
        
        Called on plugin startup to create missing shortcuts for games
        that were installed but whose shortcuts were somehow lost.
        Uses shortcuts_registry.json to recover original appid (preserves artwork!).
        
        Returns:
            dict: {'created': int, 'existing': int, 'errors': list}
        """
        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
        
        if not os.path.exists(map_file):
            logger.debug("[ReconcileShortcuts] games.map not found, nothing to reconcile")
            return {'created': 0, 'existing': 0, 'errors': []}
        
        created = 0
        existing = 0
        errors = []
        
        # Get current launcher path
        current_launcher = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')
        
        try:
            # Load games.map entries
            games_map_entries = []
            with open(map_file, 'r') as f:
                for line in f:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    parts = line_stripped.split('|')
                    if len(parts) >= 3:
                        key = parts[0]  # store:game_id
                        exe_path = parts[1]
                        work_dir = parts[2]
                        
                        # Only include entries where the exe actually exists (installed games)
                        if exe_path and os.path.exists(exe_path):
                            games_map_entries.append({
                                'key': key,
                                'exe_path': exe_path,
                                'work_dir': work_dir
                            })
            
            if not games_map_entries:
                logger.debug("[ReconcileShortcuts] No valid games.map entries found")
                return {'created': 0, 'existing': 0, 'errors': []}
            
            logger.info(f"[ReconcileShortcuts] Found {len(games_map_entries)} installed games in games.map")
            
            # Load shortcuts.vdf
            shortcuts_data = load_shortcuts_vdf(self.shortcuts_path)
            shortcuts = shortcuts_data.get('shortcuts', {})
            
            # Build set of existing LaunchOptions
            existing_launch_options = {
                shortcut.get('LaunchOptions')
                for shortcut in shortcuts.values()
                if shortcut.get('LaunchOptions')
            }
            
            # Load shortcuts registry for appid recovery
            shortcuts_registry = load_shortcuts_registry()
            
            # Find next available index
            existing_indices = [int(k) for k in shortcuts.keys() if k.isdigit()]
            next_index = max(existing_indices, default=-1) + 1
            
            modified = False
            
            for entry in games_map_entries:
                key = entry['key']  # store:game_id
                
                if key in existing_launch_options:
                    existing += 1
                    continue
                
                # Parse store and game_id
                store, game_id = key.split(':', 1)
                
                # Try to recover appid from registry (preserves artwork!)
                registered = shortcuts_registry.get(key, {})
                appid = registered.get('appid')
                title = registered.get('title', game_id)  # Fallback to game_id if no title
                
                if not appid:
                    # Generate new appid if not registered
                    appid = self.generate_app_id(title, current_launcher)
                    logger.warning(f"[ReconcileShortcuts] No registered appid for {key}, generated new: {appid}")
                
                # Create new shortcut
                logger.info(f"[ReconcileShortcuts] Creating missing shortcut for '{title}' ({key})")
                
                shortcuts[str(next_index)] = {
                    'appid': appid,
                    'AppName': title,
                    'exe': f'"{current_launcher}"',
                    'StartDir': '',
                    'icon': '',
                    'ShortcutPath': '',
                    'LaunchOptions': key,
                    'IsHidden': 0,
                    'AllowDesktopConfig': 1,
                    'OpenVR': 0,
                    'tags': {
                        '0': store.title(),
                        '1': 'Installed'  # It's in games.map, so it's installed
                    }
                }
                
                next_index += 1
                created += 1
                modified = True
            
            # Write back if modified
            if modified:
                success = save_shortcuts_vdf(self.shortcuts_path, shortcuts_data)
                if success:
                    logger.info(f"[ReconcileShortcuts] Created {created} missing shortcuts")
                else:
                    errors.append('Failed to write shortcuts.vdf')
            else:
                logger.debug(f"[ReconcileShortcuts] All {existing} shortcuts already exist")
        
        except Exception as e:
            logger.error(f"[ReconcileShortcuts] Error: {e}")
            errors.append(str(e))
        
        return {'created': created, 'existing': existing, 'errors': errors}

    async def _set_proton_compatibility(self, app_id: int, compat_tool: str = "proton_experimental"):
        """Set Proton compatibility tool for a non-Steam game in config.vdf"""
        try:
            # config.vdf is in ~/.steam/steam/config/config.vdf (not in userdata)
            config_path = os.path.expanduser("~/.steam/steam/config/config.vdf")
            
            if not os.path.exists(config_path):
                logger.warning(f"config.vdf not found at {config_path}")
                return False
            
            # Read config.vdf
            with open(config_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # Convert app_id to unsigned for VDF (Steam uses unsigned 32-bit)
            unsigned_app_id = app_id & 0xFFFFFFFF
            app_id_str = str(unsigned_app_id)
            
            # Check if this app already has a mapping
            if f'"{app_id_str}"' in content:
                logger.info(f"App {app_id_str} already has a compat mapping")
                return True
            
            # Create compat entry with proper indentation (tabs as in config.vdf)
            compat_entry = f'''
					"{app_id_str}"
					{{
						"name"		"{compat_tool}"
						"config"		""
						"priority"		"250"
					}}'''
            
            # Check if CompatToolMapping section exists
            if '"CompatToolMapping"' not in content:
                logger.warning("CompatToolMapping section not found in config.vdf")
                return False
            
            # Find CompatToolMapping and insert our entry
            insert_marker = '"CompatToolMapping"'
            marker_pos = content.find(insert_marker)
            if marker_pos >= 0:
                # Find the opening brace after CompatToolMapping
                brace_pos = content.find('{', marker_pos)
                if brace_pos >= 0:
                    # Insert after the opening brace
                    new_content = content[:brace_pos+1] + compat_entry + content[brace_pos+1:]
                    
                    # Write back
                    with open(config_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    
                    logger.info(f"Set Proton compatibility ({compat_tool}) for app {app_id_str}")
                    return True
            
            logger.warning("Could not find insertion point in config.vdf")
            return False
            
        except Exception as e:
            logger.error(f"Error setting Proton compatibility: {e}", exc_info=True)
            return False

    async def _clear_proton_compatibility(self, app_id: int):
        """Clear Proton compatibility tool setting for a native Linux game"""
        try:
            config_path = os.path.expanduser("~/.steam/steam/config/config.vdf")
            
            if not os.path.exists(config_path):
                logger.warning(f"config.vdf not found at {config_path}")
                return False
            
            with open(config_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # Convert app_id to unsigned for VDF
            unsigned_app_id = app_id & 0xFFFFFFFF
            app_id_str = str(unsigned_app_id)
            
            # Check if this app has a mapping
            if f'"{app_id_str}"' not in content:
                logger.info(f"App {app_id_str} has no compat mapping to clear")
                return True  # Already clear
            
            # Find and remove the app's compat entry
            # Pattern: "app_id" { ... }
            import re
            # Match the app entry with its braces
            pattern = rf'(\s*"{app_id_str}"\s*\{{[^}}]*\}})'
            new_content = re.sub(pattern, '', content)
            
            if new_content != content:
                with open(config_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                logger.info(f"Cleared Proton compatibility for native Linux app {app_id_str}")
                return True
            else:
                logger.warning(f"Could not find/remove compat entry for {app_id_str}")
                return False
                
        except Exception as e:
            logger.error(f"Error clearing Proton compatibility: {e}", exc_info=True)
            return False

    def generate_app_id(self, game_title: str, exe_path: str) -> int:
        """Generate AppID for non-Steam game using CRC32"""
        # ... existing implementation ...
        key = f"{exe_path}{game_title}"
        crc = binascii.crc32(key.encode('utf-8')) & 0xFFFFFFFF
        app_id = crc | 0x80000000
        app_id = struct.unpack('i', struct.pack('I', app_id))[0]
        return app_id

    # ... existing read/write methods ...

    async def mark_installed(self, game_id: str, store: str, install_path: str, exe_path: str = None, work_dir: str = None) -> bool:
        """Mark a game as installed in shortcuts.vdf (Dynamic Launch)
        
        Args:
            game_id: Game identifier
            store: Store name (epic, gog, amazon)
            install_path: Path where game is installed
            exe_path: Path to game executable
            work_dir: Working directory for game execution (from goggame-*.info or fallback to exe dir)
        """
        try:
            logger.info(f"Marking {game_id} ({store}) as installed")
            logger.info(f"[MarkInstalled] Received: exe_path='{exe_path}', install_path='{install_path}', work_dir='{work_dir}'")
            
            # 1. Update dynamic map file (No Steam restart needed)
            # Use provided work_dir, otherwise fallback to exe directory, otherwise install path
            effective_work_dir = work_dir or (os.path.dirname(exe_path) if exe_path else install_path)
            await self._update_game_map(store, game_id, exe_path or "", effective_work_dir)

            # 2. Update shortcut to point to dynamic launcher
            shortcuts_data = await self.read_shortcuts()
            shortcuts = shortcuts_data.get('shortcuts', {})
            
            # Find existing shortcut by LaunchOptions (unquoted, as set by add_game)
            target_launch_options = f"{store}:{game_id}"  # No quotes!
            target_shortcut = None
            
            for s in shortcuts.values():
                opts = s.get('LaunchOptions', '')
                if get_full_id(opts) == target_launch_options:
                    target_shortcut = s
                    break
            
            if not target_shortcut:
                logger.warning(f"Game {game_id} not found in shortcuts")
                return False

            # 3. Ensure shortcut points to dynamic launcher (Corrects AppID consistency)
            runner_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')
            target_shortcut['exe'] = f'"{runner_script}"'
            target_shortcut['StartDir'] = f'"{os.path.dirname(runner_script)}"'
            target_shortcut['LaunchOptions'] = target_launch_options
            
            # 4. Clear Proton compatibility (Launcher handles it internally via UMU)
            app_id = target_shortcut.get('appid')
            if app_id:
                logger.info(f"Clearing Proton for AppID {app_id} (Managed by dynamic launcher)")
                await self._clear_proton_compatibility(app_id)

            
            # 5. Update tags
            tags = target_shortcut.get('tags', {})
            if isinstance(tags, dict):
                tag_values = list(tags.values())
            else:
                tag_values = list(tags) if tags else []
            
            if 'Not Installed' in tag_values: 
                tag_values.remove('Not Installed')
            if 'Installed' not in tag_values: 
                tag_values.append('Installed')
            
            target_shortcut['tags'] = {str(i): t for i, t in enumerate(tag_values)}
            
            # 6. Write back
            await self.write_shortcuts(shortcuts_data)
            logger.info(f"Updated shortcut for {game_id} to use dynamic launcher")
            return True

        except Exception as e:
            logger.error(f"Error marking installed: {e}", exc_info=True)
            return False

    async def read_shortcuts(self) -> Dict[str, Any]:
        """Read shortcuts.vdf file with in-memory caching"""
        if not self.shortcuts_path:
            logger.warning("shortcuts.vdf path not found, returning empty dict")
            return {"shortcuts": {}}
        
        # Check in-memory cache first
        now = time.time()
        if self._shortcuts_cache is not None and (now - self._shortcuts_cache_time) < self.SHORTCUTS_CACHE_TTL:
            return self._shortcuts_cache

        try:
            data = load_shortcuts_vdf(self.shortcuts_path)
            logger.debug(f"Loaded {len(data.get('shortcuts', {}))} shortcuts from disk")
            # Update cache
            self._shortcuts_cache = data
            self._shortcuts_cache_time = now
            return data
        except Exception as e:
            logger.error(f"Error reading shortcuts.vdf: {e}")
            return {"shortcuts": {}}

    async def write_shortcuts(self, shortcuts: Dict[str, Any]) -> bool:
        """Write shortcuts.vdf file and update in-memory cache"""
        if not self.shortcuts_path:
            logger.error("Cannot write shortcuts.vdf: path not found")
            return False

        try:
            # Ensure parent directory exists
            os.makedirs(os.path.dirname(self.shortcuts_path), exist_ok=True)

            success = save_shortcuts_vdf(self.shortcuts_path, shortcuts)
            if success:
                logger.info(f"Wrote {len(shortcuts.get('shortcuts', {}))} shortcuts to file")
                # Update in-memory cache with what we just wrote
                self._shortcuts_cache = shortcuts
                self._shortcuts_cache_time = time.time()
            else:
                # Invalidate cache on failure so next read gets fresh data
                self._shortcuts_cache = None
            return success
        except Exception as e:
            logger.error(f"Error writing shortcuts.vdf: {e}")
            # Invalidate cache on error
            self._shortcuts_cache = None
            return False

    async def add_game(self, game: Game, launcher_script: str) -> bool:
        """Add game to shortcuts.vdf"""
        try:
            shortcuts = await self.read_shortcuts()

            # Check if game already exists (duplicate detection)
            target_launch_options = f'{game.store}:{game.id}'
            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                if get_full_id(shortcut.get('LaunchOptions', '')) == target_launch_options:
                    logger.info(f"Game {game.title} already in shortcuts, skipping")
                    return True  # Already exists, not an error

            # Generate unique AppID (using launcher_script for consistent ID generation)
            # CRITICAL: For "No Restart" support, the exe path must NOT change after creation.
            # We always use unifideck-launcher as the executable.
            runner_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')
            app_id = self.generate_app_id(game.title, runner_script)

            # Find next available index
            existing_indices = [int(k) for k in shortcuts.get("shortcuts", {}).items() if k.isdigit()] # .keys(), fixed logic below
            existing_indices = [int(k) for k in shortcuts.get("shortcuts", {}).keys() if k.isdigit()]
            next_index = max(existing_indices, default=-1) + 1

            # Create shortcut entry
            shortcuts["shortcuts"][str(next_index)] = {
                'appid': app_id,
                'AppName': game.title,
                'exe': f'"{runner_script}"', # Always use runner
                'StartDir': '',
                'icon': game.cover_image or '',
                'ShortcutPath': '',
                'LaunchOptions': f'{game.store}:{game.id}',
                'IsHidden': 0,
                'AllowDesktopConfig': 1,
                'OpenVR': 0,
                'tags': {
                    '0': game.store.title(),
                    '1': 'Not Installed' if not game.is_installed else ''
                }
            }
            
            # Register this shortcut for future reconciliation
            register_shortcut(target_launch_options, app_id, game.title)

            # Write back
            return await self.write_shortcuts(shortcuts)

        except Exception as e:
            logger.error(f"Error adding game to shortcuts: {e}")
            return False

    async def add_games_batch(self, games: List[Game], launcher_script: str, valid_stores: List[str] = None) -> Dict[str, Any]:
        """
        Add multiple games in a single write operation with smart update logic.

        Smart update strategy:
        1. Remove ONLY orphaned Unifideck shortcuts (epic:/gog: games removed from library)
        2. Preserve all non-Unifideck shortcuts (xCloud, Heroic, etc.)
        3. Add new games, skipping duplicates
        4. Update existing games if needed

        This ensures user's original shortcuts are never lost, even when Steam is running.
        """
        try:
            shortcuts = await self.read_shortcuts()

            # STEP 1: Build set of current game LaunchOptions from Epic/GOG libraries
            current_launch_options = {f'{game.store}:{game.id}' for game in games}
            logger.debug(f"Current library has {len(current_launch_options)} games")

            # STEP 2: Remove ONLY orphaned Unifideck shortcuts (games removed from library)
            removed_count = 0
            for idx in list(shortcuts["shortcuts"].keys()):
                shortcut = shortcuts["shortcuts"][idx]
                launch = shortcut.get('LaunchOptions', '')

                # Only touch Unifideck shortcuts (epic: or gog:)
                if is_unifideck_shortcut(launch):
                    # Check if we should manage this store
                    store_prefix = get_store_prefix(launch)
                    if valid_stores is not None and store_prefix not in valid_stores:
                        continue

                    # If this game no longer exists in current library, it's orphaned
                    full_id = get_full_id(launch)
                    if full_id not in current_launch_options:
                        logger.debug(f"Removing orphaned shortcut: {shortcut.get('AppName')} ({launch})")
                        del shortcuts["shortcuts"][idx]
                        removed_count += 1

            if removed_count > 0:
                logger.info(f"Removed {removed_count} orphaned Unifideck shortcuts")

            # STEP 3: Build set of existing shortcuts for duplicate detection
            existing_launch_options = {
                shortcut.get('LaunchOptions')
                for shortcut in shortcuts.get("shortcuts", {}).values()
                if shortcut.get('LaunchOptions')
            }

            # STEP 4: Find next available index
            existing_indices = [int(k) for k in shortcuts.get("shortcuts", {}).keys() if k.isdigit()]
            next_index = max(existing_indices, default=-1) + 1

            # STEP 5: Add new games (skip duplicates) with reconciliation
            added = 0
            skipped = 0
            reclaimed = 0
            
            # Load shortcuts registry for reconciliation
            shortcuts_registry = load_shortcuts_registry()
            
            # Build appid lookup for existing shortcuts (for reconciliation)
            existing_appid_to_idx = {
                shortcut.get('appid'): idx
                for idx, shortcut in shortcuts.get("shortcuts", {}).items()
                if shortcut.get('appid')
            }

            for game in games:
                target_launch_options = f'{game.store}:{game.id}'

                # Skip if already exists with correct LaunchOptions
                if target_launch_options in existing_launch_options:
                    skipped += 1
                    continue

                # RECONCILIATION: Check if we have a registered appid for this game
                registered_appid = shortcuts_registry.get(target_launch_options, {}).get('appid')
                
                if registered_appid and registered_appid in existing_appid_to_idx:
                    # Found an orphaned shortcut with our registered appid - reclaim it!
                    orphan_idx = existing_appid_to_idx[registered_appid]
                    orphan = shortcuts["shortcuts"][orphan_idx]
                    
                    logger.info(f"Reclaiming orphaned shortcut for '{game.title}' (appid={registered_appid})")
                    
                    # Restore Unifideck ownership while preserving appid (keeps artwork!)
                    orphan['LaunchOptions'] = target_launch_options
                    orphan['exe'] = launcher_script
                    orphan['AppName'] = game.title
                    
                    # Update icon from cover_image (set by artwork download)
                    if game.cover_image:
                        orphan['icon'] = game.cover_image
                    
                    orphan['tags'] = {
                        '0': game.store.title(),
                        '1': 'Not Installed' if not game.is_installed else ''
                    }
                    
                    existing_launch_options.add(target_launch_options)
                    reclaimed += 1
                    continue

                # Generate AppID (using launcher_script for consistent ID generation)
                app_id = self.generate_app_id(game.title, launcher_script)

                # Add shortcut
                shortcuts["shortcuts"][str(next_index)] = {
                    'appid': app_id,
                    'AppName': game.title,
                    'exe': launcher_script,
                    'StartDir': '',
                    'icon': game.cover_image or '',
                    'ShortcutPath': '',
                    'LaunchOptions': target_launch_options,
                    'IsHidden': 0,
                    'AllowDesktopConfig': 1,
                    'OpenVR': 0,
                    'tags': {
                        '0': game.store.title(),
                        '1': 'Not Installed' if not game.is_installed else ''
                    }
                }
                
                # Register this shortcut for future reconciliation
                register_shortcut(target_launch_options, app_id, game.title)

                existing_launch_options.add(target_launch_options)
                next_index += 1
                added += 1

            # STEP 6: Write all shortcuts (only if something changed)
            if added > 0 or removed_count > 0 or reclaimed > 0:
                success = await self.write_shortcuts(shortcuts)
                if not success:
                    return {'added': 0, 'skipped': skipped, 'removed': removed_count, 'reclaimed': 0, 'error': 'errors.shortcutWriteFailed'}

                # Log sample of what was written
                if added > 0:
                    logger.info("Sample shortcuts written:")
                    shortcut_keys = list(shortcuts["shortcuts"].keys())
                    for idx in shortcut_keys[-min(3, added):]:
                        shortcut = shortcuts["shortcuts"][idx]
                        logger.info(f"  [{idx}] {shortcut['AppName']}")
                        logger.info(f"      LaunchOptions: {shortcut['LaunchOptions']}")


            logger.info(f"Batch update complete: {added} added, {skipped} skipped, {removed_count} removed, {reclaimed} reclaimed")
            return {'added': added, 'skipped': skipped, 'removed': removed_count, 'reclaimed': reclaimed}

        except Exception as e:
            logger.error(f"Error in batch add: {e}")
            import traceback
            traceback.print_exc()
            return {'added': 0, 'skipped': 0, 'removed': 0, 'reclaimed': 0, 'error': str(e)}

    async def force_update_games_batch(self, games: List[Game], launcher_script: str, valid_stores: List[str] = None) -> Dict[str, Any]:
        """
        Force update all games - rewrites existing shortcuts with fresh data.
        
        Unlike add_games_batch which skips existing shortcuts, this method:
        1. Updates ALL existing Unifideck shortcuts with current game data
        2. Updates exe path and StartDir for installed games
        3. Preserves artwork (does not affect grid/hero/logo files)
        4. Adds new games that don't exist yet
        
        Returns:
            Dict with 'added', 'updated', 'removed' counts
        """
        try:
            shortcuts = await self.read_shortcuts()

            # STEP 1: Build set of current game LaunchOptions from Epic/GOG libraries
            current_launch_options = {f'{game.store}:{game.id}' for game in games}
            logger.debug(f"Force update: {len(current_launch_options)} games in library")

            # Build game lookup by launch options
            games_by_launch_opts = {f'{game.store}:{game.id}': game for game in games}

            # STEP 2: Remove orphaned shortcuts and update existing ones
            removed_count = 0
            updated_count = 0
            repaired_count = 0  # Shortcuts recovered via appid lookup
            to_remove = []
            
            # Load shortcuts registry for appid-based recovery
            shortcuts_registry = load_shortcuts_registry()
            # Build reverse lookup: appid -> original launch_options
            appid_to_launch_opts = {
                entry['appid']: opts 
                for opts, entry in shortcuts_registry.items() 
                if 'appid' in entry
            }

            
            for idx in list(shortcuts["shortcuts"].keys()):
                shortcut = shortcuts["shortcuts"][idx]
                launch = shortcut.get('LaunchOptions', '')
                exe_path_current = shortcut.get('Exe', '').strip('"')

                # Only touch Unifideck shortcuts (epic: or gog:)
                if is_unifideck_shortcut(launch):
                    # Check if we should manage this store
                    store_prefix = get_store_prefix(launch)
                    if valid_stores is not None and store_prefix not in valid_stores:
                        continue

                    full_id = get_full_id(launch)
                    if full_id not in current_launch_options:
                        # Game ID in LaunchOptions doesn't match library
                        # BUT check if we can recover by appid BEFORE marking as orphan
                        app_id = shortcut.get('appid')
                        
                        if app_id and app_id in appid_to_launch_opts:
                            # This shortcut has a registered appid - recover it!
                            original_launch_opts = appid_to_launch_opts[app_id]
                            game = games_by_launch_opts.get(original_launch_opts)
                            
                            if game:
                                logger.info(f"[ForceSync] Repairing modified game ID: {shortcut.get('AppName')}")
                                logger.info(f"[ForceSync]   Corrupted: {launch} -> Correct: {original_launch_opts}")
                                
                                # Restore correct LaunchOptions
                                shortcut['LaunchOptions'] = original_launch_opts
                                shortcut['exe'] = launcher_script
                                shortcut['AppName'] = game.title
                                
                                # Update icon if available
                                if game.cover_image:
                                    shortcut['icon'] = game.cover_image
                                
                                # Update tags based on actual installation status
                                store_tag = game.store.title()
                                install_tag = '' if game.is_installed else 'Not Installed'
                                shortcut['tags'] = {
                                    '0': store_tag,
                                    '1': install_tag
                                } if install_tag else {'0': store_tag}
                                
                                # Update games.map if installed
                                if game.is_installed:
                                    if game.store == 'epic':
                                        metadata = await self.epic.get_installed()
                                        if game.id in metadata:
                                            meta = metadata[game.id]
                                            install_path = meta.get('install', {}).get('install_path')
                                            executable = meta.get('manifest', {}).get('launch_exe')
                                            if install_path and executable:
                                                exe_path = os.path.join(install_path, executable)
                                                work_dir = os.path.dirname(exe_path)
                                                await self._update_game_map(game.store, game.id, exe_path, work_dir)
                                    elif game.store == 'gog':
                                        game_info = self.gog.get_installed_game_info(game.id)
                                        if game_info and game_info.get('executable'):
                                            exe_path = game_info['executable']
                                            work_dir = os.path.dirname(exe_path)
                                            await self._update_game_map(game.store, game.id, exe_path, work_dir)
                                    elif game.store == 'amazon':
                                        game_info = self.amazon.get_installed_game_info(game.id)
                                        if game_info and game_info.get('executable'):
                                            exe_path = game_info['executable']
                                            work_dir = os.path.dirname(exe_path)
                                            await self._update_game_map(game.store, game.id, exe_path, work_dir)
                                
                                repaired_count += 1
                                continue  # Skip orphan removal, we repaired it
                        
                        # Truly orphaned - game no longer in library AND no appid recovery possible
                        logger.debug(f"Removing orphaned shortcut: {shortcut.get('AppName')} ({launch})")
                        to_remove.append(idx)
                        removed_count += 1
                    else:
                        # Existing game - update it with current data
                        game = games_by_launch_opts.get(full_id)
                        if game:
                            # Update shortcut fields
                            shortcut['AppName'] = game.title
                            shortcut['exe'] = launcher_script
                            shortcut['LaunchOptions'] = full_id  # Normalize to canonical form
                            
                            # Update icon from cover_image (set by artwork download)
                            if game.cover_image:
                                shortcut['icon'] = game.cover_image
                            
                            # Update tags
                            store_tag = game.store.title()
                            install_tag = '' if game.is_installed else 'Not Installed'
                            shortcut['tags'] = {
                                '0': store_tag,
                                '1': install_tag
                            } if install_tag else {'0': store_tag}
                            
                            updated_count += 1
                            logger.debug(f"Updated shortcut: {game.title}")
                # Also handle installed games that have empty LaunchOptions (already mark_installed)
                elif not launch and (exe_path_current.lower().endswith('.exe') or 'unifideck' in exe_path_current.lower()):
                    # This might be an installed Unifideck game - check by appid match
                    app_id = shortcut.get('appid')
                    for game in games:
                        expected_app_id = self.generate_app_id(game.title, launcher_script)
                        if app_id == expected_app_id:
                            # This is a Unifideck game - update it
                            # Keep the current exe/StartDir since it's installed
                            store_tag = game.store.title()
                            shortcut['tags'] = {'0': store_tag, '1': 'Installed'}
                            updated_count += 1
                            logger.debug(f"Updated installed shortcut: {game.title}")
                            break
                else:
                    # APPID-BASED RECOVERY: Check if this shortcut's appid is in our registry
                    # This handles cases where user modified/cleared LaunchOptions entirely
                    app_id = shortcut.get('appid')
                    if app_id and app_id in appid_to_launch_opts:
                        original_launch_opts = appid_to_launch_opts[app_id]
                        game = games_by_launch_opts.get(original_launch_opts)
                        
                        if game:
                            logger.info(f"[ForceSync] Repairing shortcut: {shortcut.get('AppName')} (restoring {original_launch_opts})")
                            
                            # Restore Unifideck ownership
                            shortcut['LaunchOptions'] = original_launch_opts
                            shortcut['exe'] = launcher_script
                            shortcut['AppName'] = game.title
                            
                            # Update icon if available
                            if game.cover_image:
                                shortcut['icon'] = game.cover_image
                            
                            # Update tags
                            store_tag = game.store.title()
                            install_tag = '' if game.is_installed else 'Not Installed'
                            shortcut['tags'] = {
                                '0': store_tag,
                                '1': install_tag
                            } if install_tag else {'0': store_tag}
                            
                            # Update games.map if installed
                            if game.is_installed:
                                game_info = None
                                if game.store == 'epic':
                                    metadata = await self.epic.get_installed()
                                    if game.id in metadata:
                                        meta = metadata[game.id]
                                        install_path = meta.get('install', {}).get('install_path')
                                        executable = meta.get('manifest', {}).get('launch_exe')
                                        if install_path and executable:
                                            exe_path = os.path.join(install_path, executable)
                                            work_dir = os.path.dirname(exe_path)
                                            await self._update_game_map(game.store, game.id, exe_path, work_dir)
                                elif game.store == 'gog':
                                    game_info = self.gog.get_installed_game_info(game.id)
                                    if game_info and game_info.get('executable'):
                                        exe_path = game_info['executable']
                                        work_dir = os.path.dirname(exe_path)
                                        await self._update_game_map(game.store, game.id, exe_path, work_dir)
                                elif game.store == 'amazon':
                                    game_info = self.amazon.get_installed_game_info(game.id)
                                    if game_info and game_info.get('executable'):
                                        exe_path = game_info['executable']
                                        work_dir = os.path.dirname(exe_path)
                                        await self._update_game_map(game.store, game.id, exe_path, work_dir)
                            
                            repaired_count += 1
            
            logger.info(f"[ForceSync] Repaired {repaired_count} shortcuts with missing/corrupted LaunchOptions")
            
            # Remove orphaned shortcuts
            for idx in to_remove:
                del shortcuts["shortcuts"][idx]

            # STEP 3: Build set of existing shortcuts for new game detection
            existing_app_ids = {
                shortcut.get('appid')
                for shortcut in shortcuts.get("shortcuts", {}).values()
            if shortcut.get('appid')
            }
            
            # Build appid to index lookup for reconciliation
            existing_appid_to_idx = {
                shortcut.get('appid'): idx
                for idx, shortcut in shortcuts.get("shortcuts", {}).items()
                if shortcut.get('appid')
            }
            
            # Build LaunchOptions set to prevent duplicates after repair
            # This catches repaired shortcuts whose appid differs from newly generated app_id
            existing_launch_options = {
                shortcut.get('LaunchOptions')
                for shortcut in shortcuts.get("shortcuts", {}).values()
                if shortcut.get('LaunchOptions')
            }
            
            # shortcuts_registry already loaded earlier for appid-based recovery

            # STEP 4: Find next available index
            existing_indices = [int(k) for k in shortcuts.get("shortcuts", {}).keys() if k.isdigit()]
            next_index = max(existing_indices, default=-1) + 1

            # STEP 5: Add NEW games only (those not already in shortcuts) with reconciliation
            added = 0
            reclaimed = 0

            for game in games:
                target_launch_options = f'{game.store}:{game.id}'
                
                # Skip if shortcut with this LaunchOptions already exists
                # (handles repaired shortcuts whose appid differs from newly generated)
                if target_launch_options in existing_launch_options:
                    continue
                
                app_id = self.generate_app_id(game.title, launcher_script)
                
                # Skip if already exists by app_id
                if app_id in existing_app_ids:
                    continue
                
                # RECONCILIATION: Check if we have a registered appid for this game
                registered_appid = shortcuts_registry.get(target_launch_options, {}).get('appid')
                
                if registered_appid and registered_appid in existing_appid_to_idx:
                    # Found an orphaned shortcut with our registered appid - reclaim it!
                    orphan_idx = existing_appid_to_idx[registered_appid]
                    orphan = shortcuts["shortcuts"][orphan_idx]
                    
                    logger.info(f"Reclaiming orphaned shortcut for '{game.title}' (appid={registered_appid})")
                    
                    # Restore Unifideck ownership while preserving appid (keeps artwork!)
                    orphan['LaunchOptions'] = target_launch_options
                    orphan['exe'] = launcher_script
                    orphan['AppName'] = game.title
                    
                    # Update icon from cover_image (set by artwork download)
                    if game.cover_image:
                        orphan['icon'] = game.cover_image
                    
                    orphan['tags'] = {
                        '0': game.store.title(),
                        '1': 'Not Installed' if not game.is_installed else ''
                    }
                    
                    existing_app_ids.add(registered_appid)
                    reclaimed += 1
                    continue

                # Add new shortcut
                shortcuts["shortcuts"][str(next_index)] = {
                    'appid': app_id,
                    'AppName': game.title,
                    'exe': launcher_script,
                    'StartDir': '',
                    'icon': game.cover_image or '',
                    'ShortcutPath': '',
                    'LaunchOptions': target_launch_options,
                    'IsHidden': 0,
                    'AllowDesktopConfig': 1,
                    'OpenVR': 0,
                    'tags': {
                        '0': game.store.title(),
                        '1': 'Not Installed' if not game.is_installed else ''
                    }
                }
                
                # Register this shortcut for future reconciliation
                register_shortcut(target_launch_options, app_id, game.title)

                existing_app_ids.add(app_id)
                next_index += 1
                added += 1

            # STEP 6: Write all shortcuts
            if added > 0 or updated_count > 0 or removed_count > 0 or reclaimed > 0:
                success = await self.write_shortcuts(shortcuts)
                if not success:
                    return {'added': 0, 'updated': 0, 'removed': 0, 'reclaimed': 0, 'error': 'errors.shortcutWriteFailed'}

            logger.info(f"Force update complete: {added} added, {updated_count} updated, {removed_count} removed, {reclaimed} reclaimed")
            return {'added': added, 'updated': updated_count, 'removed': removed_count, 'reclaimed': reclaimed}

        except Exception as e:
            logger.error(f"Error in force batch update: {e}")
            import traceback
            traceback.print_exc()
            return {'added': 0, 'updated': 0, 'removed': 0, 'reclaimed': 0, 'error': str(e)}

    async def mark_uninstalled(self, game_title: str, store: str, game_id: str) -> bool:
        """Revert game shortcut to uninstalled status (Dynamic)"""
        try:
            # 1. Remove from dynamic map
            await self._remove_from_game_map(store, game_id)

            shortcuts = await self.read_shortcuts()
            runner_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')
            target_launch_options = f'{store}:{game_id}'

            # Find shortcut by LaunchOptions (reliable) or AppName (fallback)
            target_shortcut = None
            for idx, s in shortcuts.get("shortcuts", {}).items():
                if get_full_id(s.get('LaunchOptions', '')) == target_launch_options:
                    target_shortcut = s
                    break
            
            if not target_shortcut:
                for idx, s in shortcuts.get("shortcuts", {}).items():
                    if s.get('AppName') == game_title:
                        target_shortcut = s
                        break

            if target_shortcut:
                # Revert shortcut fields
                # CRITICAL: Keep exe as unifideck-runner to preserve AppID
                target_shortcut['exe'] = f'"{runner_script}"'
                target_shortcut['StartDir'] = f'"{os.path.dirname(runner_script)}"'
                target_shortcut['LaunchOptions'] = target_launch_options  # No quotes!

                # Update tags
                tags = target_shortcut.get('tags', {})
                # Convert dict tags to list for manipulation if needed, but here we assume dict structure from vdf
                # vdf tags are weird: {'0': 'tag1', '1': 'tag2'}
                # Simplest is to rebuild it
                tag_values = [v for k, v in tags.items()]
                if 'Installed' in tag_values: tag_values.remove('Installed')
                if 'Not Installed' not in tag_values: tag_values.append('Not Installed')
                
                target_shortcut['tags'] = {str(i): t for i, t in enumerate(tag_values)}

                logger.info(f"Marked {game_title} as uninstalled (Dynamic)")
                return await self.write_shortcuts(shortcuts)

            logger.warning(f"Shortcut for {game_title} not found")
            return False

        except Exception as e:
            logger.error(f"Error marking game as uninstalled: {e}", exc_info=True)
            return False

    def _find_game_executable(self, store: str, install_path: str, game_id: str) -> Optional[str]:
        """Find game executable in install directory

        Args:
            store: Store name ('epic' or 'gog')
            install_path: Game installation directory
            game_id: Game ID

        Returns:
            Path to game executable or None
        """
        try:
            if store == 'gog':
                # GOG games - look for common launcher scripts
                common_launchers = ['start.sh', 'launch.sh', 'game.sh', 'gameinfo']

                # Try common launcher names in root
                for launcher in common_launchers:
                    launcher_path = os.path.join(install_path, launcher)
                    if os.path.exists(launcher_path) and os.path.isfile(launcher_path):
                        os.chmod(launcher_path, 0o755)  # Ensure executable
                        logger.info(f"Found GOG launcher: {launcher_path}")
                        return launcher_path

                # Look for any .sh file in root
                for item in os.listdir(install_path):
                    if item.endswith('.sh'):
                        item_path = os.path.join(install_path, item)
                        if os.path.isfile(item_path):
                            os.chmod(item_path, 0o755)
                            logger.info(f"Found GOG .sh script: {item_path}")
                            return item_path

                # Check data/noarch subdirectory (common in GOG installers)
                data_dir = os.path.join(install_path, 'data', 'noarch')
                if os.path.exists(data_dir):
                    for launcher in common_launchers:
                        launcher_path = os.path.join(data_dir, launcher)
                        if os.path.exists(launcher_path) and os.path.isfile(launcher_path):
                            os.chmod(launcher_path, 0o755)
                            return launcher_path

                logger.warning(f"No GOG launcher found in {install_path}")
                return None

            elif store == 'epic':
                # Epic games - get from legendary
                # This should already be provided by the caller, but fallback just in case
                logger.warning(f"Epic game executable lookup not implemented in _find_game_executable")
                return None

            else:
                logger.warning(f"Unknown store: {store}")
                return None

        except Exception as e:
            logger.error(f"Error finding game executable: {e}", exc_info=True)
            return None

    async def remove_game(self, game_id: str, store: str) -> bool:
        """Remove game from shortcuts.vdf"""
        try:
            shortcuts = await self.read_shortcuts()

            target_launch_options = f'{store}:{game_id}'
            for idx, shortcut in list(shortcuts.get("shortcuts", {}).items()):
                if get_full_id(shortcut.get('LaunchOptions', '')) == target_launch_options:
                    del shortcuts["shortcuts"][idx]
                    logger.info(f"Removed {game_id} from shortcuts")
                    return await self.write_shortcuts(shortcuts)

            logger.warning(f"Game {game_id} not found in shortcuts")
            return False

        except Exception as e:
            logger.error(f"Error removing game: {e}")
            return False


# ============================================================================
# EpicConnector - Now imported from backend.stores.epic module
# ============================================================================
if BACKEND_AVAILABLE:
    EpicConnector = BackendEpicConnector
else:
    raise ImportError("backend.stores.epic module is required but not available")

# ============================================================================
# AmazonConnector - Now imported from backend.stores.amazon module
# ============================================================================
if BACKEND_AVAILABLE:
    AmazonConnector = BackendAmazonConnector
else:
    raise ImportError("backend.stores.amazon module is required but not available")

# ============================================================================
# GOGAPIClient - Now imported from backend.stores.gog module
# ============================================================================
if BACKEND_AVAILABLE:
    GOGAPIClient = BackendGOGAPIClient
else:
    raise ImportError("backend.stores.gog module is required but not available")

