"""core/types/enums.py — the small status and taxonomy enums.

Split out of ``events.py`` (which keeps the ``Events`` enum) to hold that
file under the 550-LOC volumetry cap. Like ``Events``, every enum here is a
``StrEnum`` so members serialize straight to JSON. ``StrEnum`` comes from
``events.py``, whose pre-3.11 shim explains why the launcher can import this
chain under an older interpreter.
"""

from __future__ import annotations

from .events import StrEnum


class StoreStatus(StrEnum):
    """Store availability after a status check."""

    UNAVAILABLE = "unavailable"
    NOT_AUTHENTICATED = "not_authenticated"
    AVAILABLE = "available"
    ERROR = "error"


class StoreEnum(StrEnum):
    """Canonical store IDs used as dict keys and frontend routes."""

    EPIC = "epic"
    GOG = "gog"
    AMAZON = "amazon"
    MICROSOFT = "microsoft"
    UBISOFT = "ubisoft"
    BATTLENET = "battlenet"


class OwnershipType(StrEnum):
    """How a game is owned (full purchase vs subscription)."""

    OWNED = "owned"
    SUBSCRIBED = "subscribed"
    TRIAL = "trial"
    UNKNOWN = "unknown"


class GameTag(StrEnum):
    """Filters applied by the UI to group/hide games."""

    NATIVE = "native"
    PROTON = "proton"
    CLOUD = "cloud"
    XCLOUD = "xcloud"
    # Played in an Edge window at ``metadata["browser_url"]``, never
    # installed: xCloud streams and itch.io HTML5 games. The launcher and
    # the Play section both key on it (``launcher/browser_games``).
    BROWSER = "browser"
    # Paid for but not yet claimed on the store's own site (an itch.io
    # bundle game). Install opens the claim page first; the next sync after
    # the claim drops the tag.
    CLAIM_REQUIRED = "claim_required"
    # A Microsoft title that Unifideck can install and run on this device
    # through its own Xodus build (owned or Game Pass). A fact about the
    # title: every installed row carries it, even with the feature off, so
    # the row stays playable and uninstallable. See ``launcher/xbox_pc``.
    XBOX_PC = "xbox_pc"
    DLC = "dlc"
    BETA = "beta"
    DEMO = "demo"
    HIDDEN = "hidden"


class ErrorCode(StrEnum):
    """Normalized error codes across stores.

    Store connectors convert their raw errors (HTTP status,
    subprocess exit code, API string) into one of these values so
    the frontend can match on stable identifiers instead of
    parsing free-form messages.
    """

    NOT_AUTHENTICATED = "not_authenticated"
    TOKEN_EXPIRED = "token_expired"  # noqa: S105 — event name constant, not a credential
    NETWORK_ERROR = "network_error"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    INSUFFICIENT_SPACE = "insufficient_space"
    BINARY_MISSING = "binary_missing"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class SubscriptionTier(StrEnum):
    """Subscription tier for stores whose catalog depends on a paid plan.

    Sprint 18e scope is Microsoft / Xbox Game Pass. The three paid
    tiers (Essential, Premium, Ultimate) are listed for forward
    compatibility — the current implementation can only discriminate
    NONE vs. any-active until real probe responses from each tier
    are captured and parsed (Sprint 18f).

    ACTIVE_UNKNOWN is the conservative bucket for "the probe responded
    200 OK but couldn't parse a tier marker". Callers treat it as
    "skip the sync" (Sprint 18e Q1 decision) to avoid showing users
    games they can't actually stream.

    FREE is an xCloud session with no subscription (the ``xgpuwebf2p``
    offering). Its catalog holds only the titles Microsoft streams for
    free, such as Fortnite, so the sync reads it like a paid tier.

    The enum inherits from str so members serialize directly to JSON
    for EventBus payloads: json.dumps(SubscriptionTier.ULTIMATE)
    produces "ultimate", which is what the frontend expects.
    """

    NONE = "none"
    FREE = "free"
    ESSENTIAL = "essential"
    PREMIUM = "premium"
    ULTIMATE = "ultimate"
    ACTIVE_UNKNOWN = "active_unknown"
