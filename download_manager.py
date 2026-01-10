"""
Download Manager for Unifideck

Manages download queue for Epic and GOG games with:
- Queue persistence across plugin restarts
- Real-time progress tracking via CLI output parsing
- Cancel functionality
- Storage location selection (Internal/SD Card)
"""

import os
import re
import json
import time
import asyncio
import subprocess
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional, Callable
from pathlib import Path
from enum import Enum

import decky

logger = decky.logger


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


class StorageLocation(str, Enum):
    INTERNAL = "internal"
    SDCARD = "sdcard"


# Storage paths
STORAGE_PATHS = {
    StorageLocation.INTERNAL: os.path.expanduser("~/Games"),
    # SD Card path will be resolved dynamically
    StorageLocation.SDCARD: "/run/media/mmcblk0p1/Games" 
}


@dataclass
class DownloadItem:
    """Represents a single download in the queue"""
    id: str                          # Unique download ID (e.g., "epic:game123")
    game_id: str                     # Store-specific game identifier
    game_title: str
    store: str                       # 'epic' or 'gog'
    status: str = DownloadStatus.QUEUED
    progress_percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_mbps: float = 0.0
    eta_seconds: int = 0             # Smoothed ETA for display
    raw_eta_seconds: int = 0         # Raw ETA from CLI output
    eta_samples: int = 0             # Number of ETA samples received (for smoothing)
    is_preparing: bool = True        # True until real progress is received
    error_message: Optional[str] = None
    added_time: float = field(default_factory=time.time)
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    storage_location: str = StorageLocation.INTERNAL
    # GUARDRAIL: Track if game was previously installed before this download started.
    # If True, we NEVER delete the directory on cancel - the game was already complete.
    was_previously_installed: bool = False
    
    # Phase tracking for multi-stage installations (download → extract → verify)
    download_phase: str = "downloading"  # downloading|extracting|verifying|complete
    phase_message: str = ""              # Human-readable phase status message

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DownloadItem':
        return cls(**data)


class DownloadQueue:
    """
    Manages the download queue with persistence.
    
    Inspired by Heroic Games Launcher's downloadqueue.ts pattern:
    - Single active download at a time
    - Queue processed sequentially
    - State persisted to JSON file
    """

    QUEUE_FILE = os.path.expanduser("~/.local/share/unifideck/download_queue.json")
    SETTINGS_FILE = os.path.expanduser("~/.local/share/unifideck/download_settings.json")

    def __init__(self, plugin_dir: str = None):
        self.queue: List[DownloadItem] = []
        self.finished: List[DownloadItem] = []
        self.state: str = "idle"  # 'idle' or 'running'
        self.current_process: Optional[asyncio.subprocess.Process] = None
        self._progress_callback: Optional[Callable] = None
        self._on_complete_callback: Optional[Callable] = None
        self._gog_install_callback: Optional[Callable] = None  # For GOG API-based downloads
        
        # Store plugin directory for finding binaries
        self.plugin_dir = plugin_dir
        
        # Ensure directories exist
        os.makedirs(os.path.dirname(self.QUEUE_FILE), exist_ok=True)
        
        # Load persisted queue
        self._load()
        
        logger.info(f"[DownloadQueue] Initialized with {len(self.queue)} queued items, plugin_dir={plugin_dir}")

    def _load(self) -> None:
        """Load queue from persistent storage"""
        try:
            if os.path.exists(self.QUEUE_FILE):
                with open(self.QUEUE_FILE, 'r') as f:
                    data = json.load(f)
                
                self.queue = [DownloadItem.from_dict(item) for item in data.get('queue', [])]
                self.finished = [DownloadItem.from_dict(item) for item in data.get('finished', [])]
                
                # Reset any "downloading" items to "queued" (from previous crash)
                for item in self.queue:
                    if item.status == DownloadStatus.DOWNLOADING:
                        item.status = DownloadStatus.QUEUED
                        item.progress_percent = 0
                        
                logger.info(f"[DownloadQueue] Loaded {len(self.queue)} queue, {len(self.finished)} finished")
        except Exception as e:
            logger.error(f"[DownloadQueue] Error loading queue: {e}")
            self.queue = []
            self.finished = []

    def _save(self) -> None:
        """Save queue to persistent storage"""
        try:
            data = {
                'queue': [item.to_dict() for item in self.queue],
                'finished': [item.to_dict() for item in self.finished[-20:]]  # Keep last 20
            }
            with open(self.QUEUE_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[DownloadQueue] Error saving queue: {e}")

    def get_default_storage(self) -> str:
        """Get default storage location from settings"""
        try:
            if os.path.exists(self.SETTINGS_FILE):
                with open(self.SETTINGS_FILE, 'r') as f:
                    settings = json.load(f)
                return settings.get('default_storage', StorageLocation.INTERNAL)
        except:
            pass
        return StorageLocation.INTERNAL

    def set_default_storage(self, location: str) -> bool:
        """Set default storage location"""
        try:
            os.makedirs(os.path.dirname(self.SETTINGS_FILE), exist_ok=True)
            settings = {}
            if os.path.exists(self.SETTINGS_FILE):
                with open(self.SETTINGS_FILE, 'r') as f:
                    settings = json.load(f)
            settings['default_storage'] = location
            with open(self.SETTINGS_FILE, 'w') as f:
                json.dump(settings, f)
            logger.info(f"[DownloadQueue] Set default storage to: {location}")
            return True
        except Exception as e:
            logger.error(f"[DownloadQueue] Error setting default storage: {e}")
            return False

    def get_storage_locations(self) -> List[Dict[str, Any]]:
        """Get available storage locations with free space info"""
        locations = []
        
        for loc, path in STORAGE_PATHS.items():
            available = False
            free_space_gb = 0
            
            # Check if path exists or can be created
            if loc == StorageLocation.INTERNAL:
                available = True
                try:
                    statvfs = os.statvfs(os.path.dirname(path))
                    free_space_gb = (statvfs.f_frsize * statvfs.f_bavail) / (1024**3)
                except:
                    pass
            elif loc == StorageLocation.SDCARD:
                # Resolve dynamic SD card path
                sd_root = self._resolve_sd_path()
                if sd_root:
                    available = True
                    # Update path to use actual mount point
                    path = os.path.join(sd_root, "Games")
                    try:
                        statvfs = os.statvfs(sd_root)
                        free_space_gb = (statvfs.f_frsize * statvfs.f_bavail) / (1024**3)
                    except:
                        pass
            
            locations.append({
                'id': loc,
                'label': 'Internal Storage' if loc == StorageLocation.INTERNAL else 'SD Card',
                'path': path,
                'available': available,
                'free_space_gb': round(free_space_gb, 1) if available else 0
            })
        
        return locations

    def get_install_path(self, storage_location: str) -> str:
        """Get the install path for a storage location"""
        if storage_location == StorageLocation.SDCARD:
            sd_root = self._resolve_sd_path()
            if sd_root:
                return os.path.join(sd_root, "Games")
            # Fallback
            return "/run/media/mmcblk0p1/Games"
        return os.path.expanduser("~/Games")

    def _resolve_sd_path(self) -> Optional[str]:
        """Resolve the actual path to the SD card"""
        # 1. Check /proc/mounts for the actual mount point of the SD card device
        try:
            with open('/proc/mounts', 'r') as f:
                for line in f:
                    if '/dev/mmcblk0' in line:
                        parts = line.split()
                        if len(parts) >= 2:
                            mount_point = parts[1]
                            # Verify it's accessible and mounted at /run/media or /sdcard
                            if os.path.isdir(mount_point) and ('/run/media' in mount_point or '/sdcard' in mount_point):
                                return mount_point
        except Exception as e:
            logger.error(f"[DownloadManager] Error reading mounts: {e}")

        # 2. Check common mount points as fallback
        candidates = ["/sdcard", "/run/media/mmcblk0p1"]
        # Also check /run/media/deck/*
        if os.path.isdir("/run/media/deck"):
            try:
                for item in os.listdir("/run/media/deck"):
                    path = os.path.join("/run/media/deck", item)
                    if os.path.isdir(path):
                        candidates.append(path)
            except:
                pass

        for path in candidates:
            if os.path.exists(path) and os.path.isdir(path):
                return path
        return None

    async def add_to_queue(
        self,
        game_id: str,
        game_title: str,
        store: str,
        storage_location: Optional[str] = None,
        was_previously_installed: bool = False
    ) -> Dict[str, Any]:
        """Add a game to the download queue
        
        Args:
            game_id: Store-specific game identifier
            game_title: Display name of the game
            store: 'epic' or 'gog'
            storage_location: Where to install (internal/sdcard)
            was_previously_installed: GUARDRAIL - if True, cancel will NOT delete game files
        """
        download_id = f"{store}:{game_id}"
        
        # Check if already in queue
        for item in self.queue:
            if item.id == download_id:
                logger.warning(f"[DownloadQueue] {download_id} already in queue")
                return {'success': False, 'error': 'Already in queue'}
        
        # Create new download item with installation guardrail
        item = DownloadItem(
            id=download_id,
            game_id=game_id,
            game_title=game_title,
            store=store,
            storage_location=storage_location or self.get_default_storage(),
            was_previously_installed=was_previously_installed
        )
        
        if was_previously_installed:
            logger.info(f"[DownloadQueue] GUARDRAIL: {game_title} marked as previously installed - will NOT delete on cancel")
        
        self.queue.append(item)
        self._save()
        
        logger.info(f"[DownloadQueue] Added {game_title} to queue (position {len(self.queue)})")
        
        # Start processing if idle
        if self.state == "idle":
            asyncio.create_task(self._process_queue())
        
        return {'success': True, 'download_id': download_id, 'position': len(self.queue)}

    def remove_from_queue(self, download_id: str) -> bool:
        """Remove a queued (not active) download"""
        for i, item in enumerate(self.queue):
            if item.id == download_id and item.status != DownloadStatus.DOWNLOADING:
                self.queue.pop(i)
                self._save()
                logger.info(f"[DownloadQueue] Removed {download_id} from queue")
                return True
        return False

    def remove_finished(self, download_id: str) -> bool:
        """Remove a finished download from the finished list"""
        for i, item in enumerate(self.finished):
            if item.id == download_id:
                self.finished.pop(i)
                self._save()
                logger.info(f"[DownloadQueue] Cleared finished download {download_id}")
                return True
        return False

    async def cancel_current(self) -> bool:
        """Cancel the currently downloading item and clean up downloaded files"""
        if not self.queue or self.queue[0].status != DownloadStatus.DOWNLOADING:
            return False
        
        current = self.queue[0]
        logger.info(f"[DownloadQueue] Cancelling {current.game_title}")
        
        # Capture process reference to avoid race condition
        # (process may complete and set self.current_process = None during async operations)
        process = self.current_process
        
        # Kill the process (only works for Epic/legendary downloads)
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Process still alive after timeout, force kill
                if process.returncode is None:
                    try:
                        process.kill()
                    except Exception as e:
                        logger.warning(f"[DownloadQueue] Could not force kill process: {e}")
            except Exception as e:
                logger.error(f"[DownloadQueue] Error terminating process: {e}")
        
        # Clean up downloaded files - ONLY if this was NOT a reinstall of existing game
        # GUARDRAIL: Never delete directories for games that were previously installed
        if current.was_previously_installed:
            logger.info(f"[DownloadQueue] Skipping cleanup - game was previously installed: {current.game_title}")
        else:
            try:
                import shutil
                install_path = self.get_install_path(current.storage_location)
                
                # Sanitize game title to match the folder name created during download
                safe_title = "".join(c for c in current.game_title if c.isalnum() or c in (' ', '-', '_')).strip()
                game_dir = os.path.join(install_path, safe_title)
                
                # ADDITIONAL GUARDRAIL: Only delete if path looks like a game install dir
                # and contains expected partial download indicators
                is_safe_path = (
                    os.path.exists(game_dir) and 
                    os.path.isdir(game_dir) and
                    '/Games/' in game_dir and
                    game_dir not in ['/', '/home/deck', '/home/deck/Games']
                )
                
                # Check if this is actually a partial download (no .unifideck-id marker means unfinished)
                # Completed installs should have a marker file from mark_installed
                marker_file = os.path.join(game_dir, '.unifideck-id')
                is_partial_download = not os.path.exists(marker_file)
                
                if is_safe_path and is_partial_download:
                    logger.info(f"[DownloadQueue] Cleaning up cancelled PARTIAL download: {game_dir}")
                    shutil.rmtree(game_dir, ignore_errors=True)
                    logger.info(f"[DownloadQueue] Deleted partial download directory: {game_dir}")
                elif not is_partial_download:
                    logger.warning(f"[DownloadQueue] NOT deleting - found .unifideck-id marker (completed install): {game_dir}")
                else:
                    logger.warning(f"[DownloadQueue] NOT deleting - failed safety checks: {game_dir}")
            except Exception as e:
                logger.error(f"[DownloadQueue] Error cleaning up cancelled download: {e}")
        
        # Just set status - let _process_queue handle queue management to avoid race condition
        current.status = DownloadStatus.CANCELLED
        current.end_time = time.time()
        self._save()
        
        return True

    def get_current(self) -> Optional[Dict[str, Any]]:
        """Get the current download item"""
        if self.queue and self.queue[0].status == DownloadStatus.DOWNLOADING:
            return self.queue[0].to_dict()
        return None

    def get_queue_info(self) -> Dict[str, Any]:
        """Get full queue information"""
        return {
            'current': self.get_current(),
            'queued': [item.to_dict() for item in self.queue[1:] if item.status == DownloadStatus.QUEUED],
            'finished': [item.to_dict() for item in reversed(self.finished[-10:])],
            'state': self.state
        }

    def is_game_downloading(self, game_id: str, store: str) -> Optional[Dict[str, Any]]:
        """Check if a specific game is currently downloading or queued"""
        download_id = f"{store}:{game_id}"
        for item in self.queue:
            if item.id == download_id:
                return item.to_dict()
        return None

    async def _process_queue(self) -> None:
        """Process downloads in the queue sequentially"""
        self.state = "running"
        logger.info("[DownloadQueue] Starting queue processing")
        
        while self.queue:
            item = self.queue[0]
            
            if item.status == DownloadStatus.CANCELLED:
                # Item was cancelled before download started - move to finished
                self.queue.pop(0)
                self.finished.append(item)
                self._save()
                continue
            
            # Start download
            item.status = DownloadStatus.DOWNLOADING
            item.start_time = time.time()
            self._save()
            
            logger.info(f"[DownloadQueue] Starting download: {item.game_title}")
            
            try:
                success = await self._execute_download(item)
                
                if item.status == DownloadStatus.CANCELLED:
                    # Was cancelled during download - move to finished
                    self.queue.pop(0)
                    self.finished.append(item)
                    self._save()
                    logger.info(f"[DownloadQueue] Cancelled download moved to finished: {item.game_title}")
                    continue
                
                if success:
                    item.status = DownloadStatus.COMPLETED
                    item.progress_percent = 100
                    logger.info(f"[DownloadQueue] Completed: {item.game_title}")
                    
                    # Trigger on-complete callback
                    if self._on_complete_callback:
                        try:
                            await self._on_complete_callback(item)
                        except Exception as e:
                            logger.error(f"[DownloadQueue] On-complete callback error: {e}")
                else:
                    item.status = DownloadStatus.ERROR
                    logger.error(f"[DownloadQueue] Failed: {item.game_title}")
                    
            except Exception as e:
                item.status = DownloadStatus.ERROR
                item.error_message = str(e)
                logger.error(f"[DownloadQueue] Download error: {e}")
            
            item.end_time = time.time()
            
            # Move to finished
            self.queue.pop(0)
            self.finished.append(item)
            self._save()
        
        self.state = "idle"
        logger.info("[DownloadQueue] Queue processing complete")

    async def _execute_download(self, item: DownloadItem) -> bool:
        """Execute the actual download using legendary, gogdl, or nile"""
        install_path = self.get_install_path(item.storage_location)
        os.makedirs(install_path, exist_ok=True)
        
        if item.store == 'epic':
            return await self._download_epic(item, install_path)
        elif item.store == 'gog':
            return await self._download_gog(item, install_path)
        elif item.store == 'amazon':
            return await self._download_amazon(item, install_path)
        else:
            logger.error(f"[DownloadQueue] Unknown store: {item.store}")
            return False

    async def _download_epic(self, item: DownloadItem, install_path: str) -> bool:
        """Download Epic game using legendary"""
        # Use bundled legendary from plugin directory
        legendary_bin = None
        if self.plugin_dir:
            legendary_bin = os.path.join(self.plugin_dir, "bin", "legendary")
            if not os.path.exists(legendary_bin):
                legendary_bin = None
        
        # Fallback to user-installed legendary
        if not legendary_bin:
            legendary_bin = os.path.expanduser("~/.local/bin/legendary")
            if not os.path.exists(legendary_bin):
                item.error_message = "legendary binary not found"
                logger.error(f"[DownloadQueue] legendary not found in plugin_dir or ~/.local/bin")
                return False
        
        # Clean up stale legendary lock files (legendary returns 0 even when blocked by lock)
        lock_dir = os.path.expanduser("~/.config/legendary")
        for lock_file in ['installed.json.lock', 'user.json.lock']:
            lock_path = os.path.join(lock_dir, lock_file)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                    logger.info(f"[DownloadQueue] Cleared stale lock: {lock_file}")
                except Exception as e:
                    logger.warning(f"[DownloadQueue] Could not clear lock {lock_file}: {e}")
        
        cmd = [
            legendary_bin,
            "install",
            item.game_id,
            "--base-path", install_path,
            "-y"  # Non-interactive
        ]
        
        logger.info(f"[DownloadQueue] Running: {' '.join(cmd)}")
        
        try:
            self.current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            
            # Parse progress from output
            await self._parse_legendary_output(item)
            
            return_code = await self.current_process.wait()
            self.current_process = None
            
            return return_code == 0
            
        except Exception as e:
            logger.error(f"[DownloadQueue] Epic download error: {e}")
            item.error_message = str(e)
            return False

    async def _download_gog(self, item: DownloadItem, install_path: str) -> bool:
        """Download GOG game using GOGAPIClient callback
        
        This method delegates to the existing GOGAPIClient.install_game() method
        which handles GOG downloads using direct API calls (not gogdl CLI).
        """
        if not self._gog_install_callback:
            item.error_message = "GOG install callback not set"
            logger.error("[DownloadQueue] GOG install callback not configured")
            return False
        
        logger.info(f"[DownloadQueue] Delegating GOG download to API client: {item.game_id}")
        
        try:
            # Progress callback to update download item
            # Can receive float (percentage) or dict (full stats)
            async def progress_callback(progress: Any):
                # Check if cancelled before processing more progress
                # This allows early exit from long-running downloads
                if item.status == DownloadStatus.CANCELLED:
                    logger.info(f"[DownloadQueue] GOG download cancelled, raising CancelledError")
                    raise asyncio.CancelledError("Download cancelled by user")
                
                if isinstance(progress, dict):
                    # Handle phase updates (for extraction/verification phases)
                    if 'phase' in progress:
                        item.download_phase = progress['phase']
                        item.phase_message = progress.get('phase_message', '')
                        logger.info(f"[DownloadQueue] Phase update: {item.download_phase} - {item.phase_message}")
                        self._save()
                        return  # Phase-only update, no need to process other fields
                    
                    item.progress_percent = progress.get('progress_percent', 0)
                    item.downloaded_bytes = int(progress.get('downloaded_bytes', 0))
                    item.total_bytes = int(progress.get('total_bytes', 0))
                    
                    # Update phase message during download if provided
                    if 'phase_message' in progress:
                        item.phase_message = progress['phase_message']
                    elif item.download_phase == 'downloading' and item.total_bytes > 0:
                        # Auto-generate download phase message
                        mb_down = item.downloaded_bytes / (1024 * 1024)
                        mb_total = item.total_bytes / (1024 * 1024)
                        item.phase_message = f"Downloading: {mb_down:.0f} MB / {mb_total:.0f} MB"
                    
                    # Convert speed from bytes/sec to MB/s
                    speed_bps = progress.get('speed_bps', 0)
                    item.speed_mbps = speed_bps / (1024 * 1024)
                    
                    # Apply ETA smoothing (same logic as Epic for uniformity)
                    raw_eta = int(progress.get('eta_seconds', 0))
                    item.raw_eta_seconds = raw_eta
                    item.eta_samples += 1
                    
                    if item.eta_samples == 1:
                        # First sample: cap at reasonable max
                        item.eta_seconds = min(raw_eta, 7200)  # Cap at 2 hours initially
                    else:
                        # EMA: start with heavy smoothing, increase responsiveness over time
                        alpha = 0.3 if item.eta_samples > 15 else 0.1
                        smoothed = alpha * raw_eta + (1 - alpha) * item.eta_seconds
                        item.eta_seconds = int(smoothed)
                    
                    # Mark as no longer preparing once we have real progress
                    if item.progress_percent > 0 or item.downloaded_bytes > 0:
                        item.is_preparing = False
                    
                    # Save periodically (every 5% or if finished)
                    if int(item.progress_percent) % 5 == 0 or item.progress_percent >= 100:
                        self._save()
                else:
                    # Legacy float support
                    item.progress_percent = float(progress)
                    if progress > 0:
                        item.is_preparing = False
                    if int(progress) % 5 == 0:
                        self._save()
            
            # Call the GOG API client's install method
            result = await self._gog_install_callback(item.game_id, install_path, progress_callback)
            
            if result.get('success'):
                logger.info(f"[DownloadQueue] GOG download completed: {item.game_title}")
                return True
            else:
                item.error_message = result.get('error', 'Unknown GOG download error')
                logger.error(f"[DownloadQueue] GOG download failed: {item.error_message}")
                return False
        
        except asyncio.CancelledError:
            # Download was cancelled via progress callback check
            logger.info(f"[DownloadQueue] GOG download cancelled cleanly: {item.game_title}")
            return False
                
        except Exception as e:
            logger.error(f"[DownloadQueue] GOG download error: {e}")
            item.error_message = str(e)
            return False

    async def _download_amazon(self, item: DownloadItem, install_path: str) -> bool:
        """Download Amazon game using nile CLI"""
        # Use bundled nile from plugin directory
        nile_bin = None
        if self.plugin_dir:
            nile_bin = os.path.join(self.plugin_dir, "bin", "nile")
            if not os.path.exists(nile_bin):
                nile_bin = None
        
        # Fallback to user-installed nile
        if not nile_bin:
            nile_bin = os.path.expanduser("~/.local/bin/nile")
            if not os.path.exists(nile_bin):
                item.error_message = "nile binary not found"
                logger.error(f"[DownloadQueue] nile not found in plugin_dir or ~/.local/bin")
                return False
        
        cmd = [
            nile_bin,
            "install",
            item.game_id,
            "--base-path", install_path
        ]
        
        logger.info(f"[DownloadQueue] Running: {' '.join(cmd)}")
        
        try:
            self.current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            
            # Parse progress from output
            await self._parse_nile_output(item)
            
            return_code = await self.current_process.wait()
            self.current_process = None
            
            return return_code == 0
            
        except Exception as e:
            logger.error(f"[DownloadQueue] Amazon download error: {e}")
            item.error_message = str(e)
            return False

    async def _parse_nile_output(self, item: DownloadItem) -> None:
        """Parse nile output for progress updates"""
        # Nile download progress format (from ProgressBar):
        # INFO [PROGRESS]:  = Progress: 25.06 137141589/442097801, Running for: 00:00:10, ETA: 00:00:29
        # INFO [PROGRESS]:  = Downloaded: 130.79 MiB, Written: 130.79 MiB
        # INFO [PROGRESS]:   + Download    - 25.14 MiB/s
        
        progress_re = re.compile(
            r'= Progress:\s+(\d+\.?\d*)\s+(\d+)/(\d+),.*ETA:\s+(\d+):(\d+):(\d+)'
        )
        downloaded_re = re.compile(r'= Downloaded:\s+(\d+\.?\d*)\s+MiB')
        speed_re = re.compile(r'\+ Download\s+-\s+(\d+\.?\d*)\s+MiB/s')
        
        # Installation/verification phase (simpler format)
        install_re = re.compile(r'\[Installation\]\s*\[(\d+)%\]')
        
        buffer = ""
        
        while self.current_process and self.current_process.returncode is None:
            try:
                chunk = await asyncio.wait_for(
                    self.current_process.stdout.read(4096),
                    timeout=1.0
                )
                if not chunk:
                    break
                    
                buffer += chunk.decode('utf-8', errors='ignore')
                lines = buffer.split('\n')
                buffer = lines[-1]  # Keep incomplete line
                
                for line in lines[:-1]:
                    logger.debug(f"[Nile] {line}")
                    
                    # Parse rich download progress (from PROGRESS logger)
                    if match := progress_re.search(line):
                        item.progress_percent = float(match.group(1))
                        item.downloaded_bytes = int(match.group(2))
                        item.total_bytes = int(match.group(3))
                        
                        # Parse ETA (HH:MM:SS format)
                        hours = int(match.group(4))
                        minutes = int(match.group(5))
                        seconds = int(match.group(6))
                        item.eta_seconds = hours * 3600 + minutes * 60 + seconds
                        item.is_preparing = False
                    
                    # Parse download speed
                    elif match := speed_re.search(line):
                        item.speed_mbps = float(match.group(1))
                    
                    # Parse installation phase (simpler format - after download)
                    elif match := install_re.search(line):
                        item.progress_percent = float(match.group(1))
                        item.is_preparing = False
                        logger.info(f"[DownloadQueue] Amazon game {item.game_id} installation at {item.progress_percent}%")
                    
                    # Check for verification
                    elif '[Verification]' in line:
                        item.is_preparing = False
                        logger.info(f"[DownloadQueue] Amazon game {item.game_id} verification in progress")
                    
                    # Save progress periodically
                    if int(item.progress_percent) % 5 == 0:
                        self._save()
                            
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.warning(f"[DownloadQueue] Error parsing nile output: {e}")
                break

    def set_gog_install_callback(self, callback: Callable) -> None:
        """Set callback for GOG game installation (uses GOGAPIClient)"""
        self._gog_install_callback = callback

    async def _parse_legendary_output(self, item: DownloadItem) -> None:
        """Parse legendary output for progress updates with ETA smoothing"""
        # Regex patterns from Junkstore's epic.py
        progress_re = re.compile(
            r"\[DLManager\] INFO: = Progress: (\d+\.?\d*)%.*ETA: (\d+:\d+:\d+)"
        )
        downloaded_re = re.compile(
            r"Downloaded: (\d+\.?\d*) MiB"
        )
        speed_re = re.compile(
            r"\+ Download\s*-\s*(\d+\.?\d*) MiB/s"
        )
        total_size_re = re.compile(
            r"Download size: (\d+\.?\d*) (MiB|GiB)"
        )
        
        buffer = ""
        
        while self.current_process and self.current_process.returncode is None:
            try:
                chunk = await asyncio.wait_for(
                    self.current_process.stdout.read(4096),
                    timeout=1.0
                )
                if not chunk:
                    break
                    
                buffer += chunk.decode('utf-8', errors='ignore')
                lines = buffer.split('\n')
                buffer = lines[-1]  # Keep incomplete line
                
                for line in lines[:-1]:
                    # Parse progress
                    if match := progress_re.search(line):
                        item.progress_percent = float(match.group(1))
                        eta_parts = match.group(2).split(':')
                        raw_eta = int(eta_parts[0]) * 3600 + int(eta_parts[1]) * 60 + int(eta_parts[2])
                        item.raw_eta_seconds = raw_eta
                        
                        # Apply Exponential Moving Average (EMA) smoothing
                        # Use lower alpha (more smoothing) in early samples to dampen wild initial ETAs
                        # Gradually increase alpha (faster response) as we get more samples
                        item.eta_samples += 1
                        if item.eta_samples == 1:
                            # First sample: just use it (but cap at reasonable max)
                            item.eta_seconds = min(raw_eta, 7200)  # Cap at 2 hours initially
                        else:
                            # EMA: higher alpha = faster response, lower = more smoothing
                            # Start with alpha=0.1 (heavy smoothing), increase to 0.3 after stabilization
                            alpha = 0.3 if item.eta_samples > 15 else 0.1
                            smoothed = alpha * raw_eta + (1 - alpha) * item.eta_seconds
                            item.eta_seconds = int(smoothed)
                        
                        # Mark as no longer preparing once we have real progress
                        if item.progress_percent > 0:
                            item.is_preparing = False
                    
                    # Parse downloaded bytes
                    if match := downloaded_re.search(line):
                        item.downloaded_bytes = int(float(match.group(1)) * 1024 * 1024)
                        # Also mark as no longer preparing
                        if item.downloaded_bytes > 0:
                            item.is_preparing = False
                    
                    # Parse speed
                    if match := speed_re.search(line):
                        item.speed_mbps = float(match.group(1))
                    
                    # Parse total size
                    if match := total_size_re.search(line):
                        size = float(match.group(1))
                        if match.group(2) == 'GiB':
                            size *= 1024
                        item.total_bytes = int(size * 1024 * 1024)
                    
                    # Save progress periodically
                    self._save()
                    
            except asyncio.TimeoutError:
                continue

    async def _parse_gogdl_output(self, item: DownloadItem) -> None:
        """Parse gogdl output for progress updates"""
        progress_re = re.compile(
            r"Progress:\s*(\d+\.?\d*)%"
        )
        speed_re = re.compile(
            r"(\d+\.?\d*)\s*MB/s"
        )
        
        buffer = ""
        
        while self.current_process and self.current_process.returncode is None:
            try:
                chunk = await asyncio.wait_for(
                    self.current_process.stdout.read(4096),
                    timeout=1.0
                )
                if not chunk:
                    break
                    
                buffer += chunk.decode('utf-8', errors='ignore')
                lines = buffer.split('\n')
                buffer = lines[-1]
                
                for line in lines[:-1]:
                    if match := progress_re.search(line):
                        item.progress_percent = float(match.group(1))
                    
                    if match := speed_re.search(line):
                        item.speed_mbps = float(match.group(1))
                    
                    self._save()
                    
            except asyncio.TimeoutError:
                continue

    def set_on_complete_callback(self, callback: Callable) -> None:
        """Set callback to be called when a download completes"""
        self._on_complete_callback = callback


# Global instance
_download_queue: Optional[DownloadQueue] = None


def get_download_queue(plugin_dir: str = None) -> DownloadQueue:
    """Get or create the global download queue instance"""
    global _download_queue
    if _download_queue is None:
        _download_queue = DownloadQueue(plugin_dir)
    return _download_queue
