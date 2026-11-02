"""The Microsoft store's achievements methods.

A mixin so ``microsoft_store.py`` stays under the file-size cap. It gives
the store the two methods the achievements RPC and watcher call on every
achievement store: ``get_game_achievements`` and ``invalidate_achievements``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from unifideck.utils.locale import get_unifideck_locale

from .achievements import MicrosoftAchievements

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

    from .tokens import MicrosoftTokenManager

#: Used until an xCloud session has told us the account's market. Title ids
#: are the same in every market; this only picks which catalog to ask.
_FALLBACK_MARKET = "US"


class MicrosoftAchievementsMixin:
    """Xbox achievements for :class:`MicrosoftStore` (display + watcher)."""

    _tokens: MicrosoftTokenManager
    _last_market: str | None
    _config_manager: ConfigManager | None
    _achievements: MicrosoftAchievements | None = None

    def _achievements_reader(self) -> MicrosoftAchievements:
        if self._achievements is None:
            self._achievements = MicrosoftAchievements(
                self._tokens,
                market_fn=lambda: self._last_market or _FALLBACK_MARKET,
                locale_fn=lambda: get_unifideck_locale(self._config_manager),
            )
        return self._achievements

    async def get_game_achievements(
        self, game_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """An Xbox game's achievements (definitions + this user's unlocks).

        Raises ``MicrosoftAchievementsError`` on auth/network failure or
        when the game has no Xbox title id; a title with no achievements is
        a normal empty payload. ``force`` bypasses the TTL cache.
        """
        return await self._achievements_reader().get_game_achievements(
            game_id, force=force,
        )

    def invalidate_achievements(self, game_id: str) -> None:
        """Drop a game's cached achievements."""
        if self._achievements is not None:
            self._achievements.invalidate(game_id)
