"""
InstallService - Handles game installation and uninstallation logic.

Responsibilities:
- Install games from Epic, GOG, Amazon stores
- Uninstall games with optional prefix cleanup
- Update shortcuts after install/uninstall operations
- Coordinate with store connectors for platform-specific operations
"""

import os
import asyncio
import logging
from typing import Dict, Any, Optional
from backend.utils.paths import LEGENDARY_CONFIG_DIR, get_prefix_path

logger = logging.getLogger(__name__)


class InstallService:
    """Service for installing and uninstalling games."""
    
    def __init__(self, epic_connector, gog_connector, amazon_connector, shortcuts_manager, install_handler):
        """Initialize InstallService with store connectors and managers.
        
        Args:
            epic_connector: EpicConnector instance for Epic Games Store
            gog_connector: GOGAPIClient instance for GOG
            amazon_connector: AmazonConnector instance for Amazon Games
            shortcuts_manager: ShortcutsManager instance for Steam shortcuts
            install_handler: InstallHandler instance for game installation
        """
        self.epic = epic_connector
        self.gog = gog_connector
        self.amazon = amazon_connector
        self.shortcuts_manager = shortcuts_manager
        self.install_handler = install_handler
    
    async def install_game_by_appid(self, app_id: int, get_game_info_func) -> Dict[str, Any]:
        """Install game by Steam shortcut app ID.
        
        Args:
            app_id: Steam shortcut app ID
            get_game_info_func: Async function to get game info from app_id
            
        Returns:
            Dict with success status and progress updates
        """
        try:
            # Get game info first
            game_info = await get_game_info_func(app_id)
            
            if 'error' in game_info:
                return game_info
            
            store = game_info['store']
            game_id = game_info['game_id']
            title = game_info['title']
            
            logger.info(f"[Install] Starting installation: {title} ({store}:{game_id})")
            
            # Start installation
            if store == 'epic':
                result = await self.epic.install_game(game_id)
                
                # Get executable path after installation
                if result.get('success'):
                    exe_path = await self._get_epic_executable(game_id)
                    if exe_path:
                        result['exe_path'] = exe_path
                        logger.info(f"[Install] Got Epic executable path: {exe_path}")
                    else:
                        logger.warning(f"[Install] Could not get executable path for {game_id}")
            
            elif store == 'gog':
                result = await self.gog.install_game(game_id)
            elif store == 'amazon':
                result = await self.amazon.install_game(game_id)
            else:
                return {'success': False, 'error': f'Unknown store: {store}'}
            
            # If successful, update shortcut to mark as installed
            if result.get('success'):
                logger.info(f"[Install] Installation successful, updating shortcut for {title}")
                # Handle both 'exe_path' (Epic) and 'executable' (GOG) keys
                exe_path = result.get('exe_path') or result.get('executable')
                await self.shortcuts_manager.mark_installed(
                    game_id,
                    store,
                    result.get('install_path', ''),
                    exe_path
                )
            else:
                logger.error(f"[Install] Installation failed for {title}: {result.get('error')}")
            
            return result
        
        except Exception as e:
            logger.error(f"Error installing game by app ID {app_id}: {e}")
            return {'success': False, 'error': str(e)}
    
    async def uninstall_game_by_appid(self, app_id: int, delete_prefix: bool, get_game_info_func) -> Dict[str, Any]:
        """Uninstall game by Steam shortcut app ID.
        
        Args:
            app_id: Steam shortcut app ID
            delete_prefix: If True, also delete the Wine/Proton prefix directory
            get_game_info_func: Async function to get game info from app_id
            
        Returns:
            Dict with success status and uninstall details
        """
        try:
            # Get game info first
            game_info = await get_game_info_func(app_id)
            
            if 'error' in game_info:
                return game_info
            
            store = game_info['store']
            game_id = game_info['game_id']
            title = game_info['title']
            
            # Check if actually installed
            if not game_info.get('is_installed'):
                return {'success': False, 'error': 'errors.gameNotInstalled'}
            
            logger.info(f"[Uninstall] Starting uninstallation: {title} ({store}:{game_id}), delete_prefix={delete_prefix}")
            
            # Perform store-specific uninstall
            if store == 'epic':
                result = await self._uninstall_epic_game(game_id)
                if not result['success']:
                    # Still remove from games.map so UI shows Install button
                    await self.shortcuts_manager._remove_from_game_map(store, game_id)
                    logger.info(f"[Uninstall] Removed {store}:{game_id} from games.map despite uninstall failure")
                    return result
            
            elif store == 'gog':
                # Get install path from games.map (same data used for launching)
                install_path = self.shortcuts_manager._get_install_dir_from_game_map(store, game_id)
                if install_path:
                    logger.info(f"[Uninstall] Using games.map path for GOG: {install_path}")
                    result = await self.gog.uninstall_game(game_id, install_path=install_path)
                else:
                    # Fallback to filesystem scan
                    result = await self.gog.uninstall_game(game_id)
                if not result['success']:
                    # Still remove from games.map so UI shows Install button
                    await self.shortcuts_manager._remove_from_game_map(store, game_id)
                    logger.info(f"[Uninstall] Removed {store}:{game_id} from games.map despite uninstall failure")
                    return result
            
            elif store == 'amazon':
                result = await self.amazon.uninstall_game(game_id)
                if not result['success']:
                    # Still remove from games.map so UI shows Install button
                    await self.shortcuts_manager._remove_from_game_map(store, game_id)
                    logger.info(f"[Uninstall] Removed {store}:{game_id} from games.map despite uninstall failure")
                    return result
            
            else:
                return {'success': False, 'error': f"Unsupported store for uninstall: {store}"}
            
            # Delete prefix if requested
            prefix_deleted = False
            if delete_prefix:
                prefix_deleted = await self._delete_prefix(game_id)
            
            # Update shortcut
            logger.info(f"[Uninstall] Reverting shortcut for {title}...")
            shortcut_updated = await self.shortcuts_manager.mark_uninstalled(title, store, game_id)
            
            if not shortcut_updated:
                logger.warning(f"[Uninstall] Failed to revert shortcut for {title}")
                return {
                    'success': True,
                    'message': 'Game uninstalled, but shortcut could not be updated. Restart Steam to fix.',
                    'prefix_deleted': prefix_deleted,
                    'game_update': {'appId': app_id, 'store': store, 'isInstalled': False}
                }
            
            return {
                'success': True,
                'message': f'{title} uninstalled successfully',
                'prefix_deleted': prefix_deleted,
                'game_update': {'appId': app_id, 'store': store, 'isInstalled': False}
            }
        
        except Exception as e:
            logger.error(f"[Uninstall] Error uninstalling game {app_id}: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}
    
    async def _uninstall_epic_game(self, game_id: str) -> Dict[str, Any]:
        """Uninstall Epic game using legendary.
        
        Args:
            game_id: Epic game ID
            
        Returns:
            Dict with success status
        """
        if not self.epic.legendary_bin:
            return {'success': False, 'error': 'errors.legendaryNotFound'}
        
        # Clean up stale legendary lock files (legendary returns 0 even when blocked by lock)
        for lock_file in ['installed.json.lock', 'user.json.lock']:
            lock_path = os.path.join(LEGENDARY_CONFIG_DIR, lock_file)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                    logger.info(f"[Uninstall] Cleared stale lock: {lock_file}")
                except Exception as e:
                    logger.warning(f"[Uninstall] Could not clear lock {lock_file}: {e}")
        
        cmd = [self.epic.legendary_bin, 'uninstall', game_id, '--yes']
        logger.info(f"[Uninstall] Running: {' '.join(cmd)}")
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        stdout_str = stdout.decode() if stdout else ''
        stderr_str = stderr.decode() if stderr else ''
        
        logger.info(f"[Uninstall] legendary return code: {proc.returncode}")
        if stdout_str:
            logger.info(f"[Uninstall] stdout: {stdout_str[:500]}")
        if stderr_str:
            logger.info(f"[Uninstall] stderr: {stderr_str[:500]}")
        
        # Check for lock failure (legendary returns 0 even when this happens!)
        combined_output = stdout_str + stderr_str
        if 'Failed to acquire installed data lock' in combined_output:
            logger.error("[Epic] Uninstall failed: Lock acquisition failed")
            return {'success': False, 'error': 'errors.lockConflict'}
        
        if proc.returncode != 0:
            logger.error(f"[Epic] Uninstall failed: {stderr_str}")
            return {'success': False, 'error': f"Legendary uninstall failed: {stderr_str}"}
        
        return {'success': True}
    
    async def _delete_prefix(self, game_id: str) -> bool:
        """Delete Wine/Proton prefix for a game.
        
        Args:
            game_id: Store-specific game ID
            
        Returns:
            True if prefix was deleted, False otherwise
        """
        prefix_path = get_prefix_path(game_id)
        if os.path.exists(prefix_path):
            try:
                import shutil
                shutil.rmtree(prefix_path)
                logger.info(f"[Uninstall] Deleted prefix directory: {prefix_path}")
                return True
            except Exception as e:
                logger.warning(f"[Uninstall] Failed to delete prefix {prefix_path}: {e}")
                return False
        else:
            logger.info(f"[Uninstall] No prefix to delete at: {prefix_path}")
            return False
    
    async def _get_epic_executable(self, game_id: str) -> Optional[str]:
        """Get executable path for an Epic game.
        
        Args:
            game_id: Epic game ID
            
        Returns:
            Executable path or None
        """
        return await self.install_handler.get_epic_game_exe(game_id)
