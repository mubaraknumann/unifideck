"""The shortcut-AppID → real-Steam-AppID cache, read in one place.

``MetadataService.fetch_appdetails_for_game`` and
``CompatLibrary._persist_steam_id`` both write this namespace keyed on
``str(game.app_id)`` — the **signed** 32-bit form the sync layer stores.
Steam's frontend hands plugins the **unsigned** form via
``overview.appid``, so a reader that tries only one form is reachable
from only one side. :func:`unifideck.core.compat_bridge.appid_candidates`
exists for exactly that, and this module is its main consumer.

Two backfill services each held a byte-identical private copy of this read
that tried the signed form alone (check 13, audit register item 47). That
was correct for their callers — both pass ``Game.app_id``, which is signed
— so it was a robustness gap rather than a live defect. Routing them here
closes the gap in passing.

**The return contract is the reason this is one function and not five.**
Three other readers of the same namespace are deliberately *not* folded in:

* ``compatibility/library.py`` and ``rpc/mixins/_metadata_display.py``
  preserve the ``-1`` sentinel ("this title has no Steam counterpart"),
  which the sync partition reads to skip the game on the next run. This
  function collapses ``-1`` to ``0``, because its two callers ask "is
  there an AppID to look up", where the sentinel and a miss mean the
  same thing.
* Those readers, and ``rpc/mixins/store.py``, reach into the cache's
  private ``_stores``/``_data`` rather than calling ``get``, to mirror
  the visible behaviour of ``get_steam_metadata_cache``. For *this*
  namespace the two are equivalent — ``bootstrap/cache_registry.py``
  registers it with ``ttl=0``, which never expires — but that equivalence
  is a property of the registration, not of the API, so it is not a
  reason to rewrite readers whose contract already differs.

A drift finding names a difference, not a direction (audit §3.2): the
difference here is real and only two of the five sides share a contract.
"""
from __future__ import annotations

from typing import Any

from unifideck.core.compat_bridge import appid_candidates

#: Cache namespace. Owned here rather than re-declared per caller: the
#: backfill services deliberately re-declare the namespaces they read
#: directly ("the wire-level cache layout is the boundary; the constant
#: names are not"), and that argument stops applying once the read itself
#: is the boundary.
STEAM_REAL_APPID_NS = "steam_real_appid"


def read_positive_steam_appid(cache: Any, shortcut_app_id: int | None) -> int:
    """The real Steam AppID mapped to *shortcut_app_id*, or ``0``.

    Returns ``0`` for a missing mapping, an unusable cache, the ``-1``
    "no Steam counterpart" sentinel, and any non-integer value — every
    case in which there is nothing to look up.

    Args:
        cache: the ``CacheManager``; ``None`` and a raising cache both
            yield ``0``, since a backfill must never fail on a cold or
            broken cache.
        shortcut_app_id: the shortcut's AppID in either 32-bit form.
    """
    if shortcut_app_id is None or cache is None:
        return 0
    for key in appid_candidates(shortcut_app_id):
        try:
            value = cache.get(STEAM_REAL_APPID_NS, key)
        except Exception:
            return 0
        if isinstance(value, int) and value > 0:
            return value
    return 0
