"""
ArtworkService - Handles game artwork fetching and caching.

Responsibilities:
- Fetch artwork from SteamGridDB (grid, hero, logo, icon)
- Check artwork existence and identify missing types
- Fetch artwork with progress tracking and timeout handling
- Delete game artwork files
- Coordinate parallel artwork downloads with semaphore control
"""

import asyncio
import logging
from typing import Dict, Any, Optional, Set
from pathlib import Path

logger = logging.getLogger(__name__)

# Artwork sync timeout (seconds per game)
ARTWORK_FETCH_TIMEOUT = 90


class ArtworkService:
    """Service for fetching and managing game artwork."""
    
    def __init__(self, steamgriddb_client, sync_progress):
        """Initialize ArtworkService with SteamGridDB client and sync progress tracker.
        
        Args:
            steamgriddb_client: SteamGridDBClient instance for artwork downloads
            sync_progress: SyncProgress instance for tracking progress
        """
        self.steamgriddb = steamgriddb_client
        self.sync_progress = sync_progress
    
    async def has_artwork(self, app_id: int) -> bool:
        """Check if artwork files exist for this app_id.
        
        Args:
            app_id: Steam shortcut app ID
            
        Returns:
            True if artwork exists, False otherwise
        """
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return False
        
        grid_path = Path(self.steamgriddb.grid_path)
        
        # Check if any artwork file exists for this app_id
        artwork_types = [
            f"{app_id}p.png",  # Grid
            f"{app_id}_hero.png",  # Hero
            f"{app_id}_logo.png",  # Logo
            f"{app_id}_icon.png",  # Icon
        ]
        
        return any((grid_path / art).exists() for art in artwork_types)
    
    async def get_missing_artwork_types(self, app_id: int) -> Set[str]:
        """Check which specific artwork types are missing for this app_id.
        
        Args:
            app_id: Steam shortcut app ID
            
        Returns:
            Set of missing artwork types (e.g., {'grid', 'hero', 'logo', 'icon'})
        """
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return {'grid', 'hero', 'logo', 'icon'}
        
        grid_path = Path(self.steamgriddb.grid_path)
        missing = set()
        
        # Check each artwork type
        if not (grid_path / f"{app_id}p.png").exists():
            missing.add('grid')
        if not (grid_path / f"{app_id}_hero.png").exists():
            missing.add('hero')
        if not (grid_path / f"{app_id}_logo.png").exists():
            missing.add('logo')
        if not (grid_path / f"{app_id}_icon.png").exists():
            missing.add('icon')
        
        return missing
    
    async def fetch_artwork_with_progress(self, game, semaphore) -> Dict[str, Any]:
        """Fetch artwork for a single game with concurrency control and timeout.
        
        Args:
            game: Game object with title, app_id, store, and id attributes
            semaphore: Asyncio semaphore for concurrency control
            
        Returns:
            dict: {success: bool, timed_out: bool, game: Game, error: str, artwork_count: int}
        """
        async with semaphore:
            try:
                # Update status to show we're working on this game (before download)
                self.sync_progress.current_game = {
                    "label": "sync.downloadingArtwork",
                    "values": {"game": game.title}
                }
                
                # Wrap with timeout to prevent sync from hanging
                try:
                    result = await asyncio.wait_for(
                        self.steamgriddb.fetch_game_art(
                            game.title,
                            game.app_id,
                            store=game.store,      # 'epic', 'gog', or 'amazon'
                            store_id=game.id       # Store-specific game ID
                        ),
                        timeout=ARTWORK_FETCH_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Artwork fetch timed out for {game.title} after {ARTWORK_FETCH_TIMEOUT}s")
                    await self.sync_progress.increment_artwork(game.title)
                    return {'success': False, 'timed_out': True, 'game': game}
                
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
                
                return {'success': result.get('success', False), 'game': game, 'artwork_count': art_count}
            except Exception as e:
                logger.error(f"Error fetching artwork for {game.title}: {e}")
                await self.sync_progress.increment_artwork(game.title)
                return {'success': False, 'error': str(e), 'game': game}
    
    async def delete_game_artwork(self, app_id: int) -> Dict[str, bool]:
        """Delete artwork files for a game.
        
        Args:
            app_id: Steam shortcut app ID
            
        Returns:
            Dict mapping artwork types to deletion success status
        """
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return {'grid': False, 'hero': False, 'logo': False, 'icon': False}
        
        grid_path = Path(self.steamgriddb.grid_path)
        results = {}
        
        artwork_files = {
            'grid': grid_path / f"{app_id}p.png",
            'hero': grid_path / f"{app_id}_hero.png",
            'logo': grid_path / f"{app_id}_logo.png",
            'icon': grid_path / f"{app_id}_icon.png",
        }
        
        for art_type, file_path in artwork_files.items():
            if file_path.exists():
                try:
                    file_path.unlink()
                    results[art_type] = True
                    logger.info(f"Deleted {art_type} artwork: {file_path}")
                except Exception as e:
                    logger.error(f"Failed to delete {art_type} artwork: {e}")
                    results[art_type] = False
            else:
                results[art_type] = False
        
        return results
    
    def get_artwork_paths(self, app_id: int) -> Dict[str, Optional[Path]]:
        """Get paths to artwork files for a game.
        
        Args:
            app_id: Steam shortcut app ID
            
        Returns:
            Dict mapping artwork types to file paths (None if not found)
        """
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return {'grid': None, 'hero': None, 'logo': None, 'icon': None}
        
        grid_path = Path(self.steamgriddb.grid_path)
        
        paths = {
            'grid': grid_path / f"{app_id}p.png",
            'hero': grid_path / f"{app_id}_hero.png",
            'logo': grid_path / f"{app_id}_logo.png",
            'icon': grid_path / f"{app_id}_icon.png",
        }
        
        # Return None for non-existent files
        return {
            art_type: path if path.exists() else None
            for art_type, path in paths.items()
        }
