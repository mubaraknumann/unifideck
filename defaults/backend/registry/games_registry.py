"""
Central game registry with JSON storage.

Replaces the line-based games.map with a structured JSON format that supports:
- Rich metadata (title, installed_at, platform)
- Shortcut appid tracking (for artwork preservation across reinstalls)
- Relative paths (for recovery after game moves)
- Backwards compatibility with legacy games.map for the launcher
"""
import json
import os
import logging
from dataclasses import dataclass, asdict, field
from typing import Dict, Optional, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

REGISTRY_PATH = os.path.expanduser("~/.local/share/unifideck/games_registry.json")
LEGACY_MAP_PATH = os.path.expanduser("~/.local/share/unifideck/games.map")


@dataclass
class GameEntry:
    """Represents an installed game in the registry"""
    store: str
    game_id: str
    title: str
    install_path: str
    executable: str
    work_dir: str
    executable_relative: str = ""  # Relative to install_path for recovery
    shortcut_appid: Optional[int] = None  # Steam shortcut appid for artwork tracking
    installed_at: Optional[str] = None
    platform: str = "windows"  # "windows" or "linux"
    
    @property
    def key(self) -> str:
        return f"{self.store}:{self.game_id}"
    
    def __post_init__(self):
        # Auto-compute relative path if not provided
        if not self.executable_relative and self.executable and self.install_path:
            try:
                if self.executable.startswith(self.install_path):
                    self.executable_relative = os.path.relpath(self.executable, self.install_path)
            except ValueError:
                pass  # Different drives on Windows, can't compute relative path


class GamesRegistry:
    """
    Manages the central game registry with JSON storage.
    
    This is the new authoritative source of truth for installed games,
    replacing the line-based games.map. For backwards compatibility,
    it also writes the legacy format for the launcher script.
    """
    
    def __init__(self):
        self._data: Dict[str, GameEntry] = {}
        self._dirty = False
        self._load()
    
    def _load(self):
        """Load registry from disk, migrating from legacy format if needed"""
        if os.path.exists(REGISTRY_PATH):
            try:
                with open(REGISTRY_PATH, 'r') as f:
                    data = json.load(f)
                for key, entry_dict in data.items():
                    # Handle missing fields gracefully
                    self._data[key] = GameEntry(
                        store=entry_dict.get('store', 'unknown'),
                        game_id=entry_dict.get('game_id', key.split(':', 1)[-1] if ':' in key else key),
                        title=entry_dict.get('title', ''),
                        install_path=entry_dict.get('install_path', ''),
                        executable=entry_dict.get('executable', ''),
                        work_dir=entry_dict.get('work_dir', ''),
                        executable_relative=entry_dict.get('executable_relative', ''),
                        shortcut_appid=entry_dict.get('shortcut_appid'),
                        installed_at=entry_dict.get('installed_at'),
                        platform=entry_dict.get('platform', 'windows')
                    )
                logger.info(f"[Registry] Loaded {len(self._data)} entries from JSON registry")
            except Exception as e:
                logger.error(f"[Registry] Failed to load JSON registry: {e}")
                self._data = {}
        
        # If no JSON registry exists, try to migrate from legacy
        if not self._data and os.path.exists(LEGACY_MAP_PATH):
            self._migrate_legacy()
    
    def _migrate_legacy(self):
        """Migrate from line-based games.map to JSON registry"""
        logger.info("[Registry] Migrating from legacy games.map...")
        migrated = 0
        try:
            with open(LEGACY_MAP_PATH, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or '|' not in line:
                        continue
                    parts = line.split('|')
                    if len(parts) >= 3:
                        key = parts[0]
                        exe_path = parts[1]
                        work_dir = parts[2]
                        
                        store, game_id = key.split(':', 1) if ':' in key else ('unknown', key)
                        
                        # Determine install path from work_dir
                        install_path = work_dir
                        
                        # Compute relative path
                        exe_relative = ""
                        if exe_path and install_path:
                            try:
                                exe_relative = os.path.relpath(exe_path, install_path)
                            except ValueError:
                                exe_relative = os.path.basename(exe_path)
                        
                        # Determine platform
                        platform = "windows" if exe_path.lower().endswith('.exe') else "linux"
                        
                        self._data[key] = GameEntry(
                            store=store,
                            game_id=game_id,
                            title=game_id,  # Unknown, will be updated on next sync
                            install_path=install_path,
                            executable=exe_path,
                            work_dir=work_dir,
                            executable_relative=exe_relative,
                            installed_at=datetime.now().isoformat(),
                            platform=platform
                        )
                        migrated += 1
            
            if migrated > 0:
                self._save()
                logger.info(f"[Registry] Migrated {migrated} entries from legacy games.map")
                
                # Rename legacy file as backup
                backup_path = LEGACY_MAP_PATH + ".bak"
                try:
                    if os.path.exists(backup_path):
                        os.remove(backup_path)
                    os.rename(LEGACY_MAP_PATH, backup_path)
                    logger.info(f"[Registry] Backed up legacy games.map to {backup_path}")
                except Exception as e:
                    logger.warning(f"[Registry] Could not backup legacy file: {e}")
            
        except Exception as e:
            logger.error(f"[Registry] Migration failed: {e}")
    
    def _save(self):
        """Persist registry to disk"""
        os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
        try:
            with open(REGISTRY_PATH, 'w') as f:
                json.dump({k: asdict(v) for k, v in self._data.items()}, f, indent=2)
            logger.debug(f"[Registry] Saved {len(self._data)} entries to JSON registry")
            
            # Also write legacy format for launcher compatibility
            self._write_legacy_map()
            self._dirty = False
        except Exception as e:
            logger.error(f"[Registry] Failed to save: {e}")
    
    def _write_legacy_map(self):
        """Write legacy games.map for launcher script compatibility"""
        try:
            os.makedirs(os.path.dirname(LEGACY_MAP_PATH), exist_ok=True)
            lines = []
            for key, entry in self._data.items():
                lines.append(f"{key}|{entry.executable}|{entry.work_dir}\n")
            with open(LEGACY_MAP_PATH, 'w') as f:
                f.writelines(lines)
            logger.debug(f"[Registry] Wrote {len(lines)} entries to legacy games.map")
        except Exception as e:
            logger.error(f"[Registry] Failed to write legacy map: {e}")
    
    def register(self, entry: GameEntry) -> None:
        """Add or update a game entry"""
        self._data[entry.key] = entry
        self._save()
        logger.info(f"[Registry] Registered {entry.key}: {entry.title}")
    
    def register_game(
        self,
        store: str,
        game_id: str,
        title: str,
        install_path: str,
        executable: str,
        work_dir: str,
        shortcut_appid: Optional[int] = None,
        platform: str = "windows"
    ) -> GameEntry:
        """Convenience method to register a game with individual parameters"""
        entry = GameEntry(
            store=store,
            game_id=game_id,
            title=title,
            install_path=install_path,
            executable=executable,
            work_dir=work_dir,
            shortcut_appid=shortcut_appid,
            installed_at=datetime.now().isoformat(),
            platform=platform
        )
        self.register(entry)
        return entry
    
    def get(self, store: str, game_id: str) -> Optional[GameEntry]:
        """Get a game entry by store and ID"""
        return self._data.get(f"{store}:{game_id}")
    
    def get_by_key(self, key: str) -> Optional[GameEntry]:
        """Get a game entry by its full key (store:game_id)"""
        return self._data.get(key)
    
    def remove(self, store: str, game_id: str) -> bool:
        """Remove a game entry"""
        key = f"{store}:{game_id}"
        if key in self._data:
            del self._data[key]
            self._save()
            logger.info(f"[Registry] Removed {key}")
            return True
        return False
    
    def is_installed(self, store: str, game_id: str) -> bool:
        """Check if game is registered AND files exist on disk"""
        entry = self.get(store, game_id)
        if not entry:
            return False
        
        # Verify path still exists
        path_to_check = entry.executable or entry.work_dir
        if path_to_check and os.path.exists(path_to_check):
            return True
        
        # Stale entry - auto-cleanup
        logger.info(f"[Registry] Stale entry detected for {store}:{game_id} (path missing), removing")
        self.remove(store, game_id)
        return False
    
    def update_shortcut_appid(self, store: str, game_id: str, appid: int) -> bool:
        """Update the shortcut appid for a game (for artwork tracking)"""
        entry = self.get(store, game_id)
        if entry:
            entry.shortcut_appid = appid
            self._save()
            logger.debug(f"[Registry] Updated appid for {store}:{game_id} to {appid}")
            return True
        return False
    
    def update_title(self, store: str, game_id: str, title: str) -> bool:
        """Update the title for a game (used during sync to fix migrated entries)"""
        entry = self.get(store, game_id)
        if entry and entry.title != title:
            entry.title = title
            self._dirty = True
            return True
        return False
    
    def all_entries(self) -> Dict[str, GameEntry]:
        """Get all registry entries"""
        return self._data.copy()
    
    def count(self) -> int:
        """Get the number of registered games"""
        return len(self._data)
    
    def flush(self) -> None:
        """Force save if there are pending changes"""
        if self._dirty:
            self._save()
    
    def reconcile(self) -> Dict[str, int]:
        """
        Remove stale entries where files no longer exist.
        
        Returns:
            Dict with 'kept' and 'removed' counts
        """
        stats = {'kept': 0, 'removed': 0}
        keys_to_remove = []
        
        for key, entry in self._data.items():
            path_to_check = entry.executable or entry.work_dir
            if path_to_check and os.path.exists(path_to_check):
                stats['kept'] += 1
            else:
                keys_to_remove.append(key)
                stats['removed'] += 1
                logger.info(f"[Registry] Reconcile: removing stale entry {key}")
        
        for key in keys_to_remove:
            del self._data[key]
        
        if keys_to_remove:
            self._save()
        
        logger.info(f"[Registry] Reconcile complete: {stats}")
        return stats


# Global singleton instance
_registry_instance: Optional[GamesRegistry] = None


def get_registry() -> GamesRegistry:
    """Get the global registry instance (singleton)"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = GamesRegistry()
    return _registry_instance
