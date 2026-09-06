"""Game compatibility ratings via ProtonDB and Steam Deck Verified."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

import aiohttp

from unifideck.core.compat_bridge import appid_candidates
from unifideck.utils.config_helpers import get_cfg

from .deck_verified import (
    TRACK_NAMES,
    TrackResult,
    parse_compat_response,
)

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager


logger = logging.getLogger(__name__)

PROTONDB_TIERS = ("platinum", "gold", "silver", "bronze", "borked")

#: Bumped when the cached entry shape changes in a way a warm cache
#: cannot satisfy. Entries stamped below this are re-fetched once.
#:
#: There is NO expiry backstop behind that one shot:
#: ``bootstrap/cache_registry.py`` registers ``("compat", 0)`` and
#: ``CacheManager.register`` no-ops on an already-registered name, so
#: this class's own ``register(..., ttl_seconds=cache_ttl.compat)`` is
#: dead and the 604800 in ``defaults/config.json`` never applies.
#: A wrong entry therefore lasts the life of the install, which is why
#: the stamp is written only when an upstream actually answered.
COMPAT_SCHEMA = 2

PROTONDB_URL = (
 "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
)

DECK_VERIFIED_URL = (
 "https://store.steampowered.com/saleaction/"
 "ajaxgetdeckappcompatibilityreport?nAppID={appid}"
)

DEFAULT_USER_AGENT = "Unifideck/1.0 (compat-library)"
CACHE_NAMESPACE = "compat"



@dataclass
class CompatRating:
    """One title's rating on every device, plus its ProtonDB tier.

    Per-track fields are **flat** (``machine_status``, not
    ``tracks["machine"].status``) so an entry cached before those fields
    existed rehydrates for free: ``_rating_from_cached`` filters unknown
    keys, and anything absent takes the dataclass default. A nested dict
    would need a hand-written normaliser and a shape check on every read.

    Both the raw ``category`` int and the ``status`` string are kept.
    They do different jobs -- the int is Valve's own, and goes verbatim
    into Steam's compat bitfield; the string is ours, for our UI.
    Deriving either from the other corrupts the SteamOS track, whose
    integers do not mean verified/playable.
    """

    appid: int | None = None
    title: str = ""
    protondb_tier: str | None = None
    deck_status: str = "unknown"
    deck_category: int = 0
    deck_test_results: list[dict[str, Any]] = field(default_factory=list)
    steamos_status: str = "unknown"
    steamos_category: int = 0
    steamos_test_results: list[dict[str, Any]] = field(default_factory=list)
    machine_status: str = "unknown"
    machine_category: int = 0
    machine_test_results: list[dict[str, Any]] = field(default_factory=list)
    frame_status: str = "unknown"
    frame_category: int = 0
    frame_test_results: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    error: str | None = None
    #: 0 means an entry written before tracks existed. See COMPAT_SCHEMA.
    schema: int = 0

    def track(self, name: str) -> TrackResult:
        """This title's rating on one device track."""
        return TrackResult(
            category=getattr(self, f"{name}_category", 0),
            status=getattr(self, f"{name}_status", "unknown"),
            test_results=list(getattr(self, f"{name}_test_results", [])),
        )

    def apply(self, tracks: dict[str, TrackResult]) -> None:
        """Adopt freshly fetched tracks, keeping cached ones on failure.

        A track is only adopted when Valve actually rated it
        (``category > 0``). A transient fetch failure returns zeros for
        every track, and must not downgrade a rating we already hold.
        """
        for name in TRACK_NAMES:
            fresh = tracks.get(name)
            if fresh is None or fresh.category <= 0:
                continue
            setattr(self, f"{name}_category", fresh.category)
            setattr(self, f"{name}_status", fresh.status)
            setattr(self, f"{name}_test_results", list(fresh.test_results))

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the cache. Mirrors the dataclass exactly."""
        payload: dict[str, Any] = {
            "appid": self.appid,
            "title": self.title,
            "protondb_tier": self.protondb_tier,
            "sources": list(self.sources),
            "error": self.error,
            "schema": self.schema,
        }
        for name in TRACK_NAMES:
            payload[f"{name}_status"] = getattr(self, f"{name}_status")
            payload[f"{name}_category"] = getattr(self, f"{name}_category")
            payload[f"{name}_test_results"] = list(
                getattr(self, f"{name}_test_results"),
            )
        return payload


# Marker keys (``dtr_checked``) live alongside the rating fields in
# the cached dict — filter to real dataclass fields so cached-entry
# construction can't crash on them.
_RATING_FIELDS = frozenset(f.name for f in fields(CompatRating))


def _rating_from_cached(cached: dict[str, Any]) -> CompatRating:
    """Build a ``CompatRating`` from a cached dict, ignoring marker keys."""
    return CompatRating(
        **{k: v for k, v in cached.items() if k in _RATING_FIELDS},
    )


def needs_refetch(cached: dict[str, Any]) -> bool:
    """Whether a cached entry predates the current schema.

    Replaces the old ``dtr_checked`` heuristic ("has a status but no
    test results"), which it subsumes: a schema-0 entry is re-fetched
    once, and that fills both the missing tracks and any missing test
    results in the same round trip.
    """
    try:
        return int(cached.get("schema", 0)) < COMPAT_SCHEMA
    except (TypeError, ValueError):
        return True


def _stamped(result: CompatRating) -> dict[str, Any]:
    """Serialise with the current schema stamp.

    ``dtr_checked`` is still written so that downgrading to an older
    plugin build does not re-trigger *its* one-shot self-heal against
    every entry this build wrote.
    """
    result.schema = COMPAT_SCHEMA
    payload = result.to_dict()
    payload["dtr_checked"] = True
    return payload


def parse_protondb_response(payload: dict[str, Any]) -> str | None:
    """Parse protondb response."""
    if not isinstance(payload, dict):
        return None  # type: ignore[unreachable]  # fallback after path-type narrowing
    tier = payload.get("tier")
    if isinstance(tier, str) and tier in PROTONDB_TIERS:
        return tier
    return None
def _cfg(config: ConfigManager | None, key: str, default: Any) -> Any:

    """Cfg."""
    return get_cfg(config, key, default)
class CompatLibrary:
    """Compat library."""
    def __init__(
        self,
        cache: CacheManager | None = None,
        config: ConfigManager | None = None,
        *,
        deferred_writes: bool = False,
    ) -> None:
        """Initialize the instance.

        ``deferred_writes=True`` makes cache writes stay in memory
        until the owner flushes (CompatibilityService's per-sync
        loop writes once per game — eager persistence would rewrite
        the whole namespace file each time). Ad-hoc/legacy
        constructions keep the eager default.
        """
        self._cache = cache
        self._config = config
        self._deferred_writes = deferred_writes
        if cache is not None:
            ttl = int(get_cfg(config, "cache_ttl.compat", 604800))
            try:
                cache.register(CACHE_NAMESPACE, ttl_seconds=ttl)
            except Exception as e:
                # Already registered or cache backend misconfigured;
                # lookups will still work, just without our preferred TTL.
                logger.debug("[CompatLibrary] cache.register failed: %s", e)
    async def get_for_appid(
        self,
        appid: int,
        *,
        refresh: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> CompatRating:
        """Look up ProtonDB + Deck-Verified for a real Steam AppID.

        ``refresh=True`` (force sync) skips the cache read and
        overwrites the entry with a fresh fetch — old data survives
        only if the caller never reaches the write (task cancelled).
        ``session`` lets the sync loop share one connection pool
        across all games.
        """
        cached = None if refresh else self._cache_get(str(appid))
        if cached is not None:
            result = _rating_from_cached(cached)
            # One-shot upgrade of an entry written before the current
            # schema. A schema-0 entry holds only the Deck rating (and
            # possibly no test results), so re-fetch once and fill in
            # every track. ProtonDB is left alone — it was already
            # correct in the old format. The stamp is what stops a
            # title with genuinely no published rating from re-hitting
            # the endpoint every sync forever.
            if needs_refetch(cached):
                tracks = await self._fetch_compat(appid, session)
                # Spend the one-shot migration only on a real answer. A
                # timeout or a 429 that stamped anyway would strand this
                # title Deck-only for the life of the install — the
                # compat cache has no TTL to expire it.
                if tracks is not None:
                    result.apply(tracks)
                    self._cache_set(str(appid), _stamped(result))
            return result
        result = CompatRating(appid=appid)
        result.protondb_tier = await self._fetch_protondb(appid, session)
        if result.protondb_tier is not None:
            result.sources.append("protondb")
        tracks = await self._fetch_compat(appid, session)
        if tracks is not None:
            result.apply(tracks)
            if any(result.track(n).category > 0 for n in TRACK_NAMES):
                result.sources.append("deck_verified")
        # Persist only if at least one upstream actually answered.
        # Caching an all-unknown entry built from two failed requests
        # would make this title permanently unrated: the namespace has
        # no TTL, and the schema stamp makes every later sync skip it.
        if result.protondb_tier is not None or tracks is not None:
            self._cache_set(str(appid), _stamped(result))
        return result
    async def get_for_title(
        self,
        title: str,
        shortcut_app_id: int | None = None,
        *,
        refresh: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> CompatRating:
        """Resolve ``title`` to a Steam AppID, then look up ProtonDB + Deck-Verified.

        When ``shortcut_app_id`` is provided we first try the
        ``steam_real_appid`` cache populated by
        :meth:`MetadataService.fetch_appdetails_for_game`. That
        cache holds the shortcut → real-Steam-AppID mapping for
        every non-Steam game the prior metadata phase saw, and
        skipping the live ``search_store`` call eliminates the
        per-game storesearch hit that used to trip Steam's rate
        limit (three services calling storesearch in parallel for
        every game across a 1000-title library).

        Falls back to ``search_store(title)`` on cache miss so the
        method still works for callers that don't have a shortcut
        AppID (e.g. ad-hoc lookups outside the sync pipeline). A
        failed search with a known shortcut is negative-cached
        (``steam_real_appid = -1``, MetadataService's convention)
        so the sync partition skips it instead of re-searching
        every sync; a force sync retries it via the metadata
        phase's re-resolution.

        ``refresh=True`` bypasses the compat cache read (force
        sync). The positive AppID mapping is still trusted — the
        metadata phase re-resolves it before this phase runs.
        """
        steam_id: int | None = None
        if shortcut_app_id is not None:
            steam_id = self._lookup_cached_steam_id(shortcut_app_id)
        if steam_id is None:
            from unifideck.steam.library import search_store
            steam = await search_store(
                title, config=self._config, session=session,
            )
            try:
                steam_id = int(steam["app_id"]) if steam else 0
            except (TypeError, ValueError, KeyError):
                steam_id = 0
            if steam_id <= 0:
                if shortcut_app_id is not None:
                    self._persist_steam_real_appid(shortcut_app_id, -1)
                return CompatRating(
                    title=title, error="not_found_on_steam_store",
                )
            # Backfill the shortcut → AppID mapping the metadata phase
            # missed, so the facet join surfaces this game's badge.
            if shortcut_app_id is not None:
                self._persist_steam_real_appid(shortcut_app_id, steam_id)
        result = await self.get_for_appid(
            steam_id, refresh=refresh, session=session,
        )
        result.title = title
        return result

    def cached_steam_mapping(self, shortcut_app_id: int) -> int | None:
        """Raw shortcut → Steam-AppID mapping, including negative sentinels.

        Mirrors :meth:`ArtworkService._lookup_cached_steam_id`. Reads
        the ``steam_real_appid`` cache namespace's raw ``_data`` dict;
        the key is ``str(game.app_id)`` (signed 32-bit, matching how
        the sync layer stores AppIDs). Tries both signed and unsigned
        forms because Steam's frontend hands the unsigned form down
        through some code paths. ``None`` = never resolved; ``<= 0``
        = negative-cached "no Steam counterpart".
        """
        cache = getattr(self, "_cache", None)
        if cache is None:
            return None
        try:
            stores = getattr(cache, "_stores", None)
            if not isinstance(stores, dict):
                return None
            data = getattr(stores.get("steam_real_appid"), "_data", None)
            if not isinstance(data, dict):
                return None
            for key in appid_candidates(shortcut_app_id):
                value = data.get(key)
                if isinstance(value, int):
                    return value
        except Exception:
            return None
        return None

    def _lookup_cached_steam_id(self, shortcut_app_id: int) -> int | None:
        """Positive-only view of :meth:`cached_steam_mapping`."""
        value = self.cached_steam_mapping(shortcut_app_id)
        return value if isinstance(value, int) and value > 0 else None

    async def bulk_fetch(
    self, titles: list[str], delay_ms: int = 50,
    ) -> dict[str, CompatRating]:
        """Bulk fetch."""
        out: dict[str, CompatRating] = {}
        for title in titles:
            out[title] = await self.get_for_title(title)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
        return out
    async def _fetch_protondb(
        self,
        appid: int,
        session: aiohttp.ClientSession | None = None,
    ) -> str | None:
        """Fetch protondb.

        Reuses ``session`` when provided (the sync loop passes one
        shared session — creating a connector per game cost two TLS
        handshakes per title on a cold sync). No rate-limit gate:
        protondb.com is a different host from the Steam Store.
        """
        url = PROTONDB_URL.format(appid=appid)
        timeout = int(_cfg(
        self._config, "compat.protondb_timeout_seconds", 30,
        ))
        payload = await self._get_json(
            url, session, timeout, log_tag=f"[compat] protondb({appid})",
            gate=None,
        )
        if not isinstance(payload, dict):
            return None
        return parse_protondb_response(payload)

    async def _fetch_compat(
        self,
        appid: int,
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, TrackResult] | None:
        """Fetch every device's verification status + per-test reasoning.

        One request answers all four tracks.

        Returns ``None`` when the request itself failed, and a dict when
        Valve answered — even if it answered "nothing rated". Those two
        must not collapse into the same value: the caller stamps the
        cache entry as migrated, and with the compat namespace carrying
        no TTL a stamp written on a timeout is permanent. Runs behind the
        shared ``STEAM_STORE_GATE`` (same host as
        storesearch/appdetails).
        """
        from unifideck.steam.http_retry import STEAM_STORE_GATE
        url = DECK_VERIFIED_URL.format(appid=appid)
        timeout = int(_cfg(
        self._config, "compat.deck_verified_timeout_seconds", 10,
        ))
        payload = await self._get_json(
            url, session, timeout, log_tag=f"[compat] deck({appid})",
            gate=STEAM_STORE_GATE,
        )
        if not isinstance(payload, dict):
            return None
        return parse_compat_response(payload)

    async def _get_json(
        self,
        url: str,
        session: aiohttp.ClientSession | None,
        timeout_s: float,
        *,
        log_tag: str,
        gate: Any,
    ) -> Any | None:
        """GET JSON on ``session`` (or a one-shot session) with 429 backoff.

        ``ssl=False`` on the one-shot connector — SteamOS's outdated
        cert store breaks SSL verification for several third-party
        hosts inside the Decky plugin process. See
        ``library.search_store`` for the same workaround.
        """
        from unifideck.steam.http_retry import get_json_with_backoff
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        try:
            if session is not None:
                return await get_json_with_backoff(
                    session, url, timeout_s=timeout_s, log_tag=log_tag,
                    headers=headers, gate=gate,
                )
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as one_shot:
                return await get_json_with_backoff(
                    one_shot, url, timeout_s=timeout_s, log_tag=log_tag,
                    headers=headers, gate=gate,
                )
        except Exception as e:
            logger.debug("%s failed: %s", log_tag, e)
            return None
    def _cache_get(self, key: str) -> dict[str, Any] | None:
        """Cache get."""
        if self._cache is None:
            return None
        try:
            return self._cache.get(CACHE_NAMESPACE, key)
        except Exception:
            return None
    def _cache_set(
        self, key: str, value: dict[str, Any],
    ) -> None:
        """Cache set (deferred when ``deferred_writes`` — owner flushes)."""
        if self._cache is None:
            return
        try:
            if self._deferred_writes:
                self._cache.set(CACHE_NAMESPACE, key, value, flush=False)
            else:
                self._cache.set(CACHE_NAMESPACE, key, value)
        except Exception as e:
            # Cache write failures are non-fatal: the rating was
            # computed successfully, we just won't re-use it.
            logger.debug("[CompatLibrary] cache.set %r failed: %s", key, e)

    def _persist_steam_real_appid(
        self, shortcut_app_id: int, steam_id: int,
    ) -> None:
        """Backfill the shortcut → real-Steam-AppID mapping.

        Compat resolves the Steam AppID by title (``search_store`` +
        the edition-strip fallback) even for games the metadata phase
        negative-cached or never resolved — but only the metadata phase
        writes ``steam_real_appid``, so those games' already-fetched
        ProtonDB / Deck-Verified rating never linked back to the
        shortcut for the library-facets join (e.g. "Among Us": compat
        cached under 945360, but the shortcut had no mapping → no
        badge). Persist it here, keyed by the signed AppID to match how
        the sync layer writes it. Non-fatal on failure.

        ``steam_id = -1`` is the negative sentinel (title has no Steam
        counterpart) — same convention MetadataService writes, read by
        the sync partition to skip the game next run.
        """
        if self._cache is None or steam_id == 0:
            return
        try:
            if self._deferred_writes:
                self._cache.set(
                    "steam_real_appid", str(shortcut_app_id), steam_id,
                    flush=False,
                )
            else:
                self._cache.set(
                    "steam_real_appid", str(shortcut_app_id), steam_id,
                )
        except Exception as e:
            logger.debug(
                "[CompatLibrary] steam_real_appid backfill %r failed: %s",
                shortcut_app_id, e,
            )
