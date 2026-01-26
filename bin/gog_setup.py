#!/usr/bin/env python3
"""
GOG game setup - installs redistributables after download/on first launch.
Port of Heroic's src/backend/storeManagers/gog/setup.ts

This script:
1. Reads game manifest from gogdl manifests directory
2. Extracts dependency IDs (DirectX, vcrun, etc.)
3. Downloads redistributables via gogdl if not present
4. Runs scriptinterpreter.exe for v2 manifests
5. Installs each redistributable into the Wine prefix

Usage: gog_setup.py <game_id> <prefix_path> <install_path>
"""

import os
import sys
import json
import subprocess
import shlex
import fcntl
import time
from pathlib import Path

# Paths
GOGDL_CONFIG = Path.home() / ".config" / "unifideck" / "gogdl"
MANIFESTS_DIR = GOGDL_CONFIG / "manifests"
REDIST_DIR = GOGDL_CONFIG / "redist"
SUPPORT_DIR = GOGDL_CONFIG / "gog-support"  # Where temp_executable files are stored

# Plugin directory (for gogdl binary and umu-run)
PLUGIN_DIR = Path.home() / "homebrew" / "plugins" / "Unifideck"
GOGDL_BIN = PLUGIN_DIR / "bin" / "gogdl"
UMU_RUN = PLUGIN_DIR / "bin" / "umu" / "umu" / "umu-run"

# Auth config for gogdl
AUTH_CONFIG = GOGDL_CONFIG / "auth.json"

# Log file

LOG_FILE = Path.home() / ".local" / "share" / "unifideck" / "gog_setup.log"


def log(message: str):
    """Log message to file and stdout."""
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    print(log_msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(log_msg + "\n")


def wait_for_prefix_ready(prefix_path: str, timeout: int = 30) -> bool:
    """Wait for Wine prefix to be initialized.

    Proton prefixes need pfx/system.reg to exist before running setup executables.
    """
    # Proton prefixes have pfx/system.reg, Wine has system.reg
    system_reg_proton = Path(prefix_path) / "pfx" / "system.reg"
    system_reg_wine = Path(prefix_path) / "system.reg"

    start = time.time()
    while not (system_reg_proton.exists() or system_reg_wine.exists()):
        elapsed = time.time() - start
        if elapsed > timeout:
            log(f"ERROR: Prefix not ready after {timeout}s")
            log(f"Checked paths: {system_reg_proton}, {system_reg_wine}")
            return False

        if elapsed % 5 == 0:  # Log every 5 seconds
            log(f"Waiting for Wine prefix initialization... ({int(elapsed)}s)")
        time.sleep(1)

    log("Wine prefix ready")
    return True


def get_manifest(game_id: str) -> dict | None:
    """Load game manifest from gogdl manifests directory."""
    manifest_path = MANIFESTS_DIR / game_id
    if not manifest_path.exists():
        log(f"No manifest found at {manifest_path}")
        return None

    try:
        with open(manifest_path) as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to parse manifest: {e}")
        return None


def get_dependencies(manifest: dict) -> list[str]:
    """Extract dependency IDs from manifest.

    v1 manifests: dependencies in product.depots[].redist
    v2 manifests: dependencies in top-level dependencies array
    """
    deps = []

    if manifest.get("version") == 1:
        # v1 format
        for depot in manifest.get("product", {}).get("depots", []):
            if "redist" in depot:
                redist = depot["redist"]
                if redist not in deps:
                    deps.append(redist)
    else:
        # v2 format
        for dep in manifest.get("dependencies", []):
            if dep not in deps:
                deps.append(dep)

    return deps


def get_redist_manifest() -> dict | None:
    """Load the redistributables manifest created by gogdl."""
    manifest_path = REDIST_DIR / ".gogdl-redist-manifest"
    if not manifest_path.exists():
        return None

    try:
        with open(manifest_path) as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to parse redist manifest: {e}")
        return None


def ensure_redist_downloaded(deps: list[str]):
    """Download redistributables if not already present."""
    # Always include ISI (scriptinterpreter) for v2 manifests
    all_deps = ["ISI"] + [d for d in deps if d != "ISI"]

    # Use lock file to prevent concurrent downloads
    lock_file = REDIST_DIR / ".download.lock"
    REDIST_DIR.mkdir(parents=True, exist_ok=True)

    # Try to acquire exclusive lock
    with open(lock_file, 'w') as lock:
        try:
            # Non-blocking lock attempt
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            log("Acquired download lock")
        except BlockingIOError:
            # Another process is downloading
            log("Another process is downloading redistributables, waiting...")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)  # Wait for lock
            log("Download lock acquired, verifying downloads")

        # Check if redistributables manifest exists
        manifest_path = REDIST_DIR / ".gogdl-redist-manifest"
        if manifest_path.exists():
            # Check if all required deps are in the manifest
            redist_manifest = get_redist_manifest()
            if redist_manifest:
                installed_deps = [depot["dependencyId"] for depot in redist_manifest.get("depots", [])]
                missing = [d for d in all_deps if d not in installed_deps]
                if not missing:
                    log("All redistributables already downloaded")
                    return
                all_deps = missing

        # Download via gogdl
        if not GOGDL_BIN.exists():
            log(f"ERROR: gogdl binary not found at {GOGDL_BIN}")
            return

        if not AUTH_CONFIG.exists():
            log(f"ERROR: GOG auth config not found at {AUTH_CONFIG}")
            return

        log(f"Downloading redistributables: {', '.join(all_deps)}")

        try:
            cmd = [
                str(GOGDL_BIN),
                "--auth-config-path", str(AUTH_CONFIG),
                "redist",
                "--ids", ",".join(all_deps),
                "--path", str(REDIST_DIR)
            ]
            log(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                log(f"Failed to download redistributables: {result.stderr}")
            else:
                log("Redistributables downloaded successfully")
        except Exception as e:
            log(f"Exception downloading redistributables: {e}")


def run_wine_command(exe_path: str, args: list[str], prefix_path: str, install_path: str) -> bool:
    """Run a Windows executable via umu-run in the Wine prefix."""
    if not UMU_RUN.exists():
        log(f"ERROR: umu-run not found at {UMU_RUN}")
        return False

    # Find Python 3.10+
    python_bin = None
    for py in ["/usr/bin/python3.13", "/usr/bin/python3.12", "/usr/bin/python3.11",
               "/usr/bin/python3.10", "/usr/bin/python3"]:
        if os.path.exists(py):
            python_bin = py
            break

    if not python_bin:
        log("ERROR: Python 3.10+ not found")
        return False

    # Set environment variables for umu-run
    env = os.environ.copy()
    env["WINEPREFIX"] = prefix_path
    env["GAMEID"] = "umu-0"
    env["STORE"] = "gog"
    env["PROTON_VERB"] = "run"
    env["STEAM_COMPAT_INSTALL_PATH"] = install_path

    # Build command
    cmd = [python_bin, str(UMU_RUN), exe_path] + args

    log(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"Command failed with exit code {result.returncode}")
            log(f"stderr: {result.stderr}")
            return False
        return True
    except Exception as e:
        log(f"Exception running command: {e}")
        return False


def run_script_interpreter(game_id: str, manifest: dict, prefix_path: str, install_path: str) -> bool:
    """Run scriptinterpreter.exe for v2 manifests.

    Heroic's setup.ts lines 246-281
    """
    isi_path = REDIST_DIR / "__redist" / "ISI" / "scriptinterpreter.exe"
    if not isi_path.exists():
        log(f"scriptinterpreter.exe not found at {isi_path}")
        return False

    support_dir = SUPPORT_DIR / game_id

    # Get install language (default to English)
    install_language = "en-US"  # TODO: Get from game info if available

    log("Running scriptinterpreter for game setup...")

    success = True
    for product in manifest.get("products", []):
        product_id = product.get("productId")
        if not product_id:
            continue

        # Get build ID and version from manifest
        build_id = manifest.get("buildId", "0")
        version = manifest.get("version_name", "1.0")

        # Construct arguments for scriptinterpreter
        # See Heroic setup.ts lines 257-270
        args = [
            "/VERYSILENT",
            f"/DIR={install_path}",
            f"/Language=English",
            f"/LANG=English",
            f"/ProductId={product_id}",
            "/galaxyclient",
            f"/buildId={build_id}",
            f"/versionName={version}",
            f"/lang-code={install_language}",
            f"/supportDir={support_dir}",
            "/nodesktopshorctut",  # Yes, this typo is in GOG's setup
            "/nodesktopshortcut"
        ]

        log(f"Installing setup for product {product_id}")
        if not run_wine_command(str(isi_path), args, prefix_path, install_path):
            log(f"ERROR: Script interpreter failed for product {product_id}")
            success = False

    return success


def run_temp_executable(game_id: str, manifest: dict, prefix_path: str, install_path: str) -> bool:
    """Run temp_executable for v2 manifests without scriptInterpreter.

    This is Heroic's setup.ts Path B (lines 283-328).
    For games like The Witcher that have a game-specific setup executable
    instead of using the generic scriptinterpreter (ISI).
    """
    log("Running temp_executable setup (v2 manifest without scriptInterpreter)...")

    success = True
    for product in manifest.get("products", []):
        temp_exe = product.get("temp_executable", "")
        if not temp_exe:
            log(f"Product {product.get('productId', 'unknown')} has no temp_executable, skipping")
            continue

        product_id = product.get("productId", game_id)

        # Path: ~/.config/unifideck/gogdl/gog-support/{game_id}/{product_id}/{temp_executable}
        exe_path = SUPPORT_DIR / game_id / product_id / temp_exe

        if not exe_path.exists():
            log(f"ERROR: temp_executable not found: {exe_path}")
            log(f"This file should have been downloaded during game installation.")
            log(f"Try re-downloading the game or manually downloading support files.")
            success = False
            continue

        # Get build ID and version from manifest
        build_id = manifest.get("buildId", "0")
        version = manifest.get("version_name", "1.0")
        install_language = manifest.get("HGLInstallLanguage", "en-US")

        # Build arguments matching Heroic's setup.ts lines 295-315
        args = [
            "/VERYSILENT",
            f"/DIR={install_path}",
            f"/Language=English",
            f"/LANG=English",
            f"/lang-code={install_language}",
            f"/ProductId={product_id}",
            "/galaxyclient",
            f"/buildId={build_id}",
            f"/versionName={version}",
            "/nodesktopshorctut",  # Note: typo is intentional (matches GOG)
            "/nodesktopshortcut"
        ]

        log(f"Running temp_executable for product {product_id}: {temp_exe}")
        if not run_wine_command(str(exe_path), args, prefix_path, install_path):
            log(f"ERROR: Failed to execute temp_executable {temp_exe}")
            success = False

    return success


def install_redistributables(deps: list[str], redist_manifest: dict, prefix_path: str, install_path: str) -> bool:
    """Install each redistributable via wine/proton.

    Heroic's setup.ts lines 349-400
    """
    log(f"Installing {len(deps)} redistributables...")

    success = True
    for dep in deps:
        # Find depot info for this dependency
        depot = None
        for d in redist_manifest.get("depots", []):
            if d.get("dependencyId") == dep:
                depot = d
                break

        if not depot:
            log(f"WARNING: Dependency {dep} not found in redist manifest")
            continue

        exe_info = depot.get("executable", {})
        exe_path_rel = exe_info.get("path", "")

        if not exe_path_rel:
            log(f"Skipping {dep} - no executable path")
            continue

        # Skip redistributables installed into game directory (not prefix)
        if not exe_path_rel.startswith("__redist"):
            log(f"Skipping {dep} - installs to game directory")
            continue

        exe_path = REDIST_DIR / exe_path_rel
        if not exe_path.exists():
            log(f"WARNING: Executable not found: {exe_path}")
            continue

        # Parse arguments
        args_str = exe_info.get("arguments", "")
        args = shlex.split(args_str) if args_str else []

        readable_name = depot.get("readableName", dep)

        # HACK: Special handling for PHYSXLEGACY (see Heroic setup.ts:375-378)
        if dep == "PHYSXLEGACY":
            args = ["msiexec", "/i", str(exe_path), "/qb"]
            exe_path = "msiexec"  # Use msiexec as the executable

        log(f"Installing {readable_name} ({dep})...")

        # Install the redistributable
        if dep == "PHYSXLEGACY":
            # For PHYSXLEGACY, we prepended msiexec to args
            if not run_wine_command(args[0], args[1:], prefix_path, install_path):
                log(f"ERROR: Failed to install {readable_name}")
                success = False
        else:
            if not run_wine_command(str(exe_path), args, prefix_path, install_path):
                log(f"ERROR: Failed to install {readable_name}")
                success = False

    return success


def run_setup(game_id: str, prefix_path: str, install_path: str):
    """Main setup function - installs redistributables."""
    log(f"=== GOG Setup for {game_id} ===")
    log(f"Prefix: {prefix_path}")
    log(f"Install: {install_path}")

    # Wait for prefix to be ready before running setup
    if not wait_for_prefix_ready(prefix_path):
        log("ERROR: Wine prefix initialization timeout")
        sys.exit(1)

    # 1. Load game manifest
    manifest = get_manifest(game_id)
    if not manifest:
        log(f"No manifest for {game_id}, skipping setup")
        return

    # 2. Get dependencies
    deps = get_dependencies(manifest)
    log(f"Dependencies: {', '.join(deps) if deps else 'none'}")

    errors = []  # Track errors

    # 3. Ensure redistributables are downloaded
    if deps:
        ensure_redist_downloaded(deps)

    # 4. Run setup executables based on manifest type (v2 has two paths)
    if manifest.get("version") == 2:
        if manifest.get("scriptInterpreter"):
            # Path A: Use ISI (scriptinterpreter.exe) - for games like Dredge
            log("Manifest requires scriptinterpreter (ISI)")
            if not run_script_interpreter(game_id, manifest, prefix_path, install_path):
                errors.append("Script interpreter execution failed")
        else:
            # Path B: Run temp_executable - for games like The Witcher
            # This is the game-specific setup executable from products[]
            log("Manifest uses temp_executable (no scriptInterpreter)")
            if not run_temp_executable(game_id, manifest, prefix_path, install_path):
                errors.append("temp_executable execution failed")

    # 5. Install redistributables
    if deps:
        redist_manifest = get_redist_manifest()
        if redist_manifest:
            if not install_redistributables(deps, redist_manifest, prefix_path, install_path):
                errors.append("Redistributable installation failed")
        else:
            log("WARNING: No redistributable manifest found")
            errors.append("Redistributable manifest not found")
    else:
        log("No redistributable dependencies for this game")

    if errors:
        log(f"=== Setup FAILED with errors: {', '.join(errors)} ===")
        sys.exit(1)  # Exit with error code

    log("=== Setup complete ===")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: gog_setup.py <game_id> <prefix_path> <install_path>")
        sys.exit(1)

    game_id = sys.argv[1]
    prefix_path = sys.argv[2]
    install_path = sys.argv[3]

    run_setup(game_id, prefix_path, install_path)
