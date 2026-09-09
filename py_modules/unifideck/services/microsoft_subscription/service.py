from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from unifideck.core.types import SubscriptionTier
from unifideck.event_bus.event_bus import EventBus
from unifideck.event_bus.event_bus_devex import auto_wire

from .cache_mixin import _CacheMixin
from .constants import _CACHE_STORE_NAME
from .event_handlers import _EventHandlersMixin
from .probe_emission import _ProbeEmissionMixin
from .time_utils import _fmt_ts

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.stores.microsoft.microsoft_subscription import (
        SubscriptionProbeResult,
    )
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager, XBLTokenChain
logger = logging.getLogger(__name__)

class MicrosoftSubscriptionService(
    _CacheMixin, _ProbeEmissionMixin, _EventHandlersMixin,
):
    """Microsoft subscription service."""
    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        config: ConfigManager | None = None,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._cache = cache
        self._config = config
        try:
            self._cache.register(_CACHE_STORE_NAME, ttl_seconds=0)
        except Exception:
            logger.exception(
                "[MSSubSvc] could not register cache store %s",
                _CACHE_STORE_NAME,
            )
        self._lock = asyncio.Lock()
        self._last_standard_chain: XBLTokenChain | None = None
        # In-memory probe session: holds the most recent SubscriptionProbeResult
        # (gsToken + regions + market) so downstream consumers (catalog reader)
        # can reuse it within the JWT's lifetime without re-probing.
        self._last_probe: SubscriptionProbeResult | None = None
        auto_wire(self, self._bus)
        logger.info(
            "[MSSubSvc] initialized (endpoint=%s)",
            self._probe_url(),
        )

    async def get_tier(
        self,
        token_manager: MicrosoftTokenManager,
    ) -> SubscriptionTier:

        """Get tier."""
        cache_key = await self._resolve_cache_key(token_manager)
        async with self._lock:
            cached = self._read_cache(cache_key)
            if cached is not None and cached.is_fresh():
                logger.debug(
                    "[MSSubSvc] cache hit for %s: tier=%s "
                    "(expires in %ds)",
                    cache_key,
                    cached.tier.value,
                    int(cached.expires_at - time.time()),
                )
                return cached.tier
            probe_result = await self._run_probe(token_manager)
            if probe_result.ok:
                return await self._handle_probe_success(cache_key, probe_result)
            if cached is not None:
                logger.warning(
                    "[MSSubSvc] probe failed (%s), using stale "
                    "cache tier=%s from %s",
                    probe_result.error,
                    cached.tier.value,
                    _fmt_ts(cached.detected_at),
                )
                return cached.tier
            # No SUBSCRIPTION_CHECK_FAILED emit here. Returning NONE makes
            # MicrosoftStore emit SYNC_SKIPPED(reason="subscription_check_error"),
            # which is the channel the frontend actually renders — a second
            # event carrying the same news had no consumer on any leg
            # (audit §1.3).
            logger.warning(
                "[MSSubSvc] probe failed (%s) and no cache "
                "— returning NONE",
                probe_result.error,
            )
            return SubscriptionTier.NONE

    async def _handle_probe_success(
        self, cache_key: str, probe_result: SubscriptionProbeResult,
    ) -> SubscriptionTier:
        """Persist a successful probe (tier + session) and return the tier.

        Session artefacts (gsToken/regions/market) are kept in memory
        for the catalog reader to reuse within the JWT lifetime, and
        persisted to disk so they survive plugin restarts.
        """
        self._last_probe = probe_result
        self._store_tier_result(cache_key, probe_result.tier)
        xuid = self._xuid_from_chain()
        if probe_result.is_session_fresh():
            self._persist_session(xuid, probe_result)
        return probe_result.tier
    async def has_active_subscription(
        self,
        token_manager: MicrosoftTokenManager,
    ) -> bool:
        """Check whether active subscription."""
        tier = await self.get_tier(token_manager)
        return tier != SubscriptionTier.NONE

    async def get_session(
        self,
        token_manager: MicrosoftTokenManager,
    ) -> SubscriptionProbeResult | None:
        """Return a fresh-enough probe result for downstream use.

        The xCloud catalog reader needs the ``gsToken`` and ``regions``
        from the login response to call the regional ``/v2/titles``
        endpoint. We check in order:

        1. **In-memory** ``_last_probe`` — fastest, no I/O.
        2. **Persisted session** from CacheManager — survives plugin
           restarts without re-probing.
        3. **Fresh probe** — runs ``_run_probe()`` directly, bypassing
           the tier cache that previously caused the gsToken to never
           be obtained when the tier was cached end-of-month.

        Returns None if no successful probe is available.
        """
        # 1. In-memory fast path.
        if self._last_probe is not None and self._last_probe.is_session_fresh():
            remaining = self._last_probe.expires_at - time.time()
            logger.debug(
                "[MSSubSvc] get_session: in-memory session valid "
                "(expires in %.0fs)", remaining,
            )
            return self._last_probe

        # 2. Persisted session (survives plugin restart).
        xuid = self._xuid_from_chain()
        persisted = self._load_persisted_session(xuid)
        if persisted is not None:
            self._last_probe = persisted
            remaining = persisted.expires_at - time.time()
            logger.info(
                "[MSSubSvc] get_session: restored persisted session "
                "(expires in %.0fs)", remaining,
            )
            return persisted

        # 3. Force a fresh probe — bypass get_tier() which may
        #    return from tier cache without running the probe.
        logger.info(
            "[MSSubSvc] get_session: no session available "
            "— running fresh probe",
        )
        # Ensure we have a standard chain for the GSSV probe.
        cache_key = await self._resolve_cache_key(token_manager)
        probe_result = await self._run_probe(token_manager)
        if probe_result.ok and probe_result.is_session_fresh():
            self._last_probe = probe_result
            # Side-effect: update the tier cache so get_tier()
            # benefits from this fresh probe too.
            self._store_tier_result(
                cache_key, probe_result.tier,
            )
            self._persist_session(xuid, probe_result)
            logger.info(
                "[MSSubSvc] get_session: fresh probe OK "
                "(gsToken expires in %.0fs)",
                probe_result.expires_at - time.time(),
            )
            return probe_result

        reason = probe_result.error or "no gsToken"
        logger.warning(
            "[MSSubSvc] get_session: probe failed — %s", reason,
        )
        return None

    def _xuid_from_chain(self) -> str:
        """Extract the XUID from the last standard chain, or 'default'."""
        if self._last_standard_chain is not None:
            return self._last_standard_chain.xuid or "default"
        return "default"
    async def invalidate(self) -> None:
        """Invalidate."""
        try:
            self._cache.clear(_CACHE_STORE_NAME)
        except Exception:
            logger.exception("[MSSubSvc] cache clear failed")
        self._last_probe = None
        self._clear_persisted_sessions()
        logger.info("[MSSubSvc] cache invalidated")
