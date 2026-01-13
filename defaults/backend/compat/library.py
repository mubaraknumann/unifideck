"""
Compatibility Library Module

Handles ProtonDB and Steam Deck Verified status fetching for non-Steam games.
This module provides:
- Steam Store search by title
- ProtonDB rating fetching
- Steam Deck compatibility status
- Background batch fetching service
"""
import asyncio
import aiohttp
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Dict, Optional, Any, List, Tuple

logger = logging.getLogger(__name__)

# Data directory
UNIFIDECK_DATA_DIR = Path.home() / ".local" / "share" / "unifideck"
CACHE_FILE = UNIFIDECK_DATA_DIR / "compat_cache.json"

# ProtonDB tier types
PROTONDB_TIERS = ['platinum', 'gold', 'silver', 'bronze', 'borked', 'pending', 'native']

# User-Agent to avoid being blocked by APIs
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# Steam Deck compatibility categories from Steam API
DECK_CATEGORIES = {
    1: 'unknown',
    2: 'unsupported',
    3: 'playable',
    4: 'verified'
}


def load_compat_cache() -> Dict[str, Dict]:
    """Load compatibility cache from JSON file."""
    try:
        UNIFIDECK_DATA_DIR.mkdir(parents=True, exist_ok=True)
        if CACHE_FILE.exists():
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading compat cache: {e}")
    return {}


def save_compat_cache(cache: Dict[str, Dict]) -> bool:
    """Save compatibility cache to JSON file."""
    try:
        UNIFIDECK_DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
        logger.debug(f"Saved {len(cache)} entries to compat cache")
        return True
    except Exception as e:
        logger.error(f"Error saving compat cache: {e}")
        return False


async def search_steam_store(session: aiohttp.ClientSession, title: str) -> Optional[Dict]:
    """
    Search Steam Store for a game by title.
    
    Returns:
        {"appId": int, "name": str} or None if not found
    """
    try:
        url = f"https://store.steampowered.com/api/storesearch/?term={title}&cc=US"
        headers = {'User-Agent': USER_AGENT}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get('items', [])
                if items:
                    # Try exact match first
                    normalized_title = title.lower().strip()
                    for item in items:
                        if item.get('name', '').lower().strip() == normalized_title:
                            return {"appId": item['id'], "name": item['name']}
                    # Fall back to first result
                    return {"appId": items[0]['id'], "name": items[0]['name']}
    except asyncio.TimeoutError:
        logger.debug(f"Steam Store search timeout: {title}")
    except Exception as e:
        logger.debug(f"Steam Store search error for '{title}': {e}")
    return None


async def fetch_protondb_rating(session: aiohttp.ClientSession, appid: int) -> Optional[str]:
    """
    Fetch ProtonDB rating for a Steam AppID.
    
    Returns:
        Tier string ('platinum', 'gold', etc.) or None
    """
    try:
        url = f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
        headers = {'User-Agent': USER_AGENT}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                data = await resp.json()
                tier = data.get('tier')
                if tier in PROTONDB_TIERS:
                    return tier
            elif resp.status == 404:
                # Normal - game not in ProtonDB
                return None
    except asyncio.TimeoutError:
        logger.debug(f"ProtonDB timeout for appid {appid}")
    except Exception as e:
        logger.debug(f"ProtonDB error for appid {appid}: {e}")
    return None


async def fetch_deck_verified(session: aiohttp.ClientSession, appid: int) -> str:
    """
    Fetch Steam Deck compatibility status.
    
    Returns:
        'verified', 'playable', 'unsupported', or 'unknown'
    """
    try:
        url = f"https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport?nAppID={appid}"
        headers = {'User-Agent': USER_AGENT}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                category = data.get('results', {}).get('resolved_category', 1)
                return DECK_CATEGORIES.get(category, 'unknown')
    except asyncio.TimeoutError:
        logger.debug(f"Deck Verified timeout for appid {appid}")
    except Exception as e:
        logger.debug(f"Deck Verified error for appid {appid}: {e}")
    return 'unknown'


async def get_compat_for_title(
    session: aiohttp.ClientSession,
    title: str
) -> Tuple[str, Dict[str, Any]]:
    """
    Get full compatibility info for a game title.
    
    Returns:
        (normalized_title, {tier, deckVerified, steamAppId, timestamp})
    """
    normalized = title.lower().strip()
    
    # Step 1: Search Steam Store for AppID
    search_result = await search_steam_store(session, title)
    if not search_result:
        return (normalized, {
            "tier": None,
            "deckVerified": "unknown",
            "steamAppId": None,
            "timestamp": int(time.time())
        })
    
    appid = search_result["appId"]
    
    # Step 2: Fetch ProtonDB and Deck status in parallel
    tier, deck = await asyncio.gather(
        fetch_protondb_rating(session, appid),
        fetch_deck_verified(session, appid)
    )
    
    result = {
        "tier": tier,
        "deckVerified": deck,
        "steamAppId": appid,
        "timestamp": int(time.time())
    }
    
    logger.debug(f"Compat: \"{title}\" -> AppID {appid}, tier={tier}, deck={deck}")
    return (normalized, result)


async def prefetch_compat(titles: List[str], batch_size: int = 10, delay_ms: int = 50) -> Dict[str, Dict]:
    """
    Prefetch compatibility info for a list of game titles.
    
    Args:
        titles: List of game title strings
        batch_size: Number of concurrent requests (default 10)
        delay_ms: Delay between batches in milliseconds (default 50)
    
    Returns:
        Dict mapping normalized title -> compat info
    """
    logger.info(f"Prefetching compatibility for {len(titles)} games...")
    
    # Load existing cache
    cache = load_compat_cache()
    
    # Filter out already cached titles (check by normalized key)
    titles_to_fetch = []
    for title in titles:
        normalized = title.lower().strip()
        if normalized not in cache:
            titles_to_fetch.append(title)
    
    logger.info(f"  {len(cache)} already cached, {len(titles_to_fetch)} to fetch")
    
    if not titles_to_fetch:
        return cache
    
    # Create SSL context that doesn't verify (same as main.py pattern)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=batch_size * 2)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        processed = 0
        successful = 0
        
        for i in range(0, len(titles_to_fetch), batch_size):
            batch = titles_to_fetch[i:i + batch_size]
            
            # Fetch batch in parallel
            results = await asyncio.gather(
                *[get_compat_for_title(session, title) for title in batch],
                return_exceptions=True
            )
            
            # Process results
            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"Batch error: {result}")
                    continue
                
                normalized, compat = result
                cache[normalized] = compat
                processed += 1
                
                if compat.get("tier") or compat.get("deckVerified") != "unknown":
                    successful += 1
            
            # Save progress after each batch
            save_compat_cache(cache)
            
            # Log progress every 50 games or at end
            if processed % 50 == 0 or i + batch_size >= len(titles_to_fetch):
                logger.info(f"[CompatService] Progress: {processed}/{len(titles_to_fetch)} ({successful} with ratings)")
            
            # Delay between batches
            if i + batch_size < len(titles_to_fetch):
                await asyncio.sleep(delay_ms / 1000)
    
    logger.info(f"Prefetch complete: {len(titles_to_fetch)} games, {successful} with ratings")
    return cache


class BackgroundCompatFetcher:
    """
    Background service to fetch ProtonDB/Deck Verified data asynchronously.
    
    - Runs in background (fire-and-forget from sync)
    - Searches Steam Store by title to get AppID
    - Fetches ProtonDB tier and Steam Deck status in parallel
    - Persists to compat_cache.json (survives plugin restarts)
    """
    
    def __init__(self):
        self._running = False
        self._task = None
        self._pending_titles = []  # List of game titles to fetch
    
    def queue_games(self, games: List):
        """
        Queue games for background compat fetching.
        
        Args:
            games: List of Game objects with 'title' and 'store' attributes
        """
        cache = load_compat_cache()
        
        for game in games:
            # Only queue non-Steam games (Epic, GOG, Amazon) that aren't cached
            if hasattr(game, 'store') and game.store in ('epic', 'gog', 'amazon'):
                if hasattr(game, 'title') and game.title:
                    normalized = game.title.lower().strip()
                    if normalized not in cache:
                        self._pending_titles.append(game.title)
        
        # Deduplicate
        self._pending_titles = list(set(self._pending_titles))
        logger.info(f"[CompatService] Queued {len(self._pending_titles)} games for compat fetching")
    
    def start(self):
        """Start background fetching (non-blocking)"""
        if self._running:
            logger.debug("[CompatService] Already running")
            return
        
        if not self._pending_titles:
            logger.debug("[CompatService] No pending games, not starting")
            return
        
        logger.info(f"[CompatService] Starting background fetch for {len(self._pending_titles)} games")
        self._running = True
        self._task = asyncio.create_task(self._fetch_all())
    
    def stop(self):
        """Stop background fetching"""
        if self._task and not self._task.done():
            self._task.cancel()
        self._running = False
        logger.info("[CompatService] Stopped")
    
    async def _fetch_all(self):
        """Fetch all pending compat info in parallel batches."""
        try:
            # Create SSL context
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_context, limit=10)
            
            cache = load_compat_cache()
            batch_size = 5
            delay_ms = 200
            processed = 0
            successful = 0
            
            logger.info(f"[CompatService] Fetching {len(self._pending_titles)} games")
            
            async with aiohttp.ClientSession(connector=connector) as session:
                for i in range(0, len(self._pending_titles), batch_size):
                    batch = self._pending_titles[i:i + batch_size]
                    
                    results = await asyncio.gather(
                        *[get_compat_for_title(session, title) for title in batch],
                        return_exceptions=True
                    )
                    
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error(f"[CompatService] Batch error: {result}")
                            continue
                        
                        normalized, compat = result
                        cache[normalized] = compat
                        processed += 1
                        
                        if compat.get("tier") or compat.get("deckVerified") != "unknown":
                            successful += 1
                    
                    save_compat_cache(cache)
                    
                    if processed % 50 == 0 or i + batch_size >= len(self._pending_titles):
                        logger.info(f"[CompatService] Progress: {processed}/{len(self._pending_titles)}")
                    
                    if i + batch_size < len(self._pending_titles):
                        await asyncio.sleep(delay_ms / 1000)
            
            logger.info(f"[CompatService] Complete: {len(self._pending_titles)} games, {successful} with ratings")
            
        except asyncio.CancelledError:
            logger.info("[CompatService] Cancelled")
        except Exception as e:
            logger.error(f"[CompatService] Error: {e}")
        finally:
            self._running = False
            self._pending_titles = []
