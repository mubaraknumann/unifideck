# Backend Package for Unifideck
# This package contains modular components for stores, auth, download, and compatibility.

from .registry import GamesRegistry, GameEntry, get_registry
from .utils import get_all_game_directories, get_games_map_path
