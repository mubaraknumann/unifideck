"""HTTP client for the two Luna web API calls the library sync needs.

1. ``GET https://<luna host>/token`` with the Amazon session cookies of the
   Luna browser profile returns a short-lived access token. It answers HTTP
   200 in both cases; a rejected session has an empty ``token`` and an
   ``invalidReason`` such as ``Unauthenticated``.
2. ``POST https://<tempo host>/getPage`` with that token returns the "my games"
   page: the titles the account can play (parsed by :mod:`.parse`).

Only headers that do not depend on a browser session are sent. Session-bound
values the web player adds (``x-amz-tdi``, ``x-amz-session-id``,
``x-amz-device-serial-number``, ``x-amz-profile-token``) are not available
outside the browser and are deliberately omitted. ``x-amz-locale`` is required:
without it ``getPage`` answers 400.

The client does not retry; ``LunaStore._with_retry`` owns that. It never logs
the token or any cookie value.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from unifideck.auth.edge_browser.chrome_installer import CHROME_VERSION
from unifideck.auth.edge_browser.cookie_reader import read_cookies
from unifideck.stores.luna.luna_api.errors import (
    LunaApiError,
    LunaSessionError,
)
from unifideck.stores.luna.luna_api.parse import ParsedLibrary, parse_library_page

if TYPE_CHECKING:
    import aiohttp

    from unifideck.stores.luna.luna_api.regions import LunaRegion

logger = logging.getLogger(__name__)

#: Version the request claims, in Chrome's reduced ``<major>.0.0.0`` form. Tied
#: to the browser the plugin downloads so the UA and the body never disagree.
BROWSER_VERSION = f"{CHROME_VERSION.split('.')[0]}.0.0.0"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{BROWSER_VERSION} Safari/537.36"
)

#: The ``getPage`` request the Luna web player sends for the "my games" view.
GET_PAGE_BODY: dict[str, Any] = {
    "timeout": 10000,
    "searchContext": {"query": "my_games", "sort": "NEW_RELEASES", "filtersList": []},
    "featureScheme": "WEB_V1",
    "pageContext": {"pageType": "multistate_browse_results", "pageId": "default"},
    "clientContext": {
        "browserMetadata": {
            "browserClientRole": "browser",
            "browserType": "Chrome",
            "browserVersion": BROWSER_VERSION,
            "deviceModel": "unknown",
            "deviceType": "unknown",
            "osName": "Windows",
            "osVersion": "unknown",
        },
        "refMarker": None,
        "referrer": None,
    },
    "inputContext": {"gamepadTypes": []},
    "dynamicFeatures": [],
}

_TIMEOUT_S = 20.0
_UNAUTHORISED = (401, 403)
_HTTP_ERROR_FROM = 400

SessionFactory = Callable[[], "aiohttp.ClientSession"]


def _default_session() -> aiohttp.ClientSession:
    """An aiohttp session with the strict TLS context and a 20 s budget."""
    import aiohttp

    from unifideck.core.net import ssl_ctx_strict

    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=ssl_ctx_strict()),
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
    )


class LunaApiClient:
    """Fetch the Luna library of the account signed in to ``profile_dir``."""

    def __init__(
        self,
        region: LunaRegion,
        profile_dir: str,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        """Bind the client to one region and one browser profile."""
        self._region = region
        self._profile_dir = profile_dir
        self._session_factory = session_factory or _default_session

    @property
    def site(self) -> str:
        """The Luna site URL, e.g. ``https://luna.amazon.de``."""
        return f"https://{self._region.luna_host}"

    async def fetch_access_token(self) -> str:
        """Exchange the profile's session cookies for an access token."""
        cookies = await asyncio.to_thread(
            read_cookies, self._profile_dir, self._region.cookie_suffix,
        )
        if not cookies:
            raise LunaSessionError("no_cookies")
        header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        headers = {"Cookie": header, "User-Agent": USER_AGENT, "Accept": "application/json"}
        status, payload = await self._request("GET", f"{self.site}/token", headers, None)
        _raise_for_status("token", status)
        if not isinstance(payload, dict):
            msg = "Luna token response is not an object"
            raise LunaApiError(msg)
        reason = payload.get("invalidReason")
        token = payload.get("token")
        if reason:
            raise LunaSessionError(str(reason))
        if not isinstance(token, str) or not token:
            raise LunaSessionError("empty_token")
        return token

    async def fetch_library(self) -> ParsedLibrary:
        """Fetch and parse the account's "my games" page."""
        token = await self.fetch_access_token()
        status, payload = await self._request(
            "POST",
            f"https://{self._region.tempo_host}/getPage",
            self.get_page_headers(token),
            json.dumps(GET_PAGE_BODY, separators=(",", ":")),
        )
        _raise_for_status("getPage", status)
        library = parse_library_page(payload, self.site)
        logger.info(
            "[LunaApi] getPage: %d title(s), %d malformed tile(s)",
            len(library.titles), library.malformed,
        )
        return library

    def get_page_headers(self, token: str) -> dict[str, str]:
        """The ``getPage`` request headers (no session-bound values)."""
        region = self._region
        return {
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": self.site,
            "Referer": f"{self.site}/",
            "x-amz-access-token": token,
            "x-amz-locale": region.locale,
            "x-amz-marketplace-id": region.marketplace_id,
            "x-amz-country-of-residence": region.country,
            "x-amz-device-type": "browser",
            "x-amz-platform": "web",
        }

    async def _request(
        self, method: str, url: str, headers: dict[str, str], body: str | None,
    ) -> tuple[int, object]:
        """Send one request; return ``(status, parsed JSON or None)``."""
        import aiohttp

        try:
            async with (
                self._session_factory() as session,
                session.request(method, url, headers=headers, data=body) as resp,
            ):
                status = resp.status
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            msg = f"Luna request failed: {type(exc).__name__}"
            raise LunaApiError(msg) from exc
        if status >= _HTTP_ERROR_FROM:
            return status, None
        try:
            return status, json.loads(text)
        except ValueError as exc:
            msg = f"Luna response is not JSON (HTTP {status})"
            raise LunaApiError(msg) from exc


def _raise_for_status(call: str, status: int) -> None:
    """Map an HTTP error status onto the client's error types."""
    if status in _UNAUTHORISED:
        raise LunaSessionError(f"http_{status}")
    if status >= _HTTP_ERROR_FROM:
        msg = f"Luna {call} returned HTTP {status}"
        raise LunaApiError(msg)
