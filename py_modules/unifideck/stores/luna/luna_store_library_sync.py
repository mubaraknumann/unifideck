"""Library retrieval for the Luna store (Req 4/13).

The ``get_library`` flow: check the browser and the session, resolve the
region constants for the configured Luna host, read the account's titles
through the Luna web API (:class:`~unifideck.stores.luna.luna_api.LunaApiClient`)
and reconcile them into the authoritative ``Game`` list. No browser is opened
for this. Split out of ``luna_store.py`` as a mixin that
:class:`~unifideck.stores.luna.luna_store.LunaStore` composes.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.core.types import Events
from unifideck.stores.luna.luna_api import (
    LunaApiClient,
    LunaSessionError,
    resolve_region,
)
from unifideck.stores.luna.luna_catalog import LunaCatalog
from unifideck.stores.luna.luna_profile import resolve_profile_dir
from unifideck.stores.luna.luna_store_base import _LunaStoreState
from unifideck.stores.luna.luna_store_constants import (
    LUNA_STORE,
    LunaOperationError,
    LunaStatus,
)

if TYPE_CHECKING:
    from unifideck.core.types import Game

logger = logging.getLogger(__name__)


class _LunaLibrarySyncMixin(_LunaStoreState):
    """``get_library`` over the Luna web API."""

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """The account's Luna titles, or ``None`` when they cannot be read.

        A ``list[Game]`` (including the empty list) is authoritative — the
        sync layer replaces every Luna shortcut with it — so every "could not
        read" path returns ``None`` to keep the existing shortcuts, exactly as
        ``MicrosoftStore.get_library`` does.

        * browser missing / no usable session → ``None``;
        * host without region constants → ``[]`` + ``region_unavailable``;
        * session rejected by the API → ``None`` + ``session_expired``;
        * transport/HTTP/shape error after every retry → ``None`` +
          ``sync_error``;
        * otherwise the reconciled titles + ``connected``.
        """
        if not self._edge.is_installed:
            logger.info("[LunaStore] get_library: browser not installed")
            return None
        if not self._session.is_usable():
            logger.info(
                "[LunaStore] get_library: no usable Luna session; "
                "library not read",
            )
            return None
        catalog = self._catalog_for_region()
        if catalog is None:
            return await self._region_unavailable()
        try:
            games: list[Game] | None = await self._with_retry(
                "get_library", lambda: self._sync_once(catalog),
            )
        except LunaOperationError:
            # Every attempt failed and the retry helper logged each one.
            # "Could not read" keeps the existing Luna shortcuts (Req 4.2/4.6).
            await self._publish_status(LunaStatus.SYNC_ERROR)
            return None
        return games

    def _catalog_for_region(self) -> LunaCatalog | None:
        """The catalog bound to the configured host's region, or ``None``."""
        if self._catalog is not None:
            return self._catalog
        region = resolve_region(self._luna_config.luna_host)
        if region is None:
            return None
        client = LunaApiClient(
            region, resolve_profile_dir(self._luna_config.profile_dir),
            locale=self._luna_config.locale,
        )
        return LunaCatalog(client.fetch_library)

    async def _region_unavailable(self) -> list[Game]:
        """No region constants for this host: authoritative empty library."""
        logger.info(
            "[LunaStore] no Luna region for host %r — excluding all titles",
            self._luna_config.luna_host,
        )
        await self._publish_status(LunaStatus.REGION_UNAVAILABLE)
        return []

    async def _sync_once(self, catalog: LunaCatalog) -> list[Game] | None:
        """One catalog-sync attempt.

        A rejected session is terminal (``None``, not retried); every other
        failure raises so :meth:`_with_retry` retries it.
        """
        try:
            result = await catalog.sync(self._previous_luna_entries())
        except LunaSessionError as exc:
            logger.info("[LunaStore] Luna session rejected (%s)", exc.reason)
            self._last_stable_status = LunaStatus.SESSION_EXPIRED
            return None
        self._last_stable_status = LunaStatus.CONNECTED
        logger.info(
            "[LunaStore] catalog sync %s -> %d title(s)",
            result.status.value, len(result.games),
        )
        return result.games

    async def _publish_status(self, status: str) -> None:
        """Record ``status`` and surface it on the frontend status channel."""
        self._last_stable_status = status
        await self._bus.emit(Events.SYNC_SKIPPED, store=LUNA_STORE, reason=status)

    def _previous_luna_entries(self) -> list[dict[str, object]]:
        """The Luna library entries from the last sync (for Req 4.7 preserve).

        Read from ``library_cache.json`` — the file the launcher also reads —
        so a malformed title this run does not drop a pre-existing entry. A
        missing/unreadable cache yields an empty list (nothing to preserve).
        """
        import json
        from pathlib import Path

        from unifideck.launcher.browser_games import LIBRARY_CACHE

        cache = Path(LIBRARY_CACHE).expanduser()
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        libraries = data.get("libraries") if isinstance(data, dict) else None
        entries = (libraries or {}).get(LUNA_STORE) if libraries else None
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]
