"""
Amazon Games Store connector using nile CLI.

This module handles all Amazon Games Store operations including authentication,
library fetching, and game installation via the nile CLI tool.
"""
import asyncio
import json
import logging
import os
import re
import shutil
from typing import Dict, Any, List, Optional

from .base import Store, Game

logger = logging.getLogger(__name__)


class AmazonConnector(Store):
    """Handles Amazon Games via nile CLI"""

    def __init__(self, plugin_dir: Optional[str] = None, plugin_instance=None):
        self.plugin_dir = plugin_dir
        self.plugin_instance = plugin_instance  # Reference to parent Plugin for auto-sync
        self.nile_bin = self._find_nile()
        self._pending_login_data = None  # Store login data during OAuth flow
        logger.info(f"Nile binary: {self.nile_bin}")
    
    @property
    def store_name(self) -> str:
        return 'amazon'

    def _find_nile(self) -> Optional[str]:
        """Find nile executable - checks bundled binary first, then system"""
        # Priority 1: Check bundled nile in plugin bin/ directory
        if self.plugin_dir:
            bundled_nile = os.path.join(self.plugin_dir, 'bin', 'nile')
            if os.path.isfile(bundled_nile) and os.access(bundled_nile, os.X_OK):
                logger.info(f"[Amazon] Using bundled nile: {bundled_nile}")
                return bundled_nile

        # Priority 2: Check system PATH
        nile_path = shutil.which("nile")
        if nile_path:
            logger.info(f"[Amazon] Using system nile: {nile_path}")
            return nile_path

        # Priority 3: Check ~/.local/bin explicitly
        local_bin_nile = os.path.expanduser("~/.local/bin/nile")
        if os.path.exists(local_bin_nile):
            logger.info(f"[Amazon] Using user nile: {local_bin_nile}")
            return local_bin_nile

        logger.warning("[Amazon] Nile not found - Amazon Games features unavailable")
        return None

    async def is_available(self) -> bool:
        """Check if nile is installed and authenticated"""
        logger.info(f"[Amazon] Checking availability, nile_bin={self.nile_bin}")

        if not self.nile_bin:
            logger.warning("[Amazon] Nile CLI not found - not installed")
            return False

        try:
            # Check for user.json which contains Amazon auth tokens
            nile_config = os.path.expanduser("~/.config/nile")
            user_file = os.path.join(nile_config, "user.json")

            if not os.path.exists(user_file):
                logger.info("[Amazon] No user.json found - not authenticated")
                return False

            # Verify the file has valid content
            try:
                with open(user_file, 'r') as f:
                    data = json.load(f)
                    if not data:
                        logger.info("[Amazon] user.json empty - not authenticated")
                        return False

                    # Check for customer_info which indicates valid auth
                    extensions = data.get('extensions', {})
                    if 'customer_info' not in extensions:
                        logger.info("[Amazon] user.json missing customer_info - not authenticated")
                        return False

                    logger.info("[Amazon] Status: Connected (authenticated)")
                    return True

            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"[Amazon] Invalid user.json: {e}")
                return False

        except Exception as e:
            logger.error(f"[Amazon] Exception checking status: {e}", exc_info=True)
            return False

    async def start_auth(self) -> Dict[str, Any]:
        """Start Amazon OAuth flow via nile (non-interactive mode)"""
        if not self.nile_bin:
            return {'success': False, 'error': 'nile not found'}

        try:
            from ..auth.browser import CDPOAuthMonitor
            
            logger.info("[Amazon] Starting OAuth flow...")

            proc = await asyncio.create_subprocess_exec(
                self.nile_bin, 'auth', '--login', '--non-interactive',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                try:
                    login_data = json.loads(stdout.decode())
                    # Store login data for completion step
                    self._pending_login_data = login_data
                    logger.info(f"[Amazon] Got login URL, waiting for user authorization")
                    
                    # Start CDP monitoring in background to auto-capture code
                    asyncio.create_task(self._monitor_and_complete_auth())
                    
                    return {
                        'success': True,
                        'url': login_data.get('url', ''),
                        'message': 'Please login in the browser window'
                    }
                except json.JSONDecodeError as e:
                    logger.error(f"[Amazon] Failed to parse login data: {e}")
                    return {'success': False, 'error': 'Failed to parse login response'}
            else:
                error_msg = stderr.decode() if stderr else 'Unknown error'
                logger.error(f"[Amazon] Auth failed: {error_msg}")
                return {'success': False, 'error': error_msg}

        except Exception as e:
            logger.error(f"[Amazon] Error starting auth: {e}")
            return {'success': False, 'error': str(e)}

    async def complete_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete Amazon OAuth with authorization code from browser"""
        if not self.nile_bin:
            return {'success': False, 'error': 'nile not found'}

        if not self._pending_login_data:
            return {'success': False, 'error': 'No pending login - call start_auth first'}

        try:
            login_data = self._pending_login_data
            logger.info(f"[Amazon] Completing auth with code...")

            proc = await asyncio.create_subprocess_exec(
                self.nile_bin, 'register',
                '--code', auth_code,
                '--code-verifier', login_data.get('code_verifier', ''),
                '--serial', login_data.get('serial', ''),
                '--client-id', login_data.get('client_id', ''),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            # Nile prints success message to stderr
            output = stderr.decode() if stderr else stdout.decode()
            
            if 'Succesfully registered' in output or 'Successfully registered' in output:
                self._pending_login_data = None  # Clear pending data
                logger.info("[Amazon] Authentication successful!")
                
                # Trigger auto-sync if plugin instance available
                if self.plugin_instance:
                    logger.info("[Amazon] Triggering library sync after auth")
                    asyncio.create_task(self.plugin_instance.force_sync_libraries())
                    
                return {'success': True, 'message': 'Authenticated successfully'}
            else:
                logger.error(f"[Amazon] Registration failed: {output}")
                return {'success': False, 'error': 'Authentication failed'}

        except Exception as e:
            logger.error(f"[Amazon] Error completing auth: {e}")
            return {'success': False, 'error': str(e)}

    async def _monitor_and_complete_auth(self):
        """Monitor browser for OAuth callback and auto-complete authentication"""
        try:
            from ..auth.browser import CDPOAuthMonitor
            
            monitor = CDPOAuthMonitor()
            logger.info("[Amazon] Starting CDP monitor for auth code...")
            
            # Monitor for Amazon OAuth code (5 min timeout)
            code, store = await monitor.monitor_for_oauth_code(expected_store='amazon', timeout=300)
            
            if code and store == 'amazon':
                logger.info(f"[Amazon] ✓ Auto-captured authorization code via CDP")
                result = await self.complete_auth(code)
                if result.get('success'):
                    logger.info("[Amazon] ✓ Auto-authentication completed successfully")
                else:
                    logger.error(f"[Amazon] Auto-auth completion failed: {result.get('error')}")
            else:
                logger.warning("[Amazon] CDP monitoring timeout - user may need to manually enter code")
                
        except Exception as e:
            logger.error(f"[Amazon] Error in CDP monitoring: {e}", exc_info=True)

    async def logout(self) -> Dict[str, Any]:
        """Logout from Amazon Games"""
        if not self.nile_bin:
            return {'success': False, 'error': 'nile not found'}

        try:
            proc = await asyncio.create_subprocess_exec(
                self.nile_bin, 'auth', '--logout',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()

            logger.info("[Amazon] Logged out successfully")
            return {'success': True, 'message': 'Logged out successfully'}

        except Exception as e:
            logger.error(f"[Amazon] Error during logout: {e}")
            return {'success': False, 'error': str(e)}

    async def sync_library(self) -> bool:
        """Sync Amazon Games library from server"""
        if not self.nile_bin:
            return False

        try:
            logger.info("[Amazon] Syncing library from server...")
            proc = await asyncio.create_subprocess_exec(
                self.nile_bin, 'library', 'sync',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                logger.info("[Amazon] Library sync complete")
                return True
            else:
                logger.warning(f"[Amazon] Library sync failed: {stderr.decode()}")
                return False

        except Exception as e:
            logger.error(f"[Amazon] Error syncing library: {e}")
            return False

    async def get_library(self) -> List[Game]:
        """Get Amazon Games library via nile"""
        if not self.nile_bin:
            logger.warning("[Amazon] Nile CLI not found")
            return []

        try:
            # First sync library to get latest
            await self.sync_library()

            # Read library directly from nile's library.json file
            nile_config = os.path.expanduser("~/.config/nile")
            library_file = os.path.join(nile_config, "library.json")
            
            if not os.path.exists(library_file):
                logger.warning("[Amazon] library.json not found")
                return []
            
            with open(library_file, 'r') as f:
                games_data = json.load(f)

            games = []

            # Get installed games to mark install status
            installed = await self.get_installed()

            for game_data in games_data:
                product = game_data.get('product', {})
                game_id = product.get('id', '')
                title = product.get('title', 'Unknown')
                
                game = Game(
                    id=game_id,
                    title=title,
                    store='amazon',
                    is_installed=game_id in installed
                )
                games.append(game)

            logger.info(f"[Amazon] Found {len(games)} games")
            return games

        except Exception as e:
            logger.error(f"[Amazon] Error fetching library: {e}", exc_info=True)
            return []

    async def get_installed(self) -> Dict[str, Any]:
        """Get list of installed Amazon games from nile config"""
        nile_config = os.path.expanduser("~/.config/nile")
        installed_file = os.path.join(nile_config, "installed.json")

        if not os.path.exists(installed_file):
            return {}

        try:
            with open(installed_file, 'r') as f:
                installed_list = json.load(f)

            installed_dict = {}
            for game in installed_list:
                game_id = game.get('id', '')
                installed_dict[game_id] = {
                    'version': game.get('version', ''),
                    'path': game.get('path', '')
                }
            return installed_dict

        except Exception as e:
            logger.error(f"[Amazon] Error reading installed.json: {e}")
            return {}

    def get_installed_game_info(self, game_id: str) -> Optional[Dict[str, Any]]:
        """Get installed game info synchronously"""
        nile_config = os.path.expanduser("~/.config/nile")
        installed_file = os.path.join(nile_config, "installed.json")

        if not os.path.exists(installed_file):
            return None

        try:
            with open(installed_file, 'r') as f:
                installed_list = json.load(f)

            for game in installed_list:
                if game.get('id') == game_id:
                    install_path = game.get('path', '')
                    
                    # Parse fuel.json for executable
                    exe_path = self._get_executable_from_fuel(install_path)
                    
                    return {
                        'id': game_id,
                        'version': game.get('version', ''),
                        'path': install_path,
                        'executable': exe_path
                    }
            return None

        except Exception as e:
            logger.error(f"[Amazon] Error getting installed game info: {e}")
            return None

    def _get_executable_from_fuel(self, install_path: str) -> Optional[str]:
        """Get executable path from fuel.json"""
        if not install_path:
            return None

        fuel_path = os.path.join(install_path, 'fuel.json')
        if not os.path.exists(fuel_path):
            logger.warning(f"[Amazon] No fuel.json found at {fuel_path}")
            return None

        try:
            # fuel.json might have comments, try json5 style parsing
            with open(fuel_path, 'r') as f:
                content = f.read()
                # Remove single-line comments
                content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
                fuel_data = json.loads(content)

            main_cmd = fuel_data.get('Main', {}).get('Command', '')
            if main_cmd:
                exe_path = os.path.join(install_path, main_cmd)
                logger.info(f"[Amazon] Found executable from fuel.json: {exe_path}")
                return exe_path

        except Exception as e:
            logger.error(f"[Amazon] Error parsing fuel.json: {e}")

        return None

    async def get_game_size(self, game_id: str) -> Optional[int]:
        """Get game download size in bytes"""
        if not self.nile_bin:
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                self.nile_bin, 'install', game_id, '--info', '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                output = stdout.decode()
                # Find the JSON line (skip INFO/log lines)
                for line in output.strip().split('\n'):
                    if line.startswith('{'):
                        try:
                            info = json.loads(line)
                            download_size = info.get('download_size', 0)
                            logger.info(f"[Amazon] Game {game_id} size: {download_size} bytes")
                            return download_size
                        except json.JSONDecodeError:
                            continue
                
                logger.warning(f"[Amazon] Could not parse size info for {game_id}")
                return None

        except Exception as e:
            logger.error(f"[Amazon] Error getting game size: {e}")

        return None

    async def install_game(self, game_id: str, progress_callback=None) -> Dict[str, Any]:
        """Install Amazon game using nile CLI"""
        if not self.nile_bin:
            return {'success': False, 'error': 'Nile CLI not found'}

        try:
            base_path = os.path.expanduser("~/Games/Amazon")
            os.makedirs(base_path, exist_ok=True)

            logger.info(f"[Amazon] Starting installation of {game_id} to {base_path}")

            proc = await asyncio.create_subprocess_exec(
                self.nile_bin, 'install', game_id,
                '--base-path', base_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )

            # Parse progress from output
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                logger.info(f"[Amazon Install] {line_str}")

                # Parse progress: [Installation] [XX%] message
                if '[Installation]' in line_str and '%' in line_str:
                    match = re.search(r'\[(\d+)%\]', line_str)
                    if match and progress_callback:
                        progress = int(match.group(1))
                        await progress_callback(progress)

            await proc.wait()

            if proc.returncode == 0:
                # Get install info
                info_proc = await asyncio.create_subprocess_exec(
                    self.nile_bin, 'install', game_id, '--info', '--json',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await info_proc.communicate()

                install_path = None
                exe_path = None

                if info_proc.returncode == 0:
                    try:
                        info = json.loads(stdout.decode())
                        install_path = info.get('game', {}).get('path', '')
                    except:
                        pass

                # Fallback: check installed.json
                if not install_path:
                    installed = await self.get_installed()
                    if game_id in installed:
                        install_path = installed[game_id].get('path', '')

                if install_path:
                    exe_path = self._get_executable_from_fuel(install_path)
                    logger.info(f"[Amazon] Successfully installed {game_id} to {install_path}")
                    
                    # Write manifest for recovery after plugin reinstall
                    try:
                        from ..discovery.startup import write_game_manifest
                        # Try to get game title from library
                        game_title = game_id  # Default to ID
                        try:
                            nile_config = os.path.expanduser("~/.config/nile")
                            library_file = os.path.join(nile_config, "library.json")
                            if os.path.exists(library_file):
                                with open(library_file, 'r') as f:
                                    games_data = json.load(f)
                                for game_data in games_data:
                                    product = game_data.get('product', {})
                                    if product.get('id') == game_id:
                                        game_title = product.get('title', game_id)
                                        break
                        except Exception:
                            pass
                        
                        exe_relative = os.path.relpath(exe_path, install_path) if exe_path else ""
                        write_game_manifest(
                            install_path=install_path,
                            store="amazon",
                            game_id=game_id,
                            title=game_title,
                            executable_relative=exe_relative,
                            platform="windows"
                        )
                    except Exception as e:
                        logger.warning(f"[Amazon] Failed to write manifest: {e}")
                    
                    return {
                        'success': True,
                        'install_path': install_path,
                        'exe_path': exe_path,
                        'message': f'Successfully installed {game_id}'
                    }
                else:
                    return {
                        'success': True,
                        'install_path': base_path,
                        'message': f'Successfully installed {game_id} (path uncertain)'
                    }
            else:
                logger.error(f"[Amazon] Installation failed for {game_id}")
                return {
                    'success': False,
                    'error': 'Installation failed - check logs for details'
                }

        except Exception as e:
            logger.error(f"[Amazon] Error installing game {game_id}: {e}")
            return {'success': False, 'error': str(e)}

    async def uninstall_game(self, game_id: str) -> Dict[str, Any]:
        """Uninstall Amazon game using nile CLI"""
        if not self.nile_bin:
            return {'success': False, 'error': 'Nile CLI not found'}

        try:
            logger.info(f"[Amazon] Starting uninstallation of {game_id}")

            proc = await asyncio.create_subprocess_exec(
                self.nile_bin, 'uninstall', game_id, '--yes',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                logger.info(f"[Amazon] Successfully uninstalled {game_id}")
                return {
                    'success': True,
                    'message': f'Successfully uninstalled {game_id}'
                }
            else:
                error_msg = stderr.decode() if stderr else 'Unknown error'
                logger.error(f"[Amazon] Uninstallation failed: {error_msg}")
                return {
                    'success': False,
                    'error': f'Uninstallation failed: {error_msg}'
                }

        except Exception as e:
            logger.error(f"[Amazon] Error uninstalling game {game_id}: {e}")
            return {'success': False, 'error': str(e)}
