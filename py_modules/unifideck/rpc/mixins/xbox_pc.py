"""XboxPcRPCMixin — Xbox PC games through our own Xodus build.

A thin proxy to the ``XboxPc`` facade the Microsoft store owns
(``stores/microsoft/xodus/xbox_pc.py``). The status routes answer with the
status snapshot (``XboxPcStatus`` in ``src/types/api.ts``); the component
install answers ``{started, reason}`` and the sign-in start
``{pending, launch_shortcut, reason, status}``. There is no Xbox PC
sign-out route: the Microsoft sign-out signs both out. No payload has a
top-level ``success`` key, so the envelope wraps it as data instead of
lifting it.

A refused request raises ``RpcError`` with the facade's code (for example
``notice_not_acknowledged``), which the frontend's throwing ``useRPC``
surfaces. With no Microsoft store registered, the status routes answer
``available: false`` and the QAM hides the section.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from unifideck.launcher.xbox_pc.dlc_records import pending_dlc
from unifideck.launcher.xbox_pc.layout import is_xodus_store
from unifideck.rpc.errors import RpcError
from unifideck.stores.microsoft.save_status_mixin import NOT_APPLICABLE
from unifideck.stores.microsoft.xodus.dlc_choice import apply_choice as apply_dlc_choice
from unifideck.stores.microsoft.xodus.dlc_choice import view_for as dlc_view_for
from unifideck.stores.microsoft.xodus.ids import record_for_game
from unifideck.stores.microsoft.xodus.xbox_pc import XboxPcError, unavailable_status

logger = logging.getLogger(__name__)


class XboxPcRPCMixin:
    """Xbox PC toggle, components, sign-in and status."""

    registry: Any
    sync_service: Any
    #: ``DownloadRPCMixin._queue_install``, on the same plugin.
    _queue_install: Any
    #: ``DownloadRPCMixin._request_update_scan``, on the same plugin.
    _request_update_scan: Any
    #: ``DownloadRPCMixin._require_download``, on the same plugin.
    _require_download: Any

    def _xbox_pc(self) -> Any:
        """The facade, or None when the Microsoft store is missing."""
        store = self.registry.get("microsoft")
        return getattr(store, "xbox_pc", None) if store is not None else None

    def _require_xbox_pc(self) -> Any:
        facade = self._xbox_pc()
        if facade is None:
            raise RpcError("xbox_pc_unavailable")
        return facade

    async def get_xbox_pc_status(self) -> dict[str, Any]:
        """The cached status."""
        facade = self._xbox_pc()
        return await facade.status() if facade is not None else unavailable_status()

    async def refresh_xbox_pc_status(self) -> dict[str, Any]:
        """Probe the components and ask Xodus about the account again."""
        facade = self._xbox_pc()
        return await facade.refresh_status() if facade is not None else unavailable_status()

    async def set_xbox_pc_enabled(
        self, enabled: bool, notice_version: int | None = None,
    ) -> dict[str, Any]:
        """Turn the feature on (after the notice) or off (deletes nothing)."""
        facade = self._require_xbox_pc()
        try:
            status: dict[str, Any] = await facade.set_enabled(bool(enabled), notice_version)
        except XboxPcError as e:
            raise RpcError(e.code) from None
        return status

    async def install_xbox_pc_runtime(self) -> dict[str, Any]:
        """Start installing the backend and runtime: ``{started, reason}``.

        Progress and the end arrive as ``XBOX_PC_STATE_CHANGED``.
        """
        result: dict[str, Any] = await self._require_xbox_pc().install_components()
        return result

    async def start_xbox_pc_sign_in(self) -> dict[str, Any]:
        """Start the Xodus sign-in: ``{pending, launch_shortcut, reason, status}``.

        On ``pending`` the frontend launches the ``launch_shortcut`` auth
        shortcut; if that launch fails it calls ``cancel_xbox_pc_sign_in``.
        """
        result: dict[str, Any] = await self._require_xbox_pc().start_sign_in()
        if not result.get("pending"):
            logger.info("[XboxPcRPC] sign-in refused: %s", result.get("reason"))
        return result

    async def cancel_xbox_pc_sign_in(self) -> dict[str, Any]:
        """Stop a running sign-in (no failure is recorded)."""
        status: dict[str, Any] = await self._require_xbox_pc().cancel_sign_in()
        return status

    async def repair_xbox_pc_game(self, game_id: str) -> dict[str, Any]:
        """Queue a repair of an installed game: ``{success, error}``.

        Play on a game whose files are missing (the launcher's
        ``not_downloaded``) calls this. It installs into the folder the game
        already has, which the installer runs as a repair or a resume.
        """
        target: dict[str, Any] = await self._require_xbox_pc().repair_target(str(game_id))
        if "reason" in target:
            logger.info("[XboxPcRPC] repair of %s refused: %s", game_id, target["reason"])
            return {"success": False, "error": target["reason"]}
        logger.info("[XboxPcRPC] repairing %s in %s", game_id, target["base_path"])
        result: dict[str, Any] = await self._queue_install(
            "microsoft", str(game_id), target["base_path"], target["title"],
        )
        return result

    def _xbox_hooks(self) -> Any:
        """The Xbox PC install hooks, or None when the Microsoft store is missing."""
        store = self.registry.get("microsoft")
        return getattr(store, "xbox_hooks", None) if store is not None else None

    async def get_xbox_pc_dlc(self, game_id: str, force: bool = False) -> dict[str, Any]:
        """The game page's DLC list: ``{applicable, installed, dlc: [...]}``.

        Loaded with the game's details, never from a library sync. Each
        entry is ``{product_id, parent, title, size_bytes, large, state,
        chosen}``. ``applicable`` is False for a game that is not Xbox PC or
        whose account entitlements cannot be read; the page then shows no
        DLC button. ``force`` reads the entitlements and catalog again.
        """
        hooks = self._xbox_hooks()
        owned = await hooks.dlc_for(str(game_id), force=bool(force)) if hooks is not None else None
        if owned is None:
            return {"applicable": False, "installed": False, "dlc": []}
        view: dict[str, Any] = await dlc_view_for(str(game_id), owned)
        return {"applicable": True, **view}

    async def set_xbox_pc_dlc(self, game_id: str, selected: list[str] | None = None) -> dict[str, Any]:
        """Tick DLC on the game page: ``{success, error?, installed}``.

        ``selected`` is every product id that should be installed. Nothing
        downloads here: on an installed game a newly ticked DLC makes Play
        show Update (the update scan is asked at once), and an unticked one
        is removed. Before install it is the large-DLC choice of the picker.
        """
        hooks = self._xbox_hooks()
        owned = await hooks.dlc_for(str(game_id)) if hooks is not None else None
        if owned is None:
            return {"success": False, "error": "xbox_dlc_unknown", "installed": False}
        result: dict[str, Any] = await apply_dlc_choice(str(game_id), owned, list(selected or []))
        if result.get("success") and result.get("installed"):
            self._request_update_scan("microsoft")
        return result

    async def install_xbox_pc_dlc(self, game_id: str) -> dict[str, Any]:
        """Queue the Update job that downloads a game's pending DLC: ``{success, error}``.

        A start that bypassed the game page's Update button refuses in the
        launcher with ``dlc_pending``; its toast carries the
        ``install-xbox-pc-dlc`` action, and the launcher-toast poll calls this
        at once, so the download starts with no extra tap.
        """
        record = await asyncio.to_thread(record_for_game, str(game_id))
        if record is None or not pending_dlc(record):
            return {"success": False, "error": "xbox_dlc_nothing_pending"}
        logger.info("[XboxPcRPC] queueing the pending DLC of %s", game_id)
        result = await self._require_download().add(
            store="microsoft", game_id=str(game_id),
            install_path=str(record.get("installRoot") or ""),
            title=str(record.get("title") or ""), is_update=True,
        )
        return {"success": result.success, "error": result.error}

    async def get_xbox_pc_save_status(self, game_id: str, force: bool = False) -> dict[str, Any]:
        """Compare an installed game's saves with the cloud. Read-only.

        ``{applicable, state, waiting, checked_at}``. ``applicable`` is False
        for anything but an installed Xbox PC game, so a stream-only title
        shows no line. The runtime does all syncing: nothing here pushes.
        """
        store = self.registry.get("microsoft")
        if store is None:
            return dict(NOT_APPLICABLE)
        status: dict[str, Any] = await store.get_xbox_save_status(str(game_id), bool(force))
        return status

    async def get_uninstall_save_warning(self, app_id: int) -> dict[str, int]:
        """``{unuploaded}``: saves that deleting this game's prefix would lose.

        Reads local files only, so the uninstall dialog never waits on the
        network. Every store but Xbox PC answers 0: their saves are not synced
        by a runtime that keeps them in the prefix until its next launch.
        """
        info = self.sync_service.get_game_info(app_id) if self.sync_service else None
        if not info or not is_xodus_store(info.get("store", "")):
            return {"unuploaded": 0}
        store = self.registry.get(info["store"])
        if store is None:
            return {"unuploaded": 0}
        count: int = await store.count_unuploaded_saves(str(info.get("store_game_id", "")))
        return {"unuploaded": count}
