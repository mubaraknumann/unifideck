"""services/achievements/watcher.py — live + post-play GOG achievement tracking.

GOG achievements unlock *in-game* via Comet, which uploads them to GOG's
servers in real time (see ``launcher/proton/compat/gog.py``). This watcher
reads them back from GOG so the user gets feedback the plugin otherwise can't
provide (Comet's overlay is disabled on Linux):

* **During play** — polls every ~60s while a GOG game runs and fires a
  Steam-style toast for each newly-unlocked achievement (via the launcher
  ``frontend_bridge``, the panel-independent toast path).
* **At game-stop** — a final reconcile (Comet has already flushed by then, as
  the launcher waits for it before the process exits, so Steam reports the
  stop only afterwards) and records a per-game ``last_session`` summary the
  game-info panel surfaces.

Only GOG is handled (Epic unlocks + notifies via its own EOS overlay). Driven
by the plugin-bus ``GAME_LAUNCHED`` / ``GAME_STOPPED`` events that the frontend
raises from Steam's app-lifetime notifications.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe
from unifideck.stores.gog.achievements import (
    GOGAchievements,
    GOGAchievementsError,
)

from .state import AchievementStateStore

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)

_STORE = "gog"
_POLL_INTERVAL_SECONDS = 60.0
_RECONCILE_RETRIES = 2
_RECONCILE_RETRY_DELAY_SECONDS = 5.0

# Strong refs to fire-and-forget reconcile tasks so the GC can't collect them
# mid-flight (RUF006); the done-callback discards each when it settles.
_BACKGROUND: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> None:
    """Track."""
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


class AchievementWatcher:
    """Live unlock toasts + an end-of-session summary for GOG games."""

    def __init__(
        self,
        bus: EventBus,
        registry: StoreRegistry | None,
        config: ConfigManager | None = None,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._registry = registry
        self._config = config
        self._state = AchievementStateStore()
        self._poll_tasks: dict[str, asyncio.Task[Any]] = {}
        self._baseline: dict[str, set[str]] = {}
        self._seen: dict[str, set[str]] = {}
        auto_wire(self, bus)

    @subscribe(Events.GAME_LAUNCHED)
    async def _on_game_launched(self, **kwargs: Any) -> None:
        """Snapshot the baseline unlocks and start the during-play poll."""
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")
        if store != _STORE or not game_id:
            return
        key = f"{store}:{game_id}"
        baseline = await self._unlocked_now(game_id)
        if baseline is None:
            # Offline at launch — fall back to the last persisted set so the
            # session diff still has a reference point.
            entry = await asyncio.to_thread(self._state.get, store, game_id)
            baseline = set((entry or {}).get("unlocked_keys") or [])
        self._baseline[key] = set(baseline)
        self._seen[key] = set(baseline)
        self._cancel(key)
        self._poll_tasks[key] = asyncio.create_task(
            self._poll_loop(store, game_id),
        )

    @subscribe(Events.GAME_STOPPED)
    async def _on_game_stopped(self, **kwargs: Any) -> None:
        """Stop polling and reconcile the session in the background."""
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")
        if store != _STORE or not game_id:
            return
        self._cancel(f"{store}:{game_id}")
        gog = self._gog()
        if gog is not None:
            # Drop the cache so the reconcile pull is fresh (Comet flushed).
            gog.invalidate_achievements(game_id)
        # Fire-and-forget so the GAME_STOPPED emit isn't blocked on the
        # network reconcile + retries (mirrors CloudSaveService.sync_up).
        _track(asyncio.create_task(self._finish_session(store, game_id)))

    async def get_last_session(
        self, store: str, game_id: str,
    ) -> dict[str, Any] | None:
        """Fast, local-only read of the last session's unlock summary (for RPC)."""
        entry = await asyncio.to_thread(self._state.get, store, game_id)
        return (entry or {}).get("last_session") if entry else None

    async def stop(self) -> None:
        """Unsubscribe + cancel in-flight poll tasks (shutdown/tests)."""
        self._bus.unsubscribe_all(self)
        for key in list(self._poll_tasks):
            self._cancel(key)

    # -- internals ---------------------------------------------------------

    async def _poll_loop(self, store: str, game_id: str) -> None:
        """Poll."""
        key = f"{store}:{game_id}"
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                payload = await self._fetch(game_id)
                if payload is not None:
                    self._emit_new_unlocks(store, game_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "[achievements.watcher] poll loop ended for %s", key,
                exc_info=True,
            )

    async def _finish_session(self, store: str, game_id: str) -> None:
        """Final reconcile + persist the session summary, then clean up."""
        key = f"{store}:{game_id}"
        try:
            await self._reconcile(store, game_id)
        finally:
            self._baseline.pop(key, None)
            self._seen.pop(key, None)

    async def _reconcile(self, store: str, game_id: str) -> None:
        """Reconcile."""
        key = f"{store}:{game_id}"
        payload: dict[str, Any] | None = None
        for attempt in range(_RECONCILE_RETRIES + 1):
            payload = await self._fetch(game_id)
            if payload is not None and payload.get("total"):
                break
            if attempt < _RECONCILE_RETRIES:
                await asyncio.sleep(_RECONCILE_RETRY_DELAY_SECONDS)
        if payload is None:
            return
        # Toast any unlocks the poll cadence missed before the game closed.
        self._emit_new_unlocks(store, game_id, payload)
        final_keys = GOGAchievements.unlocked_keys(payload)
        baseline = self._baseline.get(key, set())
        session_new = final_keys - baseline
        names = [
            a.get("name")
            for a in payload.get("achievements", [])
            if a.get("key") in session_new
        ]
        last_session = {
            "names": names,
            "unlocked": len(session_new),
            "total": payload.get("total", 0),
            "at": time.time(),
        }
        await asyncio.to_thread(
            self._state.update, store, game_id,
            unlocked_keys=final_keys, last_session=last_session,
        )

    def _emit_new_unlocks(
        self, store: str, game_id: str, payload: dict[str, Any],
    ) -> None:
        """Emit."""
        key = f"{store}:{game_id}"
        seen = self._seen.setdefault(key, set())
        for ach in payload.get("achievements", []):
            ach_key = ach.get("key")
            if ach.get("unlocked") and ach_key and ach_key not in seen:
                seen.add(ach_key)
                self._toast(store, game_id, ach)

    def _toast(
        self, store: str, game_id: str, achievement: dict[str, Any],
    ) -> None:
        """Toast."""
        try:
            from unifideck.launcher.frontend_bridge import launcher_toast
            launcher_toast(
                "toasts.achievementName",
                i18n_title_key="toasts.header.achievement",
                i18n_params={"name": achievement.get("name") or ""},
                game_title=self._game_title(store, game_id),
            )
        except Exception:
            logger.debug("[achievements.watcher] toast failed", exc_info=True)

    async def _fetch(self, game_id: str) -> dict[str, Any] | None:
        """Fetch this game's achievements, or None on auth/network failure."""
        gog = self._gog()
        if gog is None:
            return None
        try:
            result: dict[str, Any] | None = await gog.get_game_achievements(
                game_id, force=True,
            )
            return result
        except GOGAchievementsError as e:
            logger.debug("[achievements.watcher] fetch skipped (%s)", e.code)
            return None
        except Exception:
            logger.debug("[achievements.watcher] fetch error", exc_info=True)
            return None

    async def _unlocked_now(self, game_id: str) -> set[str] | None:
        """Unlocked now."""
        payload = await self._fetch(game_id)
        return (
            GOGAchievements.unlocked_keys(payload)
            if payload is not None
            else None
        )

    def _gog(self) -> Any:
        """Gog."""
        if self._registry is None:
            return None
        try:
            return self._registry.get_store(_STORE)
        except Exception:
            return None

    def _game_title(self, store: str, game_id: str) -> str:
        """Game title."""
        if self._config is not None:
            title = self._config.get(f"games.{game_id}.title")
            if title:
                return str(title)
        return str(game_id)

    def _cancel(self, key: str) -> None:
        """Cancel."""
        task = self._poll_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
