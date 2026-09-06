"""LibraryFacetsRPCMixin — per-shortcut facets for native Sort/Filters.

Exposes one bulk RPC the frontend reads at boot (and on
``unifideck-sync-completed``) to:

* enrich each Unifideck shortcut's live ``AppOverview`` so Steam's
  **native** library Sort menu + Library Filters work for non-Steam
  games (metacritic, per-device compat categories, store
  categories/tags, release date, reviews, date-added), and
* resolve the compatibility tab by shortcut AppID with zero title
  matching (``compat_status`` / ``protondb_tier`` ride along).

The heavy lifting (cache joins, signed/unsigned keying) lives in
:mod:`._library_facets` so this stays a thin endpoint wrapper.
Pure cache read — never fetches.
"""

from __future__ import annotations

import logging
from typing import Any

from unifideck.rpc.mixins._library_facets import build_enrichment_map

logger = logging.getLogger(__name__)

class LibraryFacetsRPCMixin:
    """Bulk library-facet enrichment RPC (read-only cache reshape)."""

    cache: Any
    sync_service: Any

    async def get_overview_enrichment(self) -> dict[str, Any]:
        """Return ``{shortcut_app_id: FacetRecord}`` for every mapped shortcut.

        Keyed by both signed and unsigned 32-bit string forms of the
        shortcut AppID. Each ``FacetRecord`` carries the sort fields
        (``metacritic``, ``release_date``, ``review_score``,
        ``review_percentage``, ``recommendations_total``,
        ``date_added_unix``), the filter fields (``compat_category``
        0..3 for this device, ``compat_categories`` per track for the
        packed bitfield, ``store_category`` ids, ``store_tag`` ids) and
        the compat-tab fields (``protondb_tier``, ``compat_status``,
        ``steam_app_id``). Empty when the metadata/compat caches are
        cold or unregistered — the frontend degrades to no enrichment.
        """
        try:
            # Prefer the unified games list: it gives a facet to every
            # shortcut (even ones with no resolved Steam AppID) and lets
            # the builder read metacritic by the robust ``store:game_id``
            # key. Degrade to the cache-only enumeration if the sync
            # service isn't available.
            games = None
            sync_service = getattr(self, "sync_service", None)
            if sync_service is not None:
                try:
                    games = sync_service.get_all_games()
                except Exception as exc:  # degrade, don't fail
                    logger.warning(
                        "[LibraryFacets] get_all_games failed: %s",
                        exc,
                    )
            return build_enrichment_map(self.cache, games)
        except Exception as exc:  # never break the frontend boot path
            logger.warning("[LibraryFacets] enrichment build failed: %s", exc)
            return {}
