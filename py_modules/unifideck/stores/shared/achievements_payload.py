"""The achievements payload every store returns, built one way.

``get_game_achievements`` answers the same shape for every store, so the
game page, the full list and the watcher never branch on the store::

    {store, game_id, total, unlocked, percent, achievements: [...]}

Each achievement is ``{key, name, description, image_unlocked,
image_locked, hidden, unlocked, unlocked_at, rarity}``, plus optional
store extras (``gamerscore`` on Xbox).

GOG and Epic each carried a byte-identical copy of the ordering and
summary code and of a 60 s cache. Both live here now, and Xbox uses them
too.
"""
from __future__ import annotations

import time
from typing import Any

#: Long enough that reopening the page or the full list is instant, short
#: enough that a fresh unlock shows up soon after play.
CACHE_TTL_SECONDS = 60.0


def build_payload(
    store: str, game_id: str, normalized: list[dict[str, Any]],
) -> dict[str, Any]:
    """Order normalized achievements and add the summary fields.

    Unlocked ones come first, most recent unlock on top. Locked ones keep
    the store's order, which is the developer's display order (the sort is
    stable).
    """
    unlocked = [a for a in normalized if a["unlocked"]]
    locked = [a for a in normalized if not a["unlocked"]]
    unlocked.sort(key=lambda a: a["unlocked_at"] or 0.0, reverse=True)
    total = len(unlocked) + len(locked)
    return {
        "store": store,
        "game_id": str(game_id),
        "total": total,
        "unlocked": len(unlocked),
        "percent": round(len(unlocked) / total * 100, 1) if total else 0.0,
        "achievements": unlocked + locked,
    }


def unlocked_keys(payload: dict[str, Any]) -> set[str]:
    """The keys of the unlocked achievements in a payload (for diffing)."""
    return {
        str(a.get("key"))
        for a in payload.get("achievements", [])
        if a.get("unlocked")
    }


class TtlCache:
    """Payloads by game id, each valid for ``ttl`` seconds."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS) -> None:
        """Initialize the instance."""
        self._ttl = ttl
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, game_id: str) -> dict[str, Any] | None:
        """The cached payload, or None when absent or older than the TTL."""
        entry = self._entries.get(str(game_id))
        if entry and (time.monotonic() - entry[0]) < self._ttl:
            return entry[1]
        return None

    def put(self, game_id: str, payload: dict[str, Any]) -> None:
        """Store a fresh payload."""
        self._entries[str(game_id)] = (time.monotonic(), payload)

    def invalidate(self, game_id: str) -> None:
        """Drop one game."""
        self._entries.pop(str(game_id), None)

    def clear(self) -> None:
        """Drop every game (sign-out)."""
        self._entries.clear()
