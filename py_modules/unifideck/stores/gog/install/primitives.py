"""Filesystem primitives shared by install/uninstall.

``GOGFolderOps`` is a stateless helper class exposing static methods
for the recurring filesystem operations of the install pipeline:

* ``folder_size(path)`` — sum bytes recursively;
* ``count_files(path)`` — count regular files recursively;
* ``has_goggame_info(path, game_id)`` — check for the GOG marker;
* ``force_cleanup_folder(path)`` — best-effort recursive removal
  (async, runs inside ``asyncio.to_thread``).

Errors on individual files are tolerated (broken symlinks, permission
denied) so a single problematic entry doesn't abort the whole sweep.

Refactor history (2026-05-14):
    * ``force_cleanup_folder`` was at CC=18 — its inner
      ``_sync_cleanup`` closure inlined a 3-level walk with a
      parallel directory pass plus a final ``rmdir`` try/except.
      Pulled the per-entry safe removals into module-level
      helpers (``_unlink_quietly``, ``_rmdir_quietly``) and the
      bottom-up traversal into ``_iter_bottom_up`` so the
      cleanup body is a flat linear walk.
    * Refactored function and helpers use ``pathlib.Path`` ;
      the three sibling methods (``folder_size``,
      ``count_files``, ``has_goggame_info``) are out of scope
      for the complexity gate and will be picked up by the
      dedicated PTH cascade pass.

The async surface is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

def _unlink_quietly(path: Path, counters: dict[str, int]) -> None:
    """``unlink`` a single file, mutating counters on success/failure.

    Counters are passed in (rather than returned) so the caller
    can accumulate ``deleted`` and ``errors`` across an entire
    walk in O(1) per file. Logs at DEBUG on failure — the
    operation is best-effort and noisy logs on the WARNING
    channel during cleanup would be misleading.
    """
    try:
        path.unlink()
    except OSError as err:
        logger.debug(
            "[GOGFolderOps] could not remove %s: %s",
            path.name,
            err,
        )
        counters["errors"] += 1
        return
    counters["deleted"] += 1

def _rmdir_quietly(path: Path) -> None:
    """``rmdir`` swallowing OSError.

    Used for both intermediate dirs (during the bottom-up walk)
    and the final root removal. Silent failure is the right
    behaviour: a directory that can't be removed (non-empty
    because some files failed to delete, or permission denied)
    just stays on disk ; the caller's overall result is "best
    effort" rather than "all-or-nothing".
    """
    with contextlib.suppress(OSError):
        path.rmdir()

def _iter_bottom_up(root: Path) -> list[Path]:
    """Return every descendant of ``root`` sorted deepest-first.

    Pathlib has no ``topdown=False`` knob ; we materialise the
    full list and sort by part count descending so a subsequent
    walk processes children before their parents. List
    materialisation is intentional — for the typical install
    folder (low thousands of entries) it costs less than the
    file-removal syscalls that follow, and lets us guarantee
    the parent-after-children invariant.

    Returns an empty list on any OSError (missing root,
    permission denied), so callers never have to guard the
    iteration site.
    """
    try:
        # ``rglob('*')`` yields files and directories. Sorting
        # by part-count descending puts the deepest entries first.
        return sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    except OSError:
        return []

class GOGFolderOps:
    """Gogfolder ops."""

    @staticmethod
    def folder_size(path: str) -> int:
        """Folder size."""
        total = 0
        with contextlib.suppress(OSError):
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += Path(str(Path(root) / name)).stat().st_size
                    except OSError:
                        continue
        return total

    @staticmethod
    def count_files(path: str) -> int:
        """Count files."""
        count = 0
        with contextlib.suppress(OSError):
            for _root, _dirs, files in os.walk(path):
                count += len(files)
        return count

    @staticmethod
    def has_goggame_info(path: str, game_id: str = "") -> bool:
        """Check whether goggame info."""
        with contextlib.suppress(OSError):
            for name in [entry.name for entry in Path(path).iterdir()]:
                if not name.startswith("goggame-"):
                    continue
                if not name.endswith(".info"):
                    continue
                if not game_id:
                    return True
                if name == f"goggame-{game_id}.info":
                    return True
        return False

    @staticmethod
    async def force_cleanup_folder(path: str) -> None:
        """Force cleanup folder."""

        def _sync_cleanup() -> None:
            """Bottom-up walk removing every file then every dir.

            Iteration order is built by ``_iter_bottom_up``
            (deepest-first) so we delete leaves before parents.
            The final ``_rmdir_quietly(root_p)`` removes the
            now-empty root. Counters accumulate across the walk
            for the final INFO log.
            """
            root_p = Path(path)
            counters: dict[str, int] = {"deleted": 0, "errors": 0}
            for entry in _iter_bottom_up(root_p):
                if entry.is_file() or entry.is_symlink():
                    _unlink_quietly(entry, counters)
                elif entry.is_dir():
                    _rmdir_quietly(entry)
            _rmdir_quietly(root_p)
            logger.info(
                "[GOGFolderOps] force cleanup: %d deleted, %d errors",
                counters["deleted"],
                counters["errors"],
            )

        await asyncio.to_thread(_sync_cleanup)
