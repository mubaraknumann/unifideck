"""Catalog writer + library-cache reconciliation (Req 4).

:class:`LunaCatalog` is the seam the store's ``get_library`` calls. It asks the
injected ``fetch_library`` callable (the Luna web API client) for the
account's titles, builds one browser-game :class:`Game` per valid title, and
reconciles that against the Luna entries already in ``library_cache.json``.

The return contract matches ``SyncService`` reconciliation (and mirrors
``MicrosoftStore.get_library``): a ``list[Game]`` is AUTHORITATIVE (the sync
layer replaces every Luna shortcut with it, Req 4.8) and an EMPTY list removes
all Luna entries (Req 4.9). Errors raised by ``fetch_library`` are not caught
here: the store decides between "session expired" and "retry, then keep the
existing entries".

Malformed tiles (no id, title or page path) are dropped by the parser and
counted; to honour Req 4.7 ("preserve the corresponding existing entry") the
writer then carries the pre-existing entries forward rather than letting the
authoritative replace drop them.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from unifideck.core.types import Game, GameTag
from unifideck.launcher.browser_games import BROWSER_URL_KEY
from unifideck.stores.luna.luna_catalog.models import LUNA_STORE_TAG, LunaTitle

if TYPE_CHECKING:
    from unifideck.stores.luna.luna_api.parse import ParsedLibrary

logger = logging.getLogger(__name__)

#: ``metadata`` key for the title's Amazon ASIN, when the page path has one.
ASIN_KEY = "asin"

FetchLibrary = Callable[[], Awaitable["ParsedLibrary"]]


class CatalogSyncStatus(Enum):
    """How one successful catalog synchronization ended (Req 4)."""

    #: One or more valid titles written (Req 4.3).
    SYNCED = "synced"
    #: Catalog answered with zero titles — all Luna entries removed (Req 4.9).
    SYNCED_ZERO = "synced_zero"
    #: Some tiles were malformed; the valid titles were written and the
    #: pre-existing entries preserved (Req 4.7).
    SYNCED_WITH_MALFORMED = "synced_with_malformed"


@dataclass(frozen=True)
class CatalogSyncResult:
    """The outcome of :meth:`LunaCatalog.sync`: status + authoritative games."""

    status: CatalogSyncStatus
    games: list[Game]


class LunaCatalog:
    """Fetches the Luna catalog and reconciles it into library entries."""

    def __init__(self, fetch_library: FetchLibrary) -> None:
        """Build the catalog over the API call that lists the titles."""
        self._fetch_library = fetch_library

    async def sync(
        self, previous_entries: list[dict[str, object]] | None = None,
    ) -> CatalogSyncResult:
        """Fetch the titles and reconcile them against ``previous_entries``.

        ``previous_entries`` are the Luna library entries already in
        ``library_cache.json`` (serialized :class:`Game` dicts), used to
        preserve entries when some tiles were malformed (Req 4.7). Raises
        whatever ``fetch_library`` raises.
        """
        library = await self._fetch_library()
        return self._reconcile(library.titles, library.malformed, previous_entries or [])

    def _reconcile(
        self,
        titles: list[LunaTitle],
        malformed: int,
        previous_entries: list[dict[str, object]],
    ) -> CatalogSyncResult:
        """Build the authoritative ``Game`` list (pure, no I/O).

        Titles are deduplicated on ``luna_product_id`` (first occurrence
        wins, order kept) so the list has one entry per product id.

        * zero titles and nothing malformed → ``[]``, :attr:`SYNCED_ZERO`;
        * all valid → the titles, :attr:`SYNCED`;
        * some malformed → valid titles plus the pre-existing entries not
          produced this run, :attr:`SYNCED_WITH_MALFORMED`.
        """
        valid: list[LunaTitle] = []
        seen_ids: set[str] = set()
        for title in titles:
            if not title.luna_product_id or title.luna_product_id in seen_ids:
                continue
            seen_ids.add(title.luna_product_id)
            valid.append(title)

        games = [self._to_game(t) for t in valid]
        if malformed:
            carried = self._carry_forward_unproduced(previous_entries, seen_ids)
            logger.warning(
                "[LunaCatalog] %d malformed tile(s) skipped; preserving %d "
                "pre-existing entr(y/ies), wrote %d valid title(s)",
                malformed, len(carried), len(games),
            )
            return CatalogSyncResult(
                status=CatalogSyncStatus.SYNCED_WITH_MALFORMED,
                games=games + carried,
            )
        if not games:
            logger.info(
                "[LunaCatalog] catalog returned zero titles — removing all "
                "Luna library entries",
            )
            return CatalogSyncResult(status=CatalogSyncStatus.SYNCED_ZERO, games=[])
        logger.info("[LunaCatalog] synced %d Luna title(s)", len(games))
        return CatalogSyncResult(status=CatalogSyncStatus.SYNCED, games=games)

    @staticmethod
    def _to_game(title: LunaTitle) -> Game:
        """One browser-game :class:`Game` for a valid Luna title.

        Carries ``GameTag.BROWSER`` + the ``luna`` store tag (Req 4.3, 4.5)
        and the title's page URL in ``metadata["browser_url"]`` (Req 4.4), so
        ``browser_games.browser_target`` resolves it to a ``stream`` target.
        ``app_id=0`` is assigned deterministically by the sync layer.
        """
        metadata: dict[str, object] = {BROWSER_URL_KEY: title.browser_url}
        if title.asin:
            metadata[ASIN_KEY] = title.asin
        return Game(
            app_id=0,
            store="luna",
            store_game_id=title.luna_product_id,
            title=title.name or title.luna_product_id,
            installed=False,
            tags=[GameTag.BROWSER, LUNA_STORE_TAG],
            metadata=metadata,
        )

    @staticmethod
    def _carry_forward_unproduced(
        previous_entries: list[dict[str, object]], produced_ids: set[str],
    ) -> list[Game]:
        """Pre-existing Luna entries whose id was not produced this run.

        A malformed tile has no usable id to match, so every previously
        synced entry not produced this run is preserved (Req 4.7). Entries
        missing their id or ``browser_url`` cannot be rebuilt and are left out.
        """
        carried: list[Game] = []
        for entry in previous_entries:
            game = LunaCatalog._reconstruct_carried_entry(entry, produced_ids)
            if game is not None:
                carried.append(game)
        return carried

    @staticmethod
    def _reconstruct_carried_entry(
        entry: object, produced_ids: set[str],
    ) -> Game | None:
        """Rebuild a carried-forward :class:`Game` from a previous entry."""
        if not isinstance(entry, dict):
            return None
        game_id = entry.get("store_game_id")
        if not isinstance(game_id, str) or not game_id or game_id in produced_ids:
            return None
        metadata = entry.get("metadata")
        url = metadata.get(BROWSER_URL_KEY) if isinstance(metadata, dict) else None
        if not isinstance(url, str) or not url:
            return None
        title = entry.get("title")
        return LunaCatalog._to_game(LunaTitle(
            luna_product_id=game_id,
            name=title if isinstance(title, str) and title else game_id,
            browser_url=url,
            asin=_text(metadata.get(ASIN_KEY)) if isinstance(metadata, dict) else None,
        ))


def _text(value: object) -> str | None:
    """``value`` when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None
