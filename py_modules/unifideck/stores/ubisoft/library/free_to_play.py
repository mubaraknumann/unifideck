"""
Ubisoft free-to-play catalog feed — download, cache, normalise.

Ported (and trimmed) from staging's ``_fetch_free_to_play_manifest_entries``
(staging ``ubisoft.py`` ~L1302-1360). Staging fetched the public CDN feed
*and then* enriched each entry with an authenticated space-metadata call.
The refactor has no REST API and treats local files as the sole data
source, so only the **public, auth-free** CDN feed is used here — title,
space_id, product id (the ``uplay://`` deeplink id) and cover are all
present in that payload.

The feed is a *supplement*: it labels owned F2P titles (``ownership_type
= "free"`` + cover art) and can surface F2P games the ownership binary
doesn't list. It is TTL-cached and degrades to an empty list on any
network/parse failure — never an exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.net import ssl_ctx_permissive

if TYPE_CHECKING:
    from unifideck.stores.ubisoft.config import UbisoftConfig

logger = logging.getLogger(__name__)

_LATEST_URL = "https://static3.cdn.ubi.com/orbit/uplay_launcher_14_0/free_games/latest.txt"
_CONFIGS_URL = (
    "https://static3.cdn.ubi.com/orbit/uplay_launcher_14_0/"
    "free_games/{version}/free_game_configs.json"
)
# F2P catalogue changes more often than the static game-ID DB; refresh daily.
_CACHE_MAX_AGE_SECONDS = 24 * 3600
_CACHE_FILENAME = "ubisoft_free_games.json"
_HTTP_TIMEOUT = 30.0

class _FreeToPlayFeed:
    """Fetches Ubisoft's public free-to-play catalogue as manifest entries."""

    def __init__(self, config: UbisoftConfig) -> None:
        """Initialize the instance."""
        self._config = config

    async def fetch_entries(self) -> list[dict[str, Any]]:
        """Return normalised F2P manifest entries (``[]`` on any failure).

        Cached on disk for :data:`_CACHE_MAX_AGE_SECONDS`; a stale cache is
        preferred to a hard failure when the network is unavailable.
        """
        cache_file = Path(self._config.data_dir_expanded) / _CACHE_FILENAME
        cached = await asyncio.to_thread(self._read_fresh_cache, cache_file)
        if cached is not None:
            return cached
        try:
            entries = await asyncio.to_thread(self._download_entries, cache_file)
        except Exception as e:
            logger.warning("[UbisoftLibrary] free-to-play feed failed: %s", e)
            return await asyncio.to_thread(self._read_any_cache, cache_file)
        return entries

    @staticmethod
    def _read_fresh_cache(cache_file: Path) -> list[dict[str, Any]] | None:
        """Return parsed cache when present and within TTL, else ``None``."""
        try:
            age = time.time() - cache_file.stat().st_mtime
        except OSError:
            return None
        if age >= _CACHE_MAX_AGE_SECONDS:
            return None
        return _FreeToPlayFeed._read_any_cache(cache_file)

    @staticmethod
    def _read_any_cache(cache_file: Path) -> list[dict[str, Any]]:
        """Parse the cache file regardless of age (``[]`` on any error)."""
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _download_entries(self, cache_file: Path) -> list[dict[str, Any]]:
        """Fetch the feed, normalise F2P entries, and write the cache."""
        ctx = ssl_ctx_permissive(
            "Ubisoft free-games CDN — advisory catalogue, payload "
            "treated as untrusted and validated field-by-field",
        )
        version = self._http_get(_LATEST_URL, ctx).decode("utf-8", "replace").strip()
        if not version:
            return []
        raw = self._http_get(_CONFIGS_URL.format(version=version), ctx)
        payload = json.loads(raw.decode("utf-8", "replace"))
        entries = self._normalise_payload(payload)
        with contextlib.suppress(OSError):
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(cache_file.suffix + ".tmp")
            tmp.write_text(json.dumps(entries), encoding="utf-8")
            tmp.replace(cache_file)
        logger.info(
            "[UbisoftLibrary] free-to-play feed: %d entries (version %s)",
            len(entries),
            version,
        )
        return entries

    @staticmethod
    def _http_get(url: str, ctx: Any) -> bytes:
        """GET ``url`` and return the body bytes."""
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Unifideck/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=ctx) as resp:
            return bytes(resp.read())

    @staticmethod
    def _normalise_payload(payload: Any) -> list[dict[str, Any]]:
        """Map the CDN payload's ``root`` array to manifest entries."""
        root = payload.get("root", []) if isinstance(payload, dict) else []
        entries: list[dict[str, Any]] = []
        for item in root:
            if not isinstance(item, dict):
                continue
            if (item.get("type") or "").lower() != "freetoplay":
                continue
            title = str(item.get("name") or "").strip()
            space_id = str(item.get("space_id") or "").strip()
            if not title or not space_id:
                continue
            product_id = str(item.get("product_id") or "").strip()
            entries.append(
                {
                    "title": title,
                    "space_id": space_id,
                    "install_id": product_id,
                    "launch_id": product_id,
                    "ubisoftconnect_game_id": product_id,
                    "cover_image": str(item.get("thumb_url") or "").strip(),
                    "ownership_type": "free",
                    "source": "free_feed",
                },
            )
        return entries
