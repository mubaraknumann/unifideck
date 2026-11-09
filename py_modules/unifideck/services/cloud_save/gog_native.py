"""GOG cloud saves for Linux-native installs.

GOG's cloud config only describes Windows (and Mac) save locations, so a
native install has to map them onto where its Linux build really writes.
Two engine families cover GOG's Linux catalogue well, and both map exactly:

* **Unity.** Windows saves in ``<?APPLICATION_DATA_LOCAL_LOW?>/<Company>/
  <Product>``; the same ``Application.persistentDataPath`` on Linux is
  ``$XDG_CONFIG_HOME/unity3d/<Company>/<Product>``. Same files, so the
  folder syncs as is.
* **DOSBox.** GOG's Windows configs run ``mount c ".."`` plus
  ``mount C "..\\cloud_saves" -t overlay``: every file the game writes on
  ``C:`` lands in ``<?INSTALL?>/cloud_saves`` under its ``C:`` path, and
  that folder is what Galaxy syncs. The Linux build mounts ``data/`` as
  ``C:`` with no overlay, so its writes sit among the game files. A
  staging folder stands in for ``cloud_saves``: before a sync it holds
  every file in ``data/`` that the installer did not ship as is (new, or
  a different CRC per gogdl's ``.gogdl-linux-manifest``), and after a
  download its files are copied back into ``data/``.

Anything else (``<?DOCUMENTS?>``, ``<?APPLICATION_DATA_ROAMING?>`` …) has
no reliable Linux equivalent and stays unmapped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import zlib
from pathlib import Path

from unifideck.services.cloud_save.constants import MANIFEST_FILE

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"<\?([A-Z_]+)\?>(.*)")
_UNITY_VARS = frozenset({"APPLICATION_DATA_LOCAL_LOW", "APPLICATION_DATA_LOCALLOW"})
_INSTALL_VARS = frozenset({"INSTALL", "INSTALL_DIR", "GAME_DIR"})
_MANIFEST = ".gogdl-linux-manifest"
_INSTALLER_PREFIX = "data/noarch/"
_DOSBOX_C = "data"


def is_unity_build(install_path: str) -> bool:
    """A Unity Linux build: ``game/<Name>_Data/globalgamemanagers``."""
    return any(Path(install_path, "game").glob("*_Data/globalgamemanagers"))


def is_dosbox_build(install_path: str) -> bool:
    """A GOG Linux DOSBox build: bundled ``dosbox/`` and a ``data/`` drive C."""
    root = Path(install_path)
    return (root / "dosbox").is_dir() and (root / _DOSBOX_C).is_dir()


def _xdg_config() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"


def staging_dir(local_save_root: str, game_id: str, namespace: str) -> Path:
    """Where a DOSBox game's ``cloud_saves`` stand-in lives."""
    return Path(local_save_root).parent / "native-staging" / game_id / namespace


def map_location(
    template: str, install_path: str, staging: Path,
) -> tuple[Path, bool] | None:
    """``(local dir, is_staging)`` for one GOG template, or None if unmapped."""
    match = _TEMPLATE_RE.match(template.strip())
    if not match:
        return None
    var = match.group(1)
    rest = match.group(2).replace("\\", "/").strip("/")
    if var in _UNITY_VARS and rest and is_unity_build(install_path):
        return _xdg_config() / "unity3d" / rest, False
    if var in _INSTALL_VARS and is_dosbox_build(install_path):
        if not (Path(install_path) / _MANIFEST).is_file():
            logger.info(
                "[GOGNative] %s has no %s; cannot tell its saves from its files",
                install_path, _MANIFEST,
            )
            return None
        return staging, True
    return None


def _installer_crcs(install_path: str) -> dict[str, int]:
    """Installer file → CRC32, keyed by path relative to the install dir."""
    data = json.loads((Path(install_path) / _MANIFEST).read_text(encoding="utf-8"))
    return {
        f["file_name"][len(_INSTALLER_PREFIX):]: int(f["crc32"])
        for f in data.get("files", [])
        if str(f.get("file_name", "")).startswith(_INSTALLER_PREFIX)
    }


def _crc32(path: Path) -> int:
    crc = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return crc


def game_written_files(install_path: str) -> list[str]:
    """``C:``-relative paths of files in ``data/`` the installer didn't ship."""
    shipped = _installer_crcs(install_path)
    drive_c = Path(install_path) / _DOSBOX_C
    written: list[str] = []
    for path in sorted(p for p in drive_c.rglob("*") if p.is_file()):
        rel_install = path.relative_to(install_path).as_posix()
        crc = shipped.get(rel_install)
        if crc is None or crc != _crc32(path):
            written.append(path.relative_to(drive_c).as_posix())
    return written


def stage_in(install_path: str, staging: Path) -> int:
    """Rebuild ``staging`` from the game's written files. Returns the count.

    Keeps the last-sync manifest the upload guard compares against.
    """
    staging.mkdir(parents=True, exist_ok=True)
    for child in staging.iterdir():
        if child.name == MANIFEST_FILE:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    files = game_written_files(install_path)
    drive_c = Path(install_path) / _DOSBOX_C
    for rel in files:
        dst = staging / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(drive_c / rel, dst)
    return len(files)


def stage_out(staging: Path, install_path: str) -> int:
    """Copy downloaded files from ``staging`` into ``data/``. Returns the count.

    Only regular files, only below ``data/``: a cloud object can never
    write outside the game's drive C.
    """
    drive_c = (Path(install_path) / _DOSBOX_C).resolve()
    copied = 0
    for src in staging.rglob("*"):
        if not src.is_file() or src.is_symlink() or src.name == MANIFEST_FILE:
            continue
        dst = (drive_c / src.relative_to(staging)).resolve()
        if not dst.is_relative_to(drive_c):
            logger.warning("[GOGNative] refusing to write outside drive C: %s", dst)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied
