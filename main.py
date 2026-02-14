import decky  # Required for Decky Loader framework
import os
import sys
import logging
import asyncio
import binascii
import struct
import json
import aiohttp.web
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from urllib.parse import parse_qs

# Add plugin directory to Python path for local imports
DECKY_PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR")
if DECKY_PLUGIN_DIR:
    sys.path.insert(0, DECKY_PLUGIN_DIR)
    # Also add py_modules directory so submodules can import bundled packages (e.g., steamgrid)
    py_modules_path = os.path.join(DECKY_PLUGIN_DIR, "py_modules")
    if os.path.isdir(py_modules_path):
        sys.path.insert(0, py_modules_path)

# Import VDF utilities
from py_modules.unifideck.shortcuts.vdf import load_shortcuts_vdf, save_shortcuts_vdf
from py_modules.unifideck.shortcuts.shortcuts_manager import (
    ShortcutsManager, load_shortcuts_registry, save_shortcuts_registry,
    register_shortcut, get_registered_appid,
    _invalidate_games_map_mem_cache, _load_games_map_cached, GAMES_MAP_PATH,
    SHORTCUTS_REGISTRY_FILE, get_shortcuts_registry_path
)

# Import Steam user detection utilities
from py_modules.unifideck.steam.steam_utils import get_logged_in_steam_user, migrate_user0_to_logged_in_user

# Import SteamGridDB client
try:
    from py_modules.unifideck.steam.steamgriddb import SteamGridDBClient
    STEAMGRIDDB_AVAILABLE = True
except ImportError:
    STEAMGRIDDB_AVAILABLE = False

# Import Download Manager (modular backend)
from py_modules.unifideck.download.manager import get_download_queue, DownloadQueue

# Import Cloud Save Manager
from py_modules.unifideck.cloud.cloud_save import CloudSaveManager

# Import resilient launch options parser
from py_modules.unifideck.shortcuts.launch_options import extract_store_id, is_unifideck_shortcut, get_full_id, get_store_prefix

# Import CDP modules for native PlaySection hiding
from py_modules.unifideck.cdp.cdp_utils import create_cef_debugging_flag
from py_modules.unifideck.cdp.cdp_inject import get_cdp_client, shutdown_cdp_client

# Import Account Manager for multi-account support
from py_modules.unifideck.accounts.account_manager import AccountManager

# ============================================================================
# NEW MODULAR BACKEND IMPORTS (Phase 1: Available for use alongside old code)
# These will eventually replace the inline class definitions below.
# ============================================================================
try:
    from py_modules.unifideck.stores import (
        Store, Game as BackendGame, StoreManager,
        EpicConnector as BackendEpicConnector,
        AmazonConnector as BackendAmazonConnector,
        GOGAPIClient as BackendGOGAPIClient
    )
    from py_modules.unifideck.auth import CDPOAuthMonitor as BackendCDPOAuthMonitor
    from py_modules.unifideck.compat import (
        BackgroundCompatFetcher as BackendCompatFetcher,
        load_compat_cache, save_compat_cache, prefetch_compat
    )
    BACKEND_AVAILABLE = True
except ImportError as e:
    BACKEND_AVAILABLE = False
    # Will fall back to inline implementations

# Use Decky's logger for proper integration
logger = decky.logger

# Log import status
if not STEAMGRIDDB_AVAILABLE:
    logger.warning("SteamGridDB client not available")
if BACKEND_AVAILABLE:
    logger.info("Modular backend package loaded successfully")


# Global caches for legendary CLI results (performance optimization)
import time
import re

_legendary_installed_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 30  # 30 second cache
}

_legendary_info_cache = {}  # Per-game info cache

# Artwork sync timeout (seconds per game)
ARTWORK_FETCH_TIMEOUT = 90


# ============================================================================
# CDPOAuthMonitor - Now imported from py_modules.unifideck.auth module
# ============================================================================
if BACKEND_AVAILABLE:
    CDPOAuthMonitor = BackendCDPOAuthMonitor
    logger.info("Using CDPOAuthMonitor from py_modules.unifideck.auth module")
else:
    # Fallback: Define inline if backend not available (should not happen in production)
    logger.warning("Backend not available, CDPOAuthMonitor would need inline definition")
    # The inline class was here but has been removed - backend should always be available
    raise ImportError("backend.auth module is required but not available")


# ============================================================================
# Game dataclass - Now imported from py_modules.unifideck.stores.base module
# ============================================================================
if BACKEND_AVAILABLE:
    Game = BackendGame
else:
    raise ImportError("backend.stores module is required but not available")


# Steam App ID Cache - maps shortcut appId to SteamGridDB ID for artwork lookups
# Stored as JSON file in plugin data directory
STEAM_APPID_CACHE_FILE = "steam_appid_cache.json"

# Real Steam App ID Cache - maps shortcut appId to real Steam appId for store/community links
STEAM_REAL_APPID_CACHE_FILE = "steam_real_appid_cache.json"


def _backup_cache_file(cache_path: Path) -> bool:
    """Create a backup of a cache file before overwriting.
    
    Backups are stored as .bak files and can be restored if sync fails.
    """
    try:
        if cache_path.exists():
            backup_path = cache_path.with_suffix(cache_path.suffix + '.bak')
            import shutil
            shutil.copy2(cache_path, backup_path)
            logger.debug(f"Backed up {cache_path.name} to {backup_path.name}")
            return True
    except Exception as e:
        logger.warning(f"Failed to backup {cache_path.name}: {e}")
    return False


def _restore_cache_backup(cache_path: Path) -> bool:
    """Restore a cache file from backup if it exists."""
    try:
        backup_path = cache_path.with_suffix(cache_path.suffix + '.bak')
        if backup_path.exists():
            import shutil
            shutil.copy2(backup_path, cache_path)
            logger.info(f"Restored {cache_path.name} from backup")
            return True
    except Exception as e:
        logger.error(f"Failed to restore {cache_path.name} from backup: {e}")
    return False


def get_steam_appid_cache_path() -> Path:
    """Get path to steam_app_id cache file (in user data, not plugin dir)"""
    return Path.home() / ".local" / "share" / "unifideck" / STEAM_APPID_CACHE_FILE


def load_steam_appid_cache() -> Dict[int, int]:
    """Load steam_app_id mappings from cache file. Returns {shortcut_appid: steam_appid}"""
    cache_path = get_steam_appid_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                data = json.load(f)
                # Convert string keys back to int
                return {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.error(f"Error loading steam_appid cache: {e}")
    return {}


def save_steam_appid_cache(cache: Dict[int, int]) -> bool:
    """Save steam_app_id mappings to cache file"""
    cache_path = get_steam_appid_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.info(f"Saved {len(cache)} steam_app_id mappings to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving steam_appid cache: {e}")
        return False


def get_steam_real_appid_cache_path() -> Path:
    """Get path to real Steam app_id cache file"""
    return Path.home() / ".local" / "share" / "unifideck" / STEAM_REAL_APPID_CACHE_FILE


def load_steam_real_appid_cache() -> Dict[int, int]:
    """Load real Steam app_id mappings. Returns {shortcut_appid: steam_appid}"""
    cache_path = get_steam_real_appid_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                data = json.load(f)
                return {int(k): int(v) for k, v in data.items()}
    except Exception as e:
        logger.error(f"Error loading real steam appid cache: {e}")
    return {}


def save_steam_real_appid_cache(cache: Dict[int, int]) -> bool:
    """Save real Steam app_id mappings"""
    cache_path = get_steam_real_appid_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.info(f"Saved {len(cache)} real steam app_id mappings to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving real steam appid cache: {e}")
        return False


# Steam Metadata Cache - stores Steam API game details for store patching
STEAM_METADATA_CACHE_FILE = "steam_metadata_cache.json"


def get_steam_metadata_cache_path() -> Path:
    """Get path to Steam metadata cache file"""
    return Path.home() / ".local" / "share" / "unifideck" / STEAM_METADATA_CACHE_FILE


def load_steam_metadata_cache() -> Dict[int, Dict]:
    """Load Steam metadata cache. Returns {steam_appid: metadata_dict}"""
    cache_path = get_steam_metadata_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.error(f"Error loading steam metadata cache: {e}")
    return {}


def save_steam_metadata_cache(cache: Dict[int, Dict]) -> bool:
    """Save Steam metadata cache"""
    cache_path = get_steam_metadata_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.info(f"Saved {len(cache)} Steam metadata entries to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving steam metadata cache: {e}")
        return False


# LEGACY: RAWG Metadata Cache - kept for cleanup and migration purposes only
# New metadata comes from unifiDB and Metacritic caches
RAWG_METADATA_CACHE_FILE = "rawg_metadata_cache.json"


def get_rawg_metadata_cache_path() -> Path:
    """Get path to legacy RAWG metadata cache file (for cleanup only)"""
    return Path.home() / ".local" / "share" / "unifideck" / RAWG_METADATA_CACHE_FILE


def load_rawg_metadata_cache() -> Dict[str, Dict]:
    """Load legacy RAWG metadata cache (for migration only)"""
    cache_path = get_rawg_metadata_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading legacy RAWG metadata cache: {e}")
    return {}


def save_rawg_metadata_cache(cache: Dict[str, Dict]) -> bool:
    """Save legacy RAWG metadata cache (for migration only)"""
    cache_path = get_rawg_metadata_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.info(f"Saved {len(cache)} legacy RAWG metadata entries to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving legacy RAWG metadata cache: {e}")
        return False


# unifiDB Metadata Cache - stores IGDB API results keyed by game title (lowercase)
UNIFIDB_METADATA_CACHE_FILE = "unifidb_metadata_cache.json"


def get_unifidb_metadata_cache_path() -> Path:
    """Get path to unifiDB metadata cache file"""
    return Path.home() / ".local" / "share" / "unifideck" / UNIFIDB_METADATA_CACHE_FILE


def load_unifidb_metadata_cache() -> Dict[str, Dict]:
    """Load unifiDB metadata cache. Returns {lowercase_title: unifidb_data_dict}"""
    cache_path = get_unifidb_metadata_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading unifiDB metadata cache: {e}")
    return {}


def save_unifidb_metadata_cache(cache: Dict[str, Dict]) -> bool:
    """Save unifiDB metadata cache"""
    cache_path = get_unifidb_metadata_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.info(f"Saved {len(cache)} unifiDB metadata entries to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving unifiDB metadata cache: {e}")
        return False


# Metacritic Metadata Cache - stores Metacritic scores/data keyed by game title (lowercase)
METACRITIC_METADATA_CACHE_FILE = "metacritic_metadata_cache.json"


def get_metacritic_metadata_cache_path() -> Path:
    """Get path to Metacritic metadata cache file"""
    return Path.home() / ".local" / "share" / "unifideck" / METACRITIC_METADATA_CACHE_FILE


def load_metacritic_metadata_cache() -> Dict[str, Dict]:
    """Load Metacritic metadata cache. Returns {lowercase_title: metacritic_data_dict}"""
    cache_path = get_metacritic_metadata_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading Metacritic metadata cache: {e}")
    return {}


def save_metacritic_metadata_cache(cache: Dict[str, Dict]) -> bool:
    """Save Metacritic metadata cache"""
    cache_path = get_metacritic_metadata_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.info(f"Saved {len(cache)} Metacritic metadata entries to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving Metacritic metadata cache: {e}")
        return False


# Artwork Attempts Cache - tracks which games have had artwork fetch attempted
# Maps str(app_id) -> bool (True=has artwork, False=no artwork available)
ARTWORK_ATTEMPTS_CACHE_FILE = "artwork_attempts_cache.json"


def get_artwork_attempts_cache_path() -> Path:
    """Get path to artwork attempts cache file"""
    return Path.home() / ".local" / "share" / "unifideck" / ARTWORK_ATTEMPTS_CACHE_FILE


def load_artwork_attempts_cache() -> Dict[str, bool]:
    """Load artwork attempts cache. Returns {str(app_id): bool}"""
    cache_path = get_artwork_attempts_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading artwork attempts cache: {e}")
    return {}


def save_artwork_attempts_cache(cache: Dict[str, bool]) -> bool:
    """Save artwork attempts cache"""
    cache_path = get_artwork_attempts_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.debug(f"Saved {len(cache)} artwork attempt entries to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving artwork attempts cache: {e}")
        return False


def sanitize_description(text: str, max_length: int = 1000) -> str:
    """Clean up RAWG/Steam descriptions for display.

    Strips markdown headers, HTML tags, fixes missing spaces, and normalizes whitespace.
    """
    if not text:
        return ''
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Fix markdown headers with missing space (e.g. "###Plot" -> "Plot")
    # Also strips the header markers entirely since we just want plain text
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    # Remove leftover markdown bold/italic markers
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    # Fix sentences joined without space (e.g. "end.Start" -> "end. Start")
    text = re.sub(r'([.!?])([A-Z])', r'\1 \2', text)
    # Normalize whitespace: collapse multiple spaces/newlines into single space
    text = re.sub(r'\s+', ' ', text).strip()
    # Truncate to max length
    if len(text) > max_length:
        text = text[:max_length].rsplit(' ', 1)[0] + '...'
    return text


def normalize_release_date(value: Any) -> str:
    """Normalize release date to YYYY-MM-DD when possible."""
    if value is None:
        return ''

    if isinstance(value, (int, float)):
        try:
            ts = int(value)
            if ts > 10**12:  # milliseconds
                ts = ts // 1000
            return datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
        except Exception:
            return ''

    value_str = str(value).strip()
    if not value_str:
        return ''

    if re.match(r'^\d{4}-\d{2}-\d{2}$', value_str):
        return value_str

    if value_str.isdigit():
        try:
            ts = int(value_str)
            if ts > 10**12:
                ts = ts // 1000
            return datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
        except Exception:
            return ''

    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value_str, fmt).strftime('%Y-%m-%d')
        except Exception:
            continue

    for fmt in ("%b %Y", "%B %Y"):
        try:
            parsed = datetime.strptime(value_str, fmt)
            return parsed.replace(day=1).strftime('%Y-%m-%d')
        except Exception:
            continue

    if re.match(r'^\d{4}$', value_str):
        return f"{value_str}-01-01"

    return ''


def read_steam_appinfo_vdf() -> Dict[int, Dict]:
    """
    Read and parse Steam's appinfo.vdf binary cache file (supports v27-v29).
    Steam maintains this file automatically - we just read it.

    Returns {app_id: sections_dict} for all apps.
    """
    appinfo_path = Path.home() / ".steam" / "steam" / "appcache" / "appinfo.vdf"
    if not appinfo_path.exists():
        appinfo_path = Path.home() / ".local" / "share" / "Steam" / "appcache" / "appinfo.vdf"
    if not appinfo_path.exists():
        logger.warning("Steam appinfo.vdf not found")
        return {}

    try:
        with open(appinfo_path, 'rb') as f:
            data = f.read()

        magic = struct.unpack_from('<I', data, 0)[0]
        logger.info(f"Reading Steam appinfo.vdf from {appinfo_path} ({len(data)} bytes, version 0x{magic:08x})")

        if magic == 0x07564429:
            return _parse_appinfo_v29(data)
        elif magic in (0x07564427, 0x07564428):
            return _parse_appinfo_v27(data)
        else:
            logger.error(f"Unsupported appinfo.vdf version: 0x{magic:08x}")
            return {}
    except Exception as e:
        logger.error(f"Failed to read appinfo.vdf: {e}")
        return {}


def _parse_appinfo_v29(data: bytes) -> Dict[int, Dict]:
    """Parse appinfo.vdf v29 format (string table + indexed keys)."""
    string_table_offset = struct.unpack_from('<Q', data, 8)[0]

    # Parse string table (at end of file)
    st_offset = string_table_offset
    string_count = struct.unpack_from('<I', data, st_offset)[0]
    st_offset += 4
    strings = []
    for _ in range(string_count):
        end = data.index(b'\x00', st_offset)
        strings.append(data[st_offset:end].decode('utf-8', errors='replace'))
        st_offset = end + 1

    # Parse app entries (start at offset 16)
    result = {}
    offset = 16
    while offset < string_table_offset:
        app_id = struct.unpack_from('<I', data, offset)[0]
        if app_id == 0:
            break
        offset += 4
        # App header: size(4)+state(4)+last_update(4)+access_token(8)+sha1(20)+change_number(4)+sha1_binary(20) = 64
        offset += 64
        sections, offset = _parse_vdf_sections_indexed(data, offset, strings)
        result[app_id] = sections

    logger.info(f"Parsed {len(result)} apps from appinfo.vdf v29")
    return result


def _parse_appinfo_v27(data: bytes) -> Dict[int, Dict]:
    """Parse appinfo.vdf v27/v28 format (inline string keys)."""
    result = {}
    offset = 8
    while True:
        app_id = struct.unpack_from('<I', data, offset)[0]
        if app_id == 0:
            break
        offset += 4
        # App header: size(4)+state(4)+last_update(4)+access_token(8)+sha1(20)+change_number(4) = 44
        offset += 44
        sections, offset = _parse_vdf_sections_inline(data, offset)
        result[app_id] = sections

    logger.info(f"Parsed {len(result)} apps from appinfo.vdf v27/v28")
    return result


def _parse_vdf_sections_indexed(data: bytes, offset: int, strings: list) -> tuple:
    """Parse binary VDF with string-table-indexed keys (v29)."""
    result = {}
    while offset < len(data):
        type_byte = data[offset]
        offset += 1
        if type_byte == 0x08:  # SECTION_END
            break
        key_idx = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        key = strings[key_idx] if key_idx < len(strings) else f'_unknown_{key_idx}'

        if type_byte == 0x00:  # SECTION
            val, offset = _parse_vdf_sections_indexed(data, offset, strings)
            result[key] = val
        elif type_byte == 0x01:  # STRING
            end = data.index(b'\x00', offset)
            result[key] = data[offset:end].decode('utf-8', errors='replace')
            offset = end + 1
        elif type_byte == 0x02:  # INT32
            result[key] = struct.unpack_from('<I', data, offset)[0]
            offset += 4
        elif type_byte == 0x07:  # INT64
            result[key] = struct.unpack_from('<Q', data, offset)[0]
            offset += 8
    return result, offset


def _parse_vdf_sections_inline(data: bytes, offset: int) -> tuple:
    """Parse binary VDF with inline string keys (v27/v28)."""
    result = {}
    while offset < len(data):
        type_byte = data[offset]
        offset += 1
        if type_byte == 0x08:  # SECTION_END
            break
        end = data.index(b'\x00', offset)
        key = data[offset:end].decode('utf-8', errors='replace')
        offset = end + 1

        if type_byte == 0x00:  # SECTION
            val, offset = _parse_vdf_sections_inline(data, offset)
            result[key] = val
        elif type_byte == 0x01:  # STRING
            end = data.index(b'\x00', offset)
            result[key] = data[offset:end].decode('utf-8', errors='replace')
            offset = end + 1
        elif type_byte == 0x02:  # INT32
            result[key] = struct.unpack_from('<I', data, offset)[0]
            offset += 4
        elif type_byte == 0x07:  # INT64
            result[key] = struct.unpack_from('<Q', data, offset)[0]
            offset += 8
    return result, offset


# ============================================================================
# appinfo.vdf v29 ENCODER (for writing/injecting games)
# ============================================================================

def write_steam_appinfo_vdf(apps_data: Dict[int, Dict]) -> bool:
    """
    Write apps data to Steam's appinfo.vdf in v29 format.
    Returns True if successful.
    """
    import shutil
    import time as time_module

    appinfo_path = Path.home() / ".steam" / "steam" / "appcache" / "appinfo.vdf"
    if not appinfo_path.exists():
        appinfo_path = Path.home() / ".local" / "share" / "Steam" / "appcache" / "appinfo.vdf"

    if not appinfo_path.exists():
        logger.error("Steam appinfo.vdf not found for writing")
        return False

    # Create backup on first write
    backup_path = appinfo_path.with_suffix('.vdf.unifideck_backup')
    if not backup_path.exists():
        shutil.copy2(appinfo_path, backup_path)
        logger.info(f"Created appinfo.vdf backup: {backup_path}")

    try:
        # Encode to bytes
        data = _encode_appinfo_v29(apps_data, time_module)

        # Write atomically (write to temp, then rename)
        temp_path = appinfo_path.with_suffix('.vdf.tmp')
        with open(temp_path, 'wb') as f:
            f.write(data)
        temp_path.replace(appinfo_path)

        logger.info(f"Wrote {len(apps_data)} apps to appinfo.vdf ({len(data)} bytes)")
        return True
    except Exception as e:
        logger.error(f"Failed to write appinfo.vdf: {e}")
        import traceback
        traceback.print_exc()
        return False


def _dict_to_text_vdf(data: Dict, tabs: int = 0) -> bytes:
    """Convert dict to text VDF format for checksum calculation.

    Steam requires a text VDF checksum that matches a specific format.
    This replicates the format used by Steam's internal VDF serialization.
    """
    output = b""
    tab_str = b"\t" * tabs

    for key, value in data.items():
        if isinstance(key, bytes):
            continue  # Skip internal keys
        key_bytes = str(key).replace("\\", "\\\\").encode()

        if isinstance(value, dict):
            output += tab_str + b'"' + key_bytes + b'"\n'
            output += tab_str + b"{\n"
            output += _dict_to_text_vdf(value, tabs + 1)
            output += tab_str + b"}\n"
        else:
            val_bytes = str(value).replace("\\", "\\\\").encode()
            output += tab_str + b'"' + key_bytes + b'"\t\t"' + val_bytes + b'"\n'

    return output


def _get_text_checksum(sections: Dict) -> bytes:
    """Calculate SHA1 of text VDF representation (required by Steam v29 format)."""
    import hashlib
    text_vdf = _dict_to_text_vdf(sections)
    return hashlib.sha1(text_vdf).digest()


def _encode_appinfo_v29(apps_data: Dict[int, Dict], time_module) -> bytes:
    """Encode apps data to v29 binary format."""
    import hashlib
    import io

    # Build string table from all keys (collect unique strings)
    strings_set = set()

    def collect_strings(d):
        for k, v in d.items():
            if isinstance(k, str):
                strings_set.add(k)
            if isinstance(v, dict):
                collect_strings(v)

    for app_data in apps_data.values():
        collect_strings(app_data)

    strings = sorted(strings_set)
    string_to_idx = {s: i for i, s in enumerate(strings)}

    # Encode app entries
    apps_buf = io.BytesIO()
    for app_id, sections in sorted(apps_data.items()):
        if isinstance(app_id, bytes):
            continue  # Skip internal keys like b'__vdf_version'

        # App ID (4 bytes)
        apps_buf.write(struct.pack('<I', app_id))

        # Encode binary VDF sections first to calculate checksums
        sections_bytes = _encode_vdf_sections_indexed(sections, string_to_idx)

        # Calculate BOTH checksums (Steam v29 requires both!)
        checksum_text = _get_text_checksum(sections)  # SHA1 of text VDF representation
        checksum_binary = hashlib.sha1(sections_bytes).digest()  # SHA1 of binary sections

        # Size = rest of header (60 bytes after appid+size) + sections
        # Header after appid+size: state(4) + last_update(4) + access_token(8) +
        #                          checksum_text(20) + change_number(4) + checksum_binary(20) = 60 bytes
        size = 60 + len(sections_bytes)

        # Write app header
        apps_buf.write(struct.pack('<I', size))  # size (includes header after appid+size)
        apps_buf.write(struct.pack('<I', 2))  # state (2 = available)
        apps_buf.write(struct.pack('<I', int(time_module.time())))  # last_update
        apps_buf.write(struct.pack('<Q', 0))  # access_token
        apps_buf.write(checksum_text)  # SHA1 of text VDF (required!)
        apps_buf.write(struct.pack('<I', 1))  # change_number
        apps_buf.write(checksum_binary)  # SHA1 of binary VDF sections

        # Binary VDF sections
        apps_buf.write(sections_bytes)

    # End marker (app_id = 0)
    apps_buf.write(struct.pack('<I', 0))

    # Build string table
    strings_buf = io.BytesIO()
    strings_buf.write(struct.pack('<I', len(strings)))
    for s in strings:
        strings_buf.write(s.encode('utf-8', errors='replace'))
        strings_buf.write(b'\x00')

    # Calculate string table offset (header + apps)
    string_table_offset = 16 + apps_buf.tell()

    # Combine: header(16) + apps + string_table
    output = io.BytesIO()
    output.write(struct.pack('<I', 0x07564429))  # magic (v29)
    output.write(struct.pack('<I', 1))  # universe
    output.write(struct.pack('<Q', string_table_offset))  # string table offset
    output.write(apps_buf.getvalue())
    output.write(strings_buf.getvalue())

    return output.getvalue()


def _encode_vdf_sections_indexed(sections: Dict, string_to_idx: Dict) -> bytes:
    """Encode VDF sections using string-table-indexed keys (v29 format)."""
    import io
    buf = io.BytesIO()

    for key, value in sections.items():
        if isinstance(key, bytes):
            continue  # Skip internal keys

        key_idx = string_to_idx.get(key, 0)

        if isinstance(value, dict):
            buf.write(struct.pack('<B', 0x00))  # TYPE_SECTION
            buf.write(struct.pack('<I', key_idx))
            buf.write(_encode_vdf_sections_indexed(value, string_to_idx))
        elif isinstance(value, str):
            buf.write(struct.pack('<B', 0x01))  # TYPE_STRING
            buf.write(struct.pack('<I', key_idx))
            buf.write(value.encode('utf-8', errors='replace'))
            buf.write(b'\x00')
        elif isinstance(value, int):
            if value > 0xFFFFFFFF or value < 0:
                buf.write(struct.pack('<B', 0x07))  # TYPE_INT64
                buf.write(struct.pack('<I', key_idx))
                buf.write(struct.pack('<Q', value & 0xFFFFFFFFFFFFFFFF))
            else:
                buf.write(struct.pack('<B', 0x02))  # TYPE_INT32
                buf.write(struct.pack('<I', key_idx))
                buf.write(struct.pack('<I', value))

    buf.write(struct.pack('<B', 0x08))  # SECTION_END
    return buf.getvalue()


# ============================================================================
# Single Game Injection (on-demand)
# ============================================================================

def inject_single_game_to_appinfo(shortcut_app_id: int) -> bool:
    """
    Inject a single Unifideck game into Steam's appinfo.vdf.
    Called when user opens game details view for a shortcut.

    IMPORTANT: We inject using the SHORTCUT's app ID (converted to unsigned),
    not the real Steam app ID. This way when Steam looks up data for the shortcut,
    it finds our injected metadata.

    Args:
        shortcut_app_id: The shortcut's app ID (negative number like -1404125384)

    Returns:
        True if injection successful or game already exists
    """
    try:
        # Convert shortcut ID to unsigned 32-bit (how Steam stores it internally)
        # Example: -1404125384 -> 2890841912
        unsigned_shortcut_id = shortcut_app_id & 0xFFFFFFFF
        logger.info(f"Injection request: shortcut {shortcut_app_id} -> unsigned {unsigned_shortcut_id}")

        # Load mappings to get real Steam App ID (for metadata lookup)
        steam_appid_cache = load_steam_real_appid_cache()
        steam_app_id = steam_appid_cache.get(shortcut_app_id)

        if not steam_app_id:
            logger.debug(f"No Steam App ID mapping for shortcut {shortcut_app_id}")
            return False

        # Read existing appinfo.vdf
        existing_apps = read_steam_appinfo_vdf()
        if not existing_apps:
            logger.warning("Could not read appinfo.vdf, skipping injection")
            return False

        # Check if shortcut is already in appinfo.vdf (using unsigned ID)
        if unsigned_shortcut_id in existing_apps:
            logger.debug(f"Shortcut {shortcut_app_id} (unsigned: {unsigned_shortcut_id}) already in appinfo.vdf")
            return True  # Already exists, no need to inject

        # Load metadata using the REAL Steam app ID
        metadata_cache = load_steam_metadata_cache()
        metadata = metadata_cache.get(steam_app_id)

        if not metadata:
            logger.warning(f"No metadata cached for Steam App {steam_app_id}")
            return False

        # Build appinfo entry using the SHORTCUT's unsigned ID
        # This way Steam finds it when looking up the shortcut
        app_entry = _build_appinfo_entry(unsigned_shortcut_id, metadata)

        # Add to existing apps using shortcut's unsigned ID and write back
        existing_apps[unsigned_shortcut_id] = app_entry
        success = write_steam_appinfo_vdf(existing_apps)

        if success:
            logger.info(f"Injected shortcut {shortcut_app_id} (unsigned: {unsigned_shortcut_id}) with metadata from Steam App {steam_app_id} ({metadata.get('name', '?')})")

        return success

    except Exception as e:
        logger.error(f"Failed to inject game {shortcut_app_id} to appinfo.vdf: {e}")
        import traceback
        traceback.print_exc()
        return False


def _build_appinfo_entry(steam_app_id: int, metadata: Dict) -> Dict:
    """Build an appinfo.vdf entry from cached metadata."""
    platforms = metadata.get('platforms', {})
    os_list = []
    if platforms.get('windows'):
        os_list.append('windows')
    if platforms.get('mac'):
        os_list.append('macos')
    if platforms.get('linux'):
        os_list.append('linux')

    developers = metadata.get('developers', [])
    publishers = metadata.get('publishers', [])

    return {
        'appinfo': {
            'appid': steam_app_id,
            'common': {
                'name': metadata.get('name', 'Unknown'),
                'type': 'game',
                'oslist': ','.join(os_list) if os_list else 'windows',
                'controller_support': metadata.get('controller_support', 'none'),
                'metacritic_score': metadata.get('metacritic', {}).get('score', 0),
            },
            'extended': {
                'developer': ', '.join(developers) if developers else '',
                'publisher': ', '.join(publishers) if publishers else '',
                'homepage': metadata.get('website') or '',
            }
        }
    }


def convert_appinfo_to_web_api_format(app_id: int, appinfo: Dict) -> Dict:
    """Convert appinfo.vdf format to Steam web API format for compatibility with frontend."""
    try:
        common = appinfo.get('appinfo', {}).get('common', {})
        extended = appinfo.get('appinfo', {}).get('extended', {})

        # Extract developer/publisher (can be string or list)
        developer = extended.get('developer', '')
        developers = developer.split(',') if isinstance(developer, str) and developer else (developer if isinstance(developer, list) else [])

        publisher = extended.get('publisher', '')
        publishers = publisher.split(',') if isinstance(publisher, str) and publisher else (publisher if isinstance(publisher, list) else [])

        return {
            'type': common.get('type', 'game'),
            'name': common.get('name', ''),
            'steam_appid': app_id,
            'required_age': common.get('required_age', 0),
            'is_free': common.get('is_free', False),
            'controller_support': common.get('controller_support', 'none'),
            'detailed_description': extended.get('description', ''),
            'short_description': common.get('short_description', ''),
            'supported_languages': common.get('languages', ''),
            'header_image': common.get('header_image', {}).get('english') if isinstance(common.get('header_image'), dict) else common.get('header_image', ''),
            'capsule_image': common.get('library_assets', {}).get('library_capsule', '') if isinstance(common.get('library_assets'), dict) else '',
            'website': extended.get('homepage', ''),
            'developers': [d.strip() for d in developers if d.strip()],
            'publishers': [p.strip() for p in publishers if p.strip()],
            'platforms': {
                'windows': 'oslist' in common and 'windows' in str(common.get('oslist', '')).lower(),
                'mac': 'oslist' in common and 'macos' in str(common.get('oslist', '')).lower(),
                'linux': 'oslist' in common and 'linux' in str(common.get('oslist', '')).lower(),
            },
            'metacritic': {'score': common.get('metacritic_score', 0)},
            'categories': common.get('category', {}) if isinstance(common.get('category'), dict) else [],
            'genres': common.get('genre', {}) if isinstance(common.get('genre'), dict) else [],
            'release_date': {
                'coming_soon': False,
                'date': str(common.get('steam_release_date', ''))
            },
        }
    except Exception as e:
        logger.error(f"Error converting appinfo for {app_id}: {e}")
        return {}


def normalize_title_for_matching(title: str) -> str:
    """Normalize a game title for fuzzy matching.

    Strips subtitles (after -/:), removes common suffixes, lowercases, removes punctuation.
    """
    if not title:
        return ''
    t = title.lower().strip()
    # Remove subtitles after common separators
    for sep in [' - ', ': ', ' – ']:
        if sep in t:
            t = t.split(sep)[0].strip()
    # Remove common edition suffixes
    for suffix in [
        'the final cut', 'definitive edition', 'complete edition', 'goty edition',
        'game of the year edition', 'enhanced edition', 'remastered', 'deluxe edition',
        'ultimate edition', 'gold edition', 'special edition', 'anniversary edition',
        'directors cut', "director's cut", 'legacy edition'
    ]:
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
    # Remove punctuation and extra whitespace
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


async def extract_metadata_from_appinfo(games: List, appinfo_data: Dict[int, Dict]) -> Tuple[Dict[int, int], Dict[int, Dict]]:
    """
    Extract metadata for our games from appinfo data by matching titles.

    Returns:
        Tuple of (shortcut_appid_to_steam_appid mapping, steam_appid_to_metadata mapping)
    """
    appid_mapping = {}  # shortcut_appid -> steam_appid
    metadata_results = {}  # steam_appid -> metadata (converted to web API format)

    # Pre-build normalized lookup for appinfo titles
    normalized_appinfo = {}  # normalized_name -> (app_id, original_name)
    for app_id, app_data in appinfo_data.items():
        try:
            app_common = app_data.get('appinfo', {}).get('common', {})
            app_name = app_common.get('name', '')
            if app_name:
                norm = normalize_title_for_matching(app_name)
                if norm:
                    normalized_appinfo[norm] = (app_id, app_name.lower().strip())
        except:
            continue

    for game in games:
        if not game.app_id or not game.title:
            continue

        try:
            # Search for Steam App ID by title in appinfo data
            search_lower = game.title.lower().strip()
            search_normalized = normalize_title_for_matching(game.title)
            steam_app_id = None

            # Pass 1: Exact match on original title
            for app_id, app_data in appinfo_data.items():
                try:
                    app_common = app_data.get('appinfo', {}).get('common', {})
                    app_name = app_common.get('name', '').lower().strip()

                    if app_name == search_lower:
                        steam_app_id = app_id
                        break
                except:
                    continue

            # Pass 2: Fuzzy match on normalized title
            if not steam_app_id and search_normalized:
                match = normalized_appinfo.get(search_normalized)
                if match:
                    steam_app_id = match[0]
                    logger.debug(f"Fuzzy matched '{game.title}' -> Steam ID {steam_app_id}")

            if not steam_app_id:
                continue

            appid_mapping[game.app_id] = steam_app_id

            # Convert appinfo data to web API format
            if steam_app_id not in metadata_results:
                converted = convert_appinfo_to_web_api_format(steam_app_id, appinfo_data[steam_app_id])
                if not converted:
                    try:
                        app_common = appinfo_data.get(steam_app_id, {}).get('appinfo', {}).get('common', {})
                        app_extended = appinfo_data.get(steam_app_id, {}).get('appinfo', {}).get('extended', {})
                        fallback_name = app_common.get('name') or game.title
                        fallback_type = app_common.get('type', 'game')
                        developer = app_extended.get('developer', '')
                        developers = developer.split(',') if isinstance(developer, str) and developer else (developer if isinstance(developer, list) else [])
                        publisher = app_extended.get('publisher', '')
                        publishers = publisher.split(',') if isinstance(publisher, str) and publisher else (publisher if isinstance(publisher, list) else [])

                        if fallback_name:
                            converted = {
                                'type': fallback_type,
                                'name': fallback_name,
                                'steam_appid': steam_app_id,
                                'short_description': app_common.get('short_description', ''),
                                'developers': [d.strip() for d in developers if d.strip()],
                                'publishers': [p.strip() for p in publishers if p.strip()],
                                'release_date': {
                                    'coming_soon': False,
                                    'date': str(app_common.get('steam_release_date', ''))
                                }
                            }
                    except Exception:
                        converted = {}

                if converted:
                    metadata_results[steam_app_id] = converted
                    logger.debug(f"Extracted metadata for '{game.title}' (Steam ID: {steam_app_id})")

        except Exception as e:
            logger.debug(f"Failed to extract metadata for '{game.title}': {e}")

    return appid_mapping, metadata_results




# Game Size Cache - stores download sizes for instant button loading
# Pre-populated during sync, read during get_game_info
GAME_SIZES_CACHE_FILE = "game_sizes.json"

# In-memory cache for game sizes (avoids disk I/O on every get_game_info call)
_game_sizes_mem_cache: Optional[Dict[str, Dict]] = None
_game_sizes_mem_cache_time: float = 0
GAME_SIZES_MEM_CACHE_TTL = 60.0  # 60 seconds (sizes change rarely)


def get_game_sizes_cache_path() -> Path:
    """Get path to game sizes cache file (in user data, not plugin dir)"""
    return Path.home() / ".local" / "share" / "unifideck" / GAME_SIZES_CACHE_FILE


def _invalidate_game_sizes_mem_cache():
    """Invalidate in-memory game sizes cache"""
    global _game_sizes_mem_cache, _game_sizes_mem_cache_time
    _game_sizes_mem_cache = None
    _game_sizes_mem_cache_time = 0


def load_game_sizes_cache() -> Dict[str, Dict]:
    """Load game sizes cache with in-memory caching. Returns {store:game_id: {size_bytes, updated}}"""
    global _game_sizes_mem_cache, _game_sizes_mem_cache_time
    
    # Check in-memory cache first
    now = time.time()
    if _game_sizes_mem_cache is not None and (now - _game_sizes_mem_cache_time) < GAME_SIZES_MEM_CACHE_TTL:
        return _game_sizes_mem_cache
    
    # Cache miss - read from disk
    cache_path = get_game_sizes_cache_path()
    result = {}
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                result = json.load(f)
    except Exception as e:
        logger.error(f"Error loading game sizes cache: {e}")
    
    # Update in-memory cache
    _game_sizes_mem_cache = result
    _game_sizes_mem_cache_time = now
    return result


def save_game_sizes_cache(cache: Dict[str, Dict]) -> bool:
    """Save game sizes cache to file and update in-memory cache"""
    global _game_sizes_mem_cache, _game_sizes_mem_cache_time
    
    cache_path = get_game_sizes_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)
        logger.debug(f"Saved {len(cache)} entries to game sizes cache")
        
        # Update in-memory cache immediately
        _game_sizes_mem_cache = cache
        _game_sizes_mem_cache_time = time.time()
        return True
    except Exception as e:
        logger.error(f"Error saving game sizes cache: {e}")
        _invalidate_game_sizes_mem_cache()  # Invalidate on error
        return False


def cache_game_size(store: str, game_id: str, size_bytes: int) -> bool:
    """Cache a game's download size"""
    cache = load_game_sizes_cache()
    cache_key = f"{store}:{game_id}"
    cache[cache_key] = {
        'size_bytes': size_bytes,
        'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    return save_game_sizes_cache(cache)


def get_cached_game_size(store: str, game_id: str) -> Optional[int]:
    """Get cached game size, or None if not cached"""
    cache = load_game_sizes_cache()  # Uses in-memory cache
    cache_key = f"{store}:{game_id}"
    entry = cache.get(cache_key)
    return entry.get('size_bytes') if entry else None


# Compatibility Cache - stores ProtonDB tier and Steam Deck status for games
# Pre-populated during sync for fast "Great on Deck" filtering
COMPAT_CACHE_FILE = "compat_cache.json"

# ProtonDB tier types
PROTONDB_TIERS = ['platinum', 'gold', 'silver', 'bronze', 'borked', 'pending', 'native']

# Steam Deck compatibility categories from Steam API
DECK_CATEGORIES = {
    1: 'unknown',
    2: 'unsupported',
    3: 'playable',
    4: 'verified'
}

# User-Agent to avoid being blocked by APIs
COMPAT_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


def get_compat_cache_path() -> Path:
    """Get path to compatibility cache file"""
    return Path.home() / ".local" / "share" / "unifideck" / COMPAT_CACHE_FILE


def load_compat_cache() -> Dict[str, Dict]:
    """Load compatibility cache. Returns {normalized_title: {tier, deckVerified, steamAppId, timestamp}}"""
    cache_path = get_compat_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading compat cache: {e}")
    return {}


def save_compat_cache(cache: Dict[str, Dict]) -> bool:
    """Save compatibility cache to file"""
    cache_path = get_compat_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f, indent=2)
        logger.debug(f"Saved {len(cache)} entries to compat cache")
        return True
    except Exception as e:
        logger.error(f"Error saving compat cache: {e}")
        return False


# ============================================================================
# BackgroundCompatFetcher - Now imported from py_modules.unifideck.compat.library module
# ============================================================================
if BACKEND_AVAILABLE:
    BackgroundCompatFetcher = BackendCompatFetcher
else:
    raise ImportError("backend.compat module is required but not available")

class BackgroundSizeFetcher:
    """Background service to fetch game sizes asynchronously without blocking sync.
    
    - Runs in background (fire-and-forget from sync)
    - Fetches all pending sizes in parallel (30 concurrent)
    - Persists progress to game_sizes.json (survives restarts)
    - Starts automatically on plugin load if pending games exist
    """
    
    def __init__(self, epic_connector, gog_connector, amazon_connector=None):
        self.epic = epic_connector
        self.gog = gog_connector
        self.amazon = amazon_connector
        self._running = False
        self._task = None
        self._pending_games = []  # List of (store, game_id) tuples
        
    def queue_games(self, games: List, force_refresh: bool = False):
        """Queue games for background size fetching.
        
        Args:
            games: List of Game objects with 'store' and 'id' attributes
            force_refresh: If True, re-fetch sizes even if already cached
        """
        logger.info(f"[SizeService] queue_games() called with {len(games)} games, force_refresh={force_refresh}")
        
        # If force_refresh, stop any running task first so we can restart
        if force_refresh and self._running:
            logger.info("[SizeService] Stopping previous task for force_refresh")
            self.stop()
        
        cache = load_game_sizes_cache()
        
        # Clear pending list to avoid duplicates from previous runs
        self._pending_games = []
        pending_set = set()  # For deduplication within this batch
        
        for game in games:
            cache_key = f"{game.store}:{game.id}"
            # force_refresh bypasses cache check to re-fetch all sizes
            if force_refresh or cache_key not in cache:
                if cache_key not in pending_set:
                    pending_set.add(cache_key)
                    self._pending_games.append((game.store, game.id))
                    # Mark as pending in cache (null value)
                    cache[cache_key] = None
        
        save_game_sizes_cache(cache)
        logger.info(f"[SizeService] Queued {len(self._pending_games)} games for size fetching")
    
    def start(self):
        """Start background fetching (non-blocking)"""
        logger.info(f"[SizeService] start() called, _running={self._running}, pending={len(self._pending_games)}")
        
        # Reset _running if previous task is done (handles abnormal task completion)
        if self._running and self._task and self._task.done():
            logger.info("[SizeService] Previous task finished, resetting _running flag")
            self._running = False
        
        if self._running:
            logger.info("[SizeService] Already running, skipping start")
            return
        
        # Load pending from cache if not already queued
        if not self._pending_games:
            cache = load_game_sizes_cache()
            self._pending_games = [
                tuple(k.split(':', 1)) for k, v in cache.items() 
                if v is None and ':' in k
            ]
            logger.info(f"[SizeService] Loaded {len(self._pending_games)} pending games from cache")
        
        if not self._pending_games:
            logger.info("[SizeService] No pending games, not starting")
            return
        
        logger.info(f"[SizeService] Starting background fetch for {len(self._pending_games)} games")
        self._running = True
        self._task = asyncio.create_task(self._fetch_all())
    
    def stop(self):
        """Stop background fetching"""
        if self._task and not self._task.done():
            self._task.cancel()
        self._running = False
        logger.info("[SizeService] Stopped")
    
    async def _fetch_all(self):
        """Fetch all pending sizes in parallel"""
        try:
            import aiohttp
            import ssl
            
            # Create shared session for GOG reuse (critical for performance)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            
            async with aiohttp.ClientSession(connector=connector) as session:
                semaphore = asyncio.Semaphore(30)
                
                async def fetch_one(store: str, game_id: str):
                    async with semaphore:
                        try:
                            if store == 'epic':
                                size_bytes = await self.epic.get_game_size(game_id)
                            elif store == 'gog':
                                size_bytes = await self.gog.get_game_size(game_id, session=session)
                            elif store == 'amazon' and self.amazon:
                                size_bytes = await self.amazon.get_game_size(game_id)
                            else:
                                return (store, game_id, None)
                            
                            if size_bytes and size_bytes > 0:
                                # Update cache immediately (persist progress)
                                cache = load_game_sizes_cache()
                                cache[f"{store}:{game_id}"] = {
                                    'size_bytes': size_bytes,
                                    'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                                }
                                save_game_sizes_cache(cache)
                                logger.debug(f"[SizeService] Cached {store}:{game_id} = {size_bytes}")
                                return (store, game_id, size_bytes)
                            else:
                                # Log at debug level - GOG legacy games often have no size API
                                logger.debug(f"[SizeService] No size for {store}:{game_id}")
                                return (store, game_id, None)
                        except Exception as e:
                            logger.warning(f"[SizeService] Error fetching {store}:{game_id}: {e}")
                            return (store, game_id, None)
                
                # Fire all at once
                tasks = [fetch_one(store, gid) for store, gid in self._pending_games]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            
            success = sum(1 for r in results if isinstance(r, tuple) and r[2] is not None)
            logger.info(f"[SizeService] Complete: {success}/{len(self._pending_games)} sizes cached")
            
        except asyncio.CancelledError:
            logger.info("[SizeService] Cancelled")
        except Exception as e:
            logger.error(f"[SizeService] Error: {e}")
        finally:
            self._running = False
            self._pending_games = []


class SyncProgress:
    """Track library sync progress with phase-based percentage tracking.
    
    Each sync phase has an allocated percentage range for smooth progress bar updates.
    """
    
    # Phase percentage allocations: (start_pct, end_pct)
    PHASE_RANGES = {
        'idle': (0, 0),
        'fetching': (0, 10),
        'checking_installed': (10, 20),
        'syncing': (20, 40),
        'unifidb_lookup': (40, 50),
        'sgdb_lookup': (50, 60),
        'checking_artwork': (60, 65),
        'artwork': (65, 95),
        'proton_setup': (95, 98),
        'complete': (100, 100),
        'error': (100, 100),
        'cancelled': (100, 100)
    }
    
    def __init__(self):
        self.total_games = 0
        self.synced_games = 0
        self.current_game = {
            "label": None,     # key i18n
            "values": {}       # dynamic values
        }
        self.status = "idle"  # idle, fetching, checking_installed, syncing, sgdb_lookup, checking_artwork, artwork, proton_setup, complete, error, cancelled
        self.error = None

        # Artwork-specific tracking
        self.artwork_total = 0
        self.artwork_synced = 0
        self.current_phase = "sync"  # "sync" or "artwork"

        # Steam/unifiDB metadata tracking
        self.steam_total = 0
        self.steam_synced = 0
        self.unifidb_total = 0
        self.unifidb_synced = 0

        # Lock for thread-safe updates during parallel downloads
        self._lock = asyncio.Lock()

    def reset(self):
        """Reset all progress state for a new sync operation.
        
        Must be called at the start of sync_libraries / force_sync_libraries
        to prevent stale data from the previous sync leaking into the UI.
        """
        self.total_games = 0
        self.synced_games = 0
        self.current_game = {"label": None, "values": {}}
        self.status = "idle"
        self.error = None
        self.artwork_total = 0
        self.artwork_synced = 0
        self.current_phase = "sync"
        self.steam_total = 0
        self.steam_synced = 0
        self.unifidb_total = 0
        self.unifidb_synced = 0

    async def increment_artwork(self, game_title: str) -> int:
        """Thread-safe artwork counter increment"""
        async with self._lock:
            self.artwork_synced += 1
            self.current_game = {
                "label": "artwork.downloadProgress",
                "values": {
                    "synced": self.artwork_synced,
                    "total": self.artwork_total,
                    "game": game_title
                }
            }
            return self.artwork_synced

    async def increment_steam(self, game_title: str) -> int:
        """Thread-safe Steam metadata counter increment"""
        async with self._lock:
            self.steam_synced += 1
            self.current_game = {
                "label": "sync.extractingSteamMetadata",
                "values": {
                    "synced": self.steam_synced,
                    "total": self.steam_total,
                    "game_title": game_title
                }
            }
            return self.steam_synced

    async def increment_unifidb(self, game_title: str) -> int:
        """Thread-safe unifiDB metadata counter increment"""
        async with self._lock:
            self.unifidb_synced += 1
            self.current_game = {
                "label": "sync.lookingUpUnifiDB",
                "values": {
                    "synced": self.unifidb_synced,
                    "total": self.unifidb_total,
                    "game_title": game_title
                }
            }
            return self.unifidb_synced

    def _calculate_progress(self) -> int:
        """Calculate progress based on current phase and its percentage allocation.
        
        Each phase uses its own counters for smooth sub-progress within the phase range.
        """
        phase_range = self.PHASE_RANGES.get(self.status, (0, 0))
        start_pct, end_pct = phase_range
        
        # Calculate sub-progress for phases with counters
        if self.status == 'artwork' and self.artwork_total > 0:
            sub_progress = self.artwork_synced / self.artwork_total
            return int(start_pct + (end_pct - start_pct) * sub_progress)
        
        if self.status == 'unifidb_lookup' and self.unifidb_total > 0:
            sub_progress = self.unifidb_synced / self.unifidb_total
            return int(start_pct + (end_pct - start_pct) * sub_progress)
        
        if self.status == 'syncing' and self.steam_total > 0:
            sub_progress = self.steam_synced / self.steam_total
            return int(start_pct + (end_pct - start_pct) * sub_progress)
        
        # For phases without counters, return the start of the phase range
        return start_pct

    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': True,
            'total_games': self.total_games,
            'synced_games': self.synced_games,
            'current_game': self.current_game,
            'status': self.status,
            'progress_percent': self._calculate_progress(),
            'error': self.error,
            # Artwork fields
            'artwork_total': self.artwork_total,
            'artwork_synced': self.artwork_synced,
            'current_phase': self.current_phase,
            # Steam/unifiDB metadata fields
            'steam_total': self.steam_total,
            'steam_synced': self.steam_synced,
            'unifidb_total': self.unifidb_total,
            'unifidb_synced': self.unifidb_synced
        }





# ============================================================================
# EpicConnector - Now imported from py_modules.unifideck.stores.epic module
# ============================================================================
if BACKEND_AVAILABLE:
    EpicConnector = BackendEpicConnector
else:
    raise ImportError("backend.stores.epic module is required but not available")

# ============================================================================
# AmazonConnector - Now imported from py_modules.unifideck.stores.amazon module
# ============================================================================
if BACKEND_AVAILABLE:
    AmazonConnector = BackendAmazonConnector
else:
    raise ImportError("backend.stores.amazon module is required but not available")

# ============================================================================
# GOGAPIClient - Now imported from py_modules.unifideck.stores.gog module
# ============================================================================
if BACKEND_AVAILABLE:
    GOGAPIClient = BackendGOGAPIClient
else:
    raise ImportError("backend.stores.gog module is required but not available")

class InstallHandler:
    """Handles game installations across stores"""

    def __init__(self, shortcuts_manager: ShortcutsManager, plugin_dir: Optional[str] = None):
        self.shortcuts_manager = shortcuts_manager
        self.plugin_dir = plugin_dir

    async def get_epic_game_exe(self, game_id: str) -> Optional[str]:
        """Get executable path for installed Epic game"""
        legendary_bin = EpicConnector(plugin_dir=self.plugin_dir)._find_legendary()
        if not legendary_bin:
            return None

        try:
            # Get game info in JSON format
            proc = await asyncio.create_subprocess_exec(
                legendary_bin, 'info', game_id, '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                info = json.loads(stdout.decode())
                install_path = info.get('install', {}).get('install_path', '')
                executable = info.get('manifest', {}).get('launch_exe', '')

                if install_path and executable:
                    # Strip leading slash - legendary returns paths like '/Binaries/Win64/Game.exe'
                    # which causes os.path.join to treat it as absolute, ignoring install_path
                    executable = executable.lstrip('/')
                    exe_path = os.path.join(install_path, executable)
                    logger.info(f"Found Epic game executable: {exe_path}")
                    return exe_path

        except Exception as e:
            logger.error(f"Error getting Epic game exe: {e}")

        return None

    async def install_epic_game(self, game_id: str, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install Epic game via legendary"""
        legendary_bin = EpicConnector(plugin_dir=self.plugin_dir)._find_legendary()
        if not legendary_bin:
            return {'success': False, 'error': 'legendary not found'}

        try:
            cmd = [legendary_bin, 'install', game_id, '--yes']
            if install_path:
                cmd.extend(['--base-path', install_path])

            logger.info(f"Installing Epic game: {' '.join(cmd)}")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                # Get actual executable path
                exe_path = await self.get_epic_game_exe(game_id)

                if exe_path:
                    # Extract install directory from exe path
                    import os.path
                    install_dir = os.path.dirname(exe_path)

                    # Update shortcuts.vdf with install info
                    await self.shortcuts_manager.mark_installed(game_id, 'epic', install_dir, exe_path)
                else:
                    # Fallback: keep launcher script
                    logger.warning(f"Could not find exe for {game_id}, keeping launcher script")

                logger.info(f"Successfully installed {game_id}")
                return {'success': True, 'exe_path': exe_path}
            else:
                logger.error(f"Install failed: {stderr.decode()}")
                return {'success': False, 'error': stderr.decode()}

        except Exception as e:
            logger.error(f"Error installing Epic game: {e}")
            return {'success': False, 'error': str(e)}

    async def get_gog_game_exe(self, game_id: str, install_dir: str) -> Optional[str]:
        """Find executable for GOG game"""
        # Look for start.sh or other launch scripts
        common_launchers = ['start.sh', 'launch.sh', f'{game_id}.sh']

        for launcher in common_launchers:
            launcher_path = os.path.join(install_dir, launcher)
            if os.path.exists(launcher_path):
                logger.info(f"Found GOG game launcher: {launcher_path}")
                return launcher_path

        # Try to find any .sh file in the directory
        try:
            for item in os.listdir(install_dir):
                if item.endswith('.sh') and os.path.isfile(os.path.join(install_dir, item)):
                    launcher_path = os.path.join(install_dir, item)
                    logger.info(f"Found GOG game script: {launcher_path}")
                    return launcher_path
        except Exception as e:
            logger.error(f"Error searching for GOG launcher: {e}")

        return None

    async def install_gog_game(self, game_id: str, gog_instance, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install GOG game using GOG API

        Args:
            game_id: GOG game product ID
            gog_instance: Instance of GOG class with API methods
            install_path: Optional custom install path (not used - GOG class manages this)

        Returns:
            Dict with success status and exe_path
        """
        try:
            # Use the GOG class's install_game method which uses the API
            result = await gog_instance.install_game(game_id)

            if result.get('success'):
                # Update shortcuts.vdf with the installed game info
                exe_path = result.get('executable')
                install_dir = result.get('install_path')
                work_dir = result.get('work_dir')  # From goggame-*.info

                if install_dir:
                    await self.shortcuts_manager.mark_installed(game_id, 'gog', install_dir, exe_path, work_dir)
                    logger.info(f"Successfully installed GOG game {game_id} with work_dir={work_dir}")
                    return {'success': True, 'exe_path': exe_path, 'install_path': install_dir, 'work_dir': work_dir}

            return result

        except Exception as e:
            logger.error(f"Error installing GOG game: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def get_amazon_game_exe(self, game_id: str, install_dir: str = None) -> Optional[str]:
        """Find executable for Amazon game using fuel.json"""
        # If no install_dir provided, try to find from nile config
        if not install_dir:
            nile_config = os.path.expanduser("~/.config/nile")
            installed_file = os.path.join(nile_config, "installed.json")
            
            if os.path.exists(installed_file):
                try:
                    with open(installed_file, 'r') as f:
                        installed_list = json.load(f)
                    
                    for game in installed_list:
                        if game.get('id') == game_id:
                            install_dir = game.get('path', '')
                            break
                except Exception as e:
                    logger.error(f"[Amazon] Error reading installed.json: {e}")
        
        if not install_dir:
            logger.warning(f"[Amazon] Could not find install directory for {game_id}")
            return None
        
        # Parse fuel.json for executable
        fuel_path = os.path.join(install_dir, 'fuel.json')
        if not os.path.exists(fuel_path):
            logger.warning(f"[Amazon] No fuel.json found at {fuel_path}")
            return None
        
        try:
            import re
            with open(fuel_path, 'r') as f:
                content = f.read()
                # Remove single-line comments (fuel.json may have them)
                content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
                fuel_data = json.loads(content)
            
            main_cmd = fuel_data.get('Main', {}).get('Command', '')
            if main_cmd:
                exe_path = os.path.join(install_dir, main_cmd)
                logger.info(f"[Amazon] Found executable from fuel.json: {exe_path}")
                return exe_path
        except Exception as e:
            logger.error(f"[Amazon] Error parsing fuel.json: {e}")
        
        return None

    async def install_amazon_game(self, game_id: str, amazon_instance, install_path: Optional[str] = None) -> Dict[str, Any]:
        """Install Amazon game using nile CLI

        Args:
            game_id: Amazon game product ID
            amazon_instance: Instance of AmazonConnector with install methods
            install_path: Optional custom install path

        Returns:
            Dict with success status and exe_path
        """
        try:
            # Use the AmazonConnector's install_game method
            result = await amazon_instance.install_game(game_id)

            if result.get('success'):
                # Update shortcuts.vdf with the installed game info
                exe_path = result.get('exe_path')
                install_dir = result.get('install_path')

                if install_dir:
                    await self.shortcuts_manager.mark_installed(game_id, 'amazon', install_dir, exe_path)
                    logger.info(f"Successfully installed Amazon game {game_id}")
                    return {'success': True, 'exe_path': exe_path, 'install_path': install_dir}

            return result

        except Exception as e:
            logger.error(f"Error installing Amazon game: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}


class BackgroundSyncService:
    """Background service that syncs libraries every 5 minutes"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.running = False
        self.task = None

    async def start(self):
        """Start background sync"""
        if self.running:
            logger.warning("Background sync already running")
            return

        self.running = True
        self.task = asyncio.create_task(self._sync_loop())
        logger.info("Background sync service started")

    async def stop(self):
        """Stop background sync"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("Background sync service stopped")

    async def _sync_loop(self):
        """Main sync loop - sync game lists only, no artwork"""
        while self.running:
            try:
                # Only sync game lists, don't fetch artwork in background
                await self.plugin.sync_libraries(fetch_artwork=False)
                await asyncio.sleep(300)  # 5 minutes
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in sync loop: {e}")
                await asyncio.sleep(60)  # Retry in 1 minute on error


class Plugin:
    """Main Unifideck plugin class"""

    async def _main(self):
        # === VERSION VALIDATION & CACHE CLEANUP ===
        # Decky doesn't fully delete old files when sideloading updates.
        # Old __pycache__ can cause version regression. Clean on every startup.
        try:
            plugin_dir = os.path.dirname(__file__)
            plugin_json_path = os.path.join(plugin_dir, 'plugin.json')
            with open(plugin_json_path) as f:
                plugin_info = json.load(f)
                loaded_version = plugin_info.get('version', 'unknown')
            
            logger.info(f"[INIT] Unifideck v{loaded_version} starting...")
            
            # Cleanup stale __pycache__ on every startup to prevent version mismatch
            import shutil
            cleaned_count = 0
            for root, dirs, _ in os.walk(plugin_dir):
                for d in dirs:
                    if d == '__pycache__':
                        cache_path = os.path.join(root, d)
                        try:
                            shutil.rmtree(cache_path)
                            cleaned_count += 1
                        except Exception as e:
                            logger.warning(f"[INIT] Failed to clean cache {cache_path}: {e}")
            
            if cleaned_count > 0:
                logger.info(f"[INIT] Cleaned {cleaned_count} stale __pycache__ directories")
                        
        except Exception as e:
            logger.error(f"[INIT] Version check failed: {e}")

        logger.info("[INIT] Starting Unifideck plugin initialization")

        # === CDP INITIALIZATION (for native PlaySection hiding) ===
        # Enable CEF remote debugging flag (required for CDP access)
        flag_created = create_cef_debugging_flag()
        if flag_created:
            logger.info("[INIT CDP] CEF debugging flag created - Steam restart required for CDP to work")

        # Connect to CDP (will fail gracefully if Steam not restarted yet)
        try:
            await get_cdp_client()
            logger.info("[INIT CDP] CDP client connected successfully")
        except Exception as e:
            logger.warning(f"[INIT CDP] CDP connection failed (restart Steam if needed): {e}")

        # Initialize sync progress tracker
        self.sync_progress = SyncProgress()

        logger.info("[INIT] Initializing ShortcutsManager")
        self.shortcuts_manager = ShortcutsManager(plugin_dir=os.path.dirname(os.path.abspath(__file__)))
        
        # Migrate any data from user 0 to the logged-in user (fixes past user 0 issues)
        logger.info("[INIT] Checking for user 0 data to migrate")
        migration_result = migrate_user0_to_logged_in_user()
        if migration_result.get('shortcuts_migrated', 0) > 0 or migration_result.get('artwork_migrated', 0) > 0:
            logger.info(f"[INIT] User 0 migration: {migration_result['shortcuts_migrated']} shortcuts, {migration_result['artwork_migrated']} artwork files migrated")
        
        # Detect account switch (must happen before reconciliation so modal can be shown)
        logger.info("[INIT] Checking for account switch")
        self.account_manager = AccountManager()
        self.account_manager.detect_account_switch()
        self.account_manager.save_current_user()

        # Reconcile games.map to remove orphaned entries (games deleted externally)
        logger.info("[INIT] Reconciling games.map for orphaned entries")
        reconcile_result = self.shortcuts_manager.reconcile_games_map()
        if reconcile_result.get('removed', 0) > 0:
            logger.info(f"[INIT] Reconciliation complete: {reconcile_result['removed']} orphaned entries removed")

        # Repair shortcuts pointing to old plugin paths (happens after Decky reinstall)
        logger.info("[INIT] Repairing shortcuts pointing to old plugin paths")
        repair_result = self.shortcuts_manager.repair_shortcuts_exe_path()
        if repair_result.get('repaired', 0) > 0:
            logger.info(f"[INIT] Repaired {repair_result['repaired']} shortcuts with stale launcher paths")

        # Ensure shortcuts exist for all games in games.map (recreate missing shortcuts)
        logger.info("[INIT] Reconciling shortcuts from games.map")
        shortcut_reconcile = self.shortcuts_manager.reconcile_shortcuts_from_games_map()
        if shortcut_reconcile.get('created', 0) > 0:
            logger.info(f"[INIT] Created {shortcut_reconcile['created']} missing shortcuts from games.map")

        logger.info("[INIT] Initializing EpicConnector")
        self.epic = EpicConnector(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self)

        logger.info("[INIT] Initializing GOGAPIClient")
        self.gog = GOGAPIClient(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self)
        
        # Validate and auto-correct GOG executable paths that point to installers
        logger.info("[INIT] Validating GOG executable paths")
        gog_validation = self.shortcuts_manager.validate_gog_exe_paths(self.gog)
        if gog_validation.get('corrected', 0) > 0:
            logger.info(f"[INIT] Auto-corrected {gog_validation['corrected']} GOG installer paths")

        logger.info("[INIT] Initializing AmazonConnector")
        self.amazon = AmazonConnector(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self)

        # Repair games.map for Unifideck shortcuts missing entries (fixes "Game location not mapped" errors)
        logger.info("[INIT] Reconciling games.map from installed games")
        map_reconcile = await self.shortcuts_manager.reconcile_games_map_from_installed(
            epic_client=self.epic, gog_client=self.gog, amazon_client=self.amazon
        )
        if map_reconcile.get('added', 0) > 0:
            logger.info(f"[INIT] Added {map_reconcile['added']} missing entries to games.map")

        logger.info("[INIT] Initializing InstallHandler")
        self.install_handler = InstallHandler(self.shortcuts_manager, plugin_dir=DECKY_PLUGIN_DIR)

        logger.info("[INIT] Initializing CloudSaveManager")
        self.cloud_save_manager = CloudSaveManager(plugin_dir=DECKY_PLUGIN_DIR)

        # NOTE: Background sync disabled due to method binding issues
        # Users can manually trigger sync via the UI
        logger.info("[INIT] Background sync service disabled")
        self.background_sync = None
        # logger.info("[INIT] Initializing BackgroundSyncService")
        # self.background_sync = BackgroundSyncService(self)

        # Initialize background size fetcher (non-blocking, persists across restarts)
        logger.info("[INIT] Initializing BackgroundSizeFetcher")
        self.size_fetcher = BackgroundSizeFetcher(self.epic, self.gog, self.amazon)
        # Auto-start if there are pending games from previous session
        self.size_fetcher.start()

        # Initialize background compatibility fetcher (ProtonDB/Deck Verified)
        logger.info("[INIT] Initializing BackgroundCompatFetcher")
        self.compat_fetcher = BackgroundCompatFetcher()

        # Initialize SteamGridDB client with hardcoded API key
        self.steamgriddb_api_key = "1a410cb7c288b8f21016c2df4c81df74"

        if STEAMGRIDDB_AVAILABLE:
            try:
                logger.info("[INIT] Initializing SteamGridDB client")
                self.steamgriddb = SteamGridDBClient(self.steamgriddb_api_key)
                logger.info("[INIT] SteamGridDB client initialized successfully")
            except Exception as e:
                logger.error(f"[INIT] Failed to initialize SteamGridDB: {e}", exc_info=True)
                self.steamgriddb = None
        else:
            logger.warning("[INIT] SteamGridDB not available - skipping")
            self.steamgriddb = None

        # Global lock to prevent concurrent syncs
        self._sync_lock = asyncio.Lock()
        self._is_syncing = False  # Flag for checking without blocking
        self._cancel_sync = False  # Flag for cancelling in-progress sync

        # Initialize download queue with plugin directory for finding binaries
        logger.info(f"[INIT] Initializing DownloadQueue with plugin_dir={DECKY_PLUGIN_DIR}")
        self.download_queue = get_download_queue(DECKY_PLUGIN_DIR)
        
        # STARTUP: Backend DownloadQueue handles cleanup and auto-resume internally
        # - cleanup_processes() is called automatically in __init__
        # - start_queue() is called automatically in _load() if queue has items
        logger.info("[INIT] DownloadQueue initialized with auto-cleanup and auto-resume")
        
        # Set callback for when downloads complete
        async def on_download_complete(item):
            """Mark game as installed when download completes
            
            IMPORTANT: This callback must set item.status = DownloadStatus.ERROR
            if registration fails, otherwise the UI will show 'completed' but 
            game launch will fail with 'game not found'.
            """
            registration_success = False
            error_message = None
            
            try:
                logger.info(f"[DownloadComplete] Processing completed download: {item.game_title}")
                
                # Get executable path from store-specific handler
                if item.store == 'epic':
                    exe_path = await self.install_handler.get_epic_game_exe(item.game_id)
                    if exe_path:
                        install_path = self.download_queue.get_install_path(item.storage_location)
                        game_install_path = os.path.join(install_path, item.game_id)
                        
                        # Write marker file to identify completed installs (matches GOG behavior)
                        # This helps protect against accidental deletion on cancel
                        try:
                            marker_path = os.path.join(game_install_path, '.unifideck-id')
                            if os.path.exists(game_install_path):
                                with open(marker_path, 'w') as f:
                                    f.write(item.game_id)
                                logger.info(f"[DownloadComplete] Wrote .unifideck-id marker for Epic game {item.game_id}")
                        except Exception as e:
                            logger.warning(f"[DownloadComplete] Failed to write marker file: {e}")
                        
                        await self.shortcuts_manager.mark_installed(
                            item.game_id, item.store, game_install_path, exe_path
                        )
                        logger.info(f"[DownloadComplete] Marked {item.game_title} as installed")
                        registration_success = True
                        
                        # Invalidate legendary cache to ensure fresh status on next query
                        global _legendary_installed_cache
                        _legendary_installed_cache['data'] = None
                        logger.debug("[DownloadComplete] Invalidated legendary installed cache")
                    else:
                        error_message = "Could not find Epic game executable"
                        logger.error(f"[DownloadComplete] {error_message} for {item.game_title}")
                elif item.store == 'gog':
                    # GOG installs to <install_path>/<game_title>
                    # We need to find the folder in the install location used for this download
                    
                    # 1. Start with proper search paths
                    # Order matters: [fallback, primary] ensures primary wins if found in both
                    search_paths = []
                    
                    default_gog_path = os.path.expanduser("~/GOG Games")
                    if os.path.exists(default_gog_path):
                        search_paths.append(default_gog_path)
                        
                    primary_install_path = self.download_queue.get_install_path(item.storage_location)
                    if primary_install_path != default_gog_path and os.path.exists(primary_install_path):
                        search_paths.append(primary_install_path)
                    
                    game_install_path = None
                    
                    for gog_base in search_paths:
                        # Scan directories in this base path
                        for folder in os.listdir(gog_base):
                            folder_path = os.path.join(gog_base, folder)
                            if os.path.isdir(folder_path):
                                # Check if this folder has a marker for this game ID
                                # Use GOG client's identification logic (standard .unifideck-id check)
                                found_id = self.gog._get_game_id_from_dir(folder_path)
                                if found_id == item.game_id:
                                    game_install_path = folder_path
                                    break
                                
                                # Fallback: Check for legacy filename-based marker
                                marker_path = os.path.join(folder_path, f".unifideck_gog_{item.game_id}")
                                if os.path.exists(marker_path):
                                    game_install_path = folder_path
                                    break
                    
                    if game_install_path:
                        exe_result = self.gog._find_game_executable_with_workdir(game_install_path)
                        if exe_result:
                            exe_path, work_dir = exe_result
                            await self.shortcuts_manager.mark_installed(
                                item.game_id, item.store, game_install_path, exe_path, work_dir
                            )
                            logger.info(f"[DownloadComplete] Marked {item.game_title} as installed with work_dir={work_dir}")
                            registration_success = True
                        else:
                            error_message = "Could not find GOG game executable"
                            logger.error(f"[DownloadComplete] {error_message} for {item.game_title}")
                    else:
                        error_message = "Could not find GOG install folder"
                        logger.error(f"[DownloadComplete] {error_message} for {item.game_title}")
                elif item.store == 'amazon':
                    # Amazon installs - use nile's installed.json for path info
                    game_info = self.amazon.get_installed_game_info(item.game_id)
                    if game_info:
                        game_install_path = game_info.get('path', '')
                        exe_path = game_info.get('executable')
                        
                        if game_install_path and exe_path:
                            await self.shortcuts_manager.mark_installed(
                                item.game_id, item.store, game_install_path, exe_path
                            )
                            logger.info(f"[DownloadComplete] Marked {item.game_title} as installed")
                            registration_success = True
                        else:
                            error_message = "Could not find Amazon game executable"
                            logger.error(f"[DownloadComplete] {error_message} for {item.game_title}")
                    else:
                        error_message = "Could not find Amazon install info"
                        logger.error(f"[DownloadComplete] {error_message} for {item.game_title}")
            except Exception as e:
                error_message = str(e)
                logger.error(f"[DownloadComplete] Exception marking game installed: {e}")
            
            # FIX 1: Propagate registration failures to download status
            # This ensures users see an error in the UI instead of 'completed'
            if not registration_success:
                from py_modules.unifideck.download.manager import DownloadStatus
                item.status = DownloadStatus.ERROR
                item.error_message = error_message or "Failed to register game after download"
                logger.error(f"[DownloadComplete] REGISTRATION FAILED for {item.game_title}: {item.error_message}")

        
        self.download_queue.set_on_complete_callback(on_download_complete)
        
        # Set GOG install callback to use GOGAPIClient
        async def gog_install_callback(game_id: str, install_path: str = None, progress_callback=None, language: str = None):
            """Delegate GOG downloads to GOGAPIClient.install_game"""
            return await self.gog.install_game(game_id, install_path, progress_callback, language=language)

        self.download_queue.set_gog_install_callback(gog_install_callback)
        
        # Set size cache callback to update Install button sizes when accurate size is received
        self.download_queue.set_size_cache_callback(cache_game_size)

        logger.info("[INIT] Unifideck plugin initialization complete")

    # Frontend-callable methods

    async def has_artwork(self, app_id: int) -> bool:
        """Check if required artwork files exist for this app_id.
        
        Returns True if grid, hero, and logo exist (any file extension).
        Icon is optional since not all games have icons on SteamGridDB,
        and missing icon shouldn't mark the entire game as needing re-download.
        
        Uses glob patterns to detect artwork regardless of extension,
        so custom artwork set via Steam UI, SGDBoop, Decky SteamGridDB plugin,
        or any other tool is properly recognized.
        """
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return False

        # Convert signed int32 to unsigned for filename check (same as download logic)
        # Steam artwork files use unsigned app IDs even though shortcuts.vdf stores signed
        # Example: -1257913040 (signed) -> 3037054256 (unsigned)
        unsigned_id = app_id if app_id >= 0 else app_id + 2**32

        # Check for 3 REQUIRED artwork types (icon is optional bonus)
        # Use glob to match any extension — users may set custom art via other tools
        grid_path = Path(self.steamgriddb.grid_path)
        required_patterns = [
            f"{unsigned_id}p.*",      # Grid (portrait/vertical)
            f"{unsigned_id}_hero.*",  # Hero image
            f"{unsigned_id}_logo.*",  # Logo
        ]
        # Return True if all REQUIRED types have at least one matching file
        return all(list(grid_path.glob(pattern)) for pattern in required_patterns)

    async def get_missing_artwork_types(self, app_id: int) -> set:
        """Check which specific artwork types are missing for this app_id

        Uses glob patterns to detect artwork regardless of extension,
        so custom artwork set via other tools is properly recognized.

        Returns:
            set: Set of missing artwork types (e.g., {'grid', 'grid_l', 'hero', 'logo'})
            Icon is excluded from this check since it's optional.
        """
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return {'grid', 'grid_l', 'hero', 'logo'}

        unsigned_id = app_id if app_id >= 0 else app_id + 2**32
        grid_path = Path(self.steamgriddb.grid_path)

        # Only check required types (icon is optional)
        # Use glob patterns to detect any extension
        artwork_patterns = {
            'grid': f"{unsigned_id}p.*",
            'hero': f"{unsigned_id}_hero.*",
            'logo': f"{unsigned_id}_logo.*",
        }

        missing = {art_type for art_type, pattern in artwork_patterns.items() if not list(grid_path.glob(pattern))}

        # Landscape grid needs explicit file check — glob {id}.* would also match {id}p.*, {id}_hero.*, etc.
        has_grid_l = any((grid_path / f"{unsigned_id}{ext}").exists() for ext in ('.jpg', '.png', '.webp'))
        if not has_grid_l:
            missing.add('grid_l')

        return missing

    async def fetch_artwork_with_progress(self, game, semaphore, only_types: set = None):
        """Fetch artwork for a single game with concurrency control and timeout

        Args:
            game: Game object to fetch artwork for
            semaphore: Concurrency limiter
            only_types: If provided, only fetch these artwork types ('grid', 'hero', 'logo', 'icon').
                       If None, fetch all types.

        Returns:
            dict: {success: bool, timed_out: bool, game: Game, error: str}
        """
        async with semaphore:
            try:
                # Update status to show we're working on this game (before download)
                self.sync_progress.current_game = {
                    "label": "sync.downloadingArtwork",
                    "values": {"game": game.title}
                }

                # Wrap with timeout to prevent sync from hanging
                try:
                    result = await asyncio.wait_for(
                        self.steamgriddb.fetch_game_art(
                            game.title,
                            game.app_id,
                            store=game.store,      # 'epic', 'gog', or 'amazon'
                            store_id=game.id,      # Store-specific game ID
                            only_types=only_types  # Only fetch specified types (if any)
                        ),
                        timeout=ARTWORK_FETCH_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Artwork fetch timed out for {game.title} after {ARTWORK_FETCH_TIMEOUT}s")
                    await self.sync_progress.increment_artwork(game.title)
                    return {'success': False, 'timed_out': True, 'game': game}

                # Store steam_app_id from search (for ProtonDB lookups)
                if result.get('steam_app_id'):
                    game.steam_app_id = result['steam_app_id']

                # Update progress (thread-safe)
                count = await self.sync_progress.increment_artwork(game.title)

                # Build detailed source log
                sources = result.get('sources', [])
                art_count = result.get('artwork_count', 0)
                sgdb = '+SGDB' if result.get('sgdb_filled') else ''
                source_str = ' '.join(sources) if sources else 'NO_SOURCE'

                # Log format: [progress] STORE: Title [sources] (artwork_count)
                logger.info(f"  [{count}/{self.sync_progress.artwork_total}] {game.store.upper()}: {game.title} [{source_str}{sgdb}] ({art_count}/5)")

                return {'success': result.get('success', False), 'game': game, 'artwork_count': art_count}
            except Exception as e:
                logger.error(f"Error fetching artwork for {game.title}: {e}")
                await self.sync_progress.increment_artwork(game.title)
                return {'success': False, 'error': str(e), 'game': game}

    async def sync_libraries(self, fetch_artwork: bool = True) -> Dict[str, Any]:
        """Sync all game libraries to shortcuts.vdf and optionally fetch artwork - with global lock protection"""

        # Check if sync already running (non-blocking check)
        if self._is_syncing:
            logger.warning("Sync already in progress, ignoring request")
            return {
                'success': False,
                'error': 'errors.syncInProgress',
                'epic_count': 0,
                'gog_count': 0,
                'added_count': 0,
                'artwork_count': 0
            }

        # Acquire lock (prevents concurrent syncs)
        async with self._sync_lock:
            self._is_syncing = True
            self._cancel_sync = False  # Reset cancel flag
            try:
                logger.info("Syncing libraries...")

                # Reset all progress state from previous sync
                self.sync_progress.reset()

                # Update progress: Fetching games
                self.sync_progress.status = "fetching"
                self.sync_progress.current_game = {
                    "label": "sync.fetchingGameLists",
                    "values": {}
                }

                # Get games from all stores
                epic_games = await self.epic.get_library()
                gog_games = await self.gog.get_library()
                amazon_games = await self.amazon.get_library()

                # Robustly handle API failures (None returns)
                valid_stores = []
                all_games = []

                if epic_games is not None:
                    valid_stores.append('epic')
                    all_games.extend(epic_games)
                else:
                    epic_games = [] # For iteration below

                if gog_games is not None:
                     valid_stores.append('gog')
                     all_games.extend(gog_games)
                else:
                     gog_games = []

                if amazon_games is not None:
                     valid_stores.append('amazon')
                     all_games.extend(amazon_games)
                else:
                     amazon_games = []

                # Check for cancellation
                if self._cancel_sync:
                    logger.warning("Sync cancelled by user after fetching libraries")
                    self.sync_progress.status = "cancelled"
                    self.sync_progress.current_game = {
                        "label": "sync.cancelledByUser",
                        "values": {}
                    }
                    return {
                        'success': False,
                        'error': 'errors.syncCancelled',
                        'cancelled': True,
                        'epic_count': 0,
                        'gog_count': 0,
                        'amazon_count': 0,
                        'added_count': 0,
                        'updated_count': 0,
                        'artwork_count': 0
                    }
                self.sync_progress.total_games = len(all_games)
                self.sync_progress.synced_games = 0

                # Log library composition for debugging game count discrepancies
                logger.info(f"Sync: Library composition - Epic: {len(epic_games)}, GOG: {len(gog_games)}, Amazon: {len(amazon_games)}, Total: {len(all_games)}")
                logger.debug(f"  Total Unifideck games in all libraries: {len(all_games)} (these are from store APIs)")
                logger.debug(f"  Note: Displayed game count may differ if some games fail shortcut registration or have invalid launch options")

                # NOTE: We no longer clear caches at the start of sync.
                # Caches are only overwritten after successful fetch to prevent data loss.
                # Old caches are backed up before overwriting (see save_*_cache functions).

                # Queue games for background compat fetching (ProtonDB/Deck Verified)
                logger.info("Sync: Queueing games for compatibility lookup...")
                self.compat_fetcher.queue_games(all_games)
                self.compat_fetcher.start()  # Non-blocking background fetch

                # Update progress: Checking installed status
                self.sync_progress.status = "checking_installed"
                self.sync_progress.current_game = {
                    "label": "sync.checkingInstalledGames",
                    "values": {}
                }

                # Get installed games
                epic_installed = await self.epic.get_installed()
                gog_installed = await self.gog.get_installed()
                amazon_installed = await self.amazon.get_installed()

                # Mark installed status
                for game in epic_games:
                    if game.id in epic_installed:
                        game.is_installed = True
                        
                        # OPTIMIZATION: Use cached metadata to get EXE path instead of slow subprocess
                        # epic_installed is now a dict: {app_name: metadata}
                        metadata = epic_installed[game.id]
                        
                        install_path = metadata.get('install', {}).get('install_path')
                        executable = metadata.get('manifest', {}).get('launch_exe')
                        
                        exe_path = None
                        if install_path and executable:
                            exe_path = os.path.join(install_path, executable)
                        elif metadata.get('install_path') and metadata.get('executable'):
                            # Fallback structure
                             exe_path = os.path.join(metadata['install_path'], metadata['executable'])
                             
                        if exe_path:
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('epic', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for Epic game {game.id} (FAST)")
                        else:
                            # Fallback to slow method if metadata missing
                            logger.debug(f"Metadata missing for {game.id}, falling back to slow check")
                            exe_path = await self.install_handler.get_epic_game_exe(game.id)
                            if exe_path:
                                work_dir = os.path.dirname(exe_path)
                                await self.shortcuts_manager._update_game_map('epic', game.id, exe_path, work_dir)

                for game in gog_games:
                    if game.id in gog_installed:
                        game.is_installed = True
                        # Ensure games.map is updated for games installed outside Unifideck
                        game_info = self.gog.get_installed_game_info(game.id)
                        if game_info and game_info.get('executable'):
                            exe_path = game_info['executable']
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('gog', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for GOG game {game.id}")

                for game in amazon_games:
                    if game.id in amazon_installed:
                        game.is_installed = True
                        # Ensure games.map is updated for Amazon games
                        game_info = self.amazon.get_installed_game_info(game.id)
                        if game_info and game_info.get('executable'):
                            exe_path = game_info['executable']
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('amazon', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for Amazon game {game.id}")

                # Get launcher script path (relative to plugin directory)
                launcher_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')

                # --- QUEUE SIZE FETCHING (background, non-blocking) ---
                # Sizes are fetched asynchronously in background, don't hold up sync
                self.size_fetcher.queue_games(all_games)
                self.size_fetcher.start()  # Fire-and-forget

                # --- STEP 1: ARTWORK (Queue & Download) ---
                # We do this BEFORE writing shortcuts so shortcuts can point to local icons
                
                artwork_count = 0
                games_needing_art = []
                steam_appid_cache = load_steam_appid_cache()  # Load existing cache
                
                # Pre-calculate app_ids for all games (fast, no I/O)
                for game in all_games:
                     game.app_id = self.shortcuts_manager.generate_app_id(game.title, launcher_script)

                # Resolve Steam presence via Steam Store API
                # Only fetch for games missing from cache or with incomplete metadata
                if all_games:
                    real_steam_cache = load_steam_real_appid_cache()
                    steam_metadata_cache = load_steam_metadata_cache()

                    # Incremental: skip games that already have a resolved Steam ID AND complete metadata
                    def _needs_steam_fetch(game) -> bool:
                        if not game.title:
                            return False
                        cached = real_steam_cache.get(game.app_id, 0)
                        if cached == -1:
                            return False  # Negative cache: already tried, no Steam match
                        if not cached:
                            return True  # No Steam ID resolved yet
                        meta = steam_metadata_cache.get(cached, {})
                        # Consider complete if it has a name (short_description may be empty for some games)
                        return not meta.get('name')

                    games_needing_steam = [g for g in all_games if _needs_steam_fetch(g)]
                    skipped_steam = len([g for g in all_games if g.title]) - len(games_needing_steam)
                    if skipped_steam > 0:
                        logger.info(f"Sync: Skipping {skipped_steam} games with complete Steam metadata (incremental)")

                    # Still assign cached steam_app_id to skipped games for later phases
                    # Skip negative cache sentinels (-1 means no Steam match)
                    for game in all_games:
                        cached_steam_id = real_steam_cache.get(game.app_id, 0)
                        if cached_steam_id and cached_steam_id > 0:
                            game.steam_app_id = cached_steam_id

                    self.sync_progress.steam_total = len(games_needing_steam)
                    self.sync_progress.steam_synced = 0
                    self.sync_progress.current_game = {
                        "label": "sync.extractingSteamMetadata",
                        "values": {"count": len(games_needing_steam)}
                    }

                    async def resolve_steam_for_game(game, semaphore):
                        async with semaphore:
                            try:
                                # Add timeout to prevent hanging
                                result = await asyncio.wait_for(
                                    self.resolve_steam_presence(game.title),
                                    timeout=15.0
                                )
                                steam_app_id = result.get('steam_appid', 0)
                                metadata = result.get('metadata', {})
                                if steam_app_id:
                                    real_steam_cache[game.app_id] = steam_app_id
                                    if metadata:
                                        steam_metadata_cache[steam_app_id] = metadata
                                else:
                                    # Negative cache: remember we tried and found no Steam match
                                    real_steam_cache[game.app_id] = -1
                            except asyncio.TimeoutError:
                                logger.warning(f"[SteamPresence] Timeout for {game.title}")
                            except Exception as e:
                                logger.debug(f"[SteamPresence] Error for {game.title}: {e}")
                            finally:
                                # Always increment progress, even on error
                                await self.sync_progress.increment_steam(game.title)

                    if games_needing_steam:
                        logger.info(f"Sync: Pre-fetching Steam metadata for {len(games_needing_steam)} games")
                        logger.debug(f"  Sample games: {', '.join([g.title for g in games_needing_steam[:5]])}")
                        semaphore = asyncio.Semaphore(self.STEAM_STORE_MAX_CONCURRENCY)
                        await asyncio.gather(*[resolve_steam_for_game(g, semaphore) for g in games_needing_steam])

                    if real_steam_cache:
                        save_steam_real_appid_cache(real_steam_cache)
                        logger.info(f"Sync: Cached Steam presence for {len(real_steam_cache)} games")
                    if steam_metadata_cache:
                        save_steam_metadata_cache(steam_metadata_cache)
                        logger.info(f"Sync: Cached Steam metadata for {len(steam_metadata_cache)} games")

                # === unifiDB METADATA FETCH (CDN) ===
                # Fetch IGDB-sourced game metadata from unifiDB via jsDelivr CDN
                if all_games:
                    logger.info(f"[SYNC PHASE] Starting unifiDB metadata fetch phase")
                    unifidb_cache = load_unifidb_metadata_cache()

                    # Incremental: only fetch for games not already in unifiDB cache
                    games_needing_unifidb = [
                        g for g in all_games
                        if g.title and g.title.lower() not in unifidb_cache
                    ]
                    skipped_unifidb = len([g for g in all_games if g.title]) - len(games_needing_unifidb)
                    if skipped_unifidb > 0:
                        logger.info(f"Sync: Skipping {skipped_unifidb} games with existing unifiDB metadata (incremental)")

                    if games_needing_unifidb:
                        logger.info(f"Sync: Fetching unifiDB metadata for {len(games_needing_unifidb)} games via CDN")
                        self.sync_progress.status = "unifidb_lookup"
                        self.sync_progress.unifidb_total = len(games_needing_unifidb)
                        self.sync_progress.unifidb_synced = 0
                        self.sync_progress.current_game = {
                            "label": "sync.lookingUpUnifiDB",
                            "values": {}
                        }

                        # Import unifiDB CDN fetcher
                        try:
                            from py_modules.unifideck.metadata.unifidb import fetch_unifidb_metadata
                        except ImportError as e:
                            logger.error(f"Failed to import unifiDB module: {e}")
                            fetch_unifidb_metadata = None

                        async def fetch_unifidb_for_game(game, semaphore):
                            async with semaphore:
                                try:
                                    if fetch_unifidb_metadata is None:
                                        await self.sync_progress.increment_unifidb(game.title)
                                        return None

                                    # Fetch from CDN
                                    cache_data = await fetch_unifidb_metadata(game.title, timeout=10.0)
                                    await self.sync_progress.increment_unifidb(game.title)

                                    if cache_data:
                                        logger.debug(f"[unifiDB CDN] Found metadata for {game.title}")
                                        return (game.title.lower(), cache_data)
                                    else:
                                        logger.debug(f"[unifiDB CDN] No match found for {game.title}")
                                        # Negative cache: store None so we don't retry next sync
                                        return (game.title.lower(), None)
                                except Exception as e:
                                    logger.warning(f"[unifiDB CDN] Error for {game.title}: {e}")
                                    await self.sync_progress.increment_unifidb(game.title)
                                return None

                        # Fetch unifiDB metadata via CDN (moderate concurrency to avoid rate limits)
                        semaphore = asyncio.Semaphore(5)
                        tasks = [fetch_unifidb_for_game(g, semaphore) for g in games_needing_unifidb]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # Build cache from results
                        new_cache_entries = {}
                        for result in results:
                            if isinstance(result, tuple) and result is not None:
                                new_cache_entries[result[0]] = result[1]

                        if new_cache_entries:
                            found_count = sum(1 for v in new_cache_entries.values() if v is not None)
                            logger.info(f"Sync: unifiDB CDN: {found_count} found, {len(new_cache_entries) - found_count} not found (cached) out of {len(games_needing_unifidb)} games")
                            unifidb_cache.update(new_cache_entries)
                            save_unifidb_metadata_cache(unifidb_cache)
                        else:
                            logger.warning(f"Sync: unifiDB CDN fetch returned no valid data for {len(games_needing_unifidb)} games")
                    else:
                        logger.info(f"Sync: No games need unifiDB lookup")


                if fetch_artwork and self.steamgriddb:
                    logger.info(f"[SYNC PHASE] Starting SGDB lookup and artwork phase")
                    # STEP 1: Identify games needing SGDB lookup (not in cache)
                    seen_app_ids = set()
                    games_needing_sgdb_lookup = []

                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            continue
                        seen_app_ids.add(game.app_id)

                        # Check cache first (skip negative cache sentinels: -1 means no SGDB match)
                        if game.app_id in steam_appid_cache:
                            cached_val = steam_appid_cache[game.app_id]
                            if cached_val > 0:
                                game.steam_app_id = cached_val
                            # else: -1 sentinel, skip lookup but don't set steam_app_id
                        else:
                            games_needing_sgdb_lookup.append(game)

                    # STEP 2: Parallel SteamGridDB lookups for uncached games
                    if games_needing_sgdb_lookup:
                        logger.info(f"Looking up {len(games_needing_sgdb_lookup)} games on SteamGridDB (parallel)...")
                        self.sync_progress.status = "sgdb_lookup"
                        self.sync_progress.current_game = {
                            "label": "sync.sgdbLookup",
                            "values": {"count": len(games_needing_sgdb_lookup)}
                        }

                        # Helper to lookup and store result
                        async def lookup_sgdb(game):
                            try:
                                sgdb_id = await self.steamgriddb.search_game(game.title)
                                if sgdb_id:
                                    game.steam_app_id = sgdb_id
                                    return (game.app_id, sgdb_id)
                                else:
                                    # Negative cache: no SGDB match found
                                    return (game.app_id, -1)
                            except Exception as e:
                                logger.debug(f"SGDB lookup failed for {game.title}: {e}")
                            return None  # Only None on exception → retried next sync

                        # 20 concurrent lookups (fast!)
                        semaphore = asyncio.Semaphore(30)
                        async def limited_lookup(game):
                            async with semaphore:
                                return await lookup_sgdb(game)

                        results = await asyncio.gather(*[limited_lookup(g) for g in games_needing_sgdb_lookup])

                        # Update cache with results
                        for result in results:
                            if result:
                                app_id, sgdb_id = result
                                steam_appid_cache[app_id] = sgdb_id
                        
                        logger.info(f"SteamGridDB lookup complete: {sum(1 for r in results if r)} found")
                    
                    # Save updated cache
                    if steam_appid_cache:
                        save_steam_appid_cache(steam_appid_cache)

                    # NOTE: Cleanup moved to AFTER shortcuts are written (line ~4480)
                    # This prevents deleting newly downloaded artwork for games not yet in old shortcuts.vdf

                    # STEP 3: Check which games need artwork (quick local file check)
                    logger.info(f"[SYNC PHASE] Checking artwork for {len(all_games)} games")
                    self.sync_progress.status = "checking_artwork"
                    self.sync_progress.current_game = {
                        "label": "sync.checkingExistingArtwork",
                        "values": {}
                    }
                    artwork_attempts = load_artwork_attempts_cache()
                    skipped_artwork_attempts = 0
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            str_id = str(game.app_id)
                            # Skip games we already tried and confirmed have no artwork available
                            if str_id in artwork_attempts and not artwork_attempts[str_id]:
                                skipped_artwork_attempts += 1
                            elif not await self.has_artwork(game.app_id):
                                games_needing_art.append(game)
                            seen_app_ids.discard(game.app_id)  # Only check once per app_id

                    if skipped_artwork_attempts > 0:
                        logger.info(f"[SYNC PHASE] Skipping {skipped_artwork_attempts} games with no available artwork (incremental)")
                    logger.info(f"[SYNC PHASE] Artwork check complete: {len(games_needing_art)}/{len(all_games)} games need artwork")

                    if games_needing_art:
                        logger.info(f"Fetching artwork for {len(games_needing_art)} games...")
                        self.sync_progress.current_phase = "artwork"
                        self.sync_progress.status = "artwork"
                        self.sync_progress.artwork_total = len(games_needing_art)
                        self.sync_progress.artwork_synced = 0

                        # Reset main counters for clean UI
                        self.sync_progress.synced_games = 0
                        self.sync_progress.total_games = 0

                        # Check cancellation
                        if self._cancel_sync:
                             logger.warning("Sync cancelled before artwork")
                             return {'success': False, 'cancelled': True}

                        # === PASS 1: Initial artwork fetch ===
                        logger.info(f"  [Pass 1] Starting parallel download (concurrency: 30)")
                        semaphore = asyncio.Semaphore(30)
                        tasks = [self.fetch_artwork_with_progress(game, semaphore) for game in games_needing_art]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # Count successes (results are now dicts)
                        pass1_success = sum(1 for r in results if isinstance(r, dict) and r.get('success'))
                        logger.info(f"  [Pass 1] Complete: {pass1_success}/{len(games_needing_art)} games")

                        # === PASS 2: Retry games with incomplete artwork ===
                        games_to_retry = []
                        for game in games_needing_art:
                            missing = await self.get_missing_artwork_types(game.app_id)
                            if missing:
                                games_to_retry.append(game)

                        if games_to_retry and not self._cancel_sync:
                            logger.info(f"  [Pass 2] Retrying {len(games_to_retry)} games with incomplete artwork")
                            self.sync_progress.status = "artwork_retry"
                            self.sync_progress.current_game = {
                                "label": "sync.retryingMissingArtwork",
                                "values": {"count": len(games_to_retry)}
                            }
                            self.sync_progress.artwork_total = len(games_to_retry)
                            self.sync_progress.artwork_synced = 0

                            retry_tasks = [self.fetch_artwork_with_progress(game, semaphore) for game in games_to_retry]
                            await asyncio.gather(*retry_tasks, return_exceptions=True)

                            # Count recovered games (can't use await in generator expression)
                            pass2_recovered = 0
                            for g in games_to_retry:
                                if not await self.get_missing_artwork_types(g.app_id):
                                    pass2_recovered += 1
                            logger.info(f"  [Pass 2] Recovered: {pass2_recovered}/{len(games_to_retry)} games")

                        # Log still-incomplete games for debugging
                        still_incomplete = []
                        for g in games_needing_art:
                            missing = await self.get_missing_artwork_types(g.app_id)
                            if missing:
                                still_incomplete.append((g.title, missing))
                        if still_incomplete:
                            logger.warning(f"  Artwork incomplete for {len(still_incomplete)} games after 2 passes")
                            for title, missing in still_incomplete[:5]:  # Log first 5
                                logger.debug(f"    - {title}: missing {missing}")

                        artwork_count = len(games_needing_art) - len(still_incomplete)
                        logger.info(f"Artwork download complete: {artwork_count}/{len(games_needing_art)} games fully successful")

                        # Save artwork attempt results for incremental sync
                        for game in games_needing_art:
                            has_art = await self.has_artwork(game.app_id)
                            artwork_attempts[str(game.app_id)] = has_art
                        save_artwork_attempts_cache(artwork_attempts)
                    else:
                        logger.info(f"[SYNC PHASE] All games have complete artwork, skipping download")

                # --- STEP 2: UPDATE GAME ICONS ---
                logger.info(f"[SYNC PHASE] Updating game icons")
                # Check for local icons and update game objects
                if self.steamgriddb and self.steamgriddb.grid_path:
                    grid_path = Path(self.steamgriddb.grid_path)
                    for game in all_games:
                        # Convert signed int32 to unsigned for filename check
                        unsigned_id = game.app_id if game.app_id >= 0 else game.app_id + 2**32
                        icon_path = grid_path / f"{unsigned_id}_icon.jpg"
                        
                        if icon_path.exists():
                             # If we have a local icon, use it!
                             game.cover_image = str(icon_path)
                             # shortcuts_manager.add_games_batch will use game.cover_image for the 'icon' field
                
                # --- STEP 3: WRITE SHORTCUTS ---
                logger.info(f"[SYNC PHASE] Writing shortcuts for {len(all_games)} games")
                self.sync_progress.status = "syncing"
                self.sync_progress.current_game = {
                    "label": "sync.savingShortcuts",
                    "values": {}
                }
                
                # Cull any duplicates Steam may have introduced since last sync
                await self.shortcuts_manager.deduplicate_shortcuts()
                
                # Use valid_stores to prevent deleting shortcuts for stores that failed to sync
                batch_result = await self.shortcuts_manager.add_games_batch(all_games, launcher_script, valid_stores=valid_stores)
                added_count = batch_result.get('added', 0)
                
                if batch_result.get('error'):
                     raise Exception(batch_result['error'])

                # Complete
                logger.info(f"[SYNC PHASE] Sync complete - Added: {added_count}, Artwork: {artwork_count}")
                self.sync_progress.status = "complete"
                self.sync_progress.synced_games = len(all_games)
                self.sync_progress.current_game = {
                    "label": "sync.completed",
                    "values": {"added": added_count, "artwork": artwork_count}
                }

                if added_count > 0 or artwork_count > 0:
                    logger.warning("=" * 60)
                    logger.warning("IMPORTANT: Steam restart required!")
                    logger.warning("Please EXIT Steam completely and restart to see changes")
                    logger.warning("=" * 60)

                return {
                    'success': True,
                    'epic_count': len(epic_games),
                    'gog_count': len(gog_games),
                    'amazon_count': len(amazon_games),
                    'added_count': added_count,
                    'artwork_count': artwork_count
                }

            except Exception as e:
                logger.error(f"Error syncing libraries: {e}")
                self.sync_progress.status = "error"
                self.sync_progress.error = str(e)
                return {'success': False, 'error': str(e)}

            finally:
                self._is_syncing = False
                self._cancel_sync = False

    async def force_sync_libraries(self, resync_artwork: bool = False) -> Dict[str, Any]:
        """
        Force sync all libraries - rewrites ALL existing Unifideck shortcuts and compatibility data.
        Optionally re-downloads artwork if resync_artwork=True (overwrites manual changes).
        
        This is useful when:
        - Shortcut exe paths need to be updated
        - Compatibility/Proton settings need to be refreshed
        - Games were installed externally and need proper configuration
        """
        # Check if sync already running (non-blocking check)
        if self._is_syncing:
            logger.warning("Sync already in progress, ignoring force sync request")
            return {
                'success': False,
                'error': 'errors.syncInProgress',
                'epic_count': 0,
                'gog_count': 0,
                'added_count': 0,
                'updated_count': 0,
                'artwork_count': 0
            }

        # Acquire lock (prevents concurrent syncs)
        async with self._sync_lock:
            self._is_syncing = True
            self._cancel_sync = False  # Reset cancel flag
            try:
                logger.info("Force syncing libraries (rewriting all shortcuts and compatibility data)...")

                # Reset all progress state from previous sync
                self.sync_progress.reset()

                # Backup existing caches before force sync (safety measure)
                # Caches will only be overwritten after successful fetch
                logger.info("Force Sync: Backing up existing metadata caches...")
                _backup_cache_file(get_steam_real_appid_cache_path())
                _backup_cache_file(get_steam_metadata_cache_path())
                _backup_cache_file(get_unifidb_metadata_cache_path())
                _backup_cache_file(get_metacritic_metadata_cache_path())
                logger.info("Force Sync: Cache backups created (*.bak files)")

                # Clear negative cache entries so force sync retries everything
                logger.info("Force Sync: Clearing negative cache entries for fresh retry...")
                _real_steam = load_steam_real_appid_cache()
                _neg_steam = sum(1 for v in _real_steam.values() if v == -1)
                if _neg_steam > 0:
                    _real_steam = {k: v for k, v in _real_steam.items() if v != -1}
                    save_steam_real_appid_cache(_real_steam)
                    logger.info(f"Force Sync: Cleared {_neg_steam} negative Steam presence entries")

                _unifidb = load_unifidb_metadata_cache()
                _neg_unifidb = sum(1 for v in _unifidb.values() if v is None)
                if _neg_unifidb > 0:
                    _unifidb = {k: v for k, v in _unifidb.items() if v is not None}
                    save_unifidb_metadata_cache(_unifidb)
                    logger.info(f"Force Sync: Cleared {_neg_unifidb} negative unifiDB entries")

                _sgdb = load_steam_appid_cache()
                _neg_sgdb = sum(1 for v in _sgdb.values() if v == -1)
                if _neg_sgdb > 0:
                    _sgdb = {k: v for k, v in _sgdb.items() if v != -1}
                    save_steam_appid_cache(_sgdb)
                    logger.info(f"Force Sync: Cleared {_neg_sgdb} negative SGDB entries")

                _art_path = get_artwork_attempts_cache_path()
                if _art_path.exists():
                    _art_path.unlink()
                    logger.info("Force Sync: Cleared artwork attempts cache")
                
                self.sync_progress.status = "fetching"
                self.sync_progress.current_game = {
                    "label": "force_sync.migratingOldInstallations",
                    "values": {}
                }

                # Migrate old GOG .unifideck-id markers to new JSON format
                try:
                    migration_result = self.gog.migrate_old_markers()
                    if migration_result.get('migrated', 0) > 0:
                        logger.info(f"[ForceSync] Migrated {migration_result['migrated']} GOG markers to new format")
                except Exception as e:
                    logger.warning(f"[ForceSync] Marker migration failed: {e}")

                self.sync_progress.current_game = {
                    "label": "force_sync.fetchingGameLists",
                    "values": {}
                }

                # Get games from all stores
                epic_games = await self.epic.get_library()
                gog_games = await self.gog.get_library()
                amazon_games = await self.amazon.get_library()

                # Robustly handle API failures (None returns)
                valid_stores = []
                all_games = []

                if epic_games is not None:
                    valid_stores.append('epic')
                    all_games.extend(epic_games)
                else:
                    epic_games = []

                if gog_games is not None:
                    valid_stores.append('gog')
                    all_games.extend(gog_games)
                else:
                    gog_games = []

                if amazon_games is not None:
                    valid_stores.append('amazon')
                    all_games.extend(amazon_games)
                else:
                    amazon_games = []
                self.sync_progress.total_games = len(all_games)
                self.sync_progress.synced_games = 0

                # Queue games for background compat fetching (ProtonDB/Deck Verified)
                logger.info("Force sync: Queueing games for compatibility lookup...")
                self.compat_fetcher.queue_games(all_games)
                self.compat_fetcher.start()  # Non-blocking background fetch

                # Update progress: Checking installed status
                self.sync_progress.status = "checking_installed"
                self.sync_progress.current_game = {
                    "label": "force_sync.checkingInstalledGames",
                    "values": {}
                }

                # Get installed games
                epic_installed = await self.epic.get_installed()
                gog_installed = await self.gog.get_installed()
                amazon_installed = await self.amazon.get_installed()

                # Mark installed status and update games.map
                for game in epic_games:
                    if game.id in epic_installed:
                        game.is_installed = True
                        
                        # OPTIMIZATION: Use cached metadata to get EXE path instead of slow subprocess
                        # epic_installed is now a dict: {app_name: metadata}
                        metadata = epic_installed[game.id]
                        
                        install_path = metadata.get('install', {}).get('install_path')
                        executable = metadata.get('manifest', {}).get('launch_exe')
                        
                        exe_path = None
                        if install_path and executable:
                            exe_path = os.path.join(install_path, executable)
                        elif metadata.get('install_path') and metadata.get('executable'):
                            # Fallback structure
                            exe_path = os.path.join(metadata['install_path'], metadata['executable'])
                            
                        if exe_path:
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('epic', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for Epic game {game.id} (FAST)")

                for game in gog_games:
                    if game.id in gog_installed:
                        game.is_installed = True
                        game_info = self.gog.get_installed_game_info(game.id)
                        if game_info and game_info.get('executable'):
                            exe_path = game_info['executable']
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('gog', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for GOG game {game.id}")

                for game in amazon_games:
                    if game.id in amazon_installed:
                        game.is_installed = True
                        game_info = self.amazon.get_installed_game_info(game.id)
                        if game_info and game_info.get('executable'):
                            exe_path = game_info['executable']
                            work_dir = os.path.dirname(exe_path)
                            await self.shortcuts_manager._update_game_map('amazon', game.id, exe_path, work_dir)
                            logger.debug(f"Updated games.map for Amazon game {game.id}")

                # Get launcher script path
                launcher_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')

                # --- QUEUE SIZE FETCHING (background, non-blocking) ---
                # Force sync re-fetches all sizes to fix any stale/incorrect values
                self.size_fetcher.queue_games(all_games, force_refresh=True)
                self.size_fetcher.start()  # Fire-and-forget

                # Update progress: Force syncing
                self.sync_progress.status = "syncing"
                self.sync_progress.current_game = {
                    "label": "force_sync.rewritingShortcuts",
                    "values": {}
                }

                # Cull any duplicates Steam may have introduced since last sync
                await self.shortcuts_manager.deduplicate_shortcuts()
                
                # Force update all games - rewrite existing shortcuts
                batch_result = await self.shortcuts_manager.force_update_games_batch(all_games, launcher_script, valid_stores=valid_stores, epic_client=self.epic, gog_client=self.gog, amazon_client=self.amazon)
                added_count = batch_result.get('added', 0)
                updated_count = batch_result.get('updated', 0)

                if batch_result.get('error'):
                    logger.error(f"Force batch update failed: {batch_result['error']}")
                    self.sync_progress.status = "error"
                    self.sync_progress.error = batch_result['error']
                    return {'success': False, 'error': batch_result['error']}

                logger.info(f"Force batch write complete: {added_count} games added, {updated_count} updated")

                # Check for cancellation after shortcuts written
                if self._cancel_sync:
                    logger.warning("Force sync cancelled by user after writing shortcuts")
                    self.sync_progress.status = "cancelled"
                    self.sync_progress.current_game = {
                        "label": "force_sync.cancelledShortcutsSaved",
                        "values": {}
                    }
                    return {
                        'success': True,
                        'cancelled': True,
                        'epic_count': len(epic_games),
                        'gog_count': len(gog_games),
                        'amazon_count': len(amazon_games),
                        'added_count': added_count,
                        'updated_count': updated_count,
                        'artwork_count': 0
                    }

                # Get launcher script path (relative to plugin directory)
                launcher_script = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')
                
                # --- STEP 1: ARTWORK (Queue & Download) ---
                # We do this BEFORE writing shortcuts so shortcuts can point to local icons
                
                artwork_count = 0
                games_needing_art = []
                steam_appid_cache = load_steam_appid_cache()  # Load existing cache
                
                # Pre-calculate app_ids for all games
                for game in all_games:
                     game.app_id = self.shortcuts_manager.generate_app_id(game.title, launcher_script)

                # Resolve Steam presence via Steam Store API
                if all_games:
                    real_steam_cache = load_steam_real_appid_cache()
                    steam_metadata_cache = load_steam_metadata_cache()

                    games_needing_steam = [g for g in all_games if g.title]

                    self.sync_progress.steam_total = len(games_needing_steam)
                    self.sync_progress.steam_synced = 0
                    self.sync_progress.current_game = {
                        "label": "sync.extractingSteamMetadata",
                        "values": {"count": len(games_needing_steam)}
                    }

                    async def resolve_steam_for_game(game, semaphore):
                        async with semaphore:
                            try:
                                # Add timeout to prevent hanging
                                result = await asyncio.wait_for(
                                    self.resolve_steam_presence(game.title),
                                    timeout=15.0
                                )
                                steam_app_id = result.get('steam_appid', 0)
                                metadata = result.get('metadata', {})
                                if steam_app_id:
                                    real_steam_cache[game.app_id] = steam_app_id
                                    if metadata:
                                        steam_metadata_cache[steam_app_id] = metadata
                                else:
                                    # Negative cache: remember we tried and found no Steam match
                                    real_steam_cache[game.app_id] = -1
                            except asyncio.TimeoutError:
                                logger.warning(f"[SteamPresence] Timeout for {game.title}")
                            except Exception as e:
                                logger.debug(f"[SteamPresence] Error for {game.title}: {e}")
                            finally:
                                # Always increment progress, even on error
                                await self.sync_progress.increment_steam(game.title)

                    if games_needing_steam:
                        semaphore = asyncio.Semaphore(self.STEAM_STORE_MAX_CONCURRENCY)
                        await asyncio.gather(*[resolve_steam_for_game(g, semaphore) for g in games_needing_steam])

                    if real_steam_cache:
                        save_steam_real_appid_cache(real_steam_cache)
                    if steam_metadata_cache:
                        save_steam_metadata_cache(steam_metadata_cache)

                # === unifiDB METADATA FETCH (Force Sync - CDN) ===
                # Fetch IGDB-sourced game metadata from unifiDB via jsDelivr CDN
                if all_games:
                    logger.info(f"[FORCE SYNC PHASE] Starting unifiDB metadata fetch phase")
                    unifidb_cache = load_unifidb_metadata_cache()
                    games_needing_unifidb = [g for g in all_games if g.title]

                    if games_needing_unifidb:
                        logger.info(f"Force Sync: Fetching unifiDB metadata for {len(games_needing_unifidb)} games via CDN")
                        self.sync_progress.status = "unifidb_lookup"
                        self.sync_progress.unifidb_total = len(games_needing_unifidb)
                        self.sync_progress.unifidb_synced = 0
                        self.sync_progress.current_game = {
                            "label": "sync.lookingUpUnifiDB",
                            "values": {}
                        }

                        # Import unifiDB CDN fetcher
                        try:
                            from py_modules.unifideck.metadata.unifidb import fetch_unifidb_metadata
                        except ImportError as e:
                            logger.error(f"Failed to import unifiDB module: {e}")
                            fetch_unifidb_metadata = None

                        async def fetch_unifidb_for_game(game, semaphore):
                            async with semaphore:
                                try:
                                    if fetch_unifidb_metadata is None:
                                        await self.sync_progress.increment_unifidb(game.title)
                                        return None

                                    # Fetch from CDN
                                    cache_data = await fetch_unifidb_metadata(game.title, timeout=10.0)
                                    await self.sync_progress.increment_unifidb(game.title)

                                    if cache_data:
                                        logger.debug(f"[unifiDB CDN] Found metadata for {game.title}")
                                        return (game.title.lower(), cache_data)
                                    else:
                                        logger.debug(f"[unifiDB CDN] No match found for {game.title}")
                                        # Negative cache: store None so we don't retry next sync
                                        return (game.title.lower(), None)
                                except Exception as e:
                                    logger.warning(f"[unifiDB CDN] Error for {game.title}: {e}")
                                    await self.sync_progress.increment_unifidb(game.title)
                                return None

                        # Fetch unifiDB metadata via CDN (moderate concurrency to avoid rate limits)
                        semaphore = asyncio.Semaphore(5)
                        tasks = [fetch_unifidb_for_game(g, semaphore) for g in games_needing_unifidb]
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        # Build cache from results
                        new_cache_entries = {}
                        for result in results:
                            if isinstance(result, tuple) and result is not None:
                                new_cache_entries[result[0]] = result[1]

                        if new_cache_entries:
                            found_count = sum(1 for v in new_cache_entries.values() if v is not None)
                            logger.info(f"Force Sync: unifiDB CDN: {found_count} found, {len(new_cache_entries) - found_count} not found (cached) out of {len(games_needing_unifidb)} games")
                            unifidb_cache.update(new_cache_entries)
                            save_unifidb_metadata_cache(unifidb_cache)
                        else:
                            logger.warning(f"Force Sync: unifiDB CDN fetch returned no valid data for {len(games_needing_unifidb)} games")
                    else:
                        logger.info(f"Force Sync: No games need unifiDB lookup")


                if self.steamgriddb:
                    logger.info(f"[FORCE SYNC PHASE] Starting SGDB lookup and artwork phase")
                    # STEP 1: Identify games needing SGDB lookup (not in cache)
                    seen_app_ids = set()
                    games_needing_sgdb_lookup = []

                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            continue
                        seen_app_ids.add(game.app_id)

                        # Check cache first (skip negative cache sentinels: -1 means no SGDB match)
                        if game.app_id in steam_appid_cache:
                            cached_val = steam_appid_cache[game.app_id]
                            if cached_val > 0:
                                game.steam_app_id = cached_val
                            # else: -1 sentinel, skip lookup but don't set steam_app_id
                        else:
                            games_needing_sgdb_lookup.append(game)

                    # STEP 2: Parallel SteamGridDB lookups for uncached games
                    if games_needing_sgdb_lookup:
                        logger.info(f"Force Sync: Looking up {len(games_needing_sgdb_lookup)} games on SteamGridDB (parallel)...")
                        self.sync_progress.status = "sgdb_lookup"
                        self.sync_progress.current_game = {
                            "label": "sync.sgdbLookup",
                            "values": {
                                "count": len(games_needing_sgdb_lookup)
                            }
                        }

                        # Helper to lookup and store result
                        async def lookup_sgdb(game):
                            try:
                                sgdb_id = await self.steamgriddb.search_game(game.title)
                                if sgdb_id:
                                    game.steam_app_id = sgdb_id
                                    return (game.app_id, sgdb_id)
                                else:
                                    # Negative cache: no SGDB match found
                                    return (game.app_id, -1)
                            except Exception as e:
                                logger.debug(f"SGDB lookup failed for {game.title}: {e}")
                            return None  # Only None on exception → retried next sync
                        
                        # 30 concurrent lookups (10 per source × 3 sources)
                        semaphore = asyncio.Semaphore(30)
                        async def limited_lookup(game):
                            async with semaphore:
                                return await lookup_sgdb(game)
                        
                        results = await asyncio.gather(*[limited_lookup(g) for g in games_needing_sgdb_lookup])
                        
                        # Update cache with results
                        for result in results:
                            if result:
                                app_id, sgdb_id = result
                                steam_appid_cache[app_id] = sgdb_id
                        
                        logger.info(f"SteamGridDB lookup complete: {sum(1 for r in results if r)} found")
                    
                    # Save updated cache
                    if steam_appid_cache:
                        save_steam_appid_cache(steam_appid_cache)

                    # Cleanup orphaned artwork before sync (prevents duplicate files)
                    # Only run when resync_artwork=True — skipping protects manually customized artwork
                    if resync_artwork:
                        self.sync_progress.current_game = {
                            "label": "sync.cleaningOrphanedArtwork",
                            "values": {}
                        }
                        cleanup_result = await self.cleanup_orphaned_artwork()
                        if cleanup_result.get('removed_count', 0) > 0:
                            logger.info(f"Cleaned up {cleanup_result['removed_count']} orphaned artwork files")

                    # ARTWORK: Download based on user preference
                    # If resync_artwork=True, re-download ALL artwork (overwrites everything)
                    # If resync_artwork=False, fill gaps only (download missing types per game)
                    logger.info(f"[FORCE SYNC PHASE] Checking artwork for {len(all_games)} games (resync_artwork={resync_artwork})")
                    self.sync_progress.status = "checking_artwork"
                    self.sync_progress.current_game = {
                        "label": "sync.queueRefresh" if resync_artwork else "sync.checking_artwork",
                        "values": {}
                    }
                    
                    # Build list of games needing art + their missing types
                    # This dict maps app_id -> set of missing artwork types
                    games_missing_types = {}
                    
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            if resync_artwork:
                                # Full resync: all games get all types downloaded
                                games_needing_art.append(game)
                                games_missing_types[game.app_id] = None  # None = all types
                            else:
                                # Gap-fill: only download missing types
                                missing = await self.get_missing_artwork_types(game.app_id)
                                if missing:
                                    games_needing_art.append(game)
                                    games_missing_types[game.app_id] = missing
                            seen_app_ids.discard(game.app_id)  # Only add once per app_id

                    logger.info(f"[FORCE SYNC PHASE] Artwork check complete: {len(games_needing_art)}/{len(all_games)} games need artwork")
                    if not resync_artwork and games_missing_types:
                        total_missing = sum(len(types) for types in games_missing_types.values() if types)
                        logger.info(f"[FORCE SYNC PHASE] Gap-fill mode: {total_missing} total missing types to download across {len(games_needing_art)} games")

                    if games_needing_art:
                            logger.info(f"Force Sync: Fetching artwork for {len(games_needing_art)} games...")
                            self.sync_progress.current_phase = "artwork"
                            self.sync_progress.status = "artwork"
                            self.sync_progress.artwork_total = len(games_needing_art)
                            self.sync_progress.artwork_synced = 0

                            # Reset main counters for clean UI
                            self.sync_progress.synced_games = 0
                            self.sync_progress.total_games = 0

                            # Check cancellation
                            if self._cancel_sync:
                                 logger.warning("Force Sync cancelled before artwork")
                                 return {'success': False, 'cancelled': True}

                            # === PASS 1: Initial artwork fetch ===
                            logger.info(f"  [Pass 1] Starting parallel download (concurrency: 30)")
                            semaphore = asyncio.Semaphore(30)
                            tasks = []
                            for game in games_needing_art:
                                # Pass only_types if in gap-fill mode (resync_artwork=False)
                                only_types = games_missing_types.get(game.app_id) if not resync_artwork else None
                                tasks.append(self.fetch_artwork_with_progress(game, semaphore, only_types=only_types))
                            results = await asyncio.gather(*tasks, return_exceptions=True)

                            # Count successes (results are now dicts)
                            pass1_success = sum(1 for r in results if isinstance(r, dict) and r.get('success'))
                            logger.info(f"  [Pass 1] Complete: {pass1_success}/{len(games_needing_art)} games")

                            # === PASS 2: Retry games with incomplete artwork ===
                            games_to_retry = []
                            for game in games_needing_art:
                                missing = await self.get_missing_artwork_types(game.app_id)
                                if missing:
                                    games_to_retry.append(game)

                            if games_to_retry and not self._cancel_sync:
                                logger.info(f"  [Pass 2] Retrying {len(games_to_retry)} games with incomplete artwork")
                                self.sync_progress.status = "artwork_retry"
                                self.sync_progress.current_game = {
                                    "label": "sync.retryingMissingArtwork",
                                    "values": {"count": len(games_to_retry)}
                                }
                                self.sync_progress.artwork_total = len(games_to_retry)
                                self.sync_progress.artwork_synced = 0

                                retry_tasks = []
                                for game in games_to_retry:
                                    # Pass only_types if in gap-fill mode (resync_artwork=False)
                                    only_types = games_missing_types.get(game.app_id) if not resync_artwork else None
                                    retry_tasks.append(self.fetch_artwork_with_progress(game, semaphore, only_types=only_types))
                                await asyncio.gather(*retry_tasks, return_exceptions=True)

                                # Count recovered games (can't use await in generator expression)
                                pass2_recovered = 0
                                for g in games_to_retry:
                                    if not await self.get_missing_artwork_types(g.app_id):
                                        pass2_recovered += 1
                                logger.info(f"  [Pass 2] Recovered: {pass2_recovered}/{len(games_to_retry)} games")

                            # Log still-incomplete games for debugging
                            still_incomplete = []
                            for g in games_needing_art:
                                missing = await self.get_missing_artwork_types(g.app_id)
                                if missing:
                                    still_incomplete.append((g.title, missing))
                            if still_incomplete:
                                logger.warning(f"  Artwork incomplete for {len(still_incomplete)} games after 2 passes")
                                for title, missing in still_incomplete[:5]:  # Log first 5
                                    logger.debug(f"    - {title}: missing {missing}")

                            artwork_count = len(games_needing_art) - len(still_incomplete)
                            logger.info(f"Artwork download complete: {artwork_count}/{len(games_needing_art)} games fully successful")
                    else:
                        logger.info(f"[FORCE SYNC PHASE] No games need artwork (resync_artwork={resync_artwork})")
                        artwork_count = 0

                # --- UPDATE GAME ICONS ---
                # Check for local icons and update game objects
                if self.steamgriddb and self.steamgriddb.grid_path:
                    grid_path = Path(self.steamgriddb.grid_path)
                    for game in all_games:
                        # Convert signed int32 to unsigned for filename check
                        unsigned_id = game.app_id if game.app_id >= 0 else game.app_id + 2**32
                        icon_path = grid_path / f"{unsigned_id}_icon.jpg"
                        
                        if icon_path.exists():
                             # If we have a local icon, use it!
                             game.cover_image = str(icon_path)

                # --- STEP 3: WRITE SHORTCUTS ---
                self.sync_progress.current_game = {
                    "label": "force_sync.writingShortcuts",
                    "values": {}
                }
                
                # Force update all games - rewrites existing shortcuts with fresh data (and local icons)
                force_result = await self.shortcuts_manager.force_update_games_batch(all_games, launcher_script, epic_client=self.epic, gog_client=self.gog, amazon_client=self.amazon)
                
                added_count = force_result.get('added', 0)
                updated_count = force_result.get('updated', 0)
                
                if force_result.get('error'):
                    raise Exception(force_result['error'])

                # CLEAR Proton compatibility for all Unifideck games
                # The unifideck-launcher script handles Proton internally via umu-run
                # We DON'T want Steam to wrap our launcher in Proton (causes Python path issues)
                self.sync_progress.status = "proton_setup"
                self.sync_progress.current_game = {
                    "label": "force_sync.clearingProton",
                    "values": {}
                }
                proton_cleared = 0
                
                shortcuts_data = await self.shortcuts_manager.read_shortcuts()
                for idx, shortcut in shortcuts_data.get('shortcuts', {}).items():
                    launch_opts = shortcut.get('LaunchOptions', '')
                    app_id = shortcut.get('appid')
                    
                    # Check if this is a Unifideck game (has store:game_id format in LaunchOptions)
                    if is_unifideck_shortcut(launch_opts) and app_id:
                        await self.shortcuts_manager._clear_proton_compatibility(app_id)
                        proton_cleared += 1
                
                logger.info(f"Cleared Proton compatibility for {proton_cleared} games (launcher manages Proton via umu-run)")

                # Complete
                logger.info(f"[FORCE SYNC PHASE] Force sync complete - Updated: {updated_count}, Artwork: {artwork_count}")
                self.sync_progress.status = "complete"
                self.sync_progress.synced_games = len(all_games)
                self.sync_progress.current_game = {
                    "label": "force_sync.completed",
                    "values": {"added": added_count, "updated": updated_count, "artwork": artwork_count}
                }

                # Notify about Steam restart requirement
                if added_count > 0 or updated_count > 0 or artwork_count > 0:
                    logger.warning("=" * 60)
                    logger.warning("IMPORTANT: Steam restart required!")
                    logger.warning("Please EXIT Steam completely and restart to see changes")
                    logger.warning("=" * 60)

                logger.info(f"Force synced {len(epic_games)} Epic + {len(gog_games)} GOG + {len(amazon_games)} Amazon games ({added_count} added, {updated_count} updated, {artwork_count} artwork)")
                return {
                    'success': True,
                    'epic_count': len(epic_games),
                    'gog_count': len(gog_games),
                    'amazon_count': len(amazon_games),
                    'added_count': added_count,
                    'updated_count': updated_count,
                    'artwork_count': artwork_count
                }

            except Exception as e:
                logger.error(f"Error force syncing libraries: {e}")
                self.sync_progress.status = "error"
                self.sync_progress.error = str(e)
                return {'success': False, 'error': str(e)}

            finally:
                self._is_syncing = False
                self._cancel_sync = False

    async def start_background_sync(self) -> Dict[str, Any]:
        """Start background sync service"""
        if self.background_sync:
            await self.background_sync.start()
            return {'success': True}
        else:
            return {'success': False, 'error': 'errors.backgroundSyncDisabled'}

    async def stop_background_sync(self) -> Dict[str, Any]:
        """Stop background sync service"""
        if self.background_sync:
            await self.background_sync.stop()
            return {'success': True}
        else:
            return {'success': False, 'error': 'errors.backgroundSyncDisabled'}

    async def get_sync_progress(self) -> Dict[str, Any]:
        """Get current sync progress for frontend polling"""
        return self.sync_progress.to_dict()

    async def get_sync_status(self) -> Dict[str, Any]:
        """Check if a sync operation is currently running"""
        return {
            'is_syncing': self._is_syncing,
            'sync_progress': self.sync_progress.to_dict() if self._is_syncing else None
        }

    async def cancel_sync(self) -> Dict[str, Any]:
        """Request cancellation of current sync operation"""
        if not self._is_syncing:
            return {
                'success': False,
                'message': 'No sync operation in progress'
            }

        logger.warning("Sync cancellation requested by user")
        self._cancel_sync = True
        return {
            'success': True,
            'message': 'Sync cancellation requested - will stop after current operation'
        }

    async def _get_epic_executable(self, game_id: str) -> Optional[str]:
        """Get executable path for Epic game using legendary info

        Args:
            game_id: Epic game app_name (ID)

        Returns:
            Full path to game executable, or None if not found
        """
        if not self.epic.legendary_bin:
            logger.warning("[Epic] Legendary binary not found, cannot get executable path")
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                self.epic.legendary_bin, 'info', game_id, '--json',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                info = json.loads(stdout.decode())
                install_path = info.get('install', {}).get('install_path', '')
                executable = info.get('manifest', {}).get('launch_exe', '')

                if install_path and executable:
                    exe_path = os.path.join(install_path, executable)
                    logger.info(f"[Epic] Found executable: {exe_path}")
                    return exe_path
                else:
                    logger.warning(f"[Epic] Missing install_path or executable in legendary info for {game_id}")
            else:
                error_msg = stderr.decode() if stderr else 'Unknown error'
                logger.warning(f"[Epic] legendary info failed for {game_id}: {error_msg}")

        except Exception as e:
            logger.error(f"[Epic] Error getting executable for {game_id}: {e}", exc_info=True)

        return None

    async def install_game(self, game_id: str, store: str) -> Dict[str, Any]:
        """Install a game from specified store"""
        logger.info(f"Installing {game_id} from {store}")

        try:
            if store == 'epic':
                return await self.install_handler.install_epic_game(game_id)
            elif store == 'gog':
                return await self.install_handler.install_gog_game(game_id)
            else:
                return {'success': False, 'error': f'Unknown store: {store}'}

        except Exception as e:
            logger.error(f"Error installing game: {e}")
            return {'success': False, 'error': str(e)}


    async def sync_cloud_saves(self, store: str, game_id: str, direction: str = "download", 
                               game_name: str = "", save_path: str = "") -> Dict[str, Any]:
        """
        Sync cloud saves for a game.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID (Epic app_name or GOG client_id)
            direction: "download" (pull from cloud) or "upload" (push to cloud)
            game_name: Optional game title for logging
            save_path: For GOG games, the local save path (required)
        
        Returns:
            {success, message, duration, error?}
        """
        logger.info(f"[API] sync_cloud_saves called: store={store}, game_id={game_id}, direction={direction}")
        
        try:
            if store == "epic":
                return await self.cloud_save_manager.sync_epic(game_id, direction=direction, game_name=game_name)
            elif store == "gog":
                return await self.cloud_save_manager.sync_gog(game_id, save_path=save_path, 
                                                              direction=direction, game_name=game_name)
            else:
                error = f"Unknown store: {store}"
                logger.error(f"[API] {error}")
                return {"success": False, "error": error}
        except Exception as e:
            logger.error(f"[API] sync_cloud_saves error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def get_cloud_save_status(self, store: str, game_id: str) -> Dict[str, Any]:
        """
        Get the last cloud save sync status for a game.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
        
        Returns:
            {last_sync, status, direction, error?} or None if never synced
        """
        logger.info(f"[API] get_cloud_save_status called: store={store}, game_id={game_id}")
        
        status = self.cloud_save_manager.get_sync_status(store, game_id)
        if status:
            return {"success": True, "status": status}
        return {"success": True, "status": None}
    
    async def check_cloud_save_conflict(self, store: str, game_id: str, 
                                        save_path: str = "") -> Dict[str, Any]:
        """
        Check if a game has cloud save conflicts.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
            save_path: Local save path (for file timestamp comparison)
        
        Returns:
            {has_conflict, is_fresh, local_timestamp, cloud_timestamp, local_newer}
        """
        logger.info(f"[API] check_cloud_save_conflict: {store}/{game_id}")
        
        try:
            conflict = await self.cloud_save_manager.check_for_conflicts(store, game_id, save_path)
            return {"success": True, "conflict": conflict}
        except Exception as e:
            logger.error(f"[API] check_cloud_save_conflict error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def resolve_cloud_save_conflict(self, store: str, game_id: str, 
                                          use_cloud: bool) -> Dict[str, Any]:
        """
        Resolve a cloud save conflict.
        
        Args:
            store: "epic" or "gog"
            game_id: Game ID
            use_cloud: True to use cloud saves, False to upload local saves
        
        Returns:
            {action: "download" or "upload"}
        """
        logger.info(f"[API] resolve_cloud_save_conflict: {store}/{game_id}, use_cloud={use_cloud}")
        
        try:
            result = self.cloud_save_manager.resolve_conflict(store, game_id, use_cloud)
            return {"success": True, **result}
        except Exception as e:
            logger.error(f"[API] resolve_cloud_save_conflict error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def start_game_monitor(self, pid: int, store: str, game_id: str,
                                  game_name: str = "", save_path: str = "") -> Dict[str, Any]:
        """
        Start monitoring a game process for cloud save sync on exit.
        
        Args:
            pid: Process ID of the running game
            store: "epic" or "gog"
            game_id: Game ID
            game_name: Game title for logging
            save_path: Local save path (for GOG)
        
        Returns:
            {success, message}
        """
        logger.info(f"[API] start_game_monitor: PID={pid}, {store}/{game_id}")
        
        try:
            success = await self.cloud_save_manager.process_monitor.start_monitoring(
                pid, store, game_id, game_name, save_path
            )
            return {
                "success": success,
                "message": f"Monitoring PID {pid}" if success else f"Process {pid} not found"
            }
        except Exception as e:
            logger.error(f"[API] start_game_monitor error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def get_pending_conflicts(self) -> Dict[str, Any]:
        """
        Get all pending cloud save conflicts that need resolution.
        
        Returns:
            {conflicts: {store:game_id: conflict_info}}
        """
        logger.info("[API] get_pending_conflicts called")
        
        try:
            conflicts = self.cloud_save_manager.get_pending_conflicts()
            return {"success": True, "conflicts": conflicts}
        except Exception as e:
            logger.error(f"[API] get_pending_conflicts error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def check_game_installation_status(self, store: str, game_id: str) -> bool:
        """
        **SINGLE SOURCE OF TRUTH** for checking if a game is installed.
        
        games.map + .unifideck-id work together:
        - .unifideck-id is written LAST on successful install (100% complete)
        - games.map is updated when .unifideck-id is verified
        - If not in games.map -> not installed
        
        Games installed outside Unifideck are picked up during Force Sync.
        """
        return self.shortcuts_manager._is_in_game_map(store, game_id)

    async def get_game_info(self, app_id: int) -> Dict[str, Any]:
        """Get game info including installation status and size

        Args:
            app_id: Steam shortcut app ID (can be signed or unsigned)

        Returns:
            Dict with game info: {
                'is_installed': bool,
                'store': 'epic' | 'gog',
                'game_id': str,
                'title': str,
                'size_bytes': int | None,
                'size_formatted': str | None (e.g., "18.52 GB"),
                'app_id': int
            }
        """
        try:
            # Convert unsigned to signed for shortcuts.vdf lookup
            # Steam URLs show unsigned (3037054256) but shortcuts.vdf stores signed (-1257913040)
            if app_id > 2**31:
                app_id_signed = app_id - 2**32
                logger.info(f"[GameInfo] Converted unsigned {app_id} to signed {app_id_signed}")
            else:
                app_id_signed = app_id

            shortcuts = await self.shortcuts_manager.read_shortcuts()

            # Find shortcut by app_id (using signed value)
            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                shortcut_app_id = shortcut.get('appid')
                if shortcut_app_id == app_id_signed:
                    # Parse LaunchOptions to get store and game_id (resilient to extra params)
                    launch_options = shortcut.get('LaunchOptions', '')
                    result = extract_store_id(launch_options)
                    if not result:
                        return {'error': 'No valid store:game_id found in launch options'}

                    store, game_id = result

                    # Check installation status
                    # Check if there's ANY entry in games.map before checking validity
                    # This distinguishes "never installed" from "was installed, files deleted"
                    had_entry = self.shortcuts_manager._has_game_map_entry(store, game_id)
                    
                    # Priority 1: Check games.map (fast, authoritative for Unifideck-installed games)
                    # This also auto-cleans stale entries where path is missing
                    is_installed = self.shortcuts_manager._is_in_game_map(store, game_id)

                    # Priority 2: Fall back to store-specific check (for games installed outside Unifideck)
                    # ONLY if there was no games.map entry at all (not if entry existed but was stale)
                    # IMPORTANT: Also verify install path exists - store can report installed even if files deleted
                    if not is_installed and not had_entry:
                        if store == 'epic':
                            installed_games = await self.epic.get_installed()
                            if game_id in installed_games:
                                # Verify the install path actually exists
                                game_info = installed_games.get(game_id, {})
                                install_path = game_info.get('install_path', '')
                                if install_path and os.path.exists(install_path):
                                    is_installed = True
                                    logger.info(f"[GameInfo] Epic game {game_id} found via legendary (path verified: {install_path})")
                                else:
                                    logger.warning(f"[GameInfo] Epic game {game_id} in legendary but path missing: {install_path}")
                        elif store == 'gog':
                            installed_ids = await self.gog.get_installed()
                            if game_id in installed_ids:
                                # GOG installed includes path info
                                gog_info = installed_ids.get(game_id, {})
                                install_path = gog_info.get('path', '') if isinstance(gog_info, dict) else ''
                                if install_path and os.path.exists(install_path):
                                    is_installed = True
                                    logger.info(f"[GameInfo] GOG game {game_id} found via nile (path verified)")
                                else:
                                    logger.warning(f"[GameInfo] GOG game {game_id} in config but path missing")
                        elif store == 'amazon':
                            installed_ids = await self.amazon.get_installed()
                            if game_id in installed_ids:
                                # Amazon installed includes path info
                                amazon_info = installed_ids.get(game_id, {})
                                install_path = amazon_info.get('path', '') if isinstance(amazon_info, dict) else ''
                                if install_path and os.path.exists(install_path):
                                    is_installed = True
                                    logger.info(f"[GameInfo] Amazon game {game_id} found via nile (path verified)")
                                else:
                                    logger.warning(f"[GameInfo] Amazon game {game_id} in config but path missing")
                        elif store not in ('epic', 'gog', 'amazon'):
                            return {'error': f'Unknown store: {store}'}

                    # Get game size - try cache first (instant), fallback to API (slow)
                    size_bytes = None
                    size_formatted = None
                    try:
                        # Try persistent cache first (populated during sync)
                        size_bytes = get_cached_game_size(store, game_id)
                        
                        if size_bytes is None:
                            # Cache miss - fallback to live fetch (slow)
                            logger.debug(f"[GameInfo] Size cache miss for {store}:{game_id}, fetching from API...")
                            if store == 'epic':
                                size_bytes = await self.epic.get_game_size(game_id)
                            elif store == 'gog':
                                size_bytes = await self.gog.get_game_size(game_id)
                            elif store == 'amazon':
                                size_bytes = await self.amazon.get_game_size(game_id)
                            
                            # Cache the result for next time
                            if size_bytes and size_bytes > 0:
                                cache_game_size(store, game_id, size_bytes)
                        
                        if size_bytes and size_bytes > 0:
                            # Format size nicely
                            if size_bytes >= 1024**3:
                                size_formatted = f"{size_bytes / (1024**3):.1f} GB"
                            elif size_bytes >= 1024**2:
                                size_formatted = f"{size_bytes / (1024**2):.0f} MB"
                            else:
                                size_formatted = f"{size_bytes / 1024:.0f} KB"
                    except Exception as e:
                        logger.debug(f"[GameInfo] Could not get size for {game_id}: {e}")

                    logger.info(f"[GameInfo] App {app_id}: {shortcut.get('AppName')} - Installed: {is_installed}, Size: {size_formatted}")

                    return {
                        'is_installed': is_installed,
                        'store': store,
                        'game_id': game_id,
                        'title': shortcut.get('AppName', ''),
                        'size_bytes': size_bytes,
                        'size_formatted': size_formatted,
                        'app_id': app_id
                    }

            logger.warning(f"[GameInfo] App ID {app_id} not found in shortcuts")
            return {'error': 'errors.gameNotFound'}

        except Exception as e:
            logger.error(f"Error getting game info for app {app_id}: {e}")
            return {'error': str(e)}

    # === CDP BACKEND METHODS (for native PlaySection hiding) ===

    async def inject_hide_css_cdp(self, appId: int, css_rules: str) -> Dict[str, Any]:
        """Inject CSS to hide native PlaySection via CDP

        Args:
            appId: Steam shortcut app ID
            css_rules: CSS rules to inject (from frontend with current class names)

        Returns:
            Dict with success status: {'success': bool, 'css_id': str, 'error': str}
        """
        try:
            client = await get_cdp_client()
            css_id = await client.inject_hide_css(appId, css_rules)
            logger.info(f"[CDP] Successfully injected hide CSS for app {appId}")
            return {"success": True, "css_id": css_id}
        except Exception as e:
            # Try reconnecting once on any connection-related error
            err_lower = str(e).lower()
            if any(kw in err_lower for kw in ["transport", "closing", "closed", "reset", "eof", "timeout", "not connected", "refused"]):
                try:
                    logger.warning(f"[CDP] Connection error, reconnecting...")
                    await shutdown_cdp_client()
                    client = await get_cdp_client()
                    css_id = await client.inject_hide_css(appId, css_rules)
                    logger.info(f"[CDP] Successfully injected hide CSS for app {appId} (after reconnect)")
                    return {"success": True, "css_id": css_id}
                except Exception as retry_e:
                    logger.error(f"[CDP] Reconnection failed for app {appId}: {retry_e}")
                    return {"success": False, "error": str(retry_e)}
            logger.error(f"[CDP] Failed to inject hide CSS for app {appId}: {e}")
            return {"success": False, "error": str(e)}

    async def remove_hide_css_cdp(self, appId: int) -> Dict[str, Any]:
        """Remove hide CSS for specific app via CDP

        Args:
            appId: Steam shortcut app ID

        Returns:
            Dict with success status: {'success': bool, 'error': str}
        """
        try:
            client = await get_cdp_client()
            await client.remove_hide_css(appId)
            logger.info(f"[CDP] Successfully removed hide CSS for app {appId}")
            return {"success": True}
        except Exception as e:
            # Try reconnecting once on any connection-related error
            err_lower = str(e).lower()
            if any(kw in err_lower for kw in ["transport", "closing", "closed", "reset", "eof", "timeout", "not connected", "refused"]):
                try:
                    logger.warning(f"[CDP] Connection error, reconnecting...")
                    await shutdown_cdp_client()
                    client = await get_cdp_client()
                    await client.remove_hide_css(appId)
                    logger.info(f"[CDP] Successfully removed hide CSS for app {appId} (after reconnect)")
                    return {"success": True}
                except Exception as retry_e:
                    logger.error(f"[CDP] Reconnection failed for app {appId}: {retry_e}")
                    return {"success": False, "error": str(retry_e)}
            logger.error(f"[CDP] Failed to remove hide CSS for app {appId}: {e}")
            return {"success": False, "error": str(e)}

    async def hide_native_play_section(self, appId: int) -> Dict[str, Any]:
        """Hide native Play button area via CDP DOM manipulation

        Args:
            appId: Steam shortcut app ID

        Returns:
            Dict with success status
        """
        try:
            client = await get_cdp_client()
            hidden = await client.hide_native_play_section(appId)
            logger.info(f"[CDP] hide_native_play_section({appId}) => {hidden}")
            return {"success": hidden}
        except Exception as e:
            err_lower = str(e).lower()
            if any(kw in err_lower for kw in ["transport", "closing", "closed", "reset", "eof", "timeout", "not connected", "refused"]):
                try:
                    logger.warning(f"[CDP] Connection error, reconnecting...")
                    await shutdown_cdp_client()
                    client = await get_cdp_client()
                    hidden = await client.hide_native_play_section(appId)
                    logger.info(f"[CDP] hide_native_play_section({appId}) => {hidden} (after reconnect)")
                    return {"success": hidden}
                except Exception as retry_e:
                    logger.error(f"[CDP] Reconnection failed for app {appId}: {retry_e}")
                    return {"success": False, "error": str(retry_e)}
            logger.error(f"[CDP] Failed to hide native play section for app {appId}: {e}")
            return {"success": False, "error": str(e)}

    async def unhide_native_play_section(self, appId: int) -> Dict[str, Any]:
        """Unhide native Play button area via CDP DOM manipulation

        Args:
            appId: Steam shortcut app ID

        Returns:
            Dict with success status
        """
        try:
            client = await get_cdp_client()
            unhidden = await client.unhide_native_play_section(appId)
            logger.info(f"[CDP] unhide_native_play_section({appId}) => {unhidden}")
            return {"success": True}
        except Exception as e:
            err_lower = str(e).lower()
            if any(kw in err_lower for kw in ["transport", "closing", "closed", "reset", "eof", "timeout", "not connected", "refused"]):
                try:
                    logger.warning(f"[CDP] Connection error, reconnecting...")
                    await shutdown_cdp_client()
                    client = await get_cdp_client()
                    unhidden = await client.unhide_native_play_section(appId)
                    logger.info(f"[CDP] unhide_native_play_section({appId}) => {unhidden} (after reconnect)")
                    return {"success": True}
                except Exception as retry_e:
                    logger.error(f"[CDP] Reconnection failed for app {appId}: {retry_e}")
                    return {"success": False, "error": str(retry_e)}
            logger.error(f"[CDP] Failed to unhide native play section for app {appId}: {e}")
            return {"success": False, "error": str(e)}

    async def focus_unifideck_button(self, appId: int) -> Dict[str, Any]:
        """Focus the Unifideck action button via CDP.

        Runs .focus() on our button in the SP tab's DOM so Steam's gamepad
        focus system applies the gpfocus class correctly.
        """
        try:
            client = await get_cdp_client()
            focused = await client.focus_unifideck_button(appId)
            return {"success": focused}
        except Exception as e:
            err_lower = str(e).lower()
            if any(kw in err_lower for kw in ["transport", "closing", "closed", "reset", "eof", "timeout", "not connected", "refused"]):
                try:
                    logger.warning(f"[CDP] Connection error during focus, reconnecting...")
                    await shutdown_cdp_client()
                    client = await get_cdp_client()
                    focused = await client.focus_unifideck_button(appId)
                    return {"success": focused}
                except Exception as retry_e:
                    logger.error(f"[CDP] Reconnection failed for focus on app {appId}: {retry_e}")
                    return {"success": False, "error": str(retry_e)}
            logger.error(f"[CDP] Failed to focus button for app {appId}: {e}")
            return {"success": False, "error": str(e)}

    async def debug_log_playsection_structure(self, appId: int) -> Dict[str, Any]:
        """DEBUG: Comprehensive CDP diagnostic for PlaySection hiding"""
        try:
            client = await get_cdp_client()

            js = """(function() {
    var output = '';
    output += '=== DOCUMENT ===\\n';
    output += 'URL: ' + document.location.href + '\\n';
    output += 'Title: ' + document.title + '\\n\\n';

    var styleId = 'unifideck-hide-native-play-""" + str(appId) + """';
    var styleEl = document.getElementById(styleId);
    output += '=== INJECTED STYLE ===\\n';
    output += 'Style #' + styleId + ': ' + (styleEl ? 'FOUND' : 'NOT FOUND') + '\\n';
    if (styleEl) {
        output += 'Content: ' + styleEl.textContent + '\\n';
        output += 'Parent: ' + (styleEl.parentElement ? styleEl.parentElement.tagName : 'DETACHED') + '\\n';
    }
    var allUniStyles = document.querySelectorAll('[id^="unifideck-hide-native-play-"]');
    output += 'Total unifideck styles: ' + allUniStyles.length + '\\n\\n';

    output += '=== PLAYBAR CLASS SEARCH ===\\n';
    if (styleEl) {
        var m = styleEl.textContent.match(/\\.([a-zA-Z0-9_-]+):not/);
        if (m) {
            var cls = m[1];
            output += 'Target class: .' + cls + '\\n';
            var hits = document.querySelectorAll('.' + cls);
            output += 'Elements found: ' + hits.length + '\\n';
            for (var i = 0; i < hits.length; i++) {
                var el = hits[i];
                var cs = window.getComputedStyle(el);
                output += '  [' + i + '] ' + el.tagName + ' display=' + cs.display + '\\n';
                output += '    classes=' + (el.className || '').substring(0, 150) + '\\n';
                output += '    data-unifideck=' + el.hasAttribute('data-unifideck-play-wrapper') + '\\n';
            }
        } else {
            output += 'Could not parse class from CSS\\n';
        }
    }
    output += '\\n';

    output += '=== WRAPPERS ===\\n';
    var wrappers = document.querySelectorAll('[data-unifideck-play-wrapper]');
    output += 'data-unifideck-play-wrapper elements: ' + wrappers.length + '\\n';
    for (var i = 0; i < wrappers.length; i++) {
        var w = wrappers[i];
        output += '  [' + i + '] ' + w.tagName + ' display=' + window.getComputedStyle(w).display + '\\n';
    }
    output += '\\n';

    output += '=== PLAY/INSTALL BUTTONS ===\\n';
    var btns = document.querySelectorAll('button, [role=button]');
    for (var i = 0; i < btns.length; i++) {
        var txt = (btns[i].textContent || '').trim();
        if (txt === 'Play' || txt === 'Install' || txt.indexOf('Install') === 0) {
            output += '  btn: "' + txt.substring(0, 30) + '"\\n';
            var p = btns[i];
            for (var j = 0; j < 5 && p; j++) {
                output += '    p' + j + ': ' + p.tagName + ' .' + (p.className || '').substring(0, 100) + '\\n';
                p = p.parentElement;
            }
        }
    }

    return output;
})()"""

            result = await client.execute_js(js)
            structure = result.get("result", {}).get("result", {}).get("value", "No CDP result")

            logger.info(f"[DEBUG CDP] Diagnostic for app {appId}:\n{structure}")
            return {"success": True, "structure": structure}

        except Exception as e:
            logger.error(f"[DEBUG CDP] Failed: {e}")
            return {"success": False, "error": str(e)}

    # Steam Deck compatibility test result token -> human readable text mapping
    DECK_TEST_RESULT_TOKENS = {
        '#SteamDeckVerified_TestResult_DefaultControllerConfigFullyFunctional': 'All functionality is accessible when using the default controller configuration',
        '#SteamDeckVerified_TestResult_ControllerGlyphsMatchDeckDevice': 'This game shows Steam Deck controller icons',
        '#SteamDeckVerified_TestResult_InterfaceTextIsLegible': 'In-game interface text is legible on Steam Deck',
        '#SteamDeckVerified_TestResult_DefaultConfigurationIsPerformant': "This game's default graphics configuration performs well on Steam Deck",
        '#SteamDeckVerified_TestResult_LauncherInteractionIssues': "This game's launcher/setup tool may require the touchscreen or virtual keyboard, or have difficult to read text",
        '#SteamDeckVerified_TestResult_NativeResolutionNotDefault': "This game supports Steam Deck's native display resolution but does not set it by default and may require you to configure the display resolution manually",
        '#SteamDeckVerified_TestResult_ControllerGlyphsDoNotMatchDeckDevice': 'This game sometimes shows non-Steam-Deck controller icons',
        '#SteamDeckVerified_TestResult_ExternalControllersNotSupportedLocalMultiplayer': 'This game does not default to external Bluetooth/USB controllers on Deck, and may require manually switching the active controller via the Quick Access Menu',
        '#SteamOS_TestResult_GameStartupFunctional': 'This game runs successfully on SteamOS',
    }

    # Steam Store API settings
    STEAM_STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch"
    STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
    STEAM_STORE_MAX_CONCURRENCY = 5

    async def fetch_steam_store_search(self, game_name: str) -> List[Dict[str, Any]]:
        """Search Steam Store for a game by name."""
        logger.info(f"[Steam Store API] Searching for game: {game_name}")
        try:
            import aiohttp
            import urllib.parse

            url = f"{self.STEAM_STORE_SEARCH_URL}?term={urllib.parse.quote(game_name)}&l=english&cc=US"

            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                for attempt in range(2):
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 429 and attempt == 0:
                            await asyncio.sleep(2)
                            continue
                        if response.status != 200:
                            logger.debug(f"[SteamSearch] Search failed with status {response.status}")
                            return []
                        data = await response.json()
                        results = data.get('items', []) if isinstance(data, dict) else []
                        logger.info(f"[Steam Store API] Search for '{game_name}' returned {len(results)} results")
                        return results
        except Exception as e:
            logger.debug(f"[SteamSearch] Error searching Steam for '{game_name}': {e}")
            return []

    async def fetch_steam_appdetails(self, steam_app_id: int) -> Dict[str, Any]:
        """Fetch Steam appdetails for a given app ID."""
        logger.info(f"[Steam Store API] Fetching app details for app_id={steam_app_id}")
        try:
            import aiohttp

            url = f"{self.STEAM_APPDETAILS_URL}?appids={steam_app_id}&l=english&cc=US"
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                for attempt in range(2):
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 429 and attempt == 0:
                            await asyncio.sleep(2)
                            continue
                        if response.status != 200:
                            logger.debug(f"[SteamDetails] appdetails failed for {steam_app_id} status {response.status}")
                            return {}
                        data = await response.json()
                        entry = data.get(str(steam_app_id), {}) if isinstance(data, dict) else {}
                        if not entry.get('success'):
                            return {}
                        result = entry.get('data', {}) if isinstance(entry.get('data'), dict) else {}
                        logger.info(f"[Steam Store API] App details fetched for app_id={steam_app_id}")
                        return result
        except Exception as e:
            logger.debug(f"[SteamDetails] Error fetching appdetails for {steam_app_id}: {e}")
            return {}

    def _convert_steam_store_data_to_metadata(self, steam_app_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Convert Steam Store appdetails data to our metadata cache format."""
        try:
            return {
                'type': data.get('type', 'game'),
                'name': data.get('name', ''),
                'steam_appid': steam_app_id,
                'required_age': data.get('required_age', 0),
                'is_free': data.get('is_free', False),
                'controller_support': data.get('controller_support', 'none'),
                'detailed_description': data.get('detailed_description', ''),
                'short_description': data.get('short_description', ''),
                'supported_languages': data.get('supported_languages', ''),
                'header_image': data.get('header_image', ''),
                'capsule_image': data.get('capsule_image', ''),
                'website': data.get('website', ''),
                'developers': data.get('developers', []) if isinstance(data.get('developers'), list) else [],
                'publishers': data.get('publishers', []) if isinstance(data.get('publishers'), list) else [],
                'platforms': data.get('platforms', {}) if isinstance(data.get('platforms'), dict) else {},
                'metacritic': data.get('metacritic', {}),
                'categories': data.get('categories', []),
                'genres': data.get('genres', []),
                'release_date': data.get('release_date', {'coming_soon': False, 'date': ''})
            }
        except Exception:
            return {}

    def _extract_steam_appid_from_rawg(self, rawg_data: Dict[str, Any]) -> int:
        """Extract Steam app ID from RAWG store URLs if present."""
        if not rawg_data:
            return 0
        try:
            store_urls = rawg_data.get('store_urls', {}) if isinstance(rawg_data.get('store_urls'), dict) else {}
            steam_url = store_urls.get('steam') or store_urls.get('steam_store')
            if steam_url:
                match = re.search(r"/app/(\d+)", steam_url)
                if match:
                    return int(match.group(1))
        except Exception:
            return 0
        return 0

    async def resolve_steam_presence(self, game_title: str) -> Dict[str, Any]:
        """Resolve Steam presence using Steam Store API only.

        Returns:
            Dict with keys: steam_appid, metadata
        """
        logger.info(f"[Steam Presence] Resolving store for: {game_title}")
        # Steam API first
        results = await self.fetch_steam_store_search(game_title)
        if results:
            target_norm = normalize_title_for_matching(game_title)
            top = None
            for item in results:
                item_name = item.get('name', '') if isinstance(item, dict) else ''
                if item_name and normalize_title_for_matching(item_name) == target_norm:
                    top = item
                    break
            if top is None:
                top = results[0]
            steam_app_id = top.get('id') or top.get('appid')
            if steam_app_id:
                try:
                    steam_app_id = int(steam_app_id)
                except Exception:
                    steam_app_id = 0
                if steam_app_id:
                    details = await self.fetch_steam_appdetails(steam_app_id)
                    if details:
                        metadata = self._convert_steam_store_data_to_metadata(steam_app_id, details)
                        logger.info(f"[Steam Presence] Resolved for '{game_title}': appid={steam_app_id}")
                        return {
                            'steam_appid': steam_app_id,
                            'metadata': metadata
                        }
                    logger.info(f"[Steam Presence] Resolved for '{game_title}': appid={steam_app_id}")
                    return {
                        'steam_appid': steam_app_id,
                        'metadata': {
                            'type': 'game',
                            'name': top.get('name', '') or game_title,
                            'steam_appid': steam_app_id
                        }
                    }

        # No Steam presence found - metadata will come from unifiDB/Metacritic caches
        logger.debug(f"[Steam Presence] No Steam match found for '{game_title}'")
        return {'steam_appid': 0, 'metadata': {}}

    async def fetch_steam_deck_compatibility(self, steam_app_id: int) -> Dict[str, Any]:
        """Fetch Steam Deck compatibility info from Steam's API.
        
        Args:
            steam_app_id: The Steam App ID to look up
            
        Returns:
            Dict with:
                'category': int (0=Unknown, 1=Unsupported, 2=Playable, 3=Verified)
                'testResults': List of human-readable test result strings
        """
        if not steam_app_id:
            return {'category': 0, 'testResults': []}
        
        try:
            import aiohttp
            url = f"https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport?nAppID={steam_app_id}"
            
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        logger.warning(f"[DeckCompat] API returned status {response.status} for app {steam_app_id}")
                        return {'category': 0, 'testResults': []}
                    
                    data = await response.json()
            
            # Handle edge case where API returns a list instead of dict
            if not isinstance(data, dict):
                logger.debug(f"[DeckCompat] Unexpected response type for app {steam_app_id}: {type(data).__name__}")
                return {'category': 0, 'testResults': []}
                    
            if not data.get('success'):
                return {'category': 0, 'testResults': []}
            
            results = data.get('results', {})
            if not isinstance(results, dict):
                logger.debug(f"[DeckCompat] 'results' is {type(results).__name__}, not dict, for app {steam_app_id}")
                return {'category': 0, 'testResults': []}
            category = results.get('resolved_category', 0)
            
            # Convert test result tokens to human-readable strings
            test_results = []
            for item in results.get('resolved_items', []):
                token = item.get('loc_token', '')
                display_type = item.get('display_type', 0)  # 4=pass, 3=warning
                text = self.DECK_TEST_RESULT_TOKENS.get(token, token.replace('#SteamDeckVerified_TestResult_', '').replace('#SteamOS_TestResult_', ''))
                if text:
                    test_results.append({
                        'text': text,
                        'passed': display_type == 4  # 4 = checkmark, 3 = warning
                    })
            
            logger.info(f"[DeckCompat] App {steam_app_id}: category={category}, {len(test_results)} test results")
            return {'category': category, 'testResults': test_results}

        except Exception as e:
            logger.warning(f"[DeckCompat] Failed to fetch for app {steam_app_id}: {e}")
            return {'category': 0, 'testResults': []}

    async def _get_gog_slug(self, game_id: str) -> Optional[str]:
        """Get GOG slug via GOG client for store URL generation."""
        try:
            if hasattr(self, 'gog') and self.gog:
                return await self.gog.get_game_slug(game_id)
        except Exception as e:
            logger.warning(f"[StoreURL] GOG slug fetch failed for {game_id}: {e}")
        return None

    def _get_amazon_official_url(self, game_id: str) -> Optional[str]:
        """Get Amazon official URL via Amazon connector."""
        try:
            if hasattr(self, 'amazon') and self.amazon:
                return self.amazon.get_game_official_url(game_id)
        except Exception as e:
            logger.warning(f"[StoreURL] Amazon URL fetch failed for {game_id}: {e}")
        return None

    async def get_game_metadata_display(self, app_id: int) -> Optional[Dict[str, Any]]:

        """Get formatted metadata for display in GameInfoPanel component.

        Args:
            app_id: Steam shortcut app ID (can be signed or unsigned)

        Returns:
            Dict with metadata for UI display, or None if not available:
            {
                'steamAppId': int,
                'store': str ('epic', 'gog', 'amazon'),
                'storeUrl': str (original store page URL),
                'title': str,
                'developer': str,
                'publisher': str,
                'releaseDate': str,
                'description': str,
                'deckCompatibility': int (0=Unknown, 1=Unsupported, 2=Playable, 3=Verified),
                'protonVersion': str | None,
                'homepageUrl': str | None
            }
        """
        try:
            # Convert unsigned to signed for lookup
            if app_id > 2**31:
                app_id_signed = app_id - 2**32
            else:
                app_id_signed = app_id

            # Get basic game info first (store, game_id, title)
            game_info = await self.get_game_info(app_id)
            if 'error' in game_info:
                logger.warning(f"[MetadataDisplay] Could not get game info for {app_id}: {game_info.get('error')}")
                return None

            store = game_info.get('store')
            game_id = game_info.get('game_id')
            title = game_info.get('title', 'Unknown')

            # Get real Steam App ID from cache (for Steam store/community links)
            steam_real_cache = load_steam_real_appid_cache()
            steam_app_id = steam_real_cache.get(app_id_signed, 0)
            logger.info(f"[MetadataDisplay] Cache lookup: app_id={app_id}, signed={app_id_signed}, cache_size={len(steam_real_cache)}, steam_app_id={steam_app_id}")

            # Load Steam metadata cache for detailed info
            # This cache contains data from Steam Store API (and RAWG fallback for Steam presence).
            metadata_cache = load_steam_metadata_cache()

            # Handle negative cache sentinel: -1 means sync already tried and found no Steam match
            if steam_app_id == -1:
                steam_app_id = 0
                steam_metadata = {}
                # Don't resolve on-demand — rely on unifiDB/Metacritic for metadata
                logger.debug(f"[MetadataDisplay] Negative cached Steam presence for '{title}', skipping resolve")
            else:
                # Metadata cache also has int keys
                steam_metadata = metadata_cache.get(steam_app_id, {}) if steam_app_id else {}

                # Resolve Steam presence if missing or invalid
                if (not steam_app_id) or (not steam_metadata) or (not steam_metadata.get('name')):
                    presence = await self.resolve_steam_presence(title)
                    resolved_app_id = presence.get('steam_appid', 0)
                    resolved_metadata = presence.get('metadata', {})
                    if resolved_app_id:
                        steam_app_id = resolved_app_id
                        steam_real_cache[app_id_signed] = resolved_app_id
                        save_steam_real_appid_cache(steam_real_cache)
                        if resolved_metadata:
                            metadata_cache[resolved_app_id] = resolved_metadata
                            save_steam_metadata_cache(metadata_cache)
                            steam_metadata = resolved_metadata

            # Determine if this game has a real Steam store presence.
            # Only IDs with entries in steam_metadata_cache are confirmed real Steam App IDs
            # with working store/community pages.
            #
            # Additional validation: The metadata must have:
            # 1. A valid 'type' field ('game' for games)
            # 2. A matching 'steam_appid' field to confirm it's the right game
            # 3. A 'name' field (all real Steam games have this)
            has_steam_store_page = False
            if steam_metadata:
                meta_type = str(steam_metadata.get('type', '')).lower()
                meta_appid_raw = steam_metadata.get('steam_appid', 0)
                try:
                    meta_appid = int(meta_appid_raw)
                except Exception:
                    meta_appid = 0
                meta_name = str(steam_metadata.get('name', '')).strip()
                # Only consider it a valid Steam store page if:
                # - Type is 'game' or 'application' (not 'dlc', 'demo', etc.)
                # - The steam_appid in metadata matches what we're looking up
                # - The game has a name
                if meta_type in ('game', 'application') and meta_appid == steam_app_id and meta_name:
                    has_steam_store_page = True
                    logger.debug(f"[MetadataDisplay] Valid Steam store page: {meta_name} (ID: {steam_app_id})")
                else:
                    logger.debug(f"[MetadataDisplay] Invalid Steam metadata for {steam_app_id}: type={meta_type}, appid_match={meta_appid == steam_app_id}, has_name={bool(meta_name)}")

            # Build store-specific URL with proper fallbacks
            # Epic game_id is a catalog ID (not a URL slug), GOG game_id is numeric (not a slug)
            # Use search-based URLs that always work, with optional direct links where available
            import urllib.parse
            encoded_title = urllib.parse.quote(title)

            if store == 'epic':
                # Epic: Use search URL (game_id is catalog ID, not URL slug)
                store_url = f"https://store.epicgames.com/en-US/browse?q={encoded_title}&sortBy=relevancy"
            elif store == 'gog':
                # GOG: Try to get slug from API for direct link, fallback to search
                gog_slug = await self._get_gog_slug(game_id)
                if gog_slug:
                    store_url = f"https://www.gog.com/en/game/{gog_slug}"
                else:
                    store_url = f"https://www.gog.com/games?query={encoded_title}"
            elif store == 'amazon':
                # Amazon: Use official website from metadata if available, otherwise gaming portal
                official_url = self._get_amazon_official_url(game_id)
                store_url = official_url or "https://gaming.amazon.com/intro"
            else:
                store_url = ''

            # Track data sources for debugging
            sources = {}

            # Initialize metadata variables
            developer = ''
            publisher = ''
            description = ''
            release_date = ''
            unifidb_genres = []
            metacritic = None

            # First try Steam metadata for basic info (developer, publisher, description, release date)
            if steam_metadata:
                developers = steam_metadata.get('developers', [])
                publishers = steam_metadata.get('publishers', [])
                developer = ', '.join(developers) if developers else ''
                publisher = ', '.join(publishers) if publishers else ''
                description = steam_metadata.get('short_description', '') or steam_metadata.get('detailed_description', '')
                release_info = steam_metadata.get('release_date', {})
                steam_release_raw = release_info.get('date', '') if isinstance(release_info, dict) else ''
                release_date = normalize_release_date(steam_release_raw)
                if developer:
                    sources['developer'] = 'steam_cache'
                if publisher:
                    sources['publisher'] = 'steam_cache'
                if description:
                    sources['description'] = 'steam_cache'
                if release_date:
                    sources['release_date'] = 'steam_cache'

            # Check Metacritic cache FIRST for scores (primary source)
            # If not in cache, fetch on-demand (this is the new lazy loading pattern)
            metacritic_cache = load_metacritic_metadata_cache()
            metacritic_cache_key = title.lower()
            metacritic_data = metacritic_cache.get(metacritic_cache_key)

            if metacritic_data:
                metacritic = metacritic_data.get('metascore')
                if metacritic:
                    sources['metacritic'] = 'metacritic_cache'
                    logger.debug(f"[MetadataDisplay] Metacritic score for '{title}': {metacritic}")
            else:
                # ON-DEMAND FETCH: Not in cache, so fetch live from Metacritic API
                logger.debug(f"[MetadataDisplay] No Metacritic cache for '{title}', fetching on-demand...")
                try:
                    from py_modules.unifideck.metadata.metacritic import fetch_metacritic_metadata
                    
                    # Fetch with no delay (user is waiting for panel to open)
                    metacritic_data = await fetch_metacritic_metadata(title, timeout=10.0, delay=0)
                    
                    if metacritic_data:
                        # Cache the result for future use
                        metacritic_cache[metacritic_cache_key] = metacritic_data
                        save_metacritic_metadata_cache(metacritic_cache)
                        
                        metacritic = metacritic_data.get('metascore')
                        if metacritic:
                            sources['metacritic'] = 'metacritic_live'
                            logger.info(f"[MetadataDisplay] Fetched Metacritic score for '{title}': {metacritic}")
                    else:
                        logger.debug(f"[MetadataDisplay] No Metacritic data found for '{title}'")
                except ImportError as e:
                    logger.error(f"[MetadataDisplay] Failed to import Metacritic module: {e}")
                except Exception as e:
                    logger.warning(f"[MetadataDisplay] Error fetching Metacritic data for '{title}': {e}")

            # Check unifiDB cache for additional metadata
            unifidb_cache = load_unifidb_metadata_cache()
            unifidb_cache_key = title.lower()
            unifidb_data = unifidb_cache.get(unifidb_cache_key)

            if unifidb_data:
                logger.debug(f"[MetadataDisplay] unifiDB source=cache for '{title}'")
                if not description:
                    description = unifidb_data.get('description', '')
                    if description:
                        sources['description'] = 'unifidb_cache'
                if not developer:
                    developer = ', '.join(unifidb_data.get('developers', []))
                    if developer:
                        sources['developer'] = 'unifidb_cache'
                if not publisher:
                    publisher = ', '.join(unifidb_data.get('publishers', []))
                    if publisher:
                        sources['publisher'] = 'unifidb_cache'
                if not release_date:
                    release_date = normalize_release_date(unifidb_data.get('released', ''))
                    if release_date:
                        sources['release_date'] = 'unifidb_cache'
                unifidb_genres = unifidb_data.get('genres', [])[:4]
                if unifidb_genres:
                    sources['genres'] = 'unifidb_cache'

                # If no Metacritic score yet, try unifiDB (fallback only)
                if not metacritic:
                    metacritic = unifidb_data.get('aggregated_rating')
                    if metacritic:
                        sources['metacritic'] = 'unifidb_cache'
                        logger.debug(f"[MetadataDisplay] unifiDB rating for '{title}': {metacritic}")
            else:
                logger.debug(f"[MetadataDisplay] No unifiDB cache for '{title}'")

            # Fall back to Metacritic for additional fields if unifiDB missing
            if metacritic_data and not unifidb_genres:
                genres_from_metacritic = metacritic_data.get('genres', [])[:4]
                if genres_from_metacritic:
                    unifidb_genres = genres_from_metacritic
                    sources['genres'] = 'metacritic_cache'
                if not description:
                    description = metacritic_data.get('description', '')
                    if description:
                        sources['description'] = 'metacritic_cache'

            genres = unifidb_genres

            # Fetch Steam Deck compatibility - use cached if available
            cached_deck_category = steam_metadata.get('deck_category', 0) if steam_metadata else 0
            cached_deck_results = steam_metadata.get('deck_test_results', []) if steam_metadata else []

            if cached_deck_category > 0:
                deck_category = cached_deck_category
                deck_test_results = cached_deck_results
                sources['deck_compat'] = 'steam_cache'
            elif steam_app_id > 0:
                deck_info = await self.fetch_steam_deck_compatibility(steam_app_id)
                deck_category = deck_info.get('category', 0)
                deck_test_results = deck_info.get('testResults', [])
                sources['deck_compat'] = 'steam_api' if deck_category > 0 else 'none'
            else:
                deck_category = 0
                deck_test_results = []
                sources['deck_compat'] = 'none'

            result = {
                'steamAppId': steam_app_id,
                'hasSteamStorePage': has_steam_store_page,
                'store': store,
                'storeUrl': store_url,
                'title': title,
                'developer': developer,
                'publisher': publisher,
                'releaseDate': release_date,
                'metacritic': metacritic,
                'description': sanitize_description(description),
                'deckCompatibility': deck_category,
                'deckTestResults': deck_test_results,
                'genres': genres,
                'homepageUrl': steam_metadata.get('website', '') if steam_metadata else ''
            }

            # Log full source summary for debugging
            logger.info(f"[MetadataDisplay] '{title}' (appId={app_id}, steamId={steam_app_id}) sources: {sources}")
            return result

        except Exception as e:
            logger.error(f"Error getting metadata display for app {app_id}: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def install_game_by_appid(self, app_id: int) -> Dict[str, Any]:
        """Install game by Steam shortcut app ID

        Args:
            app_id: Steam shortcut app ID

        Returns:
            Dict with success status and progress updates
        """
        try:
            # Get game info first
            game_info = await self.get_game_info(app_id)

            if 'error' in game_info:
                return game_info

            store = game_info['store']
            game_id = game_info['game_id']
            title = game_info['title']

            logger.info(f"[Install] Starting installation: {title} ({store}:{game_id})")

            # Start installation
            if store == 'epic':
                result = await self.epic.install_game(game_id)

                # Get executable path after installation
                if result.get('success'):
                    exe_path = await self._get_epic_executable(game_id)
                    if exe_path:
                        result['exe_path'] = exe_path
                        logger.info(f"[Install] Got Epic executable path: {exe_path}")
                    else:
                        logger.warning(f"[Install] Could not get executable path for {game_id}")

            elif store == 'gog':
                result = await self.gog.install_game(game_id)
            elif store == 'amazon':
                result = await self.amazon.install_game(game_id)
            else:
                return {'success': False, 'error': f'Unknown store: {store}'}

            # If successful, update shortcut to mark as installed
            if result.get('success'):
                logger.info(f"[Install] Installation successful, updating shortcut for {title}")
                # Handle both 'exe_path' (Epic) and 'executable' (GOG) keys
                exe_path = result.get('exe_path') or result.get('executable')
                await self.shortcuts_manager.mark_installed(
                    game_id,
                    store,
                    result.get('install_path', ''),
                    exe_path
                )
            else:
                logger.error(f"[Install] Installation failed for {title}: {result.get('error')}")

            return result

        except Exception as e:
            logger.error(f"Error installing game by app ID {app_id}: {e}")
            return {'success': False, 'error': str(e)}

    async def uninstall_game_by_appid(self, app_id: int, delete_prefix: bool = False) -> Dict[str, Any]:
        """Uninstall game by Steam shortcut app ID
        
        Args:
            app_id: Steam shortcut app ID
            delete_prefix: If True, also delete the Wine/Proton prefix directory
        """
        try:
            # Get game info first
            game_info = await self.get_game_info(app_id)

            if 'error' in game_info:
                return game_info

            store = game_info['store']
            game_id = game_info['game_id']
            title = game_info['title']
            
            # Check if actually installed
            if not game_info.get('is_installed'):
                 return {'success': False, 'error': 'errors.gameNotInstalled'}

            logger.info(f"[Uninstall] Starting uninstallation: {title} ({store}:{game_id}), delete_prefix={delete_prefix}")

            # Perform store-specific uninstall
            if store == 'epic':
                # legendary uninstall <id> --yes
                if not self.epic.legendary_bin:
                    return {'success': False, 'error': 'errors.legendaryNotFound'}
                
                # Clean up stale legendary lock files (legendary returns 0 even when blocked by lock)
                lock_dir = os.path.expanduser("~/.config/legendary")
                for lock_file in ['installed.json.lock', 'user.json.lock']:
                    lock_path = os.path.join(lock_dir, lock_file)
                    if os.path.exists(lock_path):
                        try:
                            os.remove(lock_path)
                            logger.info(f"[Uninstall] Cleared stale lock: {lock_file}")
                        except Exception as e:
                            logger.warning(f"[Uninstall] Could not clear lock {lock_file}: {e}")
                
                cmd = [self.epic.legendary_bin, 'uninstall', game_id, '--yes']
                logger.info(f"[Uninstall] Running: {' '.join(cmd)}")
                
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                
                stdout_str = stdout.decode() if stdout else ''
                stderr_str = stderr.decode() if stderr else ''
                
                logger.info(f"[Uninstall] legendary return code: {proc.returncode}")
                if stdout_str:
                    logger.info(f"[Uninstall] stdout: {stdout_str[:500]}")
                if stderr_str:
                    logger.info(f"[Uninstall] stderr: {stderr_str[:500]}")
                
                # Check for lock failure (legendary returns 0 even when this happens!)
                combined_output = stdout_str + stderr_str
                if 'Failed to acquire installed data lock' in combined_output:
                    logger.error("[Epic] Uninstall failed: Lock acquisition failed")
                    return {'success': False, 'error': 'errors.lockConflict'}
                
                if proc.returncode != 0:
                     logger.error(f"[Epic] Uninstall failed: {stderr_str}")
                     # Still remove from games.map so UI shows Install button
                     await self.shortcuts_manager._remove_from_game_map(store, game_id)
                     logger.info(f"[Uninstall] Removed {store}:{game_id} from games.map despite uninstall failure")
                     return {'success': False, 'error': f"Legendary uninstall failed: {stderr_str}"}
            
            elif store == 'gog':
                # Get install path from games.map (same data used for launching)
                install_path = self.shortcuts_manager._get_install_dir_from_game_map(store, game_id)
                if install_path:
                    logger.info(f"[Uninstall] Using games.map path for GOG: {install_path}")
                    result = await self.gog.uninstall_game(game_id, install_path=install_path)
                else:
                    # Fallback to filesystem scan
                    result = await self.gog.uninstall_game(game_id)
                if not result['success']:
                    # Still remove from games.map so UI shows Install button
                    await self.shortcuts_manager._remove_from_game_map(store, game_id)
                    logger.info(f"[Uninstall] Removed {store}:{game_id} from games.map despite uninstall failure")
                    return result
            
            elif store == 'amazon':
                result = await self.amazon.uninstall_game(game_id)
                if not result['success']:
                    # Still remove from games.map so UI shows Install button
                    await self.shortcuts_manager._remove_from_game_map(store, game_id)
                    logger.info(f"[Uninstall] Removed {store}:{game_id} from games.map despite uninstall failure")
                    return result
            
            else:
                return {'success': False, 'error': f"Unsupported store for uninstall: {store}"}

            # Delete prefix if requested
            prefix_deleted = False
            if delete_prefix:
                prefix_path = os.path.expanduser(f"~/.local/share/unifideck/prefixes/{game_id}")
                if os.path.exists(prefix_path):
                    try:
                        import shutil
                        shutil.rmtree(prefix_path)
                        logger.info(f"[Uninstall] Deleted prefix directory: {prefix_path}")
                        prefix_deleted = True
                    except Exception as e:
                        logger.warning(f"[Uninstall] Failed to delete prefix {prefix_path}: {e}")
                else:
                    logger.info(f"[Uninstall] No prefix to delete at: {prefix_path}")

            # Update shortcut
            logger.info(f"[Uninstall] Reverting shortcut for {title}...")
            shortcut_updated = await self.shortcuts_manager.mark_uninstalled(title, store, game_id)
            
            if not shortcut_updated:
                logger.warning(f"[Uninstall] Failed to revert shortcut for {title}")
                return {
                    'success': True, 
                    'message': 'Game uninstalled, but shortcut could not be updated. Restart Steam to fix.',
                    'prefix_deleted': prefix_deleted,
                    'game_update': {'appId': app_id, 'store': store, 'isInstalled': False}
                }

            return {
                'success': True,
                'message': f'{title} uninstalled successfully',
                'prefix_deleted': prefix_deleted,
                'game_update': {'appId': app_id, 'store': store, 'isInstalled': False}
            }

        except Exception as e:
            logger.error(f"[Uninstall] Error uninstalling game {app_id}: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}


    # ============== DOWNLOAD QUEUE API METHODS ==============

    async def get_download_queue_info(self) -> Dict[str, Any]:
        """Get current download queue status for frontend display"""
        try:
            return {
                'success': True,
                **self.download_queue.get_queue_info()
            }
        except Exception as e:
            logger.error(f"[DownloadQueue] Error getting queue info: {e}")
            return {'success': False, 'error': str(e)}

    async def add_to_download_queue(self, game_id: str, game_title: str, store: str, was_previously_installed: bool = False, language: str = None) -> Dict[str, Any]:
        """Add a game to the download queue

        Args:
            game_id: Store-specific game identifier
            game_title: Display name
            store: 'epic' or 'gog'
            was_previously_installed: GUARDRAIL - If True, cancel won't delete game files
            language: For GOG games, the language to download (e.g., 'en-US', 'de-DE')
        """
        try:
            result = await self.download_queue.add_to_queue(
                game_id=game_id,
                game_title=game_title,
                store=store,
                was_previously_installed=was_previously_installed,
                language=language
            )
            logger.info(f"[DownloadQueue] Added {game_title} to queue (was_installed={was_previously_installed}, lang={language}): {result}")
            return result
        except Exception as e:
            logger.error(f"[DownloadQueue] Error adding to queue: {e}")
            return {'success': False, 'error': str(e)}

    async def add_to_download_queue_by_appid(self, app_id: int) -> Dict[str, Any]:
        """Add a game to download queue by its Steam shortcut app ID"""
        try:
            # Get game info from app_id
            game_info = await self.get_game_info(app_id)
            
            if 'error' in game_info:
                return {'success': False, 'error': game_info['error']}
            
            # GUARDRAIL: Check if game is already installed
            # If so, we'll tell the download queue to NEVER delete files on cancel
            was_previously_installed = game_info.get('is_installed', False)
            if was_previously_installed:
                logger.info(f"[DownloadQueue] GUARDRAIL: {game_info['title']} is already installed - will protect on cancel")
            
            # Check for multi-part GOG downloads
            is_multipart = False
            if game_info['store'] == 'gog':
                try:
                    game_details = await self.gog._get_game_details(game_info['game_id'])
                    if game_details:
                        linux_installers = self.gog._find_linux_installer(game_details)
                        if linux_installers:
                            is_multipart = len(linux_installers) > 1
                        else:
                            windows_installers = self.gog._find_windows_installer(game_details)
                            if windows_installers:
                                is_multipart = len(windows_installers) > 1
                        if is_multipart:
                            logger.info(f"[DownloadQueue] Detected multi-part GOG download for {game_info['title']}")
                except Exception as e:
                    logger.warning(f"[DownloadQueue] Could not check multi-part status: {e}")
            
            result = await self.add_to_download_queue(
                game_id=game_info['game_id'],
                game_title=game_info['title'],
                store=game_info['store'],
                was_previously_installed=was_previously_installed  # GUARDRAIL
            )
            
            # Add multi-part flag to response
            if result.get('success'):
                result['is_multipart'] = is_multipart
            
            return result
        except Exception as e:
            logger.error(f"[DownloadQueue] Error adding to queue by appid: {e}")
            return {'success': False, 'error': str(e)}

    async def cancel_current_download(self) -> Dict[str, Any]:
        """Cancel the currently downloading game"""
        try:
            success = await self.download_queue.cancel_current()
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error cancelling download: {e}")
            return {'success': False, 'error': str(e)}

    async def cancel_download_by_id(self, download_id: str) -> Dict[str, Any]:
        """Cancel/remove a queued download by its ID"""
        try:
            # Check if it's the current download
            current = self.download_queue.get_current()
            if current and current.get('id') == download_id:
                success = await self.download_queue.cancel_current()
            else:
                success = self.download_queue.remove_from_queue(download_id)
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error cancelling download {download_id}: {e}")
            return {'success': False, 'error': str(e)}

    async def clear_finished_download(self, download_id: str) -> Dict[str, Any]:
        """Remove a finished download from the completed list"""
        try:
            success = self.download_queue.remove_finished(download_id)
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error clearing finished download {download_id}: {e}")
            return {'success': False, 'error': str(e)}

    async def force_clear_download_entry(self, download_id: str) -> Dict[str, Any]:
        """Force-remove a stuck entry from the download queue.
        
        This is a recovery method for when entries get stuck (e.g., download failed
        at 99% and got into a bad state). Use when getting 'Already in queue' errors.
        
        Args:
            download_id: The download ID (format: 'store:game_id', e.g., 'gog:12345')
        """
        try:
            logger.info(f"[DownloadQueue] Force-clearing stuck entry: {download_id}")
            success = self.download_queue.force_clear_entry(download_id)
            if success:
                logger.info(f"[DownloadQueue] Successfully force-cleared: {download_id}")
            else:
                logger.warning(f"[DownloadQueue] Entry not found for force-clear: {download_id}")
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error force-clearing entry {download_id}: {e}")
            return {'success': False, 'error': str(e)}

    async def clear_stale_downloads(self) -> Dict[str, Any]:
        """Clear all stale (error/cancelled) entries from the download queue.
        
        Use this as a recovery method if downloads are repeatedly blocked by
        'Already in queue' errors. This is safe to call - it only removes entries
        that are already in a terminal state (error or cancelled).
        
        Returns:
            Dict with success status and count of cleared entries
        """
        try:
            logger.info("[DownloadQueue] Clearing all stale download entries...")
            cleared_count = self.download_queue.clear_all_stale()
            logger.info(f"[DownloadQueue] Cleared {cleared_count} stale entries")
            return {'success': True, 'cleared_count': cleared_count}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error clearing stale downloads: {e}")
            return {'success': False, 'error': str(e)}

    async def is_game_downloading(self, game_id: str, store: str) -> Dict[str, Any]:
        """Check if a specific game is currently downloading or in queue"""
        try:
            # Use get_download_item to find both active and recently finished/cancelled items
            download_info = self.download_queue.get_download_item(game_id, store)
            
            is_downloading = False
            if download_info:
                # Only consider it "downloading" if in an active state
                active_states = ['queued', 'downloading', 'extracting', 'verifying']
                is_downloading = download_info.get('status') in active_states
            
            return {
                'success': True,
                'is_downloading': is_downloading,
                'download_info': download_info
            }
        except Exception as e:
            logger.error(f"[DownloadQueue] Error checking download status: {e}")
            return {'success': False, 'error': str(e)}

    async def get_storage_locations(self) -> Dict[str, Any]:
        """Get available storage locations for downloads"""
        try:
            locations = self.download_queue.get_storage_locations()
            default = self.download_queue.get_default_storage()
            return {
                'success': True,
                'locations': locations,
                'default': default
            }
        except Exception as e:
            logger.error(f"[DownloadQueue] Error getting storage locations: {e}")
            return {'success': False, 'error': str(e)}

    async def set_default_storage_location(self, location: str) -> Dict[str, Any]:
        """Set the default storage location for new downloads"""
        try:
            success = self.download_queue.set_default_storage(location)
            return {'success': success}
        except Exception as e:
            logger.error(f"[DownloadQueue] Error setting storage location: {e}")
            return {'success': False, 'error': str(e)}

    # ============== END DOWNLOAD QUEUE API ==============

    # ============== LANGUAGE SETTINGS API ==============

    async def get_language_preference(self) -> Dict[str, Any]:
        """Get saved language preference from settings file.
        
        Returns:
            Dict with success status and language code (or 'auto' for system detection)
        """
        try:
            settings_path = os.path.expanduser("~/.local/share/unifideck/settings.json")
            
            if os.path.exists(settings_path):
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
                    language = settings.get('language', 'auto')
            else:
                language = 'auto'  # Default to auto-detect
            
            logger.debug(f"[Language] Got language preference: {language}")
            return {'success': True, 'language': language}
        except Exception as e:
            logger.error(f"[Language] Error getting language preference: {e}")
            return {'success': False, 'error': str(e), 'language': 'auto'}

    async def set_language_preference(self, language: str) -> Dict[str, Any]:
        """Save language preference to settings file.
        
        Args:
            language: Language code (e.g., 'en-US', 'de-DE') or 'auto' for system detection
            
        Returns:
            Dict with success status
        """
        try:
            settings_path = os.path.expanduser("~/.local/share/unifideck/settings.json")
            settings_dir = os.path.dirname(settings_path)
            
            # Ensure directory exists
            os.makedirs(settings_dir, exist_ok=True)
            
            # Load existing settings or create new
            if os.path.exists(settings_path):
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
            else:
                settings = {}
            
            # Update language setting
            settings['language'] = language
            
            # Save
            with open(settings_path, 'w') as f:
                json.dump(settings, f, indent=2)
            
            logger.info(f"[Language] Saved language preference: {language}")
            return {'success': True}
        except Exception as e:
            logger.error(f"[Language] Error saving language preference: {e}")
            return {'success': False, 'error': str(e)}

    # ============== END LANGUAGE SETTINGS API ==============

    # ============== ACCOUNT SWITCH API ==============

    async def check_account_switch(self) -> Dict[str, Any]:
        """Check if a Steam account switch was detected on startup.

        Called by the frontend to decide whether to show the account switch modal.
        """
        return {
            'show_modal': self.account_manager.should_show_modal(),
            'current_user': self.account_manager.current_user_id,
            'previous_user': self.account_manager.previous_user_id,
            'has_auth_tokens': self.account_manager.has_active_auth_tokens(),
            'has_registry': self.account_manager.has_registry_entries(),
        }

    async def migrate_account_data(self) -> Dict[str, Any]:
        """Migrate shortcuts and artwork from previous account to current account.

        Called when the user selects 'Migrate' in the account switch modal.
        """
        shortcuts = self.account_manager.reconcile_shortcuts_from_registry(self.shortcuts_manager)
        artwork = self.account_manager.migrate_artwork()
        self.account_manager.account_switch_detected = False
        return {
            'shortcuts_created': shortcuts.get('created', 0),
            'artwork_copied': artwork.get('copied', 0),
            'errors': shortcuts.get('errors', []) + artwork.get('errors', []),
        }

    async def clear_store_auths(self) -> Dict[str, Any]:
        """Clear all store auth tokens for a fresh start.

        Called when the user selects 'Fresh Start' in the account switch modal.
        Re-initializes store connectors to pick up the cleared state.
        """
        result = self.account_manager.clear_all_auth_tokens()

        # Re-init store connectors so they reflect the cleared state
        self.gog = GOGAPIClient(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self)
        self.amazon = AmazonConnector(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self)

        self.account_manager.account_switch_detected = False
        return result

    # ============== END ACCOUNT SWITCH API ==============

    # ============== PROTON COMPAT TOOL API ==============

    async def get_compat_tool_for_game(self, store_game_id: str) -> Dict[str, Any]:
        """Get the Steam compatibility tool set for a Unifideck shortcut.

        Reads config.vdf CompatToolMapping using the shortcut's appID
        from shortcuts_registry.json. Also returns the launcher path
        for building %command% bypass launch options.

        Args:
            store_game_id: e.g., "gog:1234567890"
        """
        from py_modules.unifideck.compat.proton_tools import get_compat_tool_for_game as _get
        result = _get(store_game_id)
        result["launcher_path"] = os.path.join(os.path.dirname(__file__), 'bin', 'unifideck-launcher')
        return result

    async def temporarily_clear_compat_tool(self, appid_unsigned: int) -> Dict[str, Any]:
        """Temporarily remove a compat tool entry from config.vdf.

        Called before re-launching via RunGame to prevent Steam from
        running our bash launcher through Wine/Proton.
        """
        from py_modules.unifideck.compat.proton_tools import temporarily_clear_compat_tool as _clear
        return _clear(appid_unsigned)

    async def restore_compat_tool(self, appid_unsigned: int, tool_name: str) -> Dict[str, Any]:
        """Restore a compat tool entry in config.vdf after temporary clear."""
        from py_modules.unifideck.compat.proton_tools import restore_compat_tool as _restore
        return _restore(appid_unsigned, tool_name)

    async def save_proton_setting(self, store_game_id: str, tool_name: str) -> Dict[str, Any]:
        """Save proton tool preference for the launcher to read at Priority 2.5."""
        from py_modules.unifideck.compat.proton_tools import save_proton_setting as _save
        return _save(store_game_id, tool_name)

    # ============== END PROTON COMPAT TOOL API ==============

    async def check_store_status(self) -> Dict[str, Any]:
        """Check connectivity status of all stores"""
        logger.info("[STATUS] Checking store connectivity status")
        try:
            legendary_installed = self.epic.legendary_bin is not None
            logger.info(f"[STATUS] Legendary installed: {legendary_installed}, path: {self.epic.legendary_bin}")

            # Only check Epic if legendary is installed
            if legendary_installed:
                logger.info("[STATUS] Checking Epic Games availability")
                epic_available = await self.epic.is_available()
                epic_status = 'connected' if epic_available else 'not_connected'
                logger.info(f"[STATUS] Epic Games: {epic_status}")
            else:
                epic_status = 'legendary_not_installed'
                logger.warning("[STATUS] Epic Games: Legendary CLI not installed")

            logger.info("[STATUS] Checking GOG availability")
            gog_available = await self.gog.is_available()
            gog_status = 'connected' if gog_available else 'not_connected'
            logger.info(f"[STATUS] GOG: {gog_status}")

            # Check Amazon availability
            nile_installed = self.amazon.nile_bin is not None
            logger.info(f"[STATUS] Nile installed: {nile_installed}, path: {self.amazon.nile_bin}")

            if nile_installed:
                logger.info("[STATUS] Checking Amazon Games availability")
                amazon_available = await self.amazon.is_available()
                amazon_status = 'connected' if amazon_available else 'not_connected'
                logger.info(f"[STATUS] Amazon Games: {amazon_status}")
            else:
                amazon_status = 'nile_not_installed'
                logger.warning("[STATUS] Amazon Games: Nile CLI not installed")

            result = {
                'success': True,
                'epic': epic_status,
                'gog': gog_status,
                'amazon': amazon_status,
                'legendary_installed': legendary_installed,
                'nile_installed': nile_installed
            }
            logger.info(f"[STATUS] Check complete: {result}")
            return result

        except Exception as e:
            logger.error(f"[STATUS] Error in check_store_status: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'epic': 'error',
                'gog': 'error',
                'amazon': 'error'
            }

    async def get_real_steam_appid_mappings(self) -> Dict[str, Any]:
        """
        Get the mapping of shortcut app IDs to real Steam app IDs.
        Used by frontend to patch Steam's data stores.

        Returns:
            Dict with 'mappings' key containing { shortcutAppId: realSteamAppId }
        """
        try:
            cache = load_steam_real_appid_cache()  # Returns {shortcut_appid: steam_appid}
            return {
                "success": True,
                "mappings": cache  # Dict[int, int]
            }
        except Exception as e:
            logging.error(f"Error loading Steam App ID mappings: {e}")
            return {
                "success": False,
                "error": str(e),
                "mappings": {}
            }

    async def get_steam_metadata_cache(self) -> Dict[str, Any]:
        """
        Get cached Steam metadata for frontend store patching.
        Returns pre-fetched Steam API data for all mapped games.

        Returns:
            Dict with 'metadata' key containing { steamAppId: gameMetadata }
        """
        try:
            cache = load_steam_metadata_cache()
            return {
                "success": True,
                "metadata": cache
            }
        except Exception as e:
            logging.error(f"Error loading Steam metadata cache: {e}")
            return {
                "success": False,
                "error": str(e),
                "metadata": {}
            }

    async def inject_game_to_appinfo(self, shortcut_app_id: int) -> Dict[str, Any]:
        """
        Inject a single game into Steam's appinfo.vdf.
        Called by frontend when user opens game details view for a Unifideck shortcut.

        Args:
            shortcut_app_id: The shortcut's app ID (negative number)

        Returns:
            Dict with 'success' key indicating if injection succeeded
        """
        try:
            success = inject_single_game_to_appinfo(shortcut_app_id)
            return {"success": success}
        except Exception as e:
            logger.error(f"inject_game_to_appinfo failed for {shortcut_app_id}: {e}")
            return {"success": False, "error": str(e)}

    async def start_epic_auth(self) -> Dict[str, Any]:
        """Start Epic Games OAuth authentication"""
        return await self.epic.start_auth()

    async def complete_epic_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete Epic Games OAuth with authorization code"""
        return await self.epic.complete_auth(auth_code)

    async def start_gog_auth(self) -> Dict[str, Any]:
        """Start GOG OAuth authentication"""
        return await self.gog.start_auth()

    async def complete_gog_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete GOG OAuth with authorization code"""
        return await self.gog.complete_auth(auth_code)

    async def start_gog_auth_auto(self) -> Dict[str, Any]:
        """Start GOG OAuth with automatic code detection via CDP"""
        logger.info("[GOG] Starting OAuth authentication")

        try:
            # Generate OAuth URL
            import secrets
            state = secrets.token_urlsafe(32)

            auth_url = (
                f"{self.gog.AUTH_URL}/auth"
                f"?client_id={self.gog.CLIENT_ID}"
                f"&redirect_uri={self.gog.REDIRECT_URI}"
                f"&response_type=code"
                f"&state={state}"
                f"&layout=client2"
            )

            logger.info(f"[GOG] Generated OAuth URL (state={state[:10]}...)")

            # Start CDP monitoring in background to auto-capture code
            asyncio.create_task(self.gog._monitor_and_complete_auth())

            return {
                'success': True,
                'url': auth_url,
                'message': 'Authenticating via browser - code will be captured automatically'
            }

        except Exception as e:
            logger.error(f"[GOG] Error starting auth: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }

    async def logout_epic(self) -> Dict[str, Any]:
        """Logout from Epic Games"""
        return await self.epic.logout()

    async def logout_gog(self) -> Dict[str, Any]:
        """Logout from GOG"""
        return await self.gog.logout()

    async def get_gog_game_languages(self, game_id: str) -> Dict[str, Any]:
        """Get available languages for a GOG game.

        Returns list of language codes (e.g., ['en-US', 'de-DE', 'fr-FR']).
        Only returns multiple languages for games that have them.
        """
        try:
            languages = await self.gog.get_available_languages(game_id)
            return {'success': True, 'languages': languages}
        except Exception as e:
            logger.error(f"[GOG] Error getting languages for {game_id}: {e}")
            return {'success': False, 'error': str(e), 'languages': ['en-US']}

    async def start_amazon_auth(self) -> Dict[str, Any]:
        """Start Amazon Games OAuth authentication via nile"""
        return await self.amazon.start_auth()

    async def complete_amazon_auth(self, auth_code: str) -> Dict[str, Any]:
        """Complete Amazon Games OAuth with authorization code"""
        return await self.amazon.complete_auth(auth_code)

    async def logout_amazon(self) -> Dict[str, Any]:
        """Logout from Amazon Games"""
        return await self.amazon.logout()

    async def get_amazon_library(self) -> List[Dict[str, Any]]:
        """Get Amazon Games library"""
        games = await self.amazon.get_library()
        return [asdict(game) for game in games]

    async def open_browser(self, url: str) -> Dict[str, Any]:
        """Open URL in system browser (fallback for Steam Deck)"""
        logger.info(f"[BROWSER] Opening URL in system browser: {url[:50]}...")
        try:
            import subprocess
            subprocess.Popen(['xdg-open', url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
            logger.info("[BROWSER] Browser opened successfully")
            return {'success': True}
        except Exception as e:
            logger.error(f"[BROWSER] Failed to open browser: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def get_game_metadata(self) -> Dict[int, str]:
        """
        Get metadata for all Unifideck-managed games
        Returns mapping of appId -> store name
        """
        try:
            shortcuts = await self.shortcuts_manager.read_shortcuts()
            metadata = {}

            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                launch_options = shortcut.get('LaunchOptions', '')

                # Only process Unifideck games (have store prefix)
                if ':' not in launch_options:
                    continue

                store = launch_options.split(':')[0]

                # Get Steam's assigned appid
                steam_appid = shortcut.get('appid')

                if steam_appid:
                    metadata[steam_appid] = store
                    logger.debug(f"Mapped appid {steam_appid} to store '{store}' for {shortcut.get('AppName', 'Unknown')}")
                else:
                    # If Steam hasn't assigned an appid yet, use our generated one
                    generated_appid = self.shortcuts_manager.generate_app_id(
                        shortcut.get('AppName', ''),
                        launch_options  # Use full launch options as exe_path
                    )
                    metadata[generated_appid] = store
                    logger.debug(f"Using generated appid {generated_appid} for {shortcut.get('AppName', 'Unknown')} (store: {store})")

            logger.info(f"Loaded metadata for {len(metadata)} Unifideck games")
            return metadata

        except Exception as e:
            logger.error(f"Error loading game metadata: {e}")
            return {}

    async def get_all_unifideck_games(self) -> List[Dict[str, Any]]:
        """
        Get all Unifideck games with installation status for frontend tab filtering
        
        Uses games.map as the source of truth for installation status.
        This ensures consistency with get_game_info() and works for all install
        locations (internal, SD card, ~/GOG Games, etc.)
        
        Returns:
            List of dicts: [{'appId': int, 'store': str, 'isInstalled': bool, 'title': str, 'steamAppId': int|None}]
        """
        try:
            # Load steam_app_id cache for ProtonDB lookups
            steam_appid_cache = load_steam_appid_cache()
            
            shortcuts = await self.shortcuts_manager.read_shortcuts()
            games = []
            
            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                launch_options = shortcut.get('LaunchOptions', '')
                
                # Only process Unifideck games - use proper parser that handles LSFG etc.
                if not is_unifideck_shortcut(launch_options):
                    continue
                
                # Use robust parser that extracts store:id from anywhere in the string
                parsed = extract_store_id(launch_options)
                if not parsed:
                    continue
                store, game_id = parsed

                # Check installation status using games.map (authoritative source)
                # This works for any install location and auto-cleans stale entries
                is_installed = self.shortcuts_manager._is_in_game_map(store, game_id)
                
                # Get appId
                app_id = shortcut.get('appid')
                if not app_id:
                    continue
                
                # Get steam_app_id from cache (for ProtonDB lookup)
                steam_app_id = steam_appid_cache.get(app_id)
                
                games.append({
                    'appId': app_id,
                    'store': store,
                    'isInstalled': is_installed,
                    'title': shortcut.get('AppName', ''),
                    'gameId': game_id,
                    'steamAppId': steam_app_id  # Real Steam appId for ProtonDB
                })
            
            logger.info(f"Loaded {len(games)} Unifideck games for tab filtering")
            return games
            
        except Exception as e:
            logger.error(f"Error getting Unifideck games: {e}")
            return []

    async def get_valid_third_party_shortcuts(self) -> List[int]:
        """
        Get appIds of non-Unifideck shortcuts that have valid executables.
        
        This is used by the frontend to filter out broken third-party shortcuts
        (e.g., from Heroic, Lutris, or manual additions) that don't have a valid
        Exe path and shouldn't appear on the Installed tab.
        
        Returns:
            List of valid third-party shortcut appIds
        """
        try:
            shortcuts = await self.shortcuts_manager.read_shortcuts()
            valid_appids = []
            broken_count = 0
            
            for idx, shortcut in shortcuts.get("shortcuts", {}).items():
                launch_options = shortcut.get('LaunchOptions', '')
                
                # Skip Unifideck games - they have their own validation via games.map
                if is_unifideck_shortcut(launch_options):
                    continue
                
                app_id = shortcut.get('appid')
                if not app_id:
                    continue
                
                # Check if the Exe path exists
                exe_path = shortcut.get('Exe', '')
                
                # Valid if:
                # 1. Exe path is non-empty AND file exists, OR
                # 2. It's a URL-based shortcut (steam://, heroic://, etc.)
                if exe_path:
                    # Check for URL-based shortcuts
                    if exe_path.startswith(('steam://', 'heroic://', 'lutris://', 'http://', 'https://')):
                        valid_appids.append(app_id)
                    # Check if file exists on disk
                    elif os.path.exists(exe_path):
                        valid_appids.append(app_id)
                    else:
                        broken_count += 1
                        logger.debug(f"[ThirdParty] Broken shortcut (exe not found): {shortcut.get('AppName', 'Unknown')} - {exe_path}")
                else:
                    broken_count += 1
                    logger.debug(f"[ThirdParty] Broken shortcut (no exe): {shortcut.get('AppName', 'Unknown')}")
            
            logger.info(f"[ThirdParty] Found {len(valid_appids)} valid, {broken_count} broken third-party shortcuts")
            return valid_appids
            
        except Exception as e:
            logger.error(f"Error getting valid third-party shortcuts: {e}")
            return []

    async def get_compat_cache(self) -> Dict[str, Dict]:
        """
        Get the compatibility cache for frontend tab filtering.
        
        Returns the compat_cache.json contents which maps normalized game titles
        to their ProtonDB tier and Steam Deck verified status.
        
        Returns:
            Dict: {normalized_title: {tier, deckVerified, steamAppId, timestamp}}
        """
        try:
            cache = load_compat_cache()
            logger.info(f"Loaded {len(cache)} entries from compat cache for frontend")
            return cache
        except Exception as e:
            logger.error(f"Error loading compat cache: {e}")
            return {}

    async def get_launcher_toasts(self) -> List[Dict[str, Any]]:
        """
        Get pending toast notifications from the launcher script.
        
        The unifideck-launcher writes toasts to a JSON file when showing
        setup notifications (e.g., "Installing Dependencies"). This method
        reads those toasts so the frontend can display them via Steam's
        Gaming Mode toast API.
        
        Returns:
            List of toast dicts: [{title, body, urgency, timestamp}, ...]
        """
        toast_file = os.path.expanduser("~/.local/share/unifideck/launcher_toasts.json")
        
        try:
            if not os.path.exists(toast_file):
                return []
            
            with open(toast_file, 'r') as f:
                toasts = json.load(f)
            
            if not isinstance(toasts, list) or not toasts:
                return []
            
            # Clear the file after reading (atomic: prevents duplicates)
            with open(toast_file, 'w') as f:
                json.dump([], f)
            
            logger.debug(f"[LauncherToasts] Read and cleared {len(toasts)} toast(s)")
            return toasts
            
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"[LauncherToasts] Error reading toast file: {e}")
            return []
        except Exception as e:
            logger.error(f"[LauncherToasts] Unexpected error: {e}")
            return []

    async def set_steamgriddb_api_key(self, api_key: str) -> Dict[str, Any]:
        """Set SteamGridDB API key and initialize client"""
        try:
            if STEAMGRIDDB_AVAILABLE:
                self.steamgriddb_api_key = api_key
                self.steamgriddb = SteamGridDBClient(api_key)
                logger.info("SteamGridDB client initialized with new API key")
                return {'success': True}
            else:
                return {'success': False, 'error': 'errors.steamGridDbUnavailable'}
        except Exception as e:
            logger.error(f"Error setting SteamGridDB API key: {e}")
            return {'success': False, 'error': str(e)}

    async def get_steamgriddb_status(self) -> Dict[str, Any]:
        """Check if SteamGridDB is configured"""
        return {
            'available': STEAMGRIDDB_AVAILABLE,
            'configured': self.steamgriddb is not None,
            'has_api_key': self.steamgriddb_api_key is not None
        }

    async def cleanup_orphaned_artwork(self) -> Dict[str, Any]:
        """Remove artwork files not matching any current shortcut

        This prevents duplicate artwork when game exe path/title changes,
        which generates a new app_id but leaves old artwork orphaned.

        Returns:
            dict: {removed_count: int, removed_files: list}
        """
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return {'removed_count': 0, 'removed_files': []}

        grid_path = Path(self.steamgriddb.grid_path)
        if not grid_path.exists():
            return {'removed_count': 0, 'removed_files': []}

        # Get valid app_ids from shortcuts
        shortcuts = await self.shortcuts_manager.read_shortcuts()
        valid_ids = set()
        for shortcut in shortcuts.get('shortcuts', {}).values():
            app_id = shortcut.get('appid')
            if app_id is not None:
                unsigned_id = app_id if app_id >= 0 else app_id + 2**32
                valid_ids.add(str(unsigned_id))

        # Patterns for artwork files: filename ends with these suffixes
        # The part before the suffix is the app_id
        patterns = ['p.jpg', '_hero.jpg', '_logo.png', '_icon.jpg', '.jpg']

        # Scan and remove orphans
        removed = []
        try:
            for filepath in grid_path.iterdir():
                if not filepath.is_file():
                    continue
                filename = filepath.name

                for pattern in patterns:
                    if filename.endswith(pattern):
                        extracted_id = filename[:-len(pattern)]
                        # Only remove if it looks like a numeric app_id and is orphaned
                        if extracted_id.isdigit() and extracted_id not in valid_ids:
                            try:
                                filepath.unlink()
                                removed.append(filename)
                            except Exception as e:
                                logger.error(f"Error removing orphaned artwork {filename}: {e}")
                        break  # Only match one pattern per file
        except Exception as e:
            logger.error(f"Error scanning grid path for orphaned artwork: {e}")

        if removed:
            logger.info(f"[Cleanup] Removed {len(removed)} orphaned artwork files")

        return {'removed_count': len(removed), 'removed_files': removed}

    async def _delete_game_artwork(self, app_id: int) -> Dict[str, bool]:
        """Delete artwork files for a single game"""
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return {}

        # Convert to unsigned for filename
        unsigned_id = app_id if app_id >= 0 else app_id + 2**32

        deleted = {}
        artwork_files = [
            (f"{unsigned_id}p.jpg", 'grid'),
            (f"{unsigned_id}_hero.jpg", 'hero'),
            (f"{unsigned_id}_logo.png", 'logo'),
            (f"{unsigned_id}_icon.jpg", 'icon'),
            (f"{unsigned_id}.jpg", 'vertical')
        ]

        for filename, art_type in artwork_files:
            filepath = os.path.join(self.steamgriddb.grid_path, filename)
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    deleted[art_type] = True
                    logger.debug(f"Deleted {filename}")
            except Exception as e:
                logger.error(f"Error deleting {filename}: {e}")
                deleted[art_type] = False

        return deleted

    async def perform_full_cleanup(self, delete_files: bool = False) -> Dict[str, Any]:
        """
        Perform complete cleanup of all Unifideck data.
        
        Args:
            delete_files: If True, also delete installed game files from disk
            
        Returns:
            Dict with cleanup stats
        """
        # Prevent deletion during sync
        if self._is_syncing:
            return {
                'success': False,
                'error': 'errors.deleteSyncInProgress'
            }

        async with self._sync_lock:  # Lock to prevent sync during deletion
            try:
                logger.info(f"Starting FULL cleanup (delete_files={delete_files})...")
                
                # Stats
                stats = {
                    'deleted_games': 0,
                    'deleted_artwork': 0,
                    'preserved_shortcuts': 0,
                    'deleted_files_count': 0,
                    'auth_deleted': False,
                    'cache_deleted': False
                }

                # 1. DELETE GAME FILES (Optional)
                # Must be done BEFORE deleting games.map!
                if delete_files:
                    try:
                        import shutil
                        map_file = os.path.expanduser("~/.local/share/unifideck/games.map")
                        if os.path.exists(map_file):
                            logger.info("[Cleanup] Deleting game files...")
                            with open(map_file, 'r') as f:
                                for line in f:
                                    parts = line.strip().split('|')
                                    if len(parts) >= 3:
                                        # key|exe_path|work_dir
                                        install_dir = parts[2]
                                        
                                        # Safety check: ensure we're deleting from expected locations
                                        # Only delete if path contains "Games", "Epic", "GOG", or "unifideck"
                                        # and is NOT root or home root
                                        safe_keywords = ['/Games/', '/Epic', '/GOG', 'unifideck']
                                        is_safe = any(k in install_dir for k in safe_keywords)
                                        home_dir = os.path.expanduser("~")
                                        games_dir = os.path.join(home_dir, "Games")
                                        not_root = install_dir not in ['/', home_dir, games_dir]
                                        
                                        if is_safe and not_root and os.path.exists(install_dir):
                                            logger.info(f"[Cleanup] Deleting install dir: {install_dir}")
                                            shutil.rmtree(install_dir, ignore_errors=True)
                                            stats['deleted_files_count'] += 1
                                        else:
                                            logger.warning(f"[Cleanup] Skipping unsafe/invalid delete path: {install_dir}")
                    except Exception as e:
                        logger.error(f"[Cleanup] Error deleting game files: {e}")

                # 2. DELETE SHORTCUTS & ARTWORK (Existing Logic)
                shortcuts = await self.shortcuts_manager.read_shortcuts()
                unifideck_shortcuts = {}
                original_shortcuts = {}
                
                for idx, shortcut in shortcuts.get('shortcuts', {}).items():
                    launch_opts = shortcut.get('LaunchOptions', '')
                    if is_unifideck_shortcut(launch_opts):
                        unifideck_shortcuts[idx] = shortcut
                    else:
                        original_shortcuts[idx] = shortcut

                # Rebuild shortcuts
                new_shortcuts = {"shortcuts": {}}
                next_idx = 0
                for _, shortcut in original_shortcuts.items():
                    new_shortcuts["shortcuts"][str(next_idx)] = shortcut
                    next_idx += 1

                # Write shortcuts
                await self.shortcuts_manager.write_shortcuts(new_shortcuts)
                stats['deleted_games'] = len(unifideck_shortcuts)
                stats['preserved_shortcuts'] = len(original_shortcuts)

                # Delete artwork
                if self.steamgriddb:
                    for idx, shortcut in unifideck_shortcuts.items():
                        app_id = shortcut.get('appid')
                        if app_id:
                            deleted = await self._delete_game_artwork(app_id)
                            if any(deleted.values()):
                                stats['deleted_artwork'] += 1
                        await asyncio.sleep(0.01)

                # 3. DELETE AUTH TOKENS
                # Epic - ~/.config/legendary/user.json
                # GOG - ~/.config/unifideck/gog_token.json
                # Amazon - ~/.config/nile/user.json
                try:
                    epic_auth = os.path.expanduser("~/.config/legendary/user.json")
                    if os.path.exists(epic_auth):
                        os.remove(epic_auth)
                        logger.info("[Cleanup] Deleted Epic auth token")
                    
                    gog_auth = os.path.expanduser("~/.config/unifideck/gog_token.json")
                    if os.path.exists(gog_auth):
                        os.remove(gog_auth)
                        logger.info("[Cleanup] Deleted GOG auth token")
                    
                    amazon_auth = os.path.expanduser("~/.config/nile/user.json")
                    if os.path.exists(amazon_auth):
                        os.remove(amazon_auth)
                        logger.info("[Cleanup] Deleted Amazon auth token")
                        
                    # Reset in-memory states
                    self.gog = GOGAPIClient(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self) # Re-init to clear tokens
                    self.amazon = AmazonConnector(plugin_dir=DECKY_PLUGIN_DIR, plugin_instance=self) # Re-init Amazon too
                    # Epic relies on legendary CLI existence, which checks file, so it's auto-cleared
                    
                    stats['auth_deleted'] = True
                except Exception as e:
                    logger.error(f"[Cleanup] Error deleting auth tokens: {e}")

                # 4. DELETE CACHES & INTERNAL DATA
                # games.map should only be deleted when delete_files=True
                # It maps installed games to their executables
                files_to_delete = [
                    "~/.local/share/unifideck/game_sizes.json",
                    "~/.local/share/unifideck/shortcuts_registry.json",
                    "~/.local/share/unifideck/download_queue.json",
                    "~/.local/share/unifideck/download_settings.json",
                    os.path.join(get_steam_appid_cache_path()), # SteamGridDB AppID Cache
                    os.path.join(get_steam_real_appid_cache_path()), # Real Steam AppID Cache
                    os.path.join(get_steam_metadata_cache_path()), # Steam metadata cache
                    os.path.join(get_rawg_metadata_cache_path()), # RAWG metadata cache
                    os.path.join(get_unifidb_metadata_cache_path()), # unifiDB metadata cache
                    os.path.join(get_metacritic_metadata_cache_path()), # Metacritic metadata cache
                    os.path.join(get_artwork_attempts_cache_path()) # Artwork attempts cache
                ]
                
                # Only delete games.map and registry if we're also deleting game files (destructive mode)
                if delete_files:
                    files_to_delete.append("~/.local/share/unifideck/games.map")
                    files_to_delete.append("~/.local/share/unifideck/games_registry.json")

                for file_path in files_to_delete:
                    try:
                        full_path = os.path.expanduser(str(file_path))
                        if os.path.exists(full_path):
                            os.remove(full_path)
                            logger.info(f"[Cleanup] Deleted: {full_path}")
                    except Exception as e:
                        logger.error(f"[Cleanup] Error deleting {file_path}: {e}")
                
                stats['cache_deleted'] = True
                
                # Clear in-memory caches
                global _legendary_installed_cache, _legendary_info_cache
                _legendary_installed_cache = {'data': None, 'timestamp': 0, 'ttl': 30}
                _legendary_info_cache = {}

                logger.info(f"Cleanup complete! Stats: {stats}")
                return {
                    'success': True,
                    **stats
                }

            except Exception as e:
                logger.error(f"Error performing full cleanup: {e}", exc_info=True)
                return {
                    'success': False,
                    'error': str(e)
                }

    async def _unload(self):
        """Cleanup on plugin unload"""
        logger.info("[UNLOAD] Stopping background sync service")
        if self.background_sync:
            await self.background_sync.stop()

        # Disconnect CDP client
        try:
            await shutdown_cdp_client()
            logger.info("[UNLOAD] CDP client disconnected")
        except Exception as e:
            logger.warning(f"[UNLOAD] CDP disconnect failed: {e}")

        logger.info("[UNLOAD] Unifideck plugin unloaded")
