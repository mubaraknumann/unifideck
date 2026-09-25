"""Join the client's install records to the catalog's uids.

py_modules/unifideck/stores/battlenet/install_state.py

``aggregate.json`` and ``product.db`` are keyed on the product CODE
(``hsb``) while the catalog addresses titles by uid (``hs_beta``), and the
two disagree about case as well. Every question of the form "is this game
installed, and where" goes through here so that join is spelled exactly
once — a second hand-written lookup is how it silently stops matching, and
the failure mode is always the same: a finished install reads as missing.

Split out of ``library.py`` (2026-09-20) for its LOC cap, when versions
made the join a real lookup rather than a dict access: one program installs
as many uids, and the user picks which inside the client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from .ownership import InstalledGame, read_catalog, read_installed
from .product_db import read_product_db

logger = logging.getLogger(__name__)


def normalize_uid(uid: str) -> str:
    """The join key for a Battle.net uid, case-folded.

    Blizzard's own catalog is internally inconsistent about uid case. The PUB
    fragments spell Diablo's retail uid ``D1``, Warcraft I's ``W1`` and
    Warcraft II's ``W2``, while everything the *client* writes — ``product.db``,
    ``aggregate.json``, the Agent logs — is lowercase throughout. Joining the
    two case-sensitively reports exactly those titles as never installed: a
    real Diablo install finished on disk at 13:04, ``detect()`` never fired
    because ``product.db`` says ``d1`` and we asked for ``D1``, and five
    minutes later the watchdog failed it with "The install was never finished
    in Battle.net".

    **Only the join is normalized.** The uid we emit as ``store_game_id`` keeps
    its original case, because that string is what every released user's Steam
    shortcut is keyed on (see :mod:`unifideck.services.shortcut.games_map`) —
    re-keying it would strand their playtime, categories and artwork. The id
    map keeps its case for the same reason and needs no change: it is looked up
    with the same catalog uid it was written with, so it is already
    self-consistent, and the out-of-process launcher reads it the same way.
    """
    return uid.lower()


def install_row_for(
    state: dict[str, InstalledGame], uid: str,
) -> InstalledGame | None:
    """Look one uid up in an :func:`install_state_by_uid` mapping.

    Exists so the lookup side of the join can only be spelled once. Both
    callers — the library's ``install_row`` and the install watcher's ``row``
    — must normalize identically, and a second hand-written ``state.get(...)``
    is how that silently stops being true.
    """
    return state.get(normalize_uid(uid))


def variant_install_row(
    state: dict[str, InstalledGame], uids: Sequence[str],
) -> InstalledGame | None:
    """The install row for a title, whichever of its versions is installed.

    A Battle.net program is one tile but many installable versions, and the
    user picks the version inside the client, after our Install button has
    already committed to a uid. Measured on device: pressing Install on the
    ``wow`` tile and choosing Classic wrote ``wow_classic_era``, so the tile
    kept offering Install over a finished 5 GB install.

    With several installed, prefer a ready one and then the most recently
    played — the same order the client itself presents them in.
    """
    rows = variant_install_rows(state, uids)
    if not rows:
        return None
    return max(rows, key=lambda r: (r.is_ready, r.last_played_ms or 0))


def variant_install_rows(
    state: dict[str, InstalledGame], uids: Sequence[str],
) -> list[InstalledGame]:
    """Every installed version of a title, in catalog order.

    More than one is normal — Retail beside Classic beside Classic Era —
    and the count decides who launches: one version the client starts by
    itself, several and the user picks in the client.
    """
    rows: list[InstalledGame] = []
    for candidate in uids:
        row = install_row_for(state, candidate)
        if row is not None and row not in rows:
            rows.append(row)
    return rows


def title_install_row(
    drive_c: Path, state: dict[str, InstalledGame], uid: str,
) -> InstalledGame | None:
    """This title's install row, whichever version of it is installed.

    For the callers that hold a uid and a prefix but no catalog — the
    install watcher, and the size and path lookups. The uid asked for is
    tried first; only if nothing answers is the prefix's own PUB catalog
    read, to learn which uids are versions of this same title.

    It must be the catalog and not the lone row in the prefix: a sibling
    Blizzard title going ready would otherwise complete an install that had
    not started (``test_another_title_finishing_does_not_complete_this_one``).
    """
    direct = install_row_for(state, uid)
    if direct is not None or not state:
        return direct
    entry = read_catalog(drive_c).entry_for_uid(uid)
    if entry is None:
        return None
    row = variant_install_row(state, entry.known_uids())
    if row is not None:
        logger.info(
            "[Battlenet] %s is installed as %s — a version of the same title",
            uid, row.uid,
        )
    return row


def index_by_uid(installed: dict[str, InstalledGame]) -> dict[str, InstalledGame]:
    """Re-key install state on uid.

    ``aggregate.json`` and ``product.db`` are keyed on the product CODE
    (``hsb``) while the catalog addresses titles by uid (``hs_beta``). The
    uid is the only field common to both, so the join has to go through it —
    matching on code silently reports every installed game as not installed.

    Keys are normalized through :func:`normalize_uid`; look them up with
    :func:`install_row_for`, never with a bare ``.get``.
    """
    by_uid: dict[str, InstalledGame] = {}
    for game in installed.values():
        if game.uid:
            by_uid[normalize_uid(game.uid)] = game
    return by_uid


async def install_row(
    game_id: str, prefix: Path | None,
) -> InstalledGame | None:
    """This game's row in the client's install records, or ``None``.

    Keyed on the uid asked for, then on the catalog's other uids for the
    same title (:func:`title_install_row`) — which is how a version the
    user chose inside the client, ``wow_classic_era`` under the ``wow``
    tile, is still found here. Never the *first* row in the prefix: that is
    right only by accident, and a prefix that picked up a second Blizzard
    title reported that one's path and size under this game's id.

    Moved off the store (2026-08-26) for its LOC cap; it reads the same
    client state as everything else here. The caller resolves the prefix,
    since only it holds the id map.
    """
    from . import paths

    if prefix is None:
        return None
    drive_c = paths.drive_c(prefix)
    if drive_c is None:
        return None
    state = await asyncio.to_thread(install_state_by_uid, drive_c, prefix)
    return await asyncio.to_thread(title_install_row, drive_c, state, game_id)


def install_state_by_uid(drive_c: Path, prefix: Path) -> dict[str, InstalledGame]:
    """Install state for one prefix, keyed the way the rest of the code asks.

    The install watcher needs to ask about *one* uid — "is the title the user
    pressed Install on ready yet" — and must not re-derive the code→uid join
    to do it. Getting that join wrong reports every installed game as not
    installed, which is the regression ``index_by_uid`` exists to prevent.
    """
    return index_by_uid(read_install_state(drive_c, prefix))




def read_install_state(drive_c: Path, prefix: Path) -> dict[str, InstalledGame]:
    """Installed state for one prefix, with host paths resolved."""
    return read_installed(drive_c, read_product_db(drive_c), prefix=prefix)
