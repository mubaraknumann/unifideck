"""GOG cloud saves for Linux-native installs (Unity and DOSBox builds).

GOG's cloud config only names Windows locations, so native installs never
synced: their save dir never resolved. These pin the two mappings:

* Unity: ``<?APPLICATION_DATA_LOCAL_LOW?>/Roboatino/ShogunShowdown`` is
  ``~/.config/unity3d/Roboatino/ShogunShowdown`` on Linux.
* DOSBox: Windows overlays drive C onto ``<?INSTALL?>/cloud_saves``; Linux
  mounts ``data/`` as C, so a staging dir holds the files the game wrote
  (not shipped by the installer, or changed since) and downloads are copied
  back into ``data/``.
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

from unifideck.services.cloud_save import gog_native
from unifideck.services.cloud_save.constants import MANIFEST_FILE
from unifideck.services.cloud_save.gog_strategy import GOGCloudSaveStrategy


def _unity_install(root: Path) -> Path:
    (root / "game" / "ShogunShowdown_Data").mkdir(parents=True)
    (root / "game" / "ShogunShowdown_Data" / "globalgamemanagers").write_text("")
    (root / "start.sh").write_text("#!/bin/bash\n")
    return root


def _dosbox_install(root: Path, shipped: dict[str, bytes]) -> Path:
    (root / "dosbox").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "start.sh").write_text("#!/bin/bash\n")
    files = []
    for rel, content in shipped.items():
        (root / "data" / rel).write_bytes(content)
        files.append({"file_name": f"data/noarch/data/{rel}", "crc32": zlib.crc32(content)})
    (root / ".gogdl-linux-manifest").write_text(json.dumps({"files": files}))
    return root


# ── mapping ──────────────────────────────────────────────────────────────


def test_unity_local_low_maps_to_unity3d_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    install = _unity_install(tmp_path / "g")
    mapped = gog_native.map_location(
        "<?APPLICATION_DATA_LOCAL_LOW?>/Roboatino/ShogunShowdown", str(install), tmp_path / "s",
    )
    assert mapped == (tmp_path / "cfg" / "unity3d" / "Roboatino" / "ShogunShowdown", False)


def test_dosbox_install_location_maps_to_staging(tmp_path):
    install = _dosbox_install(tmp_path / "g", {"GAME.EXE": b"x"})
    stage = tmp_path / "stage"
    assert gog_native.map_location("<?INSTALL?>/cloud_saves", str(install), stage) == (stage, True)


def test_unmappable_locations_stay_unmapped(tmp_path):
    install = _unity_install(tmp_path / "g")
    assert gog_native.map_location("<?DOCUMENTS?>/Foo", str(install), tmp_path / "s") is None
    # A non-Unity build gets no unity3d guess.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gog_native.map_location(
        "<?APPLICATION_DATA_LOCAL_LOW?>/A/B", str(plain), tmp_path / "s",
    ) is None


def test_dosbox_without_installer_manifest_is_not_synced(tmp_path):
    install = _dosbox_install(tmp_path / "g", {"GAME.EXE": b"x"})
    (install / ".gogdl-linux-manifest").unlink()
    assert gog_native.map_location("<?INSTALL?>/cloud_saves", str(install), tmp_path / "s") is None


# ── staging ──────────────────────────────────────────────────────────────


def test_stage_in_takes_only_what_the_game_wrote(tmp_path):
    install = _dosbox_install(
        tmp_path / "g", {"GAME.EXE": b"exe", "EOBDATA.SAV": b"default", "LEVELS.TMP": b"t"},
    )
    (install / "data" / "EOBDATA.SAV").write_bytes(b"my party")  # changed
    (install / "data" / "SAVE1.DAT").write_bytes(b"new save")      # new
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / MANIFEST_FILE).write_text("{}")
    (stage / "stale.sav").write_text("old")

    assert gog_native.stage_in(str(install), stage) == 2
    assert sorted(p.name for p in stage.iterdir()) == sorted(
        [MANIFEST_FILE, "EOBDATA.SAV", "SAVE1.DAT"],
    )
    assert (stage / "EOBDATA.SAV").read_bytes() == b"my party"


def test_stage_out_copies_into_drive_c_only(tmp_path):
    install = _dosbox_install(tmp_path / "g", {"GAME.EXE": b"exe"})
    stage = tmp_path / "stage"
    (stage / "SAVES").mkdir(parents=True)
    (stage / "SAVES" / "SLOT1.SAV").write_bytes(b"cloud save")
    (stage / MANIFEST_FILE).write_text("{}")

    assert gog_native.stage_out(stage, str(install)) == 1
    assert (install / "data" / "SAVES" / "SLOT1.SAV").read_bytes() == b"cloud save"
    assert not (install / "data" / MANIFEST_FILE).exists()


# ── strategy wiring ──────────────────────────────────────────────────────


def _strategy(tmp_path: Path, install: Path, template: str) -> GOGCloudSaveStrategy:
    strat = GOGCloudSaveStrategy(str(tmp_path / "saves" / "gog"))
    strat._install_dir = lambda _gid: str(install)  # type: ignore[method-assign]
    patcher_cid = patch(
        "unifideck.services.cloud_save.gog_strategy.fetch_gog_client_id", return_value="cid",
    )
    patcher_loc = patch(
        "unifideck.services.cloud_save.gog_strategy.fetch_gog_save_locations",
        return_value=[("saves", template)],
    )
    patcher_cid.start()
    patcher_loc.start()
    return strat


def test_native_unity_install_resolves_through_gog_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    install = _unity_install(tmp_path / "g")
    strat = _strategy(tmp_path, install, "<?APPLICATION_DATA_LOCAL_LOW?>/Roboatino/ShogunShowdown")
    try:
        assert strat._resolve_store_save_dir("1") == str(
            tmp_path / "cfg" / "unity3d" / "Roboatino" / "ShogunShowdown",
        )
        assert strat._resolve_cloud_namespace("1") == "saves"
    finally:
        patch.stopall()


async def test_dosbox_download_lands_in_drive_c(tmp_path):
    install = _dosbox_install(tmp_path / "g", {"GAME.EXE": b"exe"})
    strat = _strategy(tmp_path, install, "<?INSTALL?>/cloud_saves")

    async def fake_pull(game_id, local_dir, force):
        (Path(local_dir) / "EOBDATA.SAV").write_bytes(b"from cloud")
        return True

    strat._do_sync_down = AsyncMock(side_effect=fake_pull)  # type: ignore[method-assign]
    try:
        assert await strat.sync_down("1")
    finally:
        patch.stopall()
    assert (install / "data" / "EOBDATA.SAV").read_bytes() == b"from cloud"


# ── gogdl uploads on "download" when the cloud is empty ──────────────────


async def _pull(tmp_path: Path, cloud: set[str] | None, *, force: bool):
    """Run _do_sync_down with a stubbed cloud listing; return (calls, save dir)."""
    strat = GOGCloudSaveStrategy(str(tmp_path / "saves" / "gog"))
    strat._convert_gog_token = lambda: "auth.json"  # type: ignore[method-assign]
    strat._cloud_namespaces_blocking = lambda _gid: cloud  # type: ignore[method-assign]
    strat._cached_namespace["1"] = "saves"
    save = tmp_path / "save"
    save.mkdir(parents=True)
    (save / "slot1.sav").write_text("SAVE" * 50)
    run = AsyncMock(return_value=True)
    strat._run_gogdl_save_sync = run  # type: ignore[method-assign]
    assert await strat._do_sync_down("1", str(save), force)
    return run, save


async def test_empty_cloud_namespace_never_runs_gogdl_or_clears(tmp_path):
    # gogdl's "No files in cloud, uploading" branch ignores --skip-upload.
    run, save = await _pull(tmp_path, set(), force=True)
    run.assert_not_awaited()
    assert (save / "slot1.sav").exists()


async def test_populated_or_unknown_cloud_still_downloads(tmp_path):
    run, _ = await _pull(tmp_path / "a", {"saves"}, force=False)
    run.assert_awaited_once()
    run, _ = await _pull(tmp_path / "b", None, force=False)
    run.assert_awaited_once()
