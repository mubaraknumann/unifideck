"""
Base Store class defining the interface for all game store connectors.

All store implementations (Epic, GOG, Amazon) should inherit from this
and implement the required methods.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
import logging


logger = logging.getLogger(__name__)


@dataclass
class Game:
    """Represents a game from any store"""
    id: str
    title: str
    store: str  # 'steam', 'epic', 'gog', 'amazon'
    is_installed: bool = False
    cover_image: Optional[str] = None
    install_path: Optional[str] = None
    executable: Optional[str] = None
    app_id: Optional[int] = None  # For shortcuts.vdf (our generated ID)
    steam_app_id: Optional[int] = None  # Real Steam appId for ProtonDB lookups

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Store(ABC):
    """
    Abstract base class for game store connectors.
    
    Each store (Epic, GOG, Amazon) implements this interface to provide
    a consistent API for authentication, library management, and game operations.
    """
    
    @property
    @abstractmethod
    def store_name(self) -> str:
        """Return the store identifier (e.g., 'epic', 'gog', 'amazon')"""
        pass
    
    @abstractmethod
    async def is_available(self) -> bool:
        """
        Check if the store CLI/API is available and authenticated.
        
        Returns:
            True if authenticated and ready to use, False otherwise.
        """
        pass
    
    @abstractmethod
    async def start_auth(self) -> Dict[str, Any]:
        """
        Start the OAuth/authentication flow.
        
        Returns:
            Dict with 'success', 'url' (auth URL to open), and optionally 'error'.
        """
        pass
    
    @abstractmethod
    async def complete_auth(self, auth_code: str) -> Dict[str, Any]:
        """
        Complete authentication with the OAuth code.
        
        Args:
            auth_code: The authorization code from OAuth callback.
            
        Returns:
            Dict with 'success' and optionally 'error'.
        """
        pass
    
    @abstractmethod
    async def logout(self) -> Dict[str, Any]:
        """
        Logout from the store, clearing stored credentials.
        
        Returns:
            Dict with 'success' and optionally 'error'.
        """
        pass
    
    @abstractmethod
    async def get_library(self) -> List[Game]:
        """
        Get the user's game library from this store.
        
        Returns:
            List of Game objects representing owned games.
        """
        pass
    
    async def get_installed(self) -> Dict[str, Any]:
        """
        Get installed games info. Default implementation returns empty dict.
        
        Returns:
            Dict mapping game_id to installation metadata.
        """
        return {}
    
    async def get_game_size(self, game_id: str) -> Optional[int]:
        """
        Get download size for a game in bytes.
        
        Args:
            game_id: The store-specific game identifier.
            
        Returns:
            Size in bytes, or None if unavailable.
        """
        return None
    
    async def install_game(self, game_id: str, progress_callback=None) -> Dict[str, Any]:
        """
        Install a game.
        
        Args:
            game_id: The store-specific game identifier.
            progress_callback: Optional async function for progress updates.
            
        Returns:
            Dict with 'success', 'install_path', and optionally 'error'.
        """
        return {'success': False, 'error': 'Not implemented'}
    
    async def uninstall_game(self, game_id: str) -> Dict[str, Any]:
        """
        Uninstall a game.
        
        Args:
            game_id: The store-specific game identifier.
            
        Returns:
            Dict with 'success' and optionally 'error'.
        """
        return {'success': False, 'error': 'Not implemented'}
