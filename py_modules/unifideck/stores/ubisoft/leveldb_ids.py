"""``space_id`` ↔ ``ubisoftConnectGameId`` extraction from UPC's leveldb.

Ubisoft Connect caches the canonical deeplink id (the value
``uplay://launch/{id}/0`` expects) in its localStorage leveldb, next to
the game's ``spaceId``. This module pulls those pairs back out of the
raw ``*.ldb``/``*.log`` blobs — leveldb has no Python reader we can
bundle, so the files are scanned as text.

Issue #436: the previous implementation paired the two keys by *textual
proximity* (a bounded regex gap). Nothing tied a ``spaceId`` to the
``ubisoftConnectGameId`` of the same record, so when UPC serialises the
id before the space id, every record's space id paired with the *next*
record's id — a systematic off-by-one that made Avatar: Frontiers of
Pandora launch Star Wars Outlaws. The scan here is object-scoped
instead: brace depth is tracked, and only values found inside the same
JSON object are paired.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# One pass over the decoded blob yields every token the scanner cares
# about: object boundaries, the leveldb record separator (NUL), and the
# two keys themselves. Ordering matters only in that the alternation is
# tried left-to-right; the groups are disjoint.
_TOKENS = re.compile(
    r'(?P<open>\{)|(?P<close>\})|(?P<nul>\x00)'
    r'|"spaceId"\s*:\s*"(?P<space>[a-f0-9-]+)"'
    r'|"ubisoftConnectGameId"\s*:\s*(?P<connect>\d+)',
    re.IGNORECASE,
)
# Cap per-file read + decode + scan work. localStorage leveldb entries
# are small (KB–low-MB); anything larger is cache spill we shouldn't
# scan synchronously during a library fetch (it would stall the sync).
_MAX_LEVELDB_FILE_BYTES = 16 * 1024 * 1024


class _Frame:
    """One JSON object's worth of collected ids."""

    __slots__ = ("connect", "space")

    def __init__(self) -> None:
        """Initialize the instance."""
        self.space: str | None = None
        self.connect: str | None = None


class _ObjectScanner:
    """Pair the two keys within a single JSON object.

    A stack of :class:`_Frame` mirrors brace depth. Filling a frame's
    second slot emits the pair and clears the frame, so a blob holding
    many records still yields every one of them. When a frame closes
    holding only one of the two values it donates that value to its
    *parent* (a nested object may legitimately carry the id of the
    record that contains it) — but never to a sibling, which is the
    mis-pairing #436 was about. The root frame is a real frame, so a
    truncated blob whose opening brace was cut off still pairs.
    """

    def __init__(self) -> None:
        """Initialize the instance."""
        self._stack: list[_Frame] = [_Frame()]
        self.pairs: list[tuple[str, str]] = []

    def scan(self, text: str) -> None:
        """Walk every token in ``text``, collecting pairs."""
        for match in _TOKENS.finditer(text):
            self._consume(match)
        self._unwind()

    def _consume(self, match: re.Match[str]) -> None:
        """Apply one token to the frame stack."""
        if match.group("open"):
            self._stack.append(_Frame())
        elif match.group("close"):
            self._pop()
        elif match.group("nul"):
            # leveldb record separator: nothing may pair across it.
            self._unwind()
            self._stack[0] = _Frame()
        elif match.group("space"):
            self._fill("space", match.group("space"))
        else:
            self._fill("connect", match.group("connect"))

    def _fill(self, slot: str, value: str) -> None:
        """Record ``value`` in the innermost frame, emitting a full pair."""
        frame = self._stack[-1]
        setattr(frame, slot, value)
        if frame.space and frame.connect:
            self.pairs.append((frame.space, frame.connect))
            frame.space = None
            frame.connect = None

    def _pop(self) -> None:
        """Close the innermost frame, donating leftovers to its parent."""
        if len(self._stack) <= 1:
            return
        frame = self._stack.pop()
        parent = self._stack[-1]
        for slot in ("space", "connect"):
            value = getattr(frame, slot)
            if value and getattr(parent, slot) is None:
                self._fill(slot, value)

    def _unwind(self) -> None:
        """Close every open frame down to the root."""
        while len(self._stack) > 1:
            self._pop()


def extract_cache_game_ids(
    prefix_path: str,
    localstorage_relative_path: str,
) -> dict[str, str]:
    """Map ``space_id`` → ``ubisoftConnectGameId`` from UPC's leveldb cache.

    This is more reliable for native games than the configurations
    launch_id or a community-DB name match. Both Wine layouts (root and
    ``pfx/``) are probed; the first that yields any pairs wins. All
    read/parse errors degrade to an empty/partial result — never an
    exception.
    """
    result: dict[str, str] = {}
    prefix = Path(prefix_path)
    for layout in ("", "pfx"):
        base = prefix / layout if layout else prefix
        leveldb = base / localstorage_relative_path / "leveldb"
        if not leveldb.is_dir():
            continue
        _scan_leveldb_dir(leveldb, result)
        if result:
            logger.info(
                "[UbisoftIdMap] extracted %d ubisoftConnectGameId mappings from cache",
                len(result),
            )
            return result
    return result


def _scan_leveldb_dir(leveldb: Path, result: dict[str, str]) -> None:
    """Scan every ``*.ldb``/``*.log`` file in a leveldb dir into ``result``.

    Oversized files are skipped (cache spill we must not parse synchronously
    during a library fetch); read errors degrade to a skip. Mutates
    ``result`` in place via :func:`_extract_ids_from_binary`.
    """
    files = sorted(leveldb.glob("*.ldb")) or sorted(leveldb.glob("*.log"))
    for ldb_file in files:
        try:
            if ldb_file.stat().st_size > _MAX_LEVELDB_FILE_BYTES:
                logger.debug(
                    "[UbisoftIdMap] skipping oversized leveldb file %s",
                    ldb_file,
                )
                continue
            content = ldb_file.read_bytes()
        except OSError as e:
            logger.debug(
                "[UbisoftIdMap] leveldb read failed for %s: %s",
                ldb_file,
                e,
            )
            continue
        _extract_ids_from_binary(content, result)


def _extract_ids_from_binary(data: bytes, result: dict[str, str]) -> None:
    """Pull ``spaceId``/``ubisoftConnectGameId`` pairs from a leveldb blob.

    Only values sharing a JSON object are paired (see
    :class:`_ObjectScanner`). The first id seen for a space_id wins
    (``setdefault``), matching the pre-#436 behaviour for the records
    that were already parsed correctly.
    """
    scanner = _ObjectScanner()
    scanner.scan(data.decode("utf-8", errors="ignore"))
    for space_id, connect_id in scanner.pairs:
        result.setdefault(space_id, connect_id)


def drop_conflicting_ids(mapping: dict[str, str]) -> dict[str, str]:
    """Discard any ``ubisoftConnectGameId`` claimed by two or more games.

    A deeplink id identifies exactly one title, so an id held by several
    space ids means the cache scan was fooled (#436) or UPC wrote stale
    data. Trusting either candidate risks launching the wrong game;
    dropping the id degrades to the configurations ``launch_id``, which
    is what happens anyway when no cache is present.
    """
    owners: dict[str, list[str]] = {}
    for space_id, connect_id in mapping.items():
        owners.setdefault(connect_id, []).append(space_id)
    clean = {s: c for s, c in mapping.items() if len(owners[c]) == 1}
    for connect_id, spaces in owners.items():
        if len(spaces) > 1:
            logger.warning(
                "[UbisoftIdMap] ubisoftConnectGameId %s claimed by %d space ids "
                "(%s) — discarding it as untrustworthy",
                connect_id,
                len(spaces),
                ", ".join(sorted(spaces)),
            )
    return clean
