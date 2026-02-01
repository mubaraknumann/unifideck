"""
MetadataService - Handles game metadata fetching and enrichment.

Responsibilities:
- Fetch Steam Deck compatibility data
- Fetch RAWG metadata (descriptions, ratings, screenshots)
- Fetch ProtonDB compatibility ratings
- Coordinate parallel metadata fetching during library sync
- Cache and manage metadata lookups
"""

import asyncio
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class MetadataService:
    """Service for fetching and managing game metadata."""
    
    def __init__(self, compat_fetcher, sync_progress):
        """Initialize MetadataService with compatibility fetcher and sync progress tracker.
        
        Args:
            compat_fetcher: BackgroundCompatFetcher instance for ProtonDB/Deck data
            sync_progress: SyncProgress instance for tracking progress
        """
        self.compat_fetcher = compat_fetcher
        self.sync_progress = sync_progress
    
    async def fetch_deck_compatibility(self, steam_app_id: int) -> Optional[Dict[str, Any]]:
        """Fetch Steam Deck compatibility data for a game.
        
        Args:
            steam_app_id: Steam store app ID
            
        Returns:
            Dict with deck compatibility data or None
        """
        from backend.utils.deck_compat import fetch_steam_deck_compatibility
        
        try:
            return await fetch_steam_deck_compatibility(steam_app_id)
        except Exception as e:
            logger.error(f"Error fetching Deck compatibility for {steam_app_id}: {e}")
            return None
    
    async def fetch_rawg_metadata(self, game_title: str, platforms: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Fetch RAWG metadata for a game.
        
        Args:
            game_title: Game title to search for
            platforms: Optional list of platforms to filter by
            
        Returns:
            Dict with RAWG metadata or None
        """
        from backend.utils.rawg_metadata import fetch_rawg_metadata
        
        try:
            return await fetch_rawg_metadata(game_title, platforms=platforms)
        except Exception as e:
            logger.error(f"Error fetching RAWG metadata for {game_title}: {e}")
            return None
    
    async def fetch_enhanced_metadata_for_game(self, game, semaphore) -> Dict[str, Any]:
        """Fetch enhanced metadata (RAWG) for a single game with concurrency control.
        
        This is used during force_sync_libraries to enrich game data with descriptions,
        ratings, and screenshots from RAWG.
        
        Args:
            game: Game object with title, steam_app_id, and other attributes
            semaphore: Asyncio semaphore for concurrency control
            
        Returns:
            dict: {success: bool, game: Game, metadata: dict, error: str}
        """
        async with semaphore:
            try:
                # Update progress
                self.sync_progress.current_game = {
                    "label": "sync.fetchingMetadata",
                    "values": {"game": game.title}
                }
                
                # Fetch RAWG metadata
                metadata = await self.fetch_rawg_metadata(game.title)
                
                if metadata:
                    # Store metadata on game object for later use
                    game.rawg_metadata = metadata
                    
                    count = await self.sync_progress.increment_metadata(game.title)
                    logger.info(f"  [{count}/{self.sync_progress.metadata_total}] Fetched RAWG metadata for {game.title}")
                    
                    return {'success': True, 'game': game, 'metadata': metadata}
                else:
                    await self.sync_progress.increment_metadata(game.title)
                    return {'success': False, 'game': game, 'error': 'No metadata found'}
            
            except Exception as e:
                logger.error(f"Error fetching enhanced metadata for {game.title}: {e}")
                await self.sync_progress.increment_metadata(game.title)
                return {'success': False, 'error': str(e), 'game': game}
    
    def queue_compat_fetch(self, games: List[Any]) -> None:
        """Queue games for background compatibility fetching.
        
        Args:
            games: List of Game objects to fetch compatibility data for
        """
        logger.info(f"Queueing {len(games)} games for compatibility lookup...")
        self.compat_fetcher.queue_games(games)
        self.compat_fetcher.start()  # Non-blocking background fetch
    
    def get_compat_cache(self) -> Dict[str, Dict]:
        """Get the current compatibility cache.
        
        Returns:
            Dict mapping steam_app_id to compatibility data
        """
        from backend.compat import load_compat_cache
        return load_compat_cache()
    
    async def prefetch_compat(self, games: List[Any]) -> Dict[str, Any]:
        """Prefetch compatibility data for a list of games.
        
        Args:
            games: List of Game objects
            
        Returns:
            Dict with prefetch results
        """
        from backend.compat import prefetch_compat
        
        try:
            return await prefetch_compat(games)
        except Exception as e:
            logger.error(f"Error prefetching compatibility data: {e}")
            return {'success': False, 'error': str(e)}
