"""
Startup discovery service.

Scans game directories on plugin load to:
- Rebuild registry from manifests after plugin reinstall
- Detect games installed outside normal flow
- Repair stale entries

Also provides utilities for writing per-game manifests during installation.
"""
import json
import os
import logging
from typing import Dict, Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)

# Manifest filename written to each game's install directory
MANIFEST_FILENAME = ".unifideck_manifest.json"


def write_game_manifest(
    install_path: str,
    store: str,
    game_id: str,
    title: str,
    executable_relative: str,
    platform: str = "windows",
    unifideck_version: str = "1.0"
) -> bool:
    """
    Write a manifest file to a game's installation directory.
    
    This manifest allows the game to be rediscovered after plugin reinstall
    or if the registry is lost.
    
    Args:
        install_path: Game installation directory
        store: Store name (epic, gog, amazon)
        game_id: Game ID from the store
        title: Game title
        executable_relative: Relative path to executable from install_path
        platform: "windows" or "linux"
        unifideck_version: Version of manifest format
        
    Returns:
        True if manifest was written successfully
    """
    try:
        manifest = {
            "unifideck_version": unifideck_version,
            "store": store,
            "store_id": game_id,
            "title": title,
            "executable_relative": executable_relative,
            "installed_at": datetime.now().isoformat(),
            "platform": platform
        }
        
        manifest_path = os.path.join(install_path, MANIFEST_FILENAME)
        with open(manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2)
        
        logger.info(f"[Discovery] Wrote manifest for {store}:{game_id} at {manifest_path}")
        return True
        
    except Exception as e:
        logger.error(f"[Discovery] Failed to write manifest for {store}:{game_id}: {e}")
        return False


def read_game_manifest(game_dir: str) -> Optional[Dict[str, Any]]:
    """
    Read a manifest file from a game directory.
    
    Args:
        game_dir: Path to game installation directory
        
    Returns:
        Manifest dict or None if not found/invalid
    """
    manifest_path = os.path.join(game_dir, MANIFEST_FILENAME)
    if not os.path.exists(manifest_path):
        return None
    
    try:
        with open(manifest_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"[Discovery] Error reading manifest at {manifest_path}: {e}")
        return None


async def discover_installed_games(registry=None) -> Dict[str, int]:
    """
    Scan all game directories for .unifideck_manifest.json files.
    
    Rebuilds registry entries for any games found on disk but not in registry.
    This is the primary recovery mechanism after plugin reinstall.
    
    Args:
        registry: Optional GamesRegistry instance. If not provided, will import and use singleton.
        
    Returns:
        Dict with 'discovered', 'already_registered', 'failed' counts
    """
    # Import here to avoid circular imports
    from ..utils.paths import get_all_game_directories
    from ..registry.games_registry import get_registry, GameEntry
    
    if registry is None:
        registry = get_registry()
    
    stats = {'discovered': 0, 'already_registered': 0, 'failed': 0}
    
    game_directories = get_all_game_directories()
    logger.info(f"[Discovery] Scanning {len(game_directories)} directories for manifests...")
    
    for base_path in game_directories:
        if not os.path.isdir(base_path):
            continue
        
        try:
            for item in os.listdir(base_path):
                item_path = os.path.join(base_path, item)
                if not os.path.isdir(item_path):
                    continue
                
                manifest = read_game_manifest(item_path)
                if not manifest:
                    continue
                
                store = manifest.get('store')
                game_id = manifest.get('store_id')
                
                if not store or not game_id:
                    logger.debug(f"[Discovery] Invalid manifest at {item_path}: missing store or store_id")
                    stats['failed'] += 1
                    continue
                
                # Check if already registered
                existing = registry.get(store, game_id)
                if existing:
                    stats['already_registered'] += 1
                    continue
                
                # Reconstruct executable path
                exe_relative = manifest.get('executable_relative', '')
                exe_path = os.path.join(item_path, exe_relative) if exe_relative else ""
                
                if exe_path and not os.path.exists(exe_path):
                    logger.warning(f"[Discovery] Executable not found for {store}:{game_id}: {exe_path}")
                    # Still register but with empty exe_path - can be repaired later
                    exe_path = ""
                
                # Register discovered game
                try:
                    entry = GameEntry(
                        store=store,
                        game_id=game_id,
                        title=manifest.get('title', game_id),
                        install_path=item_path,
                        executable=exe_path,
                        work_dir=os.path.dirname(exe_path) if exe_path else item_path,
                        executable_relative=exe_relative,
                        installed_at=manifest.get('installed_at'),
                        platform=manifest.get('platform', 'windows')
                    )
                    registry.register(entry)
                    stats['discovered'] += 1
                    logger.info(f"[Discovery] Recovered {store}:{game_id} ({manifest.get('title', 'Unknown')})")
                    
                except Exception as e:
                    logger.error(f"[Discovery] Failed to register {store}:{game_id}: {e}")
                    stats['failed'] += 1
                    
        except PermissionError:
            logger.debug(f"[Discovery] Permission denied scanning: {base_path}")
        except Exception as e:
            logger.error(f"[Discovery] Error scanning {base_path}: {e}")
    
    logger.info(f"[Discovery] Complete: discovered={stats['discovered']}, "
                f"already_registered={stats['already_registered']}, failed={stats['failed']}")
    return stats


async def discover_and_log() -> Dict[str, int]:
    """
    Convenience wrapper that logs discovery stats.
    Called automatically on plugin startup.
    """
    stats = await discover_installed_games()
    
    if stats['discovered'] > 0:
        logger.info(f"[Discovery] Recovered {stats['discovered']} games from disk manifests")
    
    return stats
