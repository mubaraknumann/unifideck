"""RomM-only install layout normalization."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def normalize_game_directory(game_dir: Path) -> None:
    """Hoist the contents of a lone top-level directory up one level.

    Servers that hold an unpacked game as a folder deliver it as a ZIP with
    one wrapping directory, while a game uploaded as an archive is usually
    already flat. One step only: a game whose real layout has a single
    top-level folder must not be flattened repeatedly. Hidden entries count
    toward "lone". The wrapper is renamed aside first, so a child that shares
    its name cannot collide, and no symlink is followed.
    """
    entries = list(game_dir.iterdir())
    if len(entries) != 1:
        return
    wrapper = entries[0]
    if wrapper.is_symlink() or not wrapper.is_dir():
        return
    aside = game_dir / f".romm-hoist-{secrets.token_hex(6)}"
    os.rename(wrapper, aside)
    for child in aside.iterdir():
        os.rename(child, game_dir / child.name)
    aside.rmdir()
