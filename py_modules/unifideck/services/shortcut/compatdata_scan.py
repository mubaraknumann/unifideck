"""Find stale ``compatdata`` prefixes left behind by older launch paths.

py_modules/unifideck/services/shortcut/compatdata_scan.py

Before the ``compatdata`` bridge existed, launching a Unifideck shortcut with
a compat tool assigned made **Steam** create a full Proton prefix at
``steamapps/compatdata/<appid>`` — 300–800 MB each. Those prefixes are dead
weight: the launcher sets ``WINEPREFIX`` to our own per-game directory, so
the game never reads them. Nothing pruned them on uninstall either, so they
accumulate, and Protontricks lists them *instead of* the real prefix — the
user edits a prefix the game does not use.

Every entry is classified into exactly one of three buckets:

``unifideck``  the appid maps to a shortcut tagged as ours;
``orphan``     no shortcut with that appid exists at all;
``user``       someone else's non-Steam shortcut.

Bridge symlinks created by ``core.compat_bridge`` are skipped outright — they
are not directories on disk and must never be reported as reclaimable.

Deletion requires a **marker found inside the directory**, not the appid
classification. This runs unattended at boot, with no confirmation dialog to
show the user a list first, so the identification has to be positive proof
rather than inference:

* Every prefix Unifideck initialises gets ``.unifideck*`` files written into
  it — ``.unifideck_proton_version`` (see ``compat/prefix_init``),
  ``.unifideck_legacy_migrated``, ``.unifideck_vcreg_*.done``,
  ``unifideck_winetricks_complete.marker``, the GOG setup markers, and
  ``.unifideck_prereqs_<game_id>_*.done``, which even names the game. A
  ``compatdata`` directory carrying any of them is one *we* set up.
* Nothing else writes them. Verified on the dev Deck: every managed prefix has
  at least one, and the user's own non-Steam prefixes (*The Last of Us* Part I
  and II, 1.01 GB) have none.
* Because the marker is *in* the directory, this survives uninstall — which
  appid-based attribution cannot, since ``games.map`` drops the row and
  nothing then links the leftover to us.

The appid classification is kept only as a veto: ``CLASS_USER`` is never
deletable even if a marker somehow appeared. It is deliberately NOT used to
authorise deletion. ``CLASS_ORPHAN`` used to be deletable on the reasoning
that no shortcut claims the appid, but that infers "safe" from *absence* and
inverts catastrophically when ``shortcuts.vdf`` loads empty or partial: the
index goes empty, every directory becomes ``orphan``, and the sweep proposes
deleting all of them, including the user's own. An empty ``shortcuts.vdf`` is
not hypothetical here — NonSteamLaunchers rewrites the file, and boot is when
it is least reliably readable.

Bridge symlinks created by ``core.compat_bridge`` are skipped outright — they
are not directories on disk and must never be reported as reclaimable, which
is also what keeps a live game's real prefix out of reach.

Note there is no staleness *test*: redundancy follows from the launcher always
pointing ``WINEPREFIX`` at our own per-game directory. ``atime`` is useless as
an "in use" signal because the size walk (``dir_size_bytes``, shared with
every store from ``stores/shared/installed_size``) touches the tree and
updates it; ``mtime`` survives a read and is reported for diagnostics.

Read-only. Deletion is the caller's job (``services/prefix_bridge``).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from unifideck.core.compat_bridge import compatdata_dir, to_unsigned
from unifideck.stores.shared.installed_size import dir_size_bytes

from .games_map import UNIFIDECK_TAG

logger = logging.getLogger(__name__)

#: Steam gives non-Steam shortcuts appids above 2^31; real Steam appids are
#: far below it. Scanning only above this bound keeps the sweep away from
#: genuine Steam game prefixes entirely.
NONSTEAM_APPID_MIN = 2_000_000_000

CLASS_UNIFIDECK = "unifideck"
CLASS_ORPHAN = "orphan"
CLASS_USER = "user"

#: Classifications that VETO deletion regardless of any marker found. Only
#: ``CLASS_USER`` vetoes: a directory whose appid belongs to somebody else's
#: non-Steam shortcut is never touched. ``CLASS_ORPHAN`` neither authorises nor
#: vetoes — after an uninstall every leftover of ours is an orphan, so vetoing
#: it would make the sweep useless, and authorising on it is what would eat the
#: user's prefixes when ``shortcuts.vdf`` reads empty.
VETO = (CLASS_USER,)

#: Filenames Unifideck writes into a prefix it manages. Presence of ANY of
#: these is the positive proof that we initialised the directory. Matched
#: against the top level of a ``compatdata`` entry only — these are written at
#: the prefix root, and a shallow check cannot be fooled by a game's own file
#: buried somewhere in ``drive_c``.
#:
#: Keep in step with the writers: ``compat/prefix_init`` (_MARKER_NAME),
#: ``compat/vcruntime``, ``compat/gog_setup``, ``compat/save_migration``, and
#: the winetricks/prereq markers in ``proton/prefix_setup``.
MARKER_PREFIXES = (".unifideck", "unifideck_")


def is_prefix_in_use(path: Path) -> bool:
    """True iff something currently holds *path*'s ``pfx.lock``.

    Proton (and wineserver under it) takes an exclusive ``flock`` on
    ``<prefix>/pfx.lock`` for as long as the prefix is live, so trying to take
    that lock non-blockingly is a direct observation of "in use" rather than an
    inference from our own launcher's behaviour. Every Steam-created
    ``compatdata`` entry has the file.

    Fails **closed**: if the lock cannot be tested for any reason, the prefix is
    reported as in use so the caller leaves it alone.
    """
    lock = path / "pfx.lock"
    if not lock.is_file():
        # No lock file at all: nothing can be holding one. A Steam-made prefix
        # always has it, so this is an unusual directory — but absence of the
        # file is not evidence of use.
        return False
    import fcntl

    try:
        with lock.open("rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                logger.info("[compatdata_scan] %s is locked, leaving it alone", path)
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        logger.warning(
            "[compatdata_scan] cannot test %s, assuming in use", lock,
        )
        return True


def has_unifideck_marker(path: Path) -> str | None:
    """Name of the first Unifideck marker at the top level of *path*.

    ``None`` when the directory carries none, which means Unifideck never
    initialised it — Steam created it and our launcher pointed ``WINEPREFIX``
    somewhere else. Unreadable directories return ``None`` (fail closed).
    """
    try:
        for child in path.iterdir():
            if child.name.startswith(MARKER_PREFIXES):
                return child.name
    except OSError:
        logger.debug("[compatdata_scan] cannot list %s for markers", path)
    return None


def _is_ours(entry: dict[str, Any]) -> bool:
    """True iff a ``shortcuts.vdf`` entry is a Unifideck-managed shortcut."""
    tags = entry.get("tags")
    tagvals = list(tags.values()) if isinstance(tags, dict) else []
    if UNIFIDECK_TAG in tagvals:
        return True
    return "unifideck-launcher" in str(entry.get("exe", ""))


def index_shortcuts(shortcuts: dict[str, Any]) -> dict[int, tuple[str, bool]]:
    """``{u32 appid: (name, is_unifideck)}`` from a parsed shortcuts dict.

    Accepts the ``{"0": {...}, "1": {...}}`` mapping that lives under the
    ``shortcuts`` key of a parsed ``shortcuts.vdf``.
    """
    index: dict[int, tuple[str, bool]] = {}
    for raw in shortcuts.values():
        if not isinstance(raw, dict):
            continue
        entry = {str(k).lower(): v for k, v in raw.items()}
        app_id = entry.get("appid")
        if app_id is None:
            continue
        try:
            key = to_unsigned(app_id)
        except (TypeError, ValueError):
            continue
        index[key] = (str(entry.get("appname", "")), _is_ours(entry))
    return index


def classify(app_id: int, index: dict[int, tuple[str, bool]]) -> tuple[str, str]:
    """``(classification, display name)`` for *app_id*."""
    hit = index.get(app_id)
    if hit is None:
        return CLASS_ORPHAN, ""
    name, ours = hit
    return (CLASS_UNIFIDECK if ours else CLASS_USER), name


def _is_scannable(child: Path) -> bool:
    """True iff *child* is a non-Steam ``compatdata`` prefix worth classifying.

    Bridge symlinks are excluded here, which is what keeps a live game's real
    prefix out of the results entirely.
    """
    if child.is_symlink() or not child.name.isdigit():
        return False
    return int(child.name) >= NONSTEAM_APPID_MIN and child.is_dir()


def _describe(
    child: Path, index: dict[int, tuple[str, bool]], *, with_sizes: bool,
) -> dict[str, Any]:
    """One scan entry for *child*, including the deletion verdict."""
    app_id = int(child.name)
    classification, name = classify(app_id, index)
    marker = has_unifideck_marker(child)
    # Only worth the lock syscall for a directory we would otherwise delete;
    # an untouched user prefix must not be probed needlessly.
    candidate = marker is not None and classification not in VETO
    in_use = is_prefix_in_use(child) if candidate else False
    try:
        mtime = child.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "app_id": app_id,
        "name": name,
        "classification": classification,
        "path": str(child),
        "size_bytes": dir_size_bytes(str(child)) if with_sizes else 0,
        "marker": marker,
        "mtime": mtime,
        "in_use": in_use,
        # Positive proof (a marker we wrote), no veto, and observably not
        # locked. Classification alone can never authorise — see the module
        # docstring.
        "deletable": candidate and not in_use,
    }


def scan(
    steam_root: Path | str | None,
    shortcuts: dict[str, Any] | None,
    *,
    with_sizes: bool = True,
) -> dict[str, Any]:
    """Classify every non-Steam ``compatdata`` directory under *steam_root*.

    Returns ``{"entries": [...], "deletable_bytes": int, "deletable_count":
    int}``, where each entry carries ``app_id``, ``name``, ``classification``,
    ``path``, ``size_bytes`` and ``deletable``. Never raises.

    An entry is ``deletable`` only when a Unifideck marker was found inside it
    (see :func:`has_unifideck_marker`) and its classification is not in
    :data:`VETO`. Each entry also carries ``marker`` (the filename that proved
    ownership, or ``None``) and ``mtime`` for the deletion log.
    """
    empty: dict[str, Any] = {
        "entries": [], "deletable_bytes": 0, "deletable_count": 0,
    }
    if not steam_root:
        return empty
    root = compatdata_dir(Path(steam_root).expanduser())
    if not root.is_dir():
        return empty

    index = index_shortcuts(shortcuts or {})
    try:
        children = sorted(root.iterdir())
    except OSError:
        logger.exception("[compatdata_scan] cannot list %s", root)
        return empty

    entries = [
        _describe(child, index, with_sizes=with_sizes)
        for child in children
        if _is_scannable(child)
    ]

    deletable = [e for e in entries if e["deletable"]]
    return {
        "entries": entries,
        "deletable_bytes": sum(e["size_bytes"] for e in deletable),
        "deletable_count": len(deletable),
    }
