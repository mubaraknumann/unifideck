from .shortcuts_manager import (
    ShortcutsManager, load_shortcuts_registry, save_shortcuts_registry,
    register_shortcut, get_registered_appid,
    _invalidate_games_map_mem_cache, _load_games_map_cached, GAMES_MAP_PATH,
    SHORTCUTS_REGISTRY_FILE, get_shortcuts_registry_path
)
from .launch_options import extract_store_id, is_unifideck_shortcut, get_full_id, get_store_prefix
from .vdf import load_shortcuts_vdf, save_shortcuts_vdf
