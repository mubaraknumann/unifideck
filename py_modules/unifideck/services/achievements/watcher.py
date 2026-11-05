"""services/achievements/watcher.py — live + post-play achievement tracking.

Games unlock achievements themselves and the store's servers record them in
near real time: GOG through Comet (``launcher/proton/compat/gog.py``), Epic
through EOS, Xbox through the runtime's Xbox Live session. This watcher reads
them back so the user gets feedback a Linux game often can't give (Comet's
overlay is disabled on Linux; Xbox games have no overlay under Wine):

* **During play** — polls while the game runs and fires a Steam-style toast
  for each newly-unlocked achievement (via the launcher ``frontend_bridge``,
  the panel-independent toast path).
* **At game-stop** — a final reconcile and a per-game ``last_session``
  summary the game-info panel shows.
* **GOG push** — comet POSTs each unlock the moment it happens (online or
  offline) to :class:`CometEventReceiver`; it toasts at once through the
  same ``_seen`` dedup, so the poll never repeats it.

Covers every store in ``ACHIEVEMENT_STORES``. Driven by the plugin-bus
``GAME_LAUNCHED`` / ``GAME_STOPPED`` events.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from unifideck.core.store_capabilities import ACHIEVEMENT_STORES
from unifideck.core.types import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe
from unifideck.launcher.game_title import resolve_title
from unifideck.stores.gog.achievements import normalize_gog_achievement
from unifideck.stores.gog.galaxy_api import fetch_gog_client_creds
from unifideck.stores.shared.achievements_error import StoreAchievementsError
from unifideck.stores.shared.achievements_payload import unlocked_keys

from .comet_events import CometEventReceiver
from .state import AchievementStateStore

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 60.0
#: Per-store poll overrides. Xbox Live is a service we have not polled
#: before, so it gets a gentler cadence.
_POLL_INTERVALS = {"microsoft": 90.0}
_RECONCILE_RETRIES = 2
_RECONCILE_RETRY_DELAY_SECONDS = 5.0

# Strong refs to fire-and-forget reconcile tasks so the GC can't collect them
# mid-flight (RUF006); the done-callback discards each when it settles.
_BACKGROUND: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> None:
    """Track."""
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _game_title(store: str, game_id: str) -> str:
    """The game's shortcut title, or "" so the toast falls back to the store name."""
    key = f"{store}:{game_id}"
    title = resolve_title(key)
    return "" if title == key else title


class AchievementWatcher:
    """Live unlock toasts + an end-of-session summary for every achievement store."""

    def __init__(
        self,
        bus: EventBus,
        registry: StoreRegistry | None,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._registry = registry
        self._state = AchievementStateStore()
        self._poll_tasks: dict[str, asyncio.Task[Any]] = {}
        self._baseline: dict[str, set[str]] = {}
        self._seen: dict[str, set[str]] = {}
        # GOG Galaxy client id per running GOG session, resolved on demand
        # to route a comet push when several GOG games run at once.
        self._client_ids: dict[str, str | None] = {}
        self._comet: CometEventReceiver | None = None
        auto_wire(self, bus)

    async def start(self) -> None:
        """Start comet's push receiver; the poll covers GOG if it can't."""
        receiver = CometEventReceiver(self._on_comet_unlock)
        try:
            await receiver.start()
        except Exception as e:
            logger.warning(
                "[achievements.watcher] comet push receiver failed to start "
                "(%s): GOG achievement toasts fall back to the %ss poll",
                e, _POLL_INTERVAL_SECONDS,
            )
            return
        self._comet = receiver

    @subscribe(Events.GAME_LAUNCHED)
    async def _on_game_launched(self, **kwargs: Any) -> None:
        """Snapshot the baseline unlocks and start the during-play poll."""
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")
        if store not in ACHIEVEMENT_STORES or not game_id:
            return
        key = f"{store}:{game_id}"
        baseline = await self._unlocked_now(store, game_id)
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
        if store not in ACHIEVEMENT_STORES or not game_id:
            return
        self._cancel(f"{store}:{game_id}")
        self._client_ids.pop(f"{store}:{game_id}", None)
        inst = self._store(store)
        if inst is not None:
            # Drop the cache so the reconcile pull is fresh.
            inst.invalidate_achievements(game_id)
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
        if self._comet is not None:
            comet, self._comet = self._comet, None
            await comet.stop()

    async def _on_comet_unlock(self, client_id: str, data: dict[str, Any]) -> None:
        """Toast a comet-pushed GOG unlock for the running game it belongs to."""
        game_id = await self._gog_session_for(client_id)
        if game_id is None:
            logger.debug(
                "[achievements.watcher] comet unlock for client %s matched no "
                "running GOG game; the poll will catch it", client_id,
            )
            return
        self._emit_new_unlocks(
            "gog", game_id, {"achievements": [normalize_gog_achievement(data)]},
        )

    async def _gog_session_for(self, client_id: str) -> str | None:
        """The running GOG game id a comet ``client_id`` belongs to, or None.

        One running GOG game (the normal case) is the answer outright; with
        several, each one's Galaxy client id is looked up once and matched.
        """
        game_ids = [k[len("gog:"):] for k in self._poll_tasks if k.startswith("gog:")]
        if len(game_ids) <= 1:
            return game_ids[0] if game_ids else None
        for game_id in game_ids:
            key = f"gog:{game_id}"
            if key not in self._client_ids:
                try:
                    cid, _ = await asyncio.to_thread(fetch_gog_client_creds, game_id)
                except Exception:
                    cid = None
                self._client_ids[key] = cid
            if client_id and self._client_ids[key] == client_id:
                return game_id
        return None

    # -- internals ---------------------------------------------------------

    async def _poll_loop(self, store: str, game_id: str) -> None:
        """Poll."""
        key = f"{store}:{game_id}"
        interval = _POLL_INTERVALS.get(store, _POLL_INTERVAL_SECONDS)
        try:
            while True:
                await asyncio.sleep(interval)
                payload = await self._fetch(store, game_id)
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
            payload = await self._fetch(store, game_id)
            if payload is not None and payload.get("total"):
                break
            if attempt < _RECONCILE_RETRIES:
                await asyncio.sleep(_RECONCILE_RETRY_DELAY_SECONDS)
        if payload is None:
            return
        # Toast any unlocks the poll cadence missed before the game closed.
        self._emit_new_unlocks(store, game_id, payload)
        final_keys = unlocked_keys(payload)
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
                game_title=_game_title(store, game_id),
                # The frontend draws this as Steam's achievement popup:
                # image, name, then the game and the store with its logo.
                achievement={
                    "name": achievement.get("name") or "",
                    "icon": achievement.get("image_unlocked") or "",
                    "store": store,
                    # Share of players, for Steam's rare glow on the image.
                    "rarity": achievement.get("rarity"),
                },
            )
        except Exception:
            logger.debug("[achievements.watcher] toast failed", exc_info=True)

    async def _fetch(self, store: str, game_id: str) -> dict[str, Any] | None:
        """Fetch this game's achievements, or None on auth/network failure."""
        inst = self._store(store)
        if inst is None:
            return None
        try:
            result: dict[str, Any] | None = await inst.get_game_achievements(
                game_id, force=True,
            )
            return result
        except StoreAchievementsError as e:
            logger.debug("[achievements.watcher] fetch skipped (%s)", e.code)
            return None
        except Exception:
            logger.debug("[achievements.watcher] fetch error", exc_info=True)
            return None

    async def _unlocked_now(self, store: str, game_id: str) -> set[str] | None:
        """The keys unlocked right now, or None when unreadable."""
        payload = await self._fetch(store, game_id)
        return unlocked_keys(payload) if payload is not None else None

    def _store(self, store: str) -> Any:
        """The registered store instance, or None."""
        if self._registry is None:
            return None
        try:
            return self._registry.get_store(store)
        except Exception:
            return None

    def _cancel(self, key: str) -> None:
        """Cancel."""
        task = self._poll_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
