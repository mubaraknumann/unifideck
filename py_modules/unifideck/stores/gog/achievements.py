"""stores/gog/achievements.py — GOG achievements (read + normalize).

``GOGAchievements`` fetches a game's achievement list *and* the user's
unlock status from GOG's Galaxy backend (``gameplay.gog.com``) — the same
data Heroic shows, and the same host Comet uploads unlocks to at runtime.
It is read-only: the actual *unlocking* happens in-game via Comet (see
``launcher/proton/compat/gog.py``); this just reads back what was earned.

Pipeline per game (all blocking I/O on a worker thread):
  manifest → ``(clientId, clientSecret)`` → game-scoped token exchange →
  paginated ``GET …/clients/{clientId}/users/{userId}/achievements``.

Results are TTL-cached (achievement *definitions* never change; only the
unlock status does, after play) and the cache is dropped on game-stop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.error import HTTPError, URLError

from unifideck.stores.shared.achievements_error import (
    StoreAchievementsError,
)
from unifideck.stores.shared.timestamps import parse_timestamp

from .galaxy_api import (
    exchange_game_token,
    fetch_achievements_page,
    fetch_gog_client_creds,
)

if TYPE_CHECKING:
    from .tokens import GOGTokenManager

logger = logging.getLogger(__name__)

# Cache TTL — long enough that paging/reopening the modal in a session is
# instant, short enough that it self-heals. Modal-scoped, not render-hot,
# so 60s (vs the 5s game-info cache) avoids hammering the paginated endpoint.
_CACHE_TTL_SECONDS = 60.0
# Hard bound on the pagination loop so a malformed page_token can't spin.
_MAX_PAGES = 50


class GOGAchievementsError(StoreAchievementsError):
    """A typed GOG achievements failure the RPC layer maps to an ``RpcError``.

    ``code`` is one of ``offline`` / ``auth_expired`` / ``no_client_id`` /
    ``not_authed``; ``context`` carries extra fields for the error envelope.
    """


class GOGAchievements:
    """Fetch + normalize + cache a GOG game's achievements."""

    def __init__(self, tokens: GOGTokenManager) -> None:
        """Initialize the instance."""
        self._tokens = tokens
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def get_game_achievements(
        self, game_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """Return ``{store, game_id, total, unlocked, percent, achievements}``.

        ``force`` bypasses the TTL cache (used by the manual refresh and the
        post-play reconcile). Raises :class:`GOGAchievementsError` on auth /
        network / no-client-id failures; a game with zero achievements is a
        normal (non-error) empty payload.
        """
        key = str(game_id)
        if not force:
            cached = self._get_cached(key)
            if cached is not None:
                return cached

        # Tokens are lazy-loaded; ensure they're present + fresh before use.
        if not self._tokens.has_tokens:
            await self._tokens.load()
        await self._tokens.refresh_if_stale()
        refresh_token = self._tokens.refresh_token
        user_id = self._tokens.user_info.galaxy_user_id
        if not refresh_token or not user_id:
            raise GOGAchievementsError("not_authed")

        payload = await asyncio.to_thread(
            self._fetch_blocking, key, refresh_token, user_id,
        )
        self._cache[key] = (time.monotonic(), payload)
        return payload

    def invalidate(self, game_id: str) -> None:
        """Drop a game's cached achievements (e.g. on game-stop)."""
        self._cache.pop(str(game_id), None)

    @staticmethod
    def unlocked_keys(payload: dict[str, Any]) -> set[str]:
        """The set of unlocked achievement keys in a payload (for diffing)."""
        return {
            str(a.get("key"))
            for a in payload.get("achievements", [])
            if a.get("unlocked")
        }

    # -- internals ---------------------------------------------------------

    def _get_cached(self, key: str) -> dict[str, Any] | None:
        """Cached."""
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL_SECONDS:
            return entry[1]
        return None

    def _fetch_blocking(
        self, game_id: str, refresh_token: str, user_id: str,
    ) -> dict[str, Any]:
        """The blocking creds → token → paginated-GET pipeline (worker thread)."""
        try:
            client_id, client_secret = fetch_gog_client_creds(game_id)
        except (URLError, OSError, TimeoutError) as e:
            raise GOGAchievementsError("offline") from e
        if not client_id or not client_secret:
            raise GOGAchievementsError("no_client_id", game_id=game_id)
        token = _exchange_token(client_id, client_secret, refresh_token)
        items = _fetch_all_pages(client_id, user_id, token)
        return self._build_payload(game_id, items)

    @staticmethod
    def _build_payload(
        game_id: str, items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Normalize + sort + summarize the raw GOG achievement items."""
        normalized = [
            GOGAchievements._normalize(it)
            for it in items
            if isinstance(it, dict)
        ]
        # Unlocked first (most-recent unlock on top); locked keep GOG's
        # native order (the developer's intended display order). Python's
        # sort is stable, so the locked group's order is preserved.
        unlocked = [a for a in normalized if a["unlocked"]]
        locked = [a for a in normalized if not a["unlocked"]]
        unlocked.sort(key=lambda a: a["unlocked_at"] or 0.0, reverse=True)
        ordered = unlocked + locked
        total = len(ordered)
        n_unlocked = len(unlocked)
        return {
            "store": "gog",
            "game_id": str(game_id),
            "total": total,
            "unlocked": n_unlocked,
            "percent": round(n_unlocked / total * 100, 1) if total else 0.0,
            "achievements": ordered,
        }

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        """One GOG achievement item → the frontend-facing shape."""
        date_unlocked = item.get("date_unlocked")
        image_unlocked = str(item.get("image_url_unlocked") or "")
        return {
            "key": str(
                item.get("achievement_key")
                or item.get("achievement_id")
                or "",
            ),
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
            "image_unlocked": image_unlocked,
            "image_locked": str(item.get("image_url_locked") or image_unlocked),
            "hidden": not bool(item.get("visible", True)),
            "unlocked": date_unlocked is not None,
            "unlocked_at": parse_timestamp(date_unlocked),
            "rarity": item.get("rarity"),
        }


def _raise_gog_http(e: HTTPError) -> NoReturn:
    """Translate a GOG HTTP error: 401/403 → auth_expired, else offline."""
    if e.code in (401, 403):
        raise GOGAchievementsError("auth_expired") from e
    raise GOGAchievementsError("offline") from e


def _exchange_token(
    client_id: str, client_secret: str, refresh_token: str,
) -> str:
    """Exchange creds for a game-scoped token (worker thread).

    Raises :class:`GOGAchievementsError` on HTTP/network failure or an empty
    token.
    """
    try:
        token = exchange_game_token(client_id, client_secret, refresh_token)
    except HTTPError as e:
        _raise_gog_http(e)
    except (URLError, OSError, TimeoutError) as e:
        raise GOGAchievementsError("offline") from e
    if not token:
        raise GOGAchievementsError("auth_expired")
    return token


def _fetch_all_pages(
    client_id: str, user_id: str, token: str,
) -> list[dict[str, Any]]:
    """Accumulate achievement items across the paginated endpoint.

    Bounded by ``_MAX_PAGES``; GOG terminates with page_token ``"0"`` (string),
    not empty. Raises :class:`GOGAchievementsError` on HTTP/network failure.
    """
    items: list[dict[str, Any]] = []
    page: str | None = None
    try:
        for _ in range(_MAX_PAGES):
            resp = fetch_achievements_page(client_id, user_id, token, page)
            items.extend(resp.get("items") or [])
            total = int(resp.get("total_count") or 0)
            page = resp.get("page_token")
            if not page or page == "0" or len(items) >= total:
                break
    except HTTPError as e:
        _raise_gog_http(e)
    except (URLError, OSError, TimeoutError) as e:
        raise GOGAchievementsError("offline") from e
    return items
