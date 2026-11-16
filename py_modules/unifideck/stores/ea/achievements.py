"""stores/ea/achievements.py — EA achievements (read + normalize).

``maxima-cli achievements`` (unifideck-maxima patch ``unifideck-achievements``)
asks EA's service layer for a game's achievement sets and this account's
progress: name, description, icons, ``awarded`` and ``awarded_at``. Read-only:
unlocking happens in the game, which reports to EA itself.

EA masks hidden achievements on its side (placeholder name, icon and text), so
they need no masking here. It has no rarity.
"""
from __future__ import annotations

import logging
from typing import Any

from unifideck.stores.shared.achievements_error import StoreAchievementsError
from unifideck.stores.shared.achievements_payload import TtlCache, build_payload
from unifideck.stores.shared.timestamps import parse_timestamp

from .cli import MaximaCli, MaximaCliError
from .ids import offer_id

logger = logging.getLogger(__name__)

#: EA serves each icon at 40, 208 and 416 px; 208 fits the list and the page.
_ICON_SIZE = "-208."


class EAAchievementsError(StoreAchievementsError):
    """A typed EA achievements failure: ``not_authed``, ``offline`` or ``ea_unavailable``."""


class EAAchievements:
    """Fetch, normalize and cache an EA game's achievements."""

    def __init__(self, cli: MaximaCli) -> None:
        """Initialize the instance."""
        self._cli = cli
        self._cache = TtlCache()

    async def get_game_achievements(
        self, game_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """Return ``{store, game_id, total, unlocked, percent, achievements}``.

        A game without EA achievements is an empty payload, not an error.
        """
        key = str(game_id)
        if not force:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        try:
            items = await self._cli.achievements(offer_id(key))
        except MaximaCliError as e:
            raise EAAchievementsError(_error_code(e)) from e
        payload = build_payload("ea", key, [normalize_ea_achievement(i) for i in items])
        self._cache.put(key, payload)
        return payload

    def invalidate(self, game_id: str) -> None:
        """Drop a game's cached achievements (e.g. on game-stop)."""
        self._cache.invalidate(game_id)

    def clear(self) -> None:
        """Drop every game (sign-out)."""
        self._cache.clear()


def _error_code(error: MaximaCliError) -> str:
    """The wire code the achievements modal switches on."""
    if error.code == "not_signed_in":
        return "not_authed"
    if error.code == "timeout":
        return "offline"
    return "ea_unavailable"


def _icon(icons: list[str]) -> str:
    """The mid-size icon, else the largest EA sent."""
    for icon in icons:
        if _ICON_SIZE in icon:
            return icon
    return icons[-1] if icons else ""


def normalize_ea_achievement(item: dict[str, Any]) -> dict[str, Any]:
    """One ``maxima-cli achievements`` entry → the frontend-facing shape."""
    icons = [i for i in item.get("icons") or [] if isinstance(i, str) and i]
    image = _icon(icons)
    awarded = bool(item.get("awarded"))
    return {
        "key": str(item.get("id") or ""),
        "name": str(item.get("name") or ""),
        # Some achievements carry only the how-to text
        "description": str(item.get("description") or item.get("how_to") or ""),
        "image_unlocked": image,
        "image_locked": image,
        "hidden": bool(item.get("hidden")),
        "unlocked": awarded,
        "unlocked_at": parse_timestamp(item.get("awarded_at")) if awarded else None,
        "rarity": None,
    }
