#!/usr/bin/env python3
"""
Game-specific fix database.
Combines umu-protonfixes with manual overrides.
"""
import json
import logging
from typing import Dict, List, Optional
from urllib import request
from urllib.error import URLError, HTTPError

logger = logging.getLogger("GameFixes")

# Manual overrides (curated list for known problematic games)
# Priority: These override umu-protonfixes
MANUAL_FIXES = {
    "Dodo": {  # Borderlands 2
        "winetricks": [],  # Test: likely works without redist
        "notes": "Works with Proton + EOS only"
    },
    "ea8df71f923649a193ab1c1fded7e1b3": {  # Ghostrunner
        "winetricks": ["vcrun2013", "vcrun2015"],
        "notes": "Requires VC++ 2013/2015 for game engine"
    },
    "fa5aa7e6c28c4c94aeac239eee700d5f": {  # Football Manager 2024
        "winetricks": [],
        "notes": "EOS overlay only, no redistributables needed"
    }
}

def fetch_umu_protonfixes(game_id: str) -> Optional[Dict]:
    """
    Fetch game fixes from umu-protonfixes GitHub repository.
    
    URL format: https://github.com/Open-Wine-Components/umu-database
    The database maps Epic Store IDs to required fixes.
    
    Args:
        game_id: Epic Store game ID (e.g., "Dodo", "fa5aa7...")
    
    Returns:
        Dictionary with fix data or None if not found
    """
    # umu-database uses format: umu-{store}-{id}.json
    urls = [
        f"https://raw.githubusercontent.com/Open-Wine-Components/umu-database/main/umu-egs-{game_id}.json",
        f"https://raw.githubusercontent.com/Open-Wine-Components/umu-database/main/umu-epic-{game_id}.json"
    ]
    
    for url in urls:
        try:
            logger.info(f"Querying umu-protonfixes: {url}")
            with request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                logger.info(f"Found umu-protonfixes entry for {game_id}")
                return data
        except (URLError, HTTPError) as e:
            continue
        except Exception as e:
            logger.warning(f"Error fetching umu-protonfixes for {game_id}: {e}")
    
    logger.info(f"No umu-protonfixes entry found for {game_id}")
    return None

def get_required_winetricks(game_id: str) -> List[str]:
    """
    Get winetricks packages required for a game.
    
    Priority order:
    1. Manual overrides (MANUAL_FIXES)
    2. umu-protonfixes database
    3. Global defaults (mfc140 for common compatibility)
    
    Args:
        game_id: Epic Store game ID
    
    Returns:
        List of winetricks package names (e.g., ["vcrun2015", "d3dcompiler_47"])
    """
    # Check manual overrides first
    if game_id in MANUAL_FIXES:
        packages = MANUAL_FIXES[game_id].get("winetricks", [])
        logger.info(f"Using manual override for {game_id}: {packages}")
        return packages
    
    # Try umu-protonfixes
    umu_data = fetch_umu_protonfixes(game_id)
    if umu_data and "winetricks" in umu_data:
        packages = umu_data["winetricks"]
        logger.info(f"Using umu-protonfixes for {game_id}: {packages}")
        return packages
    
    # Global defaults: Common dependencies that fix many games
    # mfc140: Microsoft Foundation Classes runtime - fixes games like Shadow of Mordor
    global_defaults = ["mfc140"]
    logger.info(f"Using global defaults for {game_id}: {global_defaults}")
    return global_defaults

def get_game_fixes(game_id: str) -> Dict:
    """
    Get complete fix data for a game.
    
    Returns dictionary with:
    - winetricks: List of packages
    - notes: Optional notes about the game
    - source: Where the fix came from (manual/umu/default)
    """
    if game_id in MANUAL_FIXES:
        fix = MANUAL_FIXES[game_id].copy()
        fix["source"] = "manual"
        return fix
    
    umu_data = fetch_umu_protonfixes(game_id)
    if umu_data:
        return {
            "winetricks": umu_data.get("winetricks", []),
            "notes": umu_data.get("notes", ""),
            "source": "umu-protonfixes"
        }
    
    return {
        "winetricks": ["mfc140"],  # Global defaults for common compatibility
        "notes": "Using global defaults (mfc140)",
        "source": "global_default"
    }

if __name__ == "__main__":
    # Test cases
    import sys
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    
    if len(sys.argv) > 1:
        game_id = sys.argv[1]
        fixes = get_game_fixes(game_id)
        print(f"\nFixes for {game_id}:")
        print(json.dumps(fixes, indent=2))
    else:
        # Test known games
        for gid in ["Dodo", "ea8df71f923649a193ab1c1fded7e1b3", "fa5aa7e6c28c4c94aeac239eee700d5f"]:
            print(f"\n{'='*60}")
            fixes = get_game_fixes(gid)
            print(f"{gid}: {fixes}")
