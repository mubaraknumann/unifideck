"""Background size fetching service.

Extracted from main.py to reduce size; behavior should remain identical.
"""

import asyncio
import logging
import time

from typing import List

from backend.cache.game_sizes import load_game_sizes_cache, save_game_sizes_cache

logger = logging.getLogger(__name__)

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
        
    def queue_games(self, games: List, force_refresh: bool = False):
        """Queue games for background size fetching.
        
        Args:
            games: List of Game objects with 'store' and 'id' attributes
            force_refresh: If True, re-fetch sizes even if already cached
        """
        logger.info(f"[SizeService] queue_games() called with {len(games)} games, force_refresh={force_refresh}")
        
        # If force_refresh, stop any running task first so we can restart
        if force_refresh and self._running:
            logger.info("[SizeService] Stopping previous task for force_refresh")
            self.stop()
        
        cache = load_game_sizes_cache()
        
        # Clear pending list to avoid duplicates from previous runs
        self._pending_games = []
        pending_set = set()  # For deduplication within this batch
        
        for game in games:
            cache_key = f"{game.store}:{game.id}"
            # force_refresh bypasses cache check to re-fetch all sizes
            if force_refresh or cache_key not in cache:
                if cache_key not in pending_set:
                    pending_set.add(cache_key)
                    self._pending_games.append((game.store, game.id))
                    # Mark as pending in cache (null value)
                    cache[cache_key] = None
        
        save_game_sizes_cache(cache)
        logger.info(f"[SizeService] Queued {len(self._pending_games)} games for size fetching")
    
    def start(self):
        """Start background fetching (non-blocking)"""
        logger.info(f"[SizeService] start() called, _running={self._running}, pending={len(self._pending_games)}")
        
        # Reset _running if previous task is done (handles abnormal task completion)
        if self._running and self._task and self._task.done():
            logger.info("[SizeService] Previous task finished, resetting _running flag")
            self._running = False
        
        if self._running:
            logger.info("[SizeService] Already running, skipping start")
            return
        
        # Load pending from cache if not already queued
        if not self._pending_games:
            cache = load_game_sizes_cache()
            self._pending_games = [
                tuple(k.split(':', 1)) for k, v in cache.items() 
                if v is None and ':' in k
            ]
            logger.info(f"[SizeService] Loaded {len(self._pending_games)} pending games from cache")
        
        if not self._pending_games:
            logger.info("[SizeService] No pending games, not starting")
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
                                # Log at debug level - GOG legacy games often have no size API
                                logger.debug(f"[SizeService] No size for {store}:{game_id}")
                                return (store, game_id, None)
                        except Exception as e:
                            logger.warning(f"[SizeService] Error fetching {store}:{game_id}: {e}")
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
