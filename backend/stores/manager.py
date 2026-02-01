"""
Store Manager - Manages multiple game store connectors.

Provides a unified interface for interacting with Epic, GOG, and Amazon stores.
"""
from typing import List, Dict, Any, Optional
import logging

from .base import Store, Game


logger = logging.getLogger(__name__)


class StoreManager:
    """
    Manages multiple game store connectors.
    
    Provides unified access to Epic, GOG, and Amazon stores with
    common operations like getting combined libraries, checking auth status, etc.
    """
    
    def __init__(self):
        self._stores: Dict[str, Store] = {}
    
    def register_store(self, store: Store):
        """Register a store connector."""
        self._stores[store.store_name] = store
        logger.info(f"Registered store: {store.store_name}")
    
    def get_store(self, store_name: str) -> Optional[Store]:
        """Get a specific store connector by name."""
        return self._stores.get(store_name)
    
    @property
    def stores(self) -> Dict[str, Store]:
        """Get all registered stores."""
        return self._stores
    
    async def get_auth_status(self) -> Dict[str, bool]:
        """
        Get authentication status for all stores.
        
        Returns:
            Dict mapping store_name to authenticated status.
        """
        status = {}
        for name, store in self._stores.items():
            try:
                status[name] = await store.is_available()
            except Exception as e:
                logger.error(f"Error checking auth for {name}: {e}")
                status[name] = False
        return status
    
    async def get_combined_library(self, stores: List[str] = None) -> List[Game]:
        """
        Get combined library from specified stores (or all if not specified).
        
        Args:
            stores: List of store names to query, or None for all.
            
        Returns:
            Combined list of Game objects from all specified stores.
        """
        games = []
        target_stores = stores or list(self._stores.keys())
        
        for store_name in target_stores:
            store = self._stores.get(store_name)
            if store:
                try:
                    if await store.is_available():
                        store_games = await store.get_library()
                        games.extend(store_games)
                        logger.info(f"Fetched {len(store_games)} games from {store_name}")
                except Exception as e:
                    logger.error(f"Error fetching library from {store_name}: {e}")
        
        return games
