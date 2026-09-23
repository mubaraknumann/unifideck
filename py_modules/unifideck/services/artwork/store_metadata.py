"""Per-store artwork metadata fetchers.

Returns ``{"urls": {kind: url}}`` dicts for the five artwork kinds
(``grid``, ``grid_l``, ``hero``, ``logo``, ``icon``).  Pure-fetch
functions: no caching, no filesystem writes — the caller
(:class:`ArtworkService`) handles selection + download.

Sources, mirroring staging's pipeline:

* **Steam Store** — ``storesearch`` API resolves a real Steam
  AppID, then four canonical ``shared.steamstatic.com`` URLs
  are built from it.  No API key required.
* **GOG / Amazon** — both use GOG's public Galaxy GamesDB
  (``gamesdb.gog.com/platforms/{platform}/external_releases/{id}``)
  which exposes ``vertical_cover``, ``background``, ``logo``,
  ``square_icon``.  Authoritative box-art with proper
  dimensions for Steam's grid.
* **Epic** — Legendary already caches ``keyImages`` from the
  Epic API at ``~/.config/legendary/metadata/{app_name}.json``;
  we pick the best match per kind via a priority table.
* **Ubisoft** — the per-game ``metadata`` dict (set during sync)
  carries ``coverUrl`` / ``backgroundUrl`` directly from
  Ubisoft's GraphQL API.
* **Microsoft** — the public ``displaycatalog.mp.microsoft.com``
  endpoint exposes ``LocalizedProperties[].Images`` (Poster,
  SuperHeroArt, TitledHeroArt, …).  Keyed on the productId, so it
  works even when the xCloud display name degraded to a slug
  ("HALO5") that no title search could resolve.

Each fetcher is best-effort: returns an empty ``urls`` dict on
any failure so the orchestrator can fall through to SGDB / Steam
CDN without raising.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any, cast

import aiohttp

from unifideck.utils.title_match import titles_match

logger = logging.getLogger(__name__)

_HTTP_OK = 200
_DEFAULT_TIMEOUT = 15
_STEAM_SEARCH_URL = "https://store.steampowered.com/api/storesearch"
_STEAM_CDN = "https://shared.steamstatic.com/store_item_assets/steam/apps"
_GAMESDB_URL = "https://gamesdb.gog.com/platforms/{platform}/external_releases/{id}"
_GOG_PRODUCT_URL = "https://api.gog.com/products/{id}?expand=description"
# Microsoft's public (no-auth) display catalog — same endpoint the
# Microsoft store sync already hits to resolve product titles. Its
# ``LocalizedProperties[].Images`` carries authoritative box art, so
# we reuse it for artwork keyed on the productId (no title-search →
# works even for mangled xCloud titleId slugs like "HALO5").
_MS_DISPLAYCATALOG = (
    "https://displaycatalog.mp.microsoft.com/v7.0/products"
    "?bigIds={id}&market=US&languages=en-US"
)


async def steam_search_appid(
    title: str,
    timeout: int = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — passed to aiohttp.ClientTimeout
) -> int | None:
    """Resolve a title to a real Steam AppID, or ``None`` on miss.

    Uses Steam Store's public ``storesearch`` endpoint and the shared
    :func:`~unifideck.utils.title_match.titles_match` to pick the row
    that actually IS this game — so ®/™ / apostrophe / edition /
    publisher-prefix / Roman-numeral noise no longer defeats the match,
    while sequels ("Hades" → "Hades II") and unrelated hits ("Control" →
    "Steam Controller") are rejected rather than blindly accepted.
    """
    if not title:
        return None
    try:
        url = f"{_STEAM_SEARCH_URL}?term={urllib.parse.quote(title)}&cc=US"
        connector = aiohttp.TCPConnector(ssl=False)
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with (
            aiohttp.ClientSession(connector=connector, timeout=client_timeout) as s,
            s.get(url) as resp,
        ):
            if resp.status != _HTTP_OK:
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError):
        return None

    items = data.get("items", []) if isinstance(data, dict) else []
    for item in items:
        steam_id = item.get("id")
        if not isinstance(steam_id, int) or steam_id <= 0:
            continue
        if titles_match(title, str(item.get("name", ""))):
            return steam_id
    return None


def steam_cdn_urls(steam_app_id: int) -> dict[str, str]:
    """Build the four canonical Steam-CDN artwork URLs for ``steam_app_id``.

    URLs come from staging's ``get_steam_metadata`` — these are
    the same paths Steam's UI itself loads from. No API key,
    no auth, exact Steam dimensions.
    """
    return {
        "grid":   f"{_STEAM_CDN}/{steam_app_id}/library_600x900_2x.jpg",
        "grid_l": f"{_STEAM_CDN}/{steam_app_id}/header.jpg",
        "hero":   f"{_STEAM_CDN}/{steam_app_id}/library_hero.jpg",
        "logo":   f"{_STEAM_CDN}/{steam_app_id}/logo.png",
    }


def _normalize_gamesdb_url(entry: dict[str, Any], ext: str) -> str | None:
    """Pull a usable URL out of a GOG GamesDB image dict."""
    fmt = entry.get("url_format")
    if not isinstance(fmt, str):
        return None
    return fmt.replace("{formatter}", "").replace("{ext}", ext)


def _extract_gamesdb_urls(game: dict[str, Any]) -> dict[str, str]:
    """Map a GOG GamesDB ``game`` object to ``{kind: url}``.

    Used by both ``gog_metadata`` and ``amazon_metadata`` —
    Amazon games go through the same GOG GamesDB endpoint, so the
    extraction logic is identical.
    """
    out: dict[str, str] = {}
    fields = (
        ("vertical_cover", "grid", "jpg"),
        ("background", "hero", "jpg"),
        ("logo", "logo", "png"),
    )
    for src_key, dst_key, ext in fields:
        entry = game.get(src_key)
        if isinstance(entry, dict):
            url = _normalize_gamesdb_url(entry, ext)
            if url:
                out[dst_key] = url
    icon = game.get("square_icon") or game.get("icon")
    if isinstance(icon, dict):
        url = _normalize_gamesdb_url(icon, "jpg")
        if url:
            out["icon"] = url
    return out


async def _fetch_json(
    url: str,
    timeout: int,  # noqa: ASYNC109 — passed to aiohttp.ClientTimeout
) -> Any:
    """GET ``url`` and return parsed JSON, or ``None`` on any failure."""
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with (
            aiohttp.ClientSession(connector=connector, timeout=client_timeout) as s,
            s.get(url) as resp,
        ):
            if resp.status != _HTTP_OK:
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
        return None


async def gog_metadata(
    gog_product_id: int,
    timeout: int = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — passed to aiohttp
) -> dict[str, Any]:
    """Fetch GOG cover URLs from GOG's Galaxy GamesDB API.

    The GamesDB endpoint exposes the rich box-art set the GOG
    Galaxy desktop client uses. Falls back to the older GOG
    products API for delisted titles.
    """
    out: dict[str, str] = {}
    try:
        data = await _fetch_json(
            _GAMESDB_URL.format(platform="gog", id=gog_product_id), timeout,
        )
        game = (
            data.get("game", {})
            if isinstance(data, dict) else {}
        )
        if isinstance(game, dict):
            out.update(_extract_gamesdb_urls(game))
        if not out:
            await _gog_products_fallback(gog_product_id, timeout, out)
    except Exception as e:
        logger.debug("[Artwork.gog] %s failed: %s", gog_product_id, e)
    return {"urls": out}


def _ensure_proto(u: Any) -> str | None:
    """Normalise a protocol-relative URL to an absolute ``https://`` form."""
    if not isinstance(u, str):
        return None
    return ("https:" + u) if u.startswith("//") else u


async def _gog_products_fallback(
    gog_product_id: int,
    timeout: int,  # noqa: ASYNC109 — passed to aiohttp
    out: dict[str, str],
) -> None:
    """Fallback path: GOG's older products API. Mutates ``out`` in place."""
    data = await _fetch_json(
        _GOG_PRODUCT_URL.format(id=gog_product_id), timeout,
    )
    if not isinstance(data, dict):
        return
    images = data.get("images", {})
    if not isinstance(images, dict):
        return
    # ``(dst_key, src_keys)`` — try src_keys in order; first hit wins.
    candidates = (
        ("icon", ("icon",)),
        ("logo", ("logo2x", "logo")),
        ("hero", ("background",)),
    )
    for dst, src_keys in candidates:
        if out.get(dst):
            continue
        for src in src_keys:
            url = _ensure_proto(images.get(src))
            if url:
                out[dst] = url
                break


async def amazon_metadata(
    amazon_game_id: str,
    timeout: int = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — passed to aiohttp
) -> dict[str, Any]:
    """Fetch Amazon cover URLs via GOG's Galaxy GamesDB (same as Heroic does).

    Amazon's ``library.json`` only ships 512x288 horizontal
    images — too small for Steam's library tile. GamesDB exposes
    rich box-art (``vertical_cover``) keyed on the Amazon ID.
    """
    out: dict[str, str] = {}
    try:
        data = await _fetch_json(
            _GAMESDB_URL.format(platform="amazon", id=amazon_game_id), timeout,
        )
        game = (
            data.get("game", {})
            if isinstance(data, dict) else {}
        )
        if isinstance(game, dict):
            out.update(_extract_gamesdb_urls(game))
    except Exception as e:
        logger.debug("[Artwork.amazon] %s failed: %s", amazon_game_id, e)
    return {"urls": out}


_EPIC_TYPE_PRIORITY: dict[str, tuple[str, ...]] = {
    "grid": (
        "DieselGameBoxTall", "OfferImageTall",
        "DieselStoreFrontTall", "DieselGameBox", "Thumbnail",
    ),
    "hero": (
        "OfferImageWide", "DieselGameBoxWide",
        "DieselStoreFrontWide", "featuredMedia",
    ),
    "logo": ("DieselGameBoxLogo", "ProductLogo"),
}


def _find_legendary_metadata(epic_app_name: str) -> Path | None:
    """Resolve the legendary metadata JSON for ``epic_app_name``.

    Legendary names the file after the canonical ``app_name`` so
    the lookup is usually direct, but some games store a different
    user-facing id — we scan the dir as a fallback.
    """
    meta_dir = Path("~/.config/legendary/metadata").expanduser()
    direct = meta_dir / f"{epic_app_name}.json"
    if direct.exists():
        return direct
    for f in meta_dir.glob("*.json"):
        try:
            payload = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("app_name") == epic_app_name:
            return f
    return None


def _pick_epic_image(
    key_images: list[Any], type_keys: tuple[str, ...],
) -> str | None:
    """First-match-wins selector across ``key_images`` for ``type_keys``."""
    for tk in type_keys:
        for img in key_images:
            if not isinstance(img, dict) or img.get("type") != tk:
                continue
            url = img.get("url")
            if isinstance(url, str):
                return url
    return None


async def epic_metadata(epic_app_name: str) -> dict[str, Any]:
    """Read Epic artwork from Legendary's cached metadata.

    Legendary writes a JSON file per game at
    ``~/.config/legendary/metadata/{app_name}.json`` containing
    ``keyImages``. We pick the best per kind via a priority list
    that matches staging.
    """
    out: dict[str, str] = {}
    try:
        candidate = _find_legendary_metadata(epic_app_name)
        if candidate is None:
            return {"urls": out}
        data = json.loads(candidate.read_text())
        key_images = (
            data.get("keyImages")
            or data.get("metadata", {}).get("keyImages")
            or []
        )
        if not isinstance(key_images, list):
            return {"urls": out}
        for kind, type_keys in _EPIC_TYPE_PRIORITY.items():
            url = _pick_epic_image(key_images, type_keys)
            if url:
                out[kind] = url
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("[Artwork.epic] %s failed: %s", epic_app_name, e)
    return {"urls": out}


def ubisoft_metadata(extras: dict[str, Any]) -> dict[str, Any]:
    """Pull Ubisoft URLs out of the per-game ``metadata`` dict.

    Ubisoft GraphQL responses are flattened into ``game.metadata``
    during sync; the keys we want are ``coverUrl`` / ``backgroundUrl``.
    """
    out: dict[str, str] = {}
    cover = extras.get("coverUrl") or extras.get("cover_image")
    bg = extras.get("backgroundUrl") or extras.get("hero_image")
    if isinstance(cover, str):
        out["grid"] = cover
    if isinstance(bg, str):
        out["hero"] = bg
    return {"urls": out}


# Microsoft ``ImagePurpose`` → kind, in priority order.  ``Poster`` is a
# true 2:3 portrait (ideal Steam grid); ``TitledHeroArt`` carries the
# wordmark like Steam's landscape capsule; ``SuperHeroArt`` is a clean,
# text-free 16:9 banner.  Logo + icon are intentionally left to SGDB —
# Microsoft's ``Logo`` purpose is a square store tile, not the
# transparent wordmark Steam composites over the hero.
_MS_IMAGE_PRIORITY: dict[str, tuple[str, ...]] = {
    "grid":   ("Poster", "BrandedKeyArt"),
    "grid_l": ("TitledHeroArt", "Hero"),
    "hero":   ("SuperHeroArt", "Hero", "TitledHeroArt"),
}


def _extract_ms_images(images: list[Any]) -> dict[str, str]:
    """Map a displaycatalog ``Images`` list to ``{kind: url}``.

    First-seen wins per ``ImagePurpose``; kinds resolve against
    :data:`_MS_IMAGE_PRIORITY`.  Protocol-relative ``//`` URIs are
    normalised to ``https://``.
    """
    by_purpose: dict[str, str] = {}
    for im in images:
        if not isinstance(im, dict):
            continue
        purpose = im.get("ImagePurpose")
        uri = _ensure_proto(im.get("Uri"))
        if isinstance(purpose, str) and uri and purpose not in by_purpose:
            by_purpose[purpose] = uri
    out: dict[str, str] = {}
    for kind, purposes in _MS_IMAGE_PRIORITY.items():
        for purpose in purposes:
            if purpose in by_purpose:
                out[kind] = by_purpose[purpose]
                break
    return out


async def microsoft_metadata(
    product_id: str,
    timeout: int = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — passed to aiohttp
) -> dict[str, Any]:
    """Fetch Microsoft box art from the public display catalog.

    Keyed on the ``productId`` (the store_game_id), so it resolves
    art without a title search — the fix for xCloud games whose
    display name degraded to a ``titleId`` slug ("HALO5",
    "GEARSOFWAR4") that no title-based search can match.
    """
    out: dict[str, str] = {}
    if not product_id:
        return {"urls": out}
    try:
        url = _MS_DISPLAYCATALOG.format(id=urllib.parse.quote(product_id))
        data = await _fetch_json(url, timeout)
        products = data.get("Products") if isinstance(data, dict) else None
        if isinstance(products, list) and products:
            loc = products[0].get("LocalizedProperties")
            if isinstance(loc, list) and loc:
                images = loc[0].get("Images")
                if isinstance(images, list):
                    out.update(_extract_ms_images(images))
    except Exception as e:
        logger.debug("[Artwork.microsoft] %s failed: %s", product_id, e)
    return {"urls": out}


async def fetch_store_urls(
    store: str, store_game_id: str, extras: dict[str, Any] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — passed to aiohttp
) -> dict[str, str]:
    """Dispatch to the right per-store fetcher; return ``{kind: url}``.

    Best-effort: any per-store fetch failure returns an empty
    dict so the orchestrator falls through to SGDB / Steam CDN.
    """
    if store == "gog":
        try:
            payload = await gog_metadata(int(store_game_id), timeout)
            return cast("dict[str, str]", payload.get("urls", {}))
        except ValueError:
            return {}
    if store == "amazon":
        payload = await amazon_metadata(store_game_id, timeout)
        return cast("dict[str, str]", payload.get("urls", {}))
    if store == "epic":
        payload = await epic_metadata(store_game_id)
        return cast("dict[str, str]", payload.get("urls", {}))
    if store == "ubisoft":
        return cast("dict[str, str]", ubisoft_metadata(extras or {}).get("urls", {}))
    if store == "microsoft":
        payload = await microsoft_metadata(store_game_id, timeout)
        return cast("dict[str, str]", payload.get("urls", {}))
    return {}


def itch_fallback_urls(extras: dict[str, Any]) -> dict[str, str]:
    """itch.io's cover, for whichever capsule kinds nothing better filled.

    Every itch.io game has a cover (``coverUrl``, ~630x500), and most of the
    long-tail indie titles have no SteamGridDB or Steam art at all (9 of 17
    on the first test account showed Steam's grey title placeholder). An
    animated ``.gif`` cover is swapped for itch.io's still version.
    """
    cover = extras.get("cover_url")
    still = extras.get("still_cover_url")
    if isinstance(cover, str) and cover.lower().endswith(".gif") and isinstance(still, str):
        cover = still
    if not isinstance(cover, str) or not cover:
        return {}
    return {"grid": cover, "grid_l": cover}


def fetch_store_fallback_urls(store: str, extras: dict[str, Any] | None) -> dict[str, str]:
    """Last-resort ``{kind: url}`` from the store, used after SGDB and the Steam CDN.

    For art that is worse than a match elsewhere but better than nothing,
    such as a landscape cover in a portrait slot. Unlike
    :func:`fetch_store_urls` it never overrides another source, because the
    artwork service only asks it for kinds that are still missing.
    """
    if store == "itch":
        return itch_fallback_urls(extras or {})
    return {}


__all__ = [
    "amazon_metadata",
    "epic_metadata",
    "fetch_store_fallback_urls",
    "fetch_store_urls",
    "gog_metadata",
    "microsoft_metadata",
    "steam_cdn_urls",
    "steam_search_appid",
    "ubisoft_metadata",
]
