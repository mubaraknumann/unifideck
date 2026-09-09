"""Heuristic .exe locator for installed Windows games.

Many stores hand back an install path but not the launcher
``.exe`` itself — Unifideck has to scan the install directory
and pick the right binary. ``ExeFinder`` implements a
score-based search:

* Walk the install tree to depth 3 (deeper trees usually
  hold third-party redistributables / engine internals).
* Skip files matching the ``WRAPPER_EXES`` blocklist —
  these are setup wrappers, crash handlers, prereq
  installers, and generic launchers that masquerade as
  the game.
* For every remaining candidate compute a score:
    + huge bonus if the filename matches a user-supplied
      hint (typically from the store's manifest);
    + depth penalty (shallower files win);
    + size bonus (real games are typically multi-MB
      executables; tiny .exes are usually shims).
* Return the highest-scoring candidate.

Exported as both a class and a module-level singleton
``exe_finder``.
"""
from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

WRAPPER_EXES = {
    "unitycrashhandler64.exe",
    "unitycrashhandler32.exe",
    "crashreportclient.exe",
    "crashpad_handler.exe",
    "bugreport.exe",
    "ue4prereqsetup_x64.exe",
    "dxwebsetup.exe",
    "vcredist_x64.exe",
    "vcredist_x86.exe",
    "dotnetfx35setup.exe",
    "ndp48-x86-x64-allos-enu.exe",
    "dxsetup.exe",
    "unins000.exe",
    "unins001.exe",
    "uninstall.exe",
    "installer.exe",
    "setup.exe",
    "updater.exe",
    "patcher.exe",
    "launcher.exe",
    "unrealcefsubprocess.exe",
}

#: Vendor download-staging directories. Nothing launchable ever lives in one:
#: the client empties it as the last step of an install, so a binary picked
#: from inside is a path that stops existing the moment the game is playable.
#:
#: ``uplay_download`` is Ubisoft Connect's. It is named again in
#: ``stores/ubisoft/library/detection_helpers.UPC_STAGING_DIR``, where the
#: same directory carries a second meaning (its emptiness is UPC's completion
#: signal). The two are deliberately not shared: ``core`` sits below
#: ``stores`` and must not import from it.
STAGING_DIRS = frozenset({"uplay_download"})

class ExeFinder:
    """Score-based .exe picker for game install directories."""

    def find(self, install_path: str, hints: list[str] | None = None) -> str | None:
        """Walk ``install_path``, score every candidate, return the best.

        Empty / missing install path returns ``None``
        immediately. ``hints`` is a list of filenames
        (case-insensitive) from the store's manifest that
        get a large score bonus.

        Args:
            install_path: root directory to walk.
            hints: optional list of preferred filenames.

        Returns:
            Absolute path to the highest-scoring .exe, or
            ``None`` if nothing matched.
        """
        if not install_path or not Path(install_path).is_dir():
            return None
        hint_lower = {h.lower() for h in hints} if hints else set()
        candidates = [
            (
                # Both ``_walk_exe_candidates`` (yield) and
                # ``_score_candidate`` (param) now agree on ``str``
                # for the path — see lot 12d alignment fix on
                # ``_walk_exe_candidates``'s Iterator type.
                self._score_candidate(
                    path,
                    depth,
                    filename,
                    hint_lower,
                ),
                path,
            )
            for path, depth, filename in (self._walk_exe_candidates(install_path))
        ]
        return self._rank_candidates(candidates, install_path)

    def _walk_exe_candidates(
        self, install_path: str,
    ) -> Iterator[tuple[str, int, str]]:
        """Yield ``(path, depth, filename)`` for every viable .exe under root.

        Walk policy:

        * Hard depth cap of 3 — deeper trees rarely hold
          the launcher and walking them blows up scan time
          on engine games with thousands of asset files.
        * ``dirs.clear()`` at the cap prunes the walk
          early (``os.walk`` only descends into dirs still
          in the mutable list).
        * ``STAGING_DIRS`` are pruned the same way, so a
          vendor's download staging tree is never even
          scored — see that constant for why.
        * Skip non-``.exe`` files and the
          ``WRAPPER_EXES`` blocklist.

        Args:
            install_path: root directory.

        Yields:
            ``(full_path, depth, filename)`` triples for
            each candidate.
        """
        for root, dirs, files in os.walk(install_path):
            rel = os.path.relpath(root, install_path)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            dirs[:] = [d for d in dirs if d not in STAGING_DIRS]
            if depth > 3:
                dirs.clear()
                continue
            for filename in files:
                lower = filename.lower()
                if not lower.endswith(".exe"):
                    continue
                if lower in WRAPPER_EXES:
                    continue
                yield str(Path(root) / filename), depth, filename

    @staticmethod
    def _score_candidate(
        full_path: str,
        depth: int,
        filename: str,
        hint_lower: set[Any],
    ) -> int:
        """Compute the heuristic score for one candidate.

        Three components composed:

        * +1000 if the filename matches a hint — dominates
          everything else, so a hint is effectively
          authoritative when available.
        * Depth bonus: ``(4 - depth) * 100`` — root files
          get +400, depth-3 files get +100. Shallower
          wins.
        * Size bonus: ``min(size_mb, 500)`` — caps at 500
          so a single huge .exe doesn't trivially beat
          everything else. Stat errors silently skip the
          size bonus.

        Args:
            full_path: absolute candidate path.
            depth: directory depth from install root (0 =
                root).
            filename: just the basename.
            hint_lower: precomputed lowercased hint set.

        Returns:
            Integer score (higher is better).
        """
        score = 0
        if filename.lower() in hint_lower:
            score += 1000
        score += (4 - depth) * 100
        with contextlib.suppress(OSError):
            size_mb = Path(full_path).stat().st_size // (1024 * 1024)
            score += min(size_mb, 500)
        return score

    @staticmethod
    def _rank_candidates(candidates: list[tuple[Any, ...]], install_path: str) -> str | None:
        """Sort candidates by score descending and return the best path.

        Empty list logs at DEBUG (common case for
        non-Windows games — nothing wrong) and returns
        ``None``. Non-empty logs the winning score + path
        at INFO so operators can audit the heuristic's
        choice without enabling DEBUG.

        Args:
            candidates: ``[(score, path), ...]`` from
                ``find``.
            install_path: root directory (used in the
                empty-case log).

        Returns:
            Highest-scoring path, or ``None`` if list is
            empty.
        """
        if not candidates:
            logger.debug(
                "[ExeFinder] No .exe found in %s",
                install_path,
            )
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_path = candidates[0]
        logger.info(
            "[ExeFinder] Best candidate (score=%d): %s",
            best_score,
            best_path,
        )
        return cast("str | None", best_path)

exe_finder = ExeFinder()
