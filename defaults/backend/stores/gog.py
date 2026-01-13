"""
GOG Store connector using direct OAuth API and gogdl binary.

This module handles all GOG.com operations including authentication,
library fetching, and game installation via gogdl binary.
"""
import asyncio
import glob
import io
import json
import logging
import os
import re
import shutil
import ssl
import time
import subprocess
from typing import Dict, Any, List, Optional, Tuple

try:
    import aiohttp
except ImportError:
    aiohttp = None

from .base import Store, Game
from ..auth.browser import CDPOAuthMonitor

logger = logging.getLogger(__name__)


class GOGAPIClient:
    """Handles GOG via direct API calls using OAuth and gogdl binary"""

    # OAuth constants
    BASE_URL = "https://embed.gog.com"
    AUTH_URL = "https://auth.gog.com"
    CLIENT_ID = "46899977096215655"
    CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
    REDIRECT_URI = "https://embed.gog.com/on_login_success?origin=client"  # GOG's registered redirect URI

    def __init__(self, plugin_dir: Optional[str] = None, plugin_instance=None):
        self.plugin_dir = plugin_dir  # Plugin root directory for finding bundled binaries
        self.plugin_instance = plugin_instance  # Reference to parent Plugin for auto-sync
        self.token_file = os.path.expanduser("~/.config/unifideck/gog_token.json")
        self.gogdl_config_path = os.path.expanduser("~/.config/unifideck/gog_credentials.json")
        self.download_dir = os.path.expanduser("~/GOG Games")
        
        # Locate gogdl binary
        self.gogdl_bin = None
        if self.plugin_dir:
            self.gogdl_bin = os.path.join(self.plugin_dir, 'bin', 'gogdl')
        
        if not self.gogdl_bin or not os.path.exists(self.gogdl_bin):
            logger.warning(f"[GOG] gogdl binary NOT found at {self.gogdl_bin}. Installation will fail.")
        else:
            logger.info(f"[GOG] Found gogdl binary at {self.gogdl_bin}")

        self.access_token = None
        self.refresh_token = None
        self._load_tokens()
        logger.info("GOG API client initialized")

    def _load_tokens(self):
        """Load stored OAuth tokens"""
        try:
            if os.path.exists(self.token_file):
                with open(self.token_file, 'r') as f:
                    data = json.load(f)
                    self.access_token = data.get('access_token')
                    self.refresh_token = data.get('refresh_token')
                    logger.info("Loaded GOG tokens from file")
        except Exception as e:
            logger.error(f"Error loading GOG tokens: {e}")

    def _save_tokens(self, access_token: str, refresh_token: str):
        """Save OAuth tokens to file"""
        try:
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
            with open(self.token_file, 'w') as f:
                json.dump({
                    'access_token': access_token,
                    'refresh_token': refresh_token
                }, f)
            self.access_token = access_token
            self.refresh_token = refresh_token
            logger.info("Saved GOG tokens to file")
            
            # Auto-sync to gogdl config format immediately
            self._ensure_auth_config()
            
        except Exception as e:
            logger.error(f"Error saving GOG tokens: {e}")
    
    def _ensure_auth_config(self) -> bool:
        """
        Convert Unifideck's gog_token.json format to gogdl's expected gog_credentials.json format.
        Called before any gogdl operation.
        
        Unifideck: {"access_token": "...", "refresh_token": "..."}
        gogdl: {"46899977096215655": {"access_token": "...", "refresh_token": "...", "token_type": "Bearer", "expires_in": 3600, ...}}
        """
        if not self.access_token:
            return False
            
        try:
            # Prepare gogdl format
            # Using standard GOG Client ID as key
            gogdl_data = {
                self.CLIENT_ID: {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "token_type": "Bearer",
                    "expires_in": 3600, # Dummy value, gogdl handles refresh if needed
                    "scope": "openid",
                    "created_at": time.time(),
                    "loginTime": time.time()  # Required by gogdl auth.py logic
                }
            }
            
            os.makedirs(os.path.dirname(self.gogdl_config_path), exist_ok=True)
            with open(self.gogdl_config_path, 'w') as f:
                json.dump(gogdl_data, f, indent=2)
            
            logger.debug("[GOG] Synced auth tokens to gogdl config")
            return True
        except Exception as e:
            logger.error(f"[GOG] Failed to sync auth tokens to gogdl config: {e}")
            return False

    async def is_available(self) -> bool:
        """Check if GOG is authenticated"""
        logger.info("[GOG] Checking availability")

        if not self.access_token:
            logger.info("[GOG] No access token found - not authenticated")
            return False

        logger.info(f"[GOG] Access token present (length: {len(self.access_token)})")

        try:
            # Try to get user data to verify token is valid
            import aiohttp
            import ssl

            # Create SSL context that doesn't verify certificates (needed on Steam Deck)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            # Add timeout
            timeout = aiohttp.ClientTimeout(total=5.0)
            # logger.info("[GOG] Requesting: GET https://embed.gog.com/userData.json")

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(
                    'https://embed.gog.com/userData.json',
                    headers={'Authorization': f'Bearer {self.access_token}'}
                ) as response:
                    # logger.info(f"[GOG] Response status: {response.status}")

                    if response.status == 200:
                        # data = await response.text()
                        logger.info("[GOG] Status: Connected (authenticated)")
                        return True
                    elif response.status == 401:
                        logger.warning("[GOG] Token expired (401), attempting refresh")
                        return await self._refresh_access_token()
                    else:
                        error_text = await response.text()
                        logger.warning(f"[GOG] Auth check failed (status: {response.status})")
                        return False

        except asyncio.TimeoutError:
            logger.error("[GOG] Status check timed out after 5 seconds")
            return False
        except Exception as e:
            logger.error(f"[GOG] Exception checking status: {e}", exc_info=True)
            return False

    async def _refresh_access_token(self) -> bool:
        """Refresh the access token using refresh token"""
        if not self.refresh_token:
            return False

        try:
            import aiohttp
            import ssl

            # Create SSL context that doesn't verify certificates (needed on Steam Deck)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    f'https://auth.gog.com/token?client_id={self.CLIENT_ID}&client_secret={self.CLIENT_SECRET}&grant_type=refresh_token&refresh_token={self.refresh_token}'
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._save_tokens(data['access_token'], data['refresh_token'])
                        logger.info("Refreshed GOG access token")
                        return True
                    else:
                        logger.error(f"Failed to refresh GOG token: {response.status}")
                        return False
        except Exception as e:
            logger.error(f"Error refreshing GOG token: {e}")
            return False

    async def start_auth(self) -> Dict[str, Any]:
        """Start GOG OAuth flow with automatic code detection via CDP"""
        auth_url = f"https://auth.gog.com/auth?client_id={self.CLIENT_ID}&redirect_uri={self.REDIRECT_URI}&response_type=code&layout=client2"

        # Start CDP monitoring in background to auto-capture code
        asyncio.create_task(self._monitor_and_complete_auth())

        return {
            'success': True,
            'url': auth_url,
            'message': 'Authenticating via browser - code will be captured automatically'
        }

    async def _monitor_and_complete_auth(self):
        """Background task to monitor for OAuth code and auto-complete authentication"""
        try:
            monitor = CDPOAuthMonitor()
            code, store = await monitor.monitor_for_oauth_code(expected_store='gog', timeout=300)

            if code and store == 'gog':
                logger.info(f"[GOG] Auto-captured authorization code, completing auth...")
                result = await self.complete_auth(code)
                if result['success']:
                    logger.info("[GOG] ✓ Authentication completed automatically!")
                    # Auto-sync library after successful auth
                    if self.plugin_instance:
                        logger.info("[GOG] Starting automatic library sync...")
                        await self.plugin_instance.sync_libraries(fetch_artwork=False)
                        logger.info("[GOG] ✓ Library sync completed!")
                else:
                    logger.error(f"[GOG] Auto-auth failed: {result.get('error')}")
            else:
                logger.error("[GOG] CDP monitoring timeout - user may have closed popup or not completed login")
        except Exception as e:
            logger.error(f"[GOG] Error in background auth monitor: {e}", exc_info=True)

    async def complete_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete GOG OAuth flow with authorization code"""
        try:
            import aiohttp
            import ssl

            # Create SSL context that doesn't verify certificates (needed on Steam Deck)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            # Exchange authorization code for tokens
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    f'https://auth.gog.com/token?client_id={self.CLIENT_ID}&client_secret={self.CLIENT_SECRET}&grant_type=authorization_code&code={auth_code}&redirect_uri={self.REDIRECT_URI}'
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._save_tokens(data['access_token'], data['refresh_token'])
                        logger.info("GOG authentication successful")
                        
                        # Sync auth to gogdl config
                        self._ensure_auth_config()
                        
                        return {'success': True, 'message': 'Successfully authenticated with GOG'}
                    else:
                        error_text = await response.text()
                        logger.error(f"GOG auth failed: {response.status} - {error_text}")
                        return {'success': False, 'error': f'Authentication failed: {error_text}'}

        except Exception as e:
            logger.error(f"Error completing GOG auth: {e}")
            return {'success': False, 'error': str(e)}

    async def logout(self) -> Dict[str, Any]:
        """Logout from GOG"""
        try:
            # Remove token file
            if os.path.exists(self.token_file):
                os.remove(self.token_file)
                logger.info(f"Removed GOG token file: {self.token_file}")

            # Also remove gogdl config if it exists
            if os.path.exists(self.gogdl_config_path):
                os.remove(self.gogdl_config_path)

            # Clear in-memory tokens
            self.access_token = None
            self.refresh_token = None

            logger.info("Logged out from GOG")

            # Clear browser cookies for GOG
            monitor = CDPOAuthMonitor()
            await monitor.clear_cookies_for_domain('gog.com')

            return {'success': True, 'message': 'Logged out from GOG'}

        except Exception as e:
            logger.error(f"Error logging out from GOG: {e}")
            return {'success': False, 'error': str(e)}

    async def get_library(self) -> List[Game]:
        """Get GOG library via API with pagination support"""
        if not self.access_token:
            logger.warning("GOG not authenticated")
            return []

        try:
            import aiohttp
            import ssl

            # Create SSL context that doesn't verify certificates (needed on Steam Deck)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                games = []
                current_page = 1
                total_pages = 1  # Will be updated from first response

                while current_page <= total_pages:
                    # Get owned games with pagination
                    url = f'https://embed.gog.com/account/getFilteredProducts?mediaType=1&page={current_page}'
                    logger.info(f"[GOG] Fetching library page {current_page}...")
                    
                    async with session.get(
                        url,
                        headers={'Authorization': f'Bearer {self.access_token}'}
                    ) as response:
                        if response.status != 200:
                            logger.error(f"Failed to get GOG library page {current_page}: {response.status}")
                            break

                        data = await response.json()
                        
                        # Update total pages from response
                        total_pages = data.get('totalPages', 1)
                        total_results = data.get('totalGamesFound', 0)
                        
                        if current_page == 1:
                            logger.info(f"[GOG] Total games in library: {total_results}, total pages: {total_pages}")

                        for product in data.get('products', []):
                            game = Game(
                                id=str(product.get('id', '')),
                                title=product.get('title', ''),
                                store='gog',
                                is_installed=False
                            )
                            games.append(game)
                        
                        logger.info(f"[GOG] Fetched page {current_page}/{total_pages}: {len(data.get('products', []))} games (total so far: {len(games)})")
                        current_page += 1

                logger.info(f"Found {len(games)} GOG games (complete library)")
                return games

        except Exception as e:
            logger.error(f"Error fetching GOG library: {e}")
            return None

    async def get_installed(self) -> List[str]:
        """
        Get installed GOG games by checking download directory
        """
        if not os.path.exists(self.download_dir):
            return []

        try:
            installed_ids = []
            for item in os.listdir(self.download_dir):
                item_path = os.path.join(self.download_dir, item)
                if os.path.isdir(item_path):
                    # Check if directory contains game files
                    if self._is_gog_game_installed(item_path):
                        # Try to extract game ID from goggame-*.info file
                        game_id = self._get_game_id_from_dir(item_path)
                        if game_id:
                            installed_ids.append(game_id)

            logger.info(f"Found {len(installed_ids)} installed GOG games")
            return installed_ids

        except Exception as e:
            logger.error(f"Error checking GOG installed games: {e}")
            return []

    def _is_gog_game_installed(self, game_dir: str) -> bool:
        """Check if a directory contains an installed GOG game.
        
        .unifideck-id marker = 100% installed status.
        This marker is written ONLY after fully successful installation.
        """
        return os.path.exists(os.path.join(game_dir, '.unifideck-id'))

    def _get_game_id_from_dir(self, game_dir: str) -> Optional[str]:
        """Extract game ID from .unifideck-id marker file.
        
        Handles both old format (plain game ID) and new format (JSON with game_id).
        Only returns ID if marker exists (= 100% installed).
        """
        marker_path = os.path.join(game_dir, '.unifideck-id')
        if os.path.exists(marker_path):
            try:
                with open(marker_path, 'r') as f:
                    content = f.read().strip()
                    
                # Try to parse as JSON first (new format)
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        return data.get('game_id') or data.get('gameId')
                    elif isinstance(data, (str, int)):
                        # Old format: just the ID as JSON string/int
                        return str(data)
                except json.JSONDecodeError:
                    # Old format: plain text game ID
                    return content if content else None
                    
            except Exception as e:
                logger.warning(f"[GOG] Error reading .unifideck-id: {e}")
        return None

    def migrate_old_markers(self) -> Dict[str, Any]:
        """Migrate old .unifideck-id files to new JSON format with goggame data.
        
        Called during Force Sync to upgrade old installations.
        """
        migrated = 0
        skipped = 0
        
        if not os.path.exists(self.download_dir):
            return {'migrated': 0, 'skipped': 0}
        
        try:
            for item in os.listdir(self.download_dir):
                item_path = os.path.join(self.download_dir, item)
                if not os.path.isdir(item_path):
                    continue
                    
                marker_path = os.path.join(item_path, '.unifideck-id')
                if not os.path.exists(marker_path):
                    continue
                    
                try:
                    with open(marker_path, 'r') as f:
                        content = f.read().strip()
                    
                    # Check if already in new format
                    try:
                        data = json.loads(content)
                        if isinstance(data, dict) and 'game_id' in data:
                            skipped += 1
                            continue
                    except json.JSONDecodeError:
                        pass
                    
                    # Get game ID from old format
                    old_game_id = None
                    try:
                        data = json.loads(content)
                        if isinstance(data, (str, int)):
                            old_game_id = str(data)
                    except json.JSONDecodeError:
                        old_game_id = content
                    
                    if not old_game_id:
                        skipped += 1
                        continue
                    
                    # Build new marker with goggame info
                    new_data = {"game_id": old_game_id}
                    for d in [item_path, os.path.join(item_path, 'game')]:
                        if not os.path.exists(d):
                            continue
                        for f in os.listdir(d):
                            if f.startswith('goggame-') and f.endswith('.info'):
                                try:
                                    with open(os.path.join(d, f), 'r') as info_f:
                                        new_data = json.load(info_f)
                                        new_data['game_id'] = old_game_id
                                except Exception:
                                    pass
                                break
                    
                    with open(marker_path, 'w') as f:
                        json.dump(new_data, f, indent=2)
                    migrated += 1
                    logger.info(f"[GOG] Migrated marker for {item}")
                except Exception as e:
                    logger.warning(f"[GOG] Failed to migrate {item}: {e}")
        except Exception as e:
            logger.error(f"[GOG] Error during marker migration: {e}")
        
        logger.info(f"[GOG] Migration: {migrated} upgraded, {skipped} current")
        return {'migrated': migrated, 'skipped': skipped}

    def get_installed_game_info(self, game_id: str) -> Optional[Dict[str, str]]:
        """Get install path and executable for an installed GOG game.
        
        Checks both .unifideck-id marker and goggame-*.info files for robustness.
        """
        if not os.path.exists(self.download_dir):
            return None
            
        try:
            for item in os.listdir(self.download_dir):
                item_path = os.path.join(self.download_dir, item)
                if not os.path.isdir(item_path):
                    continue
                    
                # Method 1: Check .unifideck-id marker
                found_id = self._get_game_id_from_dir(item_path)
                if found_id == game_id:
                    exe_path = self._find_game_executable(item_path)
                    return {'install_path': item_path, 'executable': exe_path}
                
                # Method 2: Fallback to goggame-*.info files (for old/external installs)
                if found_id is None:
                    for search_dir in [item_path, os.path.join(item_path, 'game')]:
                        if not os.path.exists(search_dir):
                            continue
                        for f in os.listdir(search_dir):
                            if f.startswith('goggame-') and f.endswith('.info'):
                                info_id = f.replace('goggame-', '').replace('.info', '')
                                if info_id == game_id:
                                    exe_path = self._find_game_executable(item_path)
                                    logger.info(f"[GOG] Found {game_id} via goggame info fallback at {item_path}")
                                    return {'install_path': item_path, 'executable': exe_path}
        except Exception as e:
            logger.error(f"[GOG] Error getting installed game info for {game_id}: {e}")
        return None

    async def get_game_size(self, game_id: str, session=None) -> Optional[int]:
        """Get game download size using gogdl"""
        if not self.gogdl_bin:
            logger.error("[GOG] gogdl binary not available")
            return None
            
        # Ensure auth is synced
        self._ensure_auth_config()
        
        try:
            # Run: gogdl --auth-config-path ... info --platform linux [id]
            cmd = [
                self.gogdl_bin,
                '--auth-config-path', self.gogdl_config_path,
                'info',
                '--platform', 'linux',
                game_id
            ]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode != 0:
                # Try Windows platform if Linux fails
                cmd[4] = 'windows'
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                # Parse the last line which should be JSON
                output_lines = stdout.decode().strip().split('\n')
                # Find the JSON line
                for line in reversed(output_lines):
                    try:
                        data = json.loads(line)
                        if 'download_size' in data:
                            return data['download_size']
                    except json.JSONDecodeError:
                        continue
            
            return None
            
        except Exception as e:
            logger.error(f"[GOG] Error getting size for {game_id}: {e}")
            return None

    async def install_game(self, game_id: str, base_path: str = None, progress_callback=None) -> Dict[str, Any]:
        """Install GOG game using gogdl binary"""
        if not self.gogdl_bin:
            return {'success': False, 'error': 'gogdl binary not found'}
            
        # 1. Ensure Auth (refreshes token if needed)
        if not await self.is_available():
             return {'success': False, 'error': 'Not authenticated with GOG or token expired'}
             
        # Force sync fresh token to gogdl config
        if not self._ensure_auth_config():
             return {'success': False, 'error': 'Failed to configure GOG authentication'}

        # 2. Determine Install Path
        if not base_path:
            base_path = os.path.expanduser("~/GOG Games")
        
        # We need game title for the folder but gogdl creates its own folder name
        # We'll pass base_path and let gogdl create the game directory inside it
        os.makedirs(base_path, exist_ok=True)
        
        logger.info(f"[GOG] Starting installation of {game_id} via gogdl to {base_path}")

        # Re-running platform check properly
        platform = 'linux'
        folder_name = None
        
        info_cmd = [
            self.gogdl_bin, '--auth-config-path', self.gogdl_config_path,
            'info', '--platform', 'linux', game_id
        ]
        proc = await asyncio.create_subprocess_exec(*info_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            logger.info(f"[GOG] Linux version not found for {game_id}, trying Windows")
            platform = 'windows'
            info_cmd[4] = 'windows'
            proc = await asyncio.create_subprocess_exec(*info_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()
            
        if proc.returncode == 0:
            try:
                # Find JSON in output
                output_lines = stdout.decode().strip().split('\n')
                for line in reversed(output_lines):
                    try:
                        data = json.loads(line)
                        if 'folder_name' in data:
                            folder_name = data['folder_name']
                            logger.info(f"[GOG] Predicted folder name: {folder_name}")
                            break
                    except json.JSONDecodeError:
                        continue
            except Exception as e:
                logger.warning(f"[GOG] Could not parse folder name from info: {e}")
        
        # 4. Start Download
        # Command: gogdl ... download [id] --platform [plat] --path [path] --skip-dlcs
        
        cmd = [
            self.gogdl_bin,
            '--auth-config-path', self.gogdl_config_path,
            'download',
            game_id,
            '--platform', platform,
            '--path', base_path,
            '--skip-dlcs' 
        ]
        
        # Redirect stderr to stdout to capture logging output from gogdl
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        
        # 5. Monitor Progress
        # gogdl outputs logs to stderr/stdout depending on config.
        # Progress format (from progressbar.py):
        #   = Progress: 86.40 5645360434/6534344139, Running for: 00:01:30, ETA: 00:00:15
        #   = Downloaded: 123.45 MiB, Written: 456.78 MiB
        #   + Download - 12.34 MiB/s (raw) / 45.67 MiB/s (decompressed)
        
        # Track progress state
        current_progress = {
            'progress_percent': 0,
            'downloaded_bytes': 0,
            'total_bytes': 0,
            'speed_bps': 0.0,  # Speed in bytes/sec (download_manager expects this)
            'eta_seconds': 0,
            'phase_message': 'Starting download...'
        }
        
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
                
            line_str = line.decode().strip()
            if not line_str:
                continue
                
            # Log non-progress lines
            if 'Progress:' not in line_str and 'Download' not in line_str:
                if not line_str.startswith('[gogdl]'):
                    logger.info(f"[gogdl] {line_str}")
            
            if progress_callback:
                try:
                    # Parse main progress line: "= Progress: 86.40 5645360434/6534344139, Running for: 00:01:30, ETA: 00:00:15"
                    if 'Progress:' in line_str:
                        # Extract percentage and bytes
                        part = line_str.split('Progress:')[1].strip()
                        # "86.40 5645360434/6534344139, Running for: ..."
                        tokens = part.split()
                        if len(tokens) >= 2:
                            percent = float(tokens[0])
                            current_progress['progress_percent'] = percent
                            
                            # Parse bytes: "5645360434/6534344139,"
                            bytes_part = tokens[1].rstrip(',')
                            if '/' in bytes_part:
                                written, total = bytes_part.split('/')
                                current_progress['downloaded_bytes'] = int(written)
                                current_progress['total_bytes'] = int(total)
                        
                        # Parse ETA: "ETA: 00:00:15"
                        if 'ETA:' in line_str:
                            eta_part = line_str.split('ETA:')[1].strip()
                            # "00:00:15" or similar
                            eta_time = eta_part.split()[0] if eta_part else "00:00:00"
                            # Convert HH:MM:SS to seconds
                            try:
                                parts = eta_time.split(':')
                                if len(parts) == 3:
                                    h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                                    current_progress['eta_seconds'] = h * 3600 + m * 60 + s
                                elif len(parts) == 2:
                                    m, s = int(parts[0]), int(parts[1])
                                    current_progress['eta_seconds'] = m * 60 + s
                            except:
                                pass
                        
                        # Update phase message
                        current_progress['phase_message'] = f'Downloading... {percent:.1f}%'
                        
                        # Send progress update
                        await progress_callback(current_progress)
                    
                    # Parse download speed line: "+ Download - 12.34 MiB/s (raw) / ..."
                    if '+ Download' in line_str and 'MiB/s' in line_str:
                        # Extract speed: "12.34 MiB/s"
                        try:
                            # Find speed value before "MiB/s"
                            speed_match = line_str.split('Download')[1]
                            # "- 12.34 MiB/s (raw) / ..."
                            speed_part = speed_match.split('MiB/s')[0].strip()
                            # "- 12.34 " -> take last token
                            speed_tokens = speed_part.split()
                            if speed_tokens:
                                speed_mib = float(speed_tokens[-1])
                                # Convert MiB/s to bytes/sec (download_manager will convert to MB/s for display)
                                # 1 MiB = 1024 * 1024 bytes
                                current_progress['speed_bps'] = speed_mib * 1024 * 1024
                                
                                # Also send update when speed changes (so UI updates immediately)
                                await progress_callback(current_progress)
                        except:
                            pass
                        
                except Exception as e:
                    logger.debug(f"[GOG] Progress parse error: {e}")

        # Wait for finish
        await proc.wait()
        
        if proc.returncode != 0:
            logger.error(f"[GOG] gogdl failed with return code {proc.returncode}")
            # Cleanup partial files
            if folder_name:
                partial_path = os.path.join(base_path, folder_name)
                if os.path.exists(partial_path):
                    logger.info(f"[GOG] Cleaning up partial install at {partial_path}")
                    shutil.rmtree(partial_path, ignore_errors=True)
            return {'success': False, 'error': f'Installation failed (code {proc.returncode})'}
            
        # 6. Verify and Locate Game
        logger.info(f"[GOG] Verifying installation in {base_path}")
        found_path = None
        
        # Priority 1: Check predicted folder name (gogdl just created it)
        if folder_name:
            predicted_path = os.path.join(base_path, folder_name)
            if os.path.exists(predicted_path) and os.path.isdir(predicted_path):
                found_path = predicted_path
                logger.info(f"[GOG] Found game at predicted path: {found_path}")
        
        # Priority 2: Scan if predicted path failed
        if not found_path:
            logger.info("[GOG] Predicted path failed, scanning directory...")
            candidates = []
            try:
                for item in os.listdir(base_path):
                    item_path = os.path.join(base_path, item)
                    if os.path.isdir(item_path):
                        candidates.append(item)
                        # Check for goggame-*.info file (before we write marker)
                        for f in os.listdir(item_path):
                            if f.startswith('goggame-') and f.endswith('.info'):
                                info_id = f.replace('goggame-', '').replace('.info', '')
                                if info_id == game_id:
                                    found_path = item_path
                                    break
                        if found_path:
                            break
            except Exception as e:
                logger.error(f"[GOG] Error listing directories: {e}")
            
        if found_path:
            # Write .unifideck-id marker LAST with goggame info data
            # This marker = 100% installed status
            info_data = {"game_id": game_id}
            try:
                # Read goggame info if available
                for item in os.listdir(found_path):
                    if item.startswith('goggame-') and item.endswith('.info'):
                        info_file = os.path.join(found_path, item)
                        with open(info_file, 'r') as f:
                            info_data = json.load(f)
                            info_data['game_id'] = game_id  # Ensure our ID is present
                        break
                
                # Also check game/ subdirectory
                game_subdir = os.path.join(found_path, 'game')
                if 'name' not in info_data and os.path.exists(game_subdir):
                    for item in os.listdir(game_subdir):
                        if item.startswith('goggame-') and item.endswith('.info'):
                            info_file = os.path.join(game_subdir, item)
                            with open(info_file, 'r') as f:
                                info_data = json.load(f)
                                info_data['game_id'] = game_id
                            break
            except Exception as e:
                logger.warning(f"[GOG] Could not read goggame info: {e}")
            
            # Write marker (this marks 100% complete)
            marker_path = os.path.join(found_path, '.unifideck-id')
            try:
                with open(marker_path, 'w') as f:
                    json.dump(info_data, f, indent=2)
                logger.info(f"[GOG] Wrote .unifideck-id marker at {marker_path}")
            except Exception as e:
                logger.error(f"[GOG] Failed to write marker: {e}")
                # Cleanup on failure to write marker
                shutil.rmtree(found_path, ignore_errors=True)
                return {'success': False, 'error': 'Failed to complete installation'}
            
            logger.info(f"[GOG] Installation successful at {found_path}")
            return {
                'success': True,
                'install_path': found_path
            }
        else:
            logger.warning(f"[GOG] Could not locate game {game_id} in {base_path}. Candidates: {candidates if 'candidates' in locals() else 'unknown'}")
            # Cleanup any partial files
            if folder_name:
                partial_path = os.path.join(base_path, folder_name)
                if os.path.exists(partial_path):
                    shutil.rmtree(partial_path, ignore_errors=True)
            return {
                'success': False,
                'error': 'Installation completed but could not locate game directory'
            }

    async def _get_game_details(self, game_id: str, session=None):
        """Legacy compatibility method - returns dummy data to satisfy main.py"""
        # Return an object that looks like it has no installers, so is_multipart becomes False
        return {} 

    def _find_linux_installer(self, game_details):
        """Legacy compatibility method"""
        return []

    def _find_windows_installer(self, game_details):
        """Legacy compatibility method"""
        return []

    async def uninstall_game(self, game_id: str, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Uninstall game by removing its directory.
        
        Args:
            game_id: GOG game ID
            install_path: Optional install path from games.map (avoids filesystem scan)
        """
        # Use provided install_path or fall back to filesystem scan
        if install_path and os.path.exists(install_path):
            logger.info(f"[GOG] Using provided install path: {install_path}")
        else:
            info = self.get_installed_game_info(game_id)
            if not info:
                return {'success': False, 'error': 'Game not found in installed games'}
            install_path = info['install_path']
        
        try:
            if os.path.exists(install_path):
                shutil.rmtree(install_path)
                logger.info(f"[GOG] Uninstalled {game_id} from {install_path}")
                return {'success': True, 'message': f'Uninstalled from {install_path}'}
            else:
                return {'success': False, 'error': f'Install path does not exist: {install_path}'}
        except Exception as e:
            logger.error(f"[GOG] Error uninstalling {game_id}: {e}")
            return {'success': False, 'error': str(e)}

    def _find_game_executable(self, install_path: str) -> Optional[str]:
        """Find the game executable using goggame info or start.sh"""
        result = self._find_game_executable_with_workdir(install_path)
        if result:
            return result[0]
        return None
    
    def _find_game_executable_with_workdir(self, install_path: str) -> Optional[Tuple[str, str]]:
        """Find game executable and working directory from install path."""
        try:
            # Look for info file either in root or in 'game/' subdir
            info_file = None
            search_dirs = [install_path]
            
            # Check nested game/ folder first if it exists, as it's the likely payload root
            game_subdir = os.path.join(install_path, 'game')
            if os.path.exists(game_subdir) and os.path.isdir(game_subdir):
                search_dirs.insert(0, game_subdir)
                
            root_dir = install_path
            
            for d in search_dirs:
                if not os.path.exists(d): continue
                for item in os.listdir(d):
                    if item.startswith('goggame-') and item.endswith('.info'):
                        info_file = os.path.join(d, item)
                        root_dir = d # Update root dir to where info file was found
                        break
                if info_file:
                    break
            
            if info_file:
                try:
                    with open(info_file, 'r') as f:
                        data = json.load(f)
                        play_tasks = data.get('playTasks', [])
                        
                        # Find primary task
                        primary = None
                        for task in play_tasks:
                            if task.get('isPrimary'):
                                primary = task
                                break
                        
                        if primary:
                            exe_rel = primary.get('path')
                            work_rel = primary.get('workingDir', '')
                            
                            full_exe = os.path.join(root_dir, exe_rel)
                            full_work = os.path.join(root_dir, work_rel) if work_rel else os.path.dirname(full_exe)
                            
                            # Fix slashes for Linux if coming from Windows JSON
                            full_exe = full_exe.replace('\\', '/')
                            full_work = full_work.replace('\\', '/')
                            
                            if os.path.exists(full_exe):
                                logger.info(f"[GOG] Found EXE via info: {full_exe}")
                                return (full_exe, full_work)
                except Exception as e:
                    logger.warning(f"[GOG] Error reading info file: {e}")

            # PRIORITY 2: Look for 'start.sh' (Linux standard)
            # Check root and optional game/ subdir
            for d in search_dirs:
                if not os.path.exists(d): continue
                start_sh = os.path.join(d, 'start.sh')
                if os.path.exists(start_sh):
                    logger.info(f"[GOG] Found start.sh: {start_sh}")
                    return (start_sh, d)
                
            # PRIORITY 3: Legacy Heuristic (Windows Exe)
            for d in search_dirs:
                 if not os.path.exists(d): continue
                 for item in os.listdir(d):
                    if item.endswith('.exe') and item.lower() not in ['uninstall.exe', 'unins000.exe']:
                        return (os.path.join(d, item), d)

            return None

        except Exception as e:
            logger.error(f"[GOG] Error finding game executable: {e}", exc_info=True)
            return None
