"""Artwork utilities for checking and managing Steam grid artwork files."""

import os
import logging
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


def convert_to_unsigned_appid(app_id: int) -> int:
    """Convert signed int32 app ID to unsigned for artwork filenames.
    
    Steam artwork files use unsigned app IDs even though shortcuts.vdf stores signed.
    Example: -1257913040 (signed) -> 3037054256 (unsigned)
    
    Args:
        app_id: Signed or unsigned app ID
        
    Returns:
        Unsigned app ID for use in artwork filenames
    """
    return app_id if app_id >= 0 else app_id + 2**32


def get_artwork_paths(grid_path: Path, app_id: int) -> Dict[str, Path]:
    """Get paths for all artwork types for a given app ID.
    
    Args:
        grid_path: Path to Steam grid directory
        app_id: App ID (will be converted to unsigned)
        
    Returns:
        Dict mapping artwork type to file path
    """
    unsigned_id = convert_to_unsigned_appid(app_id)
    
    return {
        'grid': grid_path / f"{unsigned_id}p.jpg",      # Vertical grid (460x215)
        'hero': grid_path / f"{unsigned_id}_hero.jpg",  # Hero image (1920x620)
        'logo': grid_path / f"{unsigned_id}_logo.png",  # Logo
        'icon': grid_path / f"{unsigned_id}_icon.jpg",  # Icon
        'vertical': grid_path / f"{unsigned_id}.jpg"    # Alternative vertical format
    }


def check_artwork_exists(grid_path: Optional[Path], app_id: int) -> bool:
    """Check if any artwork files exist for this app_id.
    
    Args:
        grid_path: Path to Steam grid directory (None if not available)
        app_id: App ID to check
        
    Returns:
        True if any artwork files exist
    """
    if not grid_path:
        return False
    
    artwork_paths = get_artwork_paths(grid_path, app_id)
    # Check grid, hero, logo, icon (not vertical as it's less common)
    primary_types = ['grid', 'hero', 'logo', 'icon']
    return any(artwork_paths[art_type].exists() for art_type in primary_types)


def get_missing_artwork_types(grid_path: Optional[Path], app_id: int) -> Set[str]:
    """Check which specific artwork types are missing for this app_id.
    
    Args:
        grid_path: Path to Steam grid directory (None if not available)
        app_id: App ID to check
        
    Returns:
        Set of missing artwork types (e.g., {'grid', 'hero', 'logo', 'icon'})
    """
    if not grid_path:
        return {'grid', 'hero', 'logo', 'icon'}
    
    artwork_paths = get_artwork_paths(grid_path, app_id)
    primary_types = ['grid', 'hero', 'logo', 'icon']
    
    return {art_type for art_type in primary_types if not artwork_paths[art_type].exists()}


def delete_game_artwork(grid_path: Optional[Path], app_id: int) -> Dict[str, bool]:
    """Delete artwork files for a single game.
    
    Args:
        grid_path: Path to Steam grid directory
        app_id: App ID to delete artwork for
        
    Returns:
        Dict mapping artwork type to deletion success status
    """
    if not grid_path:
        return {}
    
    artwork_paths = get_artwork_paths(grid_path, app_id)
    deleted = {}
    
    for art_type, filepath in artwork_paths.items():
        try:
            if filepath.exists():
                filepath.unlink()
                deleted[art_type] = True
                logger.debug(f"Deleted {filepath.name}")
            # Don't report types that didn't exist
        except Exception as e:
            logger.error(f"Error deleting {filepath.name}: {e}")
            deleted[art_type] = False
    
    return deleted
