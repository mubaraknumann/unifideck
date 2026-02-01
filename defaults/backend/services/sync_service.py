"""
SyncService - Handles library synchronization orchestration.

Responsibilities:
- Orchestrate library sync from multiple stores (Epic, GOG, Amazon)
- Coordinate artwork fetching and caching
- Manage Steam shortcuts creation and updates
- Track sync progress and handle cancellation
- Coordinate metadata fetching (ProtonDB, RAWG, Steam appinfo)
"""

import os
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Set, Optional

from backend.stores.base import Game
from backend.cache import (
    load_steam_appid_cache,
    save_steam_appid_cache,
    load_steam_metadata_cache,
    save_steam_metadata_cache,
    load_rawg_metadata_cache,
    save_rawg_metadata_cache,
)
from backend.utils.metadata import extract_metadata_from_appinfo
from backend.utils.rawg_metadata import fetch_rawg_metadata
from backend.utils.steam_appinfo import read_steam_appinfo_vdf

logger = logging.getLogger(__name__)


class SyncService:
    """Service for orchestrating library synchronization."""
    
    def __init__(
        self,
        epic_connector,
        gog_connector,
        amazon_connector,
        shortcuts_manager,
        metadata_service,
        artwork_service,
        size_fetcher,
        sync_progress,
        plugin_dir: str
    ):
        """Initialize SyncService with all required dependencies.
        
        Args:
            epic_connector: EpicConnector instance
            gog_connector: GOGAPIClient instance
            amazon_connector: AmazonConnector instance
            shortcuts_manager: ShortcutsManager instance
            metadata_service: MetadataService instance
            artwork_service: ArtworkService instance
            size_fetcher: BackgroundSizeFetcher instance
            sync_progress: SyncProgress tracker instance
            plugin_dir: Plugin directory path
        """
        self.epic = epic_connector
        self.gog = gog_connector
        self.amazon = amazon_connector
        self.shortcuts_manager = shortcuts_manager
        self.metadata_service = metadata_service
        self.artwork_service = artwork_service
        self.size_fetcher = size_fetcher
        self.sync_progress = sync_progress
        self.plugin_dir = plugin_dir
        
        # Sync state
        self._sync_lock = asyncio.Lock()
        self._is_syncing = False
        self._cancel_sync = False
    
    def cancel_sync(self):
        """Request cancellation of current sync operation."""
        if self._is_syncing:
            self._cancel_sync = True
            logger.info("Sync cancellation requested")
    
    async def sync_libraries(self, fetch_artwork: bool = True) -> Dict[str, Any]:
        """Sync all game libraries to shortcuts.vdf and optionally fetch artwork.
        
        Args:
            fetch_artwork: Whether to fetch artwork during sync
            
        Returns:
            Dict with success status, counts, and any errors
        """
        # Check if sync already running (non-blocking check)
        if self._is_syncing:
            logger.warning("Sync already in progress, ignoring request")
            return {
                'success': False,
                'error': 'errors.syncInProgress',
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

                # === PHASE 1: FETCH GAME LISTS ===
                self.sync_progress.status = "fetching"
                self.sync_progress.current_game = {
                    "label": "sync.fetchingGameLists",
                    "values": {}
                }
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

                # Check for cancellation
                if self._cancel_sync:
                    return self._handle_cancellation()
                
                self.sync_progress.total_games = len(all_games)
                self.sync_progress.synced_games = 0

                # === PHASE 2: QUEUE COMPATIBILITY FETCHING ===
                logger.info("Sync: Queueing games for compatibility lookup...")
                self.metadata_service.queue_compat_fetch(all_games)

                # === PHASE 3: CHECK INSTALLED STATUS ===
                self.sync_progress.status = "checking_installed"
                self.sync_progress.current_game = {
                    "label": "sync.checkingInstalledGames",
                    "values": {}
                }

                # Get installed games
                epic_installed = await self.epic.get_installed()
                gog_installed = await self.gog.get_installed()
                amazon_installed = await self.amazon.get_installed()

                # Mark installed status and update games.map
                await self._update_installed_status(
                    epic_games, epic_installed,
                    gog_games, gog_installed,
                    amazon_games, amazon_installed
                )

                # Get launcher script path
                launcher_script = os.path.join(self.plugin_dir, 'bin', 'unifideck-launcher')

                # === PHASE 4: QUEUE SIZE FETCHING (background) ===
                self.size_fetcher.queue_games(all_games)
                self.size_fetcher.start()

                # === PHASE 5: PRE-CALCULATE APP IDS ===
                for game in all_games:
                    game.app_id = self.shortcuts_manager.generate_app_id(game.title, launcher_script)

                # === PHASE 6: ARTWORK HANDLING ===
                artwork_count = 0
                if fetch_artwork and self.artwork_service:
                    artwork_count = await self._handle_artwork_sync(all_games)

                # === PHASE 7: UPDATE GAME ICONS ===
                self._update_game_icons(all_games)

                # === PHASE 8: WRITE SHORTCUTS ===
                self.sync_progress.status = "syncing"
                self.sync_progress.current_game = {
                    "label": "sync.savingShortcuts",
                    "values": {}
                }
                
                batch_result = await self.shortcuts_manager.add_games_batch(
                    all_games, launcher_script, valid_stores=valid_stores
                )
                added_count = batch_result.get('added', 0)
                
                if batch_result.get('error'):
                    raise Exception(batch_result['error'])

                # === COMPLETE ===
                self.sync_progress.status = "complete"
                self.sync_progress.synced_games = len(all_games)
                self.sync_progress.current_game = {
                    "label": "sync.completed",
                    "values": {"added": added_count, "artwork": artwork_count}
                }

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

    async def _update_installed_status(
        self,
        epic_games: List[Game],
        epic_installed: Dict,
        gog_games: List[Game],
        gog_installed: Dict,
        amazon_games: List[Game],
        amazon_installed: Dict
    ):
        """Update installed status for all games and sync games.map."""
        # Handle Epic games
        for game in epic_games:
            if game.id in epic_installed:
                game.is_installed = True
                
                # Use cached metadata to get EXE path
                metadata = epic_installed[game.id]
                install_path = metadata.get('install', {}).get('install_path')
                executable = metadata.get('manifest', {}).get('launch_exe')
                
                exe_path = None
                if install_path and executable:
                    exe_path = os.path.join(install_path, executable)
                elif metadata.get('install_path') and metadata.get('executable'):
                    exe_path = os.path.join(metadata['install_path'], metadata['executable'])
                     
                if exe_path:
                    work_dir = os.path.dirname(exe_path)
                    await self.shortcuts_manager._update_game_map('epic', game.id, exe_path, work_dir)
                    logger.debug(f"Updated games.map for Epic game {game.id}")

        # Handle GOG games
        for game in gog_games:
            if game.id in gog_installed:
                game.is_installed = True
                game_info = self.gog.get_installed_game_info(game.id)
                if game_info and game_info.get('executable'):
                    exe_path = game_info['executable']
                    work_dir = os.path.dirname(exe_path)
                    await self.shortcuts_manager._update_game_map('gog', game.id, exe_path, work_dir)
                    logger.debug(f"Updated games.map for GOG game {game.id}")

        # Handle Amazon games
        for game in amazon_games:
            if game.id in amazon_installed:
                game.is_installed = True
                game_info = self.amazon.get_installed_game_info(game.id)
                if game_info and game_info.get('executable'):
                    exe_path = game_info['executable']
                    work_dir = os.path.dirname(exe_path)
                    await self.shortcuts_manager._update_game_map('amazon', game.id, exe_path, work_dir)
                    logger.debug(f"Updated games.map for Amazon game {game.id}")

    async def _handle_artwork_sync(self, all_games: List[Game]) -> int:
        """Handle artwork synchronization for all games.
        
        Returns:
            Number of games with successfully fetched artwork
        """
        games_needing_art = []
        steam_appid_cache = load_steam_appid_cache()
        
        # === STEP 1: STEAMGRIDDB LOOKUPS ===
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
        
        # Parallel SteamGridDB lookups
        if games_needing_sgdb_lookup:
            logger.info(f"Looking up {len(games_needing_sgdb_lookup)} games on SteamGridDB...")
            self.sync_progress.status = "sgdb_lookup"
            self.sync_progress.current_game = {
                "label": "sync.sgdbLookup",
                "values": {"count": len(games_needing_sgdb_lookup)}
            }
            
            steam_appid_cache = await self._parallel_sgdb_lookup(games_needing_sgdb_lookup, steam_appid_cache)
        
        # Save updated cache
        if steam_appid_cache:
            save_steam_appid_cache(steam_appid_cache)

        # === STEP 2: EXTRACT STEAM METADATA ===
        if all_games:
            await self._extract_steam_metadata(all_games, steam_appid_cache)

        # === STEP 3: RAWG METADATA PRE-FETCH ===
        if all_games:
            await self._prefetch_rawg_metadata(all_games)

        # === STEP 4: CLEANUP ORPHANED ARTWORK ===
        self.sync_progress.current_game = {
            "label": "sync.cleaningOrphanedArtwork",
            "values": {}
        }
        cleanup_result = await self.artwork_service.cleanup_orphaned(self.shortcuts_manager)
        if cleanup_result.get('removed_count', 0) > 0:
            logger.info(f"Cleaned up {cleanup_result['removed_count']} orphaned artwork files")

        # === STEP 5: IDENTIFY GAMES NEEDING ARTWORK ===
        self.sync_progress.status = "checking_artwork"
        self.sync_progress.current_game = {
            "label": "sync.checkingExistingArtwork",
            "values": {}
        }
        
        seen_app_ids_art = set()
        for game in all_games:
            if game.app_id not in seen_app_ids_art:
                seen_app_ids_art.add(game.app_id)
                if not await self.artwork_service.has_artwork(game.app_id):
                    games_needing_art.append(game)

        # === STEP 6: FETCH ARTWORK (2-PASS) ===
        if games_needing_art:
            return await self._fetch_artwork_two_pass(games_needing_art)
        
        return 0

    async def _parallel_sgdb_lookup(
        self,
        games: List[Game],
        cache: Dict[int, int]
    ) -> Dict[int, int]:
        """Perform parallel SteamGridDB lookups for games.
        
        Returns:
            Updated steam_appid_cache
        """
        async def lookup_sgdb(game):
            try:
                sgdb_id = await self.artwork_service.search_game(game.title)
                if sgdb_id:
                    game.steam_app_id = sgdb_id
                    return (game.app_id, sgdb_id)
            except Exception as e:
                logger.debug(f"SGDB lookup failed for {game.title}: {e}")
            return None
        
        semaphore = asyncio.Semaphore(30)
        async def limited_lookup(game):
            async with semaphore:
                return await lookup_sgdb(game)
        
        results = await asyncio.gather(*[limited_lookup(g) for g in games])
        
        # Update cache with results
        for result in results:
            if result:
                app_id, sgdb_id = result
                cache[app_id] = sgdb_id
        
        logger.info(f"SteamGridDB lookup complete: {sum(1 for r in results if r)} found")
        return cache

    async def _extract_steam_metadata(self, all_games: List[Game], steam_appid_cache: Dict):
        """Extract metadata from Steam's appinfo.vdf."""
        self.sync_progress.current_game = {
            "label": "sync.extractingSteamMetadata",
            "values": {"count": len(all_games)}
        }

        appinfo_data = read_steam_appinfo_vdf()
        if appinfo_data:
            existing_metadata = load_steam_metadata_cache()
            appid_mapping, new_metadata = await extract_metadata_from_appinfo(all_games, appinfo_data)

            # Update metadata cache
            if new_metadata:
                existing_metadata.update(new_metadata)
                save_steam_metadata_cache(existing_metadata)

            # Save the shortcut->steam appid mapping
            if appid_mapping:
                steam_appid_cache.update(appid_mapping)
                save_steam_appid_cache(steam_appid_cache)
                logger.info(f"Extracted metadata for {len(new_metadata)} games from appinfo.vdf")

    async def _prefetch_rawg_metadata(self, all_games: List[Game]):
        """Pre-fetch RAWG metadata for games not in cache."""
        rawg_cache = load_rawg_metadata_cache()
        games_needing_rawg = [g for g in all_games if g.title.lower() not in rawg_cache]

        if games_needing_rawg:
            logger.info(f"Sync: Pre-fetching RAWG metadata for {len(games_needing_rawg)} games")
            self.sync_progress.current_game = {
                "label": "sync.fetchingEnhancedMetadata",
                "values": {"count": len(games_needing_rawg)}
            }

            async def prefetch_rawg_for_game(game, semaphore):
                async with semaphore:
                    try:
                        rawg_data = await fetch_rawg_metadata(game.title)
                        if rawg_data:
                            return (game.title.lower(), rawg_data)
                    except Exception as e:
                        logger.debug(f"[RAWG Prefetch] Error for {game.title}: {e}")
                    return None

            semaphore = asyncio.Semaphore(5)
            tasks = [prefetch_rawg_for_game(g, semaphore) for g in games_needing_rawg]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, tuple) and result is not None:
                    rawg_cache[result[0]] = result[1]

            save_rawg_metadata_cache(rawg_cache)
            logger.info(f"Sync: Cached RAWG metadata for {sum(1 for r in results if isinstance(r, tuple) and r)} games")

    async def _fetch_artwork_two_pass(self, games_needing_art: List[Game]) -> int:
        """Fetch artwork with retry pass for incomplete downloads.
        
        Returns:
            Count of games with complete artwork
        """
        logger.info(f"Fetching artwork for {len(games_needing_art)} games...")
        self.sync_progress.current_phase = "artwork"
        self.sync_progress.status = "artwork"
        self.sync_progress.artwork_total = len(games_needing_art)
        self.sync_progress.artwork_synced = 0
        self.sync_progress.synced_games = 0
        self.sync_progress.total_games = 0

        # Check cancellation
        if self._cancel_sync:
            logger.warning("Sync cancelled before artwork")
            return 0

        # === PASS 1: Initial artwork fetch ===
        logger.info(f"  [Pass 1] Starting parallel download (concurrency: 30)")
        semaphore = asyncio.Semaphore(30)
        tasks = [self._fetch_artwork_with_progress(game, semaphore) for game in games_needing_art]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        pass1_success = sum(1 for r in results if isinstance(r, dict) and r.get('success'))
        logger.info(f"  [Pass 1] Complete: {pass1_success}/{len(games_needing_art)} games")

        # === PASS 2: Retry incomplete artwork ===
        games_to_retry = []
        for game in games_needing_art:
            missing = await self.artwork_service.get_missing_types(game.app_id)
            if missing:
                games_to_retry.append(game)

        if games_to_retry and not self._cancel_sync:
            logger.info(f"  [Pass 2] Retrying {len(games_to_retry)} games with incomplete artwork")
            self.sync_progress.status = "artwork_retry"
            self.sync_progress.current_game = {
                "label": "sync.retryingMissingArtwork",
                "values": {"count": len(games_to_retry)}
            }
            self.sync_progress.artwork_total = len(games_to_retry)
            self.sync_progress.artwork_synced = 0

            retry_tasks = [self._fetch_artwork_with_progress(game, semaphore) for game in games_to_retry]
            await asyncio.gather(*retry_tasks, return_exceptions=True)

            # Count recovered games
            pass2_recovered = 0
            for g in games_to_retry:
                if not await self.artwork_service.get_missing_types(g.app_id):
                    pass2_recovered += 1
            logger.info(f"  [Pass 2] Recovered: {pass2_recovered}/{len(games_to_retry)} games")

        # Log still-incomplete games
        still_incomplete = []
        for g in games_needing_art:
            missing = await self.artwork_service.get_missing_types(g.app_id)
            if missing:
                still_incomplete.append((g.title, missing))
        
        if still_incomplete:
            logger.warning(f"  Artwork incomplete for {len(still_incomplete)} games after 2 passes")
            for title, missing in still_incomplete[:5]:
                logger.debug(f"    - {title}: missing {missing}")

        artwork_count = len(games_needing_art) - len(still_incomplete)
        logger.info(f"Artwork download complete: {artwork_count}/{len(games_needing_art)} games")
        return artwork_count

    async def _fetch_artwork_with_progress(self, game: Game, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
        """Fetch artwork for a game with progress tracking."""
        async with semaphore:
            result = await self.artwork_service.fetch_for_game(game)
            self.sync_progress.artwork_synced += 1
            return result

    def _update_game_icons(self, all_games: List[Game]):
        """Update game objects with local icon paths if available."""
        grid_path = self.artwork_service.get_grid_path()
        if not grid_path:
            return
        
        grid_path = Path(grid_path)
        for game in all_games:
            # Convert signed int32 to unsigned for filename check
            unsigned_id = game.app_id if game.app_id >= 0 else game.app_id + 2**32
            icon_path = grid_path / f"{unsigned_id}_icon.jpg"
            
            if icon_path.exists():
                game.cover_image = str(icon_path)

    def _handle_cancellation(self) -> Dict[str, Any]:
        """Handle sync cancellation."""
        logger.warning("Sync cancelled by user")
        self.sync_progress.status = "cancelled"
        self.sync_progress.current_game = {
            "label": "sync.cancelledByUser",
            "values": {}
        }
        return {
            'success': False,
            'error': 'errors.syncCancelled',
            'cancelled': True,
            'epic_count': 0,
            'gog_count': 0,
            'amazon_count': 0,
            'added_count': 0,
            'updated_count': 0,
            'artwork_count': 0
        }

    async def force_sync_libraries(self, resync_artwork: bool = False) -> Dict[str, Any]:
        """Force sync all libraries - rewrites ALL shortcuts.
        
        Args:
            resync_artwork: Whether to re-download artwork (overwrites manual changes)
            
        Returns:
            Dict with success status and counts
        """
        # Check if sync already running
        if self._is_syncing:
            logger.warning("Sync already in progress, ignoring force sync request")
            return {
                'success': False,
                'error': 'errors.syncInProgress',
                'epic_count': 0,
                'gog_count': 0,
                'amazon_count': 0,
                'added_count': 0
            }

        logger.info("Force syncing libraries (rewrite all shortcuts)")
        
        # Clear artwork cache if requested
        if resync_artwork:
            logger.info("Resync artwork enabled - will re-download all artwork")
            await self.artwork_service.clear_cache()

        # Run normal sync
        return await self.sync_libraries(fetch_artwork=True)
