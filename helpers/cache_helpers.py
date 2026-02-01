# cache_helpers.py
# This module provides utility functions related to game size calculation, steam_appid, and more.
import os
import json

CACHE_DIR = os.path.expanduser("~/.local/share/unifideck/cache")

def get_steam_appid(game_name):
    """Retrieve the Steam AppID for a given game."""
    # Mock implementation
    appid_map = {"Game A": 123, "Game B": 456}
    return appid_map.get(game_name, None)

def calculate_game_size(game_dir):
    """Calculate the total size of a game's directory."""
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(game_dir):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    return total_size

def ensure_cache_dir():
    """Ensure that the cache directory exists."""
    os.makedirs(CACHE_DIR, exist_ok=True)

def write_cache_entry(game_name, data):
    """Write data to the cache for a given game."""
    ensure_cache_dir()
    cache_path = os.path.join(CACHE_DIR, f"{game_name}.json")
    with open(cache_path, "w") as cache_file:
        json.dump(data, cache_file)

def read_cache_entry(game_name):
    """Read data from the cache for a given game."""
    cache_path = os.path.join(CACHE_DIR, f"{game_name}.json")
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, "r") as cache_file:
        return json.load(cache_file)
