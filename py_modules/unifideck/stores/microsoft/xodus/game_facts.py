"""stores/microsoft/xodus/game_facts.py — what a finished download must contain.

The launcher's :func:`~unifideck.launcher.xbox_pc.game_manifest.inspect_game`
reads the package manifest (``MicrosoftGame.config``, any casing, I-010).
The install adds two facts the launch does not need:

* the absolute path of the launch executable, found case-insensitively
  one path part at a time, for ``games.map`` and the install record;
* the package version (``<Identity Version="1.0.5.0">``), which the update
  check sends to the service.

Blocking file I/O: call through ``asyncio.to_thread``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from unifideck.launcher.xbox_pc import xml_lite
from unifideck.launcher.xbox_pc.errors import XboxPcLaunchError
from unifideck.launcher.xbox_pc.game_manifest import (
    CONTAINER_NAME,
    MANIFEST_NAME,
    find_ci,
    inspect_game,
)

logger = logging.getLogger(__name__)

VERSION_RE = re.compile(r"^\d{1,5}\.\d{1,5}\.\d{1,5}\.\d{1,5}\Z")


@dataclass(frozen=True)
class GameFacts:
    """The verified facts of one installed game folder."""

    title: str
    exe: Path
    exe_relative: str
    version: str | None
    title_id: str | None


def resolve_exe(game_dir: Path, executable: str) -> Path | None:
    """``executable`` under ``game_dir``, matched case-insensitively.

    The manifest may use backslashes. A part that is empty, ``.`` or ``..``,
    or a path that leaves ``game_dir``, gives None.
    """
    parts = executable.replace("\\", "/").split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    current = game_dir
    for part in parts:
        found = find_ci(current, part)
        if found is None or found.is_symlink():
            return None
        current = found
    return current if current.is_file() else None


def package_version(game_dir: Path) -> str | None:
    """The manifest's four-part ``Identity`` version, or None."""
    config = find_ci(game_dir, MANIFEST_NAME)
    if config is None:
        return None
    try:
        # The manifest comes from the game package we just verified.
        root = xml_lite.parse_file(config)
    except (OSError, xml_lite.ParseError):
        return None
    for node in root.iter():
        if node.tag == "Identity":
            version = (node.get("Version") or "").strip()
            return version if VERSION_RE.match(version) else None
    return None


def read_game_facts(game_dir: Path) -> tuple[GameFacts | None, str]:
    """``(facts, "")`` for a complete folder, else ``(None, what is missing)``.

    The missing part is one of ``game_config_missing``, ``exe_missing`` or
    ``container_missing``, for the ``xbox_install_incomplete`` error.
    """
    try:
        info = inspect_game(game_dir)
    except XboxPcLaunchError:
        return None, "game_config_missing"
    if not (game_dir / CONTAINER_NAME).is_file():
        return None, "container_missing"
    exe = resolve_exe(info.path, info.executable)
    if exe is None:
        return None, "exe_missing"
    relative = PurePosixPath(*exe.relative_to(info.path).parts).as_posix()
    return GameFacts(
        title=info.title,
        exe=exe,
        exe_relative=relative,
        version=package_version(info.path),
        title_id=info.title_id,
    ), ""
