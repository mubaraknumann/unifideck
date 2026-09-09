"""Locks the sync-cache TTL policy.

Standard sync must only fetch missing/new data day-to-day; the old
1-day ``steam_metadata`` / 7-day ``metadata`` TTLs made any sync past
the window silently re-pull the whole library (the "sync takes 10+
minutes again a day later" reports). Enrichment caches now expire
monthly — often enough for negative markers to self-heal, rare enough
that expiry-driven re-pulls stop being a daily event. Force sync is
the immediate-refresh path.
"""
from __future__ import annotations

from unifideck.bootstrap.cache_registry import _NAMED_CACHES

_THIRTY_DAYS = 30 * 24 * 3600


def _ttls() -> dict[str, int]:
    return dict(_NAMED_CACHES)


def test_enrichment_caches_expire_monthly() -> None:
    ttls = _ttls()
    assert ttls["metadata"] == _THIRTY_DAYS
    assert ttls["steam_metadata"] == _THIRTY_DAYS
    assert ttls["steam_reviews"] == _THIRTY_DAYS
    assert ttls["unifidb_metadata"] == _THIRTY_DAYS


def test_identity_caches_never_expire() -> None:
    ttls = _ttls()
    # shortcut → real-AppID mapping and the Date-Added stamp must
    # never expire (a game would look "newly added" again).
    assert ttls["steam_real_appid"] == 0
    assert ttls["shortcut_added"] == 0
    assert ttls["compat"] == 0
    assert ttls["artwork_attempts"] == 0
    # The title a ``steam_real_appid`` miss was searched under. Must share
    # that cache's lifetime: expiring it alone would make every cached miss
    # look title-less, i.e. stale, and re-search the whole library.
    assert ttls["steam_appid_miss"] == 0
