"""Install-lifecycle methods shared by stores built on ``ArchiveInstaller``."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events, InstallResult, Result, StoreInfo

from .archive_install import ArchiveInstaller
from .archive_source import ProgressCallback


class ArchiveStoreMixin:
    """Mix in before ``StoreBase``; the store supplies ``_installer``.

    Everything here is the part of a store that does not depend on where the
    archives come from: install, guarded uninstall, install-state lookups.
    """

    _installer: ArchiveInstaller
    store_info: StoreInfo

    if TYPE_CHECKING:
        async def _emit(self, event: Events, **kwargs: Any) -> None: ...

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: ProgressCallback | None = None,
        **kwargs: Any,
    ) -> InstallResult:
        """Install through the shared pipeline, from this store's source.

        ``base_path`` (or the ``install_path`` alias the install RPC uses)
        chooses the root. The shared storage picker supplies one for every
        install, so the store's configured default is the fallback.
        """
        return await self._installer.install_game(
            game_id,
            install_path=base_path or kwargs.get("install_path"),
            progress_callback=progress_cb or kwargs.get("progress_callback"),
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Remove the game, then announce it like every other store does.

        ``GAME_UNINSTALLED`` is what ``ShortcutService`` subscribes to in
        order to flip the shortcut back to "not installed" while keeping it
        (and its appid, artwork and playtime) in place. Emitting it here —
        rather than having the uninstall RPC call ``mark_uninstalled``
        directly — keeps archive stores on the same path as GOG, Epic, Amazon,
        Ubisoft and Battle.net, instead of adding a second mechanism that
        would fire twice for all of them.
        """
        result = await self._installer.uninstall_game(game_id)
        if result.success:
            await self._emit(
                Events.GAME_UNINSTALLED, store=self.store_info.name, game_id=game_id,
            )
        return result

    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Re-install the game; archive stores have no delta updates."""
        return await self.install_game(game_id, **kwargs)

    async def check_for_updates(self) -> list[str]:
        """Archive stores have no update API; a re-install is the update path."""
        return []

    async def get_installed_path(self, game_id: str) -> str | None:
        """Install dir per our own record, or ``None`` if not installed.

        The hook every store implements. It matters more here than
        elsewhere: it is what lets Change Executable resolve a directory when
        the games.map row is missing, which for this store is the situation
        the picker exists for.
        """
        info = self._installer.get_install_info(game_id)
        path = (info or {}).get("install_path")
        return path if isinstance(path, str) and path else None

    async def get_game_size(self, game_id: str) -> int | None:
        """Download/extract size in bytes, or None when unknown."""
        return await self._installer.get_game_size(game_id)

    # ── Extra helpers over the install records ──

    def _get_install_info(self, game_id: str) -> dict[str, Any] | None:
        """The persisted install record for *game_id*, verbatim."""
        return self._installer.get_install_info(game_id)

    async def get_installed(self) -> dict[str, dict[str, Any]]:
        """``{game_id: record}`` for every installed game in this store."""
        return self._installer.get_installed()
