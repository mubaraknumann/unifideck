"""
Ubisoft binary catalog parser — decode UPC's compiled game database.

In addition to the plaintext catalog handled by ``parser.py``, UPC keeps
a *compiled* representation of the catalog used internally for fast
lookups. This module exposes module-level functions to decode that
binary format: a header section followed by length-prefixed string
records and a checksum trailer.

The decoded records expose the same shape as the plaintext parser's
output so the two can be merged by the library facade.
"""

from __future__ import annotations

import math
from typing import Any


def _convert_data(data: int) -> int:
    """Convert data."""
    if data > 256 * 256:
        data -= 128 * 256 * math.ceil(data / (256 * 256))
        data -= 128 * math.ceil(data / 256)
    elif data > 256:
        data -= 128 * math.ceil(data / 256)
    return data

def parse_record_size(
    header: bytes,
    offset: int,
    second_eight: bool,
) -> tuple[int, int, int]:
    """Parse record size."""
    multiplier = 1
    record_size = 0
    tmp_size = 0
    if second_eight:
        while header[offset] != 0x08 or (
            header[offset] == 0x08 and header[offset + 1] == 0x08
        ):
            record_size += header[offset] * multiplier
            multiplier *= 256
            offset += 1
            tmp_size += 1
    else:
        while header[offset] != 0x08 or record_size == 0:
            record_size += header[offset] * multiplier
            multiplier *= 256
            offset += 1
            tmp_size += 1
    record_size = _convert_data(record_size)
    offset += 1
    return record_size, offset, tmp_size

def parse_install_id(header: bytes, offset: int) -> tuple[int, int]:
    """Parse install ID."""
    multiplier = 1
    install_id = 0
    while header[offset] != 0x10 or header[offset + 1] == 0x10:
        install_id += header[offset] * multiplier
        multiplier *= 256
        offset += 1
    install_id = _convert_data(install_id)
    offset += 1
    return install_id, offset

def parse_launch_id(header: bytes, offset: int) -> tuple[int, int]:
    """Parse launch ID."""
    multiplier = 1
    launch_id = 0
    while header[offset] != 0x1A or (
        header[offset] == 0x1A and header[offset + 1] == 0x1A
    ):
        launch_id += header[offset] * multiplier
        multiplier *= 256
        offset += 1
    launch_id = _convert_data(launch_id)
    return launch_id, offset

def parse_ownership_record(chunk: bytes) -> tuple[Any, ...] | None:
    """Parse ownership record."""
    try:
        pos = 1
        multiplier = 1
        rec_size = 0
        tmp_size = 0
        while chunk[pos] != 0x08 or rec_size == 0:
            rec_size += chunk[pos] * multiplier
            multiplier *= 256
            pos += 1
            tmp_size += 1
        rec_size = _convert_data(rec_size)
        pos += 1
        multiplier = 1
        lid1 = 0
        while chunk[pos] != 0x10 or chunk[pos + 1] == 0x10:
            lid1 += chunk[pos] * multiplier
            multiplier *= 256
            pos += 1
        lid1 = _convert_data(lid1)
        pos += 1
        multiplier = 1
        lid2 = 0
        while chunk[pos] != 0x22:
            lid2 += chunk[pos] * multiplier
            multiplier *= 256
            pos += 1
        lid2 = _convert_data(lid2)
        return rec_size, tmp_size, lid1, lid2
    except Exception:
        return None
