"""
Proton Compatibility Tool Manager

Handles reading/writing Steam compatibility tool settings from config.vdf
and managing per-game Proton version preferences for the UMU launcher.

Flow:
  1. User sets "Force Compatibility" with a Proton version in Steam's native UI
  2. Frontend interceptor detects this at launch time
  3. Interceptor calls temporarily_clear_compat_tool() to prevent Steam from
     running our bash launcher through Wine
  4. Interceptor saves the tool name via save_proton_setting()
  5. Game re-launches natively, launcher reads proton_settings.json
  6. Interceptor restores the compat tool via restore_compat_tool() (~200ms later)
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Paths
STEAM_DIR = Path.home() / ".steam" / "steam"
CONFIG_VDF = STEAM_DIR / "config" / "config.vdf"
UNIFIDECK_DATA_DIR = Path.home() / ".local" / "share" / "unifideck"
PROTON_SETTINGS_FILE = UNIFIDECK_DATA_DIR / "proton_settings.json"
SHORTCUTS_REGISTRY_FILE = UNIFIDECK_DATA_DIR / "shortcuts_registry.json"

# Steam Linux Runtime tool names - these are Linux containers, not Proton/Wine
LINUX_RUNTIME_PREFIXES = (
    "steamlinuxruntime",
    "scout",
    "sniper",
    "soldier",
)


def is_linux_runtime(tool_name: str) -> bool:
    """Check if a tool name is a Steam Linux Runtime (not Proton).

    Linux Runtimes are container environments that work fine with bash scripts,
    so they don't need the intercept-clear-restore flow.
    """
    lower = tool_name.lower()
    return any(lower.startswith(prefix) or f"_{prefix}" in lower for prefix in LINUX_RUNTIME_PREFIXES)


def _load_shortcuts_registry() -> Dict[str, Dict]:
    """Load shortcuts registry. Returns {launch_options: {appid, appid_unsigned, title, created}}"""
    try:
        if SHORTCUTS_REGISTRY_FILE.exists():
            with open(SHORTCUTS_REGISTRY_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading shortcuts registry: {e}")
    return {}


def _read_config_vdf() -> str:
    """Read config.vdf content. Returns empty string on failure."""
    try:
        if CONFIG_VDF.exists():
            with open(CONFIG_VDF, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        logger.error(f"Error reading config.vdf: {e}")
    return ""


def _write_config_vdf(content: str) -> bool:
    """Write config.vdf content. Returns True on success."""
    try:
        with open(CONFIG_VDF, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.error(f"Error writing config.vdf: {e}")
        return False


def _load_proton_settings() -> Dict[str, Any]:
    """Load proton_settings.json. Returns {"games": {}} on failure."""
    try:
        if PROTON_SETTINGS_FILE.exists():
            with open(PROTON_SETTINGS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading proton_settings.json: {e}")
    return {"games": {}}


def _save_proton_settings(data: Dict[str, Any]) -> bool:
    """Save proton_settings.json. Returns True on success."""
    try:
        PROTON_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PROTON_SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving proton_settings.json: {e}")
        return False


def get_compat_tool_for_app(appid_unsigned: int) -> str:
    """Read the compatibility tool name for a given appID from config.vdf.

    Args:
        appid_unsigned: The unsigned 32-bit shortcut appID

    Returns:
        Tool name (e.g., "proton_9", "GE-Proton9-26") or "" if none set
    """
    content = _read_config_vdf()
    if not content:
        return ""

    appid_str = str(appid_unsigned)

    # Quick check: is this appID even in the file?
    if f'"{appid_str}"' not in content:
        return ""

    # Find the CompatToolMapping section
    marker = '"CompatToolMapping"'
    marker_pos = content.find(marker)
    if marker_pos < 0:
        return ""

    # Search for this appID within a reasonable range after CompatToolMapping
    # Use grep-like approach matching launcher's get_steam_compat_tool()
    search_start = marker_pos
    # Find the app entry after the marker
    app_pattern = re.compile(
        rf'"{appid_str}"\s*\{{([^}}]*)\}}', re.DOTALL
    )

    match = app_pattern.search(content, search_start)
    if not match:
        return ""

    entry_body = match.group(1)
    # Extract "name" value
    name_match = re.search(r'"name"\s+"([^"]*)"', entry_body)
    if name_match:
        return name_match.group(1)

    return ""


def get_compat_tool_for_game(store_game_id: str) -> Dict[str, Any]:
    """Get the compatibility tool for a game by its store:game_id.

    Looks up the shortcut appID from shortcuts_registry.json, then reads
    the compat tool from config.vdf.

    Args:
        store_game_id: e.g., "gog:1234567890"

    Returns:
        {"success": True, "tool_name": "proton_9", "appid_unsigned": 2876543210,
         "is_linux_runtime": False}
        or {"success": False, "error": "..."}
    """
    registry = _load_shortcuts_registry()
    entry = registry.get(store_game_id)

    if not entry:
        return {"success": False, "error": f"Game {store_game_id} not in shortcuts registry"}

    appid_unsigned = entry.get("appid_unsigned")
    if appid_unsigned is None:
        return {"success": False, "error": f"No appid_unsigned for {store_game_id}"}

    tool_name = get_compat_tool_for_app(appid_unsigned)

    return {
        "success": True,
        "tool_name": tool_name,
        "appid_unsigned": appid_unsigned,
        "is_linux_runtime": is_linux_runtime(tool_name) if tool_name else False,
    }


def temporarily_clear_compat_tool(appid_unsigned: int) -> Dict[str, Any]:
    """Temporarily remove a compat tool entry from config.vdf.

    This is called right before re-launching via RunGame to prevent Steam
    from applying Proton to our bash launcher. The entry is restored
    immediately after via restore_compat_tool().

    Args:
        appid_unsigned: The unsigned 32-bit shortcut appID

    Returns:
        {"success": True, "original_tool": "proton_9"}
        or {"success": False, "error": "..."}
    """
    content = _read_config_vdf()
    if not content:
        return {"success": False, "error": "Could not read config.vdf"}

    appid_str = str(appid_unsigned)

    # First, extract the current tool name for the return value
    original_tool = get_compat_tool_for_app(appid_unsigned)
    if not original_tool:
        return {"success": True, "original_tool": ""}  # Nothing to clear

    # Remove the entry: pattern matches "appid" { ... }
    pattern = rf'\s*"{appid_str}"\s*\{{[^}}]*\}}'
    new_content = re.sub(pattern, "", content, count=1)

    if new_content == content:
        logger.warning(f"Could not find/remove compat entry for {appid_str}")
        return {"success": False, "error": f"Could not remove entry for {appid_str}"}

    if not _write_config_vdf(new_content):
        return {"success": False, "error": "Could not write config.vdf"}

    logger.info(f"Temporarily cleared compat tool '{original_tool}' for app {appid_str}")
    return {"success": True, "original_tool": original_tool}


def restore_compat_tool(appid_unsigned: int, tool_name: str) -> Dict[str, Any]:
    """Restore a compat tool entry in config.vdf after a temporary clear.

    Args:
        appid_unsigned: The unsigned 32-bit shortcut appID
        tool_name: The tool name to restore (e.g., "proton_9")

    Returns:
        {"success": True} or {"success": False, "error": "..."}
    """
    if not tool_name:
        return {"success": True}  # Nothing to restore

    content = _read_config_vdf()
    if not content:
        return {"success": False, "error": "Could not read config.vdf"}

    appid_str = str(appid_unsigned)

    # Check if it's already there (e.g., if clear didn't happen or was already restored)
    if f'"{appid_str}"' in content:
        logger.info(f"App {appid_str} already has a compat mapping, skipping restore")
        return {"success": True}

    # Find CompatToolMapping section and insert
    marker = '"CompatToolMapping"'
    if marker not in content:
        return {"success": False, "error": "CompatToolMapping section not found"}

    marker_pos = content.find(marker)
    brace_pos = content.find("{", marker_pos)
    if brace_pos < 0:
        return {"success": False, "error": "Could not find CompatToolMapping opening brace"}

    # Create compat entry with proper VDF indentation (tabs)
    compat_entry = (
        f'\n\t\t\t\t\t"{appid_str}"\n'
        f"\t\t\t\t\t{{\n"
        f'\t\t\t\t\t\t"name"\t\t"{tool_name}"\n'
        f'\t\t\t\t\t\t"config"\t\t""\n'
        f'\t\t\t\t\t\t"priority"\t\t"250"\n'
        f"\t\t\t\t\t}}"
    )

    new_content = content[: brace_pos + 1] + compat_entry + content[brace_pos + 1 :]

    if not _write_config_vdf(new_content):
        return {"success": False, "error": "Could not write config.vdf"}

    logger.info(f"Restored compat tool '{tool_name}' for app {appid_str}")
    return {"success": True}


def save_proton_setting(store_game_id: str, tool_name: str) -> Dict[str, Any]:
    """Save the proton tool preference for a game.

    This persists the user's Steam Compatibility selection so the launcher
    can read it at Priority 2.5 (between env vars and config.vdf direct read).

    Args:
        store_game_id: e.g., "gog:1234567890"
        tool_name: The tool name (e.g., "proton_9") or "" to clear

    Returns:
        {"success": True} or {"success": False, "error": "..."}
    """
    settings = _load_proton_settings()

    if "games" not in settings:
        settings["games"] = {}

    if tool_name:
        settings["games"][store_game_id] = {"proton_tool": tool_name}
    else:
        # Clear the setting
        settings["games"].pop(store_game_id, None)

    if _save_proton_settings(settings):
        logger.info(f"Saved proton setting for {store_game_id}: {tool_name or '(cleared)'}")
        return {"success": True}

    return {"success": False, "error": "Could not save proton_settings.json"}


def get_saved_proton_tool(store_game_id: str) -> str:
    """Get the saved proton tool for a game from proton_settings.json.

    Args:
        store_game_id: e.g., "gog:1234567890"

    Returns:
        Tool name or "" if none saved
    """
    settings = _load_proton_settings()
    game_entry = settings.get("games", {}).get(store_game_id, {})
    return game_entry.get("proton_tool", "")
