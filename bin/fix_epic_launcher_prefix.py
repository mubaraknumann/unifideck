#!/usr/bin/env python3
"""
Quick Epic Launcher fix script.
ONLY copies EpicGamesLauncher.exe wrapper and applies registry fix.
NO winetricks installation (moved to background installer).
"""
import os
import shutil
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("QuickFix")

def copy_wrapper_to_prefix(drive_c: str, bundled_wrapper: str, prefix_label: str) -> bool:
    """Copy the Epic wrapper to a specific drive_c location. Returns True if any copy was made."""
    copied = False
    
    # Copy to Program Files location
    target_dir = os.path.join(drive_c, "Program Files (x86)", "Epic Games", "Launcher", "Portal", "Binaries", "Win32")
    os.makedirs(target_dir, exist_ok=True)
    target_exe = os.path.join(target_dir, "EpicGamesLauncher.exe")
    
    if os.path.exists(bundled_wrapper):
        # Always overwrite - don't check if exists because Proton/Wine creates a dummy stub
        # Remove first to handle read-only files
        if os.path.exists(target_exe):
            try:
                os.remove(target_exe)
            except:
                pass  # Best effort
        shutil.copy2(bundled_wrapper, target_exe)
        logger.info(f"✓ Copied wrapper to Epic dir ({prefix_label})")
        copied = True
    
    # Copy to C:\windows\command\ (THIS is where LEGENDARY_WRAPPER_EXE points!)
    # CRITICAL: Always overwrite because Proton/Wine creates its own EpicGamesLauncher.exe 
    # stub in windows/command that opens Notepad instead of the game!
    win_command_dir = os.path.join(drive_c, "windows", "command")
    os.makedirs(win_command_dir, exist_ok=True)
    win_target = os.path.join(win_command_dir, "EpicGamesLauncher.exe")
    
    if os.path.exists(bundled_wrapper):
        # Remove first to handle read-only files created by Proton/Wine
        if os.path.exists(win_target):
            try:
                os.remove(win_target)
            except:
                pass  # Best effort
        shutil.copy2(bundled_wrapper, win_target)
        logger.info(f"✓ Copied wrapper to windows/command ({prefix_label})")
        copied = True
    
    return copied

def main():
    if len(sys.argv) < 2:
        print("Usage: fix_epic_launcher_prefix.py <wine_prefix_path>")
        sys.exit(1)

    prefix_path = sys.argv[1]
    
    # Get bundled wrapper path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_wrapper = os.path.join(script_dir, "EpicGamesLauncher.exe")
    
    if not os.path.exists(bundled_wrapper):
        logger.warning(f"Bundled wrapper not found at {bundled_wrapper}")
        return
    
    # Check for both possible drive_c locations and apply to BOTH if they exist
    # umu/legendary can use either the root prefix or a 'pfx' subdirectory
    root_drive_c = os.path.join(prefix_path, "drive_c")
    pfx_drive_c = os.path.join(prefix_path, "pfx", "drive_c")
    
    found_any = False
    
    if os.path.exists(root_drive_c):
        copy_wrapper_to_prefix(root_drive_c, bundled_wrapper, "root")
        found_any = True
    
    if os.path.exists(pfx_drive_c):
        copy_wrapper_to_prefix(pfx_drive_c, bundled_wrapper, "pfx")
        found_any = True
    
    if not found_any:
        logger.info("Prefix not initialized yet, skipping")
        return
    
    # Apply registry fix to the pfx subdirectory if it exists (that's what umu uses)
    # Otherwise use the root prefix
    if os.path.exists(pfx_drive_c):
        registry_prefix = os.path.join(prefix_path, "pfx")
    else:
        registry_prefix = prefix_path
    
    env = os.environ.copy()
    env["WINEPREFIX"] = registry_prefix
    
    # Find wine binary
    proton_paths = [
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton - Experimental/files/bin/wine"),
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton 10.0/files/bin/wine"),
    ]
    
    wine_bin = None
    for path in proton_paths:
        if os.path.exists(path):
            wine_bin = path
            break
    
    if not wine_bin:
        wine_bin = shutil.which("wine")
    
    if wine_bin:
        import subprocess
        try:
            cmd = [wine_bin, "reg", "add", "HKEY_CLASSES_ROOT\\\\com.epicgames.launcher", "/f"]
            subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            logger.info("✓ Applied registry fix")
        except Exception as e:
            logger.warning(f"Registry fix failed (non-critical): {e}")
    else:
        logger.warning("Wine not found, skipping registry fix")
    
    logger.info("Quick fix complete")

if __name__ == "__main__":
    main()
