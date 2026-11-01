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
quits cleanly about 20 s later. The client must initiate the launch, so the
launch always goes through ``--exec``.

Store imports stay inside functions for the reason given in
``battlenet_client._client_dir``: the store package reaches back into the
launcher, so a module-level import closes a cycle.
"""

from __future__ import annotations

import logging
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


def resolve(prefix: Path, uid: str) -> Resolution:
    """Installed versions, and the family and auto-start for the newest.

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
    newest = max(versions, key=lambda r: r.last_played_ms or 0)
    if entry is not None:
        out.family = entry.launch_family_for(newest.uid) or entry.program_id
    out.selects = len(versions) > 1 or bool(
        entry and entry.launch_family_for(newest.uid),
    )
    logger.info(
        "[battlenet] %s installed as %s → family %s%s",
        uid, [v.uid for v in versions], out.family,
        " (client selects, user presses Play)" if out.selects else "",
    )
    return out


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
