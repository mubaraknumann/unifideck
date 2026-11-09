"""stores/microsoft/achievements.py — Xbox achievements (read + normalize).

Reads a game's achievements and this account's unlock status from Xbox
Live (``achievements.xboxlive.com``, contract version 4) and normalizes them
to the shape GOG and Epic return, so the game page renders all three alike.

Read-only. Games unlock their own achievements through the runtime's Xbox
Live session; this only reads back what was earned. Works for Xbox PC and
xCloud games alike: unlocks live on the account, not on the device.

Inputs:
  * the ``xboxlive.com`` XSTS chain from :class:`MicrosoftTokenManager`
    (it carries the xuid), so no second sign-in;
  * the game's numeric Xbox title id: the install record's ``titleId``
    (hex, from ``MicrosoftGame.config``) when installed, else the
    displaycatalog product's ``XboxTitleId`` alternate id.

Row validation follows the Nexus ``achievements`` command (title check,
``Persistent`` only, revoked counts as locked, icon host allowlist, bounded
paging). We call Xbox Live directly instead of running that command because
it refuses to run while a game holds the credentials, which rules out
in-game unlock toasts, and it reports every failure as one error.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

from unifideck.stores.shared.achievements_error import StoreAchievementsError
from unifideck.stores.shared.achievements_payload import TtlCache, build_payload
from unifideck.stores.shared.timestamps import parse_timestamp

from .microsoft_catalog import fetch_products
from .tokens.endpoint import request_json
from .xodus.ids import as_pid, record_for_game

if TYPE_CHECKING:
    from .tokens import MicrosoftTokenManager
    from .tokens.xbl_chain import XBLTokenChain

logger = logging.getLogger(__name__)

_URL = "https://achievements.xboxlive.com/users/xuid({xuid})/achievements"
_ICON_HOSTS = frozenset({
    "images-eds-ssl.xboxlive.com",
    "images-eds.xboxlive.com",
    "store-images.s-microsoft.com",
})
_PAGE_SIZE = 100
_MAX_PAGES = 10
_MAX_ITEMS = 500
#: XSTS tokens live for hours; re-mint well before that, and on any 401.
_CHAIN_TTL_SECONDS = 1800.0
_TITLE_ID_ALT = "XboxTitleId"


class MicrosoftAchievementsError(StoreAchievementsError):
    """Typed Xbox achievements failure the RPC layer maps to an ``RpcError``."""


class MicrosoftAchievements:
    """Fetch + normalize + cache an Xbox game's achievements (display-only)."""

    def __init__(
        self,
        tokens: MicrosoftTokenManager,
        market_fn: Callable[[], str],
        locale_fn: Callable[[], str],
    ) -> None:
        """Initialize the instance."""
        self._tokens = tokens
        self._market_fn = market_fn
        self._locale_fn = locale_fn
        self._cache = TtlCache()
        self._title_ids: dict[str, int] = {}
        self._chain: XBLTokenChain | None = None
        self._chain_at = 0.0
        #: The access token ``_chain`` was minted from, and its xuid. A new
        #: sign-in (or sign-out) never reuses the chain or another account's
        #: cached unlocks.
        self._chain_for: str | None = None
        self._xuid: str | None = None

    async def get_game_achievements(
        self, game_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """Return the normalized achievements payload (same shape as GOG)."""
        key = str(game_id)
        if not force:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        title_id = await self.title_id(key)
        if title_id is None:
            raise MicrosoftAchievementsError("no_client_id", game_id=key)
        chain = await self._live_chain()
        try:
            items = await asyncio.to_thread(self._fetch_blocking, chain, title_id)
        except MicrosoftAchievementsError as e:
            if e.code != "auth_expired":
                raise
            logger.info("[xbox.achievements] token refused, re-minting once")
            chain = await self._live_chain(fresh=True)
            items = await asyncio.to_thread(self._fetch_blocking, chain, title_id)
        payload = build_payload("microsoft", key, normalize_rows(items, title_id))
        self._cache.put(key, payload)
        return payload

    def invalidate(self, game_id: str) -> None:
        """Drop a game's cached achievements."""
        self._cache.invalidate(game_id)

    def clear(self) -> None:
        """Forget everything tied to the account."""
        self._cache.clear()
        self._chain = None
        self._chain_for = None

    # -- title id ----------------------------------------------------------

    async def title_id(self, game_id: str) -> int | None:
        """The numeric Xbox Live title id of ``game_id``, or None."""
        if game_id in self._title_ids:
            return self._title_ids[game_id]
        record = await asyncio.to_thread(record_for_game, game_id)
        found = hex_title_id((record or {}).get("titleId"))
        if found is None:
            found = await self._catalog_title_id(game_id)
        if found is not None:
            self._title_ids[game_id] = found
        return found

    async def _catalog_title_id(self, game_id: str) -> int | None:
        pid = as_pid(game_id)
        if pid is None:
            return None
        products = await fetch_products([pid], self._market_fn())
        return catalog_title_id(products.get(pid))

    # -- auth --------------------------------------------------------------

    async def _live_chain(self, *, fresh: bool = False) -> XBLTokenChain:
        """The ``xboxlive.com`` chain with an xuid, or a typed error."""
        now = time.monotonic()
        access = self._tokens.access_token
        if not access:
            self.clear()
            raise MicrosoftAchievementsError("not_authed")
        if (
            not fresh and self._chain is not None and self._chain_for == access
            and now - self._chain_at < _CHAIN_TTL_SECONDS
        ):
            return self._chain
        self._chain = None
        chain = await self._tokens.build_chain()
        if chain is None or not chain.xuid:
            # The access token may have just expired: renew it once.
            await self._tokens.refresh_if_stale()
            chain = await self._tokens.build_chain()
        if chain is None or not chain.xuid:
            refused = self._tokens.last_xerr is not None
            raise MicrosoftAchievementsError("auth_expired" if refused else "offline")
        if chain.xuid != self._xuid:
            self._cache.clear()  # another account: its unlocks are not ours
            self._xuid = chain.xuid
        self._chain, self._chain_at = chain, now
        self._chain_for = self._tokens.access_token
        return chain

    # -- fetch (worker thread) ---------------------------------------------

    def _fetch_blocking(
        self, chain: XBLTokenChain, title_id: int,
    ) -> list[dict[str, Any]]:
        """Every achievement row of ``title_id``, following continuation tokens."""
        headers = {
            "Authorization": f"XBL3.0 x={chain.user_hash};{chain.xsts_token}",
            # v4: the same rows as v2 plus ``rarity`` (v2 leaves it out;
            # measured 2026-11-02 on two titles).
            "x-xbl-contract-version": "4",
            "Accept-Language": self._locale_fn() or "en-US",
        }
        base = _URL.format(xuid=chain.xuid)
        rows: list[dict[str, Any]] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            query = {"titleId": str(title_id), "types": "Persistent", "maxItems": str(_PAGE_SIZE)}
            if token:
                query["continuationToken"] = token
            reply = request_json("GET", f"{base}?{urlencode(query)}", headers)
            page, token = read_page(reply.status, reply.body)
            rows.extend(page)
            if not token or token in seen or len(rows) >= _MAX_ITEMS:
                break
            seen.add(token)
        return rows[:_MAX_ITEMS]


# -- pure helpers (unit-tested) -------------------------------------------


def hex_title_id(value: Any) -> int | None:
    """``MicrosoftGame.config``'s hex ``TitleId`` → int, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        number = int(value, 16)
    except ValueError:
        return None
    return number if 0 < number < 2**32 else None


def catalog_title_id(product: dict[str, Any] | None) -> int | None:
    """A displaycatalog product's decimal ``XboxTitleId`` alternate id."""
    for alt in (product or {}).get("AlternateIds") or []:
        if isinstance(alt, dict) and alt.get("IdType") == _TITLE_ID_ALT:
            value = str(alt.get("Value") or "")
            if value.isdigit() and 0 < int(value) < 2**32:
                return int(value)
    return None


def read_page(
    status: int | None, body: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """One Achievements V2 page → ``(rows, continuation token)``.

    Raises ``auth_expired`` on 401/403 and ``offline`` on any other failure
    or a body that is not an achievements page.
    """
    if status in (401, 403):
        raise MicrosoftAchievementsError("auth_expired")
    if status != 200:
        logger.warning("[xbox.achievements] HTTP %s", status)
        raise MicrosoftAchievementsError("offline")
    rows = body.get("achievements")
    paging = body.get("pagingInfo")
    if not isinstance(rows, list) or not isinstance(paging, dict):
        raise MicrosoftAchievementsError("offline")
    token = paging.get("continuationToken")
    return [r for r in rows if isinstance(r, dict)], token if isinstance(token, str) and token else None


def normalize_rows(
    rows: list[dict[str, Any]], title_id: int,
) -> list[dict[str, Any]]:
    """Normalize every valid row; skip (and count) the rest."""
    out: list[dict[str, Any]] = []
    keys: set[str] = set()
    for row in rows:
        item = normalize_row(row, title_id)
        if item is None or item["key"] in keys:
            continue
        keys.add(item["key"])
        out.append(item)
    if len(out) != len(rows):
        logger.debug("[xbox.achievements] skipped %d invalid rows", len(rows) - len(out))
    return out


def normalize_row(row: dict[str, Any], title_id: int) -> dict[str, Any] | None:
    """One Achievements V2 row → the shared shape, or None when invalid."""
    if not _for_title(row, title_id) or row.get("achievementType") != "Persistent":
        return None
    ach_id, name = row.get("id"), row.get("name")
    if not isinstance(ach_id, str) or not ach_id or not isinstance(name, str) or not name:
        return None
    unlocked = row.get("progressState") == "Achieved" and not row.get("isRevoked")
    raw_progression = row.get("progression")
    progression = raw_progression if isinstance(raw_progression, dict) else {}
    icon = _icon(row.get("mediaAssets"))
    description = row.get("description") if unlocked else row.get("lockedDescription")
    return {
        "key": f"{str(row.get('serviceConfigId') or '').lower()}:{ach_id}",
        "name": name,
        "description": str(description or row.get("description") or ""),
        "image_unlocked": icon,
        "image_locked": icon,
        "hidden": bool(row.get("isSecret")),
        "unlocked": unlocked,
        "unlocked_at": parse_timestamp(progression.get("timeUnlocked")) if unlocked else None,
        "rarity": _rarity(row.get("rarity")),
        "gamerscore": _gamerscore(row.get("rewards")),
    }


def _for_title(row: dict[str, Any], title_id: int) -> bool:
    """A row belongs to ``title_id`` (never let another title's rows in)."""
    for assoc in row.get("titleAssociations") or []:
        if isinstance(assoc, dict) and str(assoc.get("id")) == str(title_id):
            return True
    return False


def _icon(assets: Any) -> str:
    """The first ``Icon`` asset on an allowed Xbox host, else ``""``."""
    for asset in (assets if isinstance(assets, list) else [])[:20]:
        if not isinstance(asset, dict) or asset.get("type") != "Icon":
            continue
        url = asset.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            continue
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.hostname in _ICON_HOSTS and not parts.username:
            return url
    return ""


def _rarity(value: Any) -> float | None:
    """``rarity.currentPercentage`` (share of players), when present."""
    pct = value.get("currentPercentage") if isinstance(value, dict) else None
    if isinstance(pct, (int, float)) and 0 <= pct <= 100:
        return float(pct)
    return None


def _gamerscore(rewards: Any) -> int | None:
    """The ``Gamerscore`` reward value, when the row has one."""
    for reward in rewards if isinstance(rewards, list) else []:
        if isinstance(reward, dict) and reward.get("type") == "Gamerscore":
            value = str(reward.get("value") or "")
            if value.isdigit() and int(value) <= 10_000:
                return int(value)
    return None
