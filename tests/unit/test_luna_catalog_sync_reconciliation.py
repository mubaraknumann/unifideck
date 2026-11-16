"""Property test for the Luna catalog sync reconciliation.

``LunaCatalog.sync()`` turns the API's parsed library into the authoritative
value the sync layer writes back into ``library_cache.json``. The contract
(``LunaCatalog._reconcile``) is pinned across arbitrary (previous-entries,
retrieved-titles, malformed-count) combinations:

* a retrieved title list is **authoritative** — a previously synced title
  that is no longer present drops out by being absent (Req 4.8);
* zero titles and nothing malformed removes every Luna entry (Req 4.9);
* when tiles were **malformed**, the pre-existing entries not produced this
  run are **preserved unchanged** rather than dropped (Req 4.7);
* a fetch **failure** propagates (the store keeps the previous entries).

``previous_entries`` are serialized into a temp ``library_cache.json`` the way
the sync layer persists them and read back, so the writer sees the same bytes
production would.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from unifideck.launcher.browser_games import BROWSER_URL_KEY
from unifideck.stores.luna.luna_api import LunaApiError, ParsedLibrary
from unifideck.stores.luna.luna_catalog import (
    LUNA_STORE_TAG,
    CatalogSyncStatus,
    LunaCatalog,
    LunaTitle,
)

LUNA_HOST = "https://luna.amazon.de"

# A small id alphabet so the previous and fetched id sets overlap often.
_id_tails = st.text("0123456789abcdef", min_size=1, max_size=6)
_product_ids = _id_tails.map(lambda t: f"amzn1.adg.product.{t}")
_names = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=1,
    max_size=30,
)


def _url(product_id: str) -> str:
    return f"{LUNA_HOST}/game/slug-{product_id[-6:]}/B0{product_id[-6:].upper():0>8}"


def _title(product_id: str, name: str) -> LunaTitle:
    return LunaTitle(luna_product_id=product_id, name=name, browser_url=_url(product_id))


# Fetched ids are NOT unique: the reconciler must dedupe a repeated id.
_fetched = st.lists(st.tuples(_product_ids, _names), max_size=6).map(
    lambda pairs: [_title(pid, nm) for pid, nm in pairs],
)
_malformed = st.integers(min_value=0, max_value=3)
_previous_ids = st.lists(_product_ids, max_size=6, unique=True)


def _serialize_previous(ids: list[str], tmp_path: Path) -> list[dict[str, Any]]:
    """Write ``ids`` as Luna entries to a temp cache and read them back."""
    entries = [{
        "app_id": 0,
        "store": "luna",
        "store_game_id": pid,
        "title": pid,
        "installed": False,
        "tags": ["browser", LUNA_STORE_TAG],
        "metadata": {BROWSER_URL_KEY: _url(pid)},
    } for pid in ids]
    cache_path = tmp_path / "library_cache.json"
    payload = {"last_sync_time": None, "libraries": {"luna": entries}}
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    return list(loaded["libraries"]["luna"])


def _catalog(library: ParsedLibrary) -> LunaCatalog:
    async def fetch() -> ParsedLibrary:
        return library

    return LunaCatalog(fetch)


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    deadline=None,
)
@given(fetched=_fetched, malformed=_malformed, previous_ids=_previous_ids)
def test_luna_catalog_sync_reconciliation(
    fetched: list[LunaTitle],
    malformed: int,
    previous_ids: list[str],
    tmp_path: Path,
) -> None:
    """The synced id set is the fetched ids, plus carried entries on malformed.

    Validates: Requirements 4.7, 4.8, 4.9
    """
    previous_entries = _serialize_previous(previous_ids, tmp_path)
    valid_ids = {t.luna_product_id for t in fetched}
    catalog = _catalog(ParsedLibrary(titles=fetched, malformed=malformed))

    result = asyncio.run(catalog.sync(previous_entries))

    synced_ids = [g.store_game_id for g in result.games]
    # One entry per product id, even when the fetched list repeats one.
    assert len(synced_ids) == len(set(synced_ids))
    for game in result.games:
        assert game.store == "luna"
        assert "browser" in game.tags
        assert LUNA_STORE_TAG in game.tags
        assert game.metadata[BROWSER_URL_KEY] == _url(game.store_game_id)

    if malformed:
        assert result.status is CatalogSyncStatus.SYNCED_WITH_MALFORMED
        assert set(synced_ids) == valid_ids | set(previous_ids)
    elif not fetched:
        assert result.status is CatalogSyncStatus.SYNCED_ZERO
        assert result.games == []
    else:
        assert result.status is CatalogSyncStatus.SYNCED
        assert set(synced_ids) == valid_ids


def test_first_occurrence_wins_on_duplicate_id() -> None:
    first = _title("amzn1.adg.product.a", "First")
    second = _title("amzn1.adg.product.a", "Second")
    result = asyncio.run(_catalog(ParsedLibrary([first, second], 0)).sync([]))
    assert [g.title for g in result.games] == ["First"]


def test_asin_is_kept_in_metadata() -> None:
    title = LunaTitle(
        luna_product_id="amzn1.luna.product.x", name="X",
        browser_url=f"{LUNA_HOST}/game/x/B0ABCDEFGH", asin="B0ABCDEFGH",
    )
    result = asyncio.run(_catalog(ParsedLibrary([title], 0)).sync([]))
    assert result.games[0].metadata["asin"] == "B0ABCDEFGH"


def test_fetch_failure_propagates() -> None:
    """A fetch error is not turned into an (authoritative) result."""
    async def fetch() -> ParsedLibrary:
        raise LunaApiError("Luna getPage returned HTTP 503")

    with pytest.raises(LunaApiError):
        asyncio.run(LunaCatalog(fetch).sync([]))
