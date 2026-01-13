"""
Epic Games Store connector using legendary CLI.

This module handles all Epic Games Store operations including authentication,
library fetching, and game installation via the legendary CLI tool.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import time
from typing import Dict, Any, List, Optional

from .base import Store, Game

logger = logging.getLogger(__name__)

# Global caches for legendary CLI results (performance optimization)
_legendary_installed_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 30  # 30 second cache
}

_legendary_info_cache = {}  # Per-game info cache


class EpicConnector(Store):
    """Handles Epic Games Store via legendary CLI"""

    def __init__(self, plugin_dir: Optional[str] = None, plugin_instance=None):
        self.plugin_dir = plugin_dir
        self.plugin_instance = plugin_instance  # Reference to parent Plugin for auto-sync
        self.legendary_bin = self._find_legendary()
        logger.info(f"Legendary binary: {self.legendary_bin}")
    
    @property
    def store_name(self) -> str:
        return 'epic'

    def _find_legendary(self) -> Optional[str]:
        """Find legendary executable - checks bundled binary first, then system"""
        # Priority 1: Check bundled legendary in plugin bin/ directory
        if self.plugin_dir:
            bundled_legendary = os.path.join(self.plugin_dir, 'bin', 'legendary')
            if os.path.isfile(bundled_legendary) and os.access(bundled_legendary, os.X_OK):
                logger.info(f"[EPIC] Using bundled legendary: {bundled_legendary}")
                return bundled_legendary

        # Priority 2: Check system PATH
        legendary_path = shutil.which("legendary")
        if legendary_path:
            logger.info(f"[EPIC] Using system legendary: {legendary_path}")
            return legendary_path

        # Priority 3: Check ~/.local/bin explicitly
        local_bin_legendary = os.path.expanduser("~/.local/bin/legendary")
        if os.path.exists(local_bin_legendary):
            logger.info(f"[EPIC] Using user legendary: {local_bin_legendary}")
            return local_bin_legendary

        logger.warning("[EPIC] Legendary not found - Epic features unavailable")
        logger.info("[EPIC] Install with: pip install --user legendary-gl")
        return None

    async def is_available(self) -> bool:
        """Check if legendary is installed and authenticated"""
        logger.info(f"[EPIC] Checking availability, legendary_bin={self.legendary_bin}")

        if not self.legendary_bin:
            logger.warning("[EPIC] Legendary CLI not found - not installed")
            return False

        try:
            # Check for user.json which contains Epic auth tokens
            legendary_config = os.path.expanduser("~/.config/legendary/user.json")

            if not os.path.exists(legendary_config):
                logger.info("[EPIC] No user.json found - not authenticated")
                return False

            # Verify the file has valid content with access token
            try:
                with open(legendary_config, 'r') as f:
                    data = json.load(f)
                    if not data:
                        logger.info("[EPIC] user.json empty - not authenticated")
                        return False

                    # Check for access_token to ensure it's a valid auth file
                    if 'access_token' not in data:
                        logger.info("[EPIC] user.json missing access_token - not authenticated")
                        return False

                    logger.info("[EPIC] Status: Connected (authenticated)")
                    return True

            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"[EPIC] Invalid user.json: {e}")
                return False

        except Exception as e:
            logger.error(f"[EPIC] Exception checking status: {e}", exc_info=True)
            return False

    async def start_auth(self) -> Dict[str, Any]:
        """Start Epic OAuth flow with automatic code detection via CDP"""
        if not self.legendary_bin:
            return {'success': False, 'error': 'legendary not found'}

        try:
            # Import here to avoid circular dependency
            from ..auth.browser import CDPOAuthMonitor
            
            # Run legendary auth and capture the authorization URL
            proc = await asyncio.create_subprocess_exec(
                self.legendary_bin, 'auth',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # Read initial output to get the URL
            stdout_data = []
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_text = line.decode().strip()
                stdout_data.append(line_text)

                # Look for URL in the output
                if 'https://' in line_text:
                    # Extract URL from line
                    for word in line_text.split():
                        if word.startswith('https://'):
                            logger.info(f"[EPIC] Got Epic auth URL: {word}")

                            # Start CDP monitoring in background to auto-capture code
                            asyncio.create_task(self._monitor_and_complete_auth())

                            return {
                                'success': True,
                                'url': word,
                                'message': 'Authenticating via browser - code will be captured automatically'
                            }

            # If we didn't find a URL, return error
            stderr = await proc.stderr.read()
            return {
                'success': False,
                'error': f'Could not get auth URL. Output: {" ".join(stdout_data)}, Error: {stderr.decode()}'
            }

        except Exception as e:
            logger.error(f"[EPIC] Error starting Epic auth: {e}")
            return {'success': False, 'error': str(e)}

    async def _monitor_and_complete_auth(self):
        """Background task to monitor for OAuth code and auto-complete authentication"""
        try:
            from ..auth.browser import CDPOAuthMonitor
            
            monitor = CDPOAuthMonitor()
            code, store = await monitor.monitor_for_oauth_code(expected_store='epic', timeout=300)

            if code and store == 'epic':
                logger.info(f"[EPIC] Auto-captured authorization code, completing auth...")
                result = await self.complete_auth(code)
                if result['success']:
                    logger.info("[EPIC] ✓ Authentication completed automatically!")

                    # Auto-sync library after successful auth
                    if self.plugin_instance:
                        logger.info("[EPIC] Starting automatic library sync...")
                        await self.plugin_instance.sync_libraries(fetch_artwork=False)
                        logger.info("[EPIC] ✓ Library sync completed!")
                else:
                    logger.error(f"[EPIC] Auto-auth failed: {result.get('error')}")
            else:
                logger.warning("[EPIC] CDP monitoring timeout - no code detected")
        except Exception as e:
            logger.error(f"[EPIC] Error in background auth monitor: {e}", exc_info=True)

    async def complete_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete Epic OAuth flow with authorization code"""
        if not self.legendary_bin:
            return {'success': False, 'error': 'legendary not found'}

        try:
            # Run legendary auth with the code
            proc = await asyncio.create_subprocess_exec(
                self.legendary_bin, 'auth', '--code', auth_code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                logger.info("Epic authentication successful")
                return {'success': True, 'message': 'Successfully authenticated with Epic Games'}
            else:
                error_msg = stderr.decode() or stdout.decode()
                logger.error(f"Epic auth failed: {error_msg}")
                return {'success': False, 'error': error_msg}

        except Exception as e:
            logger.error(f"Error completing Epic auth: {e}")
            return {'success': False, 'error': str(e)}

    async def logout(self) -> Dict[str, Any]:
        """Logout from Epic Games"""
        if not self.legendary_bin:
            return {'success': False, 'error': 'legendary not found'}

        try:
            from ..auth.browser import CDPOAuthMonitor
            
            # Run legendary auth --delete to remove stored credentials
            proc = await asyncio.create_subprocess_exec(
                self.legendary_bin, 'auth', '--delete',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

            logger.info("Logged out from Epic Games")

            # Clear browser cookies for Epic
            monitor = CDPOAuthMonitor()
            await monitor.clear_cookies_for_domain('epicgames.com')

            return {'success': True, 'message': 'Logged out from Epic Games'}

        except Exception as e:
            logger.error(f"Error logging out from Epic: {e}")
            return {'success': False, 'error': str(e)}

    async def get_library(self) -> List[Game]:
        """Get Epic Games library via legendary"""
        if not self.legendary_bin:
            logger.warning("Legendary CLI not found")
            return []

        try:
            proc = await asyncio.create_subprocess_exec(
                self.legendary_bin, 'list', '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"legendary list failed: {stderr.decode()}")
                return []

            games_data = json.loads(stdout.decode())
            games = []

            for game_data in games_data:
                game = Game(
                    id=game_data.get('app_name', ''),
                    title=game_data.get('app_title', ''),
                    store='epic',
                    is_installed=False  # legendary list shows all games, not just installed
                )
                games.append(game)

            logger.info(f"Found {len(games)} Epic games")
            return games

        except Exception as e:
            logger.error(f"Error fetching Epic library: {e}")
            return []

    async def get_installed(self) -> Dict[str, Any]:
        """
        Get installed Epic games with caching for performance
        Returns dict of {app_name: metadata_dict}
        """
        global _legendary_installed_cache

        if not self.legendary_bin:
            return {}

        # Check cache first
        current_time = time.time()
        if (_legendary_installed_cache['data'] is not None and
            current_time - _legendary_installed_cache['timestamp'] < _legendary_installed_cache['ttl']):
            logger.info("Returning cached legendary list-installed")
            return _legendary_installed_cache['data']

        # Cache miss - run legendary command
        logger.info("Cache miss - running legendary list-installed")
        try:
            # We strictly want the full JSON metadata
            proc = await asyncio.create_subprocess_exec(
                self.legendary_bin, 'list-installed', '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"legendary list-installed failed: {stderr.decode()}")
                return {}

            games_data = json.loads(stdout.decode())
            
            # Convert list to dict keyed by app_name
            installed_map = {}
            for g in games_data:
                app_name = g.get('app_name')
                if app_name:
                    installed_map[app_name] = g

            # Cache the result
            _legendary_installed_cache['data'] = installed_map
            _legendary_installed_cache['timestamp'] = current_time

            return installed_map

        except Exception as e:
            logger.error(f"Error fetching installed Epic games: {e}")
            return {}

    async def get_game_size(self, game_id: str) -> Optional[int]:
        """Get game download size in bytes from Epic/Legendary with caching

        Args:
            game_id: Epic game app_name (ID)

        Returns:
            Download size in bytes, or None if unable to determine
        """
        global _legendary_info_cache

        if not self.legendary_bin:
            return None

        # Check cache first
        if game_id in _legendary_info_cache:
            cache_entry = _legendary_info_cache[game_id]
            if time.time() - cache_entry['timestamp'] < 300:  # 5 minute cache
                logger.info(f"Returning cached size for {game_id}")
                return cache_entry['size']

        # Cache miss - run legendary info
        try:
            proc = await asyncio.create_subprocess_exec(
                self.legendary_bin, 'info', game_id, '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                info = json.loads(stdout.decode())
                # Parse size from legendary info output
                # legendary info returns manifest with download_size
                manifest = info.get('manifest', {})
                download_size = manifest.get('download_size', 0)
                logger.info(f"[Epic] Game {game_id} size: {download_size} bytes")

                # Cache the result
                _legendary_info_cache[game_id] = {
                    'size': download_size,
                    'timestamp': time.time()
                }

                return download_size
            else:
                logger.warning(f"legendary info failed for {game_id}: {stderr.decode()}")
                return None

        except Exception as e:
            logger.error(f"Error getting game size for {game_id}: {e}")
            return None

    def _find_executable_fallback(self, install_path: str) -> Optional[str]:
        """Scan install directory for likely game executable when manifest lacks launch_exe.
        
        Args:
            install_path: Game installation directory
            
        Returns:
            Path to likely game executable, or None if not found
        """
        if not os.path.isdir(install_path):
            return None
        
        import glob
        
        # Skip patterns - these are NOT game executables
        skip_patterns = [
            'unins', 'setup', 'install', 'crash', 'ue4prereq', 'redist',
            'vcredist', 'dxsetup', 'directx', 'launcher', 'easyanticheat',
            'battleye', 'eos_', 'eossdk', 'dotnet'
        ]
        
        # Common patterns for Epic/Unreal games, ordered by likelihood
        exe_patterns = [
            # Root level executables (most common)
            "*.exe",
            # Unreal Engine patterns
            "Binaries/Win64/*.exe",
            "Binaries/Win32/*.exe",
            "**/Binaries/Win64/*.exe",
            "**/Binaries/Win32/*.exe",
            # Shipping builds
            "**/Shipping/*.exe",
            # Game subfolder
            "Game/*.exe",
            "**/Game/*.exe",
        ]
        
        candidates = []
        
        for pattern in exe_patterns:
            try:
                full_pattern = os.path.join(install_path, pattern)
                matches = glob.glob(full_pattern, recursive=('**' in pattern))
                
                for match in matches:
                    basename = os.path.basename(match).lower()
                    
                    # Skip if matches any skip pattern
                    if any(skip in basename for skip in skip_patterns):
                        continue
                    
                    # Skip if in a redistributables folder
                    if any(skip in match.lower() for skip in ['redistributables', 'redist', '__installer']):
                        continue
                    
                    # Get file size to prioritize larger executables (actual game vs utilities)
                    try:
                        size = os.path.getsize(match)
                        candidates.append((match, size))
                    except OSError:
                        candidates.append((match, 0))
                        
            except Exception as e:
                logger.debug(f"[Epic] Error scanning pattern {pattern}: {e}")
        
        if not candidates:
            return None
        
        # Sort by size descending - larger executables are more likely to be the game
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        logger.info(f"[Epic] Found {len(candidates)} executable candidates, selecting: {candidates[0][0]}")
        return candidates[0][0]

    async def install_game(self, game_id: str, progress_callback=None) -> Dict[str, Any]:
        """Install Epic game using legendary CLI

        Args:
            game_id: Epic game app_name (ID)
            progress_callback: Optional async function to call with progress updates

        Returns:
            Dict with success status, install_path, and error if any
        """
        if not self.legendary_bin:
            return {
                'success': False,
                'error': 'Legendary CLI not found'
            }

        try:
            # legendary install GAME_ID --base-path ~/Games/Epic
            base_path = os.path.expanduser("~/Games/Epic")
            os.makedirs(base_path, exist_ok=True)

            logger.info(f"[Epic] Starting installation of {game_id} to {base_path}")

            proc = await asyncio.create_subprocess_exec(
                self.legendary_bin, 'install', game_id,
                '--base-path', base_path,
                '--yes',  # Accept prompts automatically
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )

            # Stream output to track progress
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break

                line_str = line.decode().strip()

                # Parse progress from legendary output
                # Example: "Progress: [##########] 45.2% (1.2 GB / 2.5 GB)"
                if 'Progress:' in line_str:
                    match = re.search(r'(\d+\.?\d*)%', line_str)
                    if match:
                        percentage = float(match.group(1))
                        logger.info(f"[Epic Download] {game_id}: {percentage:.1f}%")

                        if progress_callback:
                            await progress_callback({
                                'progress': percentage,
                                'status': line_str
                            })
                    else:
                        logger.info(f"[Epic Install] {line_str}")
                elif line_str:  # Log other output
                    logger.info(f"[Epic Install] {line_str}")

            await proc.wait()

            if proc.returncode == 0:
                # Get actual install path from legendary (don't assume directory name)
                logger.info(f"[Epic] Installation complete, getting actual install path for {game_id}")

                info_proc = await asyncio.create_subprocess_exec(
                    self.legendary_bin, 'info', game_id, '--json',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await info_proc.communicate()

                if info_proc.returncode == 0:
                    try:
                        info = json.loads(stdout.decode())
                        install_path = info.get('install', {}).get('install_path', '')
                        executable = info.get('manifest', {}).get('launch_exe', '')

                        if install_path and executable:
                            # Strip leading slash - legendary returns paths like '/Binaries/Win64/Game.exe'
                            # which causes os.path.join to treat it as absolute, ignoring install_path
                            executable = executable.lstrip('/')
                            exe_path = os.path.join(install_path, executable)
                            logger.info(f"[Epic] Successfully installed {game_id} to {install_path}")
                            logger.info(f"[Epic] Executable: {exe_path}")
                            
                            # Write manifest for recovery after plugin reinstall
                            try:
                                from ..discovery.startup import write_game_manifest
                                game_title = info.get('game', {}).get('title', game_id)
                                write_game_manifest(
                                    install_path=install_path,
                                    store="epic",
                                    game_id=game_id,
                                    title=game_title,
                                    executable_relative=executable,  # Already stripped of leading slash
                                    platform="windows"
                                )
                            except Exception as e:
                                logger.warning(f"[Epic] Failed to write manifest: {e}")
                            
                            return {
                                'success': True,
                                'install_path': install_path,
                                'exe_path': exe_path,
                                'message': f'Successfully installed {game_id}'
                            }
                        elif install_path:
                            # Have install path but no executable info - try fallback scan
                            logger.warning(f"[Epic] Manifest missing launch_exe for {game_id}, scanning for executable...")
                            exe_path = self._find_executable_fallback(install_path)
                            if exe_path:
                                logger.info(f"[Epic] Found executable via fallback scan: {exe_path}")
                                
                                # Write manifest for recovery
                                try:
                                    from ..discovery.startup import write_game_manifest
                                    game_title = info.get('game', {}).get('title', game_id)
                                    exe_relative = os.path.relpath(exe_path, install_path)
                                    write_game_manifest(
                                        install_path=install_path,
                                        store="epic",
                                        game_id=game_id,
                                        title=game_title,
                                        executable_relative=exe_relative,
                                        platform="windows"
                                    )
                                except Exception as e:
                                    logger.warning(f"[Epic] Failed to write manifest: {e}")
                                
                                return {
                                    'success': True,
                                    'install_path': install_path,
                                    'exe_path': exe_path,
                                    'message': f'Successfully installed {game_id}'
                                }
                            else:
                                logger.warning(f"[Epic] Could not determine executable for {game_id}")
                                return {
                                    'success': True,
                                    'install_path': install_path,
                                    'message': f'Successfully installed {game_id} (executable unknown)'
                                }
                    except Exception as e:
                        logger.error(f"[Epic] Error parsing legendary info: {e}")

                # Fallback: try the assumed path
                logger.warning(f"[Epic] Could not get install path from legendary, using fallback")
                install_path = os.path.join(base_path, game_id)
                return {
                    'success': True,
                    'install_path': install_path,
                    'message': f'Successfully installed {game_id} (path uncertain)'
                }
            else:
                logger.error(f"[Epic] Installation failed for {game_id}")
                return {
                    'success': False,
                    'error': 'Installation failed - check logs for details'
                }

        except Exception as e:
            logger.error(f"Error installing game {game_id}: {e}")
            return {
                'success': False,
                'error': str(e)
            }
