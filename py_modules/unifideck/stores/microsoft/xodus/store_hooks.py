"""stores/microsoft/xodus/store_hooks.py — the ``StoreBase`` install hooks for Xbox PC.

``MicrosoftStore`` builds one :class:`XboxPcStoreHooks` and delegates each
install hook to it in one line, so the store file stays small:

    install_game          -> install
    update_game           -> update
    uninstall_game        -> uninstall
    check_for_updates     -> check_for_updates
    install_precondition  -> precondition

``get_installed_path``, ``find_installed_exe`` and ``get_game_size`` read
the library (``pc_library.XboxPcLibrary``), which already checks the records.
Every method takes the row's ``store_game_id``.

Uninstall is built with the event bus alone, so it works with the feature
off, the components missing or the user signed out.
"""
from __future__ import annotations

import asyncio
from typing import Any

from unifideck.core.safe_delete import delete_game_prefix
from unifideck.core.types import InstallResult, Result
from unifideck.launcher.xbox_pc.dlc_records import pending_dlc

from .dlc_job import DlcJob, DlcLookup
from .ids import record_for_game
from .install_steps import XboxPcLike, XodusCliLike
from .installer import TitleLookup, XboxPcInstaller
from .progress import ProgressCallback
from .uninstaller import XboxPcUninstaller
from .updater import XboxPcUpdater
from .updates import XboxPcUpdates


class XboxPcStoreHooks:
    """Install, update, uninstall and update check of Xbox PC titles.

    ``dlc_for(game_id)`` is the owned-DLC lookup (``pc_library.OwnedDlc``);
    install and Update download the chosen DLC through :class:`DlcJob`.
    """

    def __init__(
        self, bus: Any, xbox_pc: XboxPcLike, cli: XodusCliLike, title_for: TitleLookup,
        dlc_for: DlcLookup,
    ) -> None:
        """Build the parts over the shared facade and CLI adapter."""
        self._installer = XboxPcInstaller(xbox_pc, cli, title_for)
        self._updater = XboxPcUpdater(xbox_pc, cli)
        self._uninstaller = XboxPcUninstaller(bus)
        self._updates = XboxPcUpdates(xbox_pc, cli)
        self.dlc_for = dlc_for
        self.dlc = DlcJob(xbox_pc, cli, dlc_for)

    async def precondition(self, game_id: str) -> str | None:
        """``install_precondition``: why ``game_id`` cannot be queued, or None."""
        return await self._installer.precondition(game_id)

    async def install(
        self, game_id: str, base_path: str | None, progress_cb: ProgressCallback = None,
    ) -> InstallResult:
        """``install_game``: the game, and its DLC choice written down.

        The DLC downloads in a separate Update job, so cancelling it leaves
        the game installed.
        """
        base = await self._installer.install(game_id, base_path, progress_cb)
        return await self.dlc.after_install(game_id, base)

    async def update(self, game_id: str, progress_cb: ProgressCallback = None) -> InstallResult:
        """``update_game``: the game's update when it has one, then its pending DLC.

        Update is also what Play shows while chosen DLC is missing. When
        that is the only reason, the game is not streamed again.
        """
        record = await asyncio.to_thread(record_for_game, game_id)
        if record is not None and pending_dlc(record) and not self._updates.has_base_update(game_id, record):
            return await self.dlc.run(game_id, progress_cb)
        base = await self._updater.update(game_id, progress_cb)
        if not base.success:
            return base
        return await self.dlc.run(game_id, progress_cb, base=base)

    async def uninstall(self, game_id: str, *, delete_prefix: bool = False) -> Result:
        """``uninstall_game``. Not gated by the toggle, components or sign-in.

        The prefix is named after the product id. The central uninstall flow
        deletes ``prefixes/<game_id>``, so when the two ids differ the
        product id's prefix is deleted here.
        """
        record = await asyncio.to_thread(record_for_game, game_id) if delete_prefix else None
        result = await self._uninstaller.uninstall(game_id)
        pid = str(record.get("productId") or "") if record is not None else ""
        if result.success and pid and pid != game_id:
            await asyncio.to_thread(delete_game_prefix, pid)
        return result

    async def check_for_updates(self) -> list[str]:
        """``check_for_updates``: ``store_game_id`` of every game with a newer package."""
        return await self._updates.check_installed()
