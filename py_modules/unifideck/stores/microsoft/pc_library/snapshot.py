"""The last good Xbox PC list, kept across syncs and restarts.

Cache namespace ``microsoft_xbox_pc``. Its registry TTL must stay 0: a
positive TTL evicts on read, which would erase the last good list exactly
when the CLI cannot be read. Freshness is stored inside instead. Two kinds
of key::

    "library"        {"v": 1, "account_hash", "market", "checked_at", "clean",
                      "skipped", "titles": [meta, ...],
                      "ids": {UPPER product id: store_game_id},
                      "notices": [SYNC_SKIPPED reason, ...]}
    "plans:<market>" {"<root product id>": {"at": epoch s, ...summary}}

Plans are kept 24 hours. Only answered products are cached: an unknown or
absent product is fetched again next time. The account is kept only as a
hash.
"""
from __future__ import annotations

import logging
from typing import Any

from .rows import PcTitle

logger = logging.getLogger(__name__)

CACHE_NAME = "microsoft_xbox_pc"
_LIBRARY_KEY = "library"
_PLANS_PREFIX = "plans:"
_VERSION = 1
PLAN_TTL_S = 24 * 3600


def _empty() -> dict[str, Any]:
    return {
        "v": _VERSION, "account_hash": None, "market": None, "checked_at": None,
        "clean": False, "skipped": 0, "titles": [], "ids": {}, "notices": [],
    }


class XboxPcSnapshot:
    """Read and write the namespace. Cache errors are logged, never raised."""

    def __init__(self, cache: Any) -> None:
        self._cache = cache
        self._library: dict[str, Any] | None = None

    # ── library ──────────────────────────────────────────────────────────
    def library(self) -> dict[str, Any]:
        """The stored library entry (loaded once), or an empty one."""
        if self._library is None:
            self._library = self._load()
        return self._library

    def _load(self) -> dict[str, Any]:
        try:
            raw = self._cache.get(CACHE_NAME, _LIBRARY_KEY)
        except Exception as e:
            logger.warning("[XboxPcSnapshot] could not read the last Xbox PC list: %s", e)
            return _empty()
        if not isinstance(raw, dict) or raw.get("v") != _VERSION:
            return _empty()
        data = _empty()
        data.update({k: raw[k] for k in data if k in raw})
        if not isinstance(data["titles"], list) or not isinstance(data["ids"], dict):
            return _empty()
        if not isinstance(data["notices"], list):
            data["notices"] = []
        return data

    def titles(self) -> list[PcTitle]:
        """The last good titles."""
        out = (PcTitle.from_meta(m) for m in self.library()["titles"])
        return [t for t in out if t is not None]

    def ids(self) -> dict[str, str]:
        """The remembered ``store_game_id`` per upper-case product id."""
        return dict(self.library()["ids"])

    def update(self, **fields: Any) -> None:
        """Change fields of the library entry and persist it."""
        data = self.library()
        data.update(fields)
        self._save(_LIBRARY_KEY, data)

    def clear(self) -> None:
        """Forget the list and the ids (explicit sign-out or a new account)."""
        self._library = _empty()
        try:
            self._cache.delete(CACHE_NAME, _LIBRARY_KEY)
        except Exception as e:
            logger.warning("[XboxPcSnapshot] could not clear the last Xbox PC list: %s", e)

    # ── notices ──────────────────────────────────────────────────────────
    def has_notice(self, reason: str) -> bool:
        return reason in self.library()["notices"]

    def add_notice(self, reason: str) -> None:
        notices = list(self.library()["notices"])
        if reason not in notices:
            self.update(notices=[*notices, reason])

    def drop_notices(self, *reasons: str) -> None:
        notices = [n for n in self.library()["notices"] if n not in reasons]
        if notices != self.library()["notices"]:
            self.update(notices=notices)

    # ── plans ────────────────────────────────────────────────────────────
    def plans(self, market: str, now: float) -> dict[str, dict[str, Any]]:
        """The plan summaries of ``market`` cached less than 24 h ago."""
        try:
            raw = self._cache.get(CACHE_NAME, _PLANS_PREFIX + market)
        except Exception as e:
            logger.warning("[XboxPcSnapshot] could not read cached plans: %s", e)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            pid: entry for pid, entry in raw.items()
            if isinstance(entry, dict) and isinstance(entry.get("at"), (int, float))
            and 0 <= now - entry["at"] < PLAN_TTL_S
        }

    def save_plans(self, market: str, fresh: dict[str, dict[str, Any]], now: float) -> None:
        """Add ``fresh`` summaries to ``market``'s cache, dropping stale ones."""
        if not fresh:
            return
        kept = self.plans(market, now)
        kept.update({pid: {**summary, "at": now} for pid, summary in fresh.items()})
        self._save(_PLANS_PREFIX + market, kept)

    def _save(self, key: str, value: Any) -> None:
        try:
            self._cache.set(CACHE_NAME, key, value)
        except Exception as e:
            logger.warning("[XboxPcSnapshot] could not save %s: %s", key, e)
