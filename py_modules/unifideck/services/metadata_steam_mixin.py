"""services/metadata_steam_mixin.py — Steam-side metadata tail for MetadataService.

Extracted from ``metadata_service.py`` to keep that file under the
550-LOC volumetry cap. Owns the Steam-Store tail of per-game
enrichment: shortcut → real-AppID resolution, the rich ``appdetails``
payload, the review summary, and the Date-Added stamp. All consumed
state (``_cache``, ``_config``) is declared as ``TYPE_CHECKING``
annotations — the host MetadataService provides it at runtime.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import aiohttp

    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.core.types import Game

logger = logging.getLogger(__name__)

# Two caches for the Steam Store patcher (SteamStorePatcher.ts).
# ``STEAM_REAL_APPID_NS`` maps each Unifideck shortcut's synthetic
# AppID to the real Steam Store AppID found by ``search_store``.
# ``STEAM_METADATA_NS`` holds the rich ``appdetails`` payload per
# real Steam AppID. The frontend reads both via dedicated RPCs.
#: The merged per-game metadata payload written by ``MetadataService``
#: and topped up by ``metadata_backfill``. Declared here with the other
#: namespace constants so the three readers share one spelling.
CACHE_NAMESPACE = "metadata"
STEAM_REAL_APPID_NS = "steam_real_appid"
#: Title each MISS above was searched under — see ``steam_appid_miss_stale``.
STEAM_APPID_MISS_NS = "steam_appid_miss"
STEAM_METADATA_NS = "steam_metadata"
STEAM_REVIEWS_NS = "steam_reviews"
SHORTCUT_ADDED_NS = "shortcut_added"


def steam_appid_miss_stale(
    cache: CacheManager, app_id: int, title: str,
) -> bool:
    """Whether a cached "no Steam counterpart" verdict predates the title.

    ``_resolve_steam_id`` finds the real Steam AppID with
    ``library.search_store(game.title)`` — the title is the whole query. A
    negative result was therefore only ever a statement about *that* title,
    but it was cached under the shortcut AppID with nothing recording which
    title it referred to, so it outlived the answer.

    That is not hypothetical. A GameVault game arrived from the user's own
    server as ``Test Game 2026``, which of course matched nothing on Steam;
    once they curated it into ``Ghost of Tsushima`` the ``-1`` still stood,
    and everything hanging off a real Steam AppID stayed empty — the
    ProtonDB / Deck-Verified tier read "UNKNOWN" for a game with thousands
    of ProtonDB reports. Positive mappings are deliberately NOT invalidated
    here: ``_resolve_steam_id``'s docstring explains why a good match must
    survive a flaky search, and a rename rarely invalidates one.

    A miss recorded before this cache existed has no title. Treat it as
    stale: one re-search per affected game, once, then it is recorded
    properly.
    """
    try:
        searched = cache.get(STEAM_APPID_MISS_NS, str(app_id))
    except Exception:
        return True
    return searched != title


class _SteamMetadataMixin:
    """Steam appdetails/reviews resolution composed into MetadataService."""

    # State provided by the host MetadataService at runtime.
    _cache: CacheManager
    _config: ConfigManager | None

    async def fetch_appdetails_for_game(
        self,
        game: Game,
        *,
        hint_steam_id: int | None = None,
        session: aiohttp.ClientSession | None = None,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """Resolve a game to a real Steam AppID, fetch its rich appdetails.

        ``force=True`` (force sync) re-resolves the AppID mapping and
        re-fetches appdetails + reviews, overwriting cached entries.
        """
        from unifideck.steam.appdetails import fetch_appdetails
        steam_id = await self._resolve_steam_id(
            game, hint_steam_id, session=session, force=force,
        )
        if steam_id is None:
            self._cache_set_safely(
                STEAM_REAL_APPID_NS, str(game.app_id), -1,
            )
            # Record WHAT missed. ``search_store`` matches on the title, so a
            # later title makes this miss inapplicable rather than settled.
            self._cache_set_safely(
                STEAM_APPID_MISS_NS, str(game.app_id), game.title,
            )
            return None
        self._cache_set_safely(
            STEAM_REAL_APPID_NS, str(game.app_id), steam_id,
        )
        self._stamp_date_added(game.app_id)
        if not force:
            try:
                existing = self._cache.get(STEAM_METADATA_NS, str(steam_id))
                if isinstance(existing, dict):
                    return cast("dict[str, Any]", existing)
            except Exception:
                logger.debug(
                    "[MetadataService] metadata cache read failed", exc_info=True,
                )
        data = await fetch_appdetails(steam_id, config=self._config, session=session)
        if data is None:
            return None
        self._cache_set_safely(STEAM_METADATA_NS, str(steam_id), data)
        await self._fetch_reviews(steam_id, session=session, force=force)
        return data

    async def _fetch_reviews(
        self,
        steam_id: int,
        session: aiohttp.ClientSession | None = None,
        *,
        force: bool = False,
    ) -> None:
        """Fetch + cache the Steam review summary for ``steam_id`` once."""
        if not force:
            try:
                if self._cache.get(STEAM_REVIEWS_NS, str(steam_id)) is not None:
                    return
            except Exception:
                logger.debug(
                    "[MetadataService] reviews cache read failed", exc_info=True,
                )
        from unifideck.steam.appreviews import fetch_appreviews
        reviews = await fetch_appreviews(
            steam_id, config=self._config, session=session,
        )
        if reviews is not None:
            self._cache_set_safely(STEAM_REVIEWS_NS, str(steam_id), reviews)

    def _stamp_date_added(self, app_id: int) -> None:
        """Record a stable first-seen timestamp for the Date-Added sort."""
        try:
            if self._cache.get(SHORTCUT_ADDED_NS, str(app_id)) is not None:
                return
            self._cache_set_safely(
                SHORTCUT_ADDED_NS, str(app_id), int(time.time()),
            )
        except Exception:
            logger.debug(
                "[MetadataService] date-added stamp failed", exc_info=True,
            )

    def _usable_cached_steam_id(
        self, game: Game,
    ) -> tuple[bool, int | None]:
        """``(answered, steam_id)`` from the cache alone.

        A tuple rather than an optional int because there are three
        outcomes, and ``None`` is itself one of the answers: "searched
        already, and this title has no Steam counterpart". Collapsing that
        into "nothing cached" is what would send every known-absent game
        back to ``storesearch`` on every sync.

        A negative mapping only answers for the title it was searched under
        — see :func:`steam_appid_miss_stale`. Split out of
        ``_resolve_steam_id`` because that extra branch put it over the
        cognitive-complexity gate.
        """
        try:
            cached_id = self._cache.get(STEAM_REAL_APPID_NS, str(game.app_id))
        except Exception:
            logger.debug(
                "[Metadata] cached appid read failed for %s", game.app_id,
                exc_info=True,
            )
            return False, None
        if not isinstance(cached_id, int):
            return False, None
        if cached_id > 0:
            return True, cached_id
        if steam_appid_miss_stale(self._cache, game.app_id, game.title):
            return False, None
        return True, None

    async def _resolve_steam_id(
        self,
        game: Game,
        hint_steam_id: int | None,
        session: aiohttp.ClientSession | None = None,
        *,
        force: bool = False,
    ) -> int | None:
        """Return a valid Steam AppID for ``game`` — hint or cache or live search.

        ``force=True`` skips the cache read and re-searches (retrying
        negative entries and fixing wrong matches). The hint is still
        trusted — under force it comes from ``enrich()``'s own live
        storesearch this run, so honouring it avoids a duplicate call.
        When a forced re-search comes back empty but a positive mapping
        was cached, the cached mapping wins: an empty ``storesearch``
        is indistinguishable from a transient failure, and downgrading
        hundreds of good mappings to ``-1`` on a flaky network would be
        far worse than keeping a rare stale match.
        """
        if hint_steam_id is not None and hint_steam_id > 0:
            return hint_steam_id
        if not force:
            answered, cached_id = self._usable_cached_steam_id(game)
            if answered:
                return cached_id
        from unifideck.steam import library
        best: dict[str, Any] | None = None
        try:
            best = await library.search_store(
                game.title, config=self._config, session=session,
            )
        except Exception:
            logger.debug(
                "[Metadata] Steam search failed for %s", game.title,
            )
        raw = best.get("app_id") if best else None
        if isinstance(raw, int) and raw > 0:
            return raw
        if force:
            return self._cached_positive_steam_id(game.app_id)
        return None

    def _cached_positive_steam_id(self, app_id: int) -> int | None:
        """Cached positive shortcut → Steam-AppID mapping, or ``None``."""
        try:
            cached_id = self._cache.get(STEAM_REAL_APPID_NS, str(app_id))
        except Exception:
            return None
        if isinstance(cached_id, int) and cached_id > 0:
            return cached_id
        return None

    def _cache_set_safely(
        self, namespace: str, key: str, value: Any,
    ) -> None:
        """``cache.set`` that logs (at DEBUG) on failure instead of raising.

        Deferred write — every caller sits inside the per-game
        enrichment/backfill loops, so eager persistence would rewrite
        the growing namespace file once per key (O(n²) disk I/O over a
        big library). ``_flush_deferred_caches`` persists at the phase
        boundary; the store's auto-flush valve bounds crash loss.
        """
        try:
            self._cache.set(namespace, key, value, flush=False)
        except Exception:
            logger.debug(
                "[Metadata] cache set %s failed for %s",
                namespace, key,
            )

    def _flush_deferred_caches(self) -> None:
        """Persist every namespace the enrichment loops write deferred."""
        for namespace in (
            "metadata",
            STEAM_REAL_APPID_NS,
            STEAM_METADATA_NS,
            STEAM_REVIEWS_NS,
            SHORTCUT_ADDED_NS,
        ):
            try:
                self._cache.flush(namespace)
            except Exception:
                logger.debug(
                    "[Metadata] cache flush %s failed", namespace,
                )
