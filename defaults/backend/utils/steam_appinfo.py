"""Steam appinfo.vdf binary format utilities.

Reads and writes Steam's appinfo.vdf cache file (supports formats v27-v29).
Steam maintains this file automatically - we read it for metadata lookups
and can write/inject custom entries for non-Steam games.

The appinfo.vdf format is a binary VDF (Valve Data Format) that stores
metadata for all apps Steam knows about.
"""

import struct
import logging
import shutil
import time as time_module
import hashlib
import io
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


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
    text_vdf = _dict_to_text_vdf(sections)
    return hashlib.sha1(text_vdf).digest()


def _encode_appinfo_v29(apps_data: Dict[int, Dict], time_module) -> bytes:
    """Encode apps data to v29 binary format."""
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
