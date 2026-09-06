r"""Bridge Unifideck prefixes into Steam's ``compatdata`` namespace.

py_modules/unifideck/core/compat_bridge.py

Unifideck keys its Wine prefixes on the *store* game id
(``~/.local/share/unifideck/prefixes/<game_id>``) so a prefix survives
shortcut regeneration — a Force Sync rewrites ``shortcuts.vdf`` but the
store id never changes. External Wine tooling, however, only knows about
Steam's layout: Protontricks resolves a non-Steam shortcut's prefix
*exclusively* at ``<steam_lib>/steamapps/compatdata/<u32 appid>/pfx``
(``find_appid_proton_prefix``) and silently skips any shortcut without
one. The result was that **no** Unifideck prefix was ever reachable from
Protontricks, while stale Steam-made ``compatdata`` dirs left over from
older launch paths *were* listed — so users edited a prefix the game
does not run in.

This module maintains a symlink

    steamapps/compatdata/<u32 appid>  ->  ~/.local/share/unifideck/prefixes/<game_id>

which needs no change to the prefix layout: umu already creates
``pfx -> .`` and ``pfx.lock`` at the prefix root, satisfying both of
Protontricks' gates (``prefix_path.is_dir()`` and
``(prefix_path.parent / "pfx.lock").is_file()``).

Stdlib-only by design — imported both by the Decky backend (bundled
Python 3.11) and by the out-of-process launcher (system ``/usr/bin/python3``,
3.10–3.14), same constraint as ``core/paths.py``.

All functions are synchronous and do blocking I/O; call them from a thread
(``asyncio.to_thread``) when on the event loop. Every one is best-effort:
a bridge failure must never fail an install, a launch, or an uninstall.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: Default root Unifideck prefixes live under. ``unlink_prefix`` refuses to
#: remove a ``compatdata`` entry that does not look like one of ours, so a
#: user's own prefix can never be deleted by the bridge.
PREFIX_ROOT = Path("~/.local/share/unifideck/prefixes").expanduser()

#: Ubisoft (and any store honouring a user-picked install location) keeps its
#: prefixes at ``<base>/prefixes/<store>/<id>`` under whatever base the user
#: chose — e.g. ``~/Games/prefixes/ubisoft/80`` — so :data:`PREFIX_ROOT` alone
#: does not recognise them. Matching on the ``prefixes`` path segment covers
#: every base without hardcoding one; without it those bridges were
#: unprunable, and a dangling link outlived the prefix forever.
_PREFIX_SEGMENT = "prefixes"

#: Suffix given to a pre-existing real ``compatdata`` directory that occupies
#: the appid we need. Renamed rather than deleted — reversible, and never
#: destroys user data (absolute rule #2).
DISPLACED_SUFFIX = ".unifideck-displaced"


def to_unsigned(app_id: int | str) -> int:
    """Normalise a Steam shortcut appid to the unsigned 32-bit form.

    ``shortcuts.vdf`` and ``games.map`` store the *signed* value; Steam names
    the ``compatdata`` directory with the unsigned one.

    This is the only implementation. It used to say it mirrored
    ``services/shortcut/orphan_scan._to_unsigned``, which stopped being true
    when that module was changed to import this one.
    """
    return int(app_id) & 0xFFFFFFFF


def appid_candidates(app_id: int | str) -> list[str]:
    """Both the signed and unsigned 32-bit string forms of an appid.

    Sync stores ``Game.app_id`` **signed** (matching Steam's on-disk
    representation), while Steam's frontend hands plugins the **unsigned**
    form via ``overview.appid``. A cache keyed on ``str(game.app_id)`` is
    therefore reachable only through the signed string, and vice versa, so a
    lookup has to try both. Callers should not have to know which side wrote
    the entry.

    Distinct from :func:`to_unsigned`, which *converts* between the two.
    This *generates both* for a lookup — audit §1.4 deliberately left these
    apart for that reason while folding twelve conversion sites onto
    ``to_unsigned``.

    Lives here rather than in ``rpc/`` because ``rpc`` is a leaf package
    (``.importlinter``: nothing inside ``unifideck.*`` may import it), and
    two of the three former copies were outside it. Audit register item 20.
    """
    value = int(app_id)
    forms: list[str] = [str(value)]
    if value > 0x7FFFFFFF:
        forms.append(str(value - 0x100000000))
    elif value < 0:
        forms.append(str(value + 0x100000000))
    return forms


def compatdata_dir(steam_root: Path) -> Path:
    """The ``compatdata`` directory of *steam_root*."""
    return Path(steam_root) / "steamapps" / "compatdata"


def compatdata_link(steam_root: Path, app_id: int | str) -> Path:
    """Path of the bridge entry for *app_id* under *steam_root*."""
    return compatdata_dir(steam_root) / str(to_unsigned(app_id))


def _points_at(link: Path, prefix: Path) -> bool:
    """True iff *link* is a symlink already resolving to *prefix*."""
    try:
        return link.is_symlink() and link.resolve() == prefix.resolve()
    except (OSError, RuntimeError):
        return False


def _displace(link: Path) -> bool:
    """Move a real directory out of the way. Returns True on success.

    Picks the first free ``<name>.unifideck-displaced[-N]`` so repeated runs
    never clobber an earlier displacement.
    """
    target = link.with_name(link.name + DISPLACED_SUFFIX)
    n = 2
    while target.exists():
        target = link.with_name(f"{link.name}{DISPLACED_SUFFIX}-{n}")
        n += 1
    try:
        link.rename(target)
    except OSError:
        logger.exception("[compat_bridge] could not displace %s", link)
        return False
    logger.info("[compat_bridge] displaced %s -> %s", link, target.name)
    return True


def link_prefix(
    prefix: Path | str,
    app_id: int | str | None,
    steam_root: Path | str | None,
) -> str:
    """Point ``compatdata/<appid>`` at *prefix*.

    Returns the action taken, for logging and tests:

    ``"noop"``       already correct — the common case on every relaunch;
    ``"created"``    symlink created;
    ``"repointed"``  a stale bridge symlink now points at *prefix*;
    ``"displaced"``  a real directory was renamed aside, then symlinked;
    ``"skipped"``    missing inputs, or the prefix does not exist yet;
    ``"failed"``     the filesystem refused the operation.

    Never raises: callers sit on the install and launch hot paths.
    """
    if not app_id or not steam_root:
        return "skipped"
    prefix = Path(prefix).expanduser()
    if not prefix.is_dir():
        logger.debug("[compat_bridge] prefix %s absent, not linking", prefix)
        return "skipped"

    link = compatdata_link(Path(steam_root).expanduser(), app_id)
    if _points_at(link, prefix):
        return "noop"

    action = "created"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            # Stale bridge (prefix moved, or an appid was re-keyed).
            link.unlink()
            action = "repointed"
        elif link.exists():
            # A real Steam-made prefix squatting on our appid.
            if not _displace(link):
                return "failed"
            action = "displaced"
        link.symlink_to(prefix, target_is_directory=True)
    except OSError:
        logger.exception("[compat_bridge] link %s -> %s failed", link, prefix)
        return "failed"

    logger.info("[compat_bridge] %s %s -> %s", action, link.name, prefix)
    return action


def unlink_prefix(
    app_id: int | str | None,
    steam_root: Path | str | None,
) -> bool:
    """Remove the bridge entry for *app_id*. Returns True if it is gone.

    Only ever removes a **symlink into** :data:`PREFIX_ROOT`. A real
    directory — or a symlink someone else owns — is left untouched and
    reported, so the bridge can never delete a user's own prefix.
    """
    if not app_id or not steam_root:
        return False
    link = compatdata_link(Path(steam_root).expanduser(), app_id)
    if not link.is_symlink():
        if link.exists():
            logger.debug(
                "[compat_bridge] %s is a real directory, leaving it alone", link,
            )
        return not link.exists()
    if not _is_ours(link):
        logger.warning(
            "[compat_bridge] %s points outside %s, refusing to remove",
            link, PREFIX_ROOT,
        )
        return False
    try:
        link.unlink()
    except OSError:
        logger.exception("[compat_bridge] unlink(%s) failed", link)
        return False
    logger.info("[compat_bridge] removed bridge %s", link.name)
    return True


def _is_ours(link: Path) -> bool:
    """True iff *link* resolves to a Unifideck prefix.

    That means inside :data:`PREFIX_ROOT`, or under any ``.../prefixes/...``
    tree (:data:`_PREFIX_SEGMENT`) — the layout a user-picked storage base
    produces. Uses the *unresolved* target when the prefix is already gone (a
    dangling bridge left by an uninstall still counts as ours and must be
    prunable).
    """
    root = PREFIX_ROOT.expanduser()
    try:
        raw = Path(os.readlink(link))
    except OSError:
        return False
    candidate = raw if raw.is_absolute() else (link.parent / raw)
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        pass
    # Dangling target: fall back to a lexical check so prune still works.
    lexical = Path(os.path.normpath(candidate))
    try:
        lexical.relative_to(root)
        return True
    except ValueError:
        pass
    # An alternate storage base. Match the two layouts Unifideck actually
    # writes — ``<base>/prefixes/<id>`` and ``<base>/prefixes/<store>/<id>`` —
    # rather than any path that happens to contain the segment, so a user
    # directory deeper inside such a tree is still off limits.
    return _PREFIX_SEGMENT in (lexical.parent.name, lexical.parent.parent.name)


def is_bridge_link(link: Path) -> bool:
    """True iff *link* is a ``compatdata`` entry this bridge owns.

    Public face of :func:`_is_ours` for read-only callers (the support-bundle
    probe). Kept as one implementation deliberately: an ownership rule copied
    into a second module is a rule that will drift, and the cost of drift here
    is deleting — or reporting as ours — a prefix that is not.
    """
    return link.is_symlink() and _is_ours(link)


def prune_dead_bridges(steam_root: Path | str | None) -> int:
    """Delete bridge symlinks whose prefix no longer exists.

    Repairs the case where a prefix was removed without the uninstall path
    running (manual deletion, a failed uninstall, a restored backup). Returns
    the number of links removed. Only touches links owned by the bridge.
    """
    if not steam_root:
        return 0
    root = compatdata_dir(Path(steam_root).expanduser())
    if not root.is_dir():
        return 0
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        logger.exception("[compat_bridge] cannot list %s", root)
        return 0
    for entry in entries:
        if not entry.is_symlink() or not _is_ours(entry):
            continue
        if entry.exists():  # follows the link — target still there
            continue
        try:
            entry.unlink()
            removed += 1
            logger.info("[compat_bridge] pruned dead bridge %s", entry.name)
        except OSError:
            logger.exception("[compat_bridge] prune(%s) failed", entry)
    return removed
