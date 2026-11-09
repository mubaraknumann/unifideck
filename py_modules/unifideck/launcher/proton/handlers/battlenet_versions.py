"""Which version of a Battle.net title is installed, read at launch time.

py_modules/unifideck/launcher/proton/handlers/battlenet_versions.py

One tile is many versions: World of Warcraft installs as ``wow`` (Retail),
``wow_classic``, ``wow_classic_era`` and more, and the user picks which one
inside the client. The family code ``--exec`` needs differs per version,
and the id map only learns it at sync. Classic Era installed after the last
sync therefore launched with ``launch WoW``, which resets the client to
Retail and offers Install over the finished Classic install.

This reads the answer from the prefix itself, on every launch, so a version
installed a minute ago is launched as what it is.

**Why we never start the game binary ourselves.** Measured: running
``WowClassic.exe -launcherlogin -uid wow_classic_era`` while the client is
up and signed in produces a game process, but the client logs ``Pre-existing
game session detected without a pending launch`` and WoW never logs in; it
quits cleanly about 20 s later. The client must initiate the launch: retail
through ``--exec="launch <family>"``, every other version through the
client's own Play button (``battlenet_play``).

Store imports stay inside functions for the reason given in
``battlenet_client._client_dir``: the store package reaches back into the
launcher, so a module-level import closes a cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_drive_c

if TYPE_CHECKING:
    from unifideck.stores.battlenet.ownership import InstalledGame
    from unifideck.stores.battlenet.ownership.pub_catalog import CatalogEntry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Resolution:
    """What the prefix says about launching this tile."""

    versions: list[InstalledGame] = field(default_factory=list)
    family: str | None = None
    # True when ``launch <family>`` will only select the version and the
    # user must press Play in the client: several versions installed (the
    # user picks), or a non-retail one (the client never auto-starts it).
    selects: bool = False
    # The version to launch, and whether it is the program's retail product.
    # A non-retail version cannot be started with ``--exec`` at all; the
    # launcher presses the client's own Play for it (``battlenet_play``).
    target_uid: str | None = None
    non_retail: bool = False


@dataclass(frozen=True, slots=True)
class LaunchTarget:
    """What ``battlenet_launch`` starts: the resolution plus its fallbacks."""

    family: str
    selects: bool
    uid: str | None = None
    non_retail: bool = False


def resolve(prefix: Path, uid: str, chosen: str | None = None) -> Resolution:
    """Installed versions, and the family and auto-start for the one to run.

    ``chosen`` is the version the user picked in the launch popup (read from
    ``stores/battlenet/launch_choice``). Without one, or when it is no longer
    installed, the most recently played version is used.

    Never raises: an empty result leaves the caller on the id map, which
    is how every launch worked before this module existed.
    """
    try:
        entry, versions = _ready_versions(prefix, uid)
    except Exception:
        logger.exception("[battlenet] version resolution failed for %s", uid)
        return Resolution()
    out = Resolution(versions=versions)
    if not versions:
        return out
    picked = _picked(versions, chosen)
    target = picked or max(versions, key=lambda r: r.last_played_ms or 0)
    non_retail = bool(entry and entry.launch_family_for(target.uid))
    out.target_uid = target.uid
    out.non_retail = non_retail
    if entry is not None:
        out.family = entry.launch_family_for(target.uid) or entry.program_id
    # A picked version is the user's answer, so the client's own version
    # picker is not needed; only a non-retail version still waits for Play.
    out.selects = non_retail or (picked is None and len(versions) > 1)
    logger.info(
        "[battlenet] %s installed as %s, launching %s%s → family %s%s",
        uid, [v.uid for v in versions], target.uid,
        " (picked)" if picked else "", out.family,
        " (client selects, user presses Play)" if out.selects else "",
    )
    return out


def _picked(versions: list[InstalledGame], chosen: str | None) -> InstalledGame | None:
    """The installed row the user picked, or None."""
    if not chosen:
        return None
    for row in versions:
        if row.uid and row.uid.casefold() == chosen.casefold():
            return row
    logger.warning(
        "[battlenet] picked version %s is not installed (have %s) — "
        "launching the most recently played instead",
        chosen, [v.uid for v in versions],
    )
    return None


def _ready_versions(
    prefix: Path, uid: str,
) -> tuple[CatalogEntry | None, list[InstalledGame]]:
    """The tile's catalog entry and every ready version of it in ``prefix``."""
    from unifideck.stores.battlenet.install_state import ready_versions

    drive_c = resolve_drive_c(Path(prefix))
    if drive_c is None:
        return None, []
    _catalog, entry, rows = ready_versions(drive_c, Path(prefix), uid)
    return entry, rows


def consume_choice(uid: str) -> str | None:
    """The launch popup's one-shot pick for ``uid``, if fresh. Never raises."""
    from unifideck.stores.battlenet.launch_choice import consume

    choice = consume(uid)
    if choice:
        logger.info("[battlenet] launch popup chose %s for %s", choice, uid)
    return choice


def launch_target(
    prefix: Path,
    uid: str,
    chosen: str | None,
    *,
    family_fallback: Callable[[str], str | None],
    selects_fallback: Callable[[str], bool],
) -> LaunchTarget | None:
    """What ``battlenet_launch`` starts, or None when no family is known.

    Read from the prefix at launch time, so a version installed since the
    last library sync is launched as what it is. The fallbacks (the id map,
    passed in by the caller) answer when the prefix cannot.
    """
    found = resolve(prefix, uid, chosen)
    family = found.family or family_fallback(uid)
    if not family:
        return None
    selects = found.selects if found.versions else selects_fallback(uid)
    return LaunchTarget(
        family=family, selects=selects, uid=found.target_uid, non_retail=found.non_retail,
    )
