"""What the launch popup offers for a Battle.net tile.

py_modules/unifideck/stores/battlenet/launch_choices.py

One tile is many versions (Retail, Classic, Classic Era under ``wow``),
installed side by side in one prefix. The frontend shows Steam's own
launch-options popup listing each installed version plus the Battle.net
client itself. This answers the first half, read from the client's own
records in the game's prefix, so a version installed a minute ago is
offered without a sync.
"""

from __future__ import annotations

from pathlib import Path

from . import paths
from .install_state import ready_versions


def launch_choices(prefix: Path | None, uid: str) -> list[dict[str, str]]:
    """``[{"uid": …, "label": …}]`` for every ready version of ``uid``.

    Labels are the client's own names for each version ("World of Warcraft
    Classic"), falling back to the uid when the catalog has none. Empty when
    the prefix is unknown or holds no ready install.
    """
    drive_c = paths.drive_c(prefix) if prefix is not None else None
    if drive_c is None or prefix is None:
        return []
    catalog, _entry, rows = ready_versions(drive_c, prefix, uid)
    return [
        {"uid": row.uid, "label": catalog.version_name(row.uid) or row.name or row.uid}
        for row in rows
        if row.uid
    ]
