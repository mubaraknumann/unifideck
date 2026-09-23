from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.core.types import Result
from unifideck.launcher.types.context import LaunchContext
from unifideck.launcher.types.errors import DependencyMissingError, GameNotFoundError

from .browser_window import wait_for_browser_exit

if TYPE_CHECKING:
    from unifideck.auth.edge_browser import EdgeBrowser
logger = logging.getLogger(__name__)
_AUTH_URL_FILES = {
    "epic": "epic_auth_url.txt",
    "gog": "gog_auth_url.txt",
    "amazon": "amazon_auth_url.txt",
    "microsoft": "ms_auth_url.txt",
    "itch": "itch_auth_url.txt",
}
_AUTH_STORE_LABELS = {
    "epic": "Epic Games",
    "gog": "GOG",
    "amazon": "Amazon Games",
    "microsoft": "Microsoft",
    "itch": "itch.io",
}
_MAX_AUTH_SECONDS = 600
def _read_config_int(key: str, default: int) -> int:
    """Read config int."""
    from unifideck.utils.config_helpers import read_config_int_cold_start
    return read_config_int_cold_start(key, default)
def read_auth_url(store: str) -> str:
    """The OAuth URL the backend wrote for ``store``, or raise.

    Shared with the storefront flow, which redeems the same file to
    run a token reconcile once the shop window closes.
    """
    filename = _AUTH_URL_FILES.get(store)
    if filename is None:
        raise GameNotFoundError(
            f"Unknown auth store {store!r}",
            context={"store": store},
        )
    url_file = Path(
        f"~/.local/share/unifideck/{filename}",
    ).expanduser()
    if not url_file.is_file():
        raise GameNotFoundError(
            f"Auth URL file not found: {url_file}",
            context={"store": store, "file": str(url_file)},
        )
    try:
        url = url_file.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise GameNotFoundError(
            f"Cannot read auth URL file: {e}",
            context={"store": store, "file": str(url_file)},
        ) from e
    if not url:
        raise GameNotFoundError(
            f"Auth URL file is empty: {url_file}",
            context={"store": store},
        )
    return url

async def handle_store_auth(
 ctx: LaunchContext,
 edge_browser: EdgeBrowser,
) -> Result:

    """Handle store auth."""
    store = ctx.auth_store
    if store is None:
        raise GameNotFoundError(
            "handle_store_auth called without auth_store set",
            context={"game_key": ctx.game_key},
        )
    # Ubisoft doesn't use browser-based OAuth — UPC (Ubisoft
    # Connect) runs in a dedicated Wine prefix. The session
    # monitor was already started by ``UbisoftAuth.start_auth()``
    # in the plugin process; the launcher just needs to return
    # so the session monitor can do its work.
    if store == "ubisoft":
        logger.info("[launcher.auth] Ubisoft — session monitor active, exiting")
        return Result(success=True, store="ubisoft")
    label = _AUTH_STORE_LABELS.get(store, store.title())
    logger.info(
        "[launcher.auth] launching %s OAuth flow", label,
    )
    if not edge_browser.is_installed:
        raise DependencyMissingError(
            "Microsoft Edge flatpak required for OAuth",
            context={"store": store},
        )
    auth_url = read_auth_url(store)
    logger.info(
        "[launcher.auth] %s auth URL resolved (%d chars)",
        label, len(auth_url),
    )
    started = edge_browser.launch_auth(auth_url)
    if not started:
        return Result(
            success=False,
            error="edge_auth_launch_failed",
            store=store,
        )
    await _wait_for_auth_end(edge_browser)
    logger.info(
        "[launcher.auth] %s auth browser closed", label,
    )
    return Result(success=True, store=store)
async def _wait_for_auth_end(edge_browser: EdgeBrowser) -> None:
    """Wait for auth end."""
    max_seconds = _read_config_int(
        "launcher.auth_max_seconds", _MAX_AUTH_SECONDS,
    )
    await wait_for_browser_exit(
        edge_browser, max_seconds, log_tag="launcher.auth",
    )

