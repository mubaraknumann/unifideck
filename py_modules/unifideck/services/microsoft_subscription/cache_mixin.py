from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from unifideck.core.types import SubscriptionTier

from .cache import _CachedEntry
from .constants import _CACHE_KEY_PREFIX, _CACHE_STORE_NAME, _SESSION_CACHE_KEY_PREFIX
from .time_utils import _end_of_month_utc

if TYPE_CHECKING:
    from unifideck.core.cache_manager import CacheManager
    from unifideck.stores.microsoft.microsoft_subscription import (
        SubscriptionProbeResult,
    )
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager, XBLTokenChain
logger = logging.getLogger(__name__)

# Negative results (NONE / ACTIVE_UNKNOWN) cache for a short window so
# a transient probe failure (gateway 403, network blip, expired token)
# can't lock the user out of xCloud sync for the rest of the month.
_NEGATIVE_CACHE_SECONDS = 30 * 60
class _CacheMixin:
    """Cache mixin."""
    _cache: CacheManager
    _last_standard_chain: XBLTokenChain | None
    async def _resolve_cache_key(
        self,
        token_manager: MicrosoftTokenManager,
    ) -> str:
        """Resolve cache key."""
        xuid: str | None = None
        try:
            chain = await token_manager.build_chain()
            if chain is not None:
                xuid = chain.xuid
                self._last_standard_chain = chain
        except Exception:
            logger.debug(
                "[MSSubSvc] could not build chain for key resolution",
                exc_info=True,
            )
        return f"{_CACHE_KEY_PREFIX}{xuid or 'default'}"
    def _read_cache(self, key: str) -> _CachedEntry | None:
        """Read cache."""
        try:
            raw = self._cache.get(_CACHE_STORE_NAME, key)
        except Exception:
            logger.exception("[MSSubSvc] cache read failed")
            return None
        if raw is None:
            return None
        if isinstance(raw, dict):
            return _CachedEntry.from_dict(raw)
        return None
    def _write_cache(self, key: str, entry: _CachedEntry) -> None:
        """Write cache."""
        try:
            self._cache.set(_CACHE_STORE_NAME, key, entry.to_dict())
        except Exception:
            logger.exception("[MSSubSvc] cache write failed")
    def _store_tier_result(
        self, cache_key: str, tier: SubscriptionTier,
    ) -> None:
        """Store tier result.

        Positive results (PREMIUM/ULTIMATE/ESSENTIAL/ACTIVE_UNKNOWN)
        cache to end-of-month — Microsoft bills monthly so a paid
        tier is stable for that window. Negative results (NONE) cache
        for 30 minutes only, so a transient gateway 403 can't lock
        out the user for weeks.

        Synchronous: this used to end with an ``_emit_state_change`` await,
        which was the only reason it was a coroutine. That emitted
        SUBSCRIPTION_DETECTED / SUBSCRIPTION_EXPIRED into a channel with no
        consumer on any leg and was retired (audit §1.3).
        """
        now = time.time()
        if tier == SubscriptionTier.NONE:
            expires_at = now + _NEGATIVE_CACHE_SECONDS
        else:
            expires_at = _end_of_month_utc()
        entry = _CachedEntry(
            tier=tier,
            expires_at=expires_at,
            detected_at=now,
        )
        self._write_cache(cache_key, entry)

    # ── Session persistence (gsToken + regions) ──────────────────

    def _persist_session(
        self, xuid: str, probe: SubscriptionProbeResult,
    ) -> None:
        """Write the probe session to CacheManager for restart survival."""
        key = f"{_SESSION_CACHE_KEY_PREFIX}{xuid or 'default'}"
        try:
            self._cache.set(
                _CACHE_STORE_NAME, key, probe.to_dict(),
            )
            logger.debug(
                "[MSSubSvc] persisted session for %s (expires_at=%.0f)",
                xuid, probe.expires_at,
            )
        except Exception:
            logger.exception("[MSSubSvc] session persist failed")

    def _load_persisted_session(
        self, xuid: str,
    ) -> SubscriptionProbeResult | None:
        """Load a persisted probe session from CacheManager.

        Returns None if nothing is cached, the data is malformed,
        or the gsToken has expired.
        """
        from unifideck.stores.microsoft.microsoft_subscription import (
            SubscriptionProbeResult,
        )

        key = f"{_SESSION_CACHE_KEY_PREFIX}{xuid or 'default'}"
        try:
            raw = self._cache.get(_CACHE_STORE_NAME, key)
        except Exception:
            logger.exception("[MSSubSvc] session cache read failed")
            return None
        if not isinstance(raw, dict):
            return None
        probe = SubscriptionProbeResult.from_dict(raw)
        if probe is None:
            return None
        if not probe.is_session_fresh():
            logger.debug(
                "[MSSubSvc] persisted session expired for %s", xuid,
            )
            return None
        return probe

    def _clear_persisted_sessions(self) -> None:
        """Remove all persisted session entries from the cache."""
        try:
            # CacheManager.clear removes all keys for the store;
            # tier entries are re-populated on next get_tier() call.
            self._cache.clear(_CACHE_STORE_NAME)
            logger.debug("[MSSubSvc] cleared persisted sessions")
        except Exception:
            logger.exception(
                "[MSSubSvc] session cache clear failed",
            )
