"""compat/save_migration.py — bring a game's saves forward into a prefix.

Split out of ``compat/prefix_init.py``, which owns the prefix *lifecycle*
(reset on a Proton change, createprefix, the umu subprocess ladder). This
module owns the orthogonal concern of not losing user data across those
events, and has two sources for it:

1. **``.save_backup``** — ``prefix_init._reset_prefix`` copies
   ``drive_c/users`` aside before wiping a prefix. Nothing used to put it
   back, so a Proton-family change silently lost saves.
2. **The legacy shared umu prefix** — pre-0.6 launches didn't set
   ``WINEPREFIX``, so games ran in umu's default prefix
   (``~/Games/umu/umu-0``). After upgrading, the new per-game prefix is empty
   and the saves look lost.

Every copy is a **non-destructive, mtime-guarded merge**: a file is written
only when the destination is missing or older, so a save written after a reset
is never clobbered by a stale backup and a repeat run is harmless. Everything
here is best-effort — a failure logs and the launch proceeds.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.launcher.proton.infrastructure.prefix_layout import (
    resolve_registry_prefix,
)

if TYPE_CHECKING:
    from unifideck.launcher.proton.infrastructure.core import ProtonLaunchPlan

logger = logging.getLogger(__name__)

# Written into a per-game prefix once the one-time legacy-save migration
# has run, so we don't rescan the legacy umu prefixes on every launch.
_LEGACY_MIGRATED_MARKER = ".unifideck_legacy_migrated"
# Shared umu prefixes used before 0.6 set a per-game WINEPREFIX. Games
# launched then wrote their saves into umu's default prefix; we pull
# those forward on the first per-game prefix init.
_LEGACY_UMU_BASE = "~/Games/umu"
_LEGACY_UMU_SHARED = ("umu-0", "umu-default")

# A ``drive_c/users`` tree is a full Windows user profile, not a save folder.
# Both of this module's sources (the legacy shared umu prefix and a reset's
# ``.save_backup``) therefore carry a lot that is emphatically NOT save data,
# and copying it forward bloated every prefix we created.
#
# Measured on the machine this was found on: a fresh prefix came out at 1.5 GB,
# **939 MB of it a CD Projekt Red REDlauncher installation** dragged out of
# ``~/Games/umu/umu-0`` — a vendor launcher for one game cloned into every
# other game's prefix, on every install, forever. It logged as
# "migrated 1159 save file(s)", which reads like a success.
#
# The rule is a DENY-list, not an allow-list, and deliberately so: a wrong
# entry here costs disk, whereas a missing allow-list entry would silently
# lose somebody's save. Games legitimately save under ``AppData/Local``
# (as well as Roaming/LocalLow/Documents/Saved Games), so that root stays in
# and only its known-non-save subtrees come out.
_EXCLUDED_RELPATHS: tuple[tuple[str, ...], ...] = (
    # User-scoped *program installations* — the 939 MB case.
    ("appdata", "local", "programs"),
    # Installer/runtime scratch: vcredist logs, extracted redistributables.
    ("appdata", "local", "temp"),
    ("appdata", "local", "crashdumps"),
    # Windows/Wine scaffolding recreated by every prefix; never game saves.
    ("appdata", "local", "microsoft"),
    ("appdata", "local", "packages"),
    ("appdata", "roaming", "microsoft"),
)
# A single save file larger than this is vanishingly rare; a program payload
# that large is not. Belt-and-braces behind the deny-list above, so a vendor
# tree we haven't seen yet can't quietly reintroduce the bloat.
_MAX_SAVE_FILE_BYTES = 64 * 1024 * 1024


def _is_migratable(rel: Path) -> bool:
    """Whether a path relative to ``users/`` is plausibly save data.

    ``rel`` is ``<username>/AppData/...`` (the source is a ``users`` tree, so
    the first component is ``steamuser``/``Public``/…). The deny-list is
    written from just below that, and is also matched against the un-stripped
    path so a tree handed in already rooted at the user dir still filters.

    See :data:`_EXCLUDED_RELPATHS` for why this is a deny-list.
    """
    parts = tuple(p.lower() for p in rel.parts)
    for candidate in (parts[1:], parts):
        if any(
            candidate[:len(excluded)] == excluded
            for excluded in _EXCLUDED_RELPATHS
        ):
            return False
    return True


def _merge_users(src_users: Path, dst_users: Path) -> int:
    """Copy save files from ``src_users`` into ``dst_users``, non-destructively.

    A file is copied only when it looks like save data (:func:`_is_migratable`,
    plus a size guard) AND the destination is missing or older than the source
    (mtime guard) — so a save written after a reset is never clobbered by a
    stale backup, and the merge is safe to re-run. Per-file errors are logged
    and skipped — best-effort, like the rest of this module. Returns the number
    of files actually copied.
    """
    if not src_users.is_dir():
        return 0
    copied = 0
    pruned = 0
    for parent, dirnames, filenames in os.walk(src_users):
        parent_path = Path(parent)
        rel_parent = parent_path.relative_to(src_users)
        pruned += _prune_excluded(rel_parent, dirnames)
        for name in filenames:
            if _copy_one_save(parent_path / name, rel_parent / name, dst_users):
                copied += 1
    if pruned:
        logger.info(
            "[prefix_init] save merge pruned %d non-save subtree(s) under %s",
            pruned, src_users,
        )
    return copied


def _prune_excluded(rel_parent: Path, dirnames: list[str]) -> int:
    """Drop excluded directories from ``dirnames`` in place; return how many.

    This is the whole point of walking rather than globbing. The previous
    ``rglob("*")`` visited and ``stat()``ed every file in the tree before
    ``_is_migratable`` rejected it, so each new prefix paid a full scan of a
    profile whose bulk is exactly the part being thrown away: measured 6-14 s
    per install to descend a 947 MiB / 1097-file ``appdata/local/programs``
    tree and copy 62 files out of it. Mutating ``dirnames`` is how ``os.walk``
    is told not to descend, so those subtrees are never opened at all.
    """
    kept = [name for name in dirnames if _is_migratable(rel_parent / name)]
    dropped = len(dirnames) - len(kept)
    if dropped:
        dirnames[:] = kept
    return dropped


def _copy_one_save(src: Path, rel: Path, dst_users: Path) -> bool:
    """Copy one file if it is save data and newer than the destination.

    The ``_is_migratable`` check is kept here as well as in
    :func:`_prune_excluded`: a deny-listed path can name a *file* rather than a
    directory, and pruning only ever sees directories.
    """
    if not _is_migratable(rel):
        return False
    try:
        size = src.stat().st_size
        if size > _MAX_SAVE_FILE_BYTES:
            logger.info(
                "[prefix_init] save merge skipped oversized %s (%d bytes)",
                rel, size,
            )
            return False
        dst = dst_users / rel
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except OSError as e:
        logger.warning("[prefix_init] save merge skipped %s: %s", src, e)
        return False
    return True


def snapshot_user_saves(src_users: Path, dst: Path) -> int:
    """Copy the save files out of a ``users`` tree into *dst*.

    Public because ``prefix_init._reset_prefix`` needs it: that is the OTHER
    place a ``users`` tree gets copied (into ``.save_backup``, before a
    Proton-family change wipes the prefix). It used to be a raw
    ``shutil.copytree`` of the whole profile, which is the same bug this
    module's merge had — and worse, ``.save_backup`` is in ``_PRESERVE``, so
    the snapshot survives the wipe and keeps the bloat on disk.

    Sharing this one function means the definition of "is this a save?"
    (:func:`_is_migratable`) lives in exactly one place and can't drift
    between the snapshot and the restore.
    """
    return _merge_users(src_users, dst)


def _users_has_files(users_dir: Path) -> bool:
    """True if ``users_dir`` holds at least one regular file."""
    if not users_dir.is_dir():
        return False
    try:
        return any(p.is_file() for p in users_dir.rglob("*"))
    except OSError:
        return False


def _restore_save_backup(prefix_root: Path) -> None:
    """Merge a prior reset's ``.save_backup`` into the live prefix.

    ``_reset_prefix`` copies ``drive_c/users`` to ``.save_backup`` before
    wiping the prefix but nothing used to put it back, so a Proton-family
    change silently lost saves. We restore it after the prefix is
    recreated. The backup is left in place — the mtime-guarded merge
    makes a repeat harmless and the next reset refreshes it.
    """
    backup = prefix_root / ".save_backup"
    if not backup.is_dir():
        return
    dst_users = resolve_registry_prefix(prefix_root) / "drive_c" / "users"
    copied = _merge_users(backup, dst_users)
    if copied:
        logger.info(
            "[prefix_init] restored %d save file(s) from .save_backup", copied,
        )


def _legacy_prefix_candidates(plan: ProtonLaunchPlan) -> list[Path]:
    """Legacy shared-umu prefixes that may hold this game's old saves."""
    base = Path(_LEGACY_UMU_BASE).expanduser()
    candidates: list[Path] = []
    game_gameid = (plan.env or {}).get("GAMEID")
    if game_gameid:
        # Old launchers that set a per-game GAMEID but no WINEPREFIX.
        candidates.append(base / game_gameid)
    candidates.extend(base / name for name in _LEGACY_UMU_SHARED)
    return candidates


def _migrate_legacy_prefix(plan: ProtonLaunchPlan, prefix_root: Path) -> None:
    """One-time: pull saves from a legacy shared umu prefix into this one.

    Pre-0.6 launches didn't set ``WINEPREFIX``, so games ran in umu's
    shared default prefix (``~/Games/umu/umu-0``). After upgrading, the
    new per-game prefix is empty and saves look lost. Copy the legacy
    ``drive_c/users`` tree forward (first candidate with real data wins),
    leaving the legacy prefix untouched so other games can migrate from
    it too. Idempotent via a per-prefix marker.
    """
    marker = prefix_root / _LEGACY_MIGRATED_MARKER
    if marker.exists():
        return
    dst_users = resolve_registry_prefix(prefix_root) / "drive_c" / "users"
    for candidate in _legacy_prefix_candidates(plan):
        src_users = resolve_registry_prefix(candidate) / "drive_c" / "users"
        if not _users_has_files(src_users):
            continue
        copied = _merge_users(src_users, dst_users)
        logger.info(
            "[prefix_init] migrated %d save file(s) from legacy prefix %s",
            copied, candidate,
        )
        break
    # Mark done even when nothing matched so we don't rescan every launch;
    # the merge is mtime-guarded, so a future re-run would be harmless.
    with contextlib.suppress(OSError):
        marker.write_text("done", encoding="utf-8")


async def restore_or_migrate_saves(
    plan: ProtonLaunchPlan, prefix_root: Path,
) -> None:
    """After a fresh prefix is created, bring prior saves forward.

    A reset's ``.save_backup`` (this exact prefix's own data) is the most
    specific source and wins; otherwise fall back to a one-time legacy
    shared-prefix migration. Runs the blocking copy off the event loop.
    """
    if (prefix_root / ".save_backup").is_dir():
        await asyncio.to_thread(_restore_save_backup, prefix_root)
    else:
        await asyncio.to_thread(_migrate_legacy_prefix, plan, prefix_root)
