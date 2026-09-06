"""Overlay on-disk install state onto a store's owned-games list.

py_modules/unifideck/stores/shared/install_status.py

Every CLI store fetches two things during a sync — the owned library and a
map of what is installed locally — and then has to fold the second into the
first. Epic, GOG and Amazon each carried their own ``merge_install_status``
doing that (audit §3.4), near-identical but divergent in three places that
mattered. This is the single implementation; the divergences survive as
explicit keyword arguments, each with the reason it exists.

**What is now uniform, and was not.** A falsy or absent install path means
*not installed*, for every store. Epic's and Amazon's copies guarded the
disk check with ``if install_path and not ...is_dir()``, so an entry whose
path key was missing or empty skipped the check entirely and was marked
installed anyway — the exact failure the check was added to prevent (Steam
showing PLAY for a game with no files; see
``tests/unit/test_install_state_verifies_disk.py``). Amazon could reach it:
``amazon_library.read_installed_ids`` defaults the key to ``""``.

**What is deliberately still per-store:**

``verify_dir``
    Epic and Amazon read a CLI-owned ``installed.json`` that can outlive the
    directory it names — "Delete all data", or a manual ``rm``, removes the
    files but not the record — so their paths must be re-checked against
    disk. GOG's map comes from a live ``iterdir`` walk
    (``gog/library.get_installed_map``), so the directory provably existed at
    scan time and there is no separate record to go stale. Re-statting it
    would be pure cost.

``exe_key``
    GOG only, and it must stay that way. GOG's scanned ``executable`` is an
    absolute path, and reconcile needs it: ``_update_games_map_row`` writes
    the launch row only when both ``installed`` and ``exe_path`` are truthy.
    legendary's equivalent field is a **relative Windows path** —
    ``legendary/core.py`` does ``install.executable.replace('\\\\', '/')
    .lstrip('/')`` and joins it onto ``install_path`` — so lifting GOG's
    behaviour to Epic would put a relative path into ``games.map`` and break
    launch. The same holds for nile.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Game

if TYPE_CHECKING:
    from collections.abc import Mapping


def merge_install_status(
    owned: list[Game],
    installed: Mapping[str, Mapping[str, Any]],
    *,
    path_key: str = "install_path",
    exe_key: str | None = None,
    verify_dir: bool = True,
) -> list[Game]:
    """Return *owned* with install state from *installed* overlaid.

    Args:
        owned: the store's owned-games list, every entry
            ``installed=False`` as fetched.
        installed: ``{store_game_id: {...}}`` install records, in whatever
            shape the store's CLI or scanner produces.
        path_key: which key of an install record holds the install
            directory (``"install_path"`` for legendary and GOG's scanner,
            ``"path"`` for nile).
        exe_key: key holding an **absolute** executable path to carry onto
            ``Game.exe_path``, or None to leave the owned value alone. See
            the module docstring before setting this for a new store.
        verify_dir: re-check the recorded directory against disk and treat a
            missing one as not-installed. Leave it on unless the caller's map
            was itself produced by a live directory walk.

    Returns:
        A new list; input ``Game`` objects are not mutated.
    """
    merged: list[Game] = []
    for game in owned:
        entry = installed.get(game.store_game_id)
        install_path = entry.get(path_key) if entry else None
        if not install_path:
            merged.append(game)
            continue
        if verify_dir and not Path(install_path).is_dir():
            merged.append(game)
            continue
        exe = entry.get(exe_key) if (entry and exe_key) else None
        merged.append(
            dataclasses.replace(
                game,
                installed=True,
                install_path=install_path,
                exe_path=exe or game.exe_path,
                # ``replace`` would hand the new record the *same* list and
                # dict objects; all three original copies rebuilt them, so
                # a caller mutating one game's tags cannot reach the other.
                tags=list(game.tags),
                metadata=dict(game.metadata),
            )
        )
    return merged
