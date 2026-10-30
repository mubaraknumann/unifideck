import os
import json
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

from unifideck.services.cloud_save import gog_cloud_api
from unifideck.services.cloud_save.path_resolver import WinePrefixResolver
from unifideck.services.cloud_save.epic_strategy import EpicCloudSaveStrategy
from unifideck.services.cloud_save.gog_strategy import GOGCloudSaveStrategy
from unifideck.services.cloud_save.service import CloudSaveService
from unifideck.core.types import Result

@pytest.fixture
def mock_config():
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "cloud.enabled": True,
        "cloud.tolerance_seconds": 2.0,
        "cloud.sync_wait_timeout_seconds": 5.0,
        "cloud_saves.remote_root": "/tmp/test_remote_root",
        "paths.data_dir": "/tmp/test_data_dir",
        "games.amazon123.title": "My Amazon Game",
    }.get(key, default)
    config.get_bool.side_effect = lambda key, default=True: {
        "cloud.enabled": True,
    }.get(key, default)
    return config

@pytest.fixture
def mock_event_bus():
    bus = MagicMock()
    # sync_down/sync_up await bus.emit(...) for the CLOUD_SYNC_* completion
    # events, so emit must be awaitable.
    bus.emit = AsyncMock()
    return bus

def test_wine_prefix_resolver(tmp_path):
    # Setup a dummy prefix registry
    prefix = tmp_path / "test_prefix"
    prefix.mkdir()
    
    # Write a dummy user.reg file
    user_reg = prefix / "user.reg"
    user_reg.write_text(
        '[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Explorer\\\\Shell Folders]\n'
        '"AppData"="C:\\\\users\\\\steamuser\\\\AppData\\\\Roaming"\n'
        '"Local AppData"="C:\\\\users\\\\steamuser\\\\AppData\\\\Local"\n'
    )

    # Resolve
    resolved = WinePrefixResolver.resolve_path(
        cloud_save_folder="{AppData}/GameName/Saves",
        prefix_path=str(prefix),
        install_path="/tmp/install",
        account_id="ed4745",
    )
    # Epic's {AppData} token resolves to %LOCALAPPDATA% (AppData/Local),
    # NOT %APPDATA% (Roaming) — that's where Epic games actually save.
    assert "drive_c/users/steamuser/AppData/Local/GameName/Saves" in resolved


def test_wine_prefix_resolver_epicid_uses_account_id(tmp_path):
    """{EpicID} must resolve to the Epic ACCOUNT id, not the game id —
    Vampire Survivors / Brotato namespace saves under the account id, and
    using the game id pointed the sync at a folder the game never reads."""
    prefix = tmp_path / "vs_prefix"
    prefix.mkdir()
    (prefix / "user.reg").write_text(
        '[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Explorer\\\\Shell Folders]\n'
        '"AppData"="C:\\\\users\\\\steamuser\\\\AppData\\\\Roaming"\n'
        '"Local AppData"="C:\\\\users\\\\steamuser\\\\AppData\\\\Local"\n'
    )
    resolved = WinePrefixResolver.resolve_path(
        # Real Vampire Survivors template: {AppData}/../Roaming redirects
        # Local→Roaming, {EpicID} is the account-id subfolder.
        cloud_save_folder="{AppData}/../Roaming/Vampire_Survivors_EGS/{EpicID}/",
        prefix_path=str(prefix),
        account_id="ed4745dba2c6492d851bcb554dc98d60",
    )
    assert resolved.endswith(
        "AppData/Roaming/Vampire_Survivors_EGS/ed4745dba2c6492d851bcb554dc98d60"
    )
    assert "game" not in resolved.rsplit("/", 1)[-1]  # not a game-id subfolder

@pytest.mark.asyncio
async def test_epic_strategy_sync(tmp_path, mock_config):
    local_save_root = str(tmp_path / "saves")
    os.makedirs(local_save_root, exist_ok=True)
    
    # Mock legendary CLI response
    strategy = EpicCloudSaveStrategy(local_save_root, mock_config)
    strategy.legendary_bin = "mock_legendary"
    # Keep hermetic: don't read the dev machine's ~/.config/legendary, and
    # don't spin up LegendaryCore for the validating fallback.
    strategy._get_account_id = MagicMock(return_value="acct123")
    strategy._legendary_save_path = MagicMock(return_value=None)

    with patch("subprocess.run") as mock_run, \
         patch("asyncio.create_subprocess_exec") as mock_exec:
         
        # Mock legendary info JSON response
        mock_info_res = MagicMock()
        # legendary nests these under "game" / "install" (real shape).
        mock_info_res.stdout = json.dumps({
            "game": {"cloud_save_folder": "{AppData}/GameName/Saves"},
            "install": {"install_path": "/tmp/install"},
        })
        mock_run.return_value = mock_info_res
        
        # Mock legendary sync-saves subprocess
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"Success", b"")
        mock_exec.return_value = mock_proc
        
        # Test path resolution
        save_dir = strategy.get_local_save_dir("game123")
        assert save_dir is not None
        assert "GameName/Saves" in save_dir
        
        # Test sync_down — default (on-launch) must NOT force a download,
        # so newer local saves are never silently overwritten.
        success = await strategy.sync_down("game123")
        assert success is True
        assert "--force-download" not in mock_exec.call_args.args

        # Test sync_up
        # Write dummy save to satisfy empty check
        os.makedirs(save_dir, exist_ok=True)
        with open(os.path.join(save_dir, "save.bin"), "w") as f:
            f.write("data")
        success = await strategy.sync_up("game123")
        assert success is True

@pytest.mark.asyncio
async def test_gog_strategy_sync(tmp_path, mock_config):
    local_save_root = str(tmp_path / "saves")
    os.makedirs(local_save_root, exist_ok=True)
    
    strategy = GOGCloudSaveStrategy(local_save_root, mock_config)
    strategy.gogdl_bin = "mock_gogdl"

    # Mock token conversion directly
    strategy._convert_gog_token = MagicMock(return_value="/tmp/mock_auth.json")
    # Resolve to a real dir — the staging fallback was removed, so
    # get_local_save_dir returns None without a prefix; provide a location.
    strategy.get_local_save_dir = MagicMock(return_value=str(tmp_path / "gogsave"))

    # Mock subprocess
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"12345.6789", b"")
        mock_exec.return_value = mock_proc
        
        # Test sync_down
        success = await strategy.sync_down("gog123")
        assert success is True
        assert strategy._get_saved_timestamp("gog123") == "12345.6789"

@pytest.mark.asyncio
async def test_cloud_save_service_orchestration(tmp_path, mock_event_bus, mock_config):
    local_save_root = str(tmp_path / "saves")
    os.makedirs(local_save_root, exist_ok=True)
    
    # Instantiate service
    service = CloudSaveService(
        bus=mock_event_bus,
        local_save_root=local_save_root,
        cloud_root="/tmp/test_remote_root",
        config=mock_config
    )
    
    # Mock strategies
    mock_epic = MagicMock()
    mock_epic.sync_down = AsyncMock(return_value=True)
    mock_epic.sync_up = AsyncMock(return_value=True)
    mock_epic.get_local_save_dir.return_value = "/tmp/test_epic_save"
    
    service._strategies["epic"] = mock_epic
    
    # Mock fallback sync Mixin calls
    with patch.object(CloudSaveService, "_sync_down_locked", return_value=Result(success=True)), \
         patch.object(CloudSaveService, "_sync_up_locked", return_value=Result(success=True)), \
         patch.object(CloudSaveService, "_acquire_sync_lock", return_value=(MagicMock(), None)):
         
        # Verify custom path routing
        assert service.get_local_save_dir("epic", "game123") == "/tmp/test_epic_save"
        
        # Test sync_down runs both strategy and fallback (force defaults False)
        res_down = await service.sync_down("epic", "game123")
        assert res_down.success is True
        mock_epic.sync_down.assert_called_once_with("game123", False)
        
        # Test sync_up runs both strategy and fallback
        res_up = await service.sync_up("epic", "game123")
        assert res_up.success is True
        mock_epic.sync_up.assert_called_once_with("game123")

def test_amazon_prefix_auto_detect(tmp_path, mock_config, mock_event_bus):
    # Setup mock wine prefix
    prefix_dir = tmp_path / "prefixes" / "amazon123"
    drive_c = prefix_dir / "pfx" / "drive_c"
    saved_games = drive_c / "users" / "steamuser" / "Saved Games"
    game_save = saved_games / "My Amazon Game"
    os.makedirs(game_save, exist_ok=True)
    
    service = CloudSaveService(
        bus=mock_event_bus,
        local_save_root=str(tmp_path / "saves"),
        cloud_root="/tmp/test_remote",
        config=mock_config
    )
    
    # Resolve
    resolved = service.get_local_save_dir("amazon", "amazon123")
    assert resolved == str(game_save)

def test_global_cloud_root_default():
    from unifideck.services.bootstrap.paths import ServicePaths
    
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: {
        "paths.data_dir": "/tmp/unifideck_data",
        "paths.steam_root": "/tmp/steam",
    }.get(key, default)
    
    with patch("unifideck.steam.steam_user.get_active_steam_user", return_value="123456"):
        paths = ServicePaths.from_config(config)
        assert paths.cloud_root == str(Path("~/Save Games Backup").expanduser())


# ── Cloud-save wipe-prevention guardrails (shared safety module) ──────────
from unifideck.services.cloud_save import safety  # noqa: E402
from unifideck.services.cloud_save.safety import SaveConflictError  # noqa: E402


def _settings_only_dir(base, name):
    """A save dir with only settings/config (the reset-prefix state that
    previously wiped the cloud): an empty ``gamesaves/`` and a couple of
    ``*.settings`` files, but no real save data."""
    d = base / name
    (d / "gamesaves").mkdir(parents=True)
    (d / "profile.settings").write_text("cfg")
    (d / "profile.settings.bak").write_text("cfg")
    return d


def test_has_save_data_distinguishes_saves_from_settings(tmp_path):
    settings_only = _settings_only_dir(tmp_path, "settings_only")
    assert safety.has_save_data(settings_only) is False
    # A real save (top-level) counts — protects single-file-save games.
    (settings_only / "slot1.sav").write_text("save")
    assert safety.has_save_data(settings_only) is True
    # A real save in a subdir (Witcher's gamesaves/) counts too.
    sub = tmp_path / "subdir_saves"
    (sub / "gamesaves").mkdir(parents=True)
    (sub / "gamesaves" / "CheckPoint.sav").write_text("save")
    assert safety.has_save_data(sub) is True


def test_snapshot_backup_is_versioned_and_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "_backups_root", lambda p=tmp_path / "backups": p)
    monkeypatch.setattr(safety, "_KEEP_BACKUPS", 2)
    src = tmp_path / "saves"
    (src / "gamesaves").mkdir(parents=True)
    (src / "gamesaves" / "a.sav").write_text("x")
    for ts in (1000, 2000, 3000):
        out = safety.snapshot_backup(src, "gog", "g1", now=ts)
        assert out is not None and (out / "gamesaves" / "a.sav").is_file()
    kept = sorted((tmp_path / "backups" / "gog" / "g1").iterdir())
    assert [p.name for p in kept] == ["2000", "3000"]  # oldest rotated out


@pytest.mark.asyncio
async def test_gog_sync_up_blocks_and_never_calls_cli(tmp_path, mock_config, monkeypatch):
    monkeypatch.setattr(safety, "_backups_root", lambda p=tmp_path / "backups": p)
    save_dir = _settings_only_dir(tmp_path, "gog_saves")
    strategy = GOGCloudSaveStrategy(str(tmp_path), mock_config)
    strategy.gogdl_bin = "mock_gogdl"
    strategy._convert_gog_token = MagicMock(return_value="/tmp/mock_auth.json")
    strategy.get_local_save_dir = MagicMock(return_value=str(save_dir))
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        with pytest.raises(SaveConflictError) as exc:
            await strategy.sync_up("gog123")
        mock_exec.assert_not_called()  # destructive gogdl push never ran
    assert exc.value.hard is True  # empty upload is a HARD error, never a choice


@pytest.mark.asyncio
async def test_epic_sync_up_blocks_and_never_calls_cli(tmp_path, mock_config, monkeypatch):
    monkeypatch.setattr(safety, "_backups_root", lambda p=tmp_path / "backups": p)
    save_dir = _settings_only_dir(tmp_path, "epic_saves")
    strategy = EpicCloudSaveStrategy(str(tmp_path), mock_config)
    strategy.legendary_bin = "mock_legendary"
    strategy.get_local_save_dir = MagicMock(return_value=str(save_dir))
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        with pytest.raises(SaveConflictError) as exc:
            await strategy.sync_up("epic123")
        mock_exec.assert_not_called()  # destructive legendary push never ran
    assert exc.value.hard is True  # empty upload is a HARD error, never a choice


@pytest.mark.asyncio
async def test_service_soft_conflict_opens_modal(
    tmp_path, mock_event_bus, mock_config, monkeypatch,
):
    monkeypatch.setattr(safety, "_backups_root", lambda p=tmp_path / "backups": p)
    service = CloudSaveService(
        bus=mock_event_bus,
        local_save_root=str(tmp_path / "saves"),
        cloud_root=None,  # skip the local-backup mixin; isolate the strategy
        config=mock_config,
    )
    mock_event_bus.emit = AsyncMock()
    blocking = MagicMock()
    blocking.sync_up = AsyncMock(
        side_effect=SaveConflictError(
            "local_saves_regressed",  # local has saves but lost some
            {"file_count": 2, "timestamp": 1.0, "total_bytes": 99},
            store="gog", game_id="g1", hard=False,
        ),
    )
    service._strategies["gog"] = blocking
    with patch.object(
        CloudSaveService, "_acquire_sync_lock", return_value=(MagicMock(), None),
    ):
        res = await service.sync_up("gog", "g1")
    # A soft conflict is NOT a launch failure; it surfaces the pick modal
    # (LAUNCHER_STAGE + retry-sync) with both snapshots — never a wipe.
    assert res.success is True
    evt = mock_event_bus.emit.await_args
    assert evt.kwargs.get("action", {}).get("verb") == "retry-sync"
    assert evt.kwargs.get("local_snapshot", {}).get("file_count") == 2
    assert "remote_snapshot" in evt.kwargs


@pytest.mark.asyncio
async def test_service_hard_block_emits_error_not_modal(
    tmp_path, mock_event_bus, mock_config,
):
    service = CloudSaveService(
        bus=mock_event_bus,
        local_save_root=str(tmp_path / "saves"),
        cloud_root=None,
        config=mock_config,
    )
    mock_event_bus.emit = AsyncMock()
    blocking = MagicMock()
    blocking.sync_up = AsyncMock(
        side_effect=SaveConflictError(
            "no_local_save_data",
            {"file_count": 0, "timestamp": 0, "total_bytes": 0},
            store="gog", game_id="g1", hard=True,
        ),
    )
    service._strategies["gog"] = blocking
    with patch.object(
        CloudSaveService, "_acquire_sync_lock", return_value=(MagicMock(), None),
    ):
        res = await service.sync_up("gog", "g1")
    # HARD block (empty) → plain title+body toast, NEVER a "keep local" pick.
    # Severity is a warning (an expected skip when there are no local saves,
    # not a failure) and the message is split into a short title + body.
    assert res.success is True
    evt = mock_event_bus.emit.await_args
    assert "action" not in evt.kwargs  # no retry-sync → no pick modal
    assert evt.kwargs.get("severity") == "warning"
    assert evt.kwargs.get("i18n_title_key") == "toasts.header.cloudSaves"
    assert evt.kwargs.get("i18n_key") == "toasts.cloudUploadSkippedBody"


# ── Forced pull (explicit "Use Cloud") ────────────────────────────────────


@pytest.mark.asyncio
async def test_epic_sync_down_force_adds_force_download(tmp_path, mock_config):
    """force=True must add --force-download so legendary pulls even when the
    local save is newer/same-age (the only way "Use Cloud" can override)."""
    strategy = EpicCloudSaveStrategy(str(tmp_path), mock_config)
    strategy.legendary_bin = "mock_legendary"
    strategy.get_local_save_dir = MagicMock(return_value=str(tmp_path / "save"))
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"Downloading remote savegame...")
        mock_exec.return_value = proc
        assert await strategy.sync_down("epic123", force=True) is True
        assert "--force-download" in mock_exec.call_args.args


@pytest.mark.asyncio
async def test_gog_sync_down_force_uses_ts_zero(tmp_path, mock_config):
    """force=True must pull a full copy (ts=0) even when a recent last-sync
    timestamp would otherwise make gogdl skip the download."""
    strategy = GOGCloudSaveStrategy(str(tmp_path), mock_config)
    strategy.gogdl_bin = "mock_gogdl"
    strategy._convert_gog_token = MagicMock(return_value="/tmp/auth.json")
    save_dir = tmp_path / "save"
    (save_dir).mkdir()
    (save_dir / "slot.sav").write_text("data")  # local has saves → not the empty self-heal
    strategy.get_local_save_dir = MagicMock(return_value=str(save_dir))
    strategy._get_saved_timestamp = MagicMock(return_value="99999.0")
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"12345.6", b"")
        mock_exec.return_value = proc
        assert await strategy.sync_down("gog123", force=True) is True
        args = mock_exec.call_args.args
        assert "--ts" in args and args[args.index("--ts") + 1] == "0"


@pytest.mark.asyncio
async def test_service_sync_down_forwards_force(tmp_path, mock_event_bus, mock_config):
    service = CloudSaveService(
        bus=mock_event_bus, local_save_root=str(tmp_path / "saves"),
        cloud_root=None, config=mock_config,
    )
    strat = MagicMock()
    strat.sync_down = AsyncMock(return_value=True)
    service._strategies["epic"] = strat
    with patch.object(
        CloudSaveService, "_acquire_sync_lock", return_value=(MagicMock(), None),
    ):
        await service.sync_down("epic", "g1", force=True)
    strat.sync_down.assert_called_once_with("g1", True)


@pytest.mark.asyncio
async def test_dispatch_retry_sync_down_forces_pull():
    """The retry-sync 'sync_down' phase is only reached via 'Use Cloud', so it
    must force; 'sync_up' stays unforced."""
    from unifideck.actions.dispatch import _dispatch_retry_sync
    cloudsave = MagicMock()
    cloudsave.sync_down = AsyncMock(return_value=Result(success=True))
    cloudsave.sync_up = AsyncMock(return_value=Result(success=True))

    down = MagicMock(args=["epic", "g1", "sync_down"])
    await _dispatch_retry_sync(down, cloudsave)
    cloudsave.sync_down.assert_called_once_with("epic", "g1", force=True)

    up = MagicMock(args=["epic", "g1", "sync_up"])
    await _dispatch_retry_sync(up, cloudsave)
    cloudsave.sync_up.assert_called_once_with("epic", "g1")


# ── GOG dual-source save dir (Auto Cloud vs SDK IStorage) ─────────────────


def _stub_autocloud(monkeypatch):
    # Avoid network: one Auto-Cloud location named "saves" (Documents\MyGame).
    monkeypatch.setattr(
        gog_cloud_api, "fetch_gog_save_locations",
        lambda cid: [("saves", "<?DOCUMENTS?>\\MyGame")],
    )


def test_gog_pick_prefers_autocloud_when_it_has_saves(tmp_path, monkeypatch):
    _stub_autocloud(monkeypatch)
    drive_c = tmp_path / "pfx" / "drive_c"
    doc = drive_c / "users" / "steamuser" / "Documents" / "MyGame"
    doc.mkdir(parents=True)
    (doc / "slot.sav").write_text("SAVE" * 50)
    # Auto-Cloud dir + its cloud namespace name ("saves").
    assert gog_cloud_api.pick_gog_save_dir("CID", drive_c) == (doc, "saves")


def test_gog_pick_uses_sdk_istorage_when_autocloud_empty(tmp_path, monkeypatch):
    _stub_autocloud(monkeypatch)
    drive_c = tmp_path / "pfx" / "drive_c"
    sdk = (
        drive_c / "users" / "steamuser" / "AppData" / "Local"
        / "GOG.com" / "Galaxy" / "Applications" / "CID" / "Storage"
    )
    sdk.mkdir(parents=True)
    (sdk / "save.dat").write_text("DATA" * 50)
    # SDK IStorage dir maps to the "__default" cloud namespace.
    assert gog_cloud_api.pick_gog_save_dir("CID", drive_c) == (sdk, "__default")


def test_gog_pick_falls_back_to_first_autocloud_when_none_on_disk(tmp_path, monkeypatch):
    _stub_autocloud(monkeypatch)
    drive_c = tmp_path / "pfx" / "drive_c"
    (drive_c / "users" / "steamuser").mkdir(parents=True)
    chosen = gog_cloud_api.pick_gog_save_dir("CID", drive_c)
    doc = drive_c / "users" / "steamuser" / "Documents" / "MyGame"
    assert chosen == (doc, "saves")


def test_resolve_gog_location_locallow_underscore_variant(tmp_path):
    # GOG emits both APPLICATION_DATA_LOCALLOW and the underscore variant
    # APPLICATION_DATA_LOCAL_LOW (e.g. Control:Override); both must resolve.
    drive_c = tmp_path / "drive_c"
    locallow = drive_c / "users" / "steamuser" / "AppData" / "LocalLow"
    assert gog_cloud_api.resolve_gog_location(
        "<?APPLICATION_DATA_LOCAL_LOW?>/Studio/Game", drive_c,
    ) == locallow / "Studio" / "Game"
    assert gog_cloud_api.resolve_gog_location(
        "<?APPLICATION_DATA_LOCALLOW?>/Studio/Game", drive_c,
    ) == locallow / "Studio" / "Game"


def test_resolve_gog_location_install_token(tmp_path):
    # Older GOG titles (Fallout, Thief, SSI Gold Box …) save inside the
    # install dir via <?INSTALL?>; resolves against install_path, else None.
    drive_c = tmp_path / "drive_c"
    install = tmp_path / "Games" / "Fallout"
    assert gog_cloud_api.resolve_gog_location(
        "<?INSTALL?>/DATA/SAVEGAME", drive_c, str(install),
    ) == install / "DATA" / "SAVEGAME"
    # Backslash variant + no trailing path.
    assert gog_cloud_api.resolve_gog_location(
        "<?INSTALL?>\\SAVES", drive_c, str(install),
    ) == install / "SAVES"
    # Unknown install path (game not installed) -> unresolvable.
    assert gog_cloud_api.resolve_gog_location(
        "<?INSTALL?>/cloud_saves", drive_c, "",
    ) is None


def test_resolve_gog_location_case_insensitive_disk(tmp_path):
    # gogdl must sync into the dir the game actually created, regardless of
    # the casing in GOG's remote-config (Wine is case-insensitive).
    drive_c = tmp_path / "drive_c"
    created = drive_c / "users" / "steamuser" / "Documents" / "bioshockhd" / "bioshock"
    created.mkdir(parents=True)
    got = gog_cloud_api.resolve_gog_location(
        "<?DOCUMENTS?>/BioshockHD/Bioshock", drive_c,
    )
    assert got == created  # matched the on-disk casing, not the config's


def test_resolve_gog_save_locations_resolves_install_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gog_cloud_api, "fetch_gog_save_locations",
        lambda cid: [("saves", "<?INSTALL?>/Save")],
    )
    drive_c = tmp_path / "drive_c"
    install = str(tmp_path / "game")
    # With an install path the install-dir target resolves (+ SDK fallback).
    got = gog_cloud_api.resolve_gog_save_locations("CID", drive_c, install)
    assert got[0] == (Path(install) / "Save", "saves")
    # Without it, only the SDK IStorage fallback remains.
    only_sdk = gog_cloud_api.resolve_gog_save_locations("CID", drive_c, "")
    assert [n for _, n in only_sdk] == ["__default"]


def test_fetch_gog_save_locations_keeps_namespace_name(monkeypatch):
    monkeypatch.setattr(
        gog_cloud_api, "http_json",
        lambda url, decompress=False: {"content": {"Windows": {"cloudStorage": {
            "locations": [
                {"name": "saves", "location": "<?APPLICATION_DATA_ROAMING?>/TRX"},
                {"location": "<?DOCUMENTS?>/NoName"},  # missing name -> __default
            ]
        }}}},
    )
    assert gog_cloud_api.fetch_gog_save_locations("CID") == [
        ("saves", "<?APPLICATION_DATA_ROAMING?>/TRX"),
        ("__default", "<?DOCUMENTS?>/NoName"),
    ]


def _gog_strategy(tmp_path):
    """A GOGCloudSaveStrategy whose state file lives under tmp_path."""
    # _get_state_file() = local_save_root.parent / cloud_sync_state.json; in
    # production the data dir always exists, so create it here too.
    (tmp_path / "saves").mkdir(parents=True, exist_ok=True)
    return GOGCloudSaveStrategy(str(tmp_path / "saves" / "gog"))


def test_resolve_cloud_namespace_defaults_to_default(tmp_path):
    # No cached entry (e.g. enriched/title-tier or config-override dir) ->
    # gogdl's own default namespace, which is correct for SDK-IStorage games.
    strat = _gog_strategy(tmp_path)
    assert strat._resolve_cloud_namespace("g404") == "__default"


def test_resolve_cloud_namespace_uses_cached_name(tmp_path):
    strat = _gog_strategy(tmp_path)
    strat._write_cached_save_dir("g1", "/some/dir", "saves")
    # Fresh instance (no in-memory cache) reads it back off disk.
    strat2 = _gog_strategy(tmp_path)
    assert strat2._resolve_cloud_namespace("g1") == "saves"


def test_read_cached_save_dir_tolerates_legacy_string(tmp_path):
    strat = _gog_strategy(tmp_path)
    state_file = strat._get_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Legacy on-disk shape: a bare path string, no namespace.
    state_file.write_text(json.dumps({"gog_save_dirs": {"g1": "/legacy/dir"}}))
    assert strat._read_cached_save_dir("g1") == ("/legacy/dir", None)
    # -> namespace falls back to __default until re-resolved.
    assert strat._resolve_cloud_namespace("g1") == "__default"


async def _run_gog_sync_capture_cmd(tmp_path, namespace, *, direction):
    # Drive _do_sync_down/_do_sync_up directly: the base sync_* wrappers add
    # snapshot/guard machinery (real-FS side effects) irrelevant to the cmd.
    strat = GOGCloudSaveStrategy(str(tmp_path / "saves"))
    strat.gogdl_bin = "mock_gogdl"
    strat._convert_gog_token = MagicMock(return_value="/tmp/auth.json")
    save_dir = str(tmp_path / "gogsave")
    os.makedirs(save_dir, exist_ok=True)
    (Path(save_dir) / "slot.sav").write_text("SAVE" * 50)  # real save data
    strat._cached_namespace["gog123"] = namespace
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"1.0", b"")
        mock_exec.return_value = proc
        if direction == "down":
            assert await strat._do_sync_down("gog123", save_dir, False) is True
        else:
            assert await strat._do_sync_up("gog123", save_dir) is True
    return list(mock_exec.call_args.args)


@pytest.mark.asyncio
async def test_gog_sync_down_passes_cloud_namespace(tmp_path):
    cmd = await _run_gog_sync_capture_cmd(tmp_path, "saves", direction="down")
    assert "--name" in cmd
    assert cmd[cmd.index("--name") + 1] == "saves"
    assert "--skip-upload" in cmd


@pytest.mark.asyncio
async def test_gog_sync_up_passes_cloud_namespace(tmp_path):
    cmd = await _run_gog_sync_capture_cmd(tmp_path, "saves", direction="up")
    assert "--name" in cmd
    assert cmd[cmd.index("--name") + 1] == "saves"
    assert "--skip-download" in cmd


# ── GOG multi-location games (e.g. BioShock Remastered: saves + saves2) ───


def test_resolve_gog_save_locations_returns_all_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gog_cloud_api, "fetch_gog_save_locations",
        lambda cid: [
            ("saves", "<?DOCUMENTS?>/BioshockHD/Bioshock"),
            ("saves2", "<?APPLICATION_DATA_ROAMING?>/BioshockHD/Bioshock/SaveGames"),
        ],
    )
    drive_c = tmp_path / "drive_c"
    su = drive_c / "users" / "steamuser"
    assert gog_cloud_api.resolve_gog_save_locations("CID", drive_c) == [
        (su / "Documents" / "BioshockHD" / "Bioshock", "saves"),
        (su / "AppData" / "Roaming" / "BioshockHD" / "Bioshock" / "SaveGames", "saves2"),
        (su / "AppData" / "Local" / "GOG.com" / "Galaxy" / "Applications"
         / "CID" / "Storage", "__default"),
    ]


def _names_and_dirs(mock_exec):
    names, dirs = [], []
    for call in mock_exec.call_args_list:
        argv = list(call.args)
        names.append(argv[argv.index("--name") + 1])
        dirs.append(argv[argv.index("save-sync") + 1])
    return names, dirs


@pytest.mark.asyncio
async def test_gog_multi_location_sync_down_syncs_all_targets(tmp_path):
    strat = _gog_strategy(tmp_path)
    strat.gogdl_bin = "mock_gogdl"
    strat._convert_gog_token = MagicMock(return_value="/tmp/auth.json")
    a, b = tmp_path / "A", tmp_path / "B"
    for d in (a, b):
        d.mkdir()
        (d / "x.sav").write_text("SAVE" * 50)  # real saves -> no clean-pull clear
    strat._cached_targets["g"] = [(str(a), "saves"), (str(b), "saves2")]
    with patch("asyncio.create_subprocess_exec") as mock_exec, \
            patch("unifideck.services.cloud_save.safety.snapshot_backup"):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"1.0", b"")
        mock_exec.return_value = proc
        assert await strat._do_sync_down("g", str(a), False) is True
    names, dirs = _names_and_dirs(mock_exec)
    assert names == ["saves", "saves2"]      # BOTH namespaces synced
    assert dirs == [str(a), str(b)]          # each into its own dir


@pytest.mark.asyncio
async def test_gog_multi_location_sync_up_skips_dirs_without_saves(tmp_path):
    strat = _gog_strategy(tmp_path)
    strat.gogdl_bin = "mock_gogdl"
    strat._convert_gog_token = MagicMock(return_value="/tmp/auth.json")
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    (a / "x.sav").write_text("SAVE" * 50)    # real saves
    (b / "config.ini").write_text("[x]")     # config only -> has_save_data False
    strat._cached_targets["g"] = [(str(a), "saves"), (str(b), "saves2")]
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"1.0", b"")
        mock_exec.return_value = proc
        assert await strat._do_sync_up("g", str(a)) is True
    names, _ = _names_and_dirs(mock_exec)
    assert names == ["saves"]                # saves2 (config-only) NOT uploaded


def test_gog_timestamp_key_per_namespace(tmp_path):
    strat = _gog_strategy(tmp_path)
    strat._save_timestamp("g", "11.0")            # default namespace -> bare key
    strat._save_timestamp("g", "22.0", "saves2")  # extra namespace -> namespaced key
    assert strat._get_saved_timestamp("g") == "11.0"
    assert strat._get_saved_timestamp("g", "saves2") == "22.0"
    assert strat._get_saved_timestamp("g", "saves") == "0"  # untouched namespace
    state = json.loads(strat._get_state_file().read_text())
    assert state["gog"]["g"] == "11.0"            # backward-compatible key
    assert state["gog"]["g::saves2"] == "22.0"


def test_gog_native_linux_routes_to_enriched_linux(tmp_path):
    # A native-Linux GOG game (start.sh) has no Wine prefix and GOG remote-config
    # cloud is Windows/Mac-only, so resolution must go straight to the enriched
    # Linux tier with native_linux=True + the install dir.
    strat = _gog_strategy(tmp_path)
    strat._is_native_linux = lambda gid: True
    strat._install_dir = lambda gid: "/games/G"
    captured = {}

    def fake_enriched(game_id, *, prefix_path, install_path="", native_linux=False):
        captured.update(
            native_linux=native_linux, install_path=install_path,
            prefix_path=prefix_path,
        )
        return "/home/deck/.local/share/G"

    strat._resolve_enriched = fake_enriched
    assert strat._resolve_store_save_dir("g") == "/home/deck/.local/share/G"
    assert captured["native_linux"] is True
    assert captured["install_path"] == "/games/G"


@pytest.mark.asyncio
async def test_gog_native_linux_sync_uses_os_windows(tmp_path):
    # GOG's builds API answers os/linux with 404 "Unsupported OS", which
    # crashed gogdl's get_auth_ids. The Galaxy clientId is per product, so
    # native-Linux installs ask for the windows manifest too.
    strat = _gog_strategy(tmp_path)
    strat.gogdl_bin = "mock_gogdl"
    strat._convert_gog_token = MagicMock(return_value="/tmp/auth.json")
    strat._is_native_linux = lambda gid: True
    d = tmp_path / "lin"
    d.mkdir()
    (d / "x.sav").write_text("SAVE" * 50)
    strat._cached_targets["g"] = [(str(d), "__default")]
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"1.0", b"")
        mock_exec.return_value = proc
        assert await strat._do_sync_down("g", str(d), False) is True
    cmd = list(mock_exec.call_args.args)
    assert cmd[cmd.index("--os") + 1] == "windows"


def test_gog_cached_targets_roundtrip(tmp_path):
    strat = _gog_strategy(tmp_path)
    strat._write_cached_save_dir(
        "g", "/p", "saves", [("/p", "saves"), ("/q", "saves2")],
    )
    # Fresh instance reads the full target list back off disk.
    fresh = _gog_strategy(tmp_path)
    assert fresh._resolve_sync_targets("g", "/fallback") == [
        ("/p", "saves"), ("/q", "saves2"),
    ]


# ── ~/Save Games Backup is WRITE-ONLY (never pulled from) ─────────────────


def _wo_service(tmp_path, bus, cfg, local_dir):
    svc = CloudSaveService(
        bus, str(tmp_path / "saves"),
        cloud_root=str(tmp_path / "backup"), config=cfg,
    )
    strat = MagicMock()
    strat.sync_down = AsyncMock(return_value=True)
    strat.sync_up = AsyncMock(return_value=True)
    strat.get_local_save_dir.return_value = str(local_dir)
    svc._strategies["gog"] = strat
    return svc


@pytest.mark.asyncio
async def test_mirror_written_on_sync_down(tmp_path, mock_event_bus, mock_config):
    local = tmp_path / "local"
    local.mkdir()
    (local / "save.dat").write_text("DATA" * 50)
    svc = _wo_service(tmp_path, mock_event_bus, mock_config, local)
    with patch.object(CloudSaveService, "_acquire_sync_lock", return_value=(MagicMock(), None)):
        res = await svc.sync_down("gog", "g1", force=True)
    assert res.success is True
    assert (tmp_path / "backup" / "gog" / "g1" / "save.dat").is_file()


@pytest.mark.asyncio
async def test_empty_local_never_wipes_mirror(tmp_path, mock_event_bus, mock_config):
    mirror = tmp_path / "backup" / "gog" / "g1"
    mirror.mkdir(parents=True)
    (mirror / "old.dat").write_text("OLD" * 50)
    local = tmp_path / "local"
    local.mkdir()  # empty
    svc = _wo_service(tmp_path, mock_event_bus, mock_config, local)
    with patch.object(CloudSaveService, "_acquire_sync_lock", return_value=(MagicMock(), None)):
        await svc.sync_down("gog", "g1", force=True)
    assert (mirror / "old.dat").is_file()  # backup preserved


@pytest.mark.asyncio
async def test_sync_down_never_pulls_from_mirror(tmp_path, mock_event_bus, mock_config):
    mirror = tmp_path / "backup" / "gog" / "g1"
    mirror.mkdir(parents=True)
    (mirror / "cloud.dat").write_text("CLOUD" * 50)
    local = tmp_path / "local"
    local.mkdir()  # strategy is a no-op; local stays empty
    svc = _wo_service(tmp_path, mock_event_bus, mock_config, local)
    with patch.object(CloudSaveService, "_acquire_sync_lock", return_value=(MagicMock(), None)):
        await svc.sync_down("gog", "g1", force=True)
    assert not (local / "cloud.dat").exists()  # never restored from the mirror


@pytest.mark.asyncio
async def test_unresolved_when_no_real_location(tmp_path, mock_event_bus, mock_config):
    # No prefix → the strategy resolves NO real location (returns None). The
    # staging fallback is gone, so status must show unresolved + no local saves
    # (we never read a staging dir, even one with leftover files).
    saves_root = tmp_path / "saves"
    # leftover staging files exist but must be ignored entirely
    staging = saves_root / "gog" / "g1"
    staging.mkdir(parents=True)
    (staging / "old.sav").write_text("OLD" * 50)
    svc = CloudSaveService(
        mock_event_bus, str(saves_root), cloud_root=None, config=mock_config,
    )
    svc._strategies["gog"].get_local_save_dir = lambda gid: None
    svc._real_cloud_info = AsyncMock(return_value=None)
    st = await svc.get_cloud_status("gog", "g1")
    assert st["save_path"] is None
    assert st["save_path_resolved"] is False
    assert st["has_local_saves"] is False
    assert st["local_snapshot"] == {}


# ── GOG forced pull does a CLEAN download (clears local first) ─────────────


@pytest.mark.asyncio
async def test_gog_force_pull_clears_local_first(tmp_path, mock_config):
    # gogdl skips cloud-only files when local is non-empty ("conflict"); a
    # forced "Use Cloud" pull must clear local first so the full set downloads.
    local = tmp_path / "save"
    local.mkdir()
    (local / "stale.sav").write_text("STALE")
    s = GOGCloudSaveStrategy(str(tmp_path / "root"), mock_config)
    s._convert_gog_token = MagicMock(return_value="/tmp/auth.json")
    s.get_local_save_dir = MagicMock(return_value=str(local))
    s.gogdl_bin = "mock_gogdl"
    with patch("unifideck.services.cloud_save.safety.snapshot_backup"), \
         patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"1.0", b"")
        mock_exec.return_value = proc
        await s.sync_down("g1", force=True)
    assert not (local / "stale.sav").exists()  # cleared before the clean pull


@pytest.mark.asyncio
async def test_gog_normal_pull_keeps_existing_saves(tmp_path, mock_config):
    # A non-forced pull with REAL local saves must NOT clear them.
    local = tmp_path / "save"
    local.mkdir()
    (local / "keep.sav").write_text("REAL-SAVE-DATA")
    s = GOGCloudSaveStrategy(str(tmp_path / "root"), mock_config)
    s._convert_gog_token = MagicMock(return_value="/tmp/auth.json")
    s.get_local_save_dir = MagicMock(return_value=str(local))
    s.gogdl_bin = "mock_gogdl"
    with patch("unifideck.services.cloud_save.safety.snapshot_backup"), \
         patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"1.0", b"")
        mock_exec.return_value = proc
        await s.sync_down("g1", force=False)
    assert (local / "keep.sav").exists()  # preserved


def test_gog_cloud_summary_counts_only_active_prefix():
    # GOG cloud storage namespaces objects by location name. A game can carry
    # a stale older prefix (``saves/``) alongside the live one (``__default/``).
    # gogdl materializes only ONE locally, so the reported cloud count must be
    # the newest prefix group's count (matching local) — NOT every object.
    objects = [
        {"name": "__default/a.sav", "last_modified": "2026-06-08T18:00:00+00:00"},
        {"name": "__default/b.sav", "last_modified": "2026-06-08T18:01:00+00:00"},
        # our own manifest is never a save file:
        {"name": "__default/.unifideck_sync.json", "last_modified": "2026-06-08T18:02:00+00:00"},
        {"name": "saves/old1.sav", "last_modified": "2026-03-29T20:00:00+00:00"},
        {"name": "saves/old2.sav", "last_modified": "2026-03-29T20:01:00+00:00"},
        {"name": "saves/old3.sav", "last_modified": "2026-03-29T20:02:00+00:00"},
    ]
    info = gog_cloud_api.summarize_cloud_objects(objects)
    assert info["file_count"] == 2  # __default's two real files, not 5
    assert info["has_saves"] is True
    # timestamp is the active group's newest (Jun 8 b.sav, not the manifest)
    from datetime import datetime
    expected = datetime.fromisoformat("2026-06-08T18:01:00+00:00").astimezone().timestamp()
    assert info["timestamp"] == expected


def test_gog_cloud_summary_empty_and_flat():
    empty = gog_cloud_api.summarize_cloud_objects([])
    assert empty["file_count"] == 0 and empty["has_saves"] is False
    flat = gog_cloud_api.summarize_cloud_objects(
        [{"name": "solo.sav", "last_modified": "2026-01-01T00:00:00+00:00"}]
    )
    assert flat["file_count"] == 1 and flat["has_saves"] is True


def test_gog_cloud_summary_prefers_preserved_local_mtime():
    # The LIST reports server PUT times (newer, jump to "now" on every upload);
    # the preserved X-Object-Meta-LocalLastModified (via the resolver) is the
    # save's real mtime. The reported timestamp must be the newest LOCAL mtime
    # so "Cloud" matches "Local" after a push instead of showing the upload time.
    from datetime import datetime
    objects = [
        {"name": "saves/a.sav", "last_modified": "2026-06-25T00:16:08+00:00"},
        {"name": "saves/b.sav", "last_modified": "2026-06-25T00:16:07+00:00"},
    ]
    local = {
        "saves/a.sav": "2026-06-25T00:05:35+00:00",
        "saves/b.sav": "2026-06-25T00:04:20+00:00",
    }

    def resolver(name):
        v = local.get(name)
        return datetime.fromisoformat(v).astimezone().timestamp() if v else None

    info = gog_cloud_api.summarize_cloud_objects(objects, mtime_resolver=resolver)
    expected = datetime.fromisoformat(
        "2026-06-25T00:05:35+00:00"
    ).astimezone().timestamp()
    assert info["timestamp"] == expected  # newest LOCAL mtime, not 00:16:08
    assert info["file_count"] == 2


def test_gog_cloud_summary_resolver_falls_back_to_server_time():
    # When the resolver can't supply a local mtime (missing header / HEAD fail)
    # for an object, that object falls back to its server ``last_modified`` — so
    # the result is never worse than the old server-time-only behaviour.
    from datetime import datetime
    objects = [
        {"name": "saves/a.sav", "last_modified": "2026-06-25T00:16:08+00:00"},
        {"name": "saves/b.sav", "last_modified": "2026-06-25T00:10:00+00:00"},
    ]

    def resolver(name):  # only b resolves; a (None) falls back to its server ts
        if name == "saves/b.sav":
            return datetime.fromisoformat(
                "2026-06-25T00:05:00+00:00"
            ).astimezone().timestamp()
        return None

    info = gog_cloud_api.summarize_cloud_objects(objects, mtime_resolver=resolver)
    expected = datetime.fromisoformat(  # a's server 00:16:08 is newest overall
        "2026-06-25T00:16:08+00:00"
    ).astimezone().timestamp()
    assert info["timestamp"] == expected


# ── Manual pull/push are fire-and-forget (don't block the RPC) ─────────────


@pytest.mark.asyncio
async def test_cloud_save_pull_is_fire_and_forget(mock_event_bus):
    # cloud_save_pull must return immediately ({"started": True}) and NOT block
    # on the (slow) sync — otherwise the RPC client times out and shows a false
    # failure even when the download succeeds.
    from unifideck.rpc.mixins.cloud_save import CloudSaveRPCMixin, _SYNC_TASKS
    started = asyncio.Event()
    done = asyncio.Event()

    async def slow_sync_down(store, game_id, force=False):
        started.set()
        await asyncio.sleep(0.05)
        done.set()
        return Result(success=True)

    svc = MagicMock()
    svc.sync_down = slow_sync_down

    class Host(CloudSaveRPCMixin):
        def __init__(self):
            self.services = MagicMock(cloudsave=svc)

    res = await Host().cloud_save_pull("gog", "g1", True)
    assert res == {"started": True}          # returned before the sync finished
    assert not done.is_set()                 # sync still running in background
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(done.wait(), 1)   # it does complete in the background


# ── Relocate orphaned saves tests (Tomb Raider steam_api64.dll pattern) ────

def test_relocate_orphaned_saves_copies_from_numeric_subfolder(tmp_path):
    from unifideck.services.cloud_save.strategy_base import CloudSaveStrategy
    save_dir = tmp_path / "TRX"
    numeric_dir = save_dir / "76561198000000000"
    numeric_dir.mkdir(parents=True)
    (numeric_dir / "savegame.dat").write_text("SAVE_DATA")

    CloudSaveStrategy._relocate_orphaned_saves(str(save_dir))

    assert (save_dir / "savegame.dat").is_file()
    assert (save_dir / "savegame.dat").read_text() == "SAVE_DATA"
    # Original file must be preserved for cloud sync compatibility
    assert (numeric_dir / "savegame.dat").is_file()


def test_relocate_orphaned_saves_noop_if_root_save_exists(tmp_path):
    from unifideck.services.cloud_save.strategy_base import CloudSaveStrategy
    save_dir = tmp_path / "TRX"
    numeric_dir = save_dir / "76561198000000000"
    numeric_dir.mkdir(parents=True)
    (numeric_dir / "savegame.dat").write_text("OLD_SAVE_DATA")
    (save_dir / "savegame.dat").write_text("ROOT_SAVE_DATA")

    CloudSaveStrategy._relocate_orphaned_saves(str(save_dir))

    # Root save should remain untouched
    assert (save_dir / "savegame.dat").read_text() == "ROOT_SAVE_DATA"


def test_relocate_orphaned_saves_noop_for_non_numeric_dirs(tmp_path):
    from unifideck.services.cloud_save.strategy_base import CloudSaveStrategy
    save_dir = tmp_path / "Game"
    profile_dir = save_dir / "profile1"
    profile_dir.mkdir(parents=True)
    (profile_dir / "savegame.dat").write_text("PROFILE_SAVE")

    CloudSaveStrategy._relocate_orphaned_saves(str(save_dir))

    assert not (save_dir / "savegame.dat").exists()

