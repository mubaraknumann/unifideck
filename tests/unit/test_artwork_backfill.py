"""Regression: artwork must backfill missing kinds per-sync, not strand them.

`for-pr-0.7` replaced staging's per-type ``get_missing_artwork_types`` +
``only_types`` incremental backfill with a coarse grid+hero ``has_artwork``
gate. Once a game had grid+hero it was treated as "done", so logo / icon /
landscape were never fetched again — the live library held 7 icon files for
1196 shortcuts. These tests pin the per-kind detection and the
``only_kinds`` backfill + incremental-skip behaviour.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from unifideck.services.artwork import fetcher
from unifideck.services.artwork.service import ArtworkService

# Signed appid with the top bit set, like a real Unifideck shortcut.
_APP = -1234567890
_UID = _APP + 0x100000000


def _touch(d: Path, name: str) -> None:
    (d / name).write_bytes(b"x")


# ── get_missing_kinds / has_artwork ───────────────────────────────


@pytest.mark.asyncio
async def test_missing_kinds_empty_dir(tmp_path):
    assert await fetcher.get_missing_kinds(str(tmp_path), _APP) == {
        "grid", "grid_l", "hero", "logo", "icon",
    }
    assert await fetcher.has_artwork(str(tmp_path), _APP) is False


@pytest.mark.asyncio
async def test_missing_kinds_grid_hero_present_strands_rest(tmp_path):
    # The exact regression: grid + hero present (the old gate) must NOT
    # mark the game complete — landscape/logo/icon are still missing.
    _touch(tmp_path, f"{_UID}p.jpg")        # grid
    _touch(tmp_path, f"{_UID}_hero.png")    # hero (png variant)
    assert await fetcher.get_missing_kinds(str(tmp_path), _APP) == {
        "grid_l", "logo", "icon",
    }
    assert await fetcher.has_artwork(str(tmp_path), _APP) is False


@pytest.mark.asyncio
async def test_missing_kinds_all_present(tmp_path):
    for name in (f"{_UID}p.png", f"{_UID}.jpg", f"{_UID}_hero.jpg",
                 f"{_UID}_logo.png", f"{_UID}_icon.png"):
        _touch(tmp_path, name)
    assert await fetcher.get_missing_kinds(str(tmp_path), _APP) == set()
    assert await fetcher.has_artwork(str(tmp_path), _APP) is True


# ── fetch_artwork: only_kinds backfill + incremental-skip ─────────


class _FakeCache:
    """Minimal CacheManager stand-in: namespaced key/value store."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], object] = {}

    def get(self, cache: str, key: str):
        return self.store.get((cache, key))

    def set(self, cache: str, key: str, value, *, flush: bool = True) -> None:
        self.store[(cache, key)] = value

    def flush(self, cache: str) -> None:
        pass

    def clear(self, cache: str) -> None:
        for k in [k for k in self.store if k[0] == cache]:
            del self.store[k]


def _service(tmp_path) -> ArtworkService:
    svc = ArtworkService(
        bus=MagicMock(), cache=_FakeCache(),
        grid_dir=str(tmp_path), api_key="k", config=None,
    )

    async def _noop(*_a, **_k):
        return None

    # Stub the store + Steam-CDN phases; the SGDB phase is replaced
    # per-test so we can assert what `only_kinds` it received.
    svc._fill_from_store = _noop  # type: ignore[method-assign]
    svc._fill_from_steam_cdn = _noop  # type: ignore[method-assign]
    return svc


@pytest.mark.asyncio
async def test_fetch_artwork_backfills_only_missing(tmp_path):
    # grid+hero already on disk → only the three gaps should be requested.
    _touch(tmp_path, f"{_UID}p.jpg")
    _touch(tmp_path, f"{_UID}_hero.jpg")
    svc = _service(tmp_path)

    seen = {}

    async def fake_sgdb(title, app_id, result, sources, only_kinds=None):
        seen["only_kinds"] = set(only_kinds) if only_kinds else None
        result["icon"] = True          # SGDB fills the icon
        sources["icon"] = "SGDB"

    svc._fill_from_sgdb = fake_sgdb  # type: ignore[method-assign]

    result = await svc.fetch_artwork(_APP, "microsoft", "gid", "Control")

    assert seen["only_kinds"] == {"grid_l", "logo", "icon"}
    # present kinds report True without being re-fetched
    assert result["grid"] is True and result["hero"] is True
    assert result["icon"] is True
    # still-missing recorded for next sync's incremental skip, together with
    # the title it was recorded for — SGDB matches on the title, so the same
    # gaps under a later title are a different question (a stale record made
    # a renamed game unfixable; see
    # tests/unit/test_enrichment_survives_a_title_change.py).
    attempted = svc._cache.get("artwork_attempts", "microsoft:gid")
    assert set(attempted["missing"]) == {"grid_l", "logo"}
    assert attempted["title"] == "Control"


@pytest.mark.asyncio
async def test_fetch_artwork_skips_when_missing_unchanged(tmp_path):
    # Nothing on disk and SGDB returns nothing → second sync must skip
    # (same missing set) instead of re-querying SGDB.
    svc = _service(tmp_path)
    calls = {"n": 0}

    async def fake_sgdb(title, app_id, result, sources, only_kinds=None):
        calls["n"] += 1  # fills nothing — simulates a genuinely art-less title

    svc._fill_from_sgdb = fake_sgdb  # type: ignore[method-assign]

    await svc.fetch_artwork(_APP, "ms", "g", "Obscure")
    await svc.fetch_artwork(_APP, "ms", "g", "Obscure")
    assert calls["n"] == 1  # second call short-circuited by the attempts cache


@pytest.mark.asyncio
async def test_fetch_artwork_force_refetches_all(tmp_path):
    # All five on disk, but force=True must re-request every kind.
    for name in (f"{_UID}p.jpg", f"{_UID}.jpg", f"{_UID}_hero.jpg",
                 f"{_UID}_logo.png", f"{_UID}_icon.jpg"):
        _touch(tmp_path, name)
    svc = _service(tmp_path)
    seen = {}

    async def fake_sgdb(title, app_id, result, sources, only_kinds=None):
        seen["only_kinds"] = set(only_kinds) if only_kinds else None

    svc._fill_from_sgdb = fake_sgdb  # type: ignore[method-assign]

    await svc.fetch_artwork(_APP, "ms", "g", "Game", force=True)
    assert seen["only_kinds"] == {"grid", "grid_l", "hero", "logo", "icon"}


def test_attempts_namespace_is_registered_at_boot():
    """The attempts namespace MUST be pre-declared in the boot registry.

    CacheManager is strict — an unregistered namespace makes every
    ``get``/``set`` raise ``ValueError``, which is exactly what broke the
    first deploy ("Cache 'sgdb_attempts' not registered", 1169 errors).
    """
    from unifideck.bootstrap.cache_registry import _NAMED_CACHES
    from unifideck.services.artwork.service import _ATTEMPTS_NAMESPACE

    assert _ATTEMPTS_NAMESPACE in {name for name, _ttl in _NAMED_CACHES}


@pytest.mark.asyncio
async def test_store_fallback_only_fills_what_sgdb_left_empty(tmp_path, monkeypatch):
    # itch.io's landscape cover is last-resort art: when SGDB found a real
    # portrait capsule the cover must not replace it, only fill the rest.
    svc = _service(tmp_path)
    downloaded: dict[str, str] = {}

    async def fake_sgdb(title, app_id, result, sources, only_kinds=None):
        result["grid"] = True
        sources["grid"] = "SGDB"

    async def fake_download(grid_dir, app_id, kind, url, timeout):
        downloaded[kind] = url
        return True

    svc._fill_from_sgdb = fake_sgdb  # type: ignore[method-assign]
    monkeypatch.setattr("unifideck.services.artwork.service.download_and_save", fake_download)
    result = await svc.fetch_artwork(
        _APP, "itch", "1035957", "Helltaker",
        extras={"cover_url": "https://img.itch.zone/cover.png"},
    )
    assert downloaded == {"grid_l": "https://img.itch.zone/cover.png"}
    assert result["grid"] is True and result["grid_l"] is True


@pytest.mark.asyncio
async def test_store_fallback_is_not_blocked_by_a_cached_nothing_found(tmp_path, monkeypatch):
    # The first device run: 9 itch.io games carried an attempts record from
    # before the fallback phase existed, so the unchanged-missing-set skip
    # would have hidden their covers forever.
    svc = _service(tmp_path)
    svc._cache.set("artwork_attempts", "itch:2966078",
                   {"missing": ["grid", "grid_l", "hero", "icon", "logo"], "title": "Croaking Around"})
    downloaded: dict[str, str] = {}

    async def no_sgdb(title, app_id, result, sources, only_kinds=None):
        return None

    async def fake_download(grid_dir, app_id, kind, url, timeout):
        downloaded[kind] = url
        return True

    svc._fill_from_sgdb = no_sgdb  # type: ignore[method-assign]
    monkeypatch.setattr("unifideck.services.artwork.service.download_and_save", fake_download)
    await svc.fetch_artwork(_APP, "itch", "2966078", "Croaking Around",
                            extras={"cover_url": "https://img.itch.zone/c.png"})
    assert set(downloaded) == {"grid", "grid_l"}
