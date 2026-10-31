"""Start an installed Battle.net game's own binary, the way the client does.

py_modules/unifideck/launcher/proton/handlers/battlenet_direct.py

``--exec="launch <family>"`` only auto-starts a title's *retail* version.
For any other version it selects the version and waits for a click, and
``launch WoW`` even resets the client to WoW_retail, which offers Install
over a finished Classic install. The client's own Play button does
something simpler. Measured in its log::

    Launched C:/Program Files (x86)/World of Warcraft/_classic_era_/WoWClassic.exe
    with args: -launcherlogin -uid wow_classic_era -initialgamemode=cera, pid: 1156

Running that binary with those arguments ourselves, while the client is up
and signed in, starts the game. This module works out that binary and
those arguments **at launch time, from the prefix itself**: ``product.db``
says which version is installed, so a version installed a minute ago
launches correctly without a library sync.

Store imports stay inside functions for the reason given in
``battlenet_client._client_dir``: the store package reaches back into the
launcher, so a module-level import closes a cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.proton.handlers.battlenet_watch import EXCLUDED_IMAGES
from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan
from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_drive_c
from unifideck.launcher.proton.infrastructure.umu_runtime import run_umu_with_retry

if TYPE_CHECKING:
    from unifideck.stores.battlenet.ownership import InstalledGame
    from unifideck.stores.battlenet.ownership.pub_catalog import CatalogEntry

logger = logging.getLogger(__name__)

_LOG_DIR = "users/steamuser/AppData/Local/Battle.net/Logs"
_LOG_GLOB = "battle.net-*.log"
_MAX_LOG_BYTES = 4 * 1024 * 1024
# The client's own record of starting a game binary. Both shapes are
# measured, the ``pid`` suffix is not always written:
#   {Main} Launched C:/…/_retail_/x86_64/Warcraft III.exe with args: -launch -uid w3, pid: 1888
#   {Main} Launched C:/…/_classic_era_/WoWClassic.exe with args: -launcherlogin -uid wow_classic_era -initialgamemode=cera
_LAUNCHED_RE = re.compile(
    r"\bLaunched (?P<exe>[A-Za-z]:[/\\][^\r\n]+?\.exe) with args: "
    r"(?P<args>[^\r\n]*?)(?:, pid: \d+)?[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)
# Executables that sit beside a game but are not the game.
_NOT_GAME_SUFFIXES = ("launcher.exe", "uninstaller.exe", "error.exe")


@dataclass(frozen=True, slots=True)
class DirectTarget:
    """One game binary to start, and where that answer came from."""

    uid: str
    exe: Path
    args: tuple[str, ...]
    source: str


@dataclass(slots=True)
class Resolution:
    """What the prefix says about launching this tile."""

    versions: list[InstalledGame] = field(default_factory=list)
    family: str | None = None
    # True when ``launch <family>`` will only select the version and the
    # user must press Play in the client: several versions installed (the
    # user picks), or a non-retail one (the client never auto-starts it).
    selects: bool = False
    target: DirectTarget | None = None
    searched: Path | None = None


def resolve(prefix: Path, uid: str) -> Resolution:
    """Installed versions, the family for the newest, and its binary.

    Never raises: any failure leaves the caller on the ``--exec`` path,
    which is how every launch worked before this module existed.
    """
    try:
        return _resolve(prefix, uid)
    except Exception:
        logger.exception("[battlenet] direct-launch resolution failed for %s", uid)
        return Resolution()


def _ready_versions(
    prefix: Path, uid: str,
) -> tuple[CatalogEntry | None, list[InstalledGame]]:
    """The tile's catalog entry and every ready version of it in ``prefix``."""
    from unifideck.stores.battlenet.install_state import (
        install_state_by_uid,
        variant_install_rows,
    )
    from unifideck.stores.battlenet.ownership import read_catalog

    drive_c = resolve_drive_c(Path(prefix))
    if drive_c is None:
        return None, []
    state = install_state_by_uid(drive_c, Path(prefix))
    entry = read_catalog(drive_c).entry_for_uid(uid)
    uids = entry.known_uids() if entry else (uid,)
    return entry, [row for row in variant_install_rows(state, uids) if row.is_ready]


def _resolve(prefix: Path, uid: str) -> Resolution:
    entry, versions = _ready_versions(prefix, uid)
    out = Resolution(versions=versions)
    if not versions:
        return out
    newest = max(versions, key=lambda r: r.last_played_ms or 0)
    out.family = _family(entry, newest)
    out.selects = len(versions) > 1 or bool(
        entry and entry.launch_family_for(newest.uid),
    )
    if len(versions) > 1:
        # The user's choice: with several versions on disk, the client's
        # own version picker decides, never us.
        return out
    row = versions[0]
    out.searched = Path(row.host_install_path) if row.host_install_path else None
    out.target = _from_client_log(prefix, row) or _discover(row, entry)
    return out


def _family(entry: CatalogEntry | None, row: InstalledGame) -> str | None:
    """The ``--exec`` family that selects this installed version."""
    if entry is None:
        return None
    return entry.launch_family_for(row.uid) or entry.program_id


def _client_logs(prefix: Path) -> list[Path]:
    """The client's logs in ``prefix``, newest first."""
    drive_c = resolve_drive_c(Path(prefix))
    logs = drive_c / _LOG_DIR if drive_c else None
    if logs is None or not logs.is_dir():
        return []
    stamped: list[tuple[float, Path]] = []
    for path in logs.glob(_LOG_GLOB):
        with contextlib.suppress(OSError):
            stamped.append((path.stat().st_mtime, path))
    return [path for _, path in sorted(stamped, reverse=True)]


def _read(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(_MAX_LOG_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def launched_line(text: str, uid: str) -> tuple[str, tuple[str, ...]] | None:
    """The last ``Launched <exe> with args: …`` in ``text`` for ``uid``."""
    found: tuple[str, tuple[str, ...]] | None = None
    for match in _LAUNCHED_RE.finditer(text):
        args = tuple(match.group("args").split())
        if _uid_arg(args) == uid.casefold():
            found = (match.group("exe"), args)
    return found


def _uid_arg(args: tuple[str, ...]) -> str | None:
    for flag, value in itertools.pairwise(args):
        if flag.casefold() == "-uid":
            return value.casefold()
    return None


def _from_client_log(prefix: Path, row: InstalledGame) -> DirectTarget | None:
    """What the client itself last ran for this version, if still on disk."""
    from unifideck.stores.shared.wine_path import wine_path_to_linux

    if not row.uid:
        return None
    for log in _client_logs(prefix):
        hit = launched_line(_read(log), row.uid)
        if hit is None:
            continue
        host = wine_path_to_linux(hit[0], str(prefix))
        exe = match_case(Path(host)) if host else None
        if exe is not None:
            return DirectTarget(row.uid, exe, hit[1], f"client log {log.name}")
        logger.info("[battlenet] client log names %s, no longer on disk", hit[0])
        return None
    return None


def match_case(path: Path) -> Path | None:
    """``path`` on disk, matching each component case-insensitively.

    Windows paths are case-insensitive and the host filesystem is not.
    Measured: the client logs ``_classic_era_/WoWClassic.exe`` for a file
    that is ``WowClassic.exe`` on disk.
    """
    if path.is_file():
        return path
    current = Path(path.parts[0])
    for part in path.parts[1:]:
        candidate = current / part
        if not candidate.exists():
            try:
                candidate = next(
                    c for c in current.iterdir() if c.name.casefold() == part.casefold()
                )
            except (OSError, StopIteration):
                return None
        current = candidate
    return current if current.is_file() else None


def _discover(row: InstalledGame, entry: CatalogEntry | None) -> DirectTarget | None:
    """The version's game binary, found in its install dir.

    Only for the first Play after an install, before the client has ever
    logged starting this version. More than one candidate is "unknown",
    never a guess: starting the wrong binary is worse than the fallback.
    """
    if not row.uid or not row.host_install_path:
        return None
    root = Path(row.host_install_path)
    folder = _flavor_dir(root, row.uid) or root
    exes = [p for p in _exes(folder) if _is_game_exe(p.name)]
    if len(exes) != 1:
        logger.info(
            "[battlenet] %s: %d game binaries in %s, cannot pick one",
            row.uid, len(exes), folder,
        )
        return None
    extra = entry.launch_args_for(row.uid) if entry else ()
    args = ("-launcherlogin", "-uid", row.uid, *extra)
    return DirectTarget(row.uid, exes[0], args, f"discovered in {folder}")


def _flavor_dir(root: Path, uid: str) -> Path | None:
    """WoW keeps each version in ``_<flavor>_`` named by ``.flavor.info``."""
    with contextlib.suppress(OSError):
        for child in root.iterdir():
            info = child / ".flavor.info"
            if child.is_dir() and info.is_file():
                lines = [ln.strip() for ln in _read(info).splitlines() if ln.strip()]
                if lines and lines[-1].casefold() == uid.casefold():
                    return child
    return None


def _exes(folder: Path) -> list[Path]:
    with contextlib.suppress(OSError):
        return sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.casefold() == ".exe"
        )
    return []


def _is_game_exe(name: str) -> bool:
    lowered = name.casefold()
    return lowered not in EXCLUDED_IMAGES and not lowered.endswith(_NOT_GAME_SUFFIXES)


def start(plan: ProtonLaunchPlan, target: DirectTarget) -> asyncio.Task[int]:
    """Run the game binary in the client's prefix, as the client would.

    Same shape as the phase-C ``--exec`` run, for the same measured
    reasons: ``PROTON_VERB=run`` (``waitforexitandrun`` blocks on the
    client's wineserver) and ``reap_wineserver=False`` (this run does not
    own that wineserver, and reaping it kills the client). The working
    directory is the binary's own folder, matching the client.

    Returned as a task rather than awaited with a timeout like ``--exec``.
    This run lasts as long as the game does, and cancelling it kills its
    process group, game included. The caller follows the game by pid and
    cancels the task only when the session is already over or stopped.
    """
    env = dict(plan.env)
    env["PROTON_VERB"] = "run"
    argv = [str(plan.python_bin), str(plan.umu_wrapper), str(target.exe), *target.args]
    logger.info(
        "[battlenet] direct launch (%s): %s %s",
        target.source, target.exe, " ".join(target.args),
    )
    return asyncio.create_task(
        run_umu_with_retry(
            argv, env=env, cwd=target.exe.parent,
            max_attempts=1, reap_wineserver=False,
        ),
    )


async def finish(task: asyncio.Task[int]) -> None:
    """Retire the run :func:`start` began, without raising."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(BaseException):
        await task
