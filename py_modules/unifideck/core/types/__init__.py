"""Typed records — enums, dataclasses, ``Result`` family.

Re-exports the public typed surface from the three sibling
modules:

* ``domain``  — domain dataclasses (``Game``, ``StoreInfo``,
  ``CLITool``);
* ``events``  — ``Events`` enum (bus event names) plus the
  smaller enums (``StoreEnum``, ``StoreStatus``,
  ``OwnershipType``, ``SubscriptionTier``, ``GameTag``,
  ``ErrorCode``);
* ``results`` — the ``Result`` family (one per concern) plus
  the typed ``StoreError`` exception hierarchy.

Everything imported here is part of the cross-package public
contract; new types should be re-exported here so consumers can
``from unifideck.core.types import ...`` without knowing the
internal module split.
"""

from __future__ import annotations

from .domain import (
    CLITool,
    Game,
    StoreInfo,
    SyncRequest,
)
from .events import (
    ErrorCode,
    Events,
    GameTag,
    OwnershipType,
    StoreEnum,
    StoreStatus,
    SubscriptionTier,
)
from .results import (
    AccountResult,
    ArtworkResult,
    AuthResult,
    CloudSaveResult,
    DownloadResult,
    InstallResult,
    MetadataResult,
    PlaytimeResult,
    Result,
    StoreAuthError,
    StoreDownloadError,
    StoreError,
    StoreSyncError,
    SyncResult,
)

__all__ = [
    "AccountResult",
    "ArtworkResult",
    "AuthResult",
    "CLITool",
    "CloudSaveResult",
    "DownloadResult",
    "ErrorCode",
    "Events",
    "Game",
    "GameTag",
    "InstallResult",
    "MetadataResult",
    "OwnershipType",
    "PlaytimeResult",
    "Result",
    "StoreAuthError",
    "StoreDownloadError",
    "StoreEnum",
    "StoreError",
    "StoreInfo",
    "StoreStatus",
    "StoreSyncError",
    "SubscriptionTier",
    "SyncRequest",
    "SyncResult",
]
