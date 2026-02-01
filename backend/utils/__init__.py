# Utils package
from .paths import (
    get_prefix_path,
    ensure_unifideck_dir,
    is_safe_delete_path,
    UNIFIDECK_DATA_DIR,
    GAMES_MAP_PATH,
    GAMES_REGISTRY_PATH,
    GAME_SIZES_CACHE_PATH,
    SHORTCUTS_REGISTRY_PATH,
    DOWNLOAD_QUEUE_PATH,
    DOWNLOAD_SETTINGS_PATH,
    SETTINGS_PATH,
    LEGENDARY_CONFIG_DIR,
)

__all__ = [
    'get_prefix_path',
    'ensure_unifideck_dir',
    'is_safe_delete_path',
    'UNIFIDECK_DATA_DIR',
    'GAMES_MAP_PATH',
    'GAMES_REGISTRY_PATH',
    'GAME_SIZES_CACHE_PATH',
    'SHORTCUTS_REGISTRY_PATH',
    'DOWNLOAD_QUEUE_PATH',
    'DOWNLOAD_SETTINGS_PATH',
    'SETTINGS_PATH',
    'LEGENDARY_CONFIG_DIR',
]
