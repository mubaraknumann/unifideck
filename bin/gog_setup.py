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
from pathlib import Path

# Paths
GOGDL_CONFIG = Path.home() / ".config" / "unifideck" / "gogdl"
MANIFESTS_DIR = GOGDL_CONFIG / "manifests"
REDIST_DIR = GOGDL_CONFIG / "redist"
SUPPORT_DIR = GOGDL_CONFIG / "support"

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
        REDIST_DIR.mkdir(parents=True, exist_ok=True)
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


def run_wine_command(exe_path: str, args: list[str], prefix_path: str, install_path: str):
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


def run_script_interpreter(game_id: str, manifest: dict, prefix_path: str, install_path: str):
    """Run scriptinterpreter.exe for v2 manifests.

    Heroic's setup.ts lines 246-281
    """
    isi_path = REDIST_DIR / "__redist" / "ISI" / "scriptinterpreter.exe"
    if not isi_path.exists():
        log(f"scriptinterpreter.exe not found at {isi_path}")
        return

    support_dir = SUPPORT_DIR / game_id

    # Get install language (default to English)
    install_language = "en-US"  # TODO: Get from game info if available

    log("Running scriptinterpreter for game setup...")

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
        run_wine_command(str(isi_path), args, prefix_path, install_path)


def install_redistributables(deps: list[str], redist_manifest: dict, prefix_path: str, install_path: str):
    """Install each redistributable via wine/proton.

    Heroic's setup.ts lines 349-400
    """
    log(f"Installing {len(deps)} redistributables...")

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
            run_wine_command(args[0], args[1:], prefix_path, install_path)
        else:
            run_wine_command(str(exe_path), args, prefix_path, install_path)


def run_setup(game_id: str, prefix_path: str, install_path: str):
    """Main setup function - installs redistributables."""
    log(f"=== GOG Setup for {game_id} ===")
    log(f"Prefix: {prefix_path}")
    log(f"Install: {install_path}")

    # 1. Load game manifest
    manifest = get_manifest(game_id)
    if not manifest:
        log(f"No manifest for {game_id}, skipping setup")
        return

    # 2. Get dependencies
    deps = get_dependencies(manifest)
    if not deps:
        log(f"No dependencies for {game_id}")
        return

    log(f"Dependencies: {', '.join(deps)}")

    # 3. Ensure redistributables are downloaded
    ensure_redist_downloaded(deps)

    # 4. Run scriptinterpreter if needed (v2 manifests)
    if manifest.get("version") == 2 and manifest.get("scriptInterpreter"):
        log("Manifest requires scriptinterpreter")
        run_script_interpreter(game_id, manifest, prefix_path, install_path)

    # 5. Install redistributables
    redist_manifest = get_redist_manifest()
    if redist_manifest:
        install_redistributables(deps, redist_manifest, prefix_path, install_path)
    else:
        log("No redistributable manifest found - cannot install dependencies")

    log("=== Setup complete ===")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: gog_setup.py <game_id> <prefix_path> <install_path>")
        sys.exit(1)

    game_id = sys.argv[1]
    prefix_path = sys.argv[2]
    install_path = sys.argv[3]

    run_setup(game_id, prefix_path, install_path)
