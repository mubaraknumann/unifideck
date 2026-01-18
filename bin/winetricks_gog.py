#!/usr/bin/env python3
"""
GOG Winetricks Installer - Installs Windows redistributables using umu-run.
Run on first launch of each GOG Windows game.

Uses umu-run to invoke winetricks with the same Proton environment that games use.
Queries umu-database for game-specific fixes (same as Epic winetricks_installer.py).
"""
import os
import sys
import subprocess
import logging
import shutil
import json
from urllib import request
from urllib.error import URLError, HTTPError
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================

# Use shared winetricks log (same as Epic installer)
LOG_FILE = os.path.expanduser("~/.local/share/unifideck/winetricks.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# Default dependencies - matches what Heroic installs for GOG games
# Based on analysis of Heroic's Shadow of Mordor prefix (system32 DLLs)
DEFAULT_DEPS = [
    # Visual C++ Redistributables (matches Heroic's prefix)
    # Note: vcrun2019 includes 2015/2017, vcrun2022 is separate
    'vcrun2022',      # Latest VC++ runtime (msvcp140 series)
    'vcrun2019',      # VC++ 2015-2019 runtime (most games need this)
    'vcrun2013',      # VC++ 2013 runtime
    'vcrun2012',      # VC++ 2012 runtime (vcomp120)
    'vcrun2010',      # VC++ 2010 runtime (msvcr100, vcomp100)
    'vcrun2008',      # VC++ 2008 runtime (older games)
    
    # DirectX components (critical - found in Heroic prefix)
    'd3dcompiler_47', # DirectX shader compiler (modern games)
    'd3dcompiler_43', # Older shader compiler
    'd3dx9',          # DirectX 9 extensions (d3dx9_24..43.dll)
    'd3dx10',         # DirectX 10 extensions
    'd3dx11_43',      # DirectX 11 extensions
    
    # Audio (XACT - found in Heroic prefix)
    'xact',           # Xbox Audio (xactengine, xaudio2, xapofx, x3daudio)
    'xact_x64',       # 64-bit XACT
    
    # Input (found in Heroic prefix)
    'xinput',         # XInput for controller support (xinput1_*.dll)
    
    # .NET (some GOG games require this)
    'dotnet40',       # .NET Framework 4.0
    
    # Fonts (for UI/text rendering)
    'corefonts',      # Microsoft core fonts
    'tahoma',         # Tahoma font
    
    # Other common components
    'mfc140',         # MFC runtime
    'gdiplus',        # GDI+ graphics library
    'physx',          # PhysX (physics in many games)
]

# Patterns to detect non-Windows games (skip winetricks)
DOSBOX_PATTERNS = ['dosbox.exe', 'dosbox.conf']
SCUMMVM_PATTERNS = ['scummvm.exe']

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# LOGGING (with timestamps)
# ============================================================================

class WinetricksFilter(logging.Filter):
    """Filter out wine debug spam"""
    IGNORED_PATTERNS = [
        "fixme:wineusb:", "fixme:xinput:", "err:hid:", "fixme:winebth:",
        "using server-side synchronization", "stray \\", "pid ", 
        "skipping destruction", "i386-linux-gnu-capsule", "steamrt",
    ]
    
    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in self.IGNORED_PATTERNS)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("WinetricksGOG")
for handler in logger.handlers:
    handler.addFilter(WinetricksFilter())


# ============================================================================
# UMU-DATABASE QUERY (auto-detect dependencies)
# ============================================================================

def fetch_umu_protonfixes(game_id: str) -> dict:
    """
    Fetch game fixes from umu-protonfixes GitHub repository.
    Queries multiple stores (gog, egs, epic) for compatibility.
    Also checks GAMEID env var which may contain umu_id from API lookup.
    """
    urls = []
    
    # Check for GAMEID override (set by unifideck-launcher after UMU API lookup)
    # This is the preferred method - uses the same ID that umu-run will use
    gameid = os.environ.get('GAMEID', '')
    if gameid and gameid.startswith('umu-'):
        # Extract the Steam AppID from umu-XXXXX format
        steam_id = gameid.replace('umu-', '')
        logger.info(f"Using GAMEID from launcher: {gameid}")
        urls.append(f"https://raw.githubusercontent.com/Open-Wine-Components/umu-database/main/umu-{steam_id}.json")
    
    # Check for legacy STEAM_APPID override
    steam_appid = os.environ.get('STEAM_APPID')
    if steam_appid:
        logger.info(f"Using STEAM_APPID override: {steam_appid}")
        urls.append(f"https://raw.githubusercontent.com/Open-Wine-Components/umu-database/main/umu-steam-{steam_appid}.json")

    # Fall back to store-specific lookups
    urls.extend([
        f"https://raw.githubusercontent.com/Open-Wine-Components/umu-database/main/umu-gog-{game_id}.json",
        f"https://raw.githubusercontent.com/Open-Wine-Components/umu-database/main/umu-egs-{game_id}.json",
        f"https://raw.githubusercontent.com/Open-Wine-Components/umu-database/main/umu-epic-{game_id}.json",
    ])
    
    for url in urls:
        try:
            logger.info(f"Querying umu-database: {url}")
            with request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                logger.info(f"Found umu-database entry for {game_id}")
                return data
        except (URLError, HTTPError):
            continue
        except Exception as e:
            logger.debug(f"Error fetching: {e}")
    
    logger.info(f"No umu-database entry found for {game_id}")
    return None


def get_required_deps(game_id: str) -> list:
    """
    Get winetricks packages required for a game.
    
    Order:
    1. Query umu-database for game-specific fixes
    2. Fall back to defaults (vcrun2019, vcrun2022)
    """
    # Try umu-database first
    umu_data = fetch_umu_protonfixes(game_id)
    if umu_data:
        # Extract winetricks from umu entry
        winetricks = umu_data.get('winetricks', [])
        if winetricks:
            logger.info(f"Using umu-database deps for {game_id}: {winetricks}")
            return winetricks
    
    # Fall back to defaults
    logger.info(f"Using default deps for {game_id}: {DEFAULT_DEPS}")
    return list(DEFAULT_DEPS)


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def find_umu_run():
    """Find umu-run from Heroic, bundled, or system location"""
    candidates = [
        # Heroic's umu-run (preferred)
        os.path.expanduser("~/.var/app/com.heroicgameslauncher.hgl/config/heroic/tools/runtimes/umu/umu_run.py"),
        # Bundled umu-run
        os.path.join(SCRIPT_DIR, "umu", "umu", "umu-run"),
        # System umu-run
        shutil.which("umu-run"),
    ]
    
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def find_proton():
    """Find Proton installation"""
    proton_paths = [
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton - Experimental"),
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton 10.0"),
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton 9.0 (Beta)"),
        os.path.expanduser("~/.local/share/Steam/steamapps/common/Proton - Experimental"),
    ]
    
    for path in proton_paths:
        if os.path.exists(path):
            return path
    return None


def detect_game_type(game_path: str) -> str:
    """Detect game type: 'dosbox', 'scummvm', or 'windows'"""
    if not game_path or not os.path.exists(game_path):
        return 'windows'
    
    for root, dirs, files in os.walk(game_path):
        files_lower = [f.lower() for f in files]
        
        for pattern in DOSBOX_PATTERNS:
            if pattern.lower() in files_lower:
                return 'dosbox'
        
        for pattern in SCUMMVM_PATTERNS:
            if pattern.lower() in files_lower:
                return 'scummvm'
        
        if root.count(os.sep) - game_path.count(os.sep) > 2:
            break
    
    return 'windows'


def find_game_path(game_id: str) -> str:
    """Try to find the game installation path"""
    search_paths = [
        os.path.expanduser('~/GOG Games'),
        os.path.expanduser('~/Games'),
        os.path.expanduser(f'~/Games/GOG_{game_id}'),
    ]
    
    for base in search_paths:
        if not os.path.exists(base):
            continue
        try:
            for entry in os.listdir(base):
                marker = os.path.join(base, entry, '.unifideck-id')
                if os.path.exists(marker):
                    try:
                        with open(marker, 'r') as f:
                            content = f.read().strip()
                            if content == game_id or game_id in content:
                                return os.path.join(base, entry)
                    except:
                        pass
        except:
            pass
    return None


# ============================================================================
# MAIN INSTALLATION
# ============================================================================

def install_via_umu(prefix_path: str, packages: list, game_id: str) -> bool:
    """Install winetricks packages using umu-run."""
    
    marker_file = os.path.join(prefix_path, "unifideck_winetricks_complete.marker")
    
    if not packages:
        logger.info(f"No redistributables required for {game_id}")
        with open(marker_file, 'w') as f:
            f.write("No redistributables needed")
        return True
    
    logger.info(f"Installing redistributables for {game_id}: {', '.join(packages)}")
    
    # Find umu-run
    umu_run = find_umu_run()
    if not umu_run:
        logger.error("umu-run not found!")
        with open(marker_file, 'w') as f:
            f.write("Failed: umu-run not found")
        return False
    
    logger.info(f"Using umu-run: {umu_run}")
    
    # Find Proton
    proton_path = find_proton()
    if proton_path:
        logger.info(f"Using Proton: {proton_path}")
    
    # Setup environment
    env = os.environ.copy()
    env["WINEPREFIX"] = prefix_path
    env["GAMEID"] = f"umu-{game_id}"
    env["UMU_RUNTIME_UPDATE"] = "0"
    env["WINEDEBUG"] = "-all"
    
    if proton_path:
        env["PROTONPATH"] = proton_path
    
    # Install each package
    installed = []
    failed = []
    
    for pkg in packages:
        logger.info(f"Installing {pkg}...")
        try:
            cmd = ["python3", umu_run, "winetricks", pkg]
            result = subprocess.run(cmd, env=env, capture_output=True, timeout=600, text=True)
            
            # Log output for debugging "silent failure" or "already installed" detection
            if result.stdout.strip():
                logger.info(f"[winetricks stdout] {result.stdout.strip()}")
            if result.stderr.strip():
                logger.info(f"[winetricks stderr] {result.stderr.strip()}")
            
            if result.returncode == 0:
                logger.info(f"✓ {pkg} installed")
                installed.append(pkg)
            else:
                if 'already installed' in (result.stderr or '').lower():
                    logger.info(f"✓ {pkg} already installed")
                    installed.append(pkg)
                else:
                    logger.warning(f"⚠ {pkg} may have issues")
                    failed.append(pkg)
                    
        except subprocess.TimeoutExpired:
            logger.error(f"✗ {pkg} timed out")
            failed.append(pkg)
        except Exception as e:
            logger.error(f"✗ {pkg} error: {e}")
            failed.append(pkg)
    
    # Write marker
    with open(marker_file, 'w') as f:
        f.write(f"complete\ntimestamp: {datetime.now().isoformat()}\n")
        if installed:
            f.write(f"installed: {', '.join(installed)}\n")
        if failed:
            f.write(f"failed: {', '.join(failed)}\n")
    
    logger.info("Winetricks installation complete")
    return True


def main():
    if len(sys.argv) < 3:
        print("Usage: winetricks_gog.py <game_id> <prefix_path> [--force]")
        print("\nQueries umu-database for game-specific fixes, falls back to defaults.")
        sys.exit(1)
    
    game_id = sys.argv[1]
    prefix_path = sys.argv[2]
    force = '--force' in sys.argv
    
    logger.info("=" * 60)
    logger.info(f"Winetricks GOG installer for {game_id}")
    logger.info(f"Prefix: {prefix_path}")
    
    marker_file = os.path.join(prefix_path, "unifideck_winetricks_complete.marker")
    
    # Check what's already installed
    previously_installed = set()
    if os.path.exists(marker_file) and not force:
        with open(marker_file, 'r') as f:
            content = f.read()
            if 'complete' in content:
                # Parse previously installed deps
                for line in content.split('\n'):
                    if line.startswith('installed:'):
                        deps_str = line.replace('installed:', '').strip()
                        previously_installed = set(d.strip() for d in deps_str.split(',') if d.strip())
                        break
                
                if previously_installed:
                    # Check if new deps have been added to DEFAULT_DEPS
                    current_defaults = set(DEFAULT_DEPS)
                    missing = current_defaults - previously_installed
                    
                    if missing:
                        logger.info(f"New deps available: {', '.join(missing)}")
                        logger.info("Installing missing deps...")
                    else:
                        logger.info("All deps already installed, skipping")
                        logger.info("=" * 60)
                        sys.exit(0)
                else:
                    logger.info("Already installed, skipping")
                    logger.info("=" * 60)
                    sys.exit(0)
    
    # Detect game type
    game_path = find_game_path(game_id)
    game_type = detect_game_type(game_path)
    
    if game_type != 'windows':
        logger.info(f"Game type is {game_type}, no deps needed")
        with open(marker_file, 'w') as f:
            f.write(f"skipped: {game_type} game")
        logger.info("=" * 60)
        sys.exit(0)
    
    # Get dependencies (queries umu-database)
    packages = get_required_deps(game_id)
    
    # Filter out already installed packages
    if previously_installed:
        packages = [p for p in packages if p not in previously_installed]
        logger.info(f"Installing only missing: {', '.join(packages)}")
    
    # Install
    success = install_via_umu(prefix_path, packages, game_id)
    
    # On success, update marker with all installed (old + new)
    if success and previously_installed:
        marker_file = os.path.join(prefix_path, "unifideck_winetricks_complete.marker")
        all_installed = previously_installed.union(set(packages))
        with open(marker_file, 'w') as f:
            f.write(f"complete\ntimestamp: {datetime.now().isoformat()}\n")
            f.write(f"installed: {', '.join(sorted(all_installed))}\n")
    
    logger.info("=" * 60)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
