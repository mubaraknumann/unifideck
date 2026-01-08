import decky  # Required for Decky Loader framework
import os
import sys
import logging
import asyncio
import binascii
import struct
import json
import aiohttp.web
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from urllib.parse import parse_qs

# Add plugin directory to Python path for local imports
DECKY_PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR")
if DECKY_PLUGIN_DIR:
    sys.path.insert(0, DECKY_PLUGIN_DIR)

# Import VDF utilities
from vdf_utils import load_shortcuts_vdf, save_shortcuts_vdf

# Import SteamGridDB client
try:
    from steamgriddb_client import SteamGridDBClient
    STEAMGRIDDB_AVAILABLE = True
except ImportError:
    STEAMGRIDDB_AVAILABLE = False

# Import Download Manager
from download_manager import get_download_queue, DownloadQueue

# Import Cloud Save Manager
from cloud_save_manager import CloudSaveManager

# Use Decky's logger for proper integration
logger = decky.logger

# Log import status
if not STEAMGRIDDB_AVAILABLE:
    logger.warning("SteamGridDB client not available")


# Global caches for legendary CLI results (performance optimization)
import time

_legendary_installed_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 30  # 30 second cache
}

_legendary_info_cache = {}  # Per-game info cache


class CDPOAuthMonitor:
    """Monitor Steam's CEF browser for OAuth authorization codes via Chrome DevTools Protocol"""

    def __init__(self, cef_port=8080):
        self.cef_url = f'http://127.0.0.1:{cef_port}/json'
        self.monitored_urls = set()

    async def monitor_for_oauth_code(self, expected_store='epic', timeout=300, poll_interval=0.5):
        """
        Monitor CEF pages for OAuth redirect URLs and extract authorization codes

        Args:
            expected_store: Only return codes for this store ('epic' or 'gog')
            timeout: Maximum time to monitor in seconds (default 5 minutes)
            poll_interval: How often to check CEF pages in seconds (default 0.5s)

        Returns:
            (code, store) tuple or (None, None) if timeout/error
        """
        import urllib.request
        from urllib.parse import urlparse, parse_qs
        import re
        import time

        start_time = time.time()
        logger.info("[CDP] Starting OAuth code monitoring...")

        while time.time() - start_time < timeout:
            try:
                # Get current CEF pages
                with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                    pages = json.loads(response.read().decode())

                for page in pages:
                    url = page.get('url', '')

                    # Skip already monitored URLs
                    if url in self.monitored_urls:
                        continue

                    self.monitored_urls.add(url)

                    # Check for OAuth patterns
                    if any(p in url.lower() for p in ['auth', 'login', 'code=', 'epiclogin', 'on_login_success', 'oauth', 'authorizationcode', '/id/api/redirect']):
                        logger.info(f"[CDP] OAuth page detected: {url[:80]}...")

                        # Special handling for Epic's redirect page (code in JSON body)
                        if '/id/api/redirect' in url or 'epicgames.com' in url.lower():
                            code = await self._extract_epic_code_from_page(url)
                            if code:
                                # Only return if it matches expected store
                                if expected_store == 'epic':
                                    logger.info(f"[CDP] ✓ Found epic authorization code from page content (matches expected: {expected_store})")
                                    return code, 'epic'
                                else:
                                    logger.warning(f"[CDP] Ignoring epic code (expected: {expected_store})")

                        # Try to extract code from URL
                        code, store = self._extract_code(url)
                        if code:
                            # Only return if it matches expected store
                            if store == expected_store:
                                logger.info(f"[CDP] ✓ Found {store} authorization code (matches expected: {expected_store})")
                                return code, store
                            else:
                                logger.warning(f"[CDP] Ignoring {store} code (expected: {expected_store})")

            except Exception as e:
                logger.debug(f"[CDP] Polling error (normal): {e}")

            await asyncio.sleep(poll_interval)

        logger.warning("[CDP] OAuth monitoring timeout - no code found")
        return None, None

    async def close_page_by_url(self, url_pattern: str):
        """Close browser page matching URL pattern via CDP"""
        import urllib.request

        try:
            # Get current CEF pages
            with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                pages = json.loads(response.read().decode())

            # Find page matching URL pattern
            for page in pages:
                if url_pattern in page.get('url', ''):
                    page_id = page.get('id')
                    ws_url = page.get('webSocketDebuggerUrl')

                    if ws_url:
                        logger.info(f"[CDP] Closing page via CDP: {page.get('url', '')[:80]}...")

                        import websockets

                        async with websockets.connect(ws_url, ping_interval=None) as websocket:
                            await websocket.send(json.dumps({
                                'id': 1,
                                'method': 'Page.close',
                                'params': {}
                            }))
                            logger.info(f"[CDP] ✓ Page close command sent")
                            return True

            logger.warning(f"[CDP] No page found matching: {url_pattern}")
            return False

        except Exception as e:
            logger.error(f"[CDP] Error closing page: {e}")
            return False

    async def clear_cookies_for_domain(self, domain: str):
        """Clear browser cookies for specific domain via CDP"""
        import urllib.request

        try:
            logger.info(f"[CDP] Clearing cookies for domain: {domain}")

            # Get any CEF page to connect to CDP
            with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                pages = json.loads(response.read().decode())

            if not pages:
                logger.error("[CDP] No pages available for CDP connection")
                return False

            # Use first available page
            ws_url = pages[0].get('webSocketDebuggerUrl')
            if not ws_url:
                logger.error("[CDP] No WebSocket URL available")
                return False

            # Connect and clear cookies
            import websockets

            async with websockets.connect(ws_url, ping_interval=None) as websocket:
                # Clear cookies for domain
                await websocket.send(json.dumps({
                    'id': 1,
                    'method': 'Network.clearBrowserCookies',
                    'params': {}
                }))

                response_text = await asyncio.wait_for(websocket.recv(), timeout=5)
                logger.info(f"[CDP] ✓ Cleared browser cookies for {domain}")
                return True

        except Exception as e:
            logger.error(f"[CDP] Error clearing cookies: {e}")
            return False

    async def _extract_epic_code_from_page(self, url):
        """Extract authorizationCode from browser page via CDP WebSocket"""
        import urllib.request
        import re

        try:
            logger.info(f"[CDP] Getting page details for: {url[:80]}...")

            # Get page info from CDP to find WebSocket debugger URL
            with urllib.request.urlopen(self.cef_url, timeout=2) as response:
                pages = json.loads(response.read().decode())

            # Find the page matching this URL
            target_page = None
            for page in pages:
                if url in page.get('url', ''):
                    target_page = page
                    break

            if not target_page or 'webSocketDebuggerUrl' not in target_page:
                logger.error(f"[CDP] Could not find page or WebSocket URL for: {url[:80]}")
                return None

            ws_url = target_page['webSocketDebuggerUrl']
            logger.info(f"[CDP] Connecting to page via WebSocket...")

            # Connect via WebSocket and get page content
            import websockets

            async with websockets.connect(ws_url, ping_interval=None) as websocket:
                # Send Runtime.evaluate command to get page text content
                await websocket.send(json.dumps({
                    'id': 1,
                    'method': 'Runtime.evaluate',
                    'params': {
                        'expression': 'document.body.innerText',
                        'returnByValue': True
                    }
                }))

                # Wait for response
                response_text = await asyncio.wait_for(websocket.recv(), timeout=5)
                response_data = json.loads(response_text)

                # Extract the page content from CDP response
                if 'result' in response_data and 'result' in response_data['result']:
                    page_content = response_data['result']['result'].get('value', '')
                    logger.info(f"[CDP] Got page content from browser: {len(page_content)} chars")

                    # Look for authorizationCode in the JSON content
                    match = re.search(r'"authorizationCode"\s*:\s*"([^"]+)"', page_content)
                    if match:
                        code = match.group(1)
                        logger.info(f"[CDP] ✓ Extracted authorizationCode from browser page")
                        return code

                    logger.info(f"[CDP] No authorizationCode in page content (first 200 chars): {page_content[:200]}")
                    return None
                else:
                    logger.error(f"[CDP] Unexpected response format: {response_data}")
                    return None

        except Exception as e:
            logger.error(f"[CDP] Error extracting Epic code via WebSocket: {e}")
            return None

    def _extract_code(self, url):
        """Extract OAuth code from URL"""
        import re
        from urllib.parse import urlparse, parse_qs

        # Epic style (check first - more specific)
        if 'authorizationCode=' in url:
            match = re.search(r'authorizationCode=([^&\s]+)', url)
            if match:
                return match.group(1), 'epic'

        # Amazon style - looks for openid.oa2.authorization_code in URL
        if 'amazon.com' in url.lower() and 'openid.oa2.authorization_code=' in url:
            match = re.search(r'openid\.oa2\.authorization_code=([^&\s]+)', url)
            if match:
                return match.group(1), 'amazon'

        # GOG style
        if 'code=' in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'code' in params:
                return params['code'][0], 'gog'

        return None, None


@dataclass
class Game:
    """Represents a game from any store"""
    id: str
    title: str
    store: str  # 'steam', 'epic', 'gog'
    is_installed: bool = False
    cover_image: Optional[str] = None
    install_path: Optional[str] = None
    executable: Optional[str] = None
    app_id: Optional[int] = None  # For shortcuts.vdf (our generated ID)
    steam_app_id: Optional[int] = None  # Real Steam appId for ProtonDB lookups

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Steam App ID Cache - maps shortcut appId to real Steam appId for ProtonDB lookups
# Stored as JSON file in plugin data directory
STEAM_APPID_CACHE_FILE = "steam_appid_cache.json"


def get_steam_appid_cache_path() -> Path:
    """Get path to steam_app_id cache file"""
    if DECKY_PLUGIN_DIR:
        return Path(DECKY_PLUGIN_DIR) / STEAM_APPID_CACHE_FILE
    return Path.home() / ".unifideck" / STEAM_APPID_CACHE_FILE


def load_steam_appid_cache() -> Dict[int, int]:
    """Load steam_app_id mappings from cache file. Returns {shortcut_appid: steam_appid}"""
    cache_path = get_steam_appid_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                data = json.load(f)
                # Convert string keys back to int
                return {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.error(f"Error loading steam_appid cache: {e}")
    return {}


def save_steam_appid_cache(cache: Dict[int, int]) -> bool:
    """Save steam_app_id mappings to cache file"""
    cache_path = get_steam_appid_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.info(f"Saved {len(cache)} steam_app_id mappings to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving steam_appid cache: {e}")
        return False


# Shortcuts Registry - maps game launch options to appid for reconciliation after plugin reinstall
# Stored in user data directory (survives plugin uninstall/reinstall)
SHORTCUTS_REGISTRY_FILE = "shortcuts_registry.json"


def get_shortcuts_registry_path() -> Path:
    """Get path to shortcuts registry file (in user data, not plugin dir)"""
    return Path.home() / ".local" / "share" / "unifideck" / SHORTCUTS_REGISTRY_FILE


def load_shortcuts_registry() -> Dict[str, Dict]:
    """Load shortcuts registry. Returns {launch_options: {appid, appid_unsigned, title, created}}"""
    registry_path = get_shortcuts_registry_path()
    try:
        if registry_path.exists():
            with open(registry_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading shortcuts registry: {e}")
    return {}


def save_shortcuts_registry(registry: Dict[str, Dict]) -> bool:
    """Save shortcuts registry to file"""
    registry_path = get_shortcuts_registry_path()
    try:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        with open(registry_path, 'w') as f:
            json.dump(registry, f, indent=2)
        logger.info(f"Saved {len(registry)} entries to shortcuts registry")
        return True
    except Exception as e:
        logger.error(f"Error saving shortcuts registry: {e}")
        return False


def register_shortcut(launch_options: str, appid: int, title: str) -> bool:
    """Register a shortcut's appid for future reconciliation"""
    registry = load_shortcuts_registry()
    
    # Calculate unsigned appid for logging/debugging
    appid_unsigned = appid if appid >= 0 else appid + 2**32
    
    registry[launch_options] = {
        'appid': appid,
        'appid_unsigned': appid_unsigned,
        'title': title,
        'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    
    logger.debug(f"Registered shortcut: {launch_options} -> appid={appid} (unsigned={appid_unsigned})")
    return save_shortcuts_registry(registry)


def get_registered_appid(launch_options: str) -> Optional[int]:
    """Get the registered appid for a game, or None if not registered"""
    registry = load_shortcuts_registry()
    entry = registry.get(launch_options)
    return entry.get('appid') if entry else None


# Game Size Cache - stores download sizes for instant button loading
# Pre-populated during sync, read during get_game_info
GAME_SIZES_CACHE_FILE = "game_sizes.json"


def get_game_sizes_cache_path() -> Path:
    """Get path to game sizes cache file (in user data, not plugin dir)"""
    return Path.home() / ".local" / "share" / "unifideck" / GAME_SIZES_CACHE_FILE


def load_game_sizes_cache() -> Dict[str, Dict]:
    """Load game sizes cache. Returns {store:game_id: {size_bytes, updated}}"""
    cache_path = get_game_sizes_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading game sizes cache: {e}")
    return {}


def save_game_sizes_cache(cache: Dict[str, Dict]) -> bool:
    """Save game sizes cache to file"""
    cache_path = get_game_sizes_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)
        logger.debug(f"Saved {len(cache)} entries to game sizes cache")
        return True
    except Exception as e:
        logger.error(f"Error saving game sizes cache: {e}")
        return False


def cache_game_size(store: str, game_id: str, size_bytes: int) -> bool:
    """Cache a game's download size"""
    cache = load_game_sizes_cache()
    cache_key = f"{store}:{game_id}"
    cache[cache_key] = {
        'size_bytes': size_bytes,
        'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    return save_game_sizes_cache(cache)


def get_cached_game_size(store: str, game_id: str) -> Optional[int]:
    """Get cached game size, or None if not cached"""
    cache = load_game_sizes_cache()
    cache_key = f"{store}:{game_id}"
    entry = cache.get(cache_key)
    return entry.get('size_bytes') if entry else None


class BackgroundSizeFetcher:
    """Background service to fetch game sizes asynchronously without blocking sync.
    
    - Runs in background (fire-and-forget from sync)
    - Fetches all pending sizes in parallel (30 concurrent)
    - Persists progress to game_sizes.json (survives restarts)
    - Starts automatically on plugin load if pending games exist
    """
    
    def __init__(self, epic_connector, gog_connector, amazon_connector=None):
        self.epic = epic_connector
        self.gog = gog_connector
        self.amazon = amazon_connector
        self._running = False
        self._task = None
        self._pending_games = []  # List of (store, game_id) tuples
        
    def queue_games(self, games: List):
        """Queue games for background size fetching.
        
        Args:
            games: List of Game objects with 'store' and 'id' attributes
        """
        cache = load_game_sizes_cache()
        
        for game in games:
            cache_key = f"{game.store}:{game.id}"
            if cache_key not in cache:
                self._pending_games.append((game.store, game.id))
                # Mark as pending in cache (null value)
                cache[cache_key] = None
        
        save_game_sizes_cache(cache)
        logger.info(f"[SizeService] Queued {len(self._pending_games)} games for size fetching")
    
    def start(self):
        """Start background fetching (non-blocking)"""
        if self._running:
            logger.debug("[SizeService] Already running")
            return
        
        # Load pending from cache if not already queued
        if not self._pending_games:
            cache = load_game_sizes_cache()
            self._pending_games = [
                tuple(k.split(':', 1)) for k, v in cache.items() 
                if v is None and ':' in k
            ]
        
        if not self._pending_games:
            logger.debug("[SizeService] No pending games, not starting")
            return
        
        logger.info(f"[SizeService] Starting background fetch for {len(self._pending_games)} games")
        self._running = True
        self._task = asyncio.create_task(self._fetch_all())
    
    def stop(self):
        """Stop background fetching"""
        if self._task and not self._task.done():
            self._task.cancel()
        self._running = False
        logger.info("[SizeService] Stopped")
    
    async def _fetch_all(self):
        """Fetch all pending sizes in parallel"""
        try:
            import aiohttp
            import ssl
            
            # Create shared session for GOG reuse (critical for performance)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            
            async with aiohttp.ClientSession(connector=connector) as session:
                semaphore = asyncio.Semaphore(30)
                
                async def fetch_one(store: str, game_id: str):
                    async with semaphore:
                        try:
                            if store == 'epic':
                                size_bytes = await self.epic.get_game_size(game_id)
                            elif store == 'gog':
                                size_bytes = await self.gog.get_game_size(game_id, session=session)
                            elif store == 'amazon' and self.amazon:
                                size_bytes = await self.amazon.get_game_size(game_id)
                            else:
                                return (store, game_id, None)
                            
                            if size_bytes and size_bytes > 0:
                                # Update cache immediately (persist progress)
                                cache = load_game_sizes_cache()
                                cache[f"{store}:{game_id}"] = {
                                    'size_bytes': size_bytes,
                                    'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                                }
                                save_game_sizes_cache(cache)
                                logger.debug(f"[SizeService] Cached {store}:{game_id} = {size_bytes}")
                                return (store, game_id, size_bytes)
                            else:
                                return (store, game_id, None)
                        except Exception as e:
                            logger.debug(f"[SizeService] Error fetching {store}:{game_id}: {e}")
                            return (store, game_id, None)
                
                # Fire all at once
                tasks = [fetch_one(store, gid) for store, gid in self._pending_games]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success = sum(1 for r in results if isinstance(r, tuple) and r[2] is not None)
            logger.info(f"[SizeService] Complete: {success}/{len(self._pending_games)} sizes cached")
            
        except asyncio.CancelledError:
            logger.info("[SizeService] Cancelled")
        except Exception as e:
            logger.error(f"[SizeService] Error: {e}")
        finally:
            self._running = False
            self._pending_games = []


class SyncProgress:
    """Track library sync progress with phase-based percentage tracking.
    
    Each sync phase has an allocated percentage range for smooth progress bar updates.
    """
    
    # Phase percentage allocations: (start_pct, end_pct)
    PHASE_RANGES = {
        'idle': (0, 0),
        'fetching': (0, 10),
        'checking_installed': (10, 20),
        'syncing': (20, 40),
        'sgdb_lookup': (40, 55),
        'checking_artwork': (55, 60),
        'artwork': (60, 95),
        'proton_setup': (95, 98),
        'complete': (100, 100),
        'error': (100, 100),
        'cancelled': (100, 100)
    }
    
    def __init__(self):
        self.total_games = 0
        self.synced_games = 0
        self.current_game = ""
        self.status = "idle"  # idle, fetching, checking_installed, syncing, sgdb_lookup, checking_artwork, artwork, proton_setup, complete, error, cancelled
        self.error = None

        # Artwork-specific tracking
        self.artwork_total = 0
        self.artwork_synced = 0
        self.current_phase = "sync"  # "sync" or "artwork"

        # Lock for thread-safe updates during parallel downloads
        self._lock = asyncio.Lock()

    async def increment_artwork(self, game_title: str) -> int:
        """Thread-safe artwork counter increment"""
        async with self._lock:
            self.artwork_synced += 1
            self.current_game = f"Downloaded artwork {self.artwork_synced}/{self.artwork_total} ({game_title})"
            return self.artwork_synced

    def _calculate_progress(self) -> int:
        """Calculate progress based on current phase and its percentage allocation."""
        phase_range = self.PHASE_RANGES.get(self.status, (0, 0))
        start_pct, end_pct = phase_range
        
        # For artwork phase, use artwork counters for sub-progress within the phase range
        if self.status == 'artwork' and self.artwork_total > 0:
            sub_progress = self.artwork_synced / self.artwork_total
            return int(start_pct + (end_pct - start_pct) * sub_progress)
        
        # For other phases, return the start of the phase range
        # (phases transition quickly, so showing phase start is sufficient)
        return start_pct

    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': True,
            'total_games': self.total_games,
            'synced_games': self.synced_games,
            'current_game': self.current_game,
            'status': self.status,
            'progress_percent': self._calculate_progress(),
            'error': self.error,
            # Artwork fields
            'artwork_total': self.artwork_total,
            'artwork_synced': self.artwork_synced,
            'current_phase': self.current_phase
        }


class ShortcutsManager:
    """Manages Steam's shortcuts.vdf file for non-Steam games"""

    def __init__(self, steam_path: Optional[str] = None):
        self.steam_path = steam_path or self._find_steam_path()
        self.shortcuts_path = self._find_shortcuts_vdf()
        logger.info(f"Shortcuts path: {self.shortcuts_path}")

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
        """Find shortcuts.vdf file - use most recently active user"""
        if not self.steam_path:
            return None

        userdata_path = os.path.join(self.steam_path, "userdata")
        if not os.path.exists(userdata_path):
            return None

        # Find user directories (sorted by most recent activity)
        user_dirs = []
        for d in os.listdir(userdata_path):
            if d.isdigit():
                dir_path = os.path.join(userdata_path, d)
                mtime = os.path.getmtime(dir_path)
                user_dirs.append((d, mtime))

        if not user_dirs:
            return None

        # Use most recently active user (highest mtime)
        user_dirs.sort(key=lambda x: x[1], reverse=True)
        active_user = user_dirs[0][0]

        shortcuts_path = os.path.join(userdata_path, active_user, "config", "shortcuts.vdf")
        logger.info(f"Using shortcuts.vdf for user {active_user}: {shortcuts_path}")

        return shortcuts_path

    async def _update_game_map(self, store: str, game_id: str, exe_path: str, work_dir: str):
        """Update the dynamic games map file"""
        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
        os.makedirs(os.path.dirname(map_file), exist_ok=True)
        
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
        
        with open(map_file, 'w') as f:
            f.writelines(lines)
            
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

    def _is_in_game_map(self, store: str, game_id: str) -> bool:
        """Check if game is registered in games.map (fast, authoritative for Unifideck-installed games)
        
        This is the primary source of truth for installation status because games.map
        is updated immediately when a game is installed, regardless of install location.
        
        Args:
            store: Store name ('epic' or 'gog')
            game_id: Game ID
            
        Returns:
            True if game is in games.map (installed via Unifideck)
        """
        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
        if not os.path.exists(map_file):
            return False
        
        key = f"{store}:{game_id}"
        try:
            with open(map_file, 'r') as f:
                for line in f:
                    if line.startswith(f"{key}|"):
                        return True
        except Exception as e:
            logger.debug(f"[GameMap] Error checking games.map: {e}")
        return False

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
            else:
                logger.debug(f"[Reconcile] No orphaned entries found: {kept} entries all valid")
        
        except Exception as e:
            logger.error(f"[Reconcile] Error reconciling games.map: {e}")
            return {'removed': 0, 'kept': kept, 'entries_removed': [], 'error': str(e)}
        
        return {'removed': removed, 'kept': kept, 'entries_removed': entries_removed}

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

    async def mark_installed(self, game_id: str, store: str, install_path: str, exe_path: str = None) -> bool:
        """Mark a game as installed in shortcuts.vdf (Dynamic Launch)"""
        try:
            logger.info(f"Marking {game_id} ({store}) as installed")
            logger.info(f"[MarkInstalled] Received: exe_path='{exe_path}', install_path='{install_path}'")
            
            # 1. Update dynamic map file (No Steam restart needed)
            work_dir = os.path.dirname(exe_path) if exe_path else install_path
            await self._update_game_map(store, game_id, exe_path or "", work_dir)

            # 2. Update shortcut to point to dynamic launcher
            shortcuts_data = await self.read_shortcuts()
            shortcuts = shortcuts_data.get('shortcuts', {})
            
            # Find existing shortcut by LaunchOptions (unquoted, as set by add_game)
            target_launch_options = f"{store}:{game_id}"  # No quotes!
            target_shortcut = None
            
            for s in shortcuts.values():
                opts = s.get('LaunchOptions', '')
                if target_launch_options in opts:
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
        """Read shortcuts.vdf file"""
        if not self.shortcuts_path:
            logger.warning("shortcuts.vdf path not found, returning empty dict")
            return {"shortcuts": {}}

        try:
            data = load_shortcuts_vdf(self.shortcuts_path)
            logger.info(f"Loaded {len(data.get('shortcuts', {}))} shortcuts")
            return data
        except Exception as e:
            logger.error(f"Error reading shortcuts.vdf: {e}")
            return {"shortcuts": {}}

    async def write_shortcuts(self, shortcuts: Dict[str, Any]) -> bool:
        """Write shortcuts.vdf file"""
        if not self.shortcuts_path:
            logger.error("Cannot write shortcuts.vdf: path not found")
            return False

        try:
            # Ensure parent directory exists
            os.makedirs(os.path.dirname(self.shortcuts_path), exist_ok=True)

            success = save_shortcuts_vdf(self.shortcuts_path, shortcuts)
            if success:
                logger.info(f"Wrote {len(shortcuts.get('shortcuts', {}))} shortcuts to file")
            return success
        except Exception as e:
            logger.error(f"Error writing shortcuts.vdf: {e}")
            return False

    async def add_game(self, game: Game, launcher_script: str) -> bool:
        """Add game to shortcuts.vdf"""
        try:
            shortcuts = await self.read_shortcuts()

            # Check if game already exists (duplicate detection)
            target_launch_options = f'{game.store}:{game.id}'
            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                if shortcut.get('LaunchOptions') == target_launch_options:
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
                if launch.startswith('epic:') or launch.startswith('gog:') or launch.startswith('amazon:'):
                    # Check if we should manage this store
                    store_prefix = launch.split(':', 1)[0]
                    if valid_stores is not None and store_prefix not in valid_stores:
                        continue

                    # If this game no longer exists in current library, it's orphaned
                    if launch not in current_launch_options:
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
                    return {'added': 0, 'skipped': skipped, 'removed': removed_count, 'reclaimed': 0, 'error': 'Failed to write shortcuts.vdf'}

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
            to_remove = []
            
            for idx in list(shortcuts["shortcuts"].keys()):
                shortcut = shortcuts["shortcuts"][idx]
                launch = shortcut.get('LaunchOptions', '')
                exe_path_current = shortcut.get('Exe', '').strip('"')

                # Only touch Unifideck shortcuts (epic: or gog:)
                if launch.startswith('epic:') or launch.startswith('gog:') or launch.startswith('amazon:'):
                    # Check if we should manage this store
                    store_prefix = launch.split(':', 1)[0]
                    if valid_stores is not None and store_prefix not in valid_stores:
                        continue

                    if launch not in current_launch_options:
                        # Orphaned - game no longer in library
                        logger.debug(f"Removing orphaned shortcut: {shortcut.get('AppName')} ({launch})")
                        to_remove.append(idx)
                        removed_count += 1
                    else:
                        # Existing game - update it with current data
                        game = games_by_launch_opts.get(launch)
                        if game:
                            # Update shortcut fields
                            shortcut['AppName'] = game.title
                            shortcut['exe'] = launcher_script
                            shortcut['LaunchOptions'] = launch
                            
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
            
            # Load shortcuts registry for reconciliation
            shortcuts_registry = load_shortcuts_registry()

            # STEP 4: Find next available index
            existing_indices = [int(k) for k in shortcuts.get("shortcuts", {}).keys() if k.isdigit()]
            next_index = max(existing_indices, default=-1) + 1

            # STEP 5: Add NEW games only (those not already in shortcuts) with reconciliation
            added = 0
            reclaimed = 0

            for game in games:
                target_launch_options = f'{game.store}:{game.id}'
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
                    return {'added': 0, 'updated': 0, 'removed': 0, 'reclaimed': 0, 'error': 'Failed to write shortcuts.vdf'}

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
                if target_launch_options in s.get('LaunchOptions', ''):
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
                if shortcut.get('LaunchOptions') == target_launch_options:
                    del shortcuts["shortcuts"][idx]
                    logger.info(f"Removed {game_id} from shortcuts")
                    return await self.write_shortcuts(shortcuts)

            logger.warning(f"Game {game_id} not found in shortcuts")
            return False

        except Exception as e:
            logger.error(f"Error removing game: {e}")
            return False


class EpicConnector:
    """Handles Epic Games Store via legendary CLI"""

    def __init__(self, plugin_dir: Optional[str] = None, plugin_instance=None):
        self.plugin_dir = plugin_dir
        self.plugin_instance = plugin_instance  # Reference to parent Plugin for auto-sync
        self.legendary_bin = self._find_legendary()
        logger.info(f"Legendary binary: {self.legendary_bin}")

    def _find_legendary(self) -> Optional[str]:
        """Find legendary executable - checks bundled binary first, then system"""
        import shutil

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
            monitor = CDPOAuthMonitor()
            code, store = await monitor.monitor_for_oauth_code(expected_store='epic', timeout=300)

            if code and store == 'epic':
                logger.info(f"[EPIC] Auto-captured authorization code, completing auth...")
                result = await self.complete_auth(code)
                if result['success']:
                    logger.info("[EPIC] ✓ Authentication completed automatically!")

                    # Close the auth popup window
                    await monitor.close_page_by_url('epicgames.com')

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
            return None

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
                    import re
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
                            exe_path = os.path.join(install_path, executable)
                            logger.info(f"[Epic] Successfully installed {game_id} to {install_path}")
                            logger.info(f"[Epic] Executable: {exe_path}")
                            return {
                                'success': True,
                                'install_path': install_path,
                                'exe_path': exe_path,
                                'message': f'Successfully installed {game_id}'
                            }
                        elif install_path:
                            # Have install path but no executable info
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


class AmazonConnector:
    """Handles Amazon Games via nile CLI"""

    def __init__(self, plugin_dir: Optional[str] = None, plugin_instance=None):
        self.plugin_dir = plugin_dir
        self.plugin_instance = plugin_instance  # Reference to parent Plugin for auto-sync
        self.nile_bin = self._find_nile()
        self._pending_login_data = None  # Store login data during OAuth flow
        logger.info(f"Nile binary: {self.nile_bin}")

    def _find_nile(self) -> Optional[str]:
        """Find nile executable - checks bundled binary first, then system"""
        import shutil

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
            return None

    async def get_library(self) -> List[Game]:
        """Get Amazon Games library via nile"""
        if not self.nile_bin:
            logger.warning("[Amazon] Nile CLI not found")
            return None

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
                
                # Get product details for metadata
                product_detail = product.get('productDetail', {})
                details = product_detail.get('details', {})
                
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
                import re
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
                    import re
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


class GOGAPIClient:
    """Handles GOG via direct API calls using OAuth"""

    # OAuth constants
    BASE_URL = "https://embed.gog.com"
    AUTH_URL = "https://auth.gog.com"
    CLIENT_ID = "46899977096215655"
    CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
    REDIRECT_URI = "https://embed.gog.com/on_login_success?origin=client"  # GOG's registered redirect URI

    def __init__(self, plugin_instance=None):
        self.plugin_instance = plugin_instance  # Reference to parent Plugin for auto-sync
        self.token_file = os.path.expanduser("~/.config/unifideck/gog_token.json")
        self.download_dir = os.path.expanduser("~/GOG Games")
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
        except Exception as e:
            logger.error(f"Error saving GOG tokens: {e}")

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
            logger.info("[GOG] Requesting: GET https://embed.gog.com/userData.json")

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(
                    'https://embed.gog.com/userData.json',
                    headers={'Authorization': f'Bearer {self.access_token}'}
                ) as response:
                    logger.info(f"[GOG] Response status: {response.status}")

                    if response.status == 200:
                        data = await response.text()
                        logger.info(f"[GOG] Response data: {data[:100]}")
                        logger.info("[GOG] Status: Connected (authenticated)")
                        return True
                    elif response.status == 401:
                        logger.warning("[GOG] Token expired (401), attempting refresh")
                        return await self._refresh_access_token()
                    else:
                        error_text = await response.text()
                        logger.warning(f"[GOG] Auth check failed (status: {response.status})")
                        logger.warning(f"[GOG] Response: {error_text[:200]}")
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
                    f'https://auth.gog.com/token?client_id=46899977096215655&client_secret=9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9&grant_type=refresh_token&refresh_token={self.refresh_token}'
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
        # GOG OAuth client credentials
        client_id = "46899977096215655"
        redirect_uri = "https://embed.gog.com/on_login_success?origin=client"

        auth_url = f"https://auth.gog.com/auth?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&layout=client2"

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

                    # Close the auth popup window
                    await monitor.close_page_by_url('gog.com')

                    # Auto-sync library after successful auth
                    if self.plugin_instance:
                        logger.info("[GOG] Starting automatic library sync...")
                        await self.plugin_instance.sync_libraries(fetch_artwork=False)
                        logger.info("[GOG] ✓ Library sync completed!")
                else:
                    logger.error(f"[GOG] Auto-auth failed: {result.get('error')}")
            else:
                # Better error message for timeout
                logger.error("[GOG] CDP monitoring timeout - user may have closed popup or not completed login")
                logger.error("[GOG] Please try authenticating again and complete the login in the popup window")
        except Exception as e:
            logger.error(f"[GOG] Error in background auth monitor: {e}", exc_info=True)

    async def complete_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete GOG OAuth flow with authorization code"""
        try:
            import aiohttp
            import ssl

            # GOG OAuth credentials
            client_id = "46899977096215655"
            client_secret = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
            redirect_uri = "https://embed.gog.com/on_login_success?origin=client"

            # Create SSL context that doesn't verify certificates (needed on Steam Deck)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            # Exchange authorization code for tokens
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    f'https://auth.gog.com/token?client_id={client_id}&client_secret={client_secret}&grant_type=authorization_code&code={auth_code}&redirect_uri={redirect_uri}'
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._save_tokens(data['access_token'], data['refresh_token'])
                        logger.info("GOG authentication successful")
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

    async def _exchange_code_for_token(self, code: str) -> Dict[str, Any]:
        """Exchange authorization code for access token"""
        try:
            import aiohttp

            token_url = f"{self.AUTH_URL}/token"
            data = {
                'client_id': self.CLIENT_ID,
                'client_secret': self.CLIENT_SECRET,
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': self.REDIRECT_URI
            }

            logger.info(f"[GOG] Exchanging authorization code for token...")
            async with aiohttp.ClientSession() as session:
                async with session.post(token_url, data=data) as response:
                    if response.status == 200:
                        result = await response.json()
                        access_token = result.get('access_token')
                        refresh_token = result.get('refresh_token')

                        # Save tokens
                        self._save_tokens(access_token, refresh_token)

                        logger.info("[GOG] Tokens obtained and saved")
                        return {'success': True}
                    else:
                        error_text = await response.text()
                        logger.error(f"[GOG] Token exchange failed: {response.status} - {error_text}")
                        return {'success': False, 'error': f'Status {response.status}: {error_text}'}

        except Exception as e:
            logger.error(f"[GOG] Token exchange error: {e}", exc_info=True)
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
        """Check if a directory contains an installed GOG game"""
        indicators = ['start.sh', 'gameinfo', 'support']

        try:
            # Method 1: Check for Unifideck marker file (most reliable)
            if os.path.exists(os.path.join(game_dir, '.unifideck-id')):
                return True

            for item in os.listdir(game_dir):
                if item.startswith('goggame-') and item.endswith('.info'):
                    return True
                if item in indicators:
                    return True
        except Exception:
            pass

        return False

    def _get_game_id_from_dir(self, game_dir: str) -> Optional[str]:
        """Extract GOG game ID from directory"""
        try:
            # Method 1: Check for Unifideck marker file (most reliable)
            marker_path = os.path.join(game_dir, '.unifideck-id')
            if os.path.exists(marker_path):
                with open(marker_path, 'r') as f:
                    return f.read().strip()

            for item in os.listdir(game_dir):
                if item.startswith('goggame-') and item.endswith('.info'):
                    # Extract ID from filename: goggame-{id}.info
                    game_id = item.replace('goggame-', '').replace('.info', '')
                    return game_id
        except Exception:
            pass
        return None

    def get_installed_game_info(self, game_id: str) -> Optional[Dict[str, str]]:
        """Get install path and executable for an installed GOG game
        
        Returns:
            Dict with 'install_path' and 'executable' keys, or None if not found
        """
        if not os.path.exists(self.download_dir):
            return None
            
        try:
            for item in os.listdir(self.download_dir):
                item_path = os.path.join(self.download_dir, item)
                if os.path.isdir(item_path):
                    found_id = self._get_game_id_from_dir(item_path)
                    if found_id == game_id:
                        # Found the game directory
                        exe_path = self._find_game_executable(item_path)
                        return {
                            'install_path': item_path,
                            'executable': exe_path
                        }
        except Exception as e:
            logger.error(f"[GOG] Error getting installed game info for {game_id}: {e}")
        return None

    async def get_game_size(self, game_id: str, session=None) -> Optional[int]:
        """Get game download size for GOG game using GOG API
        
        Fetches game details and calculates size from installers.
        - Prefers English language installers
        - Platform priority: Linux > Windows > Mac
        - Excludes patches (only base installers)

        Args:
            game_id: GOG game ID

        Returns:
            Download size in bytes, or None if unable to determine
        """
        try:
            if not self.access_token:
                logger.debug(f"[GOG] No access token for size lookup of {game_id}")
                return None
            
            # Get game details from GOG API
            game_details = await self._get_game_details(game_id, session=session)
            if not game_details:
                return None
            
            downloads = game_details.get('downloads', {})
            
            # Collect installers by platform, preferring English
            platform_installers = {'linux': [], 'windows': [], 'mac': []}
            
            # Downloads is a list of [language, {platform: [installers]}]
            if isinstance(downloads, list):
                for item in downloads:
                    if isinstance(item, list) and len(item) >= 2:
                        lang, platforms = item[0], item[1]
                        # Prefer English, but accept any if no English available
                        is_english = lang.lower() == 'english'
                        
                        for platform, installers in platforms.items():
                            if platform in platform_installers:
                                for inst in installers:
                                    # Tag with language for priority
                                    inst['_is_english'] = is_english
                                    platform_installers[platform].append(inst)
            elif isinstance(downloads, dict):
                for platform, installers in downloads.items():
                    if platform in platform_installers and isinstance(installers, list):
                        for inst in installers:
                            inst['_is_english'] = True  # Assume English if not language-tagged
                            platform_installers[platform].append(inst)
            
            # Choose best platform: Linux > Windows > Mac
            chosen_installers = []
            for platform in ['linux', 'windows', 'mac']:
                if platform_installers[platform]:
                    chosen_installers = platform_installers[platform]
                    break
            
            if not chosen_installers:
                logger.debug(f"[GOG] No installers found for {game_id}")
                return None
            
            # Filter: only base installers (not patches), prefer English
            base_english = [i for i in chosen_installers 
                          if 'patch' not in i.get('name', '').lower() and i.get('_is_english')]
            base_any = [i for i in chosen_installers 
                       if 'patch' not in i.get('name', '').lower()]
            
            # Use English if available, otherwise any
            base_installers = base_english if base_english else base_any
            
            # Deduplicate by name (same file in multiple languages)
            seen_names = set()
            unique_installers = []
            for inst in base_installers:
                name = inst.get('name', '')
                if name not in seen_names:
                    seen_names.add(name)
                    unique_installers.append(inst)
            
            # Calculate total size
            total_bytes = 0
            for installer in unique_installers:
                size_str = installer.get('size', '0 MB')
                size_bytes = self._parse_size_string(size_str)
                total_bytes += size_bytes
            
            logger.info(f"[GOG] Game {game_id} size: {total_bytes} bytes ({total_bytes / (1024**2):.1f} MB)")
            return total_bytes if total_bytes > 0 else None
            
        except Exception as e:
            logger.debug(f"[GOG] Error getting game size for {game_id}: {e}")
            return None
    
    def _parse_size_string(self, size_str: str) -> int:
        """Parse GOG size string like '259 MB' or '1.2 GB' to bytes"""
        try:
            size_str = size_str.strip()
            parts = size_str.split()
            if len(parts) != 2:
                return 0
            
            value = float(parts[0])
            unit = parts[1].upper()
            
            if unit == 'GB':
                return int(value * 1024**3)
            elif unit == 'MB':
                return int(value * 1024**2)
            elif unit == 'KB':
                return int(value * 1024)
            else:
                return int(value)
        except:
            return 0

    async def install_game(self, game_id: str, base_path: str = None, progress_callback=None) -> Dict[str, Any]:
        """Install GOG game using GOG API

        Downloads and installs Linux version of GOG game using OAuth-authenticated API calls.

        Args:
            game_id: GOG game product ID (numeric string)
            base_path: Optional base directory for installation (e.g. /home/deck/Games or /run/media/.../Games)
            progress_callback: Optional async function to call with progress updates

        Returns:
            Dict with success status, install_path, and error if any
        """
        try:
            # 1. Check authentication (just check if token exists, will refresh during download if needed)
            if not self.access_token or not self.refresh_token:
                logger.warning(f"[GOG] No tokens found for installation of {game_id}")
                return {
                    'success': False,
                    'error': 'Not logged into GOG. Please authenticate first.'
                }

            logger.info(f"[GOG] Tokens present, proceeding with installation of {game_id}")

            # 2. Get game details including download links
            logger.info(f"[GOG] Getting download info for game {game_id}")
            game_details = await self._get_game_details(game_id)

            if not game_details:
                return {
                    'success': False,
                    'error': 'Failed to get game details from GOG API'
                }

            # 3. Find Linux installer (prefer Linux, fallback to Windows)
            linux_installers = self._find_linux_installer(game_details)
            windows_installers = []
            installers_list = []
            installer_platform = 'linux'

            if linux_installers:
                installers_list = linux_installers
                installer_platform = 'linux'
                logger.info(f"[GOG] Using Linux installer for {game_id} ({len(installers_list)} parts)")
            else:
                logger.warning(f"[GOG] No Linux installer found, trying Windows version")
                windows_installers = self._find_windows_installer(game_details)

                if not windows_installers:
                    return {
                        'success': False,
                        'error': 'No Linux or Windows installer found for this game'
                    }

                installers_list = windows_installers
                installer_platform = 'windows'
                logger.info(f"[GOG] Using Windows installer for {game_id} ({len(installers_list)} parts) (will extract with innoextract)")

            # 4. Create install directory
            if not base_path:
                base_path = os.path.expanduser("~/GOG Games")
            
            game_title = game_details.get('title', f'game_{game_id}')
            # Sanitize title for directory name
            safe_title = "".join(c for c in game_title if c.isalnum() or c in (' ', '-', '_')).strip()
            install_path = os.path.join(base_path, safe_title)
            os.makedirs(install_path, exist_ok=True)

            logger.info(f"[GOG] Installing '{game_title}' to {install_path}")

            # 5. Download installer parts with improved multi-part handling
            main_installer_path = None
            
            # Pre-calculate total size across all parts for accurate overall progress
            total_bytes_all_parts = 0
            part_sizes = []
            for inst in installers_list:
                size_str = inst.get('size', '0 MB')
                size_bytes = self._parse_size_string(size_str)
                part_sizes.append(size_bytes)
                total_bytes_all_parts += size_bytes
            
            if total_bytes_all_parts > 0:
                logger.info(f"[GOG] Total download size: {total_bytes_all_parts / (1024**3):.2f} GB across {len(installers_list)} parts")
            
            # Track cumulative downloaded bytes for weighted overall progress
            cumulative_downloaded = 0
            
            for index, installer_data in enumerate(installers_list):
                installer_url = installer_data.get('manualUrl') or installer_data.get('downloaderUrl')
                if not installer_url:
                    logger.warning(f"[GOG] Skipping installer part {index+1}: No URL found")
                    continue

                # Get installer filename and expected size
                default_filename = f'installer_part_{index}.{"sh" if installer_platform == "linux" else "exe"}'
                installer_filename = installer_data.get('name', default_filename)
                installer_path = os.path.join(install_path, installer_filename)
                expected_size = part_sizes[index] if index < len(part_sizes) else 0

                # Determine if this is the main installer (executable)
                # Windows: .exe (not .bin)
                # Linux: .sh OR the first part if no .sh extension (GOG sometimes names it without extension)
                is_main_part = False
                if installer_platform == 'linux':
                    # Check for .sh extension OR if it's the first/only part
                    if installer_filename.endswith('.sh'):
                        is_main_part = True
                    elif index == 0 and not installer_filename.endswith('.bin'):
                        # First part and not a .bin data file - likely the main installer
                        is_main_part = True
                else:
                    # Windows: .exe file or first part if not a .bin
                    if installer_filename.endswith('.exe'):
                        is_main_part = True
                    elif index == 0 and not installer_filename.endswith('.bin'):
                        is_main_part = True

                if is_main_part and not main_installer_path:
                    main_installer_path = installer_path

                logger.info(f"[GOG] Downloading part {index+1}/{len(installers_list)}: {installer_filename} ({expected_size / (1024**2):.1f} MB)")
                logger.info(f"[GOG] Download URL: {installer_url}")
                
                # Create a wrapper callback that calculates weighted overall progress
                async def weighted_progress_callback(progress_data, part_idx=index, part_expected=expected_size):
                    nonlocal cumulative_downloaded
                    if progress_callback:
                        # Calculate overall progress: completed parts + current part progress
                        completed_bytes = sum(part_sizes[:part_idx])  # Bytes from completed parts
                        current_part_bytes = progress_data.get('downloaded_bytes', 0)
                        overall_downloaded = completed_bytes + current_part_bytes
                        
                        if total_bytes_all_parts > 0:
                            overall_percent = (overall_downloaded / total_bytes_all_parts) * 100
                        else:
                            # Fallback: use per-part progress
                            overall_percent = progress_data.get('progress_percent', 0)
                        
                        await progress_callback({
                            'progress_percent': overall_percent,
                            'downloaded_bytes': overall_downloaded,
                            'total_bytes': total_bytes_all_parts,
                            'speed_bps': progress_data.get('speed_bps', 0),
                            'eta_seconds': progress_data.get('eta_seconds', 0),
                            'current_part': part_idx + 1,
                            'total_parts': len(installers_list)
                        })
                
                # Download with expected size for skip/resume functionality
                download_success = await self._download_file(
                    installer_url, 
                    installer_path, 
                    weighted_progress_callback,
                    expected_size=expected_size
                )

                if not download_success:
                    return {
                        'success': False,
                        'error': f'Failed to download installer part {index+1}/{len(installers_list)}: {installer_filename}'
                    }
                
                # Update cumulative for next iteration
                cumulative_downloaded += expected_size

            # Fallback: if still no main installer identified, use the first downloaded file
            if not main_installer_path and installers_list:
                first_installer = installers_list[0]
                first_filename = first_installer.get('name', 'installer')
                main_installer_path = os.path.join(install_path, first_filename)
                logger.warning(f"[GOG] Using first downloaded file as main installer: {main_installer_path}")

            if not main_installer_path or not os.path.exists(main_installer_path):
                 return {
                    'success': False,
                    'error': 'Downloaded parts but could not identify main installer executable'
                }

            # 6. Extract installer (different methods for Linux vs Windows)
            # Detect if it's a shell script by content, not just extension
            is_linux_installer = installer_platform == 'linux'
            
            # If platform is "linux" but file might not have .sh extension, verify by content
            if is_linux_installer or self._is_shell_script(main_installer_path):
                os.chmod(main_installer_path, 0o755)
                logger.info(f"[GOG] Extracting Linux installer: {os.path.basename(main_installer_path)}")
                extract_success = await self._extract_installer(main_installer_path, install_path)
            else:
                # Windows installer - use innoextract
                # First, rename slice files to match innoextract expected naming
                # GOG names: "Game (Part 1 of 2)", "Game (Part 2 of 2)"
                # Innoextract expects: "Game (Part 1 of 2)", "Game (Part 1 of 2)-1.bin"
                await self._rename_gog_slices_for_innoextract(main_installer_path, install_path)
                
                logger.info(f"[GOG] Extracting Windows installer with innoextract: {os.path.basename(main_installer_path)}")
                extract_success = await self._extract_windows_installer(main_installer_path, install_path)

            if not extract_success:
                logger.warning(f"[GOG] Installer extraction failed, keeping installer files at {install_path}")

            # 7. Find game executable
            game_exe = self._find_game_executable(install_path)
            if game_exe:
                logger.info(f"[GOG] Found game executable: {game_exe}")
                
                # Write marker file to ensure detection works even if extraction was messy
                try:
                    with open(os.path.join(install_path, '.unifideck-id'), 'w') as f:
                        f.write(str(game_id))
                    logger.info(f"[GOG] Wrote marker file for ID {game_id}")
                except Exception as e:
                    logger.warning(f"[GOG] Failed to write marker file: {e}")

                logger.info(f"[GOG] Game installed successfully to {install_path}")
                return {
                    'success': True,
                    'install_path': install_path,
                    'executable': game_exe,
                    'message': f'Game installed to {install_path}'
                }
            else:
                logger.warning(f"[GOG] No executable found in {install_path}")
                # Even if no executable is found, we consider the installation successful
                # if the extraction completed, as the user might manually configure it.
                # Write marker file anyway to indicate installation attempt.
                try:
                    with open(os.path.join(install_path, '.unifideck-id'), 'w') as f:
                        f.write(str(game_id))
                    logger.info(f"[GOG] Wrote marker file for ID {game_id} (no executable found)")
                except Exception as e:
                    logger.warning(f"[GOG] Failed to write marker file: {e}")

                logger.info(f"[GOG] Game installed successfully to {install_path} (no executable found)")
                return {
                    'success': True,
                    'install_path': install_path,
                    'executable': None, # Explicitly set to None if not found
                    'message': f'Game installed to {install_path}, but no executable was automatically found.'
                }

        except Exception as e:
            logger.error(f"[GOG] Error installing game {game_id}: {e}", exc_info=True)
            return {
                'success': False,
                'error': f'Installation error: {str(e)}'
            }

    async def _get_game_details(self, game_id: str, session=None) -> Optional[Dict[str, Any]]:
        """Get detailed game information including download links from GOG API"""
        try:
            import aiohttp
            import ssl

            url = f'https://embed.gog.com/account/gameDetails/{game_id}.json'
            
            async def fetch_with_session(sess):
                headers = {'Authorization': f'Bearer {self.access_token}'}
                async with sess.get(url, headers=headers) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 401:
                        # Try to refresh token and retry
                        if await self._refresh_access_token():
                            headers = {'Authorization': f'Bearer {self.access_token}'}
                            async with sess.get(url, headers=headers) as retry_response:
                                if retry_response.status == 200:
                                    return await retry_response.json()
                    
                    logger.error(f"[GOG] Failed to get game details: {response.status}")
                    return None

            if session:
                return await fetch_with_session(session)
            else:
                # Create SSL context
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                connector = aiohttp.TCPConnector(ssl=ssl_context)
                async with aiohttp.ClientSession(connector=connector) as new_session:
                    return await fetch_with_session(new_session)

        except Exception as e:
            logger.error(f"[GOG] Error getting game details: {e}", exc_info=True)
            return None

    def _find_linux_installer(self, game_details: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Find Linux installer from game details response
        
        GOG API returns downloads in format: {platform: [installers], ...}
        e.g., {'linux': [{'manualUrl': '...', ...}], 'windows': [...]}
        """
        try:
            downloads = game_details.get('downloads', {})
            
            # Debug logging to understand the structure
            logger.info(f"[GOG] Downloads structure type: {type(downloads).__name__}")
            try:
                import json
                logger.info(f"[GOG] Raw downloads content: {json.dumps(downloads, default=str)}")
            except:
                logger.info(f"[GOG] Raw downloads content (repr): {repr(downloads)}")

            if isinstance(downloads, dict):
                logger.info(f"[GOG] Available platforms: {list(downloads.keys())}")
            elif isinstance(downloads, list):
                logger.info(f"[GOG] Downloads is a list with {len(downloads)} items")
            
            # Handle dictionary format: {'linux': [installers], 'windows': [installers]}
            if isinstance(downloads, dict):
                linux_installers = downloads.get('linux', [])
                if linux_installers:
                    logger.info(f"[GOG] Found {len(linux_installers)} Linux installer(s)")
                    return linux_installers
                    
            # Fallback: handle list format (legacy or alternative API response)
            elif isinstance(downloads, list):
                for download_group in downloads:
                    # Case A: List of dictionaries: [{"platform": "linux", "installers": [...]}, ...]
                    if isinstance(download_group, dict):
                        platform = download_group.get('platform', '').lower()
                        if platform == 'linux':
                            installers = download_group.get('installers', [])
                            if installers:
                                return installers
                    
                    # Case B: List of pairs where first item is platform (legacy/hypothetical)
                    elif isinstance(download_group, list) and len(download_group) >= 2:
                        first_item = str(download_group[0]).lower()
                        second_item = download_group[1]
                        
                        # Sub-case 1: [Platform, Data] layout
                        if first_item == 'linux' and isinstance(second_item, dict):
                            installers = second_item.get('installers', [])
                            if installers:
                                return installers

                        # Sub-case 2: [Language, {platform: ...}] layout (Baldur's Gate format)
                        # e.g., ["English", {"linux": [...], "windows": [...]}]
                        if isinstance(second_item, dict):
                            installers = second_item.get('linux', [])
                            if installers:
                                return installers

            logger.warning(f"[GOG] No Linux installer found in game details")
            return None

        except Exception as e:
            logger.error(f"[GOG] Error finding Linux installer: {e}", exc_info=True)
            return None

    def _find_windows_installer(self, game_details: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Find Windows installer from game details response
        
        GOG API returns downloads in format: {platform: [installers], ...}
        e.g., {'linux': [{'manualUrl': '...', ...}], 'windows': [...]}
        """
        try:
            downloads = game_details.get('downloads', {})
            
            # Handle dictionary format: {'linux': [installers], 'windows': [installers]}
            if isinstance(downloads, dict):
                windows_installers = downloads.get('windows', [])
                if windows_installers:
                    logger.info(f"[GOG] Found {len(windows_installers)} Windows installer(s)")
                    return windows_installers
                    
            # Fallback: handle list format (legacy or alternative API response)
            elif isinstance(downloads, list):
                for download_group in downloads:
                    # Case A: List of dictionaries
                    if isinstance(download_group, dict):
                        platform = download_group.get('platform', '').lower()
                        if platform == 'windows':
                            installers = download_group.get('installers', [])
                            if installers:
                                logger.info(f"[GOG] Found Windows installer (no Linux version available)")
                                return installers
                    
                    # Case B: List of pairs where first item is platform (legacy/hypothetical)
                    elif isinstance(download_group, list) and len(download_group) >= 2:
                        first_item = str(download_group[0]).lower()
                        second_item = download_group[1]
                        
                        # Sub-case 1: [Platform, Data] layout
                        if first_item == 'windows' and isinstance(second_item, dict):
                            installers = second_item.get('installers', [])
                            if installers:
                                logger.info(f"[GOG] Found Windows installer (no Linux version available)")
                                return installers

                        # Sub-case 2: [Language, {platform: ...}] layout (Baldur's Gate format)
                        if isinstance(second_item, dict):
                            installers = second_item.get('windows', [])
                            if installers:
                                logger.info(f"[GOG] Found Windows installer inside language group")
                                return installers

            logger.warning(f"[GOG] No Windows installer found either")
            return None

        except Exception as e:
            logger.error(f"[GOG] Error finding Windows installer: {e}", exc_info=True)
            return None

    async def uninstall_game(self, game_id: str) -> Dict[str, Any]:
        """Uninstall GOG game by deleting directory"""
        try:
            # 1. Find install directory
            # We have to scan because we don't store the path
            install_path = None
            base_path = os.path.expanduser("~/GOG Games")
            
            if os.path.exists(base_path):
                for item in os.listdir(base_path):
                    item_path = os.path.join(base_path, item)
                    if os.path.isdir(item_path):
                        # Check ID in marker file or info file
                        found_id = self._get_game_id_from_dir(item_path)
                        if found_id == game_id:
                            install_path = item_path
                            break
            
            if not install_path:
                return {'success': False, 'error': 'Game installation directory not found'}

            # 2. Safety check - ensure it's a Unifideck game
            # Additional safety: ensure path contains "GOG Games" and isn't root
            if "GOG Games" not in install_path or install_path == base_path:
                 return {'success': False, 'error': 'Safety check failed: Invalid uninstall path'}

            marker_file = os.path.join(install_path, '.unifideck-id')
            if not os.path.exists(marker_file):
                 # Fallback: check for goggame info file to be sure
                 has_info = any(f.startswith('goggame-') for f in os.listdir(install_path))
                 if not has_info:
                    return {'success': False, 'error': 'Safety check failed: Not identified as a GOG game directory'}

            # 3. Delete directory
            import shutil
            logger.info(f"[GOG] Uninstalling game {game_id} (deleting {install_path})")
            shutil.rmtree(install_path)
            
            if os.path.exists(install_path):
                return {'success': False, 'error': 'Failed to delete directory'}
                
            return {'success': True, 'message': 'Game uninstalled successfully'}

        except Exception as e:
            logger.error(f"[GOG] Error uninstalling game {game_id}: {e}")
            return {'success': False, 'error': str(e)}



    async def _download_file(self, url: str, dest_path: str, progress_callback=None, expected_size: int = 0) -> bool:
        """Download file from GOG with authentication - wrapper for backwards compatibility"""
        return await self._download_file_with_resume(url, dest_path, expected_size, progress_callback)

    async def _download_file_with_resume(
        self, 
        url: str, 
        dest_path: str, 
        expected_size: int = 0,
        progress_callback=None,
        max_retries: int = 5,
        base_timeout: int = 120
    ) -> bool:
        """
        Download file from GOG with authentication, retry, and resume support.
        
        Designed for large multi-part GOG installers (Cyberpunk, Witcher, etc.):
        - Retry with exponential backoff (5 attempts: 2s, 4s, 8s, 16s, 32s delays)
        - Resume partial downloads via HTTP Range header
        - Skip already-downloaded files if size matches
        - 120-second socket timeout for slow CDN servers
        
        Args:
            url: Download URL (can be relative to gog.com)
            dest_path: Local file path to save to
            expected_size: Expected file size in bytes (0 = unknown, will use Content-Length)
            progress_callback: Async function to report progress
            max_retries: Maximum download attempts (default 5)
            base_timeout: Socket read timeout in seconds (default 120)
        
        Returns:
            True if download completed successfully, False otherwise
        """
        import aiohttp
        import ssl
        
        # Handle relative URLs from GOG API
        if url.startswith('/'):
            url = f"https://www.gog.com{url}"
            logger.info(f"[GOG] Converted relative URL to: {url}")
        
        # Check if file already exists with correct size (skip re-downloading)
        if expected_size > 0 and os.path.exists(dest_path):
            existing_size = os.path.getsize(dest_path)
            if existing_size == expected_size:
                logger.info(f"[GOG] Skipping already downloaded file: {os.path.basename(dest_path)} ({existing_size / (1024*1024):.1f} MB)")
                # Report 100% progress for this file
                if progress_callback:
                    await progress_callback({
                        'progress_percent': 100.0,
                        'downloaded_bytes': existing_size,
                        'total_bytes': existing_size,
                        'speed_bps': 0,
                        'eta_seconds': 0
                    })
                return True
            elif existing_size > expected_size:
                # File is larger than expected - corrupted, delete and re-download
                logger.warning(f"[GOG] Existing file larger than expected ({existing_size} > {expected_size}), deleting and re-downloading")
                os.remove(dest_path)
        
        # Create SSL context
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                # Check for existing partial download to resume
                resume_from = 0
                if os.path.exists(dest_path):
                    resume_from = os.path.getsize(dest_path)
                    if resume_from > 0:
                        logger.info(f"[GOG] Resuming download from byte {resume_from} ({resume_from / (1024*1024):.1f} MB)")
                
                # Build headers
                headers = {}
                if self.access_token and 'gog.com' in url:
                    headers['Authorization'] = f'Bearer {self.access_token}'
                
                # Add Range header for resume
                if resume_from > 0:
                    headers['Range'] = f'bytes={resume_from}-'
                
                # Create session with longer timeout for large files
                connector = aiohttp.TCPConnector(ssl=ssl_context)
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=base_timeout)
                
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    async with session.get(url, headers=headers) as response:
                        # Handle response status
                        if response.status == 200:
                            # Full download (no resume or server doesn't support Range)
                            if resume_from > 0:
                                logger.info(f"[GOG] Server doesn't support resume, starting from beginning")
                                resume_from = 0
                            total_size = int(response.headers.get('content-length', 0))
                            file_mode = 'wb'
                        elif response.status == 206:
                            # Partial content - resume successful
                            content_range = response.headers.get('content-range', '')
                            # Format: "bytes 12345-67890/12345678" 
                            if '/' in content_range:
                                total_size = int(content_range.split('/')[-1])
                            else:
                                total_size = resume_from + int(response.headers.get('content-length', 0))
                            file_mode = 'ab'  # Append mode for resume
                            logger.info(f"[GOG] Resume accepted, continuing from {resume_from / (1024*1024):.1f} MB")
                        elif response.status == 416:
                            # Range not satisfiable - file might be complete
                            if expected_size > 0 and resume_from >= expected_size:
                                logger.info(f"[GOG] File already complete: {os.path.basename(dest_path)}")
                                return True
                            # Otherwise, delete and restart
                            logger.warning(f"[GOG] Range not satisfiable, restarting download")
                            if os.path.exists(dest_path):
                                os.remove(dest_path)
                            resume_from = 0
                            continue  # Retry from beginning
                        else:
                            logger.error(f"[GOG] Download failed with status {response.status}")
                            last_error = f"HTTP {response.status}"
                            raise aiohttp.ClientError(f"HTTP {response.status}")
                        
                        # Use expected_size if known, otherwise use Content-Length
                        if expected_size > 0:
                            total_size = expected_size
                        
                        downloaded = resume_from  # Start from resume point
                        
                        with open(dest_path, file_mode) as f:
                            last_logged_percent = -1
                            last_callback_time = time.time()
                            last_downloaded_for_speed = downloaded
                            current_speed_bps = 0
                            
                            async for chunk in response.content.iter_chunked(65536):  # 64KB chunks for speed
                                f.write(chunk)
                                downloaded += len(chunk)
                                
                                # Real-time progress logging and callback
                                if total_size > 0:
                                    progress = (downloaded / total_size) * 100
                                    
                                    # Calculate speed
                                    now = time.time()
                                    elapsed = now - last_callback_time
                                    if elapsed >= 0.5:
                                        bytes_since_last = downloaded - last_downloaded_for_speed
                                        current_speed_bps = bytes_since_last / elapsed
                                        last_downloaded_for_speed = downloaded
                                        last_callback_time = now
                                    
                                    # Log every 5% milestone
                                    current_percent = int(progress)
                                    if current_percent % 5 == 0 and current_percent != last_logged_percent:
                                        mb_downloaded = downloaded / (1024 * 1024)
                                        mb_total = total_size / (1024 * 1024)
                                        logger.info(f"[GOG Download] {progress:.1f}% ({mb_downloaded:.1f} MB / {mb_total:.1f} MB)")
                                        last_logged_percent = current_percent
                                    
                                    # Call progress callback with full stats
                                    if progress_callback:
                                        remaining_bytes = total_size - downloaded
                                        eta_seconds = int(remaining_bytes / current_speed_bps) if current_speed_bps > 0 else 0
                                        
                                        await progress_callback({
                                            'progress_percent': progress,
                                            'downloaded_bytes': downloaded,
                                            'total_bytes': total_size,
                                            'speed_bps': current_speed_bps,
                                            'eta_seconds': eta_seconds
                                        })
                        
                        mb_size = downloaded / (1024 * 1024)
                        logger.info(f"[GOG] Download complete: {mb_size:.1f} MB downloaded to {dest_path}")
                        return True
                        
            except asyncio.CancelledError:
                # Don't retry on cancellation
                logger.info(f"[GOG] Download cancelled: {os.path.basename(dest_path)}")
                raise
                
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[GOG] Download attempt {attempt}/{max_retries} failed: {e}")
                
                if attempt < max_retries:
                    # Exponential backoff: 2, 4, 8, 16, 32 seconds
                    delay = 2 ** attempt
                    logger.info(f"[GOG] Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
        
        # All retries exhausted
        logger.error(f"[GOG] Download failed after {max_retries} attempts: {last_error}")
        return False

    async def _run_post_install(self, install_path: str):
        """
        Run GOG post-install script (common in Linux installers) to set up
        dependencies or environment. Checked after extraction.
        """
        import subprocess
        
        # Possible locations for post-install script
        # Based on observing GOG installers and Heroic behavior
        candidates = [
            os.path.join(install_path, "support", "postinst.sh"),
            os.path.join(install_path, "data", "noarch", "support", "postinst.sh"),
            os.path.join(install_path, "meta", "postinst.sh"),
        ]
        
        for script in candidates:
            if os.path.exists(script) and os.path.isfile(script):
                logger.info(f"[GOG] Found post-install script: {script}")
                try:
                    os.chmod(script, 0o755)
                    # Run synchronously or asynchronously? 
                    # install_game is async. We can use asyncio.create_subprocess_exec
                    # But extraction was synchronous? No, extract returns.
                    
                    # We want to wait for it.
                    logger.info(f"[GOG] Executing post-install script...")
                    
                    # Using subprocess.run since we are in an async function but might block briefly? 
                    # Better to use asyncio.
                    process = await asyncio.create_subprocess_exec(
                        script,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await process.communicate()
                    
                    if process.returncode == 0:
                        logger.info(f"[GOG] Post-install script completed successfully.\n{stdout.decode().strip()}")
                    else:
                        logger.warning(f"[GOG] Post-install script failed (Code {process.returncode}).\nStderr: {stderr.decode().strip()}")
                        
                    return # Only run the first one found? Usually only one.
                    
                except Exception as e:
                    logger.error(f"[GOG] Error running post-install script: {e}")

    def _is_shell_script(self, filepath: str) -> bool:
        """
        Detect if a file is a shell script by checking for shebang or shell patterns.
        This works even if the file has no .sh extension.
        """
        try:
            with open(filepath, 'rb') as f:
                # Read first 512 bytes for header detection
                header = f.read(512)
                
                # Check for shebang
                if header.startswith(b'#!'):
                    # Common shell shebangs
                    shebang_line = header.split(b'\n')[0].decode('utf-8', errors='ignore')
                    if any(shell in shebang_line for shell in ['/bin/sh', '/bin/bash', '/usr/bin/env bash', '/usr/bin/env sh']):
                        return True
                
                # Check for Makeself/MojoSetup markers (GOG installers)
                if b'Makeself' in header or b'MojoSetup' in header or b'GOG.com' in header:
                    return True
                    
        except Exception:
            pass
        return False

    def _fix_permissions(self, install_path: str):
        """
        Recursively fix execute permissions on game binaries after extraction.
        This ensures shell scripts and ELF binaries are executable.
        """
        import subprocess
        
        logger.info(f"[GOG] Fixing permissions in {install_path}")
        
        try:
            # Make all shell scripts and binaries executable
            for root, dirs, files in os.walk(install_path):
                for f in files:
                    filepath = os.path.join(root, f)
                    
                    # Make .sh files executable
                    if f.endswith('.sh'):
                        try:
                            os.chmod(filepath, 0o755)
                        except Exception as e:
                            logger.debug(f"[GOG] Could not chmod {filepath}: {e}")
                        continue
                    
                    try:
                        with open(filepath, 'rb') as file:
                            magic = file.read(512)
                            
                            # Check for ELF binaries (Linux executables)
                            # ELF files start with magic bytes: 0x7f 'E' 'L' 'F'
                            if magic[:4] == b'\x7fELF':
                                os.chmod(filepath, 0o755)
                                logger.debug(f"[GOG] Made ELF binary executable: {filepath}")
                            
                            # Check for shell scripts by shebang (handles no-extension files)
                            elif magic.startswith(b'#!'):
                                os.chmod(filepath, 0o755)
                                logger.debug(f"[GOG] Made shell script executable: {filepath}")
                    except Exception:
                        pass  # Skip files we can't read
            
            logger.info(f"[GOG] Permissions fixed successfully")
            
        except Exception as e:
            logger.warning(f"[GOG] Error fixing permissions: {e}")

    async def _extract_installer(self, installer_path: str, install_path: str, progress_callback=None) -> bool:
        """Extract GOG Linux installer (.sh file)

        GOG Linux installers are Makeself archives containing MojoSetup.
        We use the MojoSetup silent install command to extract game files.
        """
        extraction_succeeded = False
        
        try:
            # Method 1: MojoSetup silent install (the correct way for GOG installers)
            # GOG installers = Makeself + MojoSetup. The '-- ' passes args to MojoSetup.
            logger.info(f"[GOG] Running MojoSetup silent install")
            proc = await asyncio.create_subprocess_exec(
                installer_path,
                '--',  # Pass following args to embedded script (MojoSetup)
                '--i-agree-to-all-licenses',
                '--noreadme',
                '--nooptions',
                '--noprompt',
                '--destination', install_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=install_path
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                logger.info(f"[GOG] MojoSetup silent install successful")
                extraction_succeeded = True
            else:
                logger.warning(f"[GOG] MojoSetup silent install failed (code {proc.returncode})")
                if stderr:
                    logger.debug(f"[GOG] stderr: {stderr.decode()[:500]}")

            # Method 2: Try unzip (fallback for non-MojoSetup archives)
            if not extraction_succeeded:
                logger.info(f"[GOG] Trying unzip extraction")
                proc = await asyncio.create_subprocess_exec(
                    'unzip', '-o', installer_path, '-d', install_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                if proc.returncode == 0:
                    logger.info(f"[GOG] Unzip extraction successful")
                    extraction_succeeded = True

            # ALWAYS fix permissions regardless of extraction success
            self._fix_permissions(install_path)
            
            # Run post-install if extraction worked
            if extraction_succeeded:
                await self._run_post_install(install_path)
                return True

            logger.warning(f"[GOG] Could not auto-extract installer, file kept at {installer_path}")
            return False

        except Exception as e:
            logger.error(f"[GOG] Error extracting installer: {e}", exc_info=True)
            self._fix_permissions(install_path)
            return False

    async def _rename_gog_slices_for_innoextract(self, main_installer_path: str, install_path: str) -> None:
        """Rename GOG multi-part installer slices to match innoextract expectations.
        
        GOG names files like: "Game (Part 1 of 2)", "Game (Part 2 of 2)"
        But innoextract expects: "Game (Part 1 of 2)", "Game (Part 1 of 2)-1.bin"
        
        This method finds "Part X of Y" files and renames them to "-N.bin" format.
        """
        import re
        
        try:
            main_basename = os.path.basename(main_installer_path)
            main_dir = os.path.dirname(main_installer_path)
            
            # Pattern to match "Part X of Y" in filename
            # E.g., "Moonscars (Part 1 of 2)" -> groups: (Moonscars, 1, 2)
            part_pattern = re.compile(r'^(.+?)\s*\(Part\s+(\d+)\s+of\s+(\d+)\)(.*)$', re.IGNORECASE)
            
            main_match = part_pattern.match(main_basename)
            if not main_match:
                logger.info(f"[GOG] Main installer doesn't match 'Part X of Y' pattern, no renaming needed")
                return
            
            base_name = main_match.group(1).strip()
            main_part_num = int(main_match.group(2))
            total_parts = int(main_match.group(3))
            suffix = main_match.group(4)  # Any trailing extension like .exe
            
            logger.info(f"[GOG] Detected multi-part installer: '{base_name}' with {total_parts} parts")
            
            # Find and rename slice files (parts 2, 3, etc.)
            renamed_count = 0
            for filename in os.listdir(install_path):
                file_match = part_pattern.match(filename)
                if not file_match:
                    continue
                
                file_base = file_match.group(1).strip()
                file_part_num = int(file_match.group(2))
                file_suffix = file_match.group(4)
                
                # Skip the main installer (Part 1), only rename data slices
                if file_base.lower() == base_name.lower() and file_part_num > main_part_num:
                    # Calculate slice number (Part 2 -> -1.bin, Part 3 -> -2.bin, etc.)
                    slice_num = file_part_num - main_part_num
                    
                    # New name: main installer name + "-N.bin"
                    new_name = f"{main_basename}-{slice_num}.bin"
                    
                    old_path = os.path.join(install_path, filename)
                    new_path = os.path.join(install_path, new_name)
                    
                    if os.path.exists(old_path) and not os.path.exists(new_path):
                        os.rename(old_path, new_path)
                        logger.info(f"[GOG] Renamed slice: '{filename}' -> '{new_name}'")
                        renamed_count += 1
            
            if renamed_count > 0:
                logger.info(f"[GOG] Renamed {renamed_count} slice file(s) for innoextract compatibility")
            else:
                logger.info(f"[GOG] No slice files needed renaming")
                
        except Exception as e:
            logger.warning(f"[GOG] Error renaming slice files (non-fatal): {e}")

    async def _extract_windows_installer(self, installer_path: str, install_path: str) -> bool:
        """Extract GOG Windows installer (.exe file) using innoextract

        Windows GOG installers use Inno Setup format. We extract them using
        the bundled innoextract tool which doesn't require Wine/Proton.
        """
        try:
            # Use bundled innoextract binary
            innoextract_bin = os.path.join(os.path.dirname(__file__), 'bin', 'innoextract')

            if not os.path.exists(innoextract_bin):
                logger.error(f"[GOG] Bundled innoextract not found at {innoextract_bin}")
                return False

            logger.info(f"[GOG] Using bundled innoextract: {innoextract_bin}")
            logger.info(f"[GOG] Running: innoextract -e -d {install_path} {installer_path}")

            # Extract installer
            # -e: extract files only (no GUI)
            # -d: output directory
            # -g: extract GOG.com specific archives (bonus content, etc.)
            proc = await asyncio.create_subprocess_exec(
                innoextract_bin,
                '-e',  # Extract mode
                '-g',  # Extract GOG.com specific archives
                '-d', install_path,
                installer_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                logger.info(f"[GOG] Windows installer extraction successful")

                # innoextract creates an "app" subdirectory, move contents up
                app_dir = os.path.join(install_path, 'app')
                if os.path.exists(app_dir):
                    import shutil
                    for item in os.listdir(app_dir):
                        src = os.path.join(app_dir, item)
                        dst = os.path.join(install_path, item)
                        shutil.move(src, dst)
                    shutil.rmtree(app_dir)
                    logger.info(f"[GOG] Moved extracted files from app/ subdirectory")

                return True
            else:
                error_msg = stderr.decode() if stderr else 'Unknown error'
                logger.error(f"[GOG] innoextract failed: {error_msg}")
                return False

        except Exception as e:
            logger.error(f"[GOG] Error extracting Windows installer: {e}", exc_info=True)
            return False

    def _find_game_executable(self, install_path: str) -> Optional[str]:
        """Find the game executable in the install directory"""
        try:
            # PRIORITY 1: Check for goggame-*.info file (most reliable)
            # This file contains the correct executable path from GOG's metadata
            for item in os.listdir(install_path):
                if item.startswith('goggame-') and item.endswith('.info'):
                    info_path = os.path.join(install_path, item)
                    try:
                        with open(info_path, 'r') as f:
                            info_data = json.load(f)
                            play_tasks = info_data.get('playTasks', [])
                            for task in play_tasks:
                                if task.get('isPrimary') and task.get('type') == 'FileTask':
                                    exe_name = task.get('path')
                                    if exe_name:
                                        exe_path = os.path.join(install_path, exe_name)
                                        if os.path.exists(exe_path):
                                            logger.info(f"[GOG] Found executable from goggame info: {exe_path}")
                                            return exe_path
                    except Exception as e:
                        logger.warning(f"[GOG] Error reading goggame info file: {e}")
            
            # PRIORITY 2: Check for .exe files (Windows games)
            exe_files = []
            for item in os.listdir(install_path):
                if item.endswith('.exe') and os.path.isfile(os.path.join(install_path, item)):
                    exe_files.append(item)

            # If we find .exe files, this is a Windows game - return the main exe
            if exe_files:
                # Common patterns for main executable
                main_exe_patterns = ['game.exe', 'start.exe', 'launcher.exe']
                for pattern in main_exe_patterns:
                    for exe in exe_files:
                        if exe.lower() == pattern:
                            exe_path = os.path.join(install_path, exe)
                            logger.info(f"[GOG] Found Windows game executable: {exe_path}")
                            return exe_path

                # Exclude known non-game executables
                excluded_patterns = [
                    'unins', 'uninst', 'uninstall',  # Uninstallers
                    'crash', 'crashhandler', 'crashreport',  # Crash handlers
                    'setup', 'config', 'settings',  # Configuration tools
                    'redist', 'vcredist', 'directx',  # Redistributables
                ]
                
                filtered_exes = []
                for exe in exe_files:
                    exe_lower = exe.lower()
                    if not any(pattern in exe_lower for pattern in excluded_patterns):
                        filtered_exes.append(exe)
                
                if filtered_exes:
                    # Prefer the largest executable (usually the main game)
                    exe_with_sizes = []
                    for exe in filtered_exes:
                        exe_full_path = os.path.join(install_path, exe)
                        size = os.path.getsize(exe_full_path)
                        exe_with_sizes.append((exe, size))
                    
                    # Sort by size descending
                    exe_with_sizes.sort(key=lambda x: x[1], reverse=True)
                    largest_exe = exe_with_sizes[0][0]
                    exe_path = os.path.join(install_path, largest_exe)
                    logger.info(f"[GOG] Found Windows game executable (largest): {exe_path}")
                    return exe_path
                
                # Fallback: return first .exe if all were filtered
                exe_path = os.path.join(install_path, exe_files[0])
                logger.info(f"[GOG] Found Windows executable (fallback): {exe_path}")
                return exe_path

            # Linux game logic - look for shell script launchers
            common_launchers = ['start.sh', 'launch.sh', 'game.sh', 'gameinfo']

            # Try common launcher names first
            for launcher in common_launchers:
                launcher_path = os.path.join(install_path, launcher)
                if os.path.exists(launcher_path) and os.path.isfile(launcher_path):
                    os.chmod(launcher_path, 0o755)  # Ensure executable
                    logger.info(f"[GOG] Found game launcher: {launcher_path}")
                    return launcher_path

            # Look for any .sh file
            for item in os.listdir(install_path):
                if item.endswith('.sh'):
                    item_path = os.path.join(install_path, item)
                    if os.path.isfile(item_path):
                        os.chmod(item_path, 0o755)
                        logger.info(f"[GOG] Found .sh script: {item_path}")
                        return item_path


            # Look for "data" subdirectory (common in GOG games)
            data_dir = os.path.join(install_path, 'data', 'noarch')
            if os.path.exists(data_dir):
                for launcher in common_launchers:
                    launcher_path = os.path.join(data_dir, launcher)
                    if os.path.exists(launcher_path) and os.path.isfile(launcher_path):
                        os.chmod(launcher_path, 0o755)
                        return launcher_path



            # PRIORITY 4: Heuristic - Check for file matching the directory name
            # e.g., "Leap of Love/Leap of Love"
            dir_name = os.path.basename(os.path.normpath(install_path))
            
            # Candidates to check
            candidates = [
                dir_name,                       # Exact match
                dir_name.replace(' ', ''),      # No spaces
                dir_name.replace(' ', '_'),     # Underscores
                dir_name.replace(' ', '-'),     # Hyphens
                dir_name.lower(),               # Lowercase
            ]
            
            # De-duplicate candidates
            candidates = list(set(candidates))
            
            for candidate in candidates:
                candidate_path = os.path.join(install_path, candidate)
                if os.path.exists(candidate_path) and os.path.isfile(candidate_path):
                    # Verify it's not one of the excluded files
                    try:
                        os.chmod(candidate_path, 0o755)
                        logger.info(f"[GOG] Found heuristic executable (matching dir name): {candidate_path}")
                        return candidate_path
                    except Exception as e:
                        logger.warning(f"[GOG] Found candidate {candidate_path} but failed to chmod: {e}")

            logger.warning(f"[GOG] No obvious game launcher found in {install_path}")
            return None

        except Exception as e:
            logger.error(f"[GOG] Error finding game executable: {e}", exc_info=True)
            return None



# CloudSaveManager imported from cloud_save_manager.py


class InstallHandler:
    """Handles game installations across stores"""

    def __init__(self, shortcuts_manager: ShortcutsManager, plugin_dir: Optional[str] = None):
        self.shortcuts_manager = shortcuts_manager
        self.plugin_dir = plugin_dir

    async def get_epic_game_exe(self, game_id: str) -> Optional[str]:
        """Get executable path for installed Epic game"""
        legendary_bin = EpicConnector(plugin_dir=self.plugin_dir)._find_legendary()
        if not legendary_bin:
            return None

        try:
            # Get game info in JSON format
            proc = await asyncio.create_subprocess_exec(
                legendary_bin, 'info', game_id, '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                info = json.loads(stdout.decode())
                install_path = info.get('install', {}).get('install_path', '')
                executable = info.get('manifest', {}).get('launch_exe', '')

                if install_path and executable:
                    exe_path = os.path.join(install_path, executable)
                    logger.info(f"Found Epic game executable: {exe_path}")
                    return exe_path

        except Exception as e:
            logger.error(f"Error getting Epic game exe: {e}")

        return None

    async def install_epic_game(self, game_id: str, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install Epic game via legendary"""
        legendary_bin = EpicConnector(plugin_dir=self.plugin_dir)._find_legendary()
        if not legendary_bin:
            return {'success': False, 'error': 'legendary not found'}

        try:
            cmd = [legendary_bin, 'install', game_id, '--yes']
            if install_path:
                cmd.extend(['--base-path', install_path])

            logger.info(f"Installing Epic game: {' '.join(cmd)}")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                # Get actual executable path
                exe_path = await self.get_epic_game_exe(game_id)

                if exe_path:
                    # Extract install directory from exe path
                    import os.path
                    install_dir = os.path.dirname(exe_path)

                    # Update shortcuts.vdf with install info
                    await self.shortcuts_manager.mark_installed(game_id, 'epic', install_dir, exe_path)
                else:
                    # Fallback: keep launcher script
                    logger.warning(f"Could not find exe for {game_id}, keeping launcher script")

                logger.info(f"Successfully installed {game_id}")
                return {'success': True, 'exe_path': exe_path}
            else:
                logger.error(f"Install failed: {stderr.decode()}")
                return {'success': False, 'error': stderr.decode()}

        except Exception as e:
            logger.error(f"Error installing Epic game: {e}")
            return {'success': False, 'error': str(e)}

    async def get_gog_game_exe(self, game_id: str, install_dir: str) -> Optional[str]:
        """Find executable for GOG game"""
        # Look for start.sh or other launch scripts
        common_launchers = ['start.sh', 'launch.sh', f'{game_id}.sh']

        for launcher in common_launchers:
            launcher_path = os.path.join(install_dir, launcher)
            if os.path.exists(launcher_path):
                logger.info(f"Found GOG game launcher: {launcher_path}")
                return launcher_path

        # Try to find any .sh file in the directory
        try:
            for item in os.listdir(install_dir):
                if item.endswith('.sh') and os.path.isfile(os.path.join(install_dir, item)):
                    launcher_path = os.path.join(install_dir, item)
                    logger.info(f"Found GOG game script: {launcher_path}")
                    return launcher_path
        except Exception as e:
            logger.error(f"Error searching for GOG launcher: {e}")

        return None

    async def install_gog_game(self, game_id: str, gog_instance, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install GOG game using GOG API

        Args:
            game_id: GOG game product ID
            gog_instance: Instance of GOG class with API methods
            install_path: Optional custom install path (not used - GOG class manages this)

        Returns:
            Dict with success status and exe_path
        """
        try:
            # Use the GOG class's install_game method which uses the API
            result = await gog_instance.install_game(game_id)

            if result.get('success'):
                # Update shortcuts.vdf with the installed game info
                exe_path = result.get('executable')
                install_dir = result.get('install_path')

                if install_dir:
                    await self.shortcuts_manager.mark_installed(game_id, 'gog', install_dir, exe_path)
                    logger.info(f"Successfully installed GOG game {game_id}")
                    return {'success': True, 'exe_path': exe_path, 'install_path': install_dir}

            return result

        except Exception as e:
            logger.error(f"Error installing GOG game: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def get_amazon_game_exe(self, game_id: str, install_dir: str = None) -> Optional[str]:
        """Find executable for Amazon game using fuel.json"""
        # If no install_dir provided, try to find from nile config
        if not install_dir:
            nile_config = os.path.expanduser("~/.config/nile")
            installed_file = os.path.join(nile_config, "installed.json")
            
            if os.path.exists(installed_file):
                try:
                    with open(installed_file, 'r') as f:
                        installed_list = json.load(f)
                    
                    for game in installed_list:
                        if game.get('id') == game_id:
                            install_dir = game.get('path', '')
                            break
                except Exception as e:
                    logger.error(f"[Amazon] Error reading installed.json: {e}")
        
        if not install_dir:
            logger.warning(f"[Amazon] Could not find install directory for {game_id}")
            return None
        
        # Parse fuel.json for executable
        fuel_path = os.path.join(install_dir, 'fuel.json')
        if not os.path.exists(fuel_path):
            logger.warning(f"[Amazon] No fuel.json found at {fuel_path}")
            return None
        
        try:
            import re
            with open(fuel_path, 'r') as f:
                content = f.read()
                # Remove single-line comments (fuel.json may have them)
                content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
                fuel_data = json.loads(content)
            
            main_cmd = fuel_data.get('Main', {}).get('Command', '')
            if main_cmd:
                exe_path = os.path.join(install_dir, main_cmd)
                logger.info(f"[Amazon] Found executable from fuel.json: {exe_path}")
                return exe_path
        except Exception as e:
            logger.error(f"[Amazon] Error parsing fuel.json: {e}")
        
        return None

    async def install_amazon_game(self, game_id: str, amazon_instance, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install Amazon game using nile CLI

        Args:
            game_id: Amazon game product ID
            amazon_instance: Instance of AmazonConnector with install methods
            install_path: Optional custom install path

        Returns:
            Dict with success status and exe_path
        """
        try:
            # Use the AmazonConnector's install_game method
            result = await amazon_instance.install_game(game_id)

            if result.get('success'):
                # Update shortcuts.vdf with the installed game info
                exe_path = result.get('exe_path')
                install_dir = result.get('install_path')

                if install_dir:
                    await self.shortcuts_manager.mark_installed(game_id, 'amazon', install_dir, exe_path)
                    logger.info(f"Successfully installed Amazon game {game_id}")
                    return {'success': True, 'exe_path': exe_path, 'install_path': install_dir}

            return result

        except Exception as e:
            logger.error(f"Error installing Amazon game: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}


class BackgroundSyncService:
    """Background service that syncs libraries every 5 minutes"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.running = False
        self.task = None

    async def start(self):
        """Start background sync"""
        if self.running:
            logger.warning("Background sync already running")
            return

        self.running = True
        self.task = asyncio.create_task(self._sync_loop())
        logger.info("Background sync service started")

    async def stop(self):
        """Stop background sync"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("Background sync service stopped")

    async def _sync_loop(self):
        """Main sync loop - sync game lists only, no artwork"""
        while self.running:
            try:
                # Only sync game lists, don't fetch artwork in background
                await self.plugin.sync_libraries(fetch_artwork=False)
                await asyncio.sleep(300)  # 5 minutes
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in sync loop: {e}")
                await asyncio.sleep(60)  # Retry in 1 minute on error


class Plugin:
    """Main Unifideck plugin class"""

    async def _main(self):
        logger.info("[INIT] Starting Unifideck plugin initialization")

        # Initialize sync progress tracker
        self.sync_progress = SyncProgress()

        logger.info("[INIT] Initializing ShortcutsManager")
        self.shortcuts_manager = ShortcutsManager()
        
        # Reconcile games.map to remove orphaned entries (games deleted externally)
        logger.info("[INIT] Reconciling games.map for orphaned entries")
        reconcile_result = self.shortcuts_manager.reconcile_games_map()
        if reconcile_result.get('removed', 0) > 0:
            logger.info(f"[INIT] Reconciliation complete: {reconcile_result['removed']} orphaned entries removed")

        # Repair shortcuts pointing to old plugin paths (happens after Decky reinstall)
        logger.info("[INIT] Repairing shortcuts pointing to old plugin paths")
        repair_result = self.shortcuts_manager.repair_shortcuts_exe_path()
        if repair_result.get('repaired', 0) > 0:
            logger.info(f"[INIT] Repaired {repair_result['repaired']} shortcuts with stale launcher paths")

        # Ensure shortcuts exist for all games in games.map (recreate missing shortcuts)
        logger.info("[INIT] Reconciling shortcuts from games.map")
        shortcut_reconcile = self.shortcuts_manager.reconcile_shortcuts_from_games_map()
        if shortcut_reconcile.get('created', 0) > 0:
            logger.info(f"[INIT] Created {shortcut_reconcile['created']} missing shortcuts from games.map")

        logger.info("[INIT] Initializing EpicConnector")
        self.epic = EpicConnector(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self)

        logger.info("[INIT] Initializing GOGAPIClient")
        self.gog = GOGAPIClient(plugin_instance=self)

        logger.info("[INIT] Initializing AmazonConnector")
        self.amazon = AmazonConnector(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self)

        logger.info("[INIT] Initializing InstallHandler")
        self.install_handler = InstallHandler(self.shortcuts_manager, plugin_dir=DECKY_PLUGIN_DIR)

        logger.info("[INIT] Initializing CloudSaveManager")
        self.cloud_save_manager = CloudSaveManager(plugin_dir=DECKY_PLUGIN_DIR)

        # NOTE: Background sync disabled due to method binding issues
        # Users can manually trigger sync via the UI
        logger.info("[INIT] Background sync service disabled")
        self.background_sync = None
        # logger.info("[INIT] Initializing BackgroundSyncService")
        # self.background_sync = BackgroundSyncService(self)

        # Initialize background size fetcher (non-blocking, persists across restarts)
        logger.info("[INIT] Initializing BackgroundSizeFetcher")
        self.size_fetcher = BackgroundSizeFetcher(self.epic, self.gog, self.amazon)
        # Auto-start if there are pending games from previous session
        self.size_fetcher.start()

        # Initialize SteamGridDB client with hardcoded API key
        self.steamgriddb_api_key = "1a410cb7c288b8f21016c2df4c81df74"

        if STEAMGRIDDB_AVAILABLE:
            try:
                logger.info("[INIT] Initializing SteamGridDB client")
                self.steamgriddb = SteamGridDBClient(self.steamgriddb_api_key)
                logger.info("[INIT] SteamGridDB client initialized successfully")
            except Exception as e:
                logger.error(f"[INIT] Failed to initialize SteamGridDB: {e}", exc_info=True)
                self.steamgriddb = None
        else:
            logger.warning("[INIT] SteamGridDB not available - skipping")
            self.steamgriddb = None

        # Global lock to prevent concurrent syncs
        self._sync_lock = asyncio.Lock()
        self._is_syncing = False  # Flag for checking without blocking
        self._cancel_sync = False  # Flag for cancelling in-progress sync

        # Initialize download queue with plugin directory for finding binaries
        logger.info(f"[INIT] Initializing DownloadQueue with plugin_dir={DECKY_PLUGIN_DIR}")
        self.download_queue = get_download_queue(DECKY_PLUGIN_DIR)
        
        # Set callback for when downloads complete
        async def on_download_complete(item):
            """Mark game as installed when download completes"""
            try:
                logger.info(f"[DownloadComplete] Processing completed download: {item.game_title}")
                
                # Get executable path from store-specific handler
                if item.store == 'epic':
                    exe_path = await self.install_handler.get_epic_game_exe(item.game_id)
                    if exe_path:
                        install_path = self.download_queue.get_install_path(item.storage_location)
                        game_install_path = os.path.join(install_path, item.game_id)
                        
                        # Write marker file to identify completed installs (matches GOG behavior)
                        # This helps protect against accidental deletion on cancel
                        try:
                            marker_path = os.path.join(game_install_path, '.unifideck-id')
                            if os.path.exists(game_install_path):
                                with open(marker_path, 'w') as f:
                                    f.write(item.game_id)
                                logger.info(f"[DownloadComplete] Wrote .unifideck-id marker for Epic game {item.game_id}")
                        except Exception as e:
                            logger.warning(f"[DownloadComplete] Failed to write marker file: {e}")
                        
                        await self.shortcuts_manager.mark_installed(
                            item.game_id, item.store, game_install_path, exe_path
                        )
                        logger.info(f"[DownloadComplete] Marked {item.game_title} as installed")
                        
                        # Invalidate legendary cache to ensure fresh status on next query
                        global _legendary_installed_cache
                        _legendary_installed_cache['data'] = None
                        logger.debug("[DownloadComplete] Invalidated legendary installed cache")
                elif item.store == 'gog':
                    # GOG installs to <install_path>/<game_title>
                    # We need to find the folder in the install location used for this download
                    
                    # 1. Start with proper search paths
                    # Order matters: [fallback, primary] ensures primary wins if found in both
                    search_paths = []
                    
                    default_gog_path = os.path.expanduser("~/GOG Games")
                    if os.path.exists(default_gog_path):
                        search_paths.append(default_gog_path)
                        
                    primary_install_path = self.download_queue.get_install_path(item.storage_location)
                    if primary_install_path != default_gog_path and os.path.exists(primary_install_path):
                        search_paths.append(primary_install_path)
                    
                    game_install_path = None
                    
                    for gog_base in search_paths:
                        # Scan directories in this base path
                        for folder in os.listdir(gog_base):
                            folder_path = os.path.join(gog_base, folder)
                            if os.path.isdir(folder_path):
                                # Check if this folder has a marker for this game ID
                                # Use GOG client's identification logic (standard .unifideck-id check)
                                found_id = self.gog._get_game_id_from_dir(folder_path)
                                if found_id == item.game_id:
                                    game_install_path = folder_path
                                    break
                                
                                # Fallback: Check for legacy filename-based marker
                                marker_path = os.path.join(folder_path, f".unifideck_gog_{item.game_id}")
                                if os.path.exists(marker_path):
                                    game_install_path = folder_path
                                    break
                    
                    if game_install_path:
                        exe_path = self.gog._find_game_executable(game_install_path)
                        if exe_path:
                            await self.shortcuts_manager.mark_installed(
                                item.game_id, item.store, game_install_path, exe_path
                            )
                            logger.info(f"[DownloadComplete] Marked {item.game_title} as installed")
                        else:
                            logger.warning(f"[DownloadComplete] No executable found for {item.game_title}")
                    else:
                        logger.warning(f"[DownloadComplete] Could not find GOG install folder for {item.game_title}")
                elif item.store == 'amazon':
                    # Amazon installs - use nile's installed.json for path info
                    game_info = self.amazon.get_installed_game_info(item.game_id)
                    if game_info:
                        game_install_path = game_info.get('path', '')
                        exe_path = game_info.get('executable')
                        
                        if game_install_path and exe_path:
                            await self.shortcuts_manager.mark_installed(
                                item.game_id, item.store, game_install_path, exe_path
                            )
                            logger.info(f"[DownloadComplete] Marked {item.game_title} as installed")
                        else:
                            logger.warning(f"[DownloadComplete] No executable found for {item.game_title}")
                    else:
                        logger.warning(f"[DownloadComplete] Could not find Amazon install info for {item.game_title}")
            except Exception as e:
                logger.error(f"[DownloadComplete] Error marking game installed: {e}")
        
        self.download_queue.set_on_complete_callback(on_download_complete)
        
        # Set GOG install callback to use GOGAPIClient
        async def gog_install_callback(game_id: str, install_path: str = None, progress_callback=None):
            """Delegate GOG downloads to GOGAPIClient.install_game"""
            return await self.gog.install_game(game_id, install_path, progress_callback)
        
        self.download_queue.set_gog_install_callback(gog_install_callback)

        logger.info("[INIT] Unifideck plugin initialization complete")

    # Frontend-callable methods

    async def has_artwork(self, app_id: int) -> bool:
        """Check if artwork files exist for this app_id"""
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return False

        # Convert signed int32 to unsigned for filename check (same as download logic)
        # Steam artwork files use unsigned app IDs even though shortcuts.vdf stores signed
        # Example: -1257913040 (signed) -> 3037054256 (unsigned)
        unsigned_id = app_id if app_id >= 0 else app_id + 2**32

        # Check for any of the 4 artwork types
        grid_path = Path(self.steamgriddb.grid_path)
        artwork_files = [
            grid_path / f"{unsigned_id}p.jpg",     # Vertical grid (460x215)
            grid_path / f"{unsigned_id}_hero.jpg", # Hero image (1920x620)
            grid_path / f"{unsigned_id}_logo.png", # Logo
            grid_path / f"{unsigned_id}_icon.jpg"  # Icon
        ]

        return any(f.exists() for f in artwork_files)

    async def fetch_artwork_with_progress(self, game, semaphore):
        """Fetch artwork for a single game with concurrency control"""
        async with semaphore:
            try:
                # Update status to show we're working on this game (before download)
                self.sync_progress.current_game = f"Downloading artwork for {game.title}..."
                
                # Pass store and store_id for official CDN artwork sources
                result = await self.steamgriddb.fetch_game_art(
                    game.title, 
                    game.app_id,
                    store=game.store,      # 'epic', 'gog', or 'amazon'
                    store_id=game.id       # Store-specific game ID (e.g. GOG product ID, Epic app_name, Amazon game ID)
                )
                
                # Store steam_app_id from search (for ProtonDB lookups)
                if result.get('steam_app_id'):
                    game.steam_app_id = result['steam_app_id']

                # Update progress (thread-safe)
                count = await self.sync_progress.increment_artwork(game.title)
                
                # Build detailed source log
                sources = result.get('sources', [])
                art_count = result.get('artwork_count', 0)
                sgdb = '+SGDB' if result.get('sgdb_filled') else ''
                source_str = ' '.join(sources) if sources else 'NO_SOURCE'
                
                # Log format: [progress] STORE: Title [sources] (artwork_count)
                logger.info(f"  [{count}/{self.sync_progress.artwork_total}] {game.store.upper()}: {game.title} [{source_str}{sgdb}] ({art_count}/4)")

                return result.get('success', False)
            except Exception as e:
                logger.error(f"Error fetching artwork for {game.title}: {e}")
                await self.sync_progress.increment_artwork(game.title)
                return False

    async def sync_libraries(self, fetch_artwork: bool = True) -> Dict[str, Any]:
        """Sync all game libraries to shortcuts.vdf and optionally fetch artwork - with global lock protection"""

        # Check if sync already running (non-blocking check)
        if self._is_syncing:
            logger.warning("Sync already in progress, ignoring request")
            return {
                'success': False,
                'error': 'A sync operation is already in progress',
                'epic_count': 0,
                'gog_count': 0,
                'added_count': 0,
                'artwork_count': 0
            }

        # Acquire lock (prevents concurrent syncs)
        async with self._sync_lock:
            self._is_syncing = True
            self._cancel_sync = False  # Reset cancel flag
            try:
                logger.info("Syncing libraries...")

                # Update progress: Fetching games
                self.sync_progress.status = "fetching"
                self.sync_progress.current_game = "Fetching game lists..."
                self.sync_progress.error = None

                # Get games from all stores
                epic_games = await self.epic.get_library()
                gog_games = await self.gog.get_library()
                amazon_games = await self.amazon.get_library()

                # Robustly handle API failures (None returns)
                valid_stores = []
                all_games = []

                if epic_games is not None:
                    valid_stores.append('epic')
                    all_games.extend(epic_games)
                else:
                    epic_games = [] # For iteration below

                if gog_games is not None:
                     valid_stores.append('gog')
                     all_games.extend(gog_games)
                else:
                     gog_games = []

                if amazon_games is not None:
                     valid_stores.append('amazon')
                     all_games.extend(amazon_games)
                else:
                     amazon_games = []

                # Check for cancellation
                if self._cancel_sync:
                    logger.warning("Sync cancelled by user after fetching libraries")
                    self.sync_progress.status = "cancelled"
                    self.sync_progress.current_game = "Sync cancelled by user"
                    return {
                        'success': False,
                        'error': 'Sync cancelled by user',
                        'cancelled': True,
                        'epic_count': 0,
                        'gog_count': 0,
                        'amazon_count': 0,
                        'added_count': 0,
                        'updated_count': 0,
                        'artwork_count': 0
                    }
                self.sync_progress.total_games = len(all_games)
                self.sync_progress.synced_games = 0

                # Update progress: Checking installed status
                self.sync_progress.status = "checking_installed"
                self.sync_progress.current_game = "Checking installed games..."

                # Get installed games
                epic_installed = await self.epic.get_installed()
                gog_installed = await self.gog.get_installed()
                amazon_installed = await self.amazon.get_installed()

                # Mark installed status
                for game in epic_games:
                    if game.id in epic_installed:
                        game.is_installed = True
                        
                        # OPTIMIZATION: Use cached metadata to get EXE path instead of slow subprocess
                        # epic_installed is now a dict: {app_name: metadata}
                        metadata = epic_installed[game.id]
                        
                        install_path = metadata.get('install', {}).get('install_path')
                        executable = metadata.get('manifest', {}).get('launch_exe')
                        
                        exe_path = None
                        if install_path and executable:
                            exe_path = os.path.join(install_path, executable)
                        elif metadata.get('install_path') and metadata.get('executable'):
                            # Fallback structure
                             exe_path = os.path.join(metadata['install_path'], metadata['executable'])
                             
                        if exe_path:
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('epic', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for Epic game {game.id} (FAST)")
                        else:
                            # Fallback to slow method if metadata missing
                            logger.debug(f"Metadata missing for {game.id}, falling back to slow check")
                            exe_path = await self.install_handler.get_epic_game_exe(game.id)
                            if exe_path:
                                work_dir = os.path.dirname(exe_path)
                                await self.shortcuts_manager._update_game_map('epic', game.id, exe_path, work_dir)

                for game in gog_games:
                    if game.id in gog_installed:
                        game.is_installed = True
                        # Ensure games.map is updated for games installed outside Unifideck
                        game_info = self.gog.get_installed_game_info(game.id)
                        if game_info and game_info.get('executable'):
                            exe_path = game_info['executable']
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('gog', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for GOG game {game.id}")

                for game in amazon_games:
                    if game.id in amazon_installed:
                        game.is_installed = True
                        # Ensure games.map is updated for Amazon games
                        game_info = self.amazon.get_installed_game_info(game.id)
                        if game_info and game_info.get('executable'):
                            exe_path = game_info['executable']
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('amazon', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for Amazon game {game.id}")

                # Get launcher script path (relative to plugin directory)
                launcher_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')

                # --- QUEUE SIZE FETCHING (background, non-blocking) ---
                # Sizes are fetched asynchronously in background, don't hold up sync
                self.size_fetcher.queue_games(all_games)
                self.size_fetcher.start()  # Fire-and-forget

                # --- STEP 1: ARTWORK (Queue & Download) ---
                # We do this BEFORE writing shortcuts so shortcuts can point to local icons
                
                artwork_count = 0
                games_needing_art = []
                steam_appid_cache = load_steam_appid_cache()  # Load existing cache
                
                # Pre-calculate app_ids for all games (fast, no I/O)
                for game in all_games:
                     game.app_id = self.shortcuts_manager.generate_app_id(game.title, launcher_script)

                if fetch_artwork and self.steamgriddb:
                    # STEP 1: Identify games needing SGDB lookup (not in cache)
                    seen_app_ids = set()
                    games_needing_sgdb_lookup = []
                    
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            continue
                        seen_app_ids.add(game.app_id)
                        
                        # Check cache first
                        if game.app_id in steam_appid_cache:
                            game.steam_app_id = steam_appid_cache[game.app_id]
                        else:
                            games_needing_sgdb_lookup.append(game)
                    
                    # STEP 2: Parallel SteamGridDB lookups for uncached games
                    if games_needing_sgdb_lookup:
                        logger.info(f"Looking up {len(games_needing_sgdb_lookup)} games on SteamGridDB (parallel)...")
                        self.sync_progress.status = "sgdb_lookup"
                        self.sync_progress.current_game = f"Looking up {len(games_needing_sgdb_lookup)} games on SteamGridDB..."
                        
                        # Helper to lookup and store result
                        async def lookup_sgdb(game):
                            try:
                                sgdb_id = await self.steamgriddb.search_game(game.title)
                                if sgdb_id:
                                    game.steam_app_id = sgdb_id
                                    return (game.app_id, sgdb_id)
                            except Exception as e:
                                logger.debug(f"SGDB lookup failed for {game.title}: {e}")
                            return None
                        
                        # 20 concurrent lookups (fast!)
                        semaphore = asyncio.Semaphore(30)
                        async def limited_lookup(game):
                            async with semaphore:
                                return await lookup_sgdb(game)
                        
                        results = await asyncio.gather(*[limited_lookup(g) for g in games_needing_sgdb_lookup])
                        
                        # Update cache with results
                        for result in results:
                            if result:
                                app_id, sgdb_id = result
                                steam_appid_cache[app_id] = sgdb_id
                        
                        logger.info(f"SteamGridDB lookup complete: {sum(1 for r in results if r)} found")
                    
                    # Save updated cache
                    if steam_appid_cache:
                        save_steam_appid_cache(steam_appid_cache)

                    # STEP 3: Check which games need artwork (quick local file check)
                    self.sync_progress.status = "checking_artwork"
                    self.sync_progress.current_game = "Checking existing artwork..."
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            if not await self.has_artwork(game.app_id):
                                games_needing_art.append(game)
                            seen_app_ids.discard(game.app_id)  # Only check once per app_id

                    if games_needing_art:
                        logger.info(f"Fetching artwork for {len(games_needing_art)} games...")
                        self.sync_progress.current_phase = "artwork"
                        self.sync_progress.status = "artwork"
                        self.sync_progress.artwork_total = len(games_needing_art)
                        self.sync_progress.artwork_synced = 0
                        
                        # Reset main counters for clean UI
                        self.sync_progress.synced_games = 0
                        self.sync_progress.total_games = 0

                        # Check cancellation
                        if self._cancel_sync:
                             logger.warning("Sync cancelled before artwork")
                             return {'success': False, 'cancelled': True}

                        # Download in parallel - 30 concurrent (10 per source × 3 sources)
                        logger.info(f"  → Starting parallel download (concurrency: 30, sources: Epic/GOG/Amazon CDN + Steam + SGDB fallback)")
                        semaphore = asyncio.Semaphore(30)
                        tasks = [self.fetch_artwork_with_progress(game, semaphore) for game in games_needing_art]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        artwork_count = sum(1 for r in results if r is True)
                        
                        logger.info(f"Artwork download complete: {artwork_count}/{len(games_needing_art)} games successful")

                # --- STEP 2: UPDATE GAME ICONS ---
                # Check for local icons and update game objects
                if self.steamgriddb and self.steamgriddb.grid_path:
                    grid_path = Path(self.steamgriddb.grid_path)
                    for game in all_games:
                        # Convert signed int32 to unsigned for filename check
                        unsigned_id = game.app_id if game.app_id >= 0 else game.app_id + 2**32
                        icon_path = grid_path / f"{unsigned_id}_icon.jpg"
                        
                        if icon_path.exists():
                             # If we have a local icon, use it!
                             game.cover_image = str(icon_path)
                             # shortcuts_manager.add_games_batch will use game.cover_image for the 'icon' field
                
                # --- STEP 3: WRITE SHORTCUTS ---
                self.sync_progress.status = "syncing"
                self.sync_progress.current_game = "Saving shortcuts..."
                
                # Use valid_stores to prevent deleting shortcuts for stores that failed to sync
                batch_result = await self.shortcuts_manager.add_games_batch(all_games, launcher_script, valid_stores=valid_stores)
                added_count = batch_result.get('added', 0)
                
                if batch_result.get('error'):
                     raise Exception(batch_result['error'])

                # Complete
                self.sync_progress.status = "complete"
                self.sync_progress.synced_games = len(all_games)
                self.sync_progress.current_game = f"Sync completed! Added {added_count}, Art {artwork_count}."

                if added_count > 0 or artwork_count > 0:
                    logger.warning("=" * 60)
                    logger.warning("IMPORTANT: Steam restart required!")
                    logger.warning("Please EXIT Steam completely and restart to see changes")
                    logger.warning("=" * 60)

                return {
                    'success': True,
                    'epic_count': len(epic_games),
                    'gog_count': len(gog_games),
                    'amazon_count': len(amazon_games),
                    'added_count': added_count,
                    'artwork_count': artwork_count
                }

            except Exception as e:
                logger.error(f"Error syncing libraries: {e}")
                self.sync_progress.status = "error"
                self.sync_progress.error = str(e)
                return {'success': False, 'error': str(e)}

            finally:
                self._is_syncing = False

    async def force_sync_libraries(self) -> Dict[str, Any]:
        """
        Force sync all libraries - rewrites ALL existing Unifideck shortcuts and compatibility data.
        Does NOT re-download artwork (preserves existing artwork).
        
        This is useful when:
        - Shortcut exe paths need to be updated
        - Compatibility/Proton settings need to be refreshed
        - Games were installed externally and need proper configuration
        """
        # Check if sync already running (non-blocking check)
        if self._is_syncing:
            logger.warning("Sync already in progress, ignoring force sync request")
            return {
                'success': False,
                'error': 'A sync operation is already in progress',
                'epic_count': 0,
                'gog_count': 0,
                'added_count': 0,
                'updated_count': 0,
                'artwork_count': 0
            }

        # Acquire lock (prevents concurrent syncs)
        async with self._sync_lock:
            self._is_syncing = True
            self._cancel_sync = False  # Reset cancel flag
            try:
                logger.info("Force syncing libraries (rewriting all shortcuts and compatibility data)...")

                # Update progress: Fetching games
                self.sync_progress.status = "fetching"
                self.sync_progress.current_game = "Force sync: Fetching game lists..."
                self.sync_progress.error = None

                # Get games from all stores
                epic_games = await self.epic.get_library()
                gog_games = await self.gog.get_library()
                amazon_games = await self.amazon.get_library()

                # Robustly handle API failures (None returns)
                valid_stores = []
                all_games = []

                if epic_games is not None:
                    valid_stores.append('epic')
                    all_games.extend(epic_games)
                else:
                    epic_games = []

                if gog_games is not None:
                    valid_stores.append('gog')
                    all_games.extend(gog_games)
                else:
                    gog_games = []

                if amazon_games is not None:
                    valid_stores.append('amazon')
                    all_games.extend(amazon_games)
                else:
                    amazon_games = []
                self.sync_progress.total_games = len(all_games)
                self.sync_progress.synced_games = 0

                # Update progress: Checking installed status
                self.sync_progress.status = "checking_installed"
                self.sync_progress.current_game = "Force sync: Checking installed games..."

                # Get installed games
                epic_installed = await self.epic.get_installed()
                gog_installed = await self.gog.get_installed()
                amazon_installed = await self.amazon.get_installed()

                # Mark installed status and update games.map
                for game in epic_games:
                    if game.id in epic_installed:
                        game.is_installed = True
                        exe_path = await self.install_handler.get_epic_game_exe(game.id)
                        if exe_path:
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('epic', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for Epic game {game.id}")

                for game in gog_games:
                    if game.id in gog_installed:
                        game.is_installed = True
                        game_info = self.gog.get_installed_game_info(game.id)
                        if game_info and game_info.get('executable'):
                            exe_path = game_info['executable']
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('gog', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for GOG game {game.id}")

                for game in amazon_games:
                    if game.id in amazon_installed:
                        game.is_installed = True
                        game_info = self.amazon.get_installed_game_info(game.id)
                        if game_info and game_info.get('executable'):
                            exe_path = game_info['executable']
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('amazon', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for Amazon game {game.id}")

                # Get launcher script path
                launcher_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')

                # --- QUEUE SIZE FETCHING (background, non-blocking) ---
                # Sizes are fetched asynchronously in background, don't hold up sync
                self.size_fetcher.queue_games(all_games)
                self.size_fetcher.start()  # Fire-and-forget

                # Update progress: Force syncing
                self.sync_progress.status = "syncing"
                self.sync_progress.current_game = "Force sync: Rewriting shortcuts..."

                # Force update all games - rewrite existing shortcuts
                batch_result = await self.shortcuts_manager.force_update_games_batch(all_games, launcher_script, valid_stores=valid_stores)
                added_count = batch_result.get('added', 0)
                updated_count = batch_result.get('updated', 0)

                if batch_result.get('error'):
                    logger.error(f"Force batch update failed: {batch_result['error']}")
                    self.sync_progress.status = "error"
                    self.sync_progress.error = batch_result['error']
                    return {'success': False, 'error': batch_result['error']}

                logger.info(f"Force batch write complete: {added_count} games added, {updated_count} updated")

                # Check for cancellation after shortcuts written
                if self._cancel_sync:
                    logger.warning("Force sync cancelled by user after writing shortcuts")
                    self.sync_progress.status = "cancelled"
                    self.sync_progress.current_game = "Force sync cancelled - shortcuts saved"
                    return {
                        'success': True,
                        'cancelled': True,
                        'epic_count': len(epic_games),
                        'gog_count': len(gog_games),
                        'amazon_count': len(amazon_games),
                        'added_count': added_count,
                        'updated_count': updated_count,
                        'artwork_count': 0
                    }

                # Get launcher script path (relative to plugin directory)
                launcher_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')
                
                # --- STEP 1: ARTWORK (Queue & Download) ---
                # We do this BEFORE writing shortcuts so shortcuts can point to local icons
                
                artwork_count = 0
                games_needing_art = []
                steam_appid_cache = load_steam_appid_cache()  # Load existing cache
                
                # Pre-calculate app_ids for all games
                for game in all_games:
                     game.app_id = self.shortcuts_manager.generate_app_id(game.title, launcher_script)

                if self.steamgriddb:
                    # STEP 1: Identify games needing SGDB lookup (not in cache)
                    seen_app_ids = set()
                    games_needing_sgdb_lookup = []
                    
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            continue
                        seen_app_ids.add(game.app_id)
                        
                        # Check cache first
                        if game.app_id in steam_appid_cache:
                            game.steam_app_id = steam_appid_cache[game.app_id]
                        else:
                            games_needing_sgdb_lookup.append(game)
                    
                    # STEP 2: Parallel SteamGridDB lookups for uncached games
                    if games_needing_sgdb_lookup:
                        logger.info(f"Force Sync: Looking up {len(games_needing_sgdb_lookup)} games on SteamGridDB (parallel)...")
                        self.sync_progress.status = "sgdb_lookup"
                        self.sync_progress.current_game = f"Looking up {len(games_needing_sgdb_lookup)} games on SteamGridDB..."
                        
                        # Helper to lookup and store result
                        async def lookup_sgdb(game):
                            try:
                                sgdb_id = await self.steamgriddb.search_game(game.title)
                                if sgdb_id:
                                    game.steam_app_id = sgdb_id
                                    return (game.app_id, sgdb_id)
                            except Exception as e:
                                logger.debug(f"SGDB lookup failed for {game.title}: {e}")
                            return None
                        
                        # 30 concurrent lookups (10 per source × 3 sources)
                        semaphore = asyncio.Semaphore(30)
                        async def limited_lookup(game):
                            async with semaphore:
                                return await lookup_sgdb(game)
                        
                        results = await asyncio.gather(*[limited_lookup(g) for g in games_needing_sgdb_lookup])
                        
                        # Update cache with results
                        for result in results:
                            if result:
                                app_id, sgdb_id = result
                                steam_appid_cache[app_id] = sgdb_id
                        
                        logger.info(f"SteamGridDB lookup complete: {sum(1 for r in results if r)} found")
                    
                    # Save updated cache
                    if steam_appid_cache:
                        save_steam_appid_cache(steam_appid_cache)

                    # STEP 3: Check which games need artwork (quick local file check)
                    self.sync_progress.status = "checking_artwork"
                    self.sync_progress.current_game = "Checking existing artwork..."
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            if not await self.has_artwork(game.app_id):
                                games_needing_art.append(game)
                            seen_app_ids.discard(game.app_id)  # Only check once per app_id

                    if games_needing_art:
                        logger.info(f"Force Sync: Fetching artwork for {len(games_needing_art)} games...")
                        self.sync_progress.current_phase = "artwork"
                        self.sync_progress.status = "artwork"
                        self.sync_progress.artwork_total = len(games_needing_art)
                        self.sync_progress.artwork_synced = 0
                        
                        # Reset main counters for clean UI
                        self.sync_progress.synced_games = 0
                        self.sync_progress.total_games = 0

                        # Check cancellation
                        if self._cancel_sync:
                             logger.warning("Force Sync cancelled before artwork")
                             return {'success': False, 'cancelled': True}

                        # Download in parallel - 30 concurrent
                        logger.info(f"  → Starting parallel download (concurrency: 30, sources: Epic/GOG/Amazon CDN + Steam + SGDB fallback)")
                        semaphore = asyncio.Semaphore(30)
                        tasks = [self.fetch_artwork_with_progress(game, semaphore) for game in games_needing_art]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        artwork_count = sum(1 for r in results if r is True)
                        
                        logger.info(f"Artwork download complete: {artwork_count}/{len(games_needing_art)} games successful")

                # --- STEP 2: UPDATE GAME ICONS ---
                # Check for local icons and update game objects
                if self.steamgriddb and self.steamgriddb.grid_path:
                    grid_path = Path(self.steamgriddb.grid_path)
                    for game in all_games:
                        # Convert signed int32 to unsigned for filename check
                        unsigned_id = game.app_id if game.app_id >= 0 else game.app_id + 2**32
                        icon_path = grid_path / f"{unsigned_id}_icon.jpg"
                        
                        if icon_path.exists():
                             # If we have a local icon, use it!
                             game.cover_image = str(icon_path)

                # --- STEP 3: WRITE SHORTCUTS ---
                self.sync_progress.current_game = "Force Sync: Writing shortcuts..."
                
                # Force update all games - rewrites existing shortcuts with fresh data (and local icons)
                force_result = await self.shortcuts_manager.force_update_games_batch(all_games, launcher_script)
                
                added_count = force_result.get('added', 0)
                updated_count = force_result.get('updated', 0)
                
                if force_result.get('error'):
                    raise Exception(force_result['error'])

                # CLEAR Proton compatibility for all Unifideck games
                # The unifideck-launcher script handles Proton internally via umu-run
                # We DON'T want Steam to wrap our launcher in Proton (causes Python path issues)
                self.sync_progress.status = "proton_setup"
                self.sync_progress.current_game = "Force sync: Clearing Proton compatibility (launcher manages internally)..."
                proton_cleared = 0
                
                shortcuts_data = await self.shortcuts_manager.read_shortcuts()
                for idx, shortcut in shortcuts_data.get('shortcuts', {}).items():
                    launch_opts = shortcut.get('LaunchOptions', '')
                    app_id = shortcut.get('appid')
                    
                    # Check if this is a Unifideck game (has store:game_id format in LaunchOptions)
                    if ':' in launch_opts and app_id:
                        store_prefix = launch_opts.split(':')[0]
                        if store_prefix in ('epic', 'gog', 'amazon'):
                            await self.shortcuts_manager._clear_proton_compatibility(app_id)
                            proton_cleared += 1
                
                logger.info(f"Cleared Proton compatibility for {proton_cleared} games (launcher manages Proton via umu-run)")

                # Complete
                self.sync_progress.status = "complete"
                self.sync_progress.synced_games = len(all_games)
                self.sync_progress.current_game = f"Force sync completed! Updated {updated_count} games, fetched {artwork_count} artwork."

                # Notify about Steam restart requirement
                if added_count > 0 or updated_count > 0 or artwork_count > 0:
                    logger.warning("=" * 60)
                    logger.warning("IMPORTANT: Steam restart required!")
                    logger.warning("Please EXIT Steam completely and restart to see changes")
                    logger.warning("=" * 60)

                logger.info(f"Force synced {len(epic_games)} Epic + {len(gog_games)} GOG + {len(amazon_games)} Amazon games ({added_count} added, {updated_count} updated, {artwork_count} artwork)")
                return {
                    'success': True,
                    'epic_count': len(epic_games),
                    'gog_count': len(gog_games),
                    'amazon_count': len(amazon_games),
                    'added_count': added_count,
                    'updated_count': updated_count,
                    'artwork_count': artwork_count
                }

            except Exception as e:
                logger.error(f"Error force syncing libraries: {e}")
                self.sync_progress.status = "error"
                self.sync_progress.error = str(e)
                return {'success': False, 'error': str(e)}

            finally:
                self._is_syncing = False

    async def start_background_sync(self) -> Dict[str, Any]:
        """Start background sync service"""
        if self.background_sync:
            await self.background_sync.start()
            return {'success': True}
        else:
            return {'success': False, 'error': 'Background sync disabled'}

    async def stop_background_sync(self) -> Dict[str, Any]:
        """Stop background sync service"""
        if self.background_sync:
            await self.background_sync.stop()
            return {'success': True}
        else:
            return {'success': False, 'error': 'Background sync disabled'}

    async def get_sync_progress(self) -> Dict[str, Any]:
        """Get current sync progress for frontend polling"""
        return self.sync_progress.to_dict()

    async def get_sync_status(self) -> Dict[str, Any]:
        """Check if a sync operation is currently running"""
        return {
            'is_syncing': self._is_syncing,
            'sync_progress': self.sync_progress.to_dict() if self._is_syncing else None
        }

    async def cancel_sync(self) -> Dict[str, Any]:
        """Request cancellation of current sync operation"""
        if not self._is_syncing:
            return {
                'success': False,
                'message': 'No sync operation in progress'
            }

        logger.warning("Sync cancellation requested by user")
        self._cancel_sync = True
        return {
            'success': True,
            'message': 'Sync cancellation requested - will stop after current operation'
        }

    async def _get_epic_executable(self, game_id: str) -> Optional[str]:
        """Get executable path for Epic game using legendary info

        Args:
            game_id: Epic game app_name (ID)

        Returns:
            Full path to game executable, or None if not found
        """
        if not self.epic.legendary_bin:
            logger.warning("[Epic] Legendary binary not found, cannot get executable path")
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                self.epic.legendary_bin, 'info', game_id, '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                info = json.loads(stdout.decode())
                install_path = info.get('install', {}).get('install_path', '')
                executable = info.get('manifest', {}).get('launch_exe', '')

                if install_path and executable:
                    exe_path = os.path.join(install_path, executable)
                    logger.info(f"[Epic] Found executable: {exe_path}")
                    return exe_path
                else:
                    logger.warning(f"[Epic] Missing install_path or executable in legendary info for {game_id}")
            else:
                error_msg = stderr.decode() if stderr else 'Unknown error'
                logger.warning(f"[Epic] legendary info failed for {game_id}: {error_msg}")

        except Exception as e:
            logger.error(f"[Epic] Error getting executable for {game_id}: {e}", exc_info=True)

        return None

    async def install_game(self, game_id: str, store: str) -> Dict[str, Any]:
        """Install a game from specified store"""
        logger.info(f"Installing {game_id} from {store}")

        try:
            if store == 'epic':
                return await self.install_handler.install_epic_game(game_id)
            elif store == 'gog':
                return await self.install_handler.install_gog_game(game_id)
            else:
                return {'success': False, 'error': f'Unknown store: {store}'}

        except Exception as e:
            logger.error(f"Error installing game: {e}")
            return {'success': False, 'error': str(e)}


    async def sync_cloud_saves(self, store: str, game_id: str, direction: str = "download", 
                               game_name: str = "", save_path: str = "") -> Dict[str, Any]:
        """
        Sync cloud saves for a game.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID (Epic app_name or GOG client_id)
            direction: "download" (pull from cloud) or "upload" (push to cloud)
            game_name: Optional game title for logging
            save_path: For GOG games, the local save path (required)
        
        Returns:
            {success, message, duration, error?}
        """
        logger.info(f"[API] sync_cloud_saves called: store={store}, game_id={game_id}, direction={direction}")
        
        try:
            if store == "epic":
                return await self.cloud_save_manager.sync_epic(game_id, direction=direction, game_name=game_name)
            elif store == "gog":
                return await self.cloud_save_manager.sync_gog(game_id, save_path=save_path, 
                                                              direction=direction, game_name=game_name)
            else:
                error = f"Unknown store: {store}"
                logger.error(f"[API] {error}")
                return {"success": False, "error": error}
        except Exception as e:
            logger.error(f"[API] sync_cloud_saves error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def get_cloud_save_status(self, store: str, game_id: str) -> Dict[str, Any]:
        """
        Get the last cloud save sync status for a game.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
        
        Returns:
            {last_sync, status, direction, error?} or None if never synced
        """
        logger.info(f"[API] get_cloud_save_status called: store={store}, game_id={game_id}")
        
        status = self.cloud_save_manager.get_sync_status(store, game_id)
        if status:
            return {"success": True, "status": status}
        return {"success": True, "status": None}
    
    async def check_cloud_save_conflict(self, store: str, game_id: str, 
                                        save_path: str = "") -> Dict[str, Any]:
        """
        Check if a game has cloud save conflicts.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
            save_path: Local save path (for file timestamp comparison)
        
        Returns:
            {has_conflict, is_fresh, local_timestamp, cloud_timestamp, local_newer}
        """
        logger.info(f"[API] check_cloud_save_conflict: {store}/{game_id}")
        
        try:
            conflict = await self.cloud_save_manager.check_for_conflicts(store, game_id, save_path)
            return {"success": True, "conflict": conflict}
        except Exception as e:
            logger.error(f"[API] check_cloud_save_conflict error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def resolve_cloud_save_conflict(self, store: str, game_id: str, 
                                          use_cloud: bool) -> Dict[str, Any]:
        """
        Resolve a cloud save conflict.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
            use_cloud: True to use cloud saves, False to upload local saves
        
        Returns:
            {action: "download" or "upload"}
        """
        logger.info(f"[API] resolve_cloud_save_conflict: {store}/{game_id}, use_cloud={use_cloud}")
        
        try:
            result = self.cloud_save_manager.resolve_conflict(store, game_id, use_cloud)
            return {"success": True, **result}
        except Exception as e:
            logger.error(f"[API] resolve_cloud_save_conflict error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def start_game_monitor(self, pid: int, store: str, game_id: str,
                                  game_name: str = "", save_path: str = "") -> Dict[str, Any]:
        """
        Start monitoring a game process for cloud save sync on exit.
        
        Args:
            pid: Process ID of the running game
            store: "epic" or "gog"
            game_id: Game ID
            game_name: Game title for logging
            save_path: Local save path (for GOG)
        
        Returns:
            {success, message}
        """
        logger.info(f"[API] start_game_monitor: PID={pid}, {store}/{game_id}")
        
        try:
            success = await self.cloud_save_manager.process_monitor.start_monitoring(
                pid, store, game_id, game_name, save_path
            )
            return {
                "success": success,
                "message": f"Monitoring PID {pid}" if success else f"Process {pid} not found"
            }
        except Exception as e:
            logger.error(f"[API] start_game_monitor error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def get_pending_conflicts(self) -> Dict[str, Any]:
        """
        Get all pending cloud save conflicts that need resolution.
        
        Returns:
            {conflicts: {store:game_id: conflict_info}}
        """
        logger.info("[API] get_pending_conflicts called")
        
        try:
            conflicts = self.cloud_save_manager.get_pending_conflicts()
            return {"success": True, "conflicts": conflicts}
        except Exception as e:
            logger.error(f"[API] get_pending_conflicts error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def check_game_installation_status(self, store: str, game_id: str) -> bool:
        """
        **SINGLE SOURCE OF TRUTH** for checking if a game is installed.
        
        Uses priority-based lookup:
        Priority 1: games.map (fast, authoritative for Unifideck-installed games)
        Priority 2: Store-specific check (for games installed outside Unifideck)
        
        Args:
            store: 'epic' or 'gog'
            game_id: Store-specific game identifier
            
        Returns:
            True if game is installed, False otherwise
        """
        # Priority 1: Check games.map (authoritative for all Unifideck installs)
        # This works for any install location (internal, SD card, etc.)
        is_installed = self.shortcuts_manager._is_in_game_map(store, game_id)
        
        # Priority 2: Fall back to store-specific check (for games installed outside Unifideck)
        if not is_installed:
            if store == 'epic':
                installed_games = await self.epic.get_installed()
                is_installed = game_id in installed_games
            elif store == 'gog':
                installed_ids = await self.gog.get_installed()
                is_installed = game_id in installed_ids
        
        return is_installed

    async def get_game_info(self, app_id: int) -> Dict[str, Any]:
        """Get game info including installation status and size

        Args:
            app_id: Steam shortcut app ID (can be signed or unsigned)

        Returns:
            Dict with game info: {
                'is_installed': bool,
                'store': 'epic' | 'gog',
                'game_id': str,
                'title': str,
                'size_bytes': int | None,
                'size_formatted': str | None (e.g., "18.52 GB"),
                'app_id': int
            }
        """
        try:
            # Convert unsigned to signed for shortcuts.vdf lookup
            # Steam URLs show unsigned (3037054256) but shortcuts.vdf stores signed (-1257913040)
            if app_id > 2**31:
                app_id_signed = app_id - 2**32
                logger.info(f"[GameInfo] Converted unsigned {app_id} to signed {app_id_signed}")
            else:
                app_id_signed = app_id

            shortcuts = await self.shortcuts_manager.read_shortcuts()

            # Find shortcut by app_id (using signed value)
            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                shortcut_app_id = shortcut.get('appid')
                if shortcut_app_id == app_id_signed:
                    # Parse LaunchOptions to get store and game_id
                    launch_options = shortcut.get('LaunchOptions', '')
                    if ':' not in launch_options:
                        return {'error': 'Invalid launch options format'}

                    store, game_id = launch_options.split(':', 1)

                    # Check installation status
                    # Priority 1: Check games.map (fast, authoritative for Unifideck-installed games)
                    # This works for any install location (internal, SD card, etc.)
                    is_installed = self.shortcuts_manager._is_in_game_map(store, game_id)

                    # Priority 2: Fall back to store-specific check (for games installed outside Unifideck)
                    if not is_installed:
                        if store == 'epic':
                            installed_games = await self.epic.get_installed()
                            is_installed = game_id in installed_games
                        elif store == 'gog':
                            installed_ids = await self.gog.get_installed()
                            is_installed = game_id in installed_ids
                        elif store == 'amazon':
                            installed_ids = await self.amazon.get_installed()
                            is_installed = game_id in installed_ids
                        elif store not in ('epic', 'gog', 'amazon'):
                            return {'error': f'Unknown store: {store}'}

                    # Get game size - try cache first (instant), fallback to API (slow)
                    size_bytes = None
                    size_formatted = None
                    try:
                        # Try persistent cache first (populated during sync)
                        size_bytes = get_cached_game_size(store, game_id)
                        
                        if size_bytes is None:
                            # Cache miss - fallback to live fetch (slow)
                            logger.debug(f"[GameInfo] Size cache miss for {store}:{game_id}, fetching from API...")
                            if store == 'epic':
                                size_bytes = await self.epic.get_game_size(game_id)
                            elif store == 'gog':
                                size_bytes = await self.gog.get_game_size(game_id)
                            elif store == 'amazon':
                                size_bytes = await self.amazon.get_game_size(game_id)
                            
                            # Cache the result for next time
                            if size_bytes and size_bytes > 0:
                                cache_game_size(store, game_id, size_bytes)
                        
                        if size_bytes and size_bytes > 0:
                            # Format size nicely
                            if size_bytes >= 1024**3:
                                size_formatted = f"{size_bytes / (1024**3):.1f} GB"
                            elif size_bytes >= 1024**2:
                                size_formatted = f"{size_bytes / (1024**2):.0f} MB"
                            else:
                                size_formatted = f"{size_bytes / 1024:.0f} KB"
                    except Exception as e:
                        logger.debug(f"[GameInfo] Could not get size for {game_id}: {e}")

                    logger.info(f"[GameInfo] App {app_id}: {shortcut.get('AppName')} - Installed: {is_installed}, Size: {size_formatted}")

                    return {
                        'is_installed': is_installed,
                        'store': store,
                        'game_id': game_id,
                        'title': shortcut.get('AppName', ''),
                        'size_bytes': size_bytes,
                        'size_formatted': size_formatted,
                        'app_id': app_id
                    }

            logger.warning(f"[GameInfo] App ID {app_id} not found in shortcuts")
            return {'error': 'Game not found'}

        except Exception as e:
            logger.error(f"Error getting game info for app {app_id}: {e}")
            return {'error': str(e)}

    async def install_game_by_appid(self, app_id: int) -> Dict[str, Any]:
        """Install game by Steam shortcut app ID

        Args:
            app_id: Steam shortcut app ID

        Returns:
            Dict with success status and progress updates
        """
        try:
            # Get game info first
            game_info = await self.get_game_info(app_id)

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

    async def uninstall_game_by_appid(self, app_id: int) -> Dict[str, Any]:
        """Uninstall game by Steam shortcut app ID"""
        try:
            # Get game info first
            game_info = await self.get_game_info(app_id)

            if 'error' in game_info:
                return game_info

            store = game_info['store']
            game_id = game_info['game_id']
            title = game_info['title']
            
            # Check if actually installed
            if not game_info.get('is_installed'):
                 return {'success': False, 'error': 'Game is not installed'}

            logger.info(f"[Uninstall] Starting uninstallation: {title} ({store}:{game_id})")

            # Perform store-specific uninstall
            if store == 'epic':
                # legendary uninstall <id> --yes
                if not self.epic.legendary_bin:
                    return {'success': False, 'error': 'Legendary CLI not found'}
                
                # Clean up stale legendary lock files (legendary returns 0 even when blocked by lock)
                lock_dir = os.path.expanduser("~/.config/legendary")
                for lock_file in ['installed.json.lock', 'user.json.lock']:
                    lock_path = os.path.join(lock_dir, lock_file)
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
                    return {'success': False, 'error': 'Legendary lock conflict - please try again'}
                
                if proc.returncode != 0:
                     logger.error(f"[Epic] Uninstall failed: {stderr_str}")
                     return {'success': False, 'error': f"Legendary uninstall failed: {stderr_str}"}
            
            elif store == 'gog':
                result = await self.gog.uninstall_game(game_id)
                if not result['success']:
                    return result
            
            elif store == 'amazon':
                result = await self.amazon.uninstall_game(game_id)
                if not result['success']:
                    return result
            
            else:
                return {'success': False, 'error': f"Unsupported store for uninstall: {store}"}

            # Update shortcut
            logger.info(f"[Uninstall] Reverting shortcut for {title}...")
            shortcut_updated = await self.shortcuts_manager.mark_uninstalled(title, store, game_id)
            
            if not shortcut_updated:
                logger.warning(f"[Uninstall] Failed to revert shortcut for {title}")
                return {
                    'success': True, 
                    'message': 'Game uninstalled, but shortcut could not be updated. Restart Steam to fix.'
                }

            return {
                'success': True,
                'message': f'{title} uninstalled successfully'
            }

        except Exception as e:
            logger.error(f"[Uninstall] Error uninstalling game {app_id}: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    # ============== DOWNLOAD QUEUE API METHODS ==============

    async def get_download_queue_info(self) -> Dict[str, Any]:
        """Get current download queue status for frontend display"""
        try:
            return {
                'success': True,
                **self.download_queue.get_queue_info()
            }
        except Exception as e:
            logger.error(f"[DownloadQueue] Error getting queue info: {e}")
            return {'success': False, 'error': str(e)}

    async def add_to_download_queue(self, game_id: str, game_title: str, store: str, was_previously_installed: bool = False) -> Dict[str, Any]:
        """Add a game to the download queue
        
        Args:
            game_id: Store-specific game identifier
            game_title: Display name
            store: 'epic' or 'gog'
            was_previously_installed: GUARDRAIL - If True, cancel won't delete game files
        """
        try:
            result = await self.download_queue.add_to_queue(
                game_id=game_id,
                game_title=game_title,
                store=store,
                was_previously_installed=was_previously_installed
            )
            logger.info(f"[DownloadQueue] Added {game_title} to queue (was_installed={was_previously_installed}): {result}")
            return result
        except Exception as e:
            logger.error(f"[DownloadQueue] Error adding to queue: {e}")
            return {'success': False, 'error': str(e)}

    async def add_to_download_queue_by_appid(self, app_id: int) -> Dict[str, Any]:
        """Add a game to download queue by its Steam shortcut app ID"""
        try:
            # Get game info from app_id
            game_info = await self.get_game_info(app_id)
            
            if 'error' in game_info:
                return {'success': False, 'error': game_info['error']}
            
            # GUARDRAIL: Check if game is already installed
            # If so, we'll tell the download queue to NEVER delete files on cancel
            was_previously_installed = game_info.get('is_installed', False)
            if was_previously_installed:
                logger.info(f"[DownloadQueue] GUARDRAIL: {game_info['title']} is already installed - will protect on cancel")
            
            # Check for multi-part GOG downloads
            is_multipart = False
            if game_info['store'] == 'gog':
                try:
                    game_details = await self.gog._get_game_details(game_info['game_id'])
                    if game_details:
                        linux_installers = self.gog._find_linux_installer(game_details)
                        if linux_installers:
                            is_multipart = len(linux_installers) > 1
                        else:
                            windows_installers = self.gog._find_windows_installer(game_details)
                            if windows_installers:
                                is_multipart = len(windows_installers) > 1
                        if is_multipart:
                            logger.info(f"[DownloadQueue] Detected multi-part GOG download for {game_info['title']}")
                except Exception as e:
                    logger.warning(f"[DownloadQueue] Could not check multi-part status: {e}")
            
            result = await self.add_to_download_queue(
                game_id=game_info['game_id'],
                game_title=game_info['title'],
                store=game_info['store'],
                was_previously_installed=was_previously_installed  # GUARDRAIL
            )
            
            # Add multi-part flag to response
            if result.get('success'):
                result['is_multipart'] = is_multipart
            
            return result
        except Exception as e:
            logger.error(f"[DownloadQueue] Error adding to queue by appid: {e}")
            return {'success': False, 'error': str(e)}

    async def cancel_current_download(self) -> Dict[str, Any]:
        """Cancel the currently downloading game"""
        try:
            success = await self.download_queue.cancel_current()
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error cancelling download: {e}")
            return {'success': False, 'error': str(e)}

    async def cancel_download_by_id(self, download_id: str) -> Dict[str, Any]:
        """Cancel/remove a queued download by its ID"""
        try:
            # Check if it's the current download
            current = self.download_queue.get_current()
            if current and current.get('id') == download_id:
                success = await self.download_queue.cancel_current()
            else:
                success = self.download_queue.remove_from_queue(download_id)
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error cancelling download {download_id}: {e}")
            return {'success': False, 'error': str(e)}

    async def clear_finished_download(self, download_id: str) -> Dict[str, Any]:
        """Remove a finished download from the completed list"""
        try:
            success = self.download_queue.remove_finished(download_id)
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error clearing finished download {download_id}: {e}")
            return {'success': False, 'error': str(e)}

    async def is_game_downloading(self, game_id: str, store: str) -> Dict[str, Any]:
        """Check if a specific game is currently downloading or in queue"""
        try:
            download_info = self.download_queue.is_game_downloading(game_id, store)
            return {
                'success': True,
                'is_downloading': download_info is not None,
                'download_info': download_info
            }
        except Exception as e:
            logger.error(f"[DownloadQueue] Error checking download status: {e}")
            return {'success': False, 'error': str(e)}

    async def get_storage_locations(self) -> Dict[str, Any]:
        """Get available storage locations for downloads"""
        try:
            locations = self.download_queue.get_storage_locations()
            default = self.download_queue.get_default_storage()
            return {
                'success': True,
                'locations': locations,
                'default': default
            }
        except Exception as e:
            logger.error(f"[DownloadQueue] Error getting storage locations: {e}")
            return {'success': False, 'error': str(e)}

    async def set_default_storage_location(self, location: str) -> Dict[str, Any]:
        """Set the default storage location for new downloads"""
        try:
            success = self.download_queue.set_default_storage(location)
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error setting storage location: {e}")
            return {'success': False, 'error': str(e)}

    # ============== END DOWNLOAD QUEUE API ==============

    async def check_store_status(self) -> Dict[str, Any]:
        """Check connectivity status of all stores"""
        logger.info("[STATUS] Checking store connectivity status")
        try:
            legendary_installed = self.epic.legendary_bin is not None
            logger.info(f"[STATUS] Legendary installed: {legendary_installed}, path: {self.epic.legendary_bin}")

            # Only check Epic if legendary is installed
            if legendary_installed:
                logger.info("[STATUS] Checking Epic Games availability")
                epic_available = await self.epic.is_available()
                epic_status = 'Connected' if epic_available else 'Not Connected'
                logger.info(f"[STATUS] Epic Games: {epic_status}")
            else:
                epic_status = 'Legendary not installed'
                logger.warning("[STATUS] Epic Games: Legendary CLI not installed")

            logger.info("[STATUS] Checking GOG availability")
            gog_available = await self.gog.is_available()
            gog_status = 'Connected' if gog_available else 'Not Connected'
            logger.info(f"[STATUS] GOG: {gog_status}")

            # Check Amazon availability
            nile_installed = self.amazon.nile_bin is not None
            logger.info(f"[STATUS] Nile installed: {nile_installed}, path: {self.amazon.nile_bin}")

            if nile_installed:
                logger.info("[STATUS] Checking Amazon Games availability")
                amazon_available = await self.amazon.is_available()
                amazon_status = 'Connected' if amazon_available else 'Not Connected'
                logger.info(f"[STATUS] Amazon Games: {amazon_status}")
            else:
                amazon_status = 'Nile not installed'
                logger.warning("[STATUS] Amazon Games: Nile CLI not installed")

            result = {
                'success': True,
                'epic': epic_status,
                'gog': gog_status,
                'amazon': amazon_status,
                'legendary_installed': legendary_installed,
                'nile_installed': nile_installed
            }
            logger.info(f"[STATUS] Check complete: {result}")
            return result

        except Exception as e:
            logger.error(f"[STATUS] Error in check_store_status: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'epic': 'Error',
                'gog': 'Error',
                'amazon': 'Error'
            }

    async def start_epic_auth(self) -> Dict[str, Any]:
        """Start Epic Games OAuth authentication"""
        return await self.epic.start_auth()

    async def complete_epic_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete Epic Games OAuth with authorization code"""
        return await self.epic.complete_auth(auth_code)

    async def start_gog_auth(self) -> Dict[str, Any]:
        """Start GOG OAuth authentication"""
        return await self.gog.start_auth()

    async def complete_gog_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete GOG OAuth with authorization code"""
        return await self.gog.complete_auth(auth_code)

    async def start_gog_auth_auto(self) -> Dict[str, Any]:
        """Start GOG OAuth with automatic code detection via CDP"""
        logger.info("[GOG] Starting OAuth authentication")

        try:
            # Generate OAuth URL
            import secrets
            state = secrets.token_urlsafe(32)

            auth_url = (
                f"{self.gog.AUTH_URL}/auth"
                f"?client_id={self.gog.CLIENT_ID}"
                f"&redirect_uri={self.gog.REDIRECT_URI}"
                f"&response_type=code"
                f"&state={state}"
                f"&layout=client2"
            )

            logger.info(f"[GOG] Generated OAuth URL (state={state[:10]}...)")

            # Start CDP monitoring in background to auto-capture code
            asyncio.create_task(self.gog._monitor_and_complete_auth())

            return {
                'success': True,
                'url': auth_url,
                'message': 'Authenticating via browser - code will be captured automatically'
            }

        except Exception as e:
            logger.error(f"[GOG] Error starting auth: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }

    async def logout_epic(self) -> Dict[str, Any]:
        """Logout from Epic Games"""
        return await self.epic.logout()

    async def logout_gog(self) -> Dict[str, Any]:
        """Logout from GOG"""
        return await self.gog.logout()

    async def start_amazon_auth(self) -> Dict[str, Any]:
        """Start Amazon Games OAuth authentication via nile"""
        return await self.amazon.start_auth()

    async def complete_amazon_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete Amazon Games OAuth with authorization code"""
        return await self.amazon.complete_auth(auth_code)

    async def logout_amazon(self) -> Dict[str, Any]:
        """Logout from Amazon Games"""
        return await self.amazon.logout()

    async def get_amazon_library(self) -> List[Dict[str, Any]]:
        """Get Amazon Games library"""
        games = await self.amazon.get_library()
        return [asdict(game) for game in games]

    async def open_browser(self, url: str) -> Dict[str, Any]:
        """Open URL in system browser (fallback for Steam Deck)"""
        logger.info(f"[BROWSER] Opening URL in system browser: {url[:50]}...")
        try:
            import subprocess
            subprocess.Popen(['xdg-open', url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
            logger.info("[BROWSER] Browser opened successfully")
            return {'success': True}
        except Exception as e:
            logger.error(f"[BROWSER] Failed to open browser: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def get_game_metadata(self) -> Dict[int, str]:
        """
        Get metadata for all Unifideck-managed games
        Returns mapping of appId -> store name
        """
        try:
            shortcuts = await self.shortcuts_manager.read_shortcuts()
            metadata = {}

            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                launch_options = shortcut.get('LaunchOptions', '')

                # Only process Unifideck games (have store prefix)
                if ':' not in launch_options:
                    continue

                store = launch_options.split(':')[0]

                # Get Steam's assigned appid
                steam_appid = shortcut.get('appid')

                if steam_appid:
                    metadata[steam_appid] = store
                    logger.debug(f"Mapped appid {steam_appid} to store '{store}' for {shortcut.get('AppName', 'Unknown')}")
                else:
                    # If Steam hasn't assigned an appid yet, use our generated one
                    generated_appid = self.shortcuts_manager.generate_app_id(
                        shortcut.get('AppName', ''),
                        launch_options  # Use full launch options as exe_path
                    )
                    metadata[generated_appid] = store
                    logger.debug(f"Using generated appid {generated_appid} for {shortcut.get('AppName', 'Unknown')} (store: {store})")

            logger.info(f"Loaded metadata for {len(metadata)} Unifideck games")
            return metadata

        except Exception as e:
            logger.error(f"Error loading game metadata: {e}")
            return {}

    async def get_all_unifideck_games(self) -> List[Dict[str, Any]]:
        """
        Get all Unifideck games with installation status for frontend tab filtering
        
        Uses centralized check_game_installation_status() for consistency.
        
        Returns:
            List of dicts: [{'appId': int, 'store': str, 'isInstalled': bool, 'title': str, 'steamAppId': int|None}]
        """
        try:
            # Get installed game IDs from each store
            epic_installed = set(await self.epic.get_installed())
            gog_installed = set(await self.gog.get_installed())
            amazon_installed = set(await self.amazon.get_installed())

            # Load steam_app_id cache for ProtonDB lookups
            steam_appid_cache = load_steam_appid_cache()
            
            shortcuts = await self.shortcuts_manager.read_shortcuts()
            games = []
            
            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                launch_options = shortcut.get('LaunchOptions', '')
                
                # Only process Unifideck games (have store prefix)
                if ':' not in launch_options:
                    continue
                
                parts = launch_options.split(':', 1)
                store = parts[0]
                game_id = parts[1] if len(parts) > 1 else ''

                # Check installation status
                is_installed = False
                if store == 'epic':
                    is_installed = game_id in epic_installed
                elif store == 'gog':
                    is_installed = game_id in gog_installed
                elif store == 'amazon':
                    is_installed = game_id in amazon_installed
                
                # Get appId
                app_id = shortcut.get('appid')
                if not app_id:
                    continue
                
                # Get steam_app_id from cache (for ProtonDB lookup)
                steam_app_id = steam_appid_cache.get(app_id)
                
                games.append({
                    'appId': app_id,
                    'store': store,
                    'isInstalled': is_installed,
                    'title': shortcut.get('AppName', ''),
                    'gameId': game_id,
                    'steamAppId': steam_app_id  # Real Steam appId for ProtonDB
                })
            
            logger.info(f"Loaded {len(games)} Unifideck games for tab filtering")
            return games
            
        except Exception as e:
            logger.error(f"Error getting Unifideck games: {e}")
            return []

    async def set_steamgriddb_api_key(self, api_key: str) -> Dict[str, Any]:
        """Set SteamGridDB API key and initialize client"""
        try:
            if STEAMGRIDDB_AVAILABLE:
                self.steamgriddb_api_key = api_key
                self.steamgriddb = SteamGridDBClient(api_key)
                logger.info("SteamGridDB client initialized with new API key")
                return {'success': True}
            else:
                return {'success': False, 'error': 'SteamGridDB library not available'}
        except Exception as e:
            logger.error(f"Error setting SteamGridDB API key: {e}")
            return {'success': False, 'error': str(e)}

    async def get_steamgriddb_status(self) -> Dict[str, Any]:
        """Check if SteamGridDB is configured"""
        return {
            'available': STEAMGRIDDB_AVAILABLE,
            'configured': self.steamgriddb is not None,
            'has_api_key': self.steamgriddb_api_key is not None
        }

    async def _delete_game_artwork(self, app_id: int) -> Dict[str, bool]:
        """Delete artwork files for a single game"""
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return {}

        # Convert to unsigned for filename
        unsigned_id = app_id if app_id >= 0 else app_id + 2**32

        deleted = {}
        artwork_files = [
            (f"{unsigned_id}p.jpg", 'grid'),
            (f"{unsigned_id}_hero.jpg", 'hero'),
            (f"{unsigned_id}_logo.png", 'logo'),
            (f"{unsigned_id}_icon.jpg", 'icon'),
            (f"{unsigned_id}.jpg", 'vertical')
        ]

        for filename, art_type in artwork_files:
            filepath = os.path.join(self.steamgriddb.grid_path, filename)
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    deleted[art_type] = True
                    logger.debug(f"Deleted {filename}")
            except Exception as e:
                logger.error(f"Error deleting {filename}: {e}")
                deleted[art_type] = False

        return deleted

    async def perform_full_cleanup(self, delete_files: bool = False) -> Dict[str, Any]:
        """
        Perform complete cleanup of all Unifideck data.
        
        Args:
            delete_files: If True, also delete installed game files from disk
            
        Returns:
            Dict with cleanup stats
        """
        # Prevent deletion during sync
        if self._is_syncing:
            return {
                'success': False,
                'error': 'Cannot delete while sync is in progress'
            }

        async with self._sync_lock:  # Lock to prevent sync during deletion
            try:
                logger.info(f"Starting FULL cleanup (delete_files={delete_files})...")
                
                # Stats
                stats = {
                    'deleted_games': 0,
                    'deleted_artwork': 0,
                    'preserved_shortcuts': 0,
                    'deleted_files_count': 0,
                    'auth_deleted': False,
                    'cache_deleted': False
                }

                # 1. DELETE GAME FILES (Optional)
                # Must be done BEFORE deleting games.map!
                if delete_files:
                    try:
                        import shutil
                        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
                        if os.path.exists(map_file):
                            logger.info("[Cleanup] Deleting game files...")
                            with open(map_file, 'r') as f:
                                for line in f:
                                    parts = line.strip().split('|')
                                    if len(parts) >= 3:
                                        # key|exe_path|work_dir
                                        install_dir = parts[2]
                                        
                                        # Safety check: ensure we're deleting from expected locations
                                        # Only delete if path contains "Games", "Epic", "GOG", or "unifideck"
                                        # and is NOT root or home root
                                        safe_keywords = ['/Games/', '/Epic', '/GOG', 'unifideck']
                                        is_safe = any(k in install_dir for k in safe_keywords)
                                        home_dir = os.path.expanduser("~")
                                        games_dir = os.path.join(home_dir, "Games")
                                        not_root = install_dir not in ['/', home_dir, games_dir]
                                        
                                        if is_safe and not_root and os.path.exists(install_dir):
                                            logger.info(f"[Cleanup] Deleting install dir: {install_dir}")
                                            shutil.rmtree(install_dir, ignore_errors=True)
                                            stats['deleted_files_count'] += 1
                                        else:
                                            logger.warning(f"[Cleanup] Skipping unsafe/invalid delete path: {install_dir}")
                    except Exception as e:
                        logger.error(f"[Cleanup] Error deleting game files: {e}")

                # 2. DELETE SHORTCUTS & ARTWORK (Existing Logic)
                shortcuts = await self.shortcuts_manager.read_shortcuts()
                unifideck_shortcuts = {}
                original_shortcuts = {}
                
                for idx, shortcut in shortcuts.get('shortcuts', {}).items():
                    launch_opts = shortcut.get('LaunchOptions', '')
                    if launch_opts.startswith('epic:') or launch_opts.startswith('gog:') or launch_opts.startswith('amazon:'):
                        unifideck_shortcuts[idx] = shortcut
                    else:
                        original_shortcuts[idx] = shortcut

                # Rebuild shortcuts
                new_shortcuts = {"shortcuts": {}}
                next_idx = 0
                for _, shortcut in original_shortcuts.items():
                    new_shortcuts["shortcuts"][str(next_idx)] = shortcut
                    next_idx += 1

                # Write shortcuts
                await self.shortcuts_manager.write_shortcuts(new_shortcuts)
                stats['deleted_games'] = len(unifideck_shortcuts)
                stats['preserved_shortcuts'] = len(original_shortcuts)

                # Delete artwork
                if self.steamgriddb:
                    for idx, shortcut in unifideck_shortcuts.items():
                        app_id = shortcut.get('appid')
                        if app_id:
                            deleted = await self._delete_game_artwork(app_id)
                            if any(deleted.values()):
                                stats['deleted_artwork'] += 1
                        await asyncio.sleep(0.01)

                # 3. DELETE AUTH TOKENS
                # Epic - ~/.config/legendary/user.json
                # GOG - ~/.config/unifideck/gog_token.json
                try:
                    epic_auth = os.path.expanduser("~/.config/legendary/user.json")
                    if os.path.exists(epic_auth):
                        os.remove(epic_auth)
                        logger.info("[Cleanup] Deleted Epic auth token")
                    
                    gog_auth = os.path.expanduser("~/.config/unifideck/gog_token.json")
                    if os.path.exists(gog_auth):
                        os.remove(gog_auth)
                        logger.info("[Cleanup] Deleted GOG auth token")
                        
                    # Reset in-memory states
                    self.gog = GOGAPIClient(plugin_instance=self) # Re-init to clear tokens
                    # Epic relies on legendary CLI existence, which checks file, so it's auto-cleared
                    
                    stats['auth_deleted'] = True
                except Exception as e:
                    logger.error(f"[Cleanup] Error deleting auth tokens: {e}")

                # 4. DELETE CACHES & INTERNAL DATA
                # games.map should only be deleted when delete_files=True
                # It maps installed games to their executables
                files_to_delete = [
                    "~/.local/share/unifideck/game_sizes.json",
                    "~/.local/share/unifideck/shortcuts_registry.json",
                    "~/.local/share/unifideck/download_queue.json",
                    "~/.local/share/unifideck/download_settings.json",
                    os.path.join(get_steam_appid_cache_path()) # Steam AppID Cache
                ]
                
                # Only delete games.map if we're also deleting game files (destructive mode)
                if delete_files:
                    files_to_delete.append("~/.local/share/unifideck/games.map")

                for file_path in files_to_delete:
                    try:
                        full_path = os.path.expanduser(str(file_path))
                        if os.path.exists(full_path):
                            os.remove(full_path)
                            logger.info(f"[Cleanup] Deleted: {full_path}")
                    except Exception as e:
                        logger.error(f"[Cleanup] Error deleting {file_path}: {e}")
                
                stats['cache_deleted'] = True
                
                # Clear in-memory caches
                global _legendary_installed_cache, _legendary_info_cache
                _legendary_installed_cache = {'data': None, 'timestamp': 0, 'ttl': 30}
                _legendary_info_cache = {}

                logger.info(f"Cleanup complete! Stats: {stats}")
                return {
                    'success': True,
                    **stats
                }

            except Exception as e:
                logger.error(f"Error performing full cleanup: {e}", exc_info=True)
                return {
                    'success': False,
                    'error': str(e)
                }

    async def _unload(self):
        """Cleanup on plugin unload"""
        logger.info("[UNLOAD] Stopping background sync service")
        if self.background_sync:
            await self.background_sync.stop()

        logger.info("[UNLOAD] Unifideck plugin unloaded")
