#!/usr/bin/env python3
"""
UMU ID Lookup - Query the UMU API to map game IDs to umu_id.

This is the same approach Heroic Games Launcher uses to get ProtonFixes
to recognize non-Steam games.

API: https://umu.openwinecomponents.org/umu_api.php
"""
import os
import sys
import json
from urllib import request, error

CACHE_FILE = os.path.expanduser("~/.local/share/unifideck/umu_cache.json")

# Store name mapping (same as Heroic)
STORE_MAPPING = {
    "epic": "egs",
    "egs": "egs",
    "gog": "gog",
    "amazon": "amazon",
}


def load_cache() -> dict:
    """Load cached UMU IDs from disk."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_cache(cache: dict) -> None:
    """Save UMU ID cache to disk."""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def get_umu_id(app_id: str, store: str) -> str | None:
    """
    Query UMU API for the umu_id of a game.
    
    Args:
        app_id: The game's store-specific ID (e.g., GOG ID "1213504814")
        store: The store name ("gog", "epic", "amazon")
    
    Returns:
        The umu_id (e.g., "umu-241930") or None if not found.
    """
    # Normalize store name
    store_code = STORE_MAPPING.get(store.lower(), store.lower())
    cache_key = f"{store_code}_{app_id}"
    
    # Manual mappings for known games missing from UMU API
    MANUAL_MAPPING = {
        # Shadow of Mordor (GOG -> Steam ID for ProtonFixes)
        "gog_1213504814": "umu-241930",
    }
    
    if cache_key in MANUAL_MAPPING:
        return MANUAL_MAPPING[cache_key]
    
    # Check cache first
    cache = load_cache()
    if cache_key in cache:
        cached_value = cache[cache_key]
        # None means we checked before and it wasn't found
        return cached_value if cached_value else None
    
    # Query UMU API (same endpoint as Heroic)
    url = f"https://umu.openwinecomponents.org/umu_api.php?codename={app_id.lower()}&store={store_code}"
    
    try:
        req = request.Request(url, headers={"User-Agent": "Unifideck/1.0"})
        with request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
            if data and len(data) > 0:
                umu_id = data[0].get("umu_id")
                if umu_id:
                    # Cache the result
                    cache[cache_key] = umu_id
                    save_cache(cache)
                    return umu_id
    except error.URLError:
        # Network error - don't cache, might work next time
        return None
    except Exception:
        pass
    
    # Not found - cache as None to avoid repeated lookups
    cache[cache_key] = None
    save_cache(cache)
    return None


def main():
    """CLI interface: umu_lookup.py <app_id> <store>"""
    if len(sys.argv) < 3:
        print("Usage: umu_lookup.py <app_id> <store>", file=sys.stderr)
        print("Example: umu_lookup.py 1213504814 gog", file=sys.stderr)
        sys.exit(1)
    
    app_id = sys.argv[1]
    store = sys.argv[2]
    
    umu_id = get_umu_id(app_id, store)
    
    if umu_id:
        print(umu_id)
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
