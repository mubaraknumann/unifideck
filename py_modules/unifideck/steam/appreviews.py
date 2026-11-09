"""Steam Store ``appreviews`` summary fetcher.

The cached ``appdetails`` payload (see :mod:`.appdetails`) carries only
``recommendations.total`` — a raw review *count*, not the 0-9
``review_score`` / positive-percentage that Steam's library "Steam
Review" sort reads. The frontend writes it to both the
``review_*_with_bombs`` and ``review_*_without_bombs`` overview fields,
because Steam picks the pair from the user's store review preference.

This module hits the public ``appreviews`` summary endpoint to get
those, so the overview-enrichment layer can populate the review sort
for non-Steam shortcuts. Fetched once per real Steam AppID during the
metadata phase and cached in the ``steam_reviews`` namespace.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from unifideck.steam.http_retry import STEAM_STORE_GATE, get_json_with_backoff
from unifideck.utils.config_helpers import get_cfg

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

STEAM_APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
_DEFAULT_TIMEOUT = 15.0


async def fetch_appreviews(
    steam_app_id: int,
    config: ConfigManager | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any] | None:
    """Fetch the review summary for ``steam_app_id``.

    Returns ``{"review_score": int(0-9), "review_percentage": int(0-100),
    "total_reviews": int}`` or ``None`` on any error / no reviews.
    """
    if steam_app_id <= 0:
        return None
    timeout_s = float(get_cfg(
        config, "network.steam_appdetails_timeout", _DEFAULT_TIMEOUT,
    ))
    url = STEAM_APPREVIEWS_URL.format(appid=steam_app_id)
    params = {
        "json": "1",
        "language": "all",
        "purchase_type": "all",
        "num_per_page": "0",  # summary only — we don't need review bodies
    }
    if session is not None:
        return await _request_appreviews(
            session, steam_app_id, url, params, timeout_s,
        )
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session_new:
        return await _request_appreviews(
            session_new, steam_app_id, url, params, timeout_s,
        )


async def _request_appreviews(
    sess: aiohttp.ClientSession,
    steam_app_id: int,
    url: str,
    params: dict[str, str],
    timeout_s: float,
) -> dict[str, Any] | None:
    """GET the appreviews summary on ``sess``; None on error.

    Retries HTTP 429 via :func:`get_json_with_backoff` behind the
    shared ``STEAM_STORE_GATE`` (previously this endpoint had no
    rate-limit handling at all).
    """
    payload = await get_json_with_backoff(
        sess,
        url,
        params=params,
        timeout_s=timeout_s,
        log_tag=f"[steam.appreviews] {steam_app_id}",
        gate=STEAM_STORE_GATE,
    )
    if payload is None:
        return None
    return _parse_appreviews_summary(payload)


def _parse_appreviews_summary(payload: Any) -> dict[str, Any] | None:
    """Extract ``{review_score, review_percentage, total_reviews}`` from a raw
    appreviews payload, or None when absent/malformed/zero reviews."""
    if not isinstance(payload, dict) or not payload.get("success"):
        return None
    summary = payload.get("query_summary")
    if not isinstance(summary, dict):
        return None
    try:
        review_score = int(summary.get("review_score", 0) or 0)
        total_positive = int(summary.get("total_positive", 0) or 0)
        total_reviews = int(summary.get("total_reviews", 0) or 0)
    except (TypeError, ValueError):
        return None
    if total_reviews <= 0:
        return None
    review_percentage = round(total_positive / total_reviews * 100)
    return {
        "review_score": review_score,
        "review_percentage": review_percentage,
        "total_reviews": total_reviews,
    }
