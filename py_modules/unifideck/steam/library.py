"""steam/library.py — Steam install discovery + Steam Store search."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from unifideck.steam.http_retry import STEAM_STORE_GATE, get_json_with_backoff
from unifideck.utils.config_helpers import get_cfg
from unifideck.utils.title_match import (
    normalize_for_match,
    strip_edition_suffix,
    strip_platform_suffix,
    titles_match,
)
from unifideck.utils.vdf_compat import (
    STEAM_ROOT_CANDIDATES,
    resolve_live_steam_root,
)

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

# Single source of truth shared with the launcher + ``defaults/config.json``
# (``paths.steam_candidates``) so the write paths and Proton resolution agree
# on where Steam can live across SteamOS / Bazzite / CachyOS / Flatpak.
STEAM_PATH_CANDIDATES = STEAM_ROOT_CANDIDATES
STEAM_STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch"

_HTTP_OK = 200
_DEFAULT_TIMEOUT = 10.0


def _cfg(config: ConfigManager | None, key: str, default: Any) -> Any:
    """Cfg."""
    return get_cfg(config, key, default)


def find_steam_path(config: ConfigManager | None = None) -> str | None:
    """Find steam path.

    Honours an optional ``paths.steam_root`` config override, then any
    ``paths.steam_candidates`` list from config (advertised in
    ``defaults/config.json`` but previously never read), then the built-in
    cross-distro candidates. Returns the directory string on success, or
    ``None`` when no Steam install is detectable.

    A root counts only if it has a ``steamapps/`` dir. When several distinct
    installs qualify (e.g. a stale native ``~/.steam/steam`` alongside a
    running Flatpak Steam), ``resolve_live_steam_root`` picks the most
    recently active one — writing shortcuts to a root Steam never reads was
    the "synced but nothing shows in Steam" bug. The explicit
    ``paths.steam_root`` override still wins outright.
    """
    if config is not None:
        override = _cfg(config, "paths.steam_root", None)
        if override:
            full = str(Path(str(override)).expanduser())
            if (Path(full) / "steamapps").is_dir():
                return full
    candidates: list[str] = []
    if config is not None:
        configured = _cfg(config, "paths.steam_candidates", None)
        if isinstance(configured, (list, tuple)):
            candidates.extend(str(c) for c in configured)
    candidates.extend(STEAM_PATH_CANDIDATES)
    live = resolve_live_steam_root(candidates)
    return str(live) if live is not None else None


# NOTE: the per-user path resolvers that used to live here
# (``_find_most_recent_user`` / ``find_grid_path`` / ``find_shortcuts_vdf``)
# were removed — they duplicated an unvalidated directory-mtime heuristic and
# had no callers. Active-user + per-user path resolution is now owned solely by
# :mod:`unifideck.steam.current_user`.


@dataclass
class SteamStoreResult:
    """Steam store result."""

    app_id: int
    name: str
    header_image: str
    price: str
    release_date: str

    def to_dict(self) -> dict[str, Any]:
        """To dict."""
        return asdict(self)


def _format_price(price_block: Any) -> str:
    """Format the Steam Store price block to a display string."""
    if not isinstance(price_block, dict):
        return ""
    final = price_block.get("final")
    if not isinstance(final, int):
        return ""
    if final == 0:
        return "Free"
    currency = price_block.get("currency", "")
    formatted = f"{final / 100:.2f}"
    return f"{formatted} {currency}".strip()


async def _storesearch_items(
    term: str,
    timeout_s: float,
    session: aiohttp.ClientSession | None = None,
) -> list[dict[str, Any]]:
    """Hit Steam's ``storesearch`` endpoint and return the raw item list.

    ``ssl=False`` for the same SteamOS cert reason every Steam HTTP path
    here uses. Handles HTTP 429 Rate Limiting with backoff.
    """
    if not term:
        return []
    params = {"term": term, "l": "english", "cc": "us"}
    if session is not None:
        return await _request_storesearch(session, term, params, timeout_s)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session_new:
        return await _request_storesearch(session_new, term, params, timeout_s)


async def _request_storesearch(
    sess: aiohttp.ClientSession,
    term: str,
    params: dict[str, str],
    timeout_s: float,
) -> list[dict[str, Any]]:
    """GET Steam's ``storesearch`` on ``sess`` with HTTP 429 backoff.

    Retries via :func:`get_json_with_backoff` behind the shared
    ``STEAM_STORE_GATE``. Returns the raw item list, or ``[]`` on a
    non-OK status or transport error.
    """
    data = await get_json_with_backoff(
        sess,
        STEAM_STORE_SEARCH_URL,
        params=params,
        timeout_s=timeout_s,
        log_tag=f"[steam.search_store] {term!r}",
        gate=STEAM_STORE_GATE,
    )
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


async def _retry_cleaned(
    title: str, timeout_s: float, session: aiohttp.ClientSession | None,
) -> tuple[dict[str, Any], int] | None:
    """Search again without platform words, then without edition words.

    Steam's search finds nothing for "DOOM Eternal Standard Edition (PC)"
    and finds the game for "doom eternal". Platform words go first, alone,
    so an edition that is its own Steam game ("Mafia: Definitive Edition")
    is searched before its bare franchise. Every hit still has to pass
    ``titles_match`` against the full title.
    """
    normalized = normalize_for_match(title)
    tried = {normalized}
    for query in (strip_platform_suffix(normalized), strip_edition_suffix(normalized)):
        if not query or query in tried:
            continue
        tried.add(query)
        match = _pick_store_match(
            title, await _storesearch_items(query, timeout_s, session),
        )
        if match is not None:
            return match
    return None


def _pick_store_match(
    query: str, items: list[dict[str, Any]],
) -> tuple[dict[str, Any], int] | None:
    """First storesearch item whose name passes ``titles_match(query, …)``."""
    for candidate in items:
        try:
            cid = int(candidate.get("id", 0))
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        if titles_match(query, str(candidate.get("name", ""))):
            return candidate, cid
    return None


async def search_store(
    title: str,
    config: ConfigManager | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any] | None:
    """Resolve ``title`` to its Steam Store entry via ``storesearch``.

    Returns the validated top match (``app_id``, ``name``,
    ``header_image``, ``price``, ``release_date``) or ``None``.
    """
    if not title:
        return None
    timeout_s = float(_cfg(config, "network.steam_store_timeout", _DEFAULT_TIMEOUT))

    match = _pick_store_match(
        title, await _storesearch_items(title, timeout_s, session),
    )
    if match is None:
        match = await _retry_cleaned(title, timeout_s, session)
    if match is None:
        return None

    item, app_id = match
    header_image = (
        f"https://cdn.akamai.steamstatic.com/steam/apps/{app_id}/header.jpg"
    )
    return SteamStoreResult(
        app_id=app_id,
        name=str(item.get("name", "")),
        header_image=header_image,
        price=_format_price(item.get("price")),
        release_date=str(item.get("released", "")),
    ).to_dict()


async def batch_search_store(
    titles: list[str],
    session: aiohttp.ClientSession | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Batch search store."""
    if not titles:
        return {}
    results = await asyncio.gather(
        *(search_store(t, session=session) for t in titles),
        return_exceptions=False,
    )
    return dict(zip(titles, results, strict=False))
