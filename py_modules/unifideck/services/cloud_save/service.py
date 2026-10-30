"""services/cloud_save/service.py — Cloud save synchronization.

Subscribes to lifecycle events so saves sync around launches:
- ``GAME_LAUNCHED`` → ``sync_down``
- ``GAME_STOPPED`` → ``sync_up``
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events, Result
from unifideck.event_bus.event_bus import EventBus
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe

from .epic_strategy import EpicCloudSaveStrategy
from .gog_strategy import GOGCloudSaveStrategy
from .safety import SaveConflictError
from .status import _StatusMixin
from .sync import _SyncMixin

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

# Strong references to background sync tasks so the GC can't
# collect them mid-flight (see RUF006).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> None:
    """Register a fire-and-forget task so the GC doesn't collect it early."""
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


class CloudSaveService(_SyncMixin, _StatusMixin):
    """Reactive cloud save sync for game launches.

    Save-location resolution and the manual-button status surface live in
    ``_StatusMixin`` (status.py); sync helpers in ``_SyncMixin`` (sync.py).
    """

    def __init__(
        self,
        bus: EventBus,
        local_save_root: str,
        cloud_root: str | None = None,
        config: ConfigManager | None = None,
        cache: Any = None,
    ) -> None:
        self._bus = bus
        self._local_root = local_save_root
        self._cloud_root = cloud_root
        self._config = config
        self._cache = cache

        self._syncing: dict[str, asyncio.Lock] = {}
        self._tolerance = 2.0
        self._sync_wait_timeout = 30.0

        if config:
            self._tolerance = config.get("cloud.tolerance_seconds", self._tolerance)
            self._sync_wait_timeout = config.get("cloud.sync_wait_timeout_seconds", self._sync_wait_timeout)

        # Initialize store strategies
        self._strategies = {
            "epic": EpicCloudSaveStrategy(self._local_root, config, cache),
            "gog": GOGCloudSaveStrategy(self._local_root, config, cache),
        }

        auto_wire(self, self._bus)

        if not self._cloud_root:
            logger.info("[CloudSaveService] starting without cloud_root backup fallback")
        else:
            logger.info("[CloudSaveService] starting with cloud_root=%s", self._cloud_root)

    async def stop(self) -> None:
        """Unsubscribe from EventBus events (shutdown/tests)."""
        self._bus.unsubscribe_all(self)

        in_flight = [
            (key, lock) for key, lock in self._syncing.items()
            if lock.locked()
        ]
        if not in_flight:
            return
        logger.info(
            "[CloudSaveService] waiting for %d in-flight syncs",
            len(in_flight),
        )

        async def _drain(lock: asyncio.Lock) -> None:
            await lock.acquire()
            lock.release()

        try:
            await asyncio.wait_for(
                asyncio.gather(*(_drain(lock) for _key, lock in in_flight)),
                timeout=5.0,
            )
        except TimeoutError:
            still_held = [
                key for key, lock in in_flight if lock.locked()
            ]
            logger.warning(
                "[CloudSaveService] shut down with %d syncs incomplete: %s",
                len(still_held), still_held,
            )

    @subscribe(Events.GAME_LAUNCHED)
    async def _on_game_launched(self, **kwargs: Any) -> None:
        """Download saves before the game starts (when auto-pull is enabled).

        ``sync_down`` is the non-destructive direction (it snapshots before
        pulling and never deletes local saves), so it stays automatic by
        default. Gated by ``cloud.auto_pull_on_launch`` so users can opt out.
        """
        if not self._auto_enabled("auto_pull_on_launch", default=True):
            return
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")

        if not store or not game_id:
            return

        # Fire and forget; background task
        _track(asyncio.create_task(self.sync_down(store, game_id)))

    @subscribe(Events.GAME_STOPPED)
    async def _on_game_stopped(self, **kwargs: Any) -> None:
        """Upload saves after the game exits — ONLY when auto-push is enabled.

        Uploads are the destructive direction (they can overwrite cloud saves),
        so by default this is OFF: the user pushes deliberately via the
        cloud-save button. Set ``cloud.auto_push_on_stop`` true to restore
        fully-automatic sync.
        """
        if not self._auto_enabled("auto_push_on_stop", default=False):
            return
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")

        if not store or not game_id:
            return

        # Fire and forget; background task
        _track(asyncio.create_task(self.sync_up(store, game_id)))

    def auto_sync_enabled(self, direction: str) -> bool:
        """Whether AUTOMATIC sync is enabled for ``direction`` ("down"/"up").

        Public so the launcher's auto-sync phase respects the same flags as the
        event subscribers — ``down`` ⇒ ``auto_pull_on_launch`` (default on),
        ``up`` ⇒ ``auto_push_on_stop`` (default off). Manual button/conflict
        syncs call ``sync_down``/``sync_up`` directly and are never gated here.
        """
        if direction == "down":
            return self._auto_enabled("auto_pull_on_launch", default=True)
        if direction == "up":
            return self._auto_enabled("auto_push_on_stop", default=False)
        return True

    async def sync_down(self, store: str, game_id: str, force: bool = False) -> Result:
        """Pull cloud saves before a game launch.

        ``force`` pulls the cloud copy unconditionally (explicit "Use Cloud");
        the automatic on-launch path leaves it False so newer local saves are
        never silently overwritten.
        """
        if self._config and not self._config.get_bool("cloud.enabled", True):
            logger.info("[CloudSaveService] Cloud sync is disabled globally")
            return Result(success=True)

        key = f"{store}:{game_id}"
        lock, timeout_result = await self._acquire_sync_lock(
            key, "CLOUD_SYNC_DOWN_FAILED", store, game_id,
        )
        if lock is None:
            return timeout_result if timeout_result is not None else Result(
                success=False, error="lock_acquire_failed",
            )

        try:
            success = True
            store_handled = store in self._strategies

            # The store's own tool (gogdl/legendary) is the ONLY cloud restore
            # source. We never pull from the local ~/Save Games Backup mirror.
            if store_handled:
                logger.info("[CloudSaveService] Executing %s sync_down strategy for %s (force=%s)", store, game_id, force)
                success = await self._strategies[store].sync_down(game_id, force)

            # Write-only safety backup of the now-current local saves.
            await self._backup_to_mirror(store, game_id)

            # Signal completion so the (fire-and-forget) frontend can react —
            # the manual Download runs as a background task and never blocks
            # the RPC, so the result is delivered via this event, not the call.
            if success:
                await self._emit_down("CLOUD_SYNC_DOWN_COMPLETE", store, game_id, synced=True)
            else:
                await self._emit_down("CLOUD_SYNC_DOWN_FAILED", store, game_id, error="sync_failed")
            return Result(success=success)
        except Exception as e:
            logger.exception("[CloudSaveService] sync_down failed for %s", key)
            await self._emit_down("CLOUD_SYNC_DOWN_FAILED", store, game_id, error=str(e))
            return Result(success=False, error=str(e))
        finally:
            lock.release()

    async def sync_up(self, store: str, game_id: str) -> Result:
        """Push saves after a game exits."""
        if self._config and not self._config.get_bool("cloud.enabled", True):
            logger.info("[CloudSaveService] Cloud sync is disabled globally")
            return Result(success=True)

        key = f"{store}:{game_id}"
        lock, timeout_result = await self._acquire_sync_lock(
            key, "CLOUD_SYNC_UP_FAILED", store, game_id,
        )
        if lock is None:
            return timeout_result if timeout_result is not None else Result(
                success=False, error="lock_acquire_failed",
            )

        try:
            success = True
            store_handled = store in self._strategies
            conflict_handled = False

            # The store's own tool (gogdl/legendary) is the ONLY cloud upload
            # path. The ~/Save Games Backup mirror is write-only (see
            # _backup_to_mirror) — never a sync source.
            if store_handled:
                logger.info("[CloudSaveService] Executing %s sync_up strategy for %s", store, game_id)
                try:
                    success = await self._strategies[store].sync_up(game_id)
                except SaveConflictError as conflict:
                    conflict_handled = True
                    await self._handle_sync_up_conflict(store, game_id, key, conflict)

            # Write-only safety backup of the current local saves.
            await self._backup_to_mirror(store, game_id)

            # Signal completion for the fire-and-forget frontend (see sync_down).
            # On a conflict the upload was BLOCKED, not completed — the conflict
            # modal / error toast is the user-facing signal, so don't also emit
            # a COMPLETE (which would wrongly read as "uploaded").
            if not conflict_handled:
                if success:
                    await self._emit_up("CLOUD_SYNC_UP_COMPLETE", store, game_id, synced=True)
                else:
                    await self._emit_up("CLOUD_SYNC_UP_FAILED", store, game_id, error="sync_failed")
            return Result(success=success)
        except Exception as e:
            logger.exception("[CloudSaveService] sync_up failed for %s", key)
            await self._emit_up("CLOUD_SYNC_UP_FAILED", store, game_id, error=str(e))
            return Result(success=False, error=str(e))
        finally:
            lock.release()

    async def _handle_sync_up_conflict(
        self, store: str, game_id: str, key: str, conflict: SaveConflictError,
    ) -> None:
        """React to a strategy refusing to push (would WIPE cloud saves).

        Never auto-destroy, and never treat this as a launch failure — saves
        are intact and locally backed up. HARD (empty/no-save-data) surfaces a
        plain error toast; SOFT (diverged/regressed) surfaces the pick modal.
        """
        if conflict.hard:
            # HARD: empty / no-save-data. Uploading nothing could only wipe the
            # cloud, so it's never a valid choice — plain error, not a pick.
            logger.error(
                "[CloudSaveService] sync_up REFUSED for %s (%s) — "
                "no local save data; cloud copy preserved",
                key, conflict.reason,
            )
            await self._emit_save_error(store, game_id)
        else:
            # SOFT: local still has saves but diverged/regressed — surface the
            # conflict modal so the user picks.
            logger.warning(
                "[CloudSaveService] sync_up BLOCKED for %s (%s) — "
                "raising cloud-save conflict instead of wiping",
                key, conflict.reason,
            )
            await self._emit_save_conflict(store, game_id, conflict)

    async def _emit_save_conflict(
        self, store: str, game_id: str, conflict: SaveConflictError,
    ) -> None:
        """Surface a blocked upload as a user-facing cloud-save conflict.

        Reuses the existing ``retry-sync`` modal flow: emits a
        ``LAUNCHER_STAGE`` event carrying the local snapshot and a
        ``retry-sync`` action so ``CloudSaveConflictModal`` opens. The
        modal's "Use Cloud" choice dispatches ``retry-sync … sync_down``
        which re-pulls the cloud saves (the safe resolution for a local
        regression); "Use Local" maps to ``sync_up`` which simply hits this
        same guard again — we never auto-overwrite the cloud.
        """
        if not self._bus:
            return
        game_title = ""
        if self._config:
            game_title = self._config.get(f"games.{game_id}.title") or ""
        await self._bus.emit(
            Events.LAUNCHER_STAGE,
            store=store,
            game_id=game_id,
            # The listener opens the conflict modal for this payload, which
            # renders its own copy from cloudSave.*; this key is not shown.
            i18n_title_key="toasts.header.cloudSaves",
            i18n_key="toasts.cloudConflict",
            severity="warning",
            game_title=game_title or game_id,
            local_snapshot=conflict.local,
            remote_snapshot=self._cloud_snapshot(store, game_id),
            action={
                "verb": "retry-sync",
                "args": [store, game_id, "sync_down"],
            },
        )

    async def _backup_to_mirror(self, store: str, game_id: str) -> None:
        """Write-only safety backup: mirror current local saves to ``_cloud_root``.

        The ``~/Save Games Backup`` mirror is WRITE-ONLY — we copy the current
        local saves into it after a sync, but NEVER pull from it. The only
        restore source is the store's own cloud (gogdl/legendary). Guarded so an
        empty/missing local dir never overwrites a good backup, and best-effort
        so a backup failure never affects the sync result.
        """
        if not self._cloud_root:
            return
        try:
            local_dir = self.get_local_save_dir(store, game_id)
            if not local_dir or not self._has_real_local_saves(local_dir):
                return
            mirror = Path(self._cloud_root) / store / game_id
            await self._copy_tree(local_dir, str(mirror))
            logger.info("[CloudSaveService] Backed up saves to mirror: %s", mirror)
        except Exception as e:
            logger.warning(
                "[CloudSaveService] backup-to-mirror failed (non-fatal) for %s:%s: %s",
                store, game_id, e,
            )

    @staticmethod
    def _has_real_local_saves(local_dir: str) -> bool:
        """True if ``local_dir`` exists and holds real save data.

        Kept synchronous (not inline in the async ``_backup_to_mirror``) so the
        blocking ``is_dir`` stat doesn't trip the async-blocking-call gate; a
        single local stat is negligible.
        """
        from .safety import has_save_data
        return Path(local_dir).is_dir() and has_save_data(local_dir)

    def _game_title(self, store: str, game_id: str) -> str:
        """Human-readable title for toasts: config → metadata cache → id.

        The raw store id (e.g. a GOG numeric id) is a poor thing to show a
        user, so prefer a real title wherever one is cached.
        """
        if self._config:
            title = self._config.get(f"games.{game_id}.title")
            if title:
                return str(title)
        if self._cache is not None:
            try:
                meta = self._cache.get("metadata", f"{store}:{game_id}")
            except Exception:
                meta = None
            if isinstance(meta, dict) and meta.get("title"):
                return str(meta["title"])
        return game_id

    async def _emit_save_error(
        self, store: str, game_id: str,
    ) -> None:
        """Surface a HARD-blocked (empty) upload as a short title+body toast.

        No ``retry-sync`` action → the listener shows a plain toast, not the
        pick modal: uploading nothing is never a valid choice. The cloud copy
        is left untouched. Severity is a warning (this is an expected skip
        when there are no local saves, not a failure).
        """
        if not self._bus:
            return
        await self._bus.emit(
            Events.LAUNCHER_STAGE,
            store=store,
            game_id=game_id,
            i18n_title_key="toasts.header.cloudSaves",
            i18n_key="toasts.cloudUploadSkippedBody",
            severity="warning",
        )
