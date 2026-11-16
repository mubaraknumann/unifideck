"""Property test for the Luna browser-game library-entry round-trip (task 7.2).

A Luna title is persisted as an ordinary **browser game of kind ``stream``**
in ``library_cache.json``: the catalog writer (``LunaCatalog._to_game``) tags
it ``browser`` + ``luna`` and sets ``metadata["browser_url"]`` to the game's
page URL from the API (``{luna_host}/game/<slug>/<ASIN>``); the launcher resolves it back
through ``browser_games.cached_game`` / ``browser_games.browser_target`` into a
``BrowserTarget{url, kind="stream"}``. These two halves live in different
modules (the store-side writer and the stdlib-only launcher reader) and only
share bytes on disk, so the invariant worth pinning is that a title survives
the trip through ``library_cache.json`` **losslessly**.

The invariant under test (design **Property 5**, Req 4.3/4.4/4.5):

    for any valid Luna title (product id, name, page URL), writing it as a
    browser-game entry and reading it back yields the same
    ``luna_product_id``, the same page URL, kind ``stream``, and a host that
    passes ``is_allowed_browser_url``.

The test drives the REAL writer and the REAL launcher reader over a temp
``library_cache.json`` serialized exactly the way the sync layer serializes it
(``dataclasses.asdict`` through a JSON round-trip, which renders the
``GameTag`` StrEnum members as the plain ``"browser"`` / ``"luna"`` strings the
launcher sees). Product ids, names, slugs, ASINs and regional Luna hosts are
all varied.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from unifideck.launcher import browser_games
from unifideck.launcher.browser_games import (
    BrowserTarget,
    browser_target,
    cached_game,
    is_allowed_browser_url,
)
from unifideck.stores.luna.luna_catalog import LunaCatalog, LunaTitle

# ── Generators constraining to the real Luna title / host input space ──

# A UUID-shaped product-id tail. Luna product ids are
# ``amzn1.adg.product.<UUID>`` or ``amzn1.luna.product.<UUID>``.
_HEX = "0123456789abcdef"
_uuid = st.builds(
    lambda a, b, c, d, e: f"{a}-{b}-{c}-{d}-{e}",
    st.text(_HEX, min_size=8, max_size=8),
    st.text(_HEX, min_size=4, max_size=4),
    st.text(_HEX, min_size=4, max_size=4),
    st.text(_HEX, min_size=4, max_size=4),
    st.text(_HEX, min_size=12, max_size=12),
)
_product_ids = st.builds(
    lambda kind, u: f"amzn1.{kind}.product.{u}",
    st.sampled_from(["adg", "luna"]), _uuid,
)
_slugs = st.text("abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=30)
_asins = st.one_of(
    st.none(), st.text("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=10, max_size=10),
)

# Title names: any non-trivial unicode text, including blanks that the writer
# falls back to the product id for. Kept clear of control chars that a JSON
# round-trip would alter.
_names = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=0,
    max_size=60,
)

# Regional Luna sites in the structural form the allow-list accepts
# (``luna.amazon.<tld>`` / ``luna.amazon.<tld>.<tld>``).
_tlds = st.sampled_from(["de", "co.uk", "fr", "it", "es", "ca", "com"])
_luna_hosts = _tlds.map(lambda tld: f"https://luna.amazon.{tld}")


def _write_cache(path: Path, entry: dict[str, Any]) -> None:
    """Serialize one Luna entry into ``library_cache.json`` exactly as the sync
    layer does: ``asdict`` through a JSON round-trip (so the ``GameTag`` enum
    members become the plain ``"browser"`` / ``"luna"`` strings the launcher
    reads)."""
    payload = {"last_sync_time": None, "libraries": {"luna": [entry]}}
    path.write_text(json.dumps(payload), encoding="utf-8")


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    deadline=None,
)
@given(
    product_id=_product_ids, name=_names, luna_host=_luna_hosts,
    slug=_slugs, asin=_asins,
)
def test_luna_library_entry_round_trips(
    product_id: str,
    name: str,
    luna_host: str,
    slug: str,
    asin: str | None,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Feature: amazon-luna-streaming, Property 5: Browser-game library entry round-trip.

    For any valid Luna title, writing it to ``library_cache.json`` as a
    browser-game entry (tag ``browser`` + ``metadata["browser_url"]`` set to
    the page URL) and reading it back via ``cached_game`` / ``browser_target``
    yields the same product id, the same URL, kind ``stream``, and a host that
    passes ``is_allowed_browser_url``.

    Validates: Requirements 4.3, 4.4, 4.5
    """
    path = f"/game/{slug}/{asin}" if asin else f"/game/{slug}"
    expected_url = f"{luna_host}{path}"

    # Build the on-disk entry with the REAL catalog writer, then serialize it
    # the way the sync layer persists it (asdict -> JSON -> dict), so the
    # launcher reads exactly what production would write.
    game = LunaCatalog._to_game(LunaTitle(
        luna_product_id=product_id, name=name, browser_url=expected_url, asin=asin,
    ))
    entry = json.loads(json.dumps(asdict(game)))

    cache_path = tmp_path / "library_cache.json"
    _write_cache(cache_path, entry)

    # Point the stdlib-only launcher reader at the temp cache. The entry is a
    # per-example fresh file, so the function-scoped monkeypatch health check
    # is suppressed above intentionally.
    monkeypatch.setattr(browser_games, "LIBRARY_CACHE", str(cache_path))

    # The record is found by its product id (store_game_id), unchanged.
    record = cached_game("luna", product_id)
    assert record is not None
    assert record["store_game_id"] == product_id
    assert "browser" in record["tags"]
    assert "luna" in record["tags"]
    assert record["metadata"]["browser_url"] == expected_url

    # And it resolves back to a lossless stream BrowserTarget.
    target = browser_target("luna", product_id)
    assert target == BrowserTarget(url=expected_url, kind="stream")
    assert target is not None
    assert target.kind == "stream"
    assert target.url == expected_url
    assert is_allowed_browser_url(target.url, "stream")
