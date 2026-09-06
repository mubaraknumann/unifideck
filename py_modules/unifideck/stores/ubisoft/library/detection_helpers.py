"""
Detection helpers — pure functions used by the detection cascade.

A grab-bag of pure-function helpers shared by ``detection.py`` and
``detection_cascade.py``:

* ``looks_like_game_install(path)`` — heuristic checks (size, exe present);
* ``extract_space_id_from_manifest(path)`` — parse a manifest and pull
  the space_id;
* ``fingerprint_executable(exe_name)`` — normalise an .exe name for
  matching against the known-fingerprint dict;
* ``normalise_install_dir_name(name)`` — strip trademark glyphs +
  version suffixes for fuzzy matching.

All helpers are stateless and safe to call concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .detection import _InstallDetector
logger = logging.getLogger(__name__)
_EXE_SKIP_PATTERNS = (
    "unins",
    "setup",
    "install",
    "crash",
    "redist",
    "vcredist",
    "dxsetup",
    "dotnet",
    "upc",
    "uplay",
)
#: UPC's download staging directory, created inside the game folder while a
#: download runs and drained when it finishes. Two things follow, and both
#: matter:
#:
#: * nothing under it may ever be recorded as the game's executable — the
#:   directory is emptied the moment the install actually completes, so a
#:   path taken from it is dead on arrival;
#: * that same emptying is UPC's one honest "I am finished" signal, which is
#:   what ``UbisoftInstallProbe.is_complete`` reads.
#:
#: Both facts were learned the same way. A Splinter Cell install was declared
#: complete 18 minutes early, so finalisation ran while the whole tree was
#: still staged and recorded ``uplay_download/109/…/register.exe`` — an
#: installer helper — as the game's launch target.
UPC_STAGING_DIR = "uplay_download"

#: Path components that disqualify an ``.exe`` whatever it is called.
#: ``_EXE_SKIP_PATTERNS`` matches basenames, and the staging executables have
#: perfectly ordinary ones (``register.exe``, ``UCC.exe``); what disqualifies
#: them is where they live, which nothing used to look at.
_EXE_SKIP_DIRS = (UPC_STAGING_DIR,)
_GAME_INSTALL_MIN_SIZE = 100 * 1024 * 1024
_IN_PREFIX_GAMES_PATH = str(
    Path("drive_c")
    / "Program Files (x86)"
    / "Ubisoft"
    / "Ubisoft Game Launcher"
    / "games"
)
_INSTALL_MARKER_FILENAME = ".unifideck_ubisoft"

def load_json_file_safe(path: str) -> Any | None:
    """Load JSON file safe."""
    try:
        return json.loads(
            Path(path).read_text(
                encoding="utf-8",
                errors="replace",
            ),
        )
    except (OSError, json.JSONDecodeError):
        return None

def walk_install_candidates(
    roots: list[str],
) -> Iterator[tuple[str, str]]:
    """Walk install candidates.

    Every filesystem probe is guarded against ``OSError`` —
    ``PermissionError`` in particular. A configured games root may
    contain an unreadable directory (a microSD ``lost+found``, a
    permission-restricted mount), and an unguarded ``is_dir()`` there
    would propagate out of the whole library fetch and hide the user's
    entire Ubisoft library. Skip what we can't read; keep scanning.
    """
    for base_dir in roots:
        base = Path(base_dir)
        try:
            if not base.is_dir():
                continue
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            yield str(entry), entry.name

def in_prefix_game_roots(prefix_path: str) -> list[str]:
    """In prefix game roots."""
    prefix = Path(prefix_path)
    return [
        str(prefix / _IN_PREFIX_GAMES_PATH),
        str(prefix / "pfx" / _IN_PREFIX_GAMES_PATH),
    ]

def _in_skipped_dir(exe_path: Path, install_path: str) -> bool:
    """True when *exe_path* sits under a directory we must never launch from.

    Checked against the path *relative to* the install root, so a component
    of the user's own games folder — a library at ``~/uplay_download`` — can
    never disqualify every game inside it. A path that isn't under the root
    at all falls back to the whole path, which is the conservative read.
    """
    try:
        parts = exe_path.relative_to(install_path).parts
    except ValueError:
        parts = exe_path.parts
    return any(part in _EXE_SKIP_DIRS for part in parts)

def find_game_executable(
    install_path: str,
) -> str | None:
    """Find the game's launchable ``.exe`` under ``install_path``.

    Two independent filters, because they catch different things: a
    basename blocklist for uninstallers and redistributables, and a path
    blocklist for UPC's staging directory, whose executables are named like
    ordinary ones and are deleted when the install completes.
    """
    if not install_path or not Path(install_path).is_dir():
        return None
    candidates: list[tuple[str, int]] = []
    # Use ``rglob`` which recursively walks subdirectories.
    # Replaces the previous ``glob.glob`` loop over both the
    # top-level (``*.exe``) and recursive (``**/*.exe``) patterns
    # — ``rglob`` covers both cases in a single pass.
    for exe_path_obj in Path(install_path).rglob("*.exe"):
        exe_path = str(exe_path_obj)
        basename = exe_path_obj.name.lower()
        if any(skip in basename for skip in _EXE_SKIP_PATTERNS):
            continue
        if _in_skipped_dir(exe_path_obj, install_path):
            continue
        try:
            size = exe_path_obj.stat().st_size
            candidates.append((exe_path, size))
        except OSError:
            continue
    if not candidates:
        logger.warning(
            "[UbisoftLibrary] no executable found in %s",
            install_path,
        )
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    result, size = candidates[0]
    logger.info(
        "[UbisoftLibrary] found executable (%.1f MB): %s",
        size / (1024 * 1024),
        result,
    )
    return result

def _has_exe_within_depth(path: str, max_depth: int) -> bool:
    """Return True if any ``.exe`` exists within ``max_depth`` of ``path``.

    Recursive walk bounded by ``max_depth`` — bails out as soon
    as the first ``.exe`` is found. Used by
    ``looks_like_game_install`` as the cheap first check before
    the more expensive total-size sweep. ``OSError`` during
    walk is swallowed — partial result counts as "no exe found".
    """
    return _scan_for_exe(Path(path), max_depth)

def _scan_for_exe(directory: Path, remaining_depth: int) -> bool:
    """Recursive helper for :func:`_has_exe_within_depth`.

    Visits every direct entry of ``directory``: returns True
    immediately on the first ``.exe`` file, otherwise descends
    into subdirectories while ``remaining_depth > 0``. A single
    ``iterdir`` call per level keeps the syscall count tight ;
    ``OSError`` on a single level just terminates that branch
    (missing dir, permission denied) and lets siblings continue.
    """
    try:
        subdirs: list[Path] = []
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".exe":
                return True
            if entry.is_dir():
                subdirs.append(entry)
    except OSError:
        return False
    if remaining_depth <= 0:
        return False
    return any(_scan_for_exe(d, remaining_depth - 1) for d in subdirs)

def _total_size_exceeds(path: str, threshold: int) -> bool:
    """Return True as soon as the cumulative file size under
    ``path`` exceeds ``threshold`` bytes.

    Streaming check — bails out the moment the threshold is
    crossed, so the walk doesn't need to visit every file on a
    multi-GB tree. Per-file ``stat`` failure is skipped silently
    (broken symlink). Outer ``OSError`` (permission on a
    directory) terminates the walk early — partial sum stands.
    """
    total = 0
    with contextlib.suppress(OSError):
        for entry in Path(path).rglob("*"):
            if not entry.is_file():
                continue
            try:
                total += entry.stat().st_size
            except OSError:
                continue
            if total > threshold:
                return True
    return False

def looks_like_game_install(path: str) -> bool:
    """Heuristic: does ``path`` look like a real game install?

    Two cheap signals checked in priority order :

        1. Any ``.exe`` within depth 2 — most installs ship an
           executable at the root or one level down.
        2. Cumulative file size above ``_GAME_INSTALL_MIN_SIZE`` —
           covers data-only games (configs, assets, no exe at
           the top) without false-positive on a few stray temp
           files.

    Either signal alone is enough to return True. Returns False
    if neither is hit before the walk completes or errors out.

    Refactor history (2026-05-14):
        * Was a single function at CC=18 — the two phases (exe
          scan + size sweep) shared the same try/except envelope
          which made the nesting hit four levels deep on the
          size branch. Split into two helpers so this function
          is now a flat ``return A or B``.
        * Helpers use ``pathlib.Path`` (depth-bounded
          ``iterdir`` recursion for the exe scan, ``rglob`` for
          the size sweep) to align with the broader pathlib
          migration on ``stores/``.
    """
    return _has_exe_within_depth(path, max_depth=2) or _total_size_exceeds(
        path, _GAME_INSTALL_MIN_SIZE,
    )

async def write_install_marker(
    space_id: str,
    install_path: str,
    executable: str,
    game_title: str = "",
) -> None:
    """Write install marker."""
    try:
        marker_data = {
            "space_id": space_id,
            "game_title": game_title,
            "install_path": install_path,
            "executable": executable,
            # Always serialize timestamps in UTC so the marker is
            # comparable across machines and DST transitions.
            "install_date": (
                datetime.datetime.now(datetime.UTC).isoformat()
            ),
        }
        install_p = Path(install_path)
        marker_path = install_p / _INSTALL_MARKER_FILENAME
        tmp_path = marker_path.with_suffix(
            marker_path.suffix + ".tmp",
        )
        await asyncio.to_thread(lambda: install_p.mkdir(parents=True, exist_ok=True))
        tmp_path.write_text(
            json.dumps(marker_data, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(marker_path)
        logger.info(
            "[UbisoftLibrary] wrote install marker for %s",
            space_id,
        )
    except OSError as e:
        logger.warning(
            "[UbisoftLibrary] marker write failed: %s",
            e,
        )

def write_marker_sync(
    install_path: str,
    space_id: str,
    title: str,
) -> None:
    """Write marker sync."""
    marker_path = Path(install_path) / _INSTALL_MARKER_FILENAME
    if marker_path.exists():
        return
    marker_data = {
        "space_id": space_id,
        "install_path": install_path,
        "game_title": title,
    }
    with contextlib.suppress(OSError):
        marker_path.write_text(
            json.dumps(marker_data),
            encoding="utf-8",
        )

class _DetectionHelpers:
    """Detection helpers."""

    def __init__(self, parent: _InstallDetector) -> None:
        """Initialize the instance."""
        self._parent = parent

    def get_external_game_roots(self) -> list[str]:
        """Get external game roots."""
        config = self._parent._config
        roots: list[str] = [
            config.default_install_base_expanded,
            config.sdcard_install_base,
        ]
        self._append_custom_path_root(roots, config)
        self._append_mounted_media_roots(roots)
        return self._dedup_roots_by_realpath(roots)

    @staticmethod
    def _append_custom_path_root(
        roots: list[str],
        config: Any,
    ) -> None:
        """Append custom path root."""
        if config is None:
            return
        settings_file = str(Path(config.data_dir_expanded) / "download_settings.json")
        if not Path(settings_file).is_file():
            return
        settings = load_json_file_safe(settings_file)
        if not isinstance(settings, dict):
            return
        custom_path = settings.get("custom_path")
        if isinstance(custom_path, str) and custom_path:
            roots.append(
                str(Path(custom_path) / "Ubisoft"),
            )
            roots.append(custom_path)

    @staticmethod
    def _append_mounted_media_roots(roots: list[str]) -> None:
        """Append mounted media roots."""
        media_base = Path("/run/media")
        if not media_base.is_dir():
            return
        with contextlib.suppress(OSError):
            for entry_path in media_base.iterdir():
                if not entry_path.is_dir():
                    continue
                roots.append(
                    str(entry_path / "Games" / "Ubisoft"),
                )
                _DetectionHelpers._append_sub_mount_roots(
                    entry_path,
                    roots,
                )

    @staticmethod
    def _append_sub_mount_roots(
        parent: Path,
        roots: list[str],
    ) -> None:
        """Append sub mount roots."""
        with contextlib.suppress(OSError):
            for sub_path in parent.iterdir():
                if sub_path.is_dir():
                    roots.append(
                        str(sub_path / "Games" / "Ubisoft"),
                    )

    @staticmethod
    def _dedup_roots_by_realpath(
        roots: list[str],
    ) -> list[str]:
        """Dedup roots by realpath."""
        seen: set[str] = set()
        unique: list[str] = []
        for r in roots:
            try:
                real = str(Path(r).resolve())
            except OSError:
                real = r
            if real not in seen:
                seen.add(real)
                unique.append(r)
        return unique
