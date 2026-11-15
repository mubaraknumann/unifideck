"""A clean login form for every browser sign-in, with nothing declared per store.

The sign-in browser keeps one profile for every store (the shop windows and
Xbox Cloud Gaming reuse its sessions). A store session left in it makes that
store's sign-in page redirect at once: past the login form, with whichever
account signed in last, and for EA before the redirect capture is even armed
(2026-11-15). So before each sign-in the orchestrator clears the cookies of the
store's own sites.

The sites come from what every flow already hands ``AuthOrchestrator.run_flow``:
the sign-in URL, the redirect prefixes, the content-capture page and the
intercept pattern. Each host is reduced to its site (the last two labels:
``legendary.epicgames.com`` -> ``epicgames.com``), so a new store needs no
cookie list of its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from .browser_types import ContentCapture, RedirectIntercept

logger = logging.getLogger(__name__)


def site_of(url: str) -> str | None:
    """The site a URL (or ``host/path`` fragment) belongs to; None for localhost and bare names."""
    host = urlsplit(url if "://" in url else f"https://{url}").hostname
    if not host or "." not in host or host.replace(".", "").isdigit():
        return None
    return ".".join(host.split(".")[-2:])


def sign_in_sites(
    url: str, allowed_uris: Iterable[str],
    content: ContentCapture | None, intercept: RedirectIntercept | None,
) -> list[str]:
    """Every site this sign-in flow names, sorted."""
    sources = [url, *allowed_uris]
    if content is not None:
        sources.append(content.trigger_url)
    if intercept is not None:
        sources.append(intercept.url_pattern.replace("*", ""))
    return sorted({site for site in map(site_of, sources) if site})


async def clear_sign_in_cookies(sites: Iterable[str]) -> None:
    """Delete these sites' cookies from the shared sign-in profile (best effort)."""
    from .edge_browser.edge import EdgeBrowser

    for site in sites:
        try:
            await asyncio.to_thread(EdgeBrowser.clear_site_cookies, site)
        except Exception as e:
            logger.warning("[auth] could not clear %s cookies before sign-in: %s", site, e)
