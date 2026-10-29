"""Storage id → install path, for the download RPCs.

Split out of ``download.py`` (volumetry cap). ``download.py`` re-exports
``_resolve_storage_path`` and ``_size_cache_file``, the two names it and
its tests use.
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from unifideck.utils import mount_naming, mounts

logger = logging.getLogger(__name__)

# ─── Storage type → path resolution ───────────────────────────
#
# External-mount enumeration is delegated to ``utils/mounts.py``
# (shared with ``rpc/mixins/storage.py`` and ``utils/paths.py``) —
# see that module for why a FUSE-mounted external drive (NTFS via
# ntfs-3g, some exFAT setups) needs a demoted subprocess to be
# reachable from this backend's root process at all.

def _size_cache_file(config: Any) -> str:
    """Path to the persistent download-size cache (in the data dir).

    Mirrors ``SyncRPCMixin._size_cache_path`` — kept as a free function
    here rather than reaching across to the sibling mixin's method
    (mypy checks each mixin's ``self`` independently). TODO: both should
    share one helper in ``services/size_cache.py``.
    """
    data_dir = "~/.local/share/unifideck"
    if config is not None:
        with contextlib.suppress(Exception):
            data_dir = (
                config.get("paths.data_dir", None)
                or config.get("data_dir", data_dir)
                or data_dir
            )
    return str(Path(data_dir).expanduser() / "game_sizes.json")

def _resolve_storage_path(storage_type: str | None, config: Any) -> str | None:
    """Convert a storage type string to a filesystem path.

    ``"internal"``      → ``~/Games``,
    ``"sdcard"``         → legacy alias, first eligible external mount,
    ``"ext:<name>"``     → that SPECIFIC external mount only,
    ``"custom"``         → ``download.custom_path`` from config,
    ``None``             → ``None`` (store connector uses its default).
    """
    if not storage_type:
        return None

    if storage_type == "internal":
        path = str(Path.home() / "Games")
        logger.debug("[download] resolved internal → %s", path)
        return path
    if storage_type == "custom":
        return _custom_path(config)
    if storage_type == "sdcard" or storage_type.startswith("ext:"):
        return _external_games_path(storage_type)
    logger.warning("[download] unknown storage type: %s", storage_type)
    return None

def _external_games_path(storage_type: str) -> str | None:
    """Resolve an external-mount storage id to its ``Games/`` dir.

    ``storage_type == "sdcard"`` is the legacy alias (configs/callers
    predating the unique-id fix) — resolved to the first eligible
    external mount, preserving pre-fix behavior. Any other value is a
    unique ``ext:<uuid>`` id from ``mounts.mount_id`` (or the older
    ``ext:<name>`` form, still accepted) — resolved to that SPECIFIC
    mount only; if it's no longer eligible (unplugged, no longer
    writable) this returns ``None`` rather than silently substituting a
    different device (the caller falls back to internal storage — see
    ``install_game``).
    """
    home_dev = mounts.stat_dev(str(Path.home()))
    # Dedupe first so the "first external" (legacy alias) and the unique-id
    # assignment match the storage picker's device list exactly.
    externals = mounts.dedupe_by_device(
        mounts.scan_mounts(home_dev, require_writable=True),
    )
    if storage_type == "sdcard":
        match = externals[0] if externals else None
        if match is not None:
            logger.debug(
                "[download] legacy 'sdcard' resolved to first external mount: %s",
                match.mount_point,
            )
    else:
        match = next(
            (m for loc_id, m in mounts.assign_unique_ids(externals)
             if loc_id == storage_type),
            None,
        )
        if match is None:
            # An id saved before ids moved to the filesystem UUID, e.g.
            # a queued install or a frontend that hasn't re-fetched the
            # picker yet. Resolve it the old way rather than dropping
            # the user's chosen drive and falling back to internal.
            match = next(
                (m for m in externals
                 if mount_naming.legacy_mount_id(m.mount_point) == storage_type),
                None,
            )
            if match is not None:
                logger.info(
                    "[download] legacy name-based id %s resolved to %s",
                    storage_type, match.mount_point,
                )
    if match is None:
        logger.warning("[download] no eligible external mount for storage=%s", storage_type)
        return None
    games_path = mounts.ensure_games_subdir(
        match.mount_point, match.effective_uid, match.effective_gid,
    )
    logger.debug("[download] resolved %s → %s (%s)", storage_type, games_path, match.fstype)
    return games_path

def _custom_path(config: Any) -> str | None:
    """Read ``download.custom_path`` from config; None if unset/invalid."""
    if config is None:
        return None
    try:
        path = config.get("download.custom_path", None)
    except Exception as e:
        logger.warning("[download] custom_path lookup failed: %s", e)
        return None
    if isinstance(path, str) and path:
        logger.debug("[download] resolved custom → %s", path)
        return path
    return None
