"""Contracts for stores that acquire and extract game archives."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from unifideck.core.types import Game

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

@dataclass(frozen=True)
class AcquiredArchive:
    """One archive, ready to extract, plus how to name what comes out of it.

    ``title`` and ``dir_name`` travel with the archive rather than being
    re-derived by the installer, because each source knows different things:
    RomM has the catalog's name for the ROM, GameVault's server sends a
    ``Content-Disposition`` filename, and the vault scanner has already parsed
    the title out of the name and can give a directory that is not littered
    with ``(v1.2) (2021)`` tokens.
    """

    path: Path
    title: str
    dir_name: str
    # Both default False so a source that cannot know says nothing rather
    # than guessing. GameVault's remote source is in that position: the server
    # serves a per-version type, but the catalog reader does not read it, so
    # only its local vault labels an entry.
    prefer_native: bool = False
    is_installer: bool = False


class CatalogSource(Protocol):
    """Reads the list of games. Raises when it cannot — never truncates."""

    async def fetch(self) -> list[Game]:
        """Every game, with ``installed`` left False for the reader to fill."""
        ...


class ArchiveSource(Protocol):
    """Supplies the archive bytes for one game."""

    async def acquire(
        self, game_id: str, *, progress_callback: ProgressCallback | None,
    ) -> AcquiredArchive:
        """Put the archive on local disk and describe it."""
        ...

    def release(self, acquired: AcquiredArchive | None) -> None:
        """Called once the install finished or failed. Must never raise."""
        ...

    async def size(self, game_id: str) -> int | None:
        """Download/extract size in bytes, or None when unknown."""
        ...
