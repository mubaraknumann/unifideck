"""services/cloud_save/manifest.py — Manifest file ops.

Manifest (``.unifideck_sync.json``) lives inside each save
directory and records the mtime of every tracked file at the
last successful sync. Source of truth for conflict detection:
local mtimes drifted vs the last-known-good from either side.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from .constants import MANIFEST_FILE

logger = logging.getLogger(__name__)

# Was a local literal. ``constants.py`` exists precisely to hold this and
# said so ("renaming the manifest filename only touches one file") while
# being imported by nothing — this module and ``safety.py`` each carried
# their own copy, so a rename needed three edits. The filename is an
# on-disk marker, so one definition is the point.
_MANIFEST_NAME = MANIFEST_FILE


async def read_manifest(directory: str) -> dict[str, float]:
    """Load the manifest file if it exists, else return ``{}``.

    Missing file, OSError, or malformed JSON all collapse to
    empty dict — callers treat "no manifest" and "corrupt
    manifest" identically (forces a full remote compare).
    Offloaded via ``to_thread`` since read is sync.
    """
    manifest_path = str(Path(directory) / _MANIFEST_NAME)

    if not await asyncio.to_thread(lambda: Path(manifest_path).is_file()):
        return {}

    def _read_sync() -> dict[str, float]:
        try:
            with Path(manifest_path).open(encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                logger.warning("[CloudSaveManifest] %s is not a dict", manifest_path)
                return {}

            # Ensure all values are floats
            return {k: float(v) for k, v in data.items()}
        except Exception as e:
            logger.warning("[CloudSaveManifest] failed to read %s: %s", manifest_path, e)
            return {}

    return await asyncio.to_thread(_read_sync)


async def write_manifest(directory: str, manifest: dict[str, float]) -> None:
    """Write the manifest file atomically (tmp + rename).

    Writes to ``<path>.tmp``, renames into place — readers
    never observe a half-written manifest. OSError logged at
    WARNING but not raised.
    """
    manifest_path = str(Path(directory) / _MANIFEST_NAME)
    tmp_path = manifest_path + ".tmp"

    def _write_sync() -> None:
        try:
            if not Path(directory).is_dir():
                Path(directory).mkdir(parents=True, exist_ok=True)

            with Path(tmp_path).open("w", encoding="utf-8") as f:
                json.dump(manifest, f)
                f.flush()
                os.fsync(f.fileno())

            Path(tmp_path).replace(manifest_path)
        except Exception as e:
            logger.warning("[CloudSaveManifest] failed to write %s: %s", manifest_path, e)
            if Path(tmp_path).exists():
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink()

    await asyncio.to_thread(_write_sync)
