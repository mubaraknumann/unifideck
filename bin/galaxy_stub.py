#!/usr/bin/env python3
"""
GOG Galaxy Stub Installer - Installs GalaxyCommunication.exe stub to prefix.

Some GOG games expect GOG Galaxy services to be available. This installs a
dummy GalaxyCommunication.exe to satisfy those requirements without actually
running Galaxy services.

Based on Heroic Games Launcher's approach (launcher.ts:904-928).
"""
import os
import sys
import shutil
import logging

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STUB_FILE = os.path.join(SCRIPT_DIR, "stubs", "GalaxyCommunication.exe")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("GalaxyStub")


def install_galaxy_stub(prefix_path: str) -> bool:
    """
    Install GalaxyCommunication.exe stub to a Wine prefix.
    
    Args:
        prefix_path: Path to the Wine prefix (e.g., ~/.local/share/unifideck/prefixes/1234567)
    
    Returns:
        True if successful, False otherwise.
    """
    if not os.path.exists(STUB_FILE):
        logger.warning(f"GalaxyCommunication.exe stub not found at {STUB_FILE}")
        return False
    
    # Determine drive_c path (Proton uses pfx/drive_c, regular Wine uses drive_c)
    drive_c = os.path.join(prefix_path, "pfx", "drive_c")
    if not os.path.exists(drive_c):
        drive_c = os.path.join(prefix_path, "drive_c")
    
    if not os.path.exists(drive_c):
        logger.warning(f"drive_c not found in prefix: {prefix_path}")
        return False
    
    # Target path: C:\ProgramData\GOG.com\Galaxy\redists\GalaxyCommunication.exe
    target_dir = os.path.join(drive_c, "ProgramData", "GOG.com", "Galaxy", "redists")
    target_file = os.path.join(target_dir, "GalaxyCommunication.exe")
    
    # Check if already installed
    if os.path.exists(target_file):
        logger.info("GalaxyCommunication.exe stub already installed")
        return True
    
    try:
        os.makedirs(target_dir, exist_ok=True)
        shutil.copy(STUB_FILE, target_file)
        logger.info(f"Installed GalaxyCommunication.exe stub to {target_file}")
        return True
    except Exception as e:
        logger.error(f"Failed to install Galaxy stub: {e}")
        return False


def main():
    """CLI interface: galaxy_stub.py <prefix_path>"""
    if len(sys.argv) < 2:
        print("Usage: galaxy_stub.py <prefix_path>")
        print("Example: galaxy_stub.py ~/.local/share/unifideck/prefixes/1213504814")
        sys.exit(1)
    
    prefix_path = os.path.expanduser(sys.argv[1])
    
    if not os.path.isdir(prefix_path):
        logger.error(f"Prefix path does not exist: {prefix_path}")
        sys.exit(1)
    
    success = install_galaxy_stub(prefix_path)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
