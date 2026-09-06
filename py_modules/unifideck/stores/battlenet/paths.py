"""Prefix and file-path resolution for the Battle.net store.

py_modules/unifideck/stores/battlenet/paths.py

Unifideck gives every game its own prefix — the deliberate difference from
NonSteamLaunchers, where one shared prefix means one bad client update
takes out the whole library. Battle.net therefore uses the Ubisoft
three-tier layout::

    <prefixes_dir>/.bnet-auth      the user signs into the client here, once
    <prefixes_dir>/.template       pristine, pre-warmed, no games
    <prefixes_dir>/<uid>           one per game, cloned from .template

Two on-device findings shape this module:

* **umu rewrites ``WINEPREFIX`` to ``<prefix>/pfx/`` and creates ``pfx -> .``
  as a self-symlink**, so ``<prefix>/drive_c`` and ``<prefix>/pfx/drive_c``
  are the same directory. Everything here goes through
  ``resolve_drive_c``; combining ``prefix / "drive_c"`` naively is the bug
  that made ``ubisoft_recovery`` fail to find a genuinely present exe.
* A per-game prefix path is **recorded, never reconstructed** from the game
  id. ``prefix_setup.py`` documents a Ubisoft incident where a
  reconstructed path stamped a marker into a directory no launch ever
  opened, causing a permanent reset loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_drive_c

if TYPE_CHECKING:
    from collections.abc import Callable

# Prefix directory names. Dot-prefixed so a game uid can never collide.
AUTH_PREFIX_NAME = ".bnet-auth"
TEMPLATE_PREFIX_NAME = ".template"

# Client layout inside a prefix's drive_c.
CLIENT_DIR = "Program Files (x86)/Battle.net"
# Takes --exec. Confirmed on-device: the Launcher does not.
CLIENT_EXE = "Battle.net.exe"
# Owns the wineserver session; started first in a two-phase launch.
LAUNCHER_EXE = "Battle.net Launcher.exe"
# The client itself, inside the versioned payload dir. Lowercased on disk;
# matched case-insensitively anyway since ext4 under the prefix is
# case-sensitive while Wine's view is not. See :func:`client_payload_dir`.
CLIENT_DLL = "battle.net.dll"
CLIENT_CONFIG = "users/steamuser/AppData/Roaming/Battle.net/Battle.net.config"

# Marker written into a prefix we built, so ownership is provable rather
# than inferred from the path (appid inference nearly deleted 1 GB of user
# prefixes once already).
PREFIX_MARKER = ".unifideck_battlenet"


def auth_prefix(prefixes_dir: Path) -> Path:
    return Path(prefixes_dir) / AUTH_PREFIX_NAME


def template_prefix(prefixes_dir: Path) -> Path:
    return Path(prefixes_dir) / TEMPLATE_PREFIX_NAME


def game_prefix(prefixes_dir: Path, uid: str) -> Path:
    """Default per-game prefix path.

    Only for *creating* a prefix. To find an existing one, read the
    recorded path from the id map — never rebuild it from the uid.
    """
    return Path(prefixes_dir) / uid


def drive_c(prefix: Path) -> Path | None:
    """Resolve a prefix's drive_c across both layouts, or None."""
    return resolve_drive_c(Path(prefix))


def _existing(
    base: Path | None, name: str, predicate: Callable[[Path], bool],
) -> Path | None:
    """``base / name`` if it exists and satisfies *predicate*, else ``None``.

    Every lookup below is this shape, and each held its own copy of it
    (audit register item 47). Threading the ``None`` base through rather
    than raising is what lets a caller ask about a prefix that was never
    created — the normal state before first launch.
    """
    if base is None:
        return None
    found = base / name
    return found if predicate(found) else None


def client_dir(prefix: Path) -> Path | None:
    return _existing(drive_c(prefix), CLIENT_DIR, Path.is_dir)


def client_exe(prefix: Path) -> Path | None:
    """``Battle.net.exe`` — the binary that accepts ``--exec``."""
    return _existing(client_dir(prefix), CLIENT_EXE, Path.is_file)


def launcher_exe(prefix: Path) -> Path | None:
    """``Battle.net Launcher.exe`` — spawned first, owns the wineserver."""
    return _existing(client_dir(prefix), LAUNCHER_EXE, Path.is_file)


def client_config(prefix: Path) -> Path | None:
    return _existing(drive_c(prefix), CLIENT_CONFIG, Path.is_file)


def client_version_dirs(prefix: Path) -> list[Path]:
    """Sibling versioned client folders, newest last.

    The client self-updates into a new sibling (``Battle.net.17651``
    appeared beside ``Battle.net.17554`` within five minutes of first
    launch), so repair means removing the newest and letting the
    known-good one run.
    """
    parent = client_dir(prefix)
    if parent is None:
        return []
    dirs = [p for p in parent.glob("Battle.net.*") if p.is_dir()]

    def _build(path: Path) -> tuple[int, str]:
        suffix = path.name.rsplit(".", 1)[-1]
        return (int(suffix), path.name) if suffix.isdigit() else (-1, path.name)

    return sorted(dirs, key=_build)


def client_payload_dir(prefix: Path) -> Path | None:
    """The newest versioned client payload, or None when there is none.

    ``Battle.net.exe`` in the client directory is a ~1 MB **shim**: a host
    process that loads the real client out of a sibling
    ``Battle.net.<build>/``. The shim is written long before that payload
    finishes downloading, so the shim alone is not evidence of a client.

    Treating it as such is a measured field failure: a sign-in stopped
    mid-install left the auth prefix reporting "has a client", ``.template``
    was derived from it, every game prefix was cloned from that, and each
    launch started ``Battle.net Launcher.exe`` with nothing to hand off to.
    It exited within seconds and the launch sat out its full 300 s readiness
    timeout — for every title, permanently, with no way to heal it.

    **The payload is keyed on the client DLL, not on an exe.** Measured on
    this Deck, a complete build 17651 payload is::

        Battle.net.exe              1,008,848   <- the shim, one level up
        Battle.net.17651/
            battle.net.dll         28,777,680   <- THE CLIENT
            libcef.dll            165,258,368
            Battle.net.mpq        132,426,911
            BlizzardError.exe          ~small   <- auxiliary tools, NOT
            GameSessionMonitor.exe     ~small      the client

    There is no ``Battle.net.exe`` inside the payload dir — the client is a
    DLL. Keying on one made ``client_installed`` return False for every
    correctly installed client, which refused every install with "client
    files are incomplete" and every launch with an unstartable client. The
    advice that error gave ("sign in again") could never fix it, because the
    file it waited for is one Blizzard never writes.

    Keying on the DLL also serves the original purpose better than any exe
    would: the auxiliary exes above are small and land early, while the
    28 MB client DLL lands with the bulk of the payload. The false direction
    stays deliberate — a false "incomplete" costs a client reinstall, a
    false "complete" is the unrecoverable bug above.
    """
    for candidate in reversed(client_version_dirs(prefix)):
        if _holds_client_dll(candidate):
            return candidate
    return None


def _holds_client_dll(payload: Path) -> bool:
    """Whether a payload directory contains the client DLL.

    Case-insensitive: the name is lowercased on disk today, but the prefix
    sits on a case-sensitive filesystem while Wine's own view is not, so an
    exact match would be one Blizzard capitalisation change from silently
    reporting every client incomplete again.
    """
    try:
        return any(
            entry.name.lower() == CLIENT_DLL and entry.is_file()
            for entry in payload.iterdir()
        )
    except OSError:
        return False


def client_installed(prefix: Path) -> bool:
    """True when a prefix actually holds a usable client.

    Both halves are required — the shim that takes ``--exec`` *and* the
    versioned payload it loads. See :func:`client_payload_dir`.
    """
    return client_exe(prefix) is not None and client_payload_dir(prefix) is not None


def is_ours(prefix: Path) -> bool:
    """True only when the in-directory marker proves we built this prefix.

    Never infer ownership from the path. A prefix under our directory that
    lacks the marker is treated as not ours, because deleting a user's
    prefix is unrecoverable and the marker is cheap.
    """
    return (Path(prefix) / PREFIX_MARKER).exists()
