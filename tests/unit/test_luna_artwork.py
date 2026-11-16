"""Luna box art: from the catalog tile to the artwork service.

The getPage tile carries ``imagePortrait`` / ``imageLandscape``; the sync
stores them in the game's metadata and the artwork service's ``luna`` branch
turns them into the Steam grid (portrait) and wide capsule (landscape).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from unifideck.auth.edge_browser.cookie_reader import cookie_hosts
from unifideck.services.artwork.store_metadata import fetch_store_urls, luna_urls
from unifideck.stores.luna.luna_api.parse import parse_library_page
from unifideck.stores.luna.luna_catalog.models import LunaTitle
from unifideck.stores.luna.luna_catalog.sync import (
    COVER_LANDSCAPE_KEY,
    COVER_PORTRAIT_KEY,
    LunaCatalog,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "luna" / "getpage_my_games.json"


def test_parser_keeps_lunas_https_box_art() -> None:
    library = parse_library_page(json.loads(_FIXTURE.read_text()), "https://luna.amazon.de")
    with_art = [t for t in library.titles if t.cover_portrait]
    assert with_art, "fixture tiles carry imagePortrait"
    for title in with_art:
        assert title.cover_portrait.startswith("https://")  # type: ignore[union-attr]
    assert any(t.cover_landscape for t in library.titles)


def test_sync_puts_box_art_in_metadata() -> None:
    game = LunaCatalog._to_game(LunaTitle(
        luna_product_id="amzn1.luna.product.x", name="X",
        browser_url="https://luna.amazon.de/game/x/B0",
        cover_portrait="https://m.media-amazon.com/p.png",
        cover_landscape="https://m.media-amazon.com/l.png",
    ))
    assert game.metadata[COVER_PORTRAIT_KEY] == "https://m.media-amazon.com/p.png"
    assert game.metadata[COVER_LANDSCAPE_KEY] == "https://m.media-amazon.com/l.png"


def test_sync_omits_missing_box_art() -> None:
    game = LunaCatalog._to_game(LunaTitle(
        luna_product_id="amzn1.luna.product.x", name="X",
        browser_url="https://luna.amazon.de/game/x/B0",
    ))
    assert COVER_PORTRAIT_KEY not in game.metadata
    assert COVER_LANDSCAPE_KEY not in game.metadata


def test_luna_urls_map_portrait_to_grid_and_landscape_to_wide() -> None:
    extras = {COVER_PORTRAIT_KEY: "https://p", COVER_LANDSCAPE_KEY: "https://l"}
    assert luna_urls(extras) == {"grid": "https://p", "grid_l": "https://l"}
    assert luna_urls({}) == {}


async def test_artwork_dispatch_has_a_luna_branch() -> None:
    urls = await fetch_store_urls("luna", "amzn1.luna.product.x", {COVER_PORTRAIT_KEY: "https://p"})
    assert urls == {"grid": "https://p"}


def _cookie_db(profile: Path, rows: list[tuple[str, str, int]]) -> None:
    (profile / "Default").mkdir(parents=True)
    conn = sqlite3.connect(profile / "Default" / "Cookies")
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, has_expires INT, expires_utc INT)",
    )
    conn.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?)", [
        (host, name, 1, expires) for host, name, expires in rows
    ])
    conn.commit()
    conn.close()


def test_cookie_hosts_reads_names_only_and_skips_expired(tmp_path: Path) -> None:
    future = int((time.time() + 11644473600 + 86400) * 1_000_000)
    _cookie_db(tmp_path, [
        (".amazon.de", "at-acbde", future),
        (".amazon.com", "session-id", future),       # no sign-in prefix
        (".amazon.co.uk", "at-acbuk", 1),            # expired
    ])
    assert cookie_hosts(str(tmp_path), ("at-", "sess-at-")) == {"amazon.de"}


def test_cookie_hosts_without_a_profile_is_empty(tmp_path: Path) -> None:
    assert cookie_hosts(str(tmp_path / "missing"), ("at-",)) == set()
