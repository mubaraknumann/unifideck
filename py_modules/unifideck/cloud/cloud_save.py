"""
Cloud Save Manager for Unifideck

Handles cloud save synchronization for Epic and GOG games.

Features:
- Epic Games: Uses legendary sync-saves
- GOG: Uses heroic-gogdl save-sync with token conversion
- Auto-sync on game launch (download) and exit (upload)
- Game process monitoring for auto-push on exit
- Conflict detection with timestamps
- Background periodic sync
- Detailed logging at every step
"""

import os
import json
import asyncio
import logging
import time
from typing import Optional, Dict, Any, List, Callable
from pathlib import Path

# Use Decky's logger if available, otherwise standard logging
try:
    import decky
    logger = decky.logger
except ImportError:
    logger = logging.getLogger(__name__)


# Sync state file for persisting timestamps
SYNC_STATE_FILE = os.path.expanduser("~/.config/unifideck/cloud_sync_state.json")


class GameProcessMonitor:
    """
    Monitors game processes and triggers cloud save upload when they exit.
    
    Steam-like behavior: automatically push saves when game closes.
    """
    
    def __init__(self, cloud_save_manager: 'CloudSaveManager'):
        self.csm = cloud_save_manager
        self.monitored_games: Dict[int, Dict[str, Any]] = {}  # {pid: game_info}
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        self._poll_interval = 5  # Check every 5 seconds
    
    async def start_monitoring(self, pid: int, store: str, game_id: str, 
                                game_name: str = "", save_path: str = "") -> bool:
        """
        Start monitoring a game process for exit.
        
        Args:
            pid: Process ID of the game
            store: "epic" or "gog"
            game_id: Game identifier
            game_name: Display name for logging
            save_path: Local save path (required for GOG)
        
        Returns:
            True if monitoring started successfully
        """
        if not self._is_process_running(pid):
            logger.warning(f"[CloudSave/Monitor] Process {pid} not found, cannot monitor")
            return False
        
        self.monitored_games[pid] = {
            "store": store,
            "game_id": game_id,
            "game_name": game_name or game_id,
            "save_path": save_path,
            "start_time": time.time()
        }
        
        logger.info(f"[CloudSave/Monitor] Started monitoring PID {pid} for {game_name} ({store}/{game_id})")
        
        # Start monitor loop if not running
        if not self._running:
            self._running = True
            self._monitor_task = asyncio.create_task(self._monitor_loop())
        
        return True
    
    def stop_monitoring(self, pid: int) -> None:
        """Stop monitoring a specific process"""
        if pid in self.monitored_games:
            game_info = self.monitored_games.pop(pid)
            logger.info(f"[CloudSave/Monitor] Stopped monitoring PID {pid} ({game_info['game_name']})")
    
    def stop_all(self) -> None:
        """Stop all monitoring"""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
        self.monitored_games.clear()
        logger.info("[CloudSave/Monitor] Stopped all monitoring")
    
    def _is_process_running(self, pid: int) -> bool:
        """Check if a process is still running"""
        try:
            os.kill(pid, 0)  # Signal 0 = check existence
            return True
        except OSError:
            return False
    
    async def _monitor_loop(self) -> None:
        """Main monitoring loop - checks for exited processes"""
        logger.info("[CloudSave/Monitor] Monitor loop started")
        
        while self._running and self.monitored_games:
            try:
                exited_pids = []
                
                for pid, game_info in list(self.monitored_games.items()):
                    if not self._is_process_running(pid):
                        exited_pids.append(pid)
                        logger.info(f"[CloudSave/Monitor] Game exited: {game_info['game_name']} (PID {pid})")
                
                # Handle exited games
                for pid in exited_pids:
                    game_info = self.monitored_games.pop(pid)
                    await self._on_process_exit(game_info)
                
                await asyncio.sleep(self._poll_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CloudSave/Monitor] Error in monitor loop: {e}", exc_info=True)
                await asyncio.sleep(self._poll_interval)
        
        self._running = False
        logger.info("[CloudSave/Monitor] Monitor loop stopped")
    
    async def _on_process_exit(self, game_info: Dict[str, Any]) -> None:
        """Called when a monitored game process exits - triggers save upload"""
        store = game_info["store"]
        game_id = game_info["game_id"]
        game_name = game_info["game_name"]
        save_path = game_info["save_path"]
        
        logger.info(f"[CloudSave/Monitor] Uploading saves for {game_name} ({store}/{game_id})")
        
        # Trigger upload
        result = await self.csm.on_game_exit(store, game_id, game_name, save_path)
        
        if result.get("success"):
            logger.info(f"[CloudSave/Monitor] Successfully uploaded saves for {game_name}")
        else:
            logger.error(f"[CloudSave/Monitor] Failed to upload saves for {game_name}: {result.get('error')}")


class BackgroundCloudSyncService:
    """
    Periodically syncs cloud saves for installed games in the background.
    
    Runs every SYNC_INTERVAL seconds to check for cloud updates.
    """
    
    SYNC_INTERVAL = 300  # 5 minutes
    
    def __init__(self, cloud_save_manager: 'CloudSaveManager', 
                 get_installed_games: Optional[Callable] = None):
        """
        Args:
            cloud_save_manager: The CloudSaveManager instance
            get_installed_games: Callback that returns list of installed games
                                 Each game: {store, game_id, game_name, save_path?}
        """
        self.csm = cloud_save_manager
        self.get_installed_games = get_installed_games
        self._sync_task: Optional[asyncio.Task] = None
        self._running = False
    
    def start(self) -> None:
        """Start background sync service"""
        if self._running:
            logger.info("[CloudSave/BgSync] Already running")
            return
        
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info(f"[CloudSave/BgSync] Started (interval: {self.SYNC_INTERVAL}s)")
    
    def stop(self) -> None:
        """Stop background sync service"""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None
        logger.info("[CloudSave/BgSync] Stopped")
    
    async def _sync_loop(self) -> None:
        """Main sync loop"""
        while self._running:
            try:
                # Wait first, then sync (so we don't sync immediately on start)
                await asyncio.sleep(self.SYNC_INTERVAL)
                
                if not self._running:
                    break
                
                await self.sync_all_installed()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CloudSave/BgSync] Error in sync loop: {e}", exc_info=True)
    
    async def sync_all_installed(self) -> Dict[str, Any]:
        """Sync all installed games"""
        if not self.get_installed_games:
            logger.warning("[CloudSave/BgSync] No get_installed_games callback set")
            return {"success": False, "error": "No game list callback"}
        
        games = self.get_installed_games()
        if not games:
            logger.info("[CloudSave/BgSync] No installed games to sync")
            return {"success": True, "synced": 0}
        
        logger.info(f"[CloudSave/BgSync] Syncing {len(games)} installed games")
        
        synced = 0
        errors = []
        
        for game in games:
            try:
                store = game.get("store")
                game_id = game.get("game_id")
                game_name = game.get("game_name", game_id)
                save_path = game.get("save_path", "")
                
                # Download latest cloud saves
                if store == "epic":
                    result = await self.csm.sync_epic(game_id, direction="download", game_name=game_name)
                elif store == "gog" and save_path:
                    result = await self.csm.sync_gog(game_id, save_path, direction="download", game_name=game_name)
                else:
                    continue
                
                if result.get("success"):
                    synced += 1
                else:
                    errors.append(f"{game_name}: {result.get('error')}")
                    
            except Exception as e:
                errors.append(f"{game.get('game_name', 'Unknown')}: {e}")
        
        logger.info(f"[CloudSave/BgSync] Completed: {synced}/{len(games)} synced, {len(errors)} errors")
        
        return {
            "success": len(errors) == 0,
            "synced": synced,
            "total": len(games),
            "errors": errors
        }


class CloudSaveManager:
    """
    Manages cloud save synchronization for Epic and GOG games.
    
    Features:
    - Epic Games: Uses legendary sync-saves
    - GOG: Uses heroic-gogdl save-sync with token conversion
    - Auto-sync on game launch (download) and exit (upload)
    - Process monitoring for auto-push
    - Conflict detection
    - Background periodic sync
    - Detailed logging at every step
    """
    
    # GOG Galaxy client ID for token format
    GOG_CLIENT_ID = "46899977096215655"
    
    def __init__(self, plugin_dir: Optional[str] = None):
        self.plugin_dir = plugin_dir or os.environ.get("DECKY_PLUGIN_DIR", "")
        self.legendary_bin = self._find_legendary()
        self.gogdl_bin = self._find_gogdl()
        self.gogdl_auth_file = os.path.expanduser("~/.config/unifideck/gogdl_auth.json")
        self.unifideck_gog_token = os.path.expanduser("~/.config/unifideck/gog_token.json")
        self.sync_status: Dict[str, Dict[str, Any]] = {}  # {store:game_id: {last_sync, status, error}}
        self.sync_state: Dict[str, Dict[str, Any]] = {}  # Persisted timestamps
        
        # Sub-components
        self.process_monitor = GameProcessMonitor(self)
        self.background_sync: Optional[BackgroundCloudSyncService] = None
        
        # Pending conflicts for frontend
        self.pending_conflicts: Dict[str, Dict[str, Any]] = {}  # {store:game_id: conflict_info}
        
        # Load persisted sync state
        self._load_sync_state()
        
        logger.info(f"[CloudSave] Initialized - legendary: {self.legendary_bin}, gogdl: {self.gogdl_bin}")
    
    def _load_sync_state(self) -> None:
        """Load sync state from disk"""
        try:
            if os.path.exists(SYNC_STATE_FILE):
                with open(SYNC_STATE_FILE, 'r') as f:
                    self.sync_state = json.load(f)
                logger.info(f"[CloudSave] Loaded sync state: {len(self.sync_state)} games")
        except Exception as e:
            logger.error(f"[CloudSave] Failed to load sync state: {e}")
            self.sync_state = {}
    
    def _save_sync_state(self) -> None:
        """Save sync state to disk"""
        try:
            os.makedirs(os.path.dirname(SYNC_STATE_FILE), exist_ok=True)
            with open(SYNC_STATE_FILE, 'w') as f:
                json.dump(self.sync_state, f, indent=2)
            logger.debug("[CloudSave] Saved sync state")
        except Exception as e:
            logger.error(f"[CloudSave] Failed to save sync state: {e}")
    
    def _update_sync_timestamp(self, store: str, game_id: str, cloud_time: float = 0) -> None:
        """Update the last sync timestamp for a game"""
        key = f"{store}:{game_id}"
        self.sync_state[key] = {
            "last_local_sync": time.time(),
            "last_cloud_timestamp": cloud_time or time.time()
        }
        self._save_sync_state()
    
    def start_background_sync(self, get_installed_games: Callable) -> None:
        """Start the background sync service with a callback to get installed games"""
        self.background_sync = BackgroundCloudSyncService(self, get_installed_games)
        self.background_sync.start()
    
    def stop_background_sync(self) -> None:
        """Stop background sync service"""
        if self.background_sync:
            self.background_sync.stop()
            self.background_sync = None
    
    def _find_legendary(self) -> Optional[str]:
        """Find legendary binary (bundled or user-installed)"""
        paths = [
            os.path.join(self.plugin_dir, "bin", "legendary") if self.plugin_dir else None,
            os.path.expanduser("~/.local/bin/legendary"),
            "/usr/bin/legendary",
        ]
        for path in paths:
            if path and os.path.exists(path):
                logger.info(f"[CloudSave] Found legendary at: {path}")
                return path
        logger.warning("[CloudSave] legendary binary not found")
        return None
    
    def _find_gogdl(self) -> Optional[str]:
        """Find gogdl binary (bundled, Heroic flatpak, or user-installed)"""
        paths = [
            os.path.join(self.plugin_dir, "bin", "gogdl") if self.plugin_dir else None,
            "/var/lib/flatpak/app/com.heroicgameslauncher.hgl/x86_64/stable/active/files/bin/heroic/resources/app.asar.unpacked/build/bin/x64/linux/gogdl",
            os.path.expanduser("~/.local/bin/gogdl"),
        ]
        for path in paths:
            if path and os.path.exists(path):
                logger.info(f"[CloudSave] Found gogdl at: {path}")
                return path
        logger.warning("[CloudSave] gogdl binary not found")
        return None
    
    def _convert_gog_token_for_gogdl(self) -> bool:
        """
        Convert Unifideck's GOG token format to heroic-gogdl format.
        
        Unifideck: {access_token, refresh_token}
        gogdl: {client_id: {access_token, refresh_token, expires_in, ...}}
        """
        try:
            if not os.path.exists(self.unifideck_gog_token):
                logger.warning("[CloudSave] GOG token not found, cannot convert for gogdl")
                return False
            
            with open(self.unifideck_gog_token, 'r') as f:
                unifideck_token = json.load(f)
            
            # Build gogdl-compatible format
            gogdl_auth = {
                self.GOG_CLIENT_ID: {
                    "access_token": unifideck_token.get("access_token"),
                    "expires_in": 3600,
                    "token_type": "bearer",
                    "scope": "",
                    "refresh_token": unifideck_token.get("refresh_token"),
                    "user_id": "",
                    "session_id": "",
                    "loginTime": time.time()
                }
            }
            
            os.makedirs(os.path.dirname(self.gogdl_auth_file), exist_ok=True)
            with open(self.gogdl_auth_file, 'w') as f:
                json.dump(gogdl_auth, f)
            
            logger.info(f"[CloudSave] Converted GOG token for gogdl: {self.gogdl_auth_file}")
            return True
            
        except Exception as e:
            logger.error(f"[CloudSave] Failed to convert GOG token: {e}")
            return False
    
    async def check_for_conflicts(self, store: str, game_id: str, 
                                   local_save_path: str = "") -> Optional[Dict[str, Any]]:
        """
        Check if local saves conflict with cloud saves.
        
        Returns None if no conflict, otherwise returns conflict info.
        Fresh saves (no cloud saves) do not count as conflicts.
        
        Returns:
            {
                'has_conflict': bool,
                'local_timestamp': float,
                'cloud_timestamp': float,
                'local_newer': bool,
                'is_fresh': bool  # True if no cloud saves exist
            }
        """
        key = f"{store}:{game_id}"
        
        # Get cloud timestamp
        cloud_info = await self._get_cloud_info(store, game_id)
        cloud_timestamp = cloud_info.get("timestamp", 0)
        has_cloud_saves = cloud_info.get("has_saves", False)
        
        # If no cloud saves, this is a fresh save - no conflict
        if not has_cloud_saves:
            logger.info(f"[CloudSave] No cloud saves for {key}, fresh save - no conflict")
            return {
                "has_conflict": False,
                "is_fresh": True,
                "local_timestamp": time.time(),
                "cloud_timestamp": 0,
                "local_newer": True
            }
        
        # Get local timestamp (from our sync state or file mtime)
        local_timestamp = time.time()
        if key in self.sync_state:
            local_timestamp = self.sync_state[key].get("last_local_sync", time.time())
        elif local_save_path and os.path.exists(local_save_path):
            local_timestamp = os.path.getmtime(local_save_path)
        
        # Check for conflict
        # Conflict if cloud is newer than our last sync
        last_cloud_we_knew = self.sync_state.get(key, {}).get("last_cloud_timestamp", 0)
        has_conflict = cloud_timestamp > last_cloud_we_knew and local_timestamp > last_cloud_we_knew
        
        if has_conflict:
            conflict_info = {
                "has_conflict": True,
                "is_fresh": False,
                "local_timestamp": local_timestamp,
                "cloud_timestamp": cloud_timestamp,
                "local_newer": local_timestamp > cloud_timestamp
            }
            self.pending_conflicts[key] = conflict_info
            logger.warning(f"[CloudSave] Conflict detected for {key}: local={local_timestamp}, cloud={cloud_timestamp}")
            return conflict_info
        
        return None
    
    async def _get_cloud_info(self, store: str, game_id: str) -> Dict[str, Any]:
        """
        Get cloud save info (timestamp, whether saves exist).
        
        For Epic: Parse legendary output
        For GOG: Check gogdl output
        """
        if store == "epic":
            return await self._get_epic_cloud_info(game_id)
        elif store == "gog":
            return await self._get_gog_cloud_info(game_id)
        return {"has_saves": False, "timestamp": 0}
    
    async def _get_epic_cloud_info(self, app_id: str) -> Dict[str, Any]:
        """Get Epic cloud save info via legendary"""
        if not self.legendary_bin:
            return {"has_saves": False, "timestamp": 0}
        
        try:
            cmd = [self.legendary_bin, "sync-saves", app_id, "--skip-upload", "--skip-download"]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            output = (stdout.decode() if stdout else "") + (stderr.decode() if stderr else "")
            
            # Parse output for save info
            has_saves = "remote save" in output.lower() and "0 remote" not in output.lower()
            
            return {
                "has_saves": has_saves,
                "timestamp": time.time() if has_saves else 0
            }
        except Exception as e:
            logger.error(f"[CloudSave] Error getting Epic cloud info: {e}")
            return {"has_saves": False, "timestamp": 0}
    
    async def _get_gog_cloud_info(self, game_id: str) -> Dict[str, Any]:
        """Get GOG cloud save info via gogdl"""
        # For GOG, we can't easily check without doing a sync
        # Return basic info based on sync state
        key = f"gog:{game_id}"
        if key in self.sync_state:
            return {
                "has_saves": True,
                "timestamp": self.sync_state[key].get("last_cloud_timestamp", 0)
            }
        return {"has_saves": False, "timestamp": 0}
    
    def resolve_conflict(self, store: str, game_id: str, use_cloud: bool) -> Dict[str, Any]:
        """
        Resolve a pending conflict.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
            use_cloud: True to use cloud saves, False to use/upload local
        
        Returns:
            {action: "download" or "upload"}
        """
        key = f"{store}:{game_id}"
        
        if key in self.pending_conflicts:
            del self.pending_conflicts[key]
        
        action = "download" if use_cloud else "upload"
        logger.info(f"[CloudSave] Resolved conflict for {key}: use_cloud={use_cloud}, action={action}")
        
        return {"action": action}
    
    def get_pending_conflicts(self) -> Dict[str, Dict[str, Any]]:
        """Get all pending conflicts"""
        return self.pending_conflicts.copy()
    
    async def sync_epic(self, app_id: str, direction: str = "download", game_name: str = "") -> Dict[str, Any]:
        """
        Sync cloud saves for an Epic game using legendary.
        
        Args:
            app_id: Epic game app ID
            direction: "download" (pull from cloud) or "upload" (push to cloud)
            game_name: Optional game name for logging
        
        Returns:
            {success, message, files_synced, duration}
        """
        start_time = time.time()
        
        display_name = game_name or app_id
        logger.info(f"[CloudSave] Starting Epic sync for {display_name} ({app_id}) - direction: {direction}")
        
        if not self.legendary_bin:
            error = "legendary binary not found"
            logger.error(f"[CloudSave] ERROR: {error}")
            return {"success": False, "error": error}
        
        try:
            # Build command
            cmd = [self.legendary_bin, "sync-saves", app_id, "-y"]
            if direction == "download":
                cmd.append("--skip-upload")
            elif direction == "upload":
                cmd.append("--skip-download")
            
            logger.info(f"[CloudSave] Command: {' '.join(cmd)}")
            
            # Execute
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            duration = time.time() - start_time
            stdout_str = stdout.decode() if stdout else ""
            stderr_str = stderr.decode() if stderr else ""
            
            # Log output
            if stdout_str:
                logger.info(f"[CloudSave] stdout: {stdout_str[:500]}")
            if stderr_str:
                logger.info(f"[CloudSave] stderr: {stderr_str[:500]}")
            
            success = process.returncode == 0
            
            # Update status and timestamps
            self._update_sync_timestamp("epic", app_id)
            
            status_key = f"epic:{app_id}"
            self.sync_status[status_key] = {
                "last_sync": time.time(),
                "status": "synced" if success else "error",
                "direction": direction,
                "error": stderr_str if not success else None
            }
            
            if success:
                logger.info(f"[CloudSave] Sync completed for {display_name} - duration: {duration:.2f}s")
            else:
                logger.error(f"[CloudSave] Sync failed for {display_name}: return code {process.returncode}")
            
            return {
                "success": success,
                "message": stdout_str or stderr_str,
                "duration": duration,
                "return_code": process.returncode
            }
            
        except Exception as e:
            logger.error(f"[CloudSave] Exception during Epic sync: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def sync_gog(self, game_id: str, save_path: str, direction: str = "download", game_name: str = "") -> Dict[str, Any]:
        """
        Sync cloud saves for a GOG game using gogdl.
        
        Args:
            game_id: GOG game ID (client_id)
            save_path: Local path where saves are stored
            direction: "download" or "upload"
            game_name: Optional game name for logging
        
        Returns:
            {success, message, duration}
        """
        start_time = time.time()
        
        display_name = game_name or game_id
        logger.info(f"[CloudSave] Starting GOG sync for {display_name} ({game_id}) - direction: {direction}")
        
        if not self.gogdl_bin:
            error = "gogdl binary not found"
            logger.error(f"[CloudSave] ERROR: {error}")
            return {"success": False, "error": error}
        
        # Ensure token is converted
        if not self._convert_gog_token_for_gogdl():
            error = "Failed to convert GOG token for gogdl"
            logger.error(f"[CloudSave] ERROR: {error}")
            return {"success": False, "error": error}
        
        try:
            # Build command
            cmd = [
                self.gogdl_bin,
                "--auth-config-path", self.gogdl_auth_file,
                "save-sync",
                save_path,
                game_id,
                "--os", "windows",
                "--ts", "0",
            ]
            
            if direction == "download":
                cmd.append("--skip-upload")
            elif direction == "upload":
                cmd.append("--skip-download")
            
            logger.info(f"[CloudSave] Command: {' '.join(cmd)}")
            
            # Execute
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            duration = time.time() - start_time
            stdout_str = stdout.decode() if stdout else ""
            stderr_str = stderr.decode() if stderr else ""
            
            # Log output
            if stdout_str:
                logger.info(f"[CloudSave] stdout: {stdout_str[:500]}")
            if stderr_str:
                logger.info(f"[CloudSave] stderr: {stderr_str[:500]}")
            
            success = process.returncode == 0
            
            # Update status and timestamps
            self._update_sync_timestamp("gog", game_id)
            
            status_key = f"gog:{game_id}"
            self.sync_status[status_key] = {
                "last_sync": time.time(),
                "status": "synced" if success else "error",
                "direction": direction,
                "error": stderr_str if not success else None
            }
            
            if success:
                logger.info(f"[CloudSave] Sync completed for {display_name} - duration: {duration:.2f}s")
            else:
                logger.error(f"[CloudSave] Sync failed for {display_name}: return code {process.returncode}")
            
            return {
                "success": success,
                "message": stdout_str or stderr_str,
                "duration": duration,
                "return_code": process.returncode
            }
            
        except Exception as e:
            logger.error(f"[CloudSave] Exception during GOG sync: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def on_game_launch(self, store: str, game_id: str, game_name: str = "", 
                             save_path: str = "", pid: int = 0) -> Dict[str, Any]:
        """
        Called before a game launches. Downloads cloud saves and starts monitoring.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
            game_name: Game title for logging
            save_path: For GOG, the local save path
            pid: Process ID to monitor for auto-push on exit
        """
        logger.info(f"[CloudSave] on_game_launch triggered: {store}/{game_id}, pid={pid}")
        
        # Check for conflicts first
        conflict = await self.check_for_conflicts(store, game_id, save_path)
        if conflict and conflict.get("has_conflict"):
            logger.info(f"[CloudSave] Conflict detected, deferring to frontend for resolution")
            return {
                "success": False,
                "conflict": conflict,
                "needs_resolution": True
            }
        
        # Sync (download) cloud saves
        if store == "epic":
            result = await self.sync_epic(game_id, direction="download", game_name=game_name)
        elif store == "gog":
            if not save_path:
                logger.warning(f"[CloudSave] No save_path for GOG game {game_id}, skipping sync")
                result = {"success": False, "error": "No save path configured"}
            else:
                result = await self.sync_gog(game_id, save_path, direction="download", game_name=game_name)
        else:
            logger.warning(f"[CloudSave] Unknown store: {store}")
            return {"success": False, "error": f"Unknown store: {store}"}
        
        # Start process monitoring for auto-push on exit
        if pid > 0:
            await self.process_monitor.start_monitoring(pid, store, game_id, game_name, save_path)
        
        return result
    
    async def on_game_exit(self, store: str, game_id: str, game_name: str = "", save_path: str = "") -> Dict[str, Any]:
        """
        Called after a game exits. Uploads cloud saves.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
            game_name: Game title for logging
            save_path: For GOG, the local save path
        """
        logger.info(f"[CloudSave] on_game_exit triggered: {store}/{game_id}")
        
        if store == "epic":
            return await self.sync_epic(game_id, direction="upload", game_name=game_name)
        elif store == "gog":
            if not save_path:
                logger.warning(f"[CloudSave] No save_path for GOG game {game_id}, skipping sync")
                return {"success": False, "error": "No save path configured"}
            return await self.sync_gog(game_id, save_path, direction="upload", game_name=game_name)
        else:
            logger.warning(f"[CloudSave] Unknown store: {store}")
            return {"success": False, "error": f"Unknown store: {store}"}
    
    def get_sync_status(self, store: str, game_id: str) -> Optional[Dict[str, Any]]:
        """Get the last sync status for a game"""
        status_key = f"{store}:{game_id}"
        return self.sync_status.get(status_key)
