"""May this sync read the xCloud catalog? The Game Pass subscription gate.

``get_library`` has two kinds of answer, and the sync treats them very
differently (``core/sync_run_mixin.py``):

- a list, even an empty one, is authoritative: the post-sync reconcile
  deletes every xCloud shortcut that is not in it;
- ``None`` means "could not read": every shortcut is kept.

The gate used to return one boolean, so "Microsoft says this account has
no subscription" and "we could not ask" both became an empty library. A
failed probe with no cached tier therefore deleted every xCloud shortcut.
The verdict keeps the two apart.
"""
from __future__ import annotations

import enum
import logging
from typing import TYPE_CHECKING

from unifideck.core.types import Events, SubscriptionTier

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.services.microsoft_subscription import MicrosoftSubscriptionService
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager

logger = logging.getLogger(__name__)


class GateVerdict(enum.Enum):
    """What the subscription gate allows ``get_library`` to say."""

    PROCEED = "proceed"
    """Active subscription: read the catalog."""
    EMPTY = "empty"
    """Microsoft says there is no subscription: the library is empty."""
    UNREADABLE = "unreadable"
    """We could not find out: keep the shortcuts we have."""


async def check_subscription_gate(
    service: MicrosoftSubscriptionService | None,
    tokens: MicrosoftTokenManager,
    bus: EventBus,
) -> GateVerdict:
    """Decide whether the catalog may be read, and tell the user if not."""
    if service is None:
        logger.debug(
            "[MicrosoftStore] no subscription_service wired — skipping "
            "subscription gate (legacy behaviour)",
        )
        return GateVerdict.PROCEED
    try:
        answer = await service.get_tier_checked(tokens)
    except Exception as e:
        logger.warning(
            "[MicrosoftStore] subscription check raised: %s — keeping the "
            "xCloud library as it is", e,
        )
        await bus.emit(
            Events.SYNC_SKIPPED, store="microsoft",
            reason="subscription_check_error",
        )
        return GateVerdict.UNREADABLE
    if not answer.authoritative:
        logger.warning(
            "[MicrosoftStore] could not confirm the Game Pass subscription "
            "with xgpuweb.gssv-play-prod.xboxlive.com — skipping this sync "
            "and keeping the existing xCloud shortcuts",
        )
        await bus.emit(
            Events.SYNC_SKIPPED, store="microsoft",
            reason="subscription_check_error",
        )
        return GateVerdict.UNREADABLE
    if answer.tier == SubscriptionTier.NONE:
        logger.info(
            "[MicrosoftStore] no active xCloud subscription — skipping sync",
        )
        await bus.emit(
            Events.SYNC_SKIPPED, store="microsoft",
            reason="no_active_subscription",
        )
        return GateVerdict.EMPTY
    if answer.tier == SubscriptionTier.ACTIVE_UNKNOWN:
        logger.warning(
            "[MicrosoftStore] subscription active but tier unknown — "
            "skipping sync pending capture data",
        )
        await bus.emit(
            Events.SYNC_SKIPPED, store="microsoft",
            reason="subscription_tier_unknown",
        )
        return GateVerdict.UNREADABLE
    logger.info(
        "[MicrosoftStore] active subscription detected (tier=%s) — "
        "fetching catalog", answer.tier.value,
    )
    return GateVerdict.PROCEED
