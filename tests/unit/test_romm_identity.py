"""Server-local ROM IDs must not alias persisted games from another server.

RomM numbers ROMs per server, so the same number on two servers is two
different games. These tests hold that rule where breaking it is expensive:
the install record, the shortcut appid, and the descriptor a request captured
before the connection changed.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.unit.romm_fakes import Reply, romm_server  # noqa: F401
from unifideck.services.shortcut.games_map import generate_app_id
from unifideck.services.shortcut.launch_options import extract_store_id
from unifideck.stores.romm.auth import RomMAuth
from unifideck.stores.romm.client import RomMAPIError
from unifideck.stores.romm.library import RomMCatalog
from unifideck.stores.romm.sources import RomMArchiveSource
from unifideck.stores.shared.archive_install import ArchiveInstaller
from unifideck.stores.shared.archive_library import ArchiveLibraryReader
from unifideck.stores.shared.install_records import InstallRecords

A_ID = "93fb074e7b13b63568afcc41e2ed3c4bf3c8213528da1b0834baa3f2dce00389_42"
B_ID = "650b12a8f95470d576d98f0e9b301e204778848030628b9fefa0c5b92b78dd30_42"
PLATFORMS = [{"id": 1, "slug": "win", "fs_slug": "win"}]


def descriptor(name):
    return {"id": 42, "name": name, "fs_name": name + ".zip", "platform_slug": "win", "platform_fs_slug": "win"}


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "romm_config.json"
    cfg.write_text(json.dumps({"server_url": "https://a.test", "token": "fixture"}))
    auth = RomMAuth(str(cfg))
    catalog = RomMCatalog(auth)
    source = RomMArchiveSource(auth, catalog, download_dir=str(tmp_path / "staging"))
    installer = ArchiveInstaller(store="romm", source=source, default_install_root=str(tmp_path / "games"))
    return auth, catalog, source, ArchiveLibraryReader(installer, catalog)


def script(server):
    server.on("GET", "/api/platforms", Reply(json=PLATFORMS))
    server.on("GET", "/api/roms", lambda call: Reply(json={
        "items": [descriptor("Alpha" if call.url.startswith("https://a.test/") else "Beta")], "total": 1,
    }))
    server.on("GET", "/api/roms/42", lambda call: Reply(json=descriptor(
        "Alpha" if call.url.startswith("https://a.test/") else "Beta",
    )))


async def connect(auth, attempt, url):
    assert (await auth.begin(attempt, server_url=url, token="fixture")).success
    assert (await auth.complete(attempt, excluded_fs_slugs=[])).success


async def test_same_rom_id_on_two_servers_has_distinct_install_and_shortcut_identity(connection, romm_server, tmp_path):
    auth, _, _, reader = connection
    script(romm_server)
    alpha = (await reader.get_library())[0]
    InstallRecords("romm").save(alpha.store_game_id, title="Alpha", install_path=str(tmp_path / "alpha"), exe_path="Alpha.exe")
    await connect(auth, "b", "https://b.test")
    beta = (await reader.get_library())[0]
    assert not beta.installed and beta.install_path is None
    assert alpha.store_game_id == A_ID and beta.store_game_id == B_ID
    assert extract_store_id("romm:" + beta.store_game_id) == ("romm", B_ID)
    assert generate_app_id("unifideck", "romm:" + A_ID) != generate_app_id("unifideck", "romm:" + B_ID)
    assert InstallRecords("romm").load(B_ID) is None
    assert InstallRecords("romm").load(A_ID)["title"] == "Alpha"
    await connect(auth, "a", "https://a.test/")
    restored = (await reader.get_library())[0]
    assert restored.store_game_id == A_ID and restored.installed
    assert restored.install_path == str(tmp_path / "alpha")


async def test_old_server_descriptor_cannot_be_used_by_new_server(connection, romm_server):
    auth, catalog, _, _ = connection
    script(romm_server)
    old = (await catalog.fetch())[0]
    await connect(auth, "b", "https://b.test")
    calls = len(romm_server.calls)
    with pytest.raises(RomMAPIError):
        await catalog.resolve(old.store_game_id)
    assert len(romm_server.calls) == calls
    resolved = await catalog.resolve(B_ID)
    assert resolved["name"] == "Beta"
    assert romm_server.calls[-1].url == "https://b.test/api/roms/42"


async def test_switch_during_detail_request_uses_original_client_for_content(connection, romm_server, tmp_path):
    auth, catalog, _, _ = connection
    script(romm_server)
    game_id = (await catalog.fetch())[0].store_game_id
    source = RomMArchiveSource(auth, RomMCatalog(auth), download_dir=str(tmp_path / "stream"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(call):
        assert call.url == "https://a.test/api/roms/42"
        entered.set()
        await release.wait()
        return Reply(json=descriptor("Alpha"))

    romm_server._routes = [r for r in romm_server._routes if r[1] != "/api/roms/42"]
    romm_server.on("GET", "/api/roms/42", slow)
    romm_server.on("GET", "/api/roms/42/content/Alpha.zip", Reply(chunks=[b"PK"]))
    task = asyncio.create_task(source.acquire(game_id, progress_callback=None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await connect(auth, "b", "https://b.test")
    finally:
        release.set()
    acquired = await task
    try:
        assert romm_server.calls[-1].url == "https://a.test/api/roms/42/content/Alpha.zip"
    finally:
        source.release(acquired)
