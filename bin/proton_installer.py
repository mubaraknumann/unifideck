#!/usr/bin/env python3
import os
import sys
import json
import time
import shutil
import tarfile
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

# Configuration
STEAM_ROOT = Path(os.path.expanduser("~/.steam/steam"))
COMPAT_TOOLS_DIR = STEAM_ROOT / "compatibilitytools.d"
STEAM_APPS_COMMON = STEAM_ROOT / "steamapps/common"

# Official Proton AppID Mapping
OFFICIAL_PROTON_MAP = {
    "Proton Experimental": "1493710",
    "proton-experimental": "1493710",
    "Proton 10.0": "3658110",
    "Proton-10.0": "3658110", # Catch-all for subversions if mapped generically
    "Proton 9.0": "2805730",
    "Proton-9.0": "2805730",
    "Proton 8.0": "2348590",
    "Proton-8.0": "2348590",
    "Proton 7.0": "1887720",
    "Proton-7.0": "1887720"
} 

def log(msg):
    print(f"[ProtonInstaller] {msg}", flush=True)

def install_ge_proton(version_tag):
    """Downloads and installs a specific GE-Proton version from GitHub."""
    
    install_dir = COMPAT_TOOLS_DIR / version_tag
    if install_dir.exists():
        log(f"Version {version_tag} already exists at {install_dir}")
        return str(install_dir)

    log(f"Installing {version_tag}...")
    COMPAT_TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Get Release URL
    api_url = f"https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/tags/{version_tag}"
    try:
        log(f"Fetching release info from {api_url}")
        with urllib.request.urlopen(api_url) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        log(f"Failed to find release {version_tag}: {e}")
        return None

    # Find asset
    tarball_url = None
    asset_name = None
    for asset in data.get("assets", []):
        if asset["name"].endswith(".tar.gz") and "sha512" not in asset["name"]:
            tarball_url = asset["browser_download_url"]
            asset_name = asset["name"]
            break
    
    if not tarball_url:
        log("Could not find .tar.gz asset in release")
        return None

    # 2. Download with retry
    temp_tar = COMPAT_TOOLS_DIR / asset_name
    log(f"Downloading {asset_name}...")
    
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            log(f"Download attempt {attempt}/{max_attempts}...")
            urllib.request.urlretrieve(tarball_url, temp_tar)
            break  # Success
        except Exception as e:
            log(f"Download attempt {attempt} failed: {e}")
            if temp_tar.exists():
                temp_tar.unlink()
            
            if attempt < max_attempts:
                wait_time = 5 * (2 ** (attempt - 1))  # 5, 10, 20 seconds
                log(f"Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                log(f"All {max_attempts} download attempts failed")
                return None

    # 3. Extract
    log(f"Extracting to {COMPAT_TOOLS_DIR}...")
    try:
        with tarfile.open(temp_tar, "r:gz") as tar:
            tar.extractall(path=COMPAT_TOOLS_DIR)
    except Exception as e:
        log(f"Extraction failed: {e}")
        return None
    finally:
        if temp_tar.exists():
            temp_tar.unlink()

    if install_dir.exists():
        log(f"Successfully installed to {install_dir}")
        return str(install_dir)
    else:
        log("Installation finished but directory not found (folder name mismatch?)")
        # Try to find what was extracted? (Assuming standard naming convention matches tag)
        return None


def install_official_proton(version_name):
    """Triggers Steam install for official Proton versions."""
    
    # Normalize name loosely to find ID
    app_id = None
    target_name = None
    
    # 1. Try exact match
    if version_name in OFFICIAL_PROTON_MAP:
        app_id = OFFICIAL_PROTON_MAP[version_name]
        target_name = version_name
    
    # 2. Try partial match (e.g. "Proton-10.0-3" -> "Proton 10.0")
    if not app_id:
        for key, aid in OFFICIAL_PROTON_MAP.items():
            # Check if requested version starts with a mapped key (e.g. Proton-10.0 starts with Proton-10.0)
            if version_name.startswith(key.replace(" ", "-")) or version_name.startswith(key):
                app_id = aid
                target_name = key
                break
    
    if not app_id:
        log(f"Could not map '{version_name}' to a Steam AppID")
        return None

    log(f"identified {version_name} as AppID {app_id} ({target_name})")
    
    # Check if already installed (manifest exists)
    manifest_path = STEAM_ROOT / "steamapps" / f"appmanifest_{app_id}.acf"
    common_dir = STEAM_APPS_COMMON / target_name # approximate path check
    
    if manifest_path.exists():
        log(f"Manifest for {app_id} exists. Checking directory...")
        # We can try to resolve the actual directory, but the launcher does its own resolution.
        # If it's here, we assume it's installed or Steam is managing it.
        # But if we are here, the launcher FAILED to find it, so maybe it's corrupted or path is wrong?
        # Triggers verify/download anyway.
        pass

    # Trigger Install
    log(f"Triggering Steam install for AppID {app_id}...")
    try:
        subprocess.run(["steam", f"steam://install/{app_id}"], check=False)
    except FileNotFoundError:
        log("Steam executable not found in PATH")
        return None

    # Wait loop (up to 5 mins?)
    # Realistically, for big downloads, this script might time out if we wait too long.
    # But Proton downloads are usually fast.
    log("Waiting for installation to complete (checking for manifest + dir)...")
    
    start_time = time.time()
    while time.time() - start_time < 300: # 5 minute timeout
        if manifest_path.exists():
            # Check content of manifest for "State" "4" (Fully Installed)?
            # Parsing ACF is annoying in pure python without regex/vdf lib, simplistic check:
            try:
                with open(manifest_path) as f:
                    content = f.read()
                    if '"State"\t\t"4"' in content or '"State" "4"' in content:
                        log("App manifest reports State 4 (Ready)")
                        return "INSTALLED_VIA_STEAM"
            except:
                pass
            
            # Or just check if directory exists in common?
            # We don't know the exact folder name 100% without parsing manifest "installdir"
            # But we can best-guess based on standard names
            pass
        
        time.sleep(5)
        print(".", end="", flush=True)
    
    log("Timed out waiting for Steam installation.")
    return None

def main():
    if len(sys.argv) < 2:
        print("Usage: proton_installer.py <ProtonVersionString>")
        sys.exit(1)

    version_string = sys.argv[1]
    
    result = None
    if version_string.lower().startswith("ge-proton"):
        result = install_ge_proton(version_string)
    elif "proton" in version_string.lower():
        result = install_official_proton(version_string)
    else:
        log(f"Unknown Proton type: {version_string}")

    if result:
        print(result) # Print the result path (or status) to stdout for the caller
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
