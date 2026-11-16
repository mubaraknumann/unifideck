"""services/cloud_save/ea_strategy.py — EA cloud saves through ``maxima-cli``.

EA describes each game's saves as rules: folders and patterns under Windows
folders such as ``%Documents%`` and ``%LocalAppData%``, with excludes.
``maxima-cli`` resolves them inside the game's prefix and moves the files
(unifideck-maxima patch ``unifideck-cloudsync``). It keeps a ledger in the
prefix of what that prefix last synced, so it can tell a file that is newer
here from one that is newer in the cloud. A file changed on both sides is a
conflict: nothing is transferred and the result counts it.

* Pull: a conflict keeps the local saves, the same as legendary when the local
  save is newer. ``force`` (the conflict modal's "Use Cloud") takes the cloud's.
* Push: a conflict raises a soft :class:`SaveConflictError`, so the service
  opens the conflict modal instead of overwriting the cloud.

Resolving the save folder runs ``maxima-cli`` (it reads the library from EA).
That must never run on the event loop, so the syncs look it up asynchronously
first, and the synchronous hook only runs the CLI from a worker thread (the
status page calls it through ``asyncio.to_thread``).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from unifideck.launcher.ea import maxima
from unifideck.launcher.proton.infrastructure.prefix_layout import resolve_registry_prefix
from unifideck.services.cloud_save import safety
from unifideck.services.cloud_save.strategy_base import (
    CloudSaveStrategy,
    CloudUnreachableError,
)
from unifideck.services.cloud_save.sync_record import (
    CLOUD_NEWER,
    CONFLICT,
    IN_SYNC,
    LOCAL_NEWER,
)
from unifideck.stores.ea.cli import MaximaCli, MaximaCliError
from unifideck.stores.ea.ids import offer_id

logger = logging.getLogger(__name__)
_LOG = "[EASync]"


class EACloudSaveStrategy(CloudSaveStrategy):
    """Cloud saves for EA games, through ``maxima-cli cloud-sync``."""

    store_id = "ea"
    #: ``cloud-status`` compares file by file, so no sync record is kept.
    reports_verdict = True

    def __init__(
        self, local_save_root: str, config: Any = None, cache: Any = None,
    ) -> None:
        super().__init__(local_save_root, config, cache)
        self._cli: MaximaCli | None = None
        #: Games whose EA offer has no cloud saves, so there is nothing to sync.
        self._no_cloud: set[str] = set()

    # ── CLI and prefix ───────────────────────────────────────────────
    def _maxima(self) -> MaximaCli:
        """The CLI the EA store runs, found on first use."""
        if self._cli is None:
            from unifideck.core.paths import resolve_plugin_dir
            cfg = (self.config.get("stores.ea") if self.config else None) or {}
            override = str(cfg.get("cli_path") or "") if isinstance(cfg, dict) else ""
            found = maxima.find_cli(resolve_plugin_dir(start=Path(__file__)), override)
            self._cli = MaximaCli(str(found) if found else None)
        return self._cli

    def _prefix(self, game_id: str) -> Path | None:
        """The game's prefix, or None before it exists (never installed)."""
        root = self._prefix_root(game_id)
        if not root.is_dir():
            return None
        return resolve_registry_prefix(root)

    # ── save folder ──────────────────────────────────────────────────
    async def _lookup_save_dir(self, game_id: str) -> str | None:
        """The first folder the game's save rules name, from ``cloud-paths``."""
        prefix = self._prefix(game_id)
        if prefix is None or game_id in self._no_cloud:
            return None
        try:
            paths = await self._maxima().cloud_paths(offer_id(game_id), prefix)
        except MaximaCliError as e:
            logger.warning("%s save rules for %s unavailable: %s", _LOG, game_id, e)
            return None
        if not paths.get("has_cloud_save"):
            logger.info("%s %s has no EA cloud saves", _LOG, game_id)
            self._no_cloud.add(game_id)
            return None
        folders = [f for f in paths.get("folders") or [] if isinstance(f, str) and f]
        if not folders:
            logger.warning(
                "%s EA save rules for %s name no folder: %s", _LOG, game_id, paths.get("rules"),
            )
            return None
        if len(folders) > 1:
            # Maxima syncs every folder; the status page and the local
            # backups see only the first.
            logger.info(
                "%s %s saves span %d folders; status and backups use %s",
                _LOG, game_id, len(folders), folders[0],
            )
        logger.info("%s save folder for %s: %s", _LOG, game_id, folders[0])
        return folders[0]

    async def _prefetch_save_dir(self, game_id: str) -> None:
        """Resolve the save folder off the event loop before a sync needs it."""
        if self._configured_save_dir(game_id) or game_id in self._cached_save_dir:
            return
        resolved = await self._lookup_save_dir(game_id)
        if resolved:
            self._cached_save_dir[game_id] = resolved

    def _resolve_store_save_dir(self, game_id: str) -> str | None:
        """Run ``cloud-paths`` only where blocking is allowed.

        In a worker thread there is no running loop and the CLI may run. On
        the event loop it would stall every RPC for seconds, so this returns
        None there; the syncs prefetch the folder before they get here.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._lookup_save_dir(game_id))
        return None

    # ── syncs ────────────────────────────────────────────────────────
    async def sync_down(self, game_id: str, force: bool = False) -> bool:
        """Pull, after resolving the save folder. No cloud saves is not a failure."""
        await self._prefetch_save_dir(game_id)
        if game_id in self._no_cloud:
            return True
        return await super().sync_down(game_id, force)

    async def sync_up(self, game_id: str, auto: bool = False) -> bool:
        """Push, after resolving the save folder. No cloud saves is not a failure."""
        await self._prefetch_save_dir(game_id)
        if game_id in self._no_cloud:
            return True
        return await super().sync_up(game_id, auto)

    async def _fetch_cloud_info(self, game_id: str) -> dict[str, Any] | None:
        """The cloud's save set and a file-by-file verdict, from ``cloud-status``."""
        prefix = self._prefix(game_id)
        if prefix is None or game_id in self._no_cloud:
            return None
        try:
            status = await self._maxima().cloud_status(offer_id(game_id), prefix)
        except MaximaCliError as e:
            if e.code in ("timeout", "command_failed"):
                raise CloudUnreachableError(str(e)) from e
            logger.debug("%s cloud status for %s unavailable: %s", _LOG, game_id, e)
            return None
        if not status.get("has_cloud_save"):
            self._no_cloud.add(game_id)
            return None
        files = int(status.get("cloud_files") or 0)
        return {
            "has_saves": files > 0,
            "timestamp": float(status.get("cloud_modified") or 0.0),
            "file_count": files,
            "total_bytes": int(status.get("cloud_bytes") or 0),
            "verdict": cloud_verdict(status),
        }

    async def _cloud_sync(
        self, game_id: str, *, write: bool, force: bool = False,
    ) -> dict[str, Any] | None:
        """One ``cloud-sync`` run; None (logged) when it failed."""
        prefix = self._prefix(game_id)
        if prefix is None:
            logger.warning("%s no prefix for %s; nothing to sync", _LOG, game_id)
            return None
        direction = "push" if write else "pull"
        try:
            result = await self._maxima().cloud_sync(
                offer_id(game_id), prefix, write=write, force=force,
            )
        except MaximaCliError:
            logger.exception("%s %s for %s failed", _LOG, direction, game_id)
            return None
        logger.info("%s %s for %s: %s", _LOG, direction, game_id, result)
        return result

    async def _do_sync_down(
        self, game_id: str, local_dir: str, force: bool,
    ) -> bool:
        """Pull EA cloud saves into the prefix (the base already snapshotted)."""
        result = await self._cloud_sync(game_id, write=False, force=force)
        if result is None:
            return False
        if result.get("conflicts"):
            # Same outcome as legendary with a newer local save: nothing
            # pulled, local kept. The modal's "Use Cloud" pulls with force.
            logger.warning(
                "%s pull for %s took NOTHING: %s file(s) changed both here and in "
                "the cloud since the last sync; local saves kept",
                _LOG, game_id, result.get("conflicts"),
            )
        return True

    async def _do_sync_up(
        self, game_id: str, local_dir: str, *, force: bool = False,
    ) -> bool:
        """Push the prefix's saves to EA (the base already guarded them).

        Without ``force`` a file changed in the cloud since this prefix last
        synced stops the push; that is a conflict for the user to settle, not
        a failure. A manual Upload passes ``force`` and overwrites it.
        """
        result = await self._cloud_sync(game_id, write=True, force=force)
        if result is None:
            return False
        if result.get("conflicts"):
            raise safety.SaveConflictError(
                "cloud_changed_since_last_sync", safety.snapshot(local_dir),
                store=self.store_id, game_id=game_id, hard=False,
            )
        return True


def cloud_verdict(status: dict[str, Any]) -> str:
    """One verdict from ``cloud-status``'s per-file counts."""
    def count(key: str) -> int:
        return int(status.get(key) or 0)

    cloud_ahead = count("cloud_newer") + count("cloud_only")
    local_ahead = count("local_newer") + count("local_only")
    if count("conflicts") or (cloud_ahead and local_ahead):
        return CONFLICT
    if cloud_ahead:
        return CLOUD_NEWER
    if local_ahead:
        return LOCAL_NEWER
    return IN_SYNC

