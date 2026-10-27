"""stores/microsoft/xodus/case_folding.py — one folder per Windows name.

A package layout can spell one Windows folder two ways (``bin\\game.x64``
for most files, ``bin\\Game.x64`` for one file group). Windows sees one
folder. A Linux filesystem makes two, and every resolver that prefers the
exact spelling (ours, ``xodus-cli``'s launch view, Wine) then finds only
part of the game (I-058).

After a download, :func:`fold_case_collisions` moves the contents of each
such group into the member with the most entries. A name held by a file in
two members is a real conflict: nothing is moved and the path is reported.

Blocking file I/O: call through ``asyncio.to_thread``.
"""
from __future__ import annotations

from pathlib import Path

from unifideck.launcher.xbox_pc.game_manifest import find_ci


class CaseConflictError(Exception):
    """Two spellings of one folder hold the same name, not both folders."""

    def __init__(self, relative: str) -> None:
        """``relative`` is the conflicting path under the game folder."""
        super().__init__(relative)
        self.relative = relative


def _is_dir(path: Path) -> bool:
    """A real directory, never a link to one."""
    return path.is_dir() and not path.is_symlink()


def _entry_count(directory: Path) -> int:
    return sum(1 for _ in directory.iterdir())


def _conflict(root: Path, source: Path, target: Path) -> str | None:
    """The first path that cannot merge from ``source`` into ``target``."""
    for entry in source.iterdir():
        existing = find_ci(target, entry.name)
        if existing is None:
            continue
        if not (_is_dir(entry) and _is_dir(existing)):
            return entry.relative_to(root).as_posix()
        found = _conflict(root, entry, existing)
        if found:
            return found
    return None


def _merge(source: Path, target: Path) -> None:
    """Move everything in ``source`` into ``target``, then remove ``source``."""
    for entry in list(source.iterdir()):
        existing = find_ci(target, entry.name)
        if existing is None:
            entry.rename(target / entry.name)
        else:
            _merge(entry, existing)
    source.rmdir()


def _fold_group(root: Path, members: list[Path], folded: list[str]) -> None:
    """Merge one group of spellings into its largest member."""
    # Sorted first, so equal counts always pick the same keeper.
    keeper = max(sorted(members), key=_entry_count)
    others = [m for m in members if m != keeper]
    for other in others:
        conflict = _conflict(root, other, keeper)
        if conflict:
            raise CaseConflictError(conflict)
    for other in others:
        _merge(other, keeper)
        folded.append(other.relative_to(root).as_posix())


def _fold(root: Path, directory: Path, folded: list[str]) -> None:
    groups: dict[str, list[Path]] = {}
    for entry in directory.iterdir():
        if _is_dir(entry):
            groups.setdefault(entry.name.lower(), []).append(entry)
    for members in groups.values():
        if len(members) > 1:
            _fold_group(root, members, folded)
    for entry in directory.iterdir():
        if _is_dir(entry):
            _fold(root, entry, folded)


def fold_case_collisions(root: Path) -> list[str]:
    """Merge sibling folders whose names differ only in case, at any depth.

    Returns the folders merged away, relative to ``root``. Raises
    :class:`CaseConflictError` before moving anything in a group that
    cannot merge, and ``OSError`` when the filesystem refuses a move.
    """
    folded: list[str] = []
    _fold(root, root, folded)
    return folded
