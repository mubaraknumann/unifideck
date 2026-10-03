from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from unifideck.core.net import ssl_ctx_strict
from unifideck.core.types import Game, GameTag
from unifideck.utils.locale import get_unifideck_locale

from .microsoft_config import MicrosoftConfig

if TYPE_CHECKING:
    from .microsoft_subscription import SubscriptionProbeResult

from unifideck.launcher.browser_games import BROWSER_URL_KEY, XCLOUD_PLAY_URL

logger = logging.getLogger(__name__)

# Batch size for displaycatalog.mp.microsoft.com GET. 50 productIds
# per query stays well under the 4096-char URL limit.
_TITLE_BATCH_SIZE = 50
# displaycatalog is a public CDN tier — safe for higher concurrency
# than the gamepass origin.
_TITLE_BATCH_CONCURRENCY = 6

# Browser-shaped UA — required by Azure App Gateway in front of
# gssv-play-prod.xboxlive.com.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
)


class MicrosoftCatalogReader:
    """xCloud / Game Pass catalog reader.

    Pulls the user's account-scoped library from the regional GSSV
    ``/v2/titles`` endpoint (returns 2000+ cloud-streamable titles
    with per-title ``hasEntitlement`` flags), filters to entitled
    titles only — that's "Game Pass + Play Anywhere games the user
    owns" — then resolves display names via
    ``catalog.gamepass.com/v3/products``.

    Tier-tagging (Premium/Standard/Ultimate badges) is intentionally
    NOT done here. ``hasEntitlement`` already encodes tier access
    server-side: a Standard-tier account would see fewer entitled
    titles than an Ultimate one. If a per-tier UI filter is wanted
    later, cross-reference each entitled productId with the public
    ``sigls/v2`` channels (PC GP / Standard / Premium / Cloud) for
    pure display metadata — would not affect this sync.
    """

    def __init__(
        self,
        config: MicrosoftConfig,
        config_manager: Any,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._config_manager = config_manager

    async def fetch_games(
        self,
        session: SubscriptionProbeResult,
    ) -> list[Game] | None:
        """Fetch entitled games using the active xCloud session.

        ``None`` when the catalog could not be read (see
        ``MicrosoftStore.get_library``: an empty list deletes every xCloud
        shortcut). ``[]`` only when ``/v2/titles`` answered and none of its
        titles are entitled.
        """
        if not session.gs_token or not session.regions:
            logger.warning(
                "[MicrosoftCatalog] session has no gs_token/regions",
            )
            return None
        base_uri = _pick_region_base_uri(session.regions)
        if base_uri is None:
            logger.warning(
                "[MicrosoftCatalog] no usable region baseUri "
                "in session",
            )
            return None
        market = session.market or "US"
        lang = get_unifideck_locale(self._config_manager) or "en-US"

        logger.info(
            "[MicrosoftCatalog] fetching /v2/titles from %s "
            "(market=%s, lang=%s)", base_uri, market, lang,
        )
        t0 = time.time()
        titles = await self._fetch_xcloud_titles(
            base_uri, session.gs_token,
        )
        logger.info(
            "[MicrosoftCatalog] /v2/titles returned %d titles in %.1fs",
            len(titles), time.time() - t0,
        )
        entitled = _entitled_titles(titles, base_uri)
        if entitled is None:
            return None

        product_ids = _unique_product_ids(entitled)
        if not product_ids:
            logger.warning(
                "[MicrosoftCatalog] entitled titles had no "
                "productIds — keeping the existing xCloud shortcuts",
            )
            return None
        t1 = time.time()
        title_map = await self._batch_resolve_titles(
            product_ids, market,
        )
        logger.info(
            "[MicrosoftCatalog] resolved %d/%d titles in %.1fs "
            "(total fetch_games: %.1fs)",
            len(title_map), len(product_ids),
            time.time() - t1, time.time() - t0,
        )
        return self._build_xcloud_games(entitled, title_map)

    @staticmethod
    def _build_xcloud_games(
        entitled: list[dict[str, Any]], title_map: dict[str, Any],
    ) -> list[Game]:
        """Build xCloud ``Game`` records from entitled titles + names."""
        games: list[Game] = []
        for t in entitled:
            pid = (t.get("details") or {}).get("productId") or ""
            if not pid:
                continue
            games.append(Game(
                app_id=0,
                store="microsoft",
                store_game_id=pid,
                title=_title_for(title_map, pid, t.get("titleId", "")),
                installed=False,
                tags=[GameTag.XCLOUD, GameTag.BROWSER],
                metadata={BROWSER_URL_KEY: XCLOUD_PLAY_URL.format(game_id=pid)},
            ))
        return games

    async def _fetch_xcloud_titles(
        self, base_uri: str, gs_token: str,
    ) -> list[dict[str, Any]]:
        """GET ``{base}/v2/titles`` with Bearer gsToken."""
        url = base_uri.rstrip("/") + "/v2/titles"
        headers = {
            "Authorization": f"Bearer {gs_token}",
            "Accept": "application/json",
            "Accept-Language": "en-US",
            "User-Agent": _BROWSER_UA,
            "x-gssv-client": "XboxComBrowser",
        }
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _xcloud_titles_sync(url, headers),
        )
        return result or []

    async def _batch_resolve_titles(
        self, product_ids: list[str], market: str,
    ) -> dict[str, str]:
        """Resolve productIds → display titles via displaycatalog MP."""
        products = await fetch_products(product_ids, market)
        return {
            pid: title for pid, raw in products.items()
            if (title := product_title(raw))
        }


def _pick_region_base_uri(
    regions: list[dict[str, Any]],
) -> str | None:
    """Pick the default region's baseUri, else the first available."""
    for r in regions:
        base = r.get("baseUri")
        if r.get("isDefault") and isinstance(base, str):
            return base
    for r in regions:
        base = r.get("baseUri")
        if isinstance(base, str):
            return base
    return None



def _entitled_titles(
    titles: list[dict[str, Any]], base_uri: str,
) -> list[dict[str, Any]] | None:
    """The titles this account may stream, or ``None`` if the answer is unusable.

    Both ``None`` cases keep the existing shortcuts (see ``fetch_games``):

    - no titles at all: the catalog lists ~2,700 titles to every account,
      so zero means the request failed or the answer was mangled;
    - titles but none entitled: only reached with an active subscription
      (the gate ran first), so the response changed shape.
    """
    if not titles:
        logger.warning(
            "[MicrosoftCatalog] /v2/titles returned no titles from %s "
            "— keeping the existing xCloud shortcuts",
            base_uri,
        )
        return None
    entitled = [
        t for t in titles
        if isinstance(t, dict)
        and isinstance(t.get("details"), dict)
        and t["details"].get("hasEntitlement") is True
    ]
    logger.info(
        "[MicrosoftCatalog] %d total visible, %d entitled",
        len(titles), len(entitled),
    )
    if not entitled:
        logger.warning(
            "[MicrosoftCatalog] %d titles but none entitled — keeping "
            "the existing xCloud shortcuts", len(titles),
        )
        return None
    return entitled

def _unique_product_ids(entitled: list[dict[str, Any]]) -> list[str]:
    """Extract unique productIds preserving first-seen order."""
    seen: dict[str, None] = {}
    for t in entitled:
        pid = (t.get("details") or {}).get("productId")
        if isinstance(pid, str) and pid and pid not in seen:
            seen[pid] = None
    return list(seen)


def _title_for(
    title_map: dict[str, str], product_id: str, fallback: str,
) -> str:
    """Best display name: v3 ProductTitle, or titleId, or product_id.

    The lookup is case-folded: ``store_game_id`` is sometimes lowercase
    (e.g. ``brrc2bp0g9p0``) but displaycatalog always returns the
    canonical UPPERCASE ``ProductId`` (``BRRC2BP0G9P0``), which is what
    :func:`_parse_displaycatalog` keys on. A case-sensitive lookup
    missed every lowercase id and fell back to the ugly ``titleId`` slug
    ("HALO5", "GEARSOFWAR4", "DEADBYDEADLIGHT") — a wrong title that then
    poisoned metadata, compatibility, and artwork search downstream.
    """
    name = title_map.get(product_id.upper())
    if isinstance(name, str) and name:
        return name
    if fallback:
        return fallback
    return product_id


def _xcloud_titles_sync(
    url: str, headers: dict[str, str],
) -> list[dict[str, Any]]:
    """Synchronous GET for /v2/titles. Returns ``results`` list."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(
            req, timeout=30,
            context=ssl_ctx_strict(),
        ) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.exception(
            "[MicrosoftCatalog] /v2/titles HTTPError %d (reason=%s)",
            e.code, e.reason,
        )
        return []
    except urllib.error.URLError as e:
        logger.exception(
            "[MicrosoftCatalog] /v2/titles URLError (reason=%r)",
            e.reason,
        )
        return []
    except Exception as e:
        logger.exception(
            "[MicrosoftCatalog] /v2/titles unexpected %s",
            type(e).__name__,
        )
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception(
            "[MicrosoftCatalog] /v2/titles returned non-JSON "
            "(len=%d, first=%.200s)",
            len(raw), raw,
        )
        return []
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    return results if isinstance(results, list) else []


async def fetch_products(
    product_ids: list[str], market: str,
) -> dict[str, dict[str, Any]]:
    """Raw displaycatalog products by UPPER-case productId.

    displaycatalog.mp.microsoft.com is a public CDN endpoint with no auth
    requirement — faster and more reliable than catalog.gamepass.com/v3,
    which requires undisclosed calling-app-name headers and routinely 500s
    under even modest concurrency.

    Batches of ``_TITLE_BATCH_SIZE`` run concurrently, at most
    ``_TITLE_BATCH_CONCURRENCY`` at a time. A failed batch contributes
    nothing; callers treat a missing product as "unknown", never as absent.
    Shared by the xCloud library (titles) and the owned-products index
    (titles and platforms).
    """
    batches = [
        product_ids[i: i + _TITLE_BATCH_SIZE]
        for i in range(0, len(product_ids), _TITLE_BATCH_SIZE)
    ]
    logger.info(
        "[MicrosoftCatalog] resolving %d products in %d batches "
        "(size=%d, concurrency=%d) via displaycatalog.mp.ms",
        len(product_ids), len(batches),
        _TITLE_BATCH_SIZE, _TITLE_BATCH_CONCURRENCY,
    )
    sem = asyncio.Semaphore(_TITLE_BATCH_CONCURRENCY)

    async def run_one(batch: list[str]) -> list[dict[str, Any]]:
        async with sem:
            return await asyncio.to_thread(_fetch_batch_displaycatalog, batch, market)

    out: dict[str, dict[str, Any]] = {}
    for products in await asyncio.gather(*(run_one(b) for b in batches)):
        for raw in products:
            pid = raw.get("ProductId")
            if isinstance(pid, str) and pid:
                # UPPER: store_game_ids are sometimes lowercase, and every
                # lookup case-folds (see ``_title_for``).
                out[pid.upper()] = raw
    return out


def product_title(raw: dict[str, Any]) -> str:
    """``LocalizedProperties[0].ProductTitle`` of a displaycatalog product, or ""."""
    loc = raw.get("LocalizedProperties")
    if not isinstance(loc, list) or not loc or not isinstance(loc[0], dict):
        return ""
    title = loc[0].get("ProductTitle")
    return title if isinstance(title, str) else ""


def _fetch_batch_displaycatalog(
    batch: list[str], market: str,
) -> list[dict[str, Any]]:
    """GET displaycatalog.mp.microsoft.com for one batch of productIds."""
    from urllib.parse import urlencode as _uenc
    qs = _uenc({
        "bigIds": ",".join(batch),
        "market": market, "languages": "en-US",
    })
    url = (
        "https://displaycatalog.mp.microsoft.com/v7.0/products?" + qs
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": _BROWSER_UA, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(
            req, timeout=30,
            context=ssl_ctx_strict(),
        ) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.warning(
            "[MicrosoftCatalog] displaycatalog HTTPError %d "
            "(batch size %d)",
            e.code, len(batch),
        )
        return []
    except Exception as e:
        logger.warning(
            "[MicrosoftCatalog] displaycatalog %s (batch size %d)",
            type(e).__name__, len(batch),
        )
        return []
    return _parse_displaycatalog(raw)


def _parse_displaycatalog(raw: str) -> list[dict[str, Any]]:
    """Parse a displaycatalog response → its ``Products`` list."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "[MicrosoftCatalog] displaycatalog non-JSON "
            "(len=%d, first=%.200s)",
            len(raw), raw,
        )
        return []
    products = data.get("Products") if isinstance(data, dict) else None
    if not isinstance(products, list):
        return []
    return [p for p in products if isinstance(p, dict)]
