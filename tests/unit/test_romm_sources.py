"""RomM content acquisition: one whole-ROM request, staging, ownership.

Each acquisition owns a private directory under staging, so these cases are
mostly about what must not be shared or left behind: two games whose server
files share a name, an interrupted transfer, and a release that must touch
nothing the user placed in the staging root.
"""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from tests.unit.romm_fakes import Reply, romm_server  # noqa: F401
from unifideck.stores.romm.auth import RomMAuth
from unifideck.stores.romm.client import RomMAPIError
from unifideck.stores.romm.library import RomMCatalog
from unifideck.stores.romm.sources import RomMArchiveSource


PREFIX = "c709aafab596d66f75378ca5d1e689d275d37a1999123e0ce8acad0ad946dd7a_"


def rom(rid=42, **extra):
    base = {
        "id": rid, "platform_slug": "win", "platform_fs_slug": "win", "name": "Great Game",
        "fs_name": "Great Game", "fs_name_no_ext": "Great Game", "fs_size_bytes": 1000,
        "has_simple_single_file": False, "has_multiple_files": True,
    }
    base.update(extra)
    return base


@pytest.fixture
def parts(tmp_path):
    cfg = tmp_path / "romm_config.json"
    cfg.write_text(json.dumps({"server_url": "https://romm.test/base", "token": "fixture", "excluded_fs_slugs": []}))
    auth = RomMAuth(config_file=str(cfg))
    catalog = RomMCatalog(auth)
    source = RomMArchiveSource(auth, catalog, download_dir=str(tmp_path / "staging"))
    return auth, catalog, source, tmp_path / "staging"


def serve(server, descriptor, reply):
    server.on("GET", f"/base/api/roms/{descriptor['id']}", Reply(json=descriptor))
    encoded = quote(descriptor["fs_name"], safe="")
    server.on("GET", f"/base/api/roms/{descriptor['id']}/content/{encoded}", reply)


async def test_generated_zip_progress_does_not_claim_source_size(parts, romm_server):
    _, _, source, _ = parts
    serve(romm_server, rom(), Reply(chunks=[b"x" * 100], headers={"Content-Length": "100"}))
    ticks = []

    async def collect(payload):
        ticks.append(payload)

    await source.acquire(PREFIX + "42", progress_callback=collect)
    assert ticks[-1]["total_bytes"] == 100 and ticks[-1]["downloaded_bytes"] == 100


async def test_complete_content_request_has_no_file_ids(parts, romm_server):
    _, _, source, _ = parts
    serve(romm_server, rom(), Reply(chunks=[b"PK"]))
    await source.acquire(PREFIX + "42", progress_callback=None)
    content = [c for c in romm_server.calls if "/content/" in c.path]
    assert len(content) == 1 and content[0].params == {}
    assert content[0].headers["Authorization"] == "Bearer fixture"


async def test_reserved_characters_are_encoded_in_fs_name(parts, romm_server):
    _, _, source, _ = parts
    descriptor = rom(7, fs_name="a/b #?.zip", has_multiple_files=False, has_simple_single_file=True)
    serve(romm_server, descriptor, Reply(chunks=[b"PK"]))
    acquired = await source.acquire(PREFIX + "7", progress_callback=None)
    content = [c for c in romm_server.calls if "/content/" in c.path][0]
    assert content.path.endswith("/content/a%2Fb%20%23%3F.zip")
    assert acquired.path.parent.parent == parts[3]


async def test_directory_download_is_named_as_a_zip(parts, romm_server):
    _, _, source, _ = parts
    serve(romm_server, rom(), Reply(chunks=[b"PK"]))
    acquired = await source.acquire(PREFIX + "42", progress_callback=None)
    assert acquired.path.suffix == ".zip"
    assert acquired.title == "Great Game" and acquired.dir_name == "Great Game"


async def test_content_disposition_name_is_confined_to_staging(parts, romm_server):
    _, _, source, staging = parts
    serve(romm_server, rom(), Reply(chunks=[b"PK"], headers={"Content-Disposition": 'attachment; filename="../../evil.zip"'}))
    acquired = await source.acquire(PREFIX + "42", progress_callback=None)
    assert acquired.path.name == "evil.zip"
    assert staging in acquired.path.parents


async def test_partial_stream_and_staging_are_removed(parts, romm_server):
    _, _, source, staging = parts
    serve(romm_server, rom(), Reply(chunks=[b"partial"], stream_error=ConnectionError("dropped")))
    with pytest.raises(RomMAPIError, match="Could not reach RomM"):
        await source.acquire(PREFIX + "42", progress_callback=None)
    assert list(staging.iterdir()) == []


async def test_release_removes_only_the_owned_job_directory(parts, romm_server):
    _, _, source, staging = parts
    (staging).mkdir(parents=True)
    (staging / "user-file.zip").write_bytes(b"mine")
    serve(romm_server, rom(), Reply(chunks=[b"PK"]))
    acquired = await source.acquire(PREFIX + "42", progress_callback=None)
    source.release(acquired)
    assert [p.name for p in staging.iterdir()] == ["user-file.zip"]


async def test_same_name_downloads_do_not_share_a_directory(parts, romm_server):
    _, _, source, _ = parts
    serve(romm_server, rom(1, fs_name="Same"), Reply(chunks=[b"PK"]))
    serve(romm_server, rom(2, fs_name="Same"), Reply(chunks=[b"PK"]))
    one = await source.acquire(PREFIX + "1", progress_callback=None)
    two = await source.acquire(PREFIX + "2", progress_callback=None)
    assert one.path.parent != two.path.parent


async def test_native_preference_comes_from_platform(parts, romm_server):
    _, _, source, _ = parts
    serve(romm_server, rom(platform_slug="linux", platform_fs_slug="linux"), Reply(chunks=[b"PK"]))
    assert (await source.acquire(PREFIX + "42", progress_callback=None)).prefer_native is True


async def test_size_uses_catalog_descriptor(parts, romm_server):
    _, _, source, _ = parts
    romm_server.on("GET", "/base/api/roms/42", Reply(json=rom()))
    assert await source.size(PREFIX + "42") == 1000


async def test_per_connection_download_dir_overrides_default(tmp_path, romm_server):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"server_url": "https://romm.test/base", "token": "t", "download_dir": str(tmp_path / "chosen")}))
    auth = RomMAuth(config_file=str(cfg))
    source = RomMArchiveSource(auth, RomMCatalog(auth), download_dir=str(tmp_path / "default"))
    serve(romm_server, rom(), Reply(chunks=[b"PK"]))
    acquired = await source.acquire(PREFIX + "42", progress_callback=None)
    assert (tmp_path / "chosen") in acquired.path.parents
