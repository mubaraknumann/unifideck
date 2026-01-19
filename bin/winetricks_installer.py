#!/usr/bin/env python3
"""
Background winetricks installer with game-specific requirements.
Uses umu-run to invoke winetricks, ensuring it uses the same Proton/pfx structure
that games use at runtime. This avoids the race condition where winetricks
installs to root prefix but games use the pfx subdirectory.

Logs to dedicated file: ~/.local/share/unifideck/winetricks.log
"""
import os
import sys
import subprocess
import logging
from pathlib import Path
import shutil

# Setup dedicated logger
LOG_FILE = os.path.expanduser("~/.local/share/unifideck/winetricks.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

class WinetricksFilter(logging.Filter):
    """Filter out wine debug spam"""
    IGNORED_PATTERNS = [
        "fixme:wineusb:",
        "fixme:xinput:",
        "err:hid:",
        "fixme:winebth:",
        "using server-side synchronization",
        "stray \\",
        "pid ",
        "skipping destruction",
        "i386-linux-gnu-capsule",
        "steamrt",
    ]
    
    def filter(self, record):
        msg = record.getMessage()
        return not any(p in msg for p in self.IGNORED_PATTERNS)

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("WinetricksInstaller")

# Add filter to reduce noise
for handler in logger.handlers:
    handler.addFilter(WinetricksFilter())

# Import game_fixes from same directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

try:
    from game_fixes import get_required_winetricks
except ImportError:
    logger.error("Failed to import game_fixes module")
    sys.exit(1)


def find_umu_run():
    """Find umu-run from Heroic or bundled location"""
    candidates = [
        # Heroic's umu-run (preferred - proven working)
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


def install_redistributables_via_umu(prefix_path: str, packages: list[str], game_id: str):
    """
    Install winetricks packages using umu-run.
    
    This ensures winetricks uses the same Proton/pfx structure that games use,
    avoiding the race condition where winetricks and Proton use different prefixes.
    
    Args:
        prefix_path: Wine prefix path (root, umu will create/use pfx subdirectory)
        packages: List of winetricks package names
        game_id: Game ID for marker file
    """
    marker_file = os.path.join(prefix_path, "unifideck_winetricks_complete.marker")
    
    if not packages:
        logger.info(f"No redistributables required for {game_id}")
        with open(marker_file, 'w') as f:
            f.write("No redistributables needed")
        return
    
    logger.info(f"Installing redistributables for {game_id}: {', '.join(packages)}")
    
    # Find umu-run
    umu_run = find_umu_run()
    if not umu_run:
        logger.error("umu-run not found! Cannot install winetricks packages.")
        with open(marker_file, 'w') as f:
            f.write("Failed: umu-run not found")
        return
    
    logger.info(f"Using umu-run: {umu_run}")
    
    # Find Proton
    proton_path = find_proton()
    if not proton_path:
        logger.warning("Proton not found, umu-run will download one (this may take a while)")
    else:
        logger.info(f"Using Proton: {proton_path}")
    
    # Setup environment for umu-run
    env = os.environ.copy()
    env["WINEPREFIX"] = prefix_path
    env["GAMEID"] = "umu-0"  # Generic ID for non-game operations
    env["UMU_RUNTIME_UPDATE"] = "0"  # Don't update runtime during winetricks
    
    if proton_path:
        env["PROTONPATH"] = proton_path
    
    # Clean environment variables that cause noise
    env.pop("LD_PRELOAD", None)
    
    # Create attempt marker
    with open(marker_file, 'w') as f:
        f.write(f"Installing: {', '.join(packages)}")
    
    # Build command: umu-run winetricks <packages>
    # When using umu-run, winetricks becomes an argument, not the command
    if umu_run.endswith('.py'):
        cmd = ["python3", umu_run, "winetricks"] + packages
    else:
        cmd = [umu_run, "winetricks"] + packages
    
    logger.info(f"Executing: {' '.join(cmd)}")
    
    # Retry logic for network failures
    max_attempts = 3
    last_error = None
    
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(f"Attempt {attempt}/{max_attempts}...")
            result = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=1800  # 30 min timeout (first run may download Proton)
            )
            
            # Log important lines
            for line in result.stdout.splitlines():
                # Filter out noise but keep important info
                if any(pattern in line for pattern in ["Executing w_do_call", "Installing", "ERROR", "WARN", "✓", "Done"]):
                    logger.info(line)
            
            if result.returncode == 0:
                logger.info(f"✓ Redistributables installed successfully")
                with open(marker_file, 'w') as f:
                    f.write("Installation complete")
                return  # Success, exit retry loop
            else:
                # Non-zero exit - could be network failure, retry
                logger.warning(f"Attempt {attempt} exited with code {result.returncode}")
                last_error = f"exit code {result.returncode}"
        
        except subprocess.TimeoutExpired:
            logger.warning(f"Attempt {attempt} timed out after 30 minutes")
            last_error = "timeout"
        
        except Exception as e:
            logger.error(f"Attempt {attempt} failed: {e}")
            last_error = str(e)
        
        # Retry if not the last attempt
        if attempt < max_attempts:
            wait_time = 5 * (2 ** (attempt - 1))  # 5, 10, 20 seconds
            logger.info(f"Retrying in {wait_time}s...")
            import time
            time.sleep(wait_time)
    
    # All attempts failed
    logger.error(f"All {max_attempts} attempts failed. Last error: {last_error}")
    with open(marker_file, 'w') as f:
        f.write(f"Installation failed after {max_attempts} attempts: {last_error}")


def main():
    if len(sys.argv) < 3:
        print("Usage: winetricks_installer.py <game_id> <prefix_path>")
        sys.exit(1)
    
    game_id = sys.argv[1]
    prefix_path = sys.argv[2]
    
    logger.info(f"{'='*60}")
    logger.info(f"Winetricks installer started for {game_id}")
    logger.info(f"Prefix: {prefix_path}")
    
    # Ensure prefix directory exists (umu-run will initialize it properly)
    os.makedirs(prefix_path, exist_ok=True)
    
    # Check if already installed
    marker_file = os.path.join(prefix_path, "unifideck_winetricks_complete.marker")
    if os.path.exists(marker_file):
        with open(marker_file, 'r') as f:
            status = f.read().strip()
        
        if "complete" in status.lower() or "no redistributables" in status.lower():
            logger.info("Redistributables already installed, skipping")
            logger.info(f"{'='*60}")
            return
        else:
            logger.info(f"Previous installation incomplete: {status}")
            logger.info("Retrying installation...")
    
    # Get required packages from umu-protonfixes database
    try:
        packages = get_required_winetricks(game_id)
    except Exception as e:
        logger.error(f"Failed to get required packages: {e}")
        packages = []
    
    # Install using umu-run
    install_redistributables_via_umu(prefix_path, packages, game_id)
    
    logger.info(f"Winetricks installer complete for {game_id}")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
