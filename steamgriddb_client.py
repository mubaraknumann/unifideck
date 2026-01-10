"""
SteamGridDB API Client for fetching game cover art

Requires: pip install python-steamgriddb aiohttp aiofiles
"""

import os
import json
import logging
import asyncio
import aiohttp
from typing import Optional, List, Dict, Any
from pathlib import Path

# Import Steam user detection utility
from steam_user_utils import get_logged_in_steam_user

logger = logging.getLogger(__name__)

try:
    from steamgrid import SteamGridDB
    STEAMGRIDDB_AVAILABLE = True
except ImportError:
    STEAMGRIDDB_AVAILABLE = False
    logger.warning("python-steamgriddb not installed. Cover art features disabled.")


class SteamGridDBClient:
    """Client for fetching game artwork from SteamGridDB"""

    def __init__(self, api_key: Optional[str] = None, steam_path: Optional[str] = None):
        self.api_key = api_key
        self.steam_path = steam_path or self._find_steam_path()
        self.grid_path = self._find_grid_path()

        if not STEAMGRIDDB_AVAILABLE:
            logger.error("SteamGridDB client unavailable - python-steamgriddb not installed")
            self.client = None
        elif not api_key:
            logger.warning("No SteamGridDB API key provided")
            self.client = None
        else:
            try:
                self.client = SteamGridDB(api_key)
                logger.info("SteamGridDB client initialized")
            except Exception as e:
                logger.error(f"Failed to initialize SteamGridDB client: {e}")
                self.client = None

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

    def _find_grid_path(self) -> Optional[str]:
        """Find Steam grid images directory for the logged-in user.
        
        Uses loginusers.vdf to find the user with MostRecent=1, falling
        back to mtime-based detection while explicitly excluding user 0.
        """
        if not self.steam_path:
            return None

        # Use the robust user detection utility
        active_user = get_logged_in_steam_user(self.steam_path)
        
        if not active_user:
            logger.error("[SteamGridDB] Could not determine logged-in Steam user for grid path")
            return None
        
        # Safety check: never use user 0
        if active_user == '0':
            logger.error("[SteamGridDB] User 0 detected - this is a meta-directory, not a real user!")
            return None

        grid_path = os.path.join(self.steam_path, "userdata", active_user, "config", "grid")
        os.makedirs(grid_path, exist_ok=True)

        logger.info(f"[SteamGridDB] Using grid path for user {active_user}: {grid_path}")

        return grid_path

    async def search_game(self, title: str) -> Optional[int]:
        """Search for game by title and return game ID"""
        if not self.client:
            return None

        try:
            loop = asyncio.get_running_loop()
            # Run blocking synchronous call in thread pool
            results = await loop.run_in_executor(None, self.client.search_game, title)
            
            if results and len(results) > 0:
                game_id = results[0].id
                logger.debug(f"Found SteamGridDB ID {game_id} for '{title}'")
                return game_id
        except Exception as e:
            logger.error(f"Error searching for game '{title}': {e}")
            
        return None

    def select_best_artwork(self, assets: List) -> Optional[Any]:
        """
        Select the best artwork from a list of assets.
        Priority:
        1. Official/locked images (asset._lock == True)
        2. Highest score
        3. Best upvote/downvote ratio
        4. First result as fallback
        """
        if not assets:
            return None

        # Filter out NSFW/humor if desired
        filtered = [a for a in assets if not getattr(a, '_nsfw', False) and not getattr(a, '_humor', False)]
        if not filtered:
            filtered = assets  # Fall back to all if filtering removed everything

        # Sort by priority
        sorted_assets = sorted(
            filtered,
            key=lambda a: (
                not getattr(a, '_lock', False),     # Official first (False sorts before True)
                -(getattr(a, 'score', 0) or 0),     # Then highest score
                -(getattr(a, 'upvotes', 0) or 0) + (getattr(a, 'downvotes', 0) or 0)  # Then best ratio
            )
        )

        return sorted_assets[0]

    async def download_image(self, url: str, save_path: str) -> bool:
        """Download image from URL to local path"""
        try:
            # Temporarily disable SSL verification to work around certificate validation issues
            # TODO: Fix properly by updating system CA certificates or certifi package
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        content = await response.read()
                        with open(save_path, 'wb') as f:
                            f.write(content)
                        logger.info(f"Downloaded image to {save_path}")
                        return True
                    else:
                        logger.error(f"Failed to download image: HTTP {response.status}")
        except Exception as e:
            logger.error(f"Error downloading image: {e}")

        return False

    async def get_grid_images(self, sgdb_game_id: int, app_id: int) -> Dict[str, bool]:
        """
        Fetch and download grid images for a game - ALL IN PARALLEL
        Returns dict of image type -> success status
        """
        if not self.client or not self.grid_path:
            return {}

        # Convert signed int32 to unsigned for Steam filename compatibility
        unsigned_id = app_id if app_id >= 0 else app_id + 2**32

        results = {'grid': False, 'hero': False, 'logo': False, 'icon': False}

        try:
            loop = asyncio.get_running_loop()
            
            # PHASE 1: Fetch ALL artwork metadata from SGDB API in PARALLEL
            api_tasks = [
                loop.run_in_executor(None, self.client.get_grids_by_gameid, [sgdb_game_id]),
                loop.run_in_executor(None, self.client.get_heroes_by_gameid, [sgdb_game_id]),
                loop.run_in_executor(None, self.client.get_logos_by_gameid, [sgdb_game_id]),
                loop.run_in_executor(None, self.client.get_icons_by_gameid, [sgdb_game_id]),
            ]
            
            grids, heroes, logos, icons = await asyncio.gather(*api_tasks, return_exceptions=True)
            
            # PHASE 2: Select best artwork and prepare downloads
            download_tasks = []
            task_types = []
            
            # Grid
            if grids and not isinstance(grids, Exception):
                best_grid = self.select_best_artwork(grids)
                if best_grid:
                    grid_file = os.path.join(self.grid_path, f"{unsigned_id}p.jpg")
                    vertical_file = os.path.join(self.grid_path, f"{unsigned_id}.jpg")
                    download_tasks.append(self.download_image(best_grid.url, grid_file))
                    task_types.append('grid')
                    # Also queue vertical cover
                    if not os.path.exists(vertical_file):
                        download_tasks.append(self.download_image(best_grid.url, vertical_file))
                        task_types.append('vertical')
            
            # Hero
            if heroes and not isinstance(heroes, Exception):
                best_hero = self.select_best_artwork(heroes)
                if best_hero:
                    hero_file = os.path.join(self.grid_path, f"{unsigned_id}_hero.jpg")
                    download_tasks.append(self.download_image(best_hero.url, hero_file))
                    task_types.append('hero')
            
            # Logo
            if logos and not isinstance(logos, Exception):
                best_logo = self.select_best_artwork(logos)
                if best_logo:
                    logo_file = os.path.join(self.grid_path, f"{unsigned_id}_logo.png")
                    download_tasks.append(self.download_image(best_logo.url, logo_file))
                    task_types.append('logo')
            
            # Icon
            if icons and not isinstance(icons, Exception):
                best_icon = self.select_best_artwork(icons)
                if best_icon:
                    icon_file = os.path.join(self.grid_path, f"{unsigned_id}_icon.jpg")
                    download_tasks.append(self.download_image(best_icon.url, icon_file))
                    task_types.append('icon')
            
            # PHASE 3: Download ALL images in PARALLEL
            if download_tasks:
                download_results = await asyncio.gather(*download_tasks, return_exceptions=True)
                
                for i, result in enumerate(download_results):
                    if result is True and task_types[i] in results:
                        results[task_types[i]] = True

        except Exception as e:
            logger.error(f"Error fetching grid images: {e}")

        return results

    async def get_steam_metadata(self, title: str) -> Dict[str, Any]:
        """
        Fetch Steam metadata (AppID and CDN URLs)
        Returns: {'steam_id': id, 'urls': {type: url}}
        """
        result = {'steam_id': None, 'urls': {}}
        
        try:
            steam_app_id = await self.search_steam_appid(title)
            if not steam_app_id:
                return result
                
            result['steam_id'] = steam_app_id
            
            # CDN URLs
            result['urls'] = {
                'grid': f"https://shared.steamstatic.com/store_item_assets/steam/apps/{steam_app_id}/library_600x900_2x.jpg",
                'hero': f"https://shared.steamstatic.com/store_item_assets/steam/apps/{steam_app_id}/library_hero.jpg",
                'logo': f"https://shared.steamstatic.com/store_item_assets/steam/apps/{steam_app_id}/logo.png"
            }
            
        except Exception as e:
            logger.debug(f"Steam metadata error for '{title}': {e}")
            
        return result

    async def get_gog_metadata(self, gog_product_id: int) -> Dict[str, Any]:
        """Fetch GOG artwork URLs"""
        result = {'urls': {}}
        
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                api_url = f"https://api.gog.com/products/{gog_product_id}?expand=description"
                async with session.get(api_url) as response:
                    if response.status != 200:
                        return result
                    
                    data = await response.json()
                    images = data.get('images', {})
                    
                    # Icon
                    if images.get('icon'):
                        url = images['icon']
                        if url.startswith('//'): url = 'https:' + url
                        result['urls']['icon'] = url

                    # Logo
                    if images.get('logo2x') or images.get('logo'):
                        url = images.get('logo2x') or images.get('logo')
                        if url.startswith('//'): url = 'https:' + url
                        result['urls']['logo'] = url

                    # Hero (from background)
                    if images.get('background'):
                        url = images['background']
                        if url.startswith('//'): url = 'https:' + url
                        result['urls']['hero'] = url
                        
        except Exception as e:
            logger.debug(f"GOG API error: {e}")
            
        return result

    async def get_epic_metadata(self, epic_app_name: str) -> Dict[str, Any]:
        """Fetch Epic artwork URLs from Legendary cache"""
        result = {'urls': {}}
        
        try:
            legendary_path = Path.home() / ".config" / "legendary" / "metadata"
            meta_file = legendary_path / f"{epic_app_name}.json"
            
            if not meta_file.exists():
                # Try scanning for app_name
                for f in legendary_path.glob("*.json"):
                    try:
                        with open(f) as fp:
                            data = json.load(fp)
                            if data.get('app_name') == epic_app_name:
                                meta_file = f
                                break
                    except: continue
                else:
                    return result
            
            with open(meta_file) as f:
                data = json.load(f)
            
            key_images = data.get('keyImages', []) or data.get('metadata', {}).get('keyImages', [])
            if not key_images:
                return result
                
            type_mapping = {
                'grid': ['OfferImageTall', 'DieselGameBoxTall', 'DieselGameBox', 'Thumbnail'],
                'hero': ['OfferImageWide', 'DieselGameBoxWide', 'featuredMedia', 'DieselStoreFrontWide'],
                'logo': ['DieselGameBoxLogo', 'ProductLogo'],
            }
            
            for art_type, epic_types in type_mapping.items():
                for et in epic_types:
                    # Find first matching image for this priority type
                    for img in key_images:
                        if img.get('type') == et and img.get('url'):
                            result['urls'][art_type] = img['url']
                            break
                    else: continue
                    break
                    
        except Exception as e:
            logger.debug(f"Epic metadata error: {e}")
            
        return result

    async def get_amazon_metadata(self, amazon_game_id: str) -> Dict[str, Any]:
        """Fetch Amazon artwork URLs from Nile library cache"""
        result = {'urls': {}}
        
        try:
            nile_library = Path.home() / ".config" / "nile" / "library.json"
            
            if not nile_library.exists():
                return result
            
            with open(nile_library) as f:
                library = json.load(f)
            
            # Find game by ID
            for entry in library:
                product = entry.get('product', {})
                if product.get('id') == amazon_game_id:
                    detail = product.get('productDetail', {})
                    details = detail.get('details', {})
                    
                    # Grid: Prime Gaming crown image (vertical box art)
                    if details.get('pgCrownImageUrl'):
                        result['urls']['grid'] = details['pgCrownImageUrl']
                    
                    # Hero: Background image
                    if details.get('backgroundUrl1'):
                        result['urls']['hero'] = details['backgroundUrl1']
                    
                    # Logo
                    if details.get('logoUrl'):
                        result['urls']['logo'] = details['logoUrl']
                    
                    # Icon
                    if detail.get('iconUrl'):
                        result['urls']['icon'] = detail['iconUrl']
                    
                    break
                    
        except Exception as e:
            logger.debug(f"Amazon metadata error: {e}")
            
        return result

    async def search_steam_appid(self, title: str) -> Optional[int]:
        """
        Search Steam Store for AppID by game title
        Used for Epic/GOG games that also exist on Steam
        """
        try:
            import urllib.parse
            encoded = urllib.parse.quote(title)
            url = f"https://store.steampowered.com/api/storesearch/?term={encoded}&cc=US"
            
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    
                    data = await response.json()
                    items = data.get('items', [])
                    
                    if items:
                        # Return first match
                        steam_id = items[0].get('id')
                        logger.debug(f"Steam search: '{title}' -> AppID {steam_id}")
                        return steam_id
                    
        except Exception as e:
            logger.debug(f"Steam search error for '{title}': {e}")
        
        return None

    async def fetch_game_art(self, title: str, app_id: int, store: str = None, store_id: str = None) -> Dict[str, Any]:
        """
        Orchestrated Artwork Pipeline:
        1. Metadata Phase: Fetch URLs from all sources CONCURRENTLY
        2. Selection Phase: Prioritize Store URLs > Steam URLs
        3. Download Phase: Download unique, selected images CONCURRENTLY
        """
        final_result = {'success': False, 'steam_app_id': None, 'sources': []}
        
        # Unsigned ID for filenames
        unsigned_id = app_id if app_id >= 0 else app_id + 2**32
        
        try:
            # === PHASE 1: METADATA FETCH (Parallel) ===
            tasks = []
            
            # Always check Steam (for ID + backup art)
            tasks.append(self.get_steam_metadata(title))
            
            # Store-specific checks
            if store == 'gog' and store_id:
                try:
                    tasks.append(self.get_gog_metadata(int(store_id)))
                except:
                    tasks.append(asyncio.sleep(0, result={'urls': {}})) # Dummy
            elif store == 'epic' and store_id:
                tasks.append(self.get_epic_metadata(store_id))
            elif store == 'amazon' and store_id:
                tasks.append(self.get_amazon_metadata(store_id))
            else:
                tasks.append(asyncio.sleep(0, result={'urls': {}})) # Dummy to keep parallel structure simple
                
            # Wait for both
            steam_res, store_res = await asyncio.gather(*tasks)
            
            # Save Steam ID if found
            if steam_res.get('steam_id'):
                final_result['steam_app_id'] = steam_res['steam_id']
                
            # === PHASE 2: SELECTION (Top 1 Logic) ===
            # Start with Steam URLs
            selected_urls = steam_res.get('urls', {}).copy()
            source_map = {k: 'STEAM' for k in selected_urls}
            
            # Overwrite with Store URLs (Priority!)
            store_urls = store_res.get('urls', {})
            store_label = store.upper() if store else 'STORE'
            
            for k, url in store_urls.items():
                if url: # If store has this image, it WINS
                    selected_urls[k] = url
                    source_map[k] = store_label
            
            # === PHASE 3: DOWNLOAD (Parallel) ===
            download_tasks = []
            
            # Map art types to filenames
            # Note: We only download the WINNER for each type
            
            if 'grid' in selected_urls:
                path = os.path.join(self.grid_path, f"{unsigned_id}p.jpg")
                task = self.download_image(selected_urls['grid'], path)
                download_tasks.append((task, 'grid'))
                
                # Also save vertical copy (for Steam search view) - tracked, not fire-and-forget
                v_path = os.path.join(self.grid_path, f"{unsigned_id}.jpg")
                v_task = self.download_image(selected_urls['grid'], v_path)
                download_tasks.append((v_task, 'grid_vertical'))

            if 'hero' in selected_urls:
                path = os.path.join(self.grid_path, f"{unsigned_id}_hero.jpg")
                task = self.download_image(selected_urls['hero'], path)
                download_tasks.append((task, 'hero'))
                
            if 'logo' in selected_urls:
                path = os.path.join(self.grid_path, f"{unsigned_id}_logo.png")
                task = self.download_image(selected_urls['logo'], path)
                download_tasks.append((task, 'logo'))
                
            if 'icon' in selected_urls:
                # File ext might be png or jpg, force jpg for Steam icon usually? 
                # Actually Steam uses jpg mostly, but let's stick to .jpg for simplicity or respect URL
                # The old code forced _icon.jpg
                path = os.path.join(self.grid_path, f"{unsigned_id}_icon.jpg")
                task = self.download_image(selected_urls['icon'], path)
                download_tasks.append((task, 'icon'))

            # Execute downloads
            downloaded = set()
            if download_tasks:
                d_coroutines = [t[0] for t in download_tasks]
                d_results = await asyncio.gather(*d_coroutines, return_exceptions=True)
                
                for i, res in enumerate(d_results):
                    if res is True:
                        art_type = download_tasks[i][1]
                        downloaded.add(art_type)
            
            # Build Source Log
            # e.g. "STEAM:grid+logo GOG:hero+icon"
            summary_parts = []
            
            # Group by source
            by_source = {}
            for k in downloaded:
                src = source_map[k]
                if src not in by_source: by_source[src] = []
                by_source[src].append(k)
                
            for src, types in by_source.items():
                summary_parts.append(f"{src}:{'+'.join(sorted(types))}")
                
            final_result['sources'] = summary_parts
            final_result['artwork_count'] = len(downloaded)

            # === PHASE 4: FALLBACK (SGDB) ===
            # If significant art is missing (Grid or Hero), use SGDB to fill gaps
            needed = {'grid', 'hero', 'logo'}
            missing = needed - downloaded
            
            if missing and self.client:
                try:
                    gid = await self.search_game(title)
                    if gid:
                        sgdb_results = await self.get_grid_images(gid, app_id)
                        # Track SGDB downloads that succeeded
                        for art_type, success in sgdb_results.items():
                            if success and art_type not in downloaded:
                                downloaded.add(art_type)
                                source_map[art_type] = 'SGDB'
                        # Rebuild sources summary with SGDB additions
                        by_source = {}
                        for k in downloaded:
                            src = source_map.get(k, 'UNKNOWN')
                            if src not in by_source: by_source[src] = []
                            by_source[src].append(k)
                        summary_parts = []
                        for src, types in by_source.items():
                            summary_parts.append(f"{src}:{'+'.join(sorted(types))}")
                        final_result['sources'] = summary_parts
                        final_result['artwork_count'] = len(downloaded)
                except Exception as e:
                    logger.debug(f"SGDB fallback failed for {title}: {e}")
                    
                final_result['sgdb_filled'] = True
                
            if downloaded:
                final_result['success'] = True

        except Exception as e:
            logger.error(f"Error in artwork pipeline for {title}: {e}")

        return final_result


    async def batch_fetch_artwork(
        self,
        games: List[Dict[str, Any]]
    ) -> Dict[int, bool]:
        """Batch fetch (wrapper)"""
        results = {}
        for game in games:
            if game.get('app_id'):
                res = await self.fetch_game_art(
                    game['title'], 
                    game['app_id'], 
                    store=game.get('store'),
                    store_id=game.get('store_id')
                )
                results[game['app_id']] = res.get('success', False)
        return results
