import decky  # Required for Decky Loader framework
import os
import sys
import logging
import asyncio
import binascii
import struct
import json
import aiohttp.web
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from urllib.parse import parse_qs

# Add plugin directory to Python path for local imports
DECKY_PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR")
if DECKY_PLUGIN_DIR:
    sys.path.insert(0, DECKY_PLUGIN_DIR)

# Import VDF utilities
from vdf_utils import load_shortcuts_vdf, save_shortcuts_vdf

# Import Steam user detection utilities
from steam_user_utils import get_logged_in_steam_user, migrate_user0_to_logged_in_user

# Import SteamGridDB client
try:
    from steamgriddb_client import SteamGridDBClient
    STEAMGRIDDB_AVAILABLE = True
except ImportError:
    STEAMGRIDDB_AVAILABLE = False

# Import Download Manager (modular backend)
from backend.download.manager import get_download_queue, DownloadQueue
from backend.controllers.background_sync_service import BackgroundSyncService
from backend.controllers.size_service import BackgroundSizeFetcher
from backend.controllers.sync_progress import SyncProgress
from backend.cache.compat_cache import load_compat_cache, save_compat_cache
from backend.cache.steam_appid import (
    load_steam_appid_cache,
    save_steam_appid_cache,
)
from helpers.cache_helpers import get_steam_appid, calculate_game_size, read_cache_entry, write_cache_entry

# Import Cloud Save Manager
from cloud_save_manager import CloudSaveManager

# Import resilient launch options parser
from launch_options_parser import extract_store_id, is_unifideck_shortcut, get_full_id, get_store_prefix

# ============================================================================
# NEW MODULAR BACKEND IMPORTS (Phase 1: Available for use alongside old code)
# These will eventually replace the inline class definitions below.
# ============================================================================
try:
    from backend.stores import (
        Store, Game as BackendGame, StoreManager,
        EpicConnector as BackendEpicConnector,
        AmazonConnector as BackendAmazonConnector,
        GOGAPIClient as BackendGOGAPIClient
    )
    from backend.auth import CDPOAuthMonitor as BackendCDPOAuthMonitor
    from backend.compat import (
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
# CDPOAuthMonitor - Now imported from backend.auth module
# ============================================================================
if BACKEND_AVAILABLE:
    CDPOAuthMonitor = BackendCDPOAuthMonitor
    logger.info("Using CDPOAuthMonitor from backend.auth module")
else:
    # Fallback: Define inline if backend not available (should not happen in production)
    logger.warning("Backend not available, CDPOAuthMonitor would need inline definition")
    # The inline class was here but has been removed - backend should always be available
    raise ImportError("backend.auth module is required but not available")


# ============================================================================
# Game dataclass - Now imported from backend.stores.base module
# ============================================================================
if BACKEND_AVAILABLE:
    Game = BackendGame
else:
    raise ImportError("backend.stores module is required but not available")


# Steam App ID Cache - maps shortcut appId to real Steam appId for ProtonDB lookups
# Stored as JSON file in plugin data directory
STEAM_APPID_CACHE_FILE = "steam_appid_cache.json"


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


# RAWG Metadata Cache - stores RAWG API results keyed by game title (lowercase)
RAWG_METADATA_CACHE_FILE = "rawg_metadata_cache.json"


def get_rawg_metadata_cache_path() -> Path:
    """Get path to RAWG metadata cache file"""
    return Path.home() / ".local" / "share" / "unifideck" / RAWG_METADATA_CACHE_FILE


def load_rawg_metadata_cache() -> Dict[str, Dict]:
    """Load RAWG metadata cache. Returns {lowercase_title: rawg_data_dict}"""
    cache_path = get_rawg_metadata_cache_path()
    try:
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading RAWG metadata cache: {e}")
    return {}


def save_rawg_metadata_cache(cache: Dict[str, Dict]) -> bool:
    """Save RAWG metadata cache"""
    cache_path = get_rawg_metadata_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump(cache, f)
        logger.info(f"Saved {len(cache)} RAWG metadata entries to cache")
        return True
    except Exception as e:
        logger.error(f"Error saving RAWG metadata cache: {e}")
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
        steam_appid_cache = load_steam_appid_cache()
        steam_app_id = steam_appid_cache.get(str(shortcut_app_id))

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
        metadata = metadata_cache.get(str(steam_app_id))

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


async def extract_metadata_from_appinfo(games: List, appinfo_data: Dict[int, Dict]) -> Tuple[Dict[int, int], Dict[int, Dict]]:
    """
    Extract metadata for our games from appinfo data by matching titles.

    Returns:
        Tuple of (shortcut_appid_to_steam_appid mapping, steam_appid_to_metadata mapping)
    """
    appid_mapping = {}  # shortcut_appid -> steam_appid
    metadata_results = {}  # steam_appid -> metadata (converted to web API format)

    for game in games:
        if not game.app_id or not game.title:
            continue

        try:
            # Search for Steam App ID by title in appinfo data
            search_lower = game.title.lower().strip()
            steam_app_id = None

            for app_id, app_data in appinfo_data.items():
                try:
                    # Get app name from common section
                    app_common = app_data.get('appinfo', {}).get('common', {})
                    app_name = app_common.get('name', '').lower().strip()

                    if app_name == search_lower:
                        steam_app_id = app_id
                        break
                except:
                    continue

            if not steam_app_id:
                continue

            appid_mapping[game.app_id] = steam_app_id

            # Convert appinfo data to web API format
            if steam_app_id not in metadata_results:
                converted = convert_appinfo_to_web_api_format(steam_app_id, appinfo_data[steam_app_id])
                if converted:
                    metadata_results[steam_app_id] = converted
                    logger.debug(f"Extracted metadata for '{game.title}' (Steam ID: {steam_app_id})")

        except Exception as e:
            logger.debug(f"Failed to extract metadata for '{game.title}': {e}")

    return appid_mapping, metadata_results


# Shortcuts Registry - maps game launch options to appid for reconciliation after plugin reinstall
# Stored in user data directory (survives plugin uninstall/reinstall)
SHORTCUTS_REGISTRY_FILE = "shortcuts_registry.json"


def get_shortcuts_registry_path() -> Path:
    """Get path to shortcuts registry file (in user data, not plugin dir)"""
    return Path.home() / ".local" / "share" / "unifideck" / SHORTCUTS_REGISTRY_FILE


def load_shortcuts_registry() -> Dict[str, Dict]:
    """Load shortcuts registry. Returns {launch_options: {appid, appid_unsigned, title, created}}"""
    registry_path = get_shortcuts_registry_path()
    try:
        if registry_path.exists():
            with open(registry_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading shortcuts registry: {e}")
    return {}


def save_shortcuts_registry(registry: Dict[str, Dict]) -> bool:
    """Save shortcuts registry to file"""
    registry_path = get_shortcuts_registry_path()
    try:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        with open(registry_path, 'w') as f:
            json.dump(registry, f, indent=2)
        logger.info(f"Saved {len(registry)} entries to shortcuts registry")
        return True
    except Exception as e:
        logger.error(f"Error saving shortcuts registry: {e}")
        return False


def register_shortcut(launch_options: str, appid: int, title: str) -> bool:
    """Register a shortcut's appid for future reconciliation"""
    registry = load_shortcuts_registry()
    
    # Calculate unsigned appid for logging/debugging
    appid_unsigned = appid if appid >= 0 else appid + 2**32
    
    registry[launch_options] = {
        'appid': appid,
        'appid_unsigned': appid_unsigned,
        'title': title,
        'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    
    logger.debug(f"Registered shortcut: {launch_options} -> appid={appid} (unsigned={appid_unsigned})")
    return save_shortcuts_registry(registry)


def get_registered_appid(launch_options: str) -> Optional[int]:
    """Get the registered appid for a game, or None if not registered"""
    registry = load_shortcuts_registry()
    entry = registry.get(launch_options)
    return entry.get('appid') if entry else None


# ============================================================================
# BackgroundCompatFetcher - Now imported from backend.compat.library module
# ============================================================================
if BACKEND_AVAILABLE:
    BackgroundCompatFetcher = BackendCompatFetcher
else:
    raise ImportError("backend.compat module is required but not available")



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

        # Initialize sync progress tracker
        self.sync_progress = SyncProgress()

        logger.info("[INIT] Initializing ShortcutsManager")
        self.shortcuts_manager = ShortcutsManager()
        
        # Migrate any data from user 0 to the logged-in user (fixes past user 0 issues)
        logger.info("[INIT] Checking for user 0 data to migrate")
        migration_result = migrate_user0_to_logged_in_user()
        if migration_result.get('shortcuts_migrated', 0) > 0 or migration_result.get('artwork_migrated', 0) > 0:
            logger.info(f"[INIT] User 0 migration: {migration_result['shortcuts_migrated']} shortcuts, {migration_result['artwork_migrated']} artwork files migrated")
        
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
                from backend.download.manager import DownloadStatus
                item.status = DownloadStatus.ERROR
                item.error_message = error_message or "Failed to register game after download"
                logger.error(f"[DownloadComplete] REGISTRATION FAILED for {item.game_title}: {item.error_message}")

        
        self.download_queue.set_on_complete_callback(on_download_complete)
        
        # Set GOG install callback to use GOGAPIClient
        async def gog_install_callback(game_id: str, install_path: str = None, progress_callback=None):
            """Delegate GOG downloads to GOGAPIClient.install_game"""
            return await self.gog.install_game(game_id, install_path, progress_callback)
        
        self.download_queue.set_gog_install_callback(gog_install_callback)
        
        # Set size cache callback to update Install button sizes when accurate size is received
        self.download_queue.set_size_cache_callback(cache_game_size)

        logger.info("[INIT] Unifideck plugin initialization complete")

    # Frontend-callable methods

    async def has_artwork(self, app_id: int) -> bool:
        """Check if artwork files exist for this app_id"""
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return False

        # Convert signed int32 to unsigned for filename check (same as download logic)
        # Steam artwork files use unsigned app IDs even though shortcuts.vdf stores signed
        # Example: -1257913040 (signed) -> 3037054256 (unsigned)
        unsigned_id = app_id if app_id >= 0 else app_id + 2**32

        # Check for any of the 4 artwork types
        grid_path = Path(self.steamgriddb.grid_path)
        artwork_files = [
            grid_path / f"{unsigned_id}p.jpg",     # Vertical grid (460x215)
            grid_path / f"{unsigned_id}_hero.jpg", # Hero image (1920x620)
            grid_path / f"{unsigned_id}_logo.png", # Logo
            grid_path / f"{unsigned_id}_icon.jpg"  # Icon
        ]
        return any(f.exists() for f in artwork_files)

    async def get_missing_artwork_types(self, app_id: int) -> set:
        """Check which specific artwork types are missing for this app_id

        Returns:
            set: Set of missing artwork types (e.g., {'grid', 'hero', 'logo', 'icon'})
        """
        if not self.steamgriddb or not self.steamgriddb.grid_path:
            return {'grid', 'hero', 'logo', 'icon'}

        unsigned_id = app_id if app_id >= 0 else app_id + 2**32
        grid_path = Path(self.steamgriddb.grid_path)

        artwork_checks = {
            'grid': grid_path / f"{unsigned_id}p.jpg",
            'hero': grid_path / f"{unsigned_id}_hero.jpg",
            'logo': grid_path / f"{unsigned_id}_logo.png",
            'icon': grid_path / f"{unsigned_id}_icon.jpg"
        }

        return {art_type for art_type, path in artwork_checks.items() if not path.exists()}

    async def fetch_artwork_with_progress(self, game, semaphore):
        """Fetch artwork for a single game with concurrency control and timeout

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
                            store_id=game.id       # Store-specific game ID
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
                logger.info(f"  [{count}/{self.sync_progress.artwork_total}] {game.store.upper()}: {game.title} [{source_str}{sgdb}] ({art_count}/4)")

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

                # Update progress: Fetching games
                self.sync_progress.status = "fetching"
                self.sync_progress.current_game = {
                    "label": "sync.fetchingGameLists",
                    "values": {}
                }
                self.sync_progress.error = None

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

                if fetch_artwork and self.steamgriddb:
                    # STEP 1: Identify games needing SGDB lookup (not in cache)
                    seen_app_ids = set()
                    games_needing_sgdb_lookup = []
                    
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            continue
                        seen_app_ids.add(game.app_id)
                        
                        # Check cache first
                        if game.app_id in steam_appid_cache:
                            game.steam_app_id = steam_appid_cache[game.app_id]
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
                            except Exception as e:
                                logger.debug(f"SGDB lookup failed for {game.title}: {e}")
                            return None
                        
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

                    # Extract Steam metadata from appinfo.vdf (Steam's local cache)
                    if all_games:
                        self.sync_progress.current_game = {
                            "label": "sync.extractingSteamMetadata",
                            "values": {"count": len(all_games)}
                        }

                        # Read appinfo.vdf once (fast - local file)
                        appinfo_data = read_steam_appinfo_vdf()

                        if appinfo_data:
                            existing_metadata = load_steam_metadata_cache()
                            appid_mapping, new_metadata = await extract_metadata_from_appinfo(all_games, appinfo_data)

                            # Update metadata cache
                            if new_metadata:
                                existing_metadata.update(new_metadata)
                                save_steam_metadata_cache(existing_metadata)

                            # Save the shortcut->steam appid mapping for frontend store patching
                            if appid_mapping:
                                steam_appid_cache.update(appid_mapping)
                                save_steam_appid_cache(steam_appid_cache)
                                logger.info(f"Extracted metadata for {len(new_metadata)} games from appinfo.vdf, mapped {len(appid_mapping)} shortcuts to Steam IDs")

                    # === RAWG METADATA PRE-FETCH ===
                    # Pre-populate RAWG cache for games that don't have entries yet
                    # This makes GameInfoPanel load instantly instead of fetching per-view
                    if all_games:
                        rawg_cache = load_rawg_metadata_cache()
                        games_needing_rawg = [g for g in all_games if g.title.lower() not in rawg_cache]

                        if games_needing_rawg:
                            logger.info(f"Sync: Pre-fetching RAWG metadata for {len(games_needing_rawg)} games")
                            self.sync_progress.current_game = {
                                "label": "sync.fetchingEnhancedMetadata",
                                "values": {"count": len(games_needing_rawg)}
                            }

                            async def prefetch_rawg_for_game(game, semaphore):
                                async with semaphore:
                                    try:
                                        rawg_data = await self.fetch_rawg_metadata(game.title)
                                        if rawg_data:
                                            return (game.title.lower(), rawg_data)
                                    except Exception as e:
                                        logger.debug(f"[RAWG Prefetch] Error for {game.title}: {e}")
                                    return None

                            semaphore = asyncio.Semaphore(5)
                            tasks = [prefetch_rawg_for_game(g, semaphore) for g in games_needing_rawg]
                            results = await asyncio.gather(*tasks, return_exceptions=True)

                            for result in results:
                                if isinstance(result, tuple) and result is not None:
                                    rawg_cache[result[0]] = result[1]

                            save_rawg_metadata_cache(rawg_cache)
                            logger.info(f"Sync: Cached RAWG metadata for {sum(1 for r in results if isinstance(r, tuple) and r)} games")

                    # Cleanup orphaned artwork before sync (prevents duplicate files)
                    if self.steamgriddb:
                        self.sync_progress.current_game = {
                            "label": "sync.cleaningOrphanedArtwork",
                            "values": {}
                        }
                        cleanup_result = await self.cleanup_orphaned_artwork()
                        if cleanup_result.get('removed_count', 0) > 0:
                            logger.info(f"Cleaned up {cleanup_result['removed_count']} orphaned artwork files")

                    # STEP 3: Check which games need artwork (quick local file check)
                    self.sync_progress.status = "checking_artwork"
                    self.sync_progress.current_game = {
                        "label": "sync.checkingExistingArtwork",
                        "values": {}
                    }
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            if not await self.has_artwork(game.app_id):
                                games_needing_art.append(game)
                            seen_app_ids.discard(game.app_id)  # Only check once per app_id

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

                # --- STEP 2: UPDATE GAME ICONS ---
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
                self.sync_progress.status = "syncing"
                self.sync_progress.current_game = {
                    "label": "sync.savingShortcuts",
                    "values": {}
                }
                
                # Use valid_stores to prevent deleting shortcuts for stores that failed to sync
                batch_result = await self.shortcuts_manager.add_games_batch(all_games, launcher_script, valid_stores=valid_stores)
                added_count = batch_result.get('added', 0)
                
                if batch_result.get('error'):
                     raise Exception(batch_result['error'])

                # Complete
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

                # Update progress: Fetching games
                self.sync_progress.status = "fetching"
                self.sync_progress.current_game = {
                    "label": "force_sync.migratingOldInstallations",
                    "values": {}
                }
                self.sync_progress.error = None

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

                # Force update all games - rewrite existing shortcuts
                batch_result = await self.shortcuts_manager.force_update_games_batch(all_games, launcher_script, valid_stores=valid_stores)
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

                if self.steamgriddb:
                    # STEP 1: Identify games needing SGDB lookup (not in cache)
                    seen_app_ids = set()
                    games_needing_sgdb_lookup = []
                    
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            continue
                        seen_app_ids.add(game.app_id)
                        
                        # Check cache first
                        if game.app_id in steam_appid_cache:
                            game.steam_app_id = steam_appid_cache[game.app_id]
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
                            except Exception as e:
                                logger.debug(f"SGDB lookup failed for {game.title}: {e}")
                            return None
                        
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

                    # Extract Steam metadata from appinfo.vdf (Steam's local cache)
                    if all_games:
                        self.sync_progress.current_game = {
                            "label": "sync.extractingSteamMetadata",
                            "values": {"count": len(all_games)}
                        }

                        # Read appinfo.vdf once (fast - local file)
                        appinfo_data = read_steam_appinfo_vdf()

                        if appinfo_data:
                            existing_metadata = load_steam_metadata_cache()
                            appid_mapping, new_metadata = await extract_metadata_from_appinfo(all_games, appinfo_data)

                            # Update metadata cache
                            if new_metadata:
                                existing_metadata.update(new_metadata)
                                save_steam_metadata_cache(existing_metadata)

                            # Save the shortcut->steam appid mapping for frontend store patching
                            if appid_mapping:
                                steam_appid_cache.update(appid_mapping)
                                save_steam_appid_cache(steam_appid_cache)
                                logger.info(f"Force Sync: Extracted metadata for {len(new_metadata)} games from appinfo.vdf, mapped {len(appid_mapping)} shortcuts to Steam IDs")

                    # === ENHANCED METADATA: RAWG fallback + Deck Compatibility ===
                    # Fetch metadata from RAWG for games missing data, and deck compat from Steam
                    if all_games:
                        self.sync_progress.current_game = {
                            "label": "sync.fetchingEnhancedMetadata",
                            "values": {"count": len(all_games)}
                        }
                        
                        # Reload metadata cache after appinfo update
                        existing_metadata = load_steam_metadata_cache()
                        rawg_cache = load_rawg_metadata_cache()
                        updated_count = 0

                        # Process in batches with limited concurrency
                        async def fetch_enhanced_metadata_for_game(game, semaphore):
                            """Fetch RAWG + deck compat for a single game"""
                            async with semaphore:
                                try:
                                    # Get signed app_id for cache lookup
                                    app_id = game.app_id
                                    if app_id > 2**31:
                                        app_id_signed = app_id - 2**32
                                    else:
                                        app_id_signed = app_id

                                    # Get Steam App ID from mapping
                                    steam_app_id = steam_appid_cache.get(app_id_signed, 0)

                                    # Get existing metadata for this Steam app
                                    game_meta = existing_metadata.get(steam_app_id, {}) if steam_app_id else {}
                                    updated = False

                                    # Check what's missing - field-level fallback
                                    needs_rawg = (
                                        not game_meta.get('short_description') or
                                        not game_meta.get('developers') or
                                        game_meta.get('metacritic') is None
                                    )
                                    needs_deck = steam_app_id > 0 and game_meta.get('deck_category', 0) == 0

                                    # Fetch RAWG data if needed (check RAWG cache first)
                                    rawg_cache_key = game.title.lower()
                                    if needs_rawg:
                                        rawg_data = rawg_cache.get(rawg_cache_key)
                                        if not rawg_data:
                                            rawg_data = await self.fetch_rawg_metadata(game.title)
                                            if rawg_data:
                                                rawg_cache[rawg_cache_key] = rawg_data
                                        if rawg_data:
                                            if not game_meta.get('short_description') and rawg_data.get('description'):
                                                game_meta['short_description'] = rawg_data['description'][:500]
                                                updated = True
                                            if not game_meta.get('developers') and rawg_data.get('developers'):
                                                game_meta['developers'] = rawg_data['developers']
                                                updated = True
                                            if not game_meta.get('publishers') and rawg_data.get('publishers'):
                                                game_meta['publishers'] = rawg_data['publishers']
                                                updated = True
                                            if game_meta.get('metacritic') is None and rawg_data.get('metacritic'):
                                                game_meta['metacritic'] = rawg_data['metacritic']
                                                updated = True
                                            if not game_meta.get('tags') and rawg_data.get('tags'):
                                                game_meta['tags'] = rawg_data['tags'][:5]
                                                updated = True
                                            if not game_meta.get('genres') and rawg_data.get('genres'):
                                                game_meta['genres'] = [{'description': g} for g in rawg_data['genres'][:4]]
                                                updated = True
                                    
                                    # Fetch deck compat if needed
                                    if needs_deck:
                                        deck_info = await self.fetch_steam_deck_compatibility(steam_app_id)
                                        if deck_info.get('category', 0) > 0:
                                            game_meta['deck_category'] = deck_info['category']
                                            game_meta['deck_test_results'] = deck_info.get('testResults', [])
                                            updated = True
                                    
                                    return (steam_app_id, game_meta, updated)
                                except Exception as e:
                                    logger.debug(f"[EnhancedMeta] Error for {game.title}: {e}")
                                    return (None, None, False)
                        
                        # Run with limited concurrency (5 parallel for API rate limits)
                        semaphore = asyncio.Semaphore(5)
                        tasks = [fetch_enhanced_metadata_for_game(game, semaphore) for game in all_games]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        
                        # Update cache with new metadata
                        for result in results:
                            if isinstance(result, tuple) and result[2]:  # updated=True
                                steam_app_id, game_meta, _ = result
                                if steam_app_id and game_meta:
                                    existing_metadata[steam_app_id] = game_meta
                                    updated_count += 1
                        
                        if updated_count > 0:
                            save_steam_metadata_cache(existing_metadata)
                            logger.info(f"Force Sync: Enhanced metadata for {updated_count} games (RAWG + deck compat)")

                        # Save RAWG cache (populated during batch fetch above)
                        if rawg_cache:
                            save_rawg_metadata_cache(rawg_cache)

                    # Cleanup orphaned artwork before sync (prevents duplicate files)
                    if self.steamgriddb:
                        self.sync_progress.current_game = {
                            "label": "sync.cleaningOrphanedArtwork",
                            "values": {}
                        }
                        cleanup_result = await self.cleanup_orphaned_artwork()
                        if cleanup_result.get('removed_count', 0) > 0:
                            logger.info(f"Cleaned up {cleanup_result['removed_count']} orphaned artwork files")

                    # STEP 3: Artwork handling based on user preference
                    # If resync_artwork=True, re-download ALL artwork (overwrites manual changes)
                    # If resync_artwork=False, only download for games missing artwork
                    self.sync_progress.status = "checking_artwork"
                    self.sync_progress.current_game = {
                        "label": "sync.checking_artwork" if not resync_artwork else "sync.queueRefresh",
                        "values": {}
                    }
                    for game in all_games:
                        if game.app_id in seen_app_ids:
                            if resync_artwork or not await self.has_artwork(game.app_id):
                                games_needing_art.append(game)
                            seen_app_ids.discard(game.app_id)  # Only add once per app_id

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

                # --- STEP 2: UPDATE GAME ICONS ---
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
                force_result = await self.shortcuts_manager.force_update_games_batch(all_games, launcher_script)
                
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
                self.sync_progress.status = "complete"
                self.sync_progress.synced_games = len(all_games)
                self.sync_progress.current_game = {
                    "label": "force_sync.completed",
                    "values": {"updated": updated_count, "artwork": artwork_count}
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

    # RAWG.io API key for fallback metadata
    RAWG_API_KEY = 'ba1f3b6abe404ba993d6ac12479f2977'

    async def fetch_rawg_metadata(self, game_name: str) -> Dict[str, Any]:
        """Fetch game metadata from RAWG.io as fallback.
        
        Args:
            game_name: Name of the game to search for
            
        Returns:
            Dict with: description, genres, tags, developers, publishers, metacritic, website
        """
        try:
            import aiohttp
            import urllib.parse
            
            # Search for game by name
            search_url = f"https://api.rawg.io/api/games?key={self.RAWG_API_KEY}&search={urllib.parse.quote(game_name)}&page_size=1"
            
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        logger.debug(f"[RAWG] Search failed with status {response.status}")
                        return {}
                    
                    search_data = await response.json()
                    
            results = search_data.get('results', [])
            if not results:
                logger.debug(f"[RAWG] No results for '{game_name}'")
                return {}
            
            game = results[0]
            game_id = game.get('id')
            
            # Get detailed game info
            detail_url = f"https://api.rawg.io/api/games/{game_id}?key={self.RAWG_API_KEY}"
            
            connector2 = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector2) as session:
                async with session.get(detail_url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        # Return basic info from search if detail fails
                        return {
                            'name': game.get('name', ''),
                            'description': '',
                            'genres': [g.get('name', '') for g in game.get('genres', [])],
                            'tags': [t.get('name', '') for t in game.get('tags', [])[:10]],
                            'metacritic': game.get('metacritic'),
                            'released': game.get('released', ''),
                        }
                    
                    detail = await response.json()
            
            result = {
                'name': detail.get('name', ''),
                'description': detail.get('description_raw', ''),
                'genres': [g.get('name', '') for g in detail.get('genres', [])],
                'tags': [t.get('name', '') for t in detail.get('tags', [])[:10]],
                'developers': [d.get('name', '') for d in detail.get('developers', [])],
                'publishers': [p.get('name', '') for p in detail.get('publishers', [])],
                'metacritic': detail.get('metacritic'),
                'website': detail.get('website', ''),
                'released': detail.get('released', ''),
            }
            
            logger.info(f"[RAWG] Got metadata for '{game_name}': metacritic={result.get('metacritic')}, {len(result.get('tags', []))} tags")
            return result
            
        except Exception as e:
            logger.warning(f"[RAWG] Failed to fetch metadata for '{game_name}': {e}")
            return {}

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

            # Get Steam App ID from cache (for ProtonDB and Steam community links)
            steam_appid_cache = load_steam_appid_cache()
            # Cache has int keys (load_steam_appid_cache converts them)
            steam_app_id = steam_appid_cache.get(app_id_signed, 0)
            logger.info(f"[MetadataDisplay] Cache lookup: app_id={app_id}, signed={app_id_signed}, cache_size={len(steam_appid_cache)}, steam_app_id={steam_app_id}")

            # Load Steam metadata cache for detailed info
            # This cache only contains data from appinfo.vdf (games that exist on Steam).
            # If steam_app_id is a SteamGridDB ID (used for artwork), it won't be here.
            metadata_cache = load_steam_metadata_cache()
            # Metadata cache also has int keys
            steam_metadata = metadata_cache.get(steam_app_id, {}) if steam_app_id else {}

            # Determine if this game has a real Steam store presence.
            # steam_appid_cache stores both SteamGridDB IDs (for artwork) and real Steam
            # App IDs (from appinfo.vdf). Only IDs with entries in steam_metadata_cache
            # are confirmed real Steam App IDs with working store/community pages.
            #
            # Additional validation: The metadata must have:
            # 1. A valid 'type' field ('game' for games)
            # 2. A matching 'steam_appid' field to confirm it's the right game
            # 3. A 'name' field (all real Steam games have this)
            has_steam_store_page = False
            if steam_metadata:
                meta_type = steam_metadata.get('type', '')
                meta_appid = steam_metadata.get('steam_appid', 0)
                meta_name = steam_metadata.get('name', '')
                # Only consider it a valid Steam store page if:
                # - Type is 'game' (not 'dlc', 'demo', etc.)
                # - The steam_appid in metadata matches what we're looking up
                # - The game has a name
                if meta_type == 'game' and meta_appid == steam_app_id and meta_name:
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
            rawg_genres = []

            # First try Steam metadata for basic info (developer, publisher, description, release date)
            # NOTE: We do NOT use Steam for Metacritic or genres
            if steam_metadata:
                developers = steam_metadata.get('developers', [])
                publishers = steam_metadata.get('publishers', [])
                developer = ', '.join(developers) if developers else ''
                publisher = ', '.join(publishers) if publishers else ''
                description = steam_metadata.get('short_description', '') or steam_metadata.get('detailed_description', '')
                release_info = steam_metadata.get('release_date', {})
                release_date = release_info.get('date', '') if isinstance(release_info, dict) else ''
                if developer:
                    sources['developer'] = 'steam_cache'
                if publisher:
                    sources['publisher'] = 'steam_cache'
                if description:
                    sources['description'] = 'steam_cache'
                if release_date:
                    sources['release_date'] = 'steam_cache'

            # Check RAWG cache first, then fetch if missing
            rawg_cache = load_rawg_metadata_cache()
            rawg_cache_key = title.lower()
            rawg_data = rawg_cache.get(rawg_cache_key)
            rawg_source = None

            if rawg_data:
                rawg_source = 'rawg_cache'
                logger.info(f"[MetadataDisplay] RAWG source=cache for '{title}' (key='{rawg_cache_key}')")
            else:
                logger.info(f"[MetadataDisplay] RAWG cache miss for '{title}' (key='{rawg_cache_key}', cache_size={len(rawg_cache)})")
                rawg_data = await self.fetch_rawg_metadata(title)
                if rawg_data:
                    rawg_source = 'rawg_api'
                    rawg_cache[rawg_cache_key] = rawg_data
                    save_rawg_metadata_cache(rawg_cache)
                    logger.info(f"[MetadataDisplay] RAWG source=api_fetch for '{title}' - saved to cache")
                else:
                    logger.info(f"[MetadataDisplay] RAWG returned no data for '{title}'")

            metacritic = None  # Always from RAWG, never from Steam

            if rawg_data:
                if not description:
                    description = rawg_data.get('description', '')
                    if description:
                        sources['description'] = rawg_source
                if not developer:
                    developer = ', '.join(rawg_data.get('developers', []))
                    if developer:
                        sources['developer'] = rawg_source
                if not publisher:
                    publisher = ', '.join(rawg_data.get('publishers', []))
                    if publisher:
                        sources['publisher'] = rawg_source
                if not release_date:
                    release_date = rawg_data.get('released', '')
                    if release_date:
                        sources['release_date'] = rawg_source
                metacritic = rawg_data.get('metacritic')
                sources['metacritic'] = rawg_source
                rawg_genres = rawg_data.get('genres', [])[:4]
                if rawg_genres:
                    sources['genres'] = rawg_source

            genres = rawg_genres

            # Fetch Steam Deck compatibility - use cached if available
            cached_deck_category = steam_metadata.get('deck_category', 0) if steam_metadata else 0
            cached_deck_results = steam_metadata.get('deck_test_results', []) if steam_metadata else []

            if cached_deck_category > 0:
                deck_category = cached_deck_category
                deck_test_results = cached_deck_results
                sources['deck_compat'] = 'steam_cache'
            else:
                deck_info = await self.fetch_steam_deck_compatibility(steam_app_id)
                deck_category = deck_info.get('category', 0)
                deck_test_results = deck_info.get('testResults', [])
                sources['deck_compat'] = 'steam_api' if deck_category > 0 else 'none'

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

    async def add_to_download_queue(self, game_id: str, game_title: str, store: str, was_previously_installed: bool = False) -> Dict[str, Any]:
        """Add a game to the download queue
        
        Args:
            game_id: Store-specific game identifier
            game_title: Display name
            store: 'epic' or 'gog'
            was_previously_installed: GUARDRAIL - If True, cancel won't delete game files
        """
        try:
            result = await self.download_queue.add_to_queue(
                game_id=game_id,
                game_title=game_title,
                store=store,
                was_previously_installed=was_previously_installed
            )
            logger.info(f"[DownloadQueue] Added {game_title} to queue (was_installed={was_previously_installed}): {result}")
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
            cache = load_steam_appid_cache()  # Returns {shortcut_appid: steam_appid}
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
                    os.path.join(get_steam_appid_cache_path()) # Steam AppID Cache
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

        logger.info("[UNLOAD] Unifideck plugin unloaded")
