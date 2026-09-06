"""
Shortcut registry operations — read/write entries for the auth shortcut.

Thin abstraction layer over ``services.shortcut.ShortcutService`` for
the operations the auth flow needs: ``register_auth_shortcut``,
``locate_auth_shortcut_appid``, ``remove_stale_auth_shortcut``. Keeps
the auth facade ignorant of the underlying shortcut-service API surface.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.services.shortcut import ShortcutService
    from unifideck.stores.ubisoft.config import UbisoftConfig
_LEGACY_AUTH_SHORTCUT_STORE_ID = "ubisoft:.template"
logger = logging.getLogger(__name__)

class _ShortcutRegistryOps:
    """Shortcut registry ops."""

    def __init__(self, *, config: UbisoftConfig) -> None:
        """Initialize the instance."""
        self._config = config

    async def load(self, sm: ShortcutService) -> dict[str, Any]:
        """Load."""
        try:
            if hasattr(sm, "load_shortcuts_registry"):
                result = sm.load_shortcuts_registry()
                if asyncio.iscoroutine(result):
                    result = await result
                    if isinstance(result, dict):
                        return result
        except Exception as e:
            logger.debug(
                "[UbisoftAuth] registry load failed: %s",
                e,
            )
        return {}

    async def register(
        self,
        sm: ShortcutService,
        appid: int,
        name: str,
    ) -> None:
        """Register."""
        try:
            if hasattr(sm, "register_shortcut"):
                result = sm.register_shortcut(
                    self._config.auth_shortcut_store_id,
                    appid,
                    name,
                )
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:
            logger.debug(
                "[UbisoftAuth] shortcut register failed: %s",
                e,
            )

    async def clear_compat(
        self,
        sm: ShortcutService,
        appid: int,
    ) -> None:
        """Clear compat."""
        try:
            if hasattr(sm, "_clear_proton_compatibility"):
                result = sm._clear_proton_compatibility(appid)
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:
            logger.debug(
                "[UbisoftAuth] clear compat failed: %s",
                e,
            )

    async def cleanup_legacy(self, sm: ShortcutService) -> None:
        """Cleanup legacy."""
        try:
            if not hasattr(sm, "load_shortcuts_registry"):
                return
            registry = sm.load_shortcuts_registry()
            if asyncio.iscoroutine(registry):
                registry = await registry
            if (
                not isinstance(registry, dict)
                or _LEGACY_AUTH_SHORTCUT_STORE_ID not in registry
            ):
                return
            del registry[_LEGACY_AUTH_SHORTCUT_STORE_ID]
            if hasattr(sm, "save_shortcuts_registry"):
                result = sm.save_shortcuts_registry(registry)
                if asyncio.iscoroutine(result):
                    await result
                logger.info(
                    "[UbisoftAuth] removed legacy .template from shortcuts registry",
                )
        except Exception as e:
            logger.debug(
                "[UbisoftAuth] legacy registry cleanup failed: %s",
                e,
            )
