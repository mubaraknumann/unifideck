"""``core/store_urls`` — the per-store web URL tables.

``store_search_url`` was MOVED here from ``rpc/mixins/_metadata_display``
so the launcher could reach it without importing ``rpc`` (import-linter's
``rpc-is-leaf``). The App-Details "Store Page" button renders whatever it
returns, so the move has to be behaviour-preserving down to the string —
hence the exact-value assertions rather than a shape check.
"""
from __future__ import annotations

import pytest

from unifideck.core.store_urls import store_search_url, storefront_url

# The six stores' search URLs, exactly as they read before the move.
_SEARCH_URLS_BEFORE_THE_MOVE = {
    "epic": "https://store.epicgames.com/en-US/browse?q=Hades&sortBy=relevancy",
    "gog": "https://www.gog.com/games?query=Hades",
    "amazon": "https://gaming.amazon.com/home",
    "ubisoft": "https://store.ubisoft.com/us/search?q=Hades",
    "battlenet": "https://us.shop.battle.net/en-us/search?q=Hades",
    "microsoft": "https://www.xbox.com/en-US/games",
}


@pytest.mark.parametrize(
    ("store", "expected"), sorted(_SEARCH_URLS_BEFORE_THE_MOVE.items()),
)
def test_search_urls_survived_the_move_byte_for_byte(
    store: str, expected: str,
) -> None:
    assert store_search_url(store, "Hades") == expected


def test_search_url_encodes_the_title() -> None:
    assert "Hades%20II" in store_search_url("epic", "Hades II")


def test_search_url_is_empty_for_an_unknown_store() -> None:
    assert store_search_url("nope", "Hades") == ""


# ── Storefront landing pages ────────────────────────────────────────


@pytest.mark.parametrize(
    ("store", "host"),
    [
        ("epic", "store.epicgames.com"),
        ("gog", "www.gog.com"),
        ("amazon", "luna.amazon.com"),
        ("microsoft", "www.xbox.com"),
    ],
)
def test_every_browser_store_has_a_shop(store: str, host: str) -> None:
    """Amazon points at Luna.

    It reaches that subdomain signed in only because
    ``AmazonStore.prepare_web_session`` plants auth cookies scoped to
    ``.amazon.com``; a domain cookie is sent to every subdomain. Without
    that step this host loads logged out, which is exactly what it did.
    """
    url = storefront_url(store)
    assert url.startswith("https://")
    assert host in url


@pytest.mark.parametrize("store", ["ubisoft", "battlenet", "steam", "", "nope"])
def test_stores_without_a_reusable_web_session_answer_empty(store: str) -> None:
    """Not an oversight — the empty string is the routing tripwire.

    Ubisoft and Battle.net sign in inside a Wine prefix, so their web
    storefronts would load signed OUT. ``handle_store_storefront`` turns
    this empty answer into a raise, which is how a mis-route surfaces as
    a failure instead of as a mysteriously logged-out shop.
    """
    assert storefront_url(store) == ""
