"""Recognising a Battle.net install arriving in the prefix.

py_modules/unifideck/stores/battlenet/install_watch.py

The per-store half of
:mod:`unifideck.stores.shared.wrapper_install` — the loop itself is shared with
Ubisoft. What is Battle.net's is that it does not have to *guess* when an
install has finished.

Three facts, all measured, decide the shape of this file:

* **``aggregate.json`` is written early.** During a real 12.43 GB Hearthstone
  install the entry appeared at roughly 40% downloaded. So it answers "the
  client has started writing this game" — which is exactly what
  :meth:`~.BattlenetInstallProbe.detect` wants — and nothing more.
* **``product.db`` is authoritative about completion and useless about
  progress.** ``installed``/``playable``/``update_complete`` all flip in a
  single write, but the completion field sits at 0.0 across 12 GB and the
  total-size field stays 0 until the end. So completion comes from those flags
  and progress comes from bytes on disk — two sources, because neither can do
  both jobs.
* **The join goes through the uid.** ``aggregate.json`` and ``product.db`` are
  keyed on the product CODE (``hsb``); the library addresses titles by uid
  (``hs_beta``). Asking about the code reports every installed game as not
  installed, so this reads through ``library.install_state_by_uid``.

Asking per-uid rather than "is anything ready in this prefix" also means a
sibling Blizzard title finishing can never complete *this* install. That is the
opposite trade-off from ``install.holds_ready_install``, which is deliberately
the broadest possible answer because it guards a deletion.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from unifideck.stores.shared.installed_size import dir_allocated_bytes

from . import agent_status, paths
from . import library as library_mod
from .ownership import InstalledGame

logger = logging.getLogger(__name__)

STORE_ID = "battlenet"
CLIENT_LABEL = "Battle.net"

# A Blizzard install is bigger and slower than a Ubisoft one, and the client
# restarts itself to self-update, so both give-up graces are longer than the
# shared defaults. The timeout matches: 100 GB over a home connection is a
# legitimate overnight download.
_POLL_INTERVAL_S = 15.0
_TIMEOUT_S = 4 * 60 * 60
_GRACE_S = 300.0


def install_dir_of(row: InstalledGame | None) -> str | None:
    """Where a game's files are going, from whichever field knows yet.

    ``product.db``'s install path is authoritative and normally present from
    the moment the download starts — that row exists mid-download with its
    ready flags off, which is the whole reason the aggregate/product merge is
    not optional. The executable from ``aggregate.json`` is the fallback for
    the window where the row exists without a path yet: its parent is the
    install directory for every Blizzard title, because the client gives each
    one its own ``C:/Program Files (x86)/<Game>``.

    Only ever provisional. The path recorded at the end comes from
    ``product.db``, by which point it certainly exists.
    """
    if row is None:
        return None
    if row.host_install_path:
        return row.host_install_path
    if row.host_exe_path:
        return str(Path(row.host_exe_path).parent)
    return None


class BattlenetInstallProbe:
    """Reads the Battle.net client's own state for one uid."""

    store = STORE_ID
    client_label = CLIENT_LABEL
    poll_interval_s = _POLL_INTERVAL_S
    timeout_s = _TIMEOUT_S
    never_started_grace_s = _GRACE_S
    client_gone_grace_s = _GRACE_S

    def __init__(self, uid: str, prefix: Path) -> None:
        self._uid = uid
        self._prefix = Path(prefix)
        # Cut-off for reading the Agent's logs. The prefix is an rsync clone
        # of ``.template`` and rsync preserves mtimes, so it arrives carrying
        # the previous runs' Agent logs with their original timestamps.
        # Anything not written after this moment belongs to another run, in
        # another prefix, possibly under another region tag.
        self.started_at = time.time()

    def row(self) -> InstalledGame | None:
        """This game's install state, or ``None`` when the client hasn't written it."""
        drive_c = paths.drive_c(self._prefix)
        if drive_c is None:
            return None
        try:
            state = library_mod.install_state_by_uid(drive_c, self._prefix)
        except Exception:
            logger.debug(
                "[Battlenet] could not read install state in %s",
                self._prefix, exc_info=True,
            )
            return None
        return library_mod.install_row_for(state, self._uid)

    def snapshot(self) -> None:
        """No baseline needed — the probe asks about one uid by name.

        A directory diff needs a "before" because it cannot tell our game from
        anything else that appears. Reading the client's records for a specific
        uid has no such ambiguity, so there is nothing to remember.
        """

    def detect(self, baseline: Any) -> str | None:
        """The game's install directory once the client starts writing it."""
        del baseline
        return install_dir_of(self.row())

    def measure(self, install_dir: str) -> int:
        """Bytes actually committed so far — never the apparent size.

        Only feeds the "Installing… (N GB)" tick here, since ``is_complete``
        answers from ``product.db`` and never defers to size. It still has to
        be the allocated figure: the Agent pre-allocates like UPC does, so
        apparent size would show a 22 GB download sitting at 22 GB from the
        first minute.
        """
        return dir_allocated_bytes(install_dir)

    def status_message(self) -> str | None:
        """Why this game is not downloading yet, in the Agent's own words.

        Optional on the shared probe. See the note in
        ``shared/wrapper_install/probe.py``. Battle.net can answer it because
        its Agent runs one exclusive operation at a time and logs which one
        holds the slot, so "queued behind the updater" is a fact here rather
        than a guess. ``None`` whenever this game *is* the active operation,
        which hands the caller back its own byte-count tick.
        """
        drive_c = paths.drive_c(self._prefix)
        if drive_c is None:
            return None
        # Normalized for the same reason ``row`` is: the Agent writes its log
        # lines with the client's lowercase uid, so an uppercase catalog uid
        # matches nothing and this title never gets its "queued behind the
        # updater" explanation.
        return agent_status.describe_wait(
            drive_c, self.started_at, library_mod.normalize_uid(self._uid),
        )

    def is_complete(self, install_dir: str) -> bool | None:
        """``product.db``'s verdict for this uid — never a size heuristic.

        Always a real boolean, never ``None``: a store that can answer must
        answer, because ``None`` hands the decision to the size heuristic that
        ends an install when a download merely pauses.
        """
        del install_dir
        row = self.row()
        return bool(row and row.is_ready)
