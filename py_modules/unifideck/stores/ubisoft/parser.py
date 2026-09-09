"""
Ubisoft UPC binary cache parser — configurations + ownership.

UPC stores its catalog in two protobuf-like binary files inside the Wine
prefix:

* ``configuration/configurations`` — one length-prefixed record per
  configured game (install_id, launch_id, and an embedded YAML blob with
  name / space_id / executable);
* ``ownership/{userId}`` — the owned-game records (the install_ids the
  signed-in account actually owns).

Records are framed by a ``0x0A`` marker followed by a varint size; the
body uses ``0x08``/``0x10``/``0x1A`` field markers. The scanner is
**index-based** (``data[offset]``, never ``data[offset:]``) and always
advances by at least one byte, so a malformed/unexpected region can
never wedge it into an O(n²) re-slice or an infinite loop — a regression
that previously hung the whole library sync at "Ubisoft 5/5".

Field extraction from the embedded YAML is done with targeted regexes
(no full YAML parse) — robust against the partial/garbled blobs real UPC
dumps contain, and fast.
"""

import logging
import math
import re
from pathlib import Path
from typing import Any

from .parser_binary import parse_ownership_record

logger = logging.getLogger(__name__)
# Records smaller than this are markers / fragments, not real game
# entries (a genuine game config's YAML blob is several KB).
_MIN_RECORD_SIZE = 500
# Names UPC emits as placeholders — fall back to localized name when hit.
BLACKLISTED_NAMES = frozenset(
    {"gamename", "l1", "l2", "thumbimage", "", "ubisoft game", "name"},
)

class GameConfig:
    """A parsed game entry from the configurations binary."""

    def __init__(self) -> None:
        """Initialize the instance."""
        self.install_id: int = 0
        self.launch_id: int = 0
        self.space_id: str = ""
        self.name: str = ""
        self.executable: str = ""
        self.thumb_image: str = ""
        self.game_identifier: str = ""
        self.yaml_raw: str = ""
        self.third_party_platform: str = ""

    def __repr__(self) -> str:
        """Repr."""
        return (
            f"GameConfig(name={self.name!r}, space_id={self.space_id!r}, "
            f"install_id={self.install_id}, launch_id={self.launch_id})"
        )

# ── varint primitives ────────────────────────────────────────────

def _decode_varint(raw: int) -> int:
    """Decode UPC's compact varint (the 'Konrad' correction formula)."""
    if raw > 256 * 256:
        raw -= 128 * 256 * math.ceil(raw / (256 * 256))
        raw -= 128 * math.ceil(raw / 256)
    elif raw > 256:
        raw -= 128 * math.ceil(raw / 256)
    return raw

def _read_varint_at(buf: bytes, offset: int) -> tuple[int, int]:
    """Read a varint at ``offset``; return ``(raw_value, bytes_consumed)``."""
    raw = 0
    consumed = 0
    shift = 0
    while offset + consumed < len(buf):
        byte = buf[offset + consumed]
        raw |= (byte & 0x7F) << shift
        consumed += 1
        shift += 7
        if not (byte & 0x80):
            break
    return raw, consumed

def _read_binary_file(filepath: str, label: str) -> bytes | None:
    """Read a binary file, returning ``None`` (logged) on any failure."""
    if not Path(filepath).is_file():
        logger.warning("[UbiParser] %s file not found: %s", label, filepath)
        return None
    try:
        with Path(filepath).open("rb") as f:
            return f.read()
    except Exception:
        logger.exception("[UbiParser] Failed to read %s", label)
        return None

# ── configurations parser ────────────────────────────────────────

def parse_configurations(filepath: str) -> list[GameConfig]:
    """Parse the UPC ``configurations`` binary into game configs.

    Index-based scan: find a ``0x0A`` record marker, read the varint
    size, and (for real game-sized records) parse the body. ``offset``
    always advances, so the loop is O(n) and can never hang.
    """
    data = _read_binary_file(filepath, "Configurations")
    if data is None:
        return []
    results: list[GameConfig] = []
    offset = 0
    n = len(data)
    while offset < n:
        if data[offset] != 0x0A:
            offset += 1
            continue
        record_start = offset
        try:
            offset += 1  # skip the 0x0A marker
            obj_size_raw, consumed = _read_varint_at(data, offset)
            obj_size = _decode_varint(obj_size_raw)
            offset += consumed
            if obj_size < _MIN_RECORD_SIZE:
                continue
            record_end = record_start + 1 + consumed + obj_size
            if record_end > n:
                break
            record = data[record_start + 1 + consumed : record_end]
            config = _parse_single_record(record)
            if config and config.name:
                results.append(config)
            # Skip the body we just consumed; the next record's 0x0A
            # follows. (A genuine game record can't contain a nested
            # >=500-byte record, so this is safe and avoids re-scanning
            # the YAML blob byte-by-byte.)
            offset = record_end
        except Exception as e:
            logger.debug(
                "[UbiParser] skipping malformed record at %d: %s",
                record_start,
                e,
            )
            offset = record_start + 1
    logger.info(
        "[UbiParser] Parsed %d game configs from %s",
        len(results),
        filepath,
    )
    return results

def _parse_single_record(record: bytes) -> GameConfig | None:
    """Parse one configurations record (header ids + embedded YAML)."""
    config = GameConfig()
    pos = 0
    if pos < len(record) and record[pos] == 0x08:
        pos += 1
        raw, consumed = _read_varint_at(record, pos)
        config.install_id = _decode_varint(raw)
        pos += consumed
    if pos < len(record) and record[pos] == 0x10:
        pos += 1
        raw, consumed = _read_varint_at(record, pos)
        config.launch_id = _decode_varint(raw)
        pos += consumed
    if config.launch_id == 0 or config.launch_id == config.install_id:
        config.launch_id = config.install_id

    yaml_text = _extract_yaml_from_record(record, pos)
    if not yaml_text or "start_game" not in yaml_text:
        return None
    config.yaml_raw = yaml_text
    config.name = _yaml_extract(yaml_text, r"(?:^|\n)\s*name:\s*(.+?)(?:\n|$)")
    config.space_id = _yaml_extract(yaml_text, r"space_id:\s*([a-f0-9\-]+)")
    config.thumb_image = _yaml_extract(yaml_text, r"thumb_image:\s*(.+?)(?:\n|$)")
    config.game_identifier = _yaml_extract(
        yaml_text, r"game_identifier:\s*(.+?)(?:\n|$)",
    )
    # ``third_party_platform`` is usually a nested block
    # (``third_party_platform:\n  name: Steam``) — extract the inner
    # ``name``; fall back to an inline scalar form. This marks Ubisoft
    # entitlements that are really Steam/Epic copies (non-launchable via
    # uplay://), used by the Steam-linked library filter.
    config.third_party_platform = _yaml_extract(
        yaml_text,
        r"third_party_platform:[^\S\n]*\n[^\S\n]*name:\s*(.+?)(?:\n|$)",
    ) or _yaml_extract(
        yaml_text, r"third_party_platform:[^\S\n]*(\S.*?)(?:\n|$)",
    )
    exe_match = re.search(r"relative:\s*(.+?\.exe)", yaml_text, re.IGNORECASE)
    if exe_match:
        config.executable = exe_match.group(1).strip().strip("'\"")
    # UPC sometimes emits a placeholder ``name`` and the real one under
    # ``GAMENAME`` in a localization block.
    if config.name.lower() in BLACKLISTED_NAMES:
        localized = _yaml_extract(yaml_text, r"GAMENAME:\s*(.+?)(?:\n|$)")
        if localized:
            config.name = localized
    return config

def _extract_yaml_from_record(record: bytes, start_pos: int) -> str | None:
    """Extract the YAML blob (after the ``0x1A`` length-prefixed field)."""
    yaml_start = -1
    for i in range(start_pos, len(record)):
        if record[i] == 0x1A:
            yaml_start = i + 1
            break
    if yaml_start < 0:
        return None
    length_raw, consumed = _read_varint_at(record, yaml_start)
    length = _decode_varint(length_raw)
    yaml_start += consumed
    if yaml_start + length > len(record):
        raw = record[yaml_start:]
    else:
        raw = record[yaml_start : yaml_start + length]
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text if text.strip() else None

def _yaml_extract(text: str, pattern: str) -> str:
    """Pull a single value out of the YAML-ish text via regex."""
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip().strip("'\"")
    return ""

# ── ownership parser ─────────────────────────────────────────────

def parse_ownership(filepath: str) -> list[int]:
    """Parse the UPC ``ownership`` binary into a list of owned ids."""
    data = _read_binary_file(filepath, "Ownership")
    if data is None:
        return []
    owned: list[int] = []
    offset = 0x108
    while offset < len(data):
        chunk = data[offset:]
        if chunk[0] != 0x0A:
            break
        record = parse_ownership_record(chunk)
        if record is None:
            break
        rec_size, tmp_size, lid1, lid2 = record
        owned.append(lid1)
        if lid2 != lid1:
            owned.append(lid2)
        offset += rec_size + tmp_size + 1
    logger.info(
        "[UbiParser] Found %d owned IDs in %s",
        len(owned),
        filepath,
    )
    return owned

_OWNERSHIP_UUID_RE = re.compile(
    rb"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
)

def parse_ownership_uuids(filepath: str) -> list[str]:
    """Extract product UUIDs (appId/spaceId) from the UPC ownership binary.

    The ownership binary stores each owned product under BOTH a numeric
    install_id (parsed by :func:`parse_ownership`) AND a product UUID. The
    UUIDs are the modern namespace that matches Ubisoft Connect's public
    Algolia catalog (``uuid_catalog.json`` in unifiDB), so they name the
    modern owned games the legacy install_id → name list doesn't cover.
    Order-preserving, de-duplicated.
    """
    data = _read_binary_file(filepath, "Ownership")
    if data is None:
        return []
    seen: dict[str, None] = {}
    for match in _OWNERSHIP_UUID_RE.findall(data):
        seen.setdefault(match.decode("ascii"), None)
    uuids = list(seen)
    logger.info(
        "[UbiParser] Found %d product UUIDs in %s",
        len(uuids),
        filepath,
    )
    return uuids

def check_install_state(state_file: str) -> bool:
    """A first byte of ``0x0A`` in uplay_install.state means installed."""
    if not Path(state_file).is_file():
        return False
    try:
        with Path(state_file).open("rb") as f:
            return f.read(1) == b"\x0a"
    except Exception:
        return False

def build_id_map_from_configurations(
    filepath: str,
) -> dict[str, dict[str, Any]]:
    """Build a ``space_id → {ids, name, exe}`` map from the configs binary."""
    configs = parse_configurations(filepath)
    id_map: dict[str, dict[str, Any]] = {}
    for cfg in configs:
        if not cfg.space_id:
            continue
        id_map[cfg.space_id] = {
            "install_id": str(cfg.install_id),
            "launch_id": str(cfg.launch_id),
            "name": cfg.name,
            "executable": cfg.executable,
            "game_identifier": cfg.game_identifier,
        }
    logger.info(
        "[UbiParser] Built ID map with %d entries",
        len(id_map),
    )
    return id_map
