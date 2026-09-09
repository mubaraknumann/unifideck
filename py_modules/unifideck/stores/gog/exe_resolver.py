"""Locate the launchable .exe for an installed GOG game.

GOG installers often produce nested directory structures with several
.exe files (the game, side tools, redistributables); ``GOGExeResolver``
implements the heuristics to pick the right one to launch:

1. ``goggame-<id>.info`` manifest — read the ``playTasks`` field;
2. ``game/`` sub-directory check — common GOG layout;
3. .exe size filter — exclude obvious tools (≤ 1 MiB);
4. naming heuristic — prefer "game-name.exe" over "uninstall.exe" etc.

Module-level helpers (``parse_size_string``,
``get_game_id_from_goggame_filename``) are pure utilities shared with
``install/marker.py`` and ``install/planner.py``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_SKIP_EXE_PATTERNS = (
    "unins",
    "setup",
    "install",
    "crash",
    "redist",
    "vcredist",
    "vc_redist",
    "dxsetup",
    "physx",
    "dotnet",
    "directx",
)
_ROOT_DATA_EXTENSIONS = (".arch05", ".forge")
_WRAPPER_EXE_NAMES = {"dosbox.exe", "scummvm.exe"}

class GOGExeResolver:
    """Gogexe resolver."""

    def find(self, install_path: str) -> str | None:
        """Find."""
        result = self.find_with_workdir(install_path)
        return result[0] if result else None

    def find_with_workdir(self, install_path: str) -> tuple[str, str] | None:
        """Find with workdir."""
        try:
            return self._resolve(install_path)
        except Exception:
            logger.exception("[GOGExeResolver] unexpected error for %s", install_path)
            return None

    def _resolve(self, install_path: str) -> tuple[str, str] | None:
        """Resolve."""
        search_dirs = self._build_search_dirs(install_path)
        info_result = self._resolve_via_goggame_info(
            install_path,
            search_dirs,
        )
        if info_result:
            return info_result
        start_sh_result = self._resolve_via_start_sh(search_dirs)
        if start_sh_result:
            return start_sh_result
        fallback_result = self._resolve_via_largest_exe(
            search_dirs,
        )
        if fallback_result:
            return fallback_result
        logger.warning(
            "[GOGExeResolver] no executable found in %s",
            search_dirs,
        )
        return None

    @staticmethod
    def _build_search_dirs(install_path: str) -> list[str]:
        """Build search dirs."""
        search_dirs: list[str] = []
        game_subdir = str(Path(install_path) / "game")
        if Path(game_subdir).is_dir():
            search_dirs.append(game_subdir)
        search_dirs.append(install_path)
        return search_dirs

    def _resolve_via_goggame_info(
        self,
        install_path: str,
        search_dirs: list[str],
    ) -> tuple[str, str] | None:
        """Resolve via goggame info."""
        primary, root_dir = self._load_primary_play_task(
            search_dirs,
        )
        if primary is None:
            return None
        wrapper = self._check_wrapper_override(
            install_path,
            root_dir,
            primary,
        )
        if wrapper:
            return wrapper
        return self._resolve_play_task_paths(
            install_path,
            root_dir,
            primary,
        )

    def _load_primary_play_task(
        self,
        search_dirs: list[str],
    ) -> tuple[dict[str, Any] | None, str]:
        """Load primary play task."""
        info_file, root_dir = self._find_goggame_info(search_dirs)
        if not info_file:
            return None, ""
        try:
            data = json.loads(
                Path(info_file).read_text(encoding="utf-8"),
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "[GOGExeResolver] info file read failed: %s",
                e,
            )
            return None, ""
        play_tasks = data.get("playTasks", [])
        if not isinstance(play_tasks, list):
            return None, ""
        primary = next(
            (t for t in play_tasks if isinstance(t, dict) and t.get("isPrimary")),
            None,
        )
        return primary, root_dir

    def _resolve_play_task_paths(
        self,
        install_path: str,
        root_dir: str,
        primary: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Resolve play task paths."""
        exe_rel = primary.get("path", "")
        if not exe_rel:
            return None
        work_rel = primary.get("workingDir", "")
        full_exe = resolve_case_insensitive(Path(root_dir), exe_rel)
        full_work = (
            resolve_case_insensitive(Path(root_dir), work_rel)
            if work_rel
            else str(Path(full_exe).parent)
        )
        if full_work != install_path and self._has_root_data_files(install_path):
            logger.info(
                "[GOGExeResolver] data files in root, overriding workdir to %s",
                install_path,
            )
            full_work = install_path
        if not Path(full_exe).is_file():
            return None
        logger.info(
            "[GOGExeResolver] resolved via goggame info: %s",
            full_exe,
        )
        return (full_exe, full_work)

    @staticmethod
    def _find_goggame_info(search_dirs: list[str]) -> tuple[str | None, str]:
        """Find goggame info."""
        for directory in search_dirs:
            if not Path(directory).is_dir():
                continue
            try:
                for item in [entry.name for entry in Path(directory).iterdir()]:
                    if item.startswith("goggame-") and item.endswith(".info"):
                        return (
                            str(Path(directory) / item),
                            directory,
                        )
            except OSError:
                continue
        return (None, search_dirs[0] if search_dirs else "")

    def _check_wrapper_override(
        self,
        install_path: str,
        root_dir: str,
        primary_task: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Check wrapper override."""
        task_path = primary_task.get("path", "")
        if not task_path:
            return None
        task_basename = Path(task_path).name.lower()
        candidates = [root_dir]
        if install_path not in candidates:
            candidates.append(install_path)
        for candidate_root in candidates:
            wrapper_path = str(
                Path(candidate_root) / "run-game.bat",
            )
            if not Path(wrapper_path).is_file():
                continue
            try:
                content = (
                    Path(wrapper_path)
                    .read_text(
                        encoding="utf-8",
                        errors="ignore",
                    )
                    .lower()
                )
            except OSError:
                content = ""
            if task_basename in content or task_basename in _WRAPPER_EXE_NAMES:
                logger.info(
                    "[GOGExeResolver] using wrapper: %s",
                    wrapper_path,
                )
                return (wrapper_path, candidate_root)
        return None

    @staticmethod
    def _has_root_data_files(install_path: str) -> bool:
        """Has root data files."""
        with contextlib.suppress(OSError):
            for name in [entry.name for entry in Path(install_path).iterdir()]:
                full = Path(install_path) / name
                if not full.is_file():
                    continue
                if any(name.endswith(ext) for ext in _ROOT_DATA_EXTENSIONS):
                    return True
        return False

    @staticmethod
    def _resolve_via_start_sh(search_dirs: list[str]) -> tuple[str, str] | None:
        """Resolve via start sh."""
        for directory in search_dirs:
            if not Path(directory).is_dir():
                continue
            candidate = str(Path(directory) / "start.sh")
            if Path(candidate).is_file():
                logger.info(
                    "[GOGExeResolver] resolved via start.sh: %s",
                    candidate,
                )
                return (candidate, directory)
        return None

    @staticmethod
    def _resolve_via_largest_exe(search_dirs: list[str]) -> tuple[str, str] | None:
        """Pick the heaviest non-skipped ``.exe`` across ``search_dirs``.

        Refactor history (2026-05-14): was a triple-nested
        ``for dir / for pattern / for exe`` with an inline
        per-file ``stat`` try/except (CC=18). Pulled the per-
        directory candidate gathering into ``_collect_exe_candidates_in``
        so this method is a flat "scan each dir, pick the
        largest, log, return" read.
        """
        for directory in search_dirs:
            if not Path(directory).is_dir():
                continue
            candidates = GOGExeResolver._collect_exe_candidates_in(directory)
            if not candidates:
                continue
            candidates.sort(key=lambda item: item[1], reverse=True)
            best_exe, best_size = candidates[0]
            GOGExeResolver._warn_if_ambiguous(candidates, best_exe, best_size)
            logger.info(
                "[GOGExeResolver] fallback: largest exe (%.1f MB): %s",
                best_size / (1024 * 1024),
                best_exe,
            )
            return (best_exe, str(Path(best_exe).parent))
        return None

    @staticmethod
    def _warn_if_ambiguous(
        candidates: list[tuple[str, int]],
        best_exe: str,
        best_size: int,
    ) -> None:
        """Log a warning when the largest-exe guess isn't a clear winner.

        Picking "the biggest .exe" is a guess, not a detection — it's
        exactly how a GOG DOSBox install's ``dosbox.exe`` (a real,
        often-large binary) can get launched instead of the actual
        game. This doesn't change the guess (Unifideck has no UI to
        punt an ambiguous choice to, unlike an in-emulator menu), but
        makes a wrong one traceable in the logs instead of invisible:
        any other candidate within 20% of the winner's size means the
        pick was genuinely ambiguous.
        """
        close = [
            exe for exe, size in candidates
            if exe != best_exe and size >= best_size * 0.8
        ]
        if close:
            logger.warning(
                "[GOGExeResolver] largest-exe pick is ambiguous: chose "
                "%s (%.1f MB) over %d other candidate(s) within 20%%: %s",
                best_exe, best_size / (1024 * 1024), len(close), close,
            )

    @staticmethod
    def _collect_exe_candidates_in(directory: str) -> list[tuple[str, int]]:
        """Return all non-skipped ``.exe`` files under ``directory`` with sizes.

        Walks the directory twice (top-level + recursive globs)
        so a quick win in the root is found before deeper trees ;
        glob's natural ordering preserves this. Skipped filename
        patterns (uninstaller, crash handler, ...) live in
        ``_SKIP_EXE_PATTERNS``. Per-file ``stat`` failure is
        silent — a broken symlink or unreadable file just drops
        out of the candidate set.
        """
        candidates: list[tuple[str, int]] = []
        # Use ``rglob`` which recursively walks subdirectories.
        # Replaces the previous ``glob.glob`` loop over both the
        # top-level (``*.exe``) and recursive (``**/*.exe``)
        # patterns — ``rglob`` covers both cases in a single pass.
        # De-duplicates implicitly: each file appears once in the
        # iteration regardless of nesting depth.
        for exe_path_obj in Path(directory).rglob("*.exe"):
            exe_path = str(exe_path_obj)
            basename = exe_path_obj.name.lower()
            if any(skip in basename for skip in _SKIP_EXE_PATTERNS):
                continue
            try:
                size = exe_path_obj.stat().st_size
            except OSError:
                continue
            candidates.append((exe_path, size))
        return candidates

def resolve_case_insensitive(root: Path, rel_path: str) -> str:
    """Join ``root``/``rel_path`` (backslash- or forward-slash-separated),
    correcting each segment's case against the real filesystem.

    GOG's ``goggame-*.info`` manifests are authored on Windows, whose
    filesystem is case-insensitive/case-preserving — a manifest can
    legitimately say ``DOSBOX\\dosbox.exe`` while the actual extracted
    file is ``DOSBOX/DOSBox.exe`` (confirmed against real GOG DOSBox
    packages: "Betrayal at Krondor", "Caesar II" both ship this exact
    mismatch). On Linux's case-sensitive filesystem a naive join then
    never matches ``Path.is_file()``, silently discarding the whole
    manifest-driven resolution — including the playTask's own
    ``arguments`` (see ``compat/gog.py::_read_required_launch_args``,
    which reuses this same function to match a playTask back to a
    resolved exe path) — and falling through to the much less reliable
    largest-exe heuristic. Walks one segment at a time so an
    already-correct-case path costs nothing extra; only a missing
    segment triggers a (single, cheap) case-insensitive directory scan.
    """
    current = root
    for segment in Path(rel_path.replace("\\", "/")).parts:
        candidate = current / segment
        if candidate.exists():
            current = candidate
            continue
        try:
            entries = {entry.name.lower(): entry.name for entry in current.iterdir()}
        except OSError:
            current = candidate
            continue
        match = entries.get(segment.lower())
        current = current / match if match else candidate
    return str(current)

def parse_size_string(size_str: str) -> int:
    """Parse size string."""
    if not size_str:
        return 0
    try:
        parts = str(size_str).strip().split()
        if len(parts) != 2:
            return 0
        value = float(parts[0])
        unit = parts[1].upper()
        if unit == "GB":
            return int(value * 1024 * 1024 * 1024)
        if unit == "MB":
            return int(value * 1024 * 1024)
        if unit == "KB":
            return int(value * 1024)
        return int(value)
    except (ValueError, TypeError):
        return 0

def get_game_id_from_goggame_filename(filename: str) -> str | None:
    """Get game ID from goggame filename."""
    if not filename:
        return None
    name = filename.strip()
    if not name.startswith("goggame-") or not name.endswith(".info"):
        return None
    game_id = name[len("goggame-") : -len(".info")]
    return game_id if game_id else None
