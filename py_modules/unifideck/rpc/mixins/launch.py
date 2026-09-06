"""Launch RPC mixin for Plugin class.
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.core.types.events import Events
from unifideck.rpc.errors import RpcError

logger = logging.getLogger(__name__)

_MAX_SAVE_FILES = 500

class LaunchRPCMixin:
    """Game launch notifications, circuit breaker, launch logs, save folders."""

    bus: Any
    config: Any
    services: Any

    def _require_launch_history(self) -> Any:
        """Return LaunchHistoryService or raise ``service_unavailable``."""
        svc = getattr(self.services, "launch_history", None)
        if svc is None:
            raise RpcError("service_unavailable", service="launch_history")
        return svc

    async def notify_game_launched(
        self,
        app_id: int | None = None,
        store: str | None = None,
        game_id: str | None = None,
        **kw: Any,
    ) -> Any:
        """Bridge a frontend-initiated launch onto the bus.

        Two call signatures accepted :

        1. ``notify_game_launched(app_id)`` — what the
           frontend bootstrap subscriber sends after
           Steam's ``RegisterForAppLifetimeNotifications``
           fires (the only info Steam gives us is the
           AppID). The mixin resolves ``store`` / ``game_id``
           internally via the sync-service lookup ; if the
           AppID isn't a Unifideck shortcut, it's a no-op.

        2. ``notify_game_launched(store=…, game_id=…)`` —
           explicit callers that already know the pair.

        Emitting ``GAME_LAUNCHED`` with partial info would
        confuse downstream subscribers (playtime, cloud
        save), so missing args yield a typed skip response
        rather than a crash.

        Returns:
            ``{success: True}`` on emit, or
            ``{success: True, skipped: <reason>}`` when
            the AppID isn't a Unifideck shortcut.
        """
        resolved_store, resolved_game, resolved_title = self._resolve_app_id(
            app_id, store, game_id,
        )
        if resolved_store is None or resolved_game is None:
            logger.debug(
                "notify_game_launched skip: app_id=%s not a unifideck shortcut",
                app_id,
            )
            return {"success": True, "skipped": "not_unifideck_app"}
        kw.setdefault("title", resolved_title or "")
        await self.bus.emit(
            Events.GAME_LAUNCHED,
            store=resolved_store,
            game_id=resolved_game,
            app_id=app_id,
            **kw,
        )
        return {"success": True}

    async def notify_game_stopped(
        self,
        app_id: int | None = None,
        store: str | None = None,
        game_id: str | None = None,
        exit_code: int = 0,
    ) -> Any:
        """Bridge a frontend-detected game exit onto the bus.

        Counterpart to ``notify_game_launched`` — accepts
        the same two-signature contract.
        """
        resolved_store, resolved_game, _ = self._resolve_app_id(
            app_id, store, game_id,
        )
        if resolved_store is None or resolved_game is None:
            logger.debug(
                "notify_game_stopped skip: app_id=%s not a unifideck shortcut",
                app_id,
            )
            return {"success": True, "skipped": "not_unifideck_app"}
        await self.bus.emit(
            Events.GAME_STOPPED,
            store=resolved_store,
            game_id=resolved_game,
            app_id=app_id,
            exit_code=exit_code,
        )
        return {"success": True}

    def _resolve_app_id(
        self,
        app_id: int | None,
        store: str | None,
        game_id: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve ``(store, game_id, title)`` from any of the inputs.

        Explicit ``(store, game_id)`` always wins ; otherwise
        we ask the sync service to find the game whose
        AppID matches. Returns ``(None, None, None)`` if the
        AppID doesn't belong to a known Unifideck shortcut —
        the caller treats that as a quiet no-op.

        ``title`` is best-effort (``None`` for explicit callers
        that don't supply one) so the playtime/launch subscribers
        can record a human-readable name instead of an empty string.
        """
        if store and game_id:
            return store, game_id, None
        if app_id is None:
            return None, None, None
        sync = getattr(self, "sync_service", None)
        if sync is None or not hasattr(sync, "get_game_info"):
            return None, None, None
        try:
            info = sync.get_game_info(app_id)
        except Exception:
            return None, None, None
        if not isinstance(info, dict):
            return None, None, None
        # The sync layer returns ``asdict(Game)``, whose store-native
        # id field is ``store_game_id`` (there is no ``id``/``game_id``
        # key). Keep the legacy fallbacks for any non-Game dict callers.
        resolved_game = (
            info.get("store_game_id")
            or info.get("id")
            or info.get("game_id")
        )
        return info.get("store"), resolved_game, info.get("title")

    async def get_launch_failures(self, game_key: str) -> Any:
        """Return recent failures + circuit state for a game.

        Bundles two service methods (``get_recent_failures`` and
        ``is_circuit_open``) into one RPC payload — neither exists
        as ``get_failures`` on :class:`LaunchHistoryService`, so the
        previous version raised ``AttributeError``.
        """
        svc = self._require_launch_history()
        is_open, fail_count = svc.is_circuit_open(game_key)
        return {
            "failures": svc.get_recent_failures(game_key),
            "circuit_open": is_open,
            "fail_count": fail_count,
        }

    async def clear_launch_failures(self, game_key: str) -> Any:
        """Wipe failure history for one game (full reset)."""
        return self._require_launch_history().clear_failures(game_key)

    async def arm_circuit_bypass(self, game_key: str) -> Any:
        """Arm a one-shot bypass flag (5-minute validity)."""
        return self._require_launch_history().arm_bypass(game_key)

    # no-frontend-caller: audit register 4b — reached only by the
    # show-logs toast action, which no frontend renders. LaunchLogsModal
    # exists as translated strings in 16 locales but not as a component.
    # Delete this with the dead verb, or build the modal.
    async def get_launch_logs(
        self, launch_id: str, max_lines: int = 500,
    ) -> Any:
        """Tail the log file for a specific launch id."""
        svc = getattr(self.services, "launch_logs", None)
        if svc is None:
            raise RpcError("service_unavailable", service="launch_logs")
        return await svc.read(launch_id, max_lines=max_lines)

    # no-frontend-caller: audit register 4b — reached only by the
    # open-save-folder toast action; SaveFolderModal likewise exists only
    # as locale strings. Same decision as get_launch_logs.
    async def list_save_folder(
        self,
        store: str,
        game_id: str,
        max_depth: int = 2,
        filter_substring: str = "",
    ) -> Any:
        """Return contents of a game's local cloud save folder."""
        cloudsave = getattr(self.services, "cloudsave", None)
        if cloudsave is None:
            raise RpcError("service_unavailable", service="cloudsave")
        entries = await cloudsave.list_save_folder(
            store, game_id, max_depth=max_depth,
        )
        if filter_substring:
            entries = [e for e in entries if filter_substring in e.get("name", "")]
        entries.sort(key=lambda e: e.get("size", 0), reverse=True)
        truncated = len(entries) > _MAX_SAVE_FILES
        return {
            "files": entries[:_MAX_SAVE_FILES],
            "truncated": truncated,
        }
