"""Legacy module-level compat facades.

Extracted from ``compatibility/library.py`` to keep that file under
the 550-LOC volumetry cap. These passthroughs preserve the pre-0.7
call shapes for older callers; new code should use
:class:`~unifideck.compatibility.library.CompatLibrary` directly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from .deck_verified import TRACK_DECK
from .library import CompatLibrary

logger = logging.getLogger(__name__)


def load_compat_cache() -> dict[str, Any]:
    """Load compat cache (legacy passthrough — returns empty dict)."""
    logger.debug("[compat] load_compat_cache called via legacy path")
    return {}


def save_compat_cache(cache: dict[str, Any]) -> bool:
    """Save compat cache (legacy passthrough — always succeeds)."""
    logger.debug("[compat] save_compat_cache called via legacy path")
    return True


async def search_steam_store(
    session: Any | None = None,
    title: str = "",
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Search Steam store for ``title`` (legacy passthrough)."""
    from unifideck.steam.library import search_store
    return await search_store(title)


async def fetch_protondb_rating(
    session: Any | None = None,
    appid: int = 0,
    **kwargs: Any,
) -> str | None:
    """Fetch the ProtonDB rating for ``appid`` (legacy passthrough)."""
    lib = CompatLibrary()
    return await lib._fetch_protondb(int(appid))


async def fetch_deck_verified(
    session: Any | None = None,
    appid: int = 0,
    **kwargs: Any,
) -> str:
    """Fetch the Steam Deck verification status for ``appid``.

    Module-level facade — keeps the legacy single-string return
    shape for older callers, and answers for the Deck specifically
    regardless of the host device. New code should use
    :meth:`CompatLibrary._fetch_compat` directly to receive every
    device's rating plus the per-test result entries.
    """
    lib = CompatLibrary()
    tracks = await lib._fetch_compat(appid)
    if tracks is None:          # request failed, not "nothing rated"
        return "unknown"
    return tracks[TRACK_DECK].status


async def get_compat_for_title(
    session: Any | None = None,
    title: str = "",
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Get compat rating for ``title`` (legacy passthrough)."""
    lib = CompatLibrary()
    rating = await lib.get_for_title(title)
    status = "ok" if rating.error is None else rating.error
    return (status, rating.to_dict())


async def prefetch_compat(
    titles: Iterable[str],
    _batch_size: int = 10,
    delay_ms: int = 50,
) -> Any:
    """Prefetch compat ratings for a list of ``titles`` (legacy)."""
    lib = CompatLibrary()
    return await lib.bulk_fetch(list(titles), delay_ms=delay_ms)


class BackgroundCompatFetcher:

    """Background compat fetcher."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the instance."""
        self._lib = CompatLibrary()
    def start(self) -> None:
        """Start the background fetcher (legacy no-op)."""
    def stop(self) -> None:
        """Stop the background fetcher (legacy no-op)."""
    async def fetch(self, title: str) -> Any:
        """Fetch compat rating for ``title``."""
        return await self._lib.get_for_title(title)
