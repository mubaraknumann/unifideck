"""
Build the auth-context dict the frontend renders.

When the user clicks "Sign in to Ubisoft Connect" in the QAM panel,
the frontend calls an RPC that returns an "auth context" dict
containing:

* the appid of the Steam shortcut to launch (so the frontend can call
  ``SteamClient.Apps.RunGame(appid)``);
* the canonical UPC shortcut store_id (for the icon);
* a friendly name ("Ubisoft Connect") for the toast;
* a delay (``launch_wait_ms``) the frontend should respect before
  starting to monitor the auth state (gives UPC time to spawn).

``_AuthContext`` builds this dict, optionally fetching the SteamGridDB
artwork in the background if not already cached.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .facade import UbisoftAuth
logger = logging.getLogger(__name__)
_SGDB_UBISOFT_CONNECT_ID = 5270094
_AUTH_SHORTCUT_NAME = "Ubisoft Connect"

class _AuthContext:
    """Auth context."""

    def __init__(self, parent: UbisoftAuth) -> None:
        """Initialize the instance."""
        self._parent = parent

    async def fetch_auth_shortcut_artwork(
        self,
        unsigned_id: int,
        force: bool = False,
    ) -> None:
        """Fetch auth shortcut artwork."""
        sgdb = self._parent._steamgriddb
        if sgdb is None:
            logger.debug(
                "[UbisoftAuth] SteamGridDB client not available, skipping artwork",
            )
            return
        try:
            if (
                not force
                and hasattr(sgdb, "has_artwork")
                and await sgdb.has_artwork(unsigned_id)
            ):
                logger.debug(
                    "[UbisoftAuth] auth shortcut artwork already exists",
                )
                return
            only_types = None
            if not force and hasattr(
                sgdb,
                "get_missing_artwork_types",
            ):
                missing = await sgdb.get_missing_artwork_types(
                    unsigned_id,
                )
                if missing:
                    only_types = missing
                    logger.info(
                        "[UbisoftAuth] auth shortcut artwork gap-fill: %s",
                        missing,
                    )
            logger.info(
                "[UbisoftAuth] fetching SteamGridDB "
                "artwork for Ubisoft Connect (force=%s)",
                force,
            )
            await sgdb.fetch_game_art(  # type: ignore[attr-defined]  # fetch_game_art added by dependency injection in tests/fixtures
                title=_AUTH_SHORTCUT_NAME,
                app_id=unsigned_id,
                only_types=only_types,
                sgdb_game_id=_SGDB_UBISOFT_CONNECT_ID,
            )
        except Exception as e:
            logger.warning(
                "[UbisoftAuth] auth shortcut artwork fetch failed: %s",
                e,
            )

    def build_auth_context_success(
        self,
        unsigned_appid: int,
        *,
        with_launch_wait: bool = True,
    ) -> dict[str, Any]:
        """Build auth context success.

        ``launcher_path`` lets the frontend fall back to a temporary
        shortcut (created via ``AddShortcut``) when the persistent auth
        shortcut isn't yet in Steam's in-memory app store — the first
        session after it's written to ``shortcuts.vdf``, which Steam only
        loads at startup. Without that fallback ``RunGame`` on the
        unregistered shortcut fails with "Game configuration unavailable".
        """
        return {
            "success": True,
            "appid_unsigned": unsigned_appid,
            "launcher_path": self._parent._shortcut.get_launcher_path(),
            "launch_wait_ms": (
                self._parent._config.auth_shortcut_launch_wait_ms
                if with_launch_wait
                else 0
            ),
        }

    async def get_auth_shortcut_context(self) -> dict[str, Any]:
        """Get auth shortcut context."""
        if self._parent._shortcut_service is None:
            return {
                "success": False,
                "error": "shortcut_service_unavailable",
            }
        sm = self._parent._shortcut_service
        from_registry = await self._try_existing_registry(sm)
        if from_registry is not None:
            return from_registry
        logger.info(
            "[UbisoftAuth] auth shortcut not in registry, scanning VDF for recovery",
        )
        vdf_found = await self._parent._shortcut.validate_auth_shortcut(sm)
        if vdf_found:
            store_id = self._parent._config.auth_shortcut_store_id
            registry = await self._parent._load_registry(sm)
            entry = registry.get(store_id)
            if entry and entry.get("appid_unsigned"):
                logger.info(
                    "[UbisoftAuth] auth shortcut recovered from VDF: appid=%d",
                    entry["appid_unsigned"],
                )
                return self.build_auth_context_success(
                    entry["appid_unsigned"],
                )
        unsigned_id = await self._parent.ensure_auth_shortcut()
        if not unsigned_id:
            return {
                "success": False,
                "error": "auth_shortcut_not_ready",
            }
        return self.build_auth_context_success(unsigned_id)

    async def _try_existing_registry(
        self,
        sm: Any,
    ) -> dict[str, Any] | None:
        """Try existing registry."""
        store_id = self._parent._config.auth_shortcut_store_id
        registry = await self._parent._load_registry(sm)
        entry = registry.get(store_id)
        if not entry or not entry.get("appid_unsigned"):
            return None
        if await self._parent.auth_shortcut_exists_in_vdf():
            logger.info(
                "[UbisoftAuth] auth shortcut context: appid=%d",
                entry["appid_unsigned"],
            )
            return self.build_auth_context_success(
                entry["appid_unsigned"],
                with_launch_wait=False,
            )
        logger.info(
            "[UbisoftAuth] auth shortcut in registry but missing from VDF, recreating",
        )
        unsigned_id = await self._parent.ensure_auth_shortcut()
        if unsigned_id:
            return self.build_auth_context_success(unsigned_id)
        return None
