"""Backend cache utilities."""

from .game_sizes import (
    load_game_sizes_cache,
    save_game_sizes_cache,
    cache_game_size,
    get_cached_game_size,
)
from .steam_appid import load_steam_appid_cache, save_steam_appid_cache
from .steam_metadata import load_steam_metadata_cache, save_steam_metadata_cache
from .rawg_metadata import load_rawg_metadata_cache, save_rawg_metadata_cache
from .shortcuts_registry import (
    load_shortcuts_registry,
    save_shortcuts_registry,
    register_shortcut,
    get_registered_appid,
)

__all__ = [
    "load_game_sizes_cache",
    "save_game_sizes_cache",
    "cache_game_size",
    "get_cached_game_size",
    "load_steam_appid_cache",
    "save_steam_appid_cache",
    "load_steam_metadata_cache",
    "save_steam_metadata_cache",
    "load_rawg_metadata_cache",
    "save_rawg_metadata_cache",
    "load_shortcuts_registry",
    "save_shortcuts_registry",
    "register_shortcut",
    "get_registered_appid",
]
