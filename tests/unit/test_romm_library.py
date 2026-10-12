"""RomM catalog mapping, pagination, and what a failure must not look like.

The catalog answers the whole library or raises, and much of this file checks
that: a page that fails after the first, overlapping pages, a listing that
ends before its declared total. A short catalog reaches the sync as a library
that shrank.
"""
from __future__ import annotations

import json

import pytest

from tests.unit.romm_fakes import Reply, romm_server  # noqa: F401
from unifideck.stores.romm.auth import RomMAuth
from unifideck.stores.romm.client import RomMAPIError
from unifideck.stores.romm.library import RomMCatalog

PREFIX = "1ad0c8ff8989849d4235d070383964a906b3ddaa73b0cf69d37dc9d00066eda5_"

PLATFORMS = [
    {"id": 1, "slug": "win", "fs_slug": "win", "display_name": "Windows"},
    {"id": 2, "slug": "win", "fs_slug": "windows9x", "display_name": "Windows 9x"},
    {"id": 3, "slug": "linux", "fs_slug": "linux", "display_name": "Linux"},
    {"id": 4, "slug": "snes", "fs_slug": "snes", "display_name": "SNES"},
]


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "romm_config.json"
    path.write_text(json.dumps({"server_url": "https://romm.test", "token": "fixture", "excluded_fs_slugs": []}))
    return RomMCatalog(RomMAuth(config_file=str(path)))


def rom(rid, slug="win", name="Game", **extra):
    base = {
        "id": rid, "platform_slug": slug, "platform_fs_slug": slug, "name": name,
        "fs_name": f"{name or 'file'}.zip", "fs_name_no_ext": name or "file",
        "fs_size_bytes": 1000, "url_cover": None, "metadatum": {"first_release_date": None},
    }
    base.update(extra)
    return base


def page(items, total, offset=0):
    return Reply(json={"items": items, "total": total, "limit": 500, "offset": offset})


def serve(server, pages, platforms=PLATFORMS):
    server.on("GET", "/api/platforms", Reply(json=platforms))
    queue = list(pages)
    server.on("GET", "/api/roms", lambda _c: queue.pop(0))


async def test_empty_platform_selection_never_fetches_unfiltered_roms(catalog, romm_server):
    romm_server.on("GET", "/api/platforms", Reply(json=[{"id": 1, "slug": "snes", "fs_slug": "snes"}]))
    assert await catalog.fetch() == []
    assert romm_server.calls_to("GET", "/api/roms") == []


async def test_linux_wins_only_for_exact_nonempty_names(catalog, romm_server):
    serve(romm_server, [page([
        rom(1, "linux", "Game"), rom(2, "win", "Game"), rom(3, "win", "game"),
        rom(4, "win", None), rom(5, "linux", None),
    ], 5)])
    games = await catalog.fetch()
    assert {g.store_game_id for g in games} == {PREFIX + n for n in ("1", "3", "4", "5")}
    by_id = {g.store_game_id.removeprefix(PREFIX): g for g in games}
    assert by_id["1"].metadata["prefer_native"] is True
    assert by_id["3"].metadata["prefer_native"] is False


async def test_later_page_failure_is_not_a_partial_catalog(catalog, romm_server):
    serve(romm_server, [page([rom(i) for i in range(1, 501)], 1000), Reply(status=503)])
    with pytest.raises(RomMAPIError):
        await catalog.fetch()


async def test_platform_ids_are_repeated_query_parameters(catalog, romm_server):
    serve(romm_server, [page([], 0)])
    await catalog.fetch()
    params = romm_server.calls_to("GET", "/api/roms")[0].params
    assert params["platform_ids"] == ["1", "2", "3"]
    assert params["limit"] == ["500"] and params["order_by"] == ["id"]
    assert params["with_char_index"] == ["false"]


async def test_slug_ids_are_reresolved_after_server_change(catalog, romm_server):
    serve(romm_server, [page([], 0)])
    await catalog.fetch()
    romm_server._routes.clear()
    serve(romm_server, [page([], 0)], platforms=[{"id": 90, "slug": "win", "fs_slug": "win"}])
    await catalog.fetch()
    assert romm_server.calls_to("GET", "/api/roms")[-1].params["platform_ids"] == ["90"]


async def test_fs_variant_exclusions_apply(tmp_path, romm_server):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"server_url": "https://romm.test", "token": "t", "excluded_fs_slugs": ["windows9x"]}))
    serve(romm_server, [page([], 0)])
    await RomMCatalog(RomMAuth(config_file=str(path))).fetch()
    assert romm_server.calls_to("GET", "/api/roms")[0].params["platform_ids"] == ["1", "3"]


async def test_pages_use_items_total_limit_offset(catalog, romm_server):
    serve(romm_server, [page([rom(i) for i in range(1, 501)], 501), page([rom(501)], 501, 500)])
    games = await catalog.fetch()
    assert len(games) == 501
    assert [c.params["offset"] for c in romm_server.calls_to("GET", "/api/roms")] == [["0"], ["500"]]


async def test_missing_title_falls_back_without_deduping(catalog, romm_server):
    serve(romm_server, [page([
        rom(1, name="", fs_name_no_ext="Fallback One"), rom(2, name=None, fs_name_no_ext="Fallback One"),
    ], 2)])
    games = await catalog.fetch()
    assert [g.title for g in games] == ["Fallback One", "Fallback One"]


async def test_same_platform_duplicates_remain(catalog, romm_server):
    serve(romm_server, [page([rom(1, "win", "Same"), rom(2, "win", "Same")], 2)])
    assert len(await catalog.fetch()) == 2


@pytest.mark.parametrize("bad", [
    {"items": "nope", "total": 1}, {"total": 1}, [], {"items": [{"id": 0}], "total": 1},
    {"items": [{"id": True}], "total": 1}, {"items": [], "total": -1},
])
async def test_malformed_page_raises(catalog, romm_server, bad):
    serve(romm_server, [Reply(json=bad)])
    with pytest.raises(RomMAPIError):
        await catalog.fetch()


async def test_truncated_listing_raises(catalog, romm_server):
    serve(romm_server, [
        page([rom(i) for i in range(1, 501)], 1200), page([rom(i) for i in range(501, 1001)], 1200, 500), page([], 1200, 1000),
    ])
    with pytest.raises(RomMAPIError):
        await catalog.fetch()


async def test_absent_total_ends_on_empty_page(catalog, romm_server):
    serve(romm_server, [
        Reply(json={"items": [rom(i) for i in range(1, 501)]}),
        Reply(json={"items": [rom(i) for i in range(501, 1001)]}),
        Reply(json={"items": []}),
    ])
    assert len(await catalog.fetch()) == 1000


async def test_overlapping_pages_raise(catalog, romm_server):
    serve(romm_server, [page([rom(1)], 2), page([rom(1)], 2, 1)])
    with pytest.raises(RomMAPIError):
        await catalog.fetch()


async def test_release_timestamp_maps_to_utc_date(catalog, romm_server):
    serve(romm_server, [page([
        rom(1, metadatum={"first_release_date": 946684800}),
        rom(2, metadatum={"first_release_date": None}),
        rom(3, metadatum={"first_release_date": True}),
        rom(4, metadatum={"first_release_date": 10**30}),
    ], 4)])
    by_id = {g.store_game_id.removeprefix(PREFIX): g for g in await catalog.fetch()}
    assert by_id["1"].metadata["release_date"] == "2000-01-01"
    for rid in "234":
        assert "release_date" not in by_id[rid].metadata


async def test_cover_and_size_are_mapped(catalog, romm_server):
    serve(romm_server, [page([rom(1, url_cover="https://romm.test/cover.png", fs_size_bytes=12345)], 1)])
    game = (await catalog.fetch())[0]
    assert game.icon_url == "https://romm.test/cover.png"
    assert game.metadata["fs_size_bytes"] == 12345


async def test_resolve_uses_cached_descriptor_then_detail(catalog, romm_server):
    serve(romm_server, [page([rom(1)], 1)])
    await catalog.fetch()
    assert (await catalog.resolve(PREFIX + "1"))["id"] == 1
    romm_server.on("GET", "/api/roms/7", Reply(json=rom(7)))
    assert (await catalog.resolve(PREFIX + "7"))["id"] == 7
    assert await catalog.size(PREFIX + "7") == 1000


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1/2", "", "１", "01", "1_extra"])
async def test_resolve_rejects_invalid_ids(catalog, romm_server, bad):
    with pytest.raises(RomMAPIError):
        await catalog.resolve(PREFIX + bad)
    assert not romm_server.calls


async def test_resolve_rejects_unscoped_numeric_id(catalog, romm_server):
    with pytest.raises(RomMAPIError):
        await catalog.resolve("42")
    assert not romm_server.calls


async def test_resolve_rejects_non_pc_platform(catalog, romm_server):
    romm_server.on("GET", "/api/roms/9", Reply(json=rom(9, "snes")))
    with pytest.raises(RomMAPIError):
        await catalog.resolve(PREFIX + "9")
