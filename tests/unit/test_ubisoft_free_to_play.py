"""Tests for the Ubisoft free-to-play feed supplement.

Covers the public-CDN payload normalisation, the on-disk TTL cache, and
the manifest ``supplement`` seam (enrich owned F2P titles + inject
unseen ones) — without removing any owned game.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.core.types import Game
from unifideck.stores.ubisoft.id_map import UbisoftIdMap
from unifideck.stores.ubisoft.library.free_to_play import _FreeToPlayFeed
from unifideck.stores.ubisoft.library.manifest import _VisibleManifestProcessor

_SPACE = "11111111-2222-3333-4444-555555555555"

def _norm(name: str) -> str:
    return UbisoftIdMap._normalize_for_matching(name)

class _Cfg:
    def __init__(self, data_dir: str) -> None:
        self.data_dir_expanded = data_dir

class _IdMap:
    def __init__(self) -> None:
        self.merged: dict[str, dict[str, Any]] = {}

    def normalize_for_matching(self, name: str) -> str:
        return _norm(name)

    def merge_entry(self, space_id: str, fields: dict[str, Any]) -> bool:
        self.merged.setdefault(space_id, {}).update(fields)
        return True

    def set_connect_id(
        self,
        space_id: str,
        connect_id: str | None,
        source: str,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        fields = dict(extra or {})
        if connect_id:
            fields["ubisoftconnect_game_id"] = str(connect_id)
            fields["ubisoftconnect_game_id_source"] = source
        return self.merge_entry(space_id, fields)

def _ftp_entry(title: str, space_id: str, product_id: str = "999") -> dict[str, Any]:
    return {
        "title": title,
        "space_id": space_id,
        "install_id": product_id,
        "launch_id": product_id,
        "ubisoftconnect_game_id": product_id,
        "cover_image": "https://cdn/x.jpg",
        "ownership_type": "free",
        "source": "free_feed",
    }

# ── payload normalisation ─────────────────────────────────────────

def test_normalise_payload_filters_and_maps():
    payload = {
        "root": [
            {
                "type": "freetoplay",
                "name": "Brawlhalla",
                "space_id": _SPACE,
                "product_id": "4242",
                "thumb_url": "https://cdn/b.jpg",
            },
            {"type": "premium", "name": "Not Free", "space_id": "x"},
            {"type": "freetoplay", "name": "", "space_id": "y"},  # no title
            {"type": "freetoplay", "name": "No Space"},  # no space_id
        ],
    }
    out = _FreeToPlayFeed._normalise_payload(payload)
    assert out == [
        {
            "title": "Brawlhalla",
            "space_id": _SPACE,
            "install_id": "4242",
            "launch_id": "4242",
            "ubisoftconnect_game_id": "4242",
            "cover_image": "https://cdn/b.jpg",
            "ownership_type": "free",
            "source": "free_feed",
        },
    ]

def test_normalise_payload_handles_garbage():
    assert _FreeToPlayFeed._normalise_payload({}) == []
    assert _FreeToPlayFeed._normalise_payload([]) == []
    assert _FreeToPlayFeed._normalise_payload({"root": ["str", 5]}) == []

# ── disk cache ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_entries_uses_fresh_cache(tmp_path: Path):
    cache = tmp_path / "ubisoft_free_games.json"
    cache.write_text(json.dumps([_ftp_entry("Cached Game", _SPACE)]), encoding="utf-8")
    feed = _FreeToPlayFeed(config=_Cfg(str(tmp_path)))
    out = await feed.fetch_entries()
    assert len(out) == 1
    assert out[0]["title"] == "Cached Game"

@pytest.mark.asyncio
async def test_fetch_entries_network_failure_returns_stale_cache(
    tmp_path: Path, monkeypatch,
):
    cache = tmp_path / "ubisoft_free_games.json"
    cache.write_text(json.dumps([_ftp_entry("Stale", _SPACE)]), encoding="utf-8")
    # Force the cache to look expired so the download path runs…
    import os
    import time as _t

    old = _t.time() - 10 * 24 * 3600
    os.utime(cache, (old, old))
    # …and make the download raise.
    def _boom(_cache_file: Path) -> list[dict[str, Any]]:
        raise OSError("offline")

    feed = _FreeToPlayFeed(config=_Cfg(str(tmp_path)))
    monkeypatch.setattr(feed, "_download_entries", _boom)
    out = await feed.fetch_entries()
    assert out and out[0]["title"] == "Stale"

# ── manifest supplement (enrich + inject, never filter) ───────────

def _processor() -> _VisibleManifestProcessor:
    return _VisibleManifestProcessor(
        config=object(),
        id_map=_IdMap(),
        load_json_file_safe=lambda _p: None,
    )

def test_supplement_injects_unseen_free_game():
    proc = _processor()
    owned = Game(app_id=0, store="ubisoft", store_game_id="owned-1", title="Owned Game")
    entries = [_ftp_entry("Brawlhalla", _SPACE)]
    result = proc.supplement([owned], {}, entries, "free-to-play")
    titles = {g.title for g in result}
    assert titles == {"Owned Game", "Brawlhalla"}
    free = next(g for g in result if g.title == "Brawlhalla")
    assert free.metadata.get("ownership_type") == "free"

def test_supplement_does_not_drop_owned_games():
    proc = _processor()
    owned = [
        Game(app_id=0, store="ubisoft", store_game_id="a", title="Game A"),
        Game(app_id=0, store="ubisoft", store_game_id="b", title="Game B"),
    ]
    # Feed lists something the user already owns — must enrich, not dup/drop.
    entries = [_ftp_entry("Game A", "a")]
    result = proc.supplement(owned, {}, entries, "free-to-play")
    assert len(result) == 2
    assert {g.title for g in result} == {"Game A", "Game B"}

def test_supplement_empty_entries_noop():
    proc = _processor()
    owned = [Game(app_id=0, store="ubisoft", store_game_id="a", title="Game A")]
    assert proc.supplement(owned, {}, [], "free-to-play") == owned
