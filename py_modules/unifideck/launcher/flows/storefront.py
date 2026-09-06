"""launcher/flows/storefront.py — open a store's shop, signed in.

Third sibling of ``flows/auth.py`` (sign-in) and ``flows/xcloud.py``
(game streaming). Runs in the launcher subprocess, under the SYSTEM
python3, launched by Steam via a temporary shortcut — because in
Gaming Mode a window from a process Steam did not launch has no
gamescope session and never renders.

Only the four **browser-OAuth** stores reach this module. Ubisoft and
Battle.net authenticate inside a Wine prefix, so they have no browser
session and their shop is the vendor client's own Store/Shop tab;
``LauncherService._handle_auth_path`` routes them to
``_launch_wrapper_client`` *before* this module is consulted. A
wrapper store arriving here is a routing bug, and
:func:`_resolve_storefront_url` raises rather than opening a
signed-out web page.

Two properties are load-bearing and easy to break:

1. **Cookies are never cleared on this path.** The shared Edge
   profile's live web session IS the signed-in shop. The four
   ``clear_store_cookies`` call sites exist to force a fresh login
   form before a real sign-in; running one here would guarantee the
   signed-out page this flow exists to avoid.
2. **No ``STORE_AUTH_*`` event is ever emitted.** A ``STORE_AUTH_FAILED``
   would flip the store's row to ``error``, where the settings UI
   renders no button at all — stranding the user with no way to sign
   in or out. A shop that failed to open must leave auth state alone.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from unifideck.core.store_urls import storefront_url
from unifideck.core.types import Result
from unifideck.launcher.types.errors import (
    DependencyMissingError,
    GameNotFoundError,
)

from .auth import wait_for_browser_exit

if TYPE_CHECKING:
    from unifideck.auth.edge_browser import EdgeBrowser
    from unifideck.launcher.types.context import LaunchContext

logger = logging.getLogger(__name__)

# Ceiling on one shop session. Browsing a store is not signing in —
# 600s (the auth ceiling) would kill the window mid-purchase.
#
# COUPLED to TEMP_SHORTCUT_SAFETY_CLEANUP_MS in
# ``src/lib/steam-bridge/temp-shortcut.ts``: that timer removes the
# temporary shortcut, which ends the gamescope session and destroys
# this window. It must stay strictly LARGER than this ceiling. Change
# one, change the other.
_MAX_STOREFRONT_SECONDS = 1800

def _read_config_int(key: str, default: int) -> int:
    """Read an int from the merged config, cold-start safe."""
    from unifideck.utils.config_helpers import read_config_int_cold_start
    return read_config_int_cold_start(key, default)


def _resolve_storefront_url(store: str | None) -> str:
    """The shop URL for ``store``, raising if it has none.

    ``storefront_url`` answers ``""`` for the wrapper stores and for
    unknown ids. Both mean the request was mis-routed, so this fails
    loudly instead of handing an empty URL to the browser.
    """
    url = storefront_url(store or "")
    if not url:
        raise GameNotFoundError(
            f"no storefront URL for store {store!r}",
            context={"store": store or ""},
        )
    return url


def _busy_flavour(edge_browser: EdgeBrowser) -> str | None:
    """Which other Edge window is holding the shared profile, if any.

    Chromium refuses a second process on one ``--user-data-dir``: the
    new invocation hands its URL to the running instance over
    ``SingletonSocket`` and exits at once, its own flags ignored. That
    would leave us exiting immediately (so Steam tears down the
    shortcut) while a window opened inside *another* app's gamescope
    session — invisible behind an xCloud kiosk, or hijacking a live
    sign-in.

    So probe the per-flavour CDP ports first and refuse rather than
    spawn something doomed. A *leaked* auth instance self-heals:
    ``EdgeBrowser.prepare_auth_launch`` closes lingering auth targets
    before every real sign-in.
    """
    if edge_browser.cdp_alive(edge_browser.cdp_port):
        return "auth"
    if edge_browser.cdp_alive(edge_browser.xcloud_cdp_port()):
        return "xcloud"
    return None


async def _reuse_open_storefront(
    edge_browser: EdgeBrowser, url: str,
) -> bool:
    """Steer an already-open shop window at ``url``.

    Pressing the cart for Epic and then for GOG must not try to spawn
    a second Chromium. Navigating the live window is both correct and
    what the user expects. Returns False when there is nothing to
    reuse, or when the navigation failed and the stale targets have
    been closed so the caller can spawn fresh.
    """
    port = edge_browser.storefront_cdp_port()
    if not edge_browser.cdp_alive(port):
        return False
    logger.info(
        "[launcher.storefront] reusing open store window on port %d", port,
    )
    if await edge_browser.navigate_on_port(port, url):
        return True
    logger.warning(
        "[launcher.storefront] navigate failed on port %d — "
        "closing stale targets and respawning", port,
    )
    await edge_browser.close_targets_on_port(port, log_prefix="storefront")
    return False


async def _spawn_and_wait(
    edge_browser: EdgeBrowser, url: str, store: str,
) -> Result:
    """Spawn the shop window and outlive it.

    The wait is the point: Steam ends the shortcut's gamescope session
    the moment this process exits, and that destroys the window. Only
    reached when we actually own the browser process — the reuse path
    must NOT wait, because the window it steered belongs to another
    launcher process's session and this one has nothing to guard.
    """
    if not edge_browser.launch_storefront(url):
        return Result(
            success=False,
            store=store,
            error="edge_storefront_launch_failed",
        )
    await wait_for_browser_exit(
        edge_browser,
        _read_config_int(
            "launcher.storefront_max_seconds", _MAX_STOREFRONT_SECONDS,
        ),
        log_tag="launcher.storefront",
    )
    logger.info("[launcher.storefront] %s store window closed", store)
    return Result(success=True, store=store)


async def handle_store_storefront(
    ctx: LaunchContext,
    edge_browser: EdgeBrowser,
) -> Result:
    """Open ``ctx``'s store shop in the shared Edge profile."""
    store = ctx.auth_store or ctx.store or ""
    url = _resolve_storefront_url(store)
    if not edge_browser.is_installed:
        raise DependencyMissingError(
            "Microsoft Edge flatpak required for the store browser",
            context={"store": store or ""},
        )
    busy = _busy_flavour(edge_browser)
    if busy is not None:
        logger.warning(
            "[launcher.storefront] refusing: an Edge %s window already "
            "holds the shared profile", busy,
        )
        return Result(
            success=False, store=store, error=f"edge_busy_{busy}",
        )
    logger.info("[launcher.storefront] opening %s store", store)
    if await _reuse_open_storefront(edge_browser, url):
        # Someone else's launcher owns that window and is already
        # waiting on it. Exiting now is correct — and necessary, or
        # this shortcut would sit "running" for the full ceiling.
        return Result(success=True, store=store)
    return await _spawn_and_wait(edge_browser, url, store)
