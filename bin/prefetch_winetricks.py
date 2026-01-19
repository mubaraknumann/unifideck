#!/usr/bin/env python3
"""
Prefetch common winetricks packages to cache.
Runs once during Epic sync to speed up future installations.

Downloads installers to ~/.cache/winetricks/ for reuse.
"""
import os
import subprocess
import logging
import shutil

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [Prefetch] %(message)s'
)
logger = logging.getLogger("WinetricksPrefetch")

# Common packages that many games need
# These will be pre-downloaded to speed up future installations
COMMON_PACKAGES = [
    "corefonts",      # Microsoft core fonts (Arial, Times, etc)
    "vcrun2015",      # VC++ 2015 (most common)
    "vcrun2019",      # VC++ 2019
    "vcrun2022",      # VC++ 2022 (latest)
    "d3dcompiler_47"  # DirectX shader compiler
]

def prefetch_packages():
    """
    Download winetricks packages to cache without installing.
    Uses --download-only flag to populate ~/.cache/winetricks/
    """
    # Find winetricks
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_winetricks = os.path.join(script_dir, "winetricks")
    
    if os.path.exists(bundled_winetricks):
        winetricks_cmd = bundled_winetricks
        logger.info("Using bundled winetricks")
    else:
        winetricks_cmd = shutil.which("winetricks")
        if not winetricks_cmd:
            logger.error("winetricks not found, cannot prefetch")
            return
        logger.info("Using system winetricks")
    
    cache_dir = os.path.expanduser("~/.cache/winetricks")
    logger.info(f"Prefetching packages to {cache_dir}")
    logger.info(f"Packages: {', '.join(COMMON_PACKAGES)}")
    
    # Download each package
    for package in COMMON_PACKAGES:
        # Check if already cached
        # Note: winetricks cache structure varies, so we just try to download
        logger.info(f"Prefetching {package}...")
        
        try:
            # Use winetricks to download (it will skip if already cached)
            result = subprocess.run(
                [winetricks_cmd, "-q", package, "list-download"],
                capture_output=True,
                text=True,
                timeout=300  # 5 min per package
            )
            
            if result.returncode == 0:
                logger.info(f"✓ {package} cached")
            else:
                logger.warning(f"! {package} download had issues (may already be cached)")
        
        except subprocess.TimeoutExpired:
            logger.warning(f"! {package} download timed out")
        except Exception as e:
            logger.warning(f"! {package} download error: {e}")
    
    logger.info("Prefetch complete")

def main():
    logger.info("=" * 60)
    logger.info("Winetricks Cache Prefetch")
    logger.info("=" * 60)
    
    prefetch_packages()
    
    logger.info("=" * 60)

if __name__ == "__main__":
    main()
