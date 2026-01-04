#!/usr/bin/env python3
import os
import shutil
import sys
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FixEpic")

def main():
    if len(sys.argv) < 2:
        print("Usage: fix_epic_launcher_prefix.py <wine_prefix_path>")
        sys.exit(1)

    prefix_path = sys.argv[1]
    
    # Handle the 'pfx' subdirectory nuance
    drive_c = os.path.join(prefix_path, "drive_c")
    if not os.path.exists(drive_c):
        # Try 'pfx' subfolder
        pfx_drive_c = os.path.join(prefix_path, "pfx", "drive_c")
        if os.path.exists(pfx_drive_c):
            prefix_path = os.path.join(prefix_path, "pfx")
            drive_c = pfx_drive_c
        else:
            logger.error(f"drive_c not found in {prefix_path}")
            sys.exit(1)

    logger.info(f"Targeting prefix: {prefix_path}")

    # 1. Create directory structure for Epic Launcher
    # Standard path checks: C:\Program Files (x86)\Epic Games\Launcher\Portal\Binaries\Win32\EpicGamesLauncher.exe
    
    target_dir = os.path.join(drive_c, "Program Files (x86)", "Epic Games", "Launcher", "Portal", "Binaries", "Win32")
    os.makedirs(target_dir, exist_ok=True)
    logger.info(f"Created directory: {target_dir}")
    
    target_exe = os.path.join(target_dir, "EpicGamesLauncher.exe")
    
    if os.path.exists(target_exe):
        logger.info(f"EpicGamesLauncher.exe already exists at {target_exe}")
    else:
        # Use notepad.exe as the dummy executable
        # It's a valid PE executable, so it satisfies 'file exists' and 'is executable' checks
        source_exe = os.path.join(drive_c, "windows", "notepad.exe")
        
        # Fallback to explorer.exe if notepad is missing for some reason
        if not os.path.exists(source_exe):
             source_exe = os.path.join(drive_c, "windows", "explorer.exe")
             
        if os.path.exists(source_exe):
            shutil.copy2(source_exe, target_exe)
            logger.info(f"Created fake EpicGamesLauncher.exe at {target_exe} (copied from {os.path.basename(source_exe)})")
        else:
            logger.error("Could not find source exe (notepad.exe or explorer.exe) to copy")
         
    # Optional: Heroic also copies it to C:\windows\command\EpicGamesLauncher.exe
    # We can do that too just in case
    win_command_dir = os.path.join(drive_c, "windows", "command")
    if os.path.exists(win_command_dir):
        win_target = os.path.join(win_command_dir, "EpicGamesLauncher.exe")
        if not os.path.exists(win_target) and os.path.exists(target_exe):
             shutil.copy2(target_exe, win_target)
             logger.info(f"Also created copy at {win_target}")

    # 2. Add Registry Key (Critical for some games like Fallout NV, Dishonored)
    # Heroic adds: reg add HKEY_CLASSES_ROOT\com.epicgames.launcher /f
    logger.info("Applying registry fix...")
    
    # We need to run reg.exe inside the prefix
    # Um, we are in python. We can use os.system with WINEPREFIX set
    # Or just write a .reg file and import it? "reg add" is simpler if we have wine.
    
    # Let's try to find 'wine' or 'proton' to run this.
    # Since we are essentially "outside" the game launch context, we can try to use 
    # the system wine if available, or just rely on the fact that if this is Steam Deck,
    # we might not have 'wine' in path globally easily.
    
    # Alternative: Write a .reg file to drive_c and let the user import it? 
    # No, automation is better.
    
    # Let's use the umu-run or just try to find the system 'reg' or 'wine'
    # Actually, simpler way: edit system.reg directly? No, unsafe.
    
    # Let's treat this as a "best effort" using 'wine' command if available.
    # Note: On Steam Deck, 'wine' might not be in PATH.
    # But usually 'proton' handles this.
    
    # Let's try to construct a command to run 'reg.exe' inside the prefix.
    # If we are effectively in a script, we can perhaps use the environment variables.
    
    env = os.environ.copy()
    env["WINEPREFIX"] = prefix_path
    
    
    # Check if registry key already exists to avoid slow reg.exe call every time
    # This is a simple optimization.
    registry_check_marker = os.path.join(prefix_path, "unifideck_epic_fix_applied.marker")
    if os.path.exists(registry_check_marker):
        # Already applied, skip unless forced?
        # For robustness, let's trust the marker.
        return 
        
    env = os.environ.copy()
    env["WINEPREFIX"] = prefix_path
    
    # Try to find a wine binary from Proton paths
    proton_paths = [
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton - Experimental/files/bin/wine"),
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton 10.0/files/bin/wine"),
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton 9.0 (Beta)/files/bin/wine"),
        os.path.expanduser("~/.steam/steam/steamapps/common/Proton 8.0/files/bin/wine"),
        os.path.expanduser("~/.steam/root/steamapps/common/Proton - Experimental/files/bin/wine")
    ]
    
    wine_bin = None
    for path in proton_paths:
        if os.path.exists(path):
            wine_bin = path
            break
            
    if not wine_bin and shutil.which("wine"):
        wine_bin = "wine" # Fallback to system wine if found
        
    if not wine_bin:
        logger.warning("No wine binary found. Skipping registry fix.")
    else:
        import subprocess
        try:
           # reg add HKEY_CLASSES_ROOT\com.epicgames.launcher /f
           cmd = [wine_bin, "reg", "add", "HKEY_CLASSES_ROOT\\com.epicgames.launcher", "/f"]
           subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
           # Create marker file
           with open(registry_check_marker, 'w') as f:
               f.write("Registry fix applied")
           logger.info("Universal Epic Launcher fix applied.")
        except Exception as e:
           logger.error(f"Failed to apply registry key: {e}")

    logger.info("Epic Launcher workaround applied successfully.")

if __name__ == "__main__":
    main()
