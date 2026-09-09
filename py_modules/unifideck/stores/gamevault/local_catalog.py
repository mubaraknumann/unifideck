"""The local vault — a folder of archives read as a GameVault library.

This is the only module that knows local mode exists. It satisfies the two
seams in :mod:`.sources` (``CatalogSource`` and ``LocalVaultLocator``) and
everything downstream — the install pipeline, the executable finder, the
install marker, uninstall, the never-truncate library rule — is the same code
the remote server drives.

Layout of a vault folder::

    ~/Games/UnifideckVault/
        .unifideck-vault           ← sentinel, see `_require_vault`
        README.txt
        Stardew Valley (v1.6) (2016).zip
        Hollow Knight (2017).7z
        disk2/                     ← one level of nesting, for a mounted card
            Celeste (2018).zip
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from unifideck.core.types import Game

from .filename import ParsedName, is_indexable, parse_archive_name, version_sort_key
from .library import GameVaultFetchError
from .sources import AcquiredArchive, safe_dir_name

logger = logging.getLogger(__name__)

STORE_NAME = "gamevault"

# Written into the vault directory when the user connects. Its absence is
# what distinguishes "the vault is empty" from "the vault is not there" —
# see `_require_vault`.
SENTINEL_NAME = ".unifideck-vault"

_README_NAME = "README.txt"
_ID_PREFIX = "lv_"
_ID_LENGTH = 16

# Flat, plus one level of subdirectories. GameVault's own server models extra
# storage as additional mounted volumes under /files rather than arbitrary
# nesting, and matching that keeps a vault folder portable to a real server.
_MAX_DEPTH = 1

_README_TEXT = """Unifideck local vault
=====================

Drop game archives in this folder. They appear in your Steam library after
the next library sync (Unifideck -> Sync Library).

Supported archives
------------------
  .zip  .7z  .rar  .tar  .tar.gz  .tar.bz2  .tar.xz  .tar.zst  .iso  .wim
  .cab

Naming
------
Only the title is required:

  Stardew Valley.zip

Everything else is optional, in parentheses, and helps Unifideck match
artwork and pick the right launcher:

  Title (v1.5.0) (EA) (W_P) (2021).zip
         |        |    |     `- release year
         |        |    `------- W_P Windows game   L_P Linux game
         |        |             W_S Windows setup  L_SW Linux installer
         |        `------------ early access
         `--------------------- version

Games are identified by title and year, so replacing
"Stardew Valley (v1.5) (2016).zip" with "Stardew Valley (v1.6) (2016).zip"
updates the existing entry instead of creating a second one.

Subfolders one level deep are scanned too, which is useful for an SD card
mounted inside the vault.

This file and the .unifideck-vault marker beside it are safe to leave alone.
Removing the marker makes Unifideck treat the folder as missing, which
protects your library from being wiped when a drive fails to mount.
"""


@dataclass(frozen=True)
class VaultEntry:
    """One game in the vault, and the archive chosen to install it."""

    game_id: str
    parsed: ParsedName
    path: Path
    all_versions: tuple[str, ...]


def initialise_vault(vault_dir: Path) -> None:
    """Create the vault folder and its marker. Idempotent.

    The README is rewritten every time so a user who upgrades gets the
    current conventions; the sentinel is only touched when absent.
    """
    vault_dir.mkdir(parents=True, exist_ok=True)
    sentinel = vault_dir / SENTINEL_NAME
    if not sentinel.exists():
        sentinel.write_text(
            "Unifideck local vault marker. Do not delete.\n",
        )
    (vault_dir / _README_NAME).write_text(_README_TEXT)


def prepare_vault(vault_dir: str) -> Path:
    """Validate and create the vault folder. Blocking; call from a thread.

    Takes only the archive folder. Where a game is *installed* is not asked
    here and is not stored: every install already goes through the shared
    storage picker (``pickStorageForInstall`` → ``install_game``'s
    ``install_path``), which knows about SD cards and USB drives and applies
    to all seven stores. A second install-location setting on this one store
    would be a copy of that decision, and the two would disagree the first
    time a user changed one of them.

    Raises ``ValueError`` when the path is unusable and ``OSError`` when the
    directory cannot be created.
    """
    if not vault_dir.strip():
        raise ValueError("Choose a folder for your game archives")
    vault = Path(vault_dir).expanduser()
    if not vault.is_absolute():
        raise ValueError("The archive folder must be an absolute path")
    initialise_vault(vault)
    return vault


class LocalVaultCatalog:
    """Reads a folder of archives as a game library."""

    def __init__(self, vault_dir: str) -> None:
        self._vault_dir = Path(vault_dir).expanduser()

    @property
    def vault_dir(self) -> Path:
        return self._vault_dir

    async def is_present(self) -> bool:
        """True when the vault folder is really there.

        Used by ``is_available`` so an unmounted SD card keeps the store out
        of the sync's store set entirely, rather than letting it answer with
        an empty library. Threaded because a stat on a dead network mount can
        block for seconds.
        """
        return await asyncio.to_thread((self._vault_dir / SENTINEL_NAME).exists)

    # ── CatalogSource ───────────────────────────────────────────────

    async def fetch(self) -> list[Game]:
        """Every game in the vault.

        Raises :class:`GameVaultFetchError` when the folder cannot be read,
        which :class:`GameVaultLibraryReader` turns into ``None``. It must
        never answer a missing drive with ``[]``: the shortcut reconcile
        believes an empty list, and an SD card that has not finished mounting
        leaves a real, empty directory at the mount point. That is what the
        sentinel file distinguishes.
        """
        # Off the event loop: a vault can live on a sleeping SD card, and
        # this coroutine shares the loop with the download queue. The remote
        # catalog's equivalent work is async I/O; this one is a directory
        # walk, so it goes to a thread instead.
        entries = await asyncio.to_thread(self._scan)
        games = [
            Game(
                app_id=0,
                store=STORE_NAME,
                store_game_id=entry.game_id,
                title=entry.parsed.title,
                installed=False,
                metadata={
                    "file_path": str(entry.path),
                    "release_date": (
                        str(entry.parsed.year) if entry.parsed.year else ""
                    ),
                    "early_access": entry.parsed.early_access,
                    "version": entry.parsed.version or "",
                    "game_type": entry.parsed.game_type or "",
                    "is_installer": entry.parsed.is_installer,
                    "versions": list(entry.all_versions),
                },
            )
            for entry in entries.values()
        ]
        logger.info(
            "[GameVault/local] %d game(s) in %s", len(games), self._vault_dir,
        )
        return games

    # ── LocalVaultLocator ───────────────────────────────────────────

    def resolve(self, game_id: str) -> AcquiredArchive | None:
        """The archive to install for *game_id*, re-read from disk.

        Deliberately rescans rather than trusting a cache: an install can
        start minutes after the sync that listed the game, and the user may
        have swapped the file for a newer version in between. A stale path
        would fail the install with a confusing "no such file".
        """
        try:
            entry = self._scan().get(game_id)
        except GameVaultFetchError:
            return None
        if entry is None:
            return None
        return AcquiredArchive(
            path=entry.path,
            title=entry.parsed.title,
            # The title, not the filename: the extracted directory should read
            # "Stardew Valley", not "Stardew Valley (v1.6) (W_P) (2016)".
            dir_name=safe_dir_name(entry.parsed.title),
            prefer_native=entry.parsed.is_linux,
            is_installer=entry.parsed.is_installer,
        )

    # ── Internals ───────────────────────────────────────────────────

    def _scan(self) -> dict[str, VaultEntry]:
        """``{game_id: entry}``, newest version per game."""
        self._require_vault()
        grouped: dict[str, list[tuple[ParsedName, Path]]] = {}
        for path in self._iter_archives():
            parsed = parse_archive_name(path.name)
            if not parsed.title:
                logger.debug(
                    "[GameVault/local] no title parsed from %s; skipping",
                    path.name,
                )
                continue
            grouped.setdefault(parsed.identity, []).append((parsed, path))
        entries: dict[str, VaultEntry] = {}
        for identity, candidates in grouped.items():
            entry = _pick_version(identity, candidates)
            entries[entry.game_id] = entry
        return entries

    def _require_vault(self) -> None:
        if not self._vault_dir.is_dir():
            raise GameVaultFetchError(
                f"vault folder {self._vault_dir} is not there — if it is on a "
                f"removable drive, it may not be mounted",
            )
        if not (self._vault_dir / SENTINEL_NAME).exists():
            raise GameVaultFetchError(
                f"vault folder {self._vault_dir} has no {SENTINEL_NAME} "
                f"marker — refusing to report an empty library from a folder "
                f"that may be an unmounted mount point",
            )

    def _iter_archives(self) -> list[Path]:
        """Indexable files at the vault root and one level below it.

        Explicitly two levels of ``iterdir`` rather than ``rglob``: a user
        who points the vault at a folder that also holds extracted games
        would otherwise pay a full recursive walk of every game tree on
        every sync.
        """
        found: list[Path] = []
        for path in _sorted_children(self._vault_dir):
            if path.is_file():
                if is_indexable(path.name):
                    found.append(path)
            elif _MAX_DEPTH >= 1 and path.is_dir():
                found.extend(
                    child
                    for child in _sorted_children(path)
                    if child.is_file() and is_indexable(child.name)
                )
        return found


def _sorted_children(directory: Path) -> list[Path]:
    """``iterdir`` in a stable order, tolerating an unreadable directory."""
    try:
        return sorted(directory.iterdir())
    except OSError as exc:
        logger.warning("[GameVault/local] could not read %s: %s", directory, exc)
        return []


def _pick_version(
    identity: str, candidates: list[tuple[ParsedName, Path]],
) -> VaultEntry:
    """The highest-versioned archive for one game, plus every version seen."""
    best = max(candidates, key=lambda c: version_sort_key(c[0].version))
    versions = tuple(
        sorted({parsed.version for parsed, _ in candidates if parsed.version}),
    )
    return VaultEntry(
        game_id=_game_id(identity),
        parsed=best[0],
        path=best[1],
        all_versions=versions,
    )


def _game_id(identity: str) -> str:
    """A stable id for a game, derived from title and year.

    Not from the filename: replacing ``Game (v1.0).zip`` with
    ``Game (v1.1).zip`` has to keep the same shortcut, appId, artwork and
    playtime. The ``lv_`` prefix keeps these ids from ever colliding with a
    remote server's numeric ones in the shared install-marker directory.
    """
    digest = hashlib.sha1(
        identity.encode("utf-8"), usedforsecurity=False,
    ).hexdigest()
    return f"{_ID_PREFIX}{digest[:_ID_LENGTH]}"
