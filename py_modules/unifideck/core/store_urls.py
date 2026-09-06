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

import urllib.parse

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
_STOREFRONT_URLS: dict[str, str] = {
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
    return ""
