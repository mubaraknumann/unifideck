#!/usr/bin/env python3
"""
Cloud Save Sync Script for Unifideck Launcher

Called by unifideck-launcher before/after game launch:
  - Before launch: downloads cloud saves
  - After exit: uploads cloud saves

Usage: cloud_save_sync.py <store> <game_id> <direction> <prefix_path> [save_path]
  store: "epic" or "gog"
  game_id: Game identifier
  direction: "download" or "upload"
  prefix_path: Wine prefix path (required for Epic)
  save_path: Local save path (optional, used for GOG)

Exit codes:
  0: Success (or no saves to sync)
  1: Error during sync
"""

import sys
import os
import subprocess
import json
import time

# Plugin directory (parent of bin/)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(SCRIPT_DIR)

# Binary paths
LEGENDARY_BIN = os.path.join(PLUGIN_DIR, "bin", "legendary")
GOGDL_BIN = os.path.join(PLUGIN_DIR, "bin", "gogdl")

# Config paths
GOGDL_AUTH_FILE = os.path.expanduser("~/.config/unifideck/gogdl_auth.json")
UNIFIDECK_GOG_TOKEN = os.path.expanduser("~/.config/unifideck/gog_token.json")

# GOG Galaxy client ID for token format
GOG_CLIENT_ID = "46899977096215655"


def log(msg: str) -> None:
    """Log to stderr (captured by launcher)"""
    print(f"[CloudSave] {msg}", file=sys.stderr)


def find_legendary() -> str:
    """Find legendary binary"""
    paths = [
        LEGENDARY_BIN,
        os.path.expanduser("~/.local/bin/legendary"),
        "/usr/bin/legendary",
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    return ""


def find_gogdl() -> str:
    """Find gogdl binary"""
    paths = [
        GOGDL_BIN,
        "/var/lib/flatpak/app/com.heroicgameslauncher.hgl/x86_64/stable/active/files/bin/heroic/resources/app.asar.unpacked/build/bin/x64/linux/gogdl",
        os.path.expanduser("~/.local/bin/gogdl"),
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    return ""


def convert_gog_token() -> bool:
    """Convert Unifideck's GOG token to gogdl format"""
    try:
        if not os.path.exists(UNIFIDECK_GOG_TOKEN):
            log("GOG token not found")
            return False
        
        with open(UNIFIDECK_GOG_TOKEN, 'r') as f:
            token = json.load(f)
        
        gogdl_auth = {
            GOG_CLIENT_ID: {
                "access_token": token.get("access_token"),
                "expires_in": 3600,
                "token_type": "bearer",
                "scope": "",
                "refresh_token": token.get("refresh_token"),
                "user_id": "",
                "session_id": "",
                "loginTime": time.time()
            }
        }
        
        os.makedirs(os.path.dirname(GOGDL_AUTH_FILE), exist_ok=True)
        with open(GOGDL_AUTH_FILE, 'w') as f:
            json.dump(gogdl_auth, f)
        
        return True
    except Exception as e:
        log(f"Failed to convert GOG token: {e}")
        return False


def get_legendary_save_path(game_id: str) -> str:
    """Read the save_path from Legendary's installed.json"""
    installed_json = os.path.expanduser("~/.config/legendary/installed.json")
    try:
        if os.path.exists(installed_json):
            with open(installed_json, 'r') as f:
                data = json.load(f)
            if game_id in data:
                return data[game_id].get("save_path", "")
    except Exception as e:
        log(f"Error reading installed.json: {e}")
    return ""

def get_wine_prefix(prefix_path: str) -> str:
    """
    Detect the correct WINEPREFIX from a prefix path.
    
    Handles both:
    - Wine: /path/prefix/drive_c (WINEPREFIX = /path/prefix)
    - Proton: /path/prefix/pfx/drive_c (WINEPREFIX = /path/prefix/pfx)
    
    Returns the path to set as WINEPREFIX, or empty string if not found.
    """
    if not prefix_path or not os.path.exists(prefix_path):
        return ""
    
    # Check for Proton-style: prefix_path/pfx/drive_c
    proton_pfx = os.path.join(prefix_path, "pfx")
    if os.path.exists(os.path.join(proton_pfx, "drive_c")):
        return proton_pfx
    
    # Check for Wine-style: prefix_path/drive_c
    if os.path.exists(os.path.join(prefix_path, "drive_c")):
        return prefix_path
    
    # Prefix exists but no drive_c yet - return prefix_path and let Wine/Proton create it
    return prefix_path


def ensure_epic_save_path_configured(legendary: str, game_id: str, prefix_path: str) -> str:
    """
    Auto-configure the save path by running sync-saves with --accept-path.
    This resolves the Windows CloudSaveFolder template to a Linux path
    within the Wine prefix, just like Heroic does.
    
    Args:
        legendary: Path to legendary binary
        game_id: Epic game app name
        prefix_path: Base prefix path (may contain /pfx subdirectory)
    
    Returns the computed save_path if successful, empty string otherwise.
    """
    # Check if already configured
    existing_path = get_legendary_save_path(game_id)
    if existing_path:
        log(f"Save path already configured: {existing_path}")
        return existing_path
    
    if not prefix_path:
        log("No Wine prefix provided, cannot compute save path")
        return ""
    
    # Detect correct WINEPREFIX (Wine vs Proton)
    wineprefix = get_wine_prefix(prefix_path)
    if not wineprefix:
        log(f"Could not detect Wine prefix structure in: {prefix_path}")
        return ""
    
    log(f"Computing save path for {game_id} (first-time setup)...")
    log(f"WINEPREFIX: {wineprefix}")
    
    cmd = [legendary, "sync-saves", game_id, "--accept-path", 
           "--skip-upload", "--skip-download"]
    
    # Set up Wine environment for path resolution
    env = os.environ.copy()
    env["WINEPREFIX"] = wineprefix
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        if result.stderr:
            log(f"Path computation stderr: {result.stderr[:200]}")
        
        # Read the computed path from installed.json
        computed_path = get_legendary_save_path(game_id)
        if computed_path:
            log(f"Computed save path: {computed_path}")
        else:
            log("Could not compute save path (game may not support cloud saves)")
        return computed_path
    except Exception as e:
        log(f"Error computing save path: {e}")
        return ""

def is_save_folder_empty(save_path: str) -> bool:
    """Check if local save folder is empty or missing."""
    if not save_path:
        return True
    if not os.path.exists(save_path):
        return True
    try:
        contents = os.listdir(save_path)
        return len(contents) == 0
    except Exception:
        return True


def sync_epic(game_id: str, direction: str, prefix_path: str) -> bool:
    """Sync Epic cloud saves using legendary"""
    legendary = find_legendary()
    if not legendary:
        log("Legendary not found, skipping Epic sync")
        return True  # Non-fatal
    
    # Step 1: Ensure save path is configured (auto-compute if needed)
    save_path = ensure_epic_save_path_configured(legendary, game_id, prefix_path)
    
    # Step 2: Safety check for uploads - don't upload empty saves
    if direction == "upload" and is_save_folder_empty(save_path):
        log(f"Local save folder empty or missing, skipping upload to protect cloud saves")
        return True  # Non-fatal, but safe
    
    # Step 3: Perform the actual sync
    # Use --disable-filters to ensure ALL save files are synced, not just filtered ones
    cmd = [legendary, "sync-saves", game_id, "-y", "--disable-filters"]
    if direction == "download":
        cmd.append("--skip-upload")
    elif direction == "upload":
        cmd.append("--skip-download")
    
    log(f"Running: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.stdout:
            log(f"stdout: {result.stdout[:300]}")
        if result.stderr:
            log(f"stderr: {result.stderr[:300]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log("Sync timed out (120s)")
        return False
    except Exception as e:
        log(f"Sync error: {e}")
        return False


def sync_gog(game_id: str, direction: str, save_path: str) -> bool:
    """Sync GOG cloud saves using gogdl"""
    gogdl = find_gogdl()
    if not gogdl:
        log("gogdl not found, skipping GOG sync")
        return True  # Non-fatal
    
    if not save_path:
        log("No save path provided for GOG sync")
        return True  # Non-fatal
    
    if not convert_gog_token():
        log("Failed to convert GOG token")
        return True  # Non-fatal
    
    cmd = [
        gogdl,
        "--auth-config-path", GOGDL_AUTH_FILE,
        "save-sync",
        save_path,
        game_id,
        "--os", "windows",
        "--ts", "0",
    ]
    
    if direction == "download":
        cmd.append("--skip-upload")
    elif direction == "upload":
        cmd.append("--skip-download")
    
    log(f"Running: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.stdout:
            log(f"stdout: {result.stdout[:300]}")
        if result.stderr:
            log(f"stderr: {result.stderr[:300]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log("Sync timed out (120s)")
        return False
    except Exception as e:
        log(f"Sync error: {e}")
        return False


def main():
    if len(sys.argv) < 5:
        print(f"Usage: {sys.argv[0]} <store> <game_id> <direction> <prefix_path> [save_path]")
        sys.exit(1)
    
    store = sys.argv[1]
    game_id = sys.argv[2]
    direction = sys.argv[3]
    prefix_path = sys.argv[4] if len(sys.argv) > 4 else ""
    save_path = sys.argv[5] if len(sys.argv) > 5 else ""
    
    log(f"Starting sync: store={store}, game_id={game_id}, direction={direction}")
    
    success = False
    if store == "epic":
        success = sync_epic(game_id, direction, prefix_path)
    elif store == "gog":
        success = sync_gog(game_id, direction, save_path)
    else:
        log(f"Unknown store: {store}")
        success = True  # Non-fatal for unknown stores
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
