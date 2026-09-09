from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .constants import _DEFAULT_PROBE_URL

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.microsoft.microsoft_subscription import (
        SubscriptionProbeResult,
    )
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager, XBLTokenChain
logger = logging.getLogger(__name__)
class _ProbeEmissionMixin:
    """Probe emission mixin."""
    _bus: EventBus
    _config: ConfigManager | None
    _last_standard_chain: XBLTokenChain | None
    async def _run_probe(
        self,
        token_manager: MicrosoftTokenManager,
    ) -> SubscriptionProbeResult:
        """Run probe."""
        from unifideck.core.types import SubscriptionTier
        from unifideck.stores.microsoft.microsoft_subscription import (
            SubscriptionProbeResult,
            probe_subscription,
        )
        xbl_token = None
        if self._last_standard_chain is not None:
            xbl_token = self._last_standard_chain.xbl_token
        gssv_chain = await token_manager.build_gssv_chain(
            xbl_token=xbl_token,
        )
        if gssv_chain is None:
            return SubscriptionProbeResult(
                tier=SubscriptionTier.NONE,
                ok=False,
                error="gssv_chain_failed",
            )
        return await probe_subscription(
            user_hash=gssv_chain.user_hash,
            gssv_xsts_token=gssv_chain.xsts_token,
            endpoint_url=self._probe_url(),
        )
    def _probe_url(self) -> str:
        """Probe URL."""
        if self._config is None:
            return _DEFAULT_PROBE_URL
        try:
            raw = self._config.get(
                "stores.microsoft.subscription_check_url",
            )
            return str(raw) if raw else _DEFAULT_PROBE_URL
        except Exception:
            return _DEFAULT_PROBE_URL

    # ``_emit_state_change`` lived here and was retired 2026-08 (audit §1.3).
    # It emitted SUBSCRIPTION_DETECTED / SUBSCRIPTION_EXPIRED on every tier
    # transition, deduped through a ``_last_emitted`` map. Neither event had a
    # consumer on any leg — both were absent from ``src/types/events.ts`` AND
    # from ``WATCHED_EVENTS``, so the frontend never even polled for them, and
    # no Python handler subscribed. The tier that matters to the user already
    # reaches them through SYNC_SKIPPED, which the boot event listener renders.
    # Do not re-add a second channel for the same news.
