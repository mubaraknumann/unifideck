"""Epic Games library filters — exclude non-game assets.

Epic's owned-products list includes many things that aren't games :
Unreal Engine asset-pack purchases (marketplace), mods, plugins,
free promotional add-ons. Module-level functions exclude these from
the displayed library :

* ``has_ue_namespace(record)``   — True iff the record is in the UE
  namespace (asset packs, plugins, engine builds);
* ``has_asset_category(record)`` — True iff the categories include
  ``"asset"`` (marketplace asset);
* ``has_mod_category(record)``   — True iff the categories include
  ``"mod"`` (community-uploaded mods).
* ``filter_real_games(records)`` — top-level wrapper applying every
  exclusion rule.

Kept stateless (module-level, no class) — each function is a pure
predicate on a record dict. Centralising the exclusion rules here
makes it easy to add new ones as Epic introduces new product types.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

ASSET_CATEGORIES: set[str] = {
    "assets",
    "asset-format",
    "plugins",
    "projects",
}

MOBILE_PLATFORMS: set[str] = {"Android", "iOS"}

def has_ue_namespace(metadata: dict[str, Any]) -> bool:
    """Check whether ue namespace."""
    return metadata.get("namespace") == "ue"

def has_asset_category(metadata: dict[str, Any]) -> bool:
    """Check whether asset category."""
    categories = metadata.get("categories") or []
    for cat in categories:
        path = (cat.get("path") or "").lower()
        if path in ASSET_CATEGORIES:
            return True
    return False

def has_mod_category(metadata: dict[str, Any]) -> bool:
    """Check whether mod category."""
    categories = metadata.get("categories") or []
    return any((cat.get("path") or "").lower() == "mods" for cat in categories)

def is_mobile_only(metadata: dict[str, Any]) -> bool:
    """Check whether mobile only."""
    release_info = metadata.get("releaseInfo") or []
    if not release_info:
        return False
    for info in release_info:
        platforms: Iterable[str] = info.get("platform") or []
        if not platforms:
            return False
        if not all(p in MOBILE_PLATFORMS for p in platforms):
            return False
    return True

def should_filter_epic_item(game_data: dict[str, Any]) -> bool:
    """Check whether filter epic item."""
    metadata = game_data.get("metadata") or {}
    if has_ue_namespace(metadata):
        logger.debug(
            "[epic_filter] UE namespace: %s",
            game_data.get("app_title"),
        )
        return True
    if has_asset_category(metadata):
        logger.debug(
            "[epic_filter] asset category: %s",
            game_data.get("app_title"),
        )
        return True
    if has_mod_category(metadata):
        logger.debug(
            "[epic_filter] mod category: %s",
            game_data.get("app_title"),
        )
        return True
    if is_mobile_only(metadata):
        logger.debug(
            "[epic_filter] mobile-only: %s",
            game_data.get("app_title"),
        )
        return True
    return False
