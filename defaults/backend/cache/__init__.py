"""Backend cache utilities."""

from .game_sizes import (
    load_game_sizes_cache,
    save_game_sizes_cache,
    cache_game_size,
    get_cached_game_size,
)
from .compat_cache import load_compat_cache, save_compat_cache
from .steam_appid import load_steam_appid_cache, save_steam_appid_cache
from .steam_metadata import load_steam_metadata_cache, save_steam_metadata_cache
from .rawg_metadata import load_rawg_metadata_cache, save_rawg_metadata_cache

__all__ = [
    "load_game_sizes_cache",
    "save_game_sizes_cache",
    "cache_game_size",
    "get_cached_game_size",
    "load_compat_cache",
    "save_compat_cache",
    "load_steam_appid_cache",
    "save_steam_appid_cache",
    "load_steam_metadata_cache",
    "save_steam_metadata_cache",
    "load_rawg_metadata_cache",
    "save_rawg_metadata_cache",
]
