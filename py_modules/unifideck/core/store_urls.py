"""core/store_urls.py — Per-store web URLs (search + storefront landing).

Two related tables live here:

* :func:`store_search_url` — a per-store *search* URL for one game
  title. Powers the App-Details "Store Page" button for shortcuts
  with no real Steam store presence. Moved verbatim from
  ``rpc/mixins/_metadata_display.py`` so the launcher can reach it.
* :func:`storefront_url` — the per-store *shop landing page*, opened
  by the QAM "Store Connections" cart button.

Why ``core/`` and not ``rpc/`` or ``stores/``
---------------------------------------------
The game launcher (``bin/unifideck-launcher``) runs out-of-process
under the SYSTEM ``python3`` (3.10-3.14), not Decky's bundled 3.11,
and it needs :func:`storefront_url`. Two constraints follow:

1. import-linter's ``rpc-is-leaf`` contract forbids anything
   importing ``unifideck.rpc``, which is where ``store_search_url``
   used to live.
2. A ``stores/`` module is unsafe from the launcher: the store
   package ``__init__`` chains pull in auth → security →
   ``cryptography``, whose native ``_cffi_backend`` is not
   importable there (see ``launcher/dispatcher._resolve_exe_from_install``
   for the same reasoning).

So this module is stdlib-only and lives beside ``core.exe_finder``,
the existing precedent for "dependency-light and launcher-safe".

Storefront sessions
-------------------
The four browser-OAuth stores (Epic, GOG, Amazon, Microsoft) sign in
inside the shared Edge profile, so opening these URLs in that same
profile reuses the live web session. The two *wrapper* stores
(Ubisoft, Battle.net) sign in inside a Wine prefix and have no
browser session at all — their shop is the vendor client's own
Store/Shop tab. :func:`storefront_url` therefore returns ``""`` for
them **on purpose**: a mis-routed wrapper store fails loudly in
``launcher/flows/storefront.py`` instead of quietly opening a
signed-out web page.
"""
from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

# Shop landing pages for the browser-OAuth stores only.
#
# Each host is chosen to match where that store's OAuth flow actually
# plants its cookies, so the shared Edge profile carries the session:
#   epic      — login lands on www.epicgames.com; .epicgames.com
#               cookies reach the store subdomain.
#   gog       — login lands on auth.gog.com / login.gog.com; .gog.com
#               cookies reach www.
#   amazon    — luna.amazon.com. It reaches that subdomain signed in
#               only because ``AmazonStore.prepare_web_session`` plants
#               auth cookies scoped to ``.amazon.com``: nile signs in
#               through Amazon's device-registration flow, which leaves
#               the browser holding tracking cookies and no session, so
#               this page loaded logged out. A domain cookie is sent to
#               every subdomain, which is what makes it work now.
#               Prime Gaming (gaming.amazon.com), where nile's own
#               titles are claimed, is signed in by the same cookies if
#               this should ever move.
#   microsoft — /play is the Game Pass cloud catalogue, and xbox.com is
#               the domain EdgeProfileManager.has_xbox_session() reads,
#               i.e. the one we know the session is planted on.
#   itch      — the front page. The sign-in leaves an itch.io session in
#               the shared Edge profile (the user signs in on itch.io
#               itself), so the shop opens signed in with no cookie work.
_STOREFRONT_URLS: dict[str, str] = {
    "itch": "https://itch.io/",
    "epic": "https://store.epicgames.com/",
    "gog": "https://www.gog.com/",
    "amazon": "https://luna.amazon.com/",
    "microsoft": "https://www.xbox.com/play",
}


def storefront_url(store: str) -> str:
    """The shop landing page for ``store``, or ``""`` if it has none.

    ``""`` is returned for the wrapper stores (Ubisoft, Battle.net)
    and for any unknown id. Callers must treat that as a hard error
    rather than a URL to open — see the module docstring.
    """
    return _STOREFRONT_URLS.get(store, "")


#: A one-time page a store asks the next shop window to open instead of
#: its landing page: itch.io's bundles page, where an unclaimed bundle game
#: is claimed before it installs. Written by the backend, taken (read and
#: removed) by ``launcher/flows/storefront.py``. Older than this, it is
#: left over from a window that never opened and is ignored.
_TARGET_MAX_AGE_S = 300


def storefront_target_path(store: str) -> Path:
    """Where the backend leaves ``store``'s one-time shop page."""
    return Path.home() / ".local" / "share" / "unifideck" / f"storefront_target_{store}.url"


def _same_site(store: str, url: str) -> bool:
    """``url`` is https on ``store``'s shop host or one of its subdomains."""
    home = urllib.parse.urlparse(storefront_url(store)).hostname or ""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    return bool(home) and parsed.scheme == "https" and (
        host == home or host.endswith("." + home)
    )


def take_storefront_target(store: str) -> str:
    """The one-time page for ``store``'s next shop window, or ``""``.

    Removed as it is read, so it opens once. A page that is stale or not
    on the store's own site is dropped: this file decides what a signed-in
    browser opens.
    """
    path = storefront_target_path(store)
    try:
        age = time.time() - path.stat().st_mtime
        url = path.read_text(encoding="utf-8").strip()
        path.unlink()
    except OSError:
        return ""
    return url if age <= _TARGET_MAX_AGE_S and _same_site(store, url) else ""


def store_search_url(store: str, title: str) -> str:
    """Build a fallback store landing URL for non-Steam stores.

    Used by the "Store Page" button when the shortcut has no real
    Steam store presence.
    """
    encoded = urllib.parse.quote(title or "")
    if store == "epic":
        return f"https://store.epicgames.com/en-US/browse?q={encoded}&sortBy=relevancy"
    if store == "gog":
        return f"https://www.gog.com/games?query={encoded}"
    if store == "amazon":
        return "https://gaming.amazon.com/home"
    if store == "ubisoft":
        return f"https://store.ubisoft.com/us/search?q={encoded}"
    if store == "battlenet":
        return f"https://us.shop.battle.net/en-us/search?q={encoded}"
    if store == "microsoft":
        return "https://www.xbox.com/en-US/games"
    if store == "itch":
        return f"https://itch.io/search?q={encoded}"
    return ""
