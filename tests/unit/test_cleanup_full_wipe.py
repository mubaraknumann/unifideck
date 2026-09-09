"""Tests for the "100% clean" pieces of ``perform_full_cleanup``.

Covers the gaps surfaced by live validation (a destructive "Delete all
Unifideck data" left ~22 GB of Proton prefixes, all residual state, the
Ubisoft installer cache, and a working GOG login on disk):

* :func:`safe_delete.is_safe_to_delete` structural guard (replaces the old
  loose substring allowlist that skipped custom install locations).
* ``_wipe_data_dir`` two-tier behaviour — non-destructive keeps the data
  needed to keep installed games playable (``prefixes``/``saves``/
  ``save_backups``); destructive removes everything.
* ``_delete_external_prefixes`` reaches SD/custom Ubisoft prefixes recorded
  in ``ubisoft_id_map.json`` (outside the data dir).
* ``_wipe_config_auth`` deletes the real GOG creds while preserving the
  user's ``config.json`` and Heroic's ``heroic_gogdl``.
* ``_delete_install_dir`` deletes a recorded work_dir at any location but
  refuses ``$HOME`` / shallow paths.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from unifideck.core import marker_sweep, safe_delete
from unifideck.core import cleanup_sweeps
from unifideck.rpc.mixins.sync import SyncRPCMixin


def _mixin(**attrs: Any) -> SyncRPCMixin:
    m = SyncRPCMixin()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# --------------------------------------------------------------------------
# safe_delete guard
# --------------------------------------------------------------------------
def test_is_safe_to_delete_rejects_dangerous_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    assert not safe_delete.is_safe_to_delete("")
    assert not safe_delete.is_safe_to_delete("/")
    assert not safe_delete.is_safe_to_delete(str(home))
    assert not safe_delete.is_safe_to_delete(str(home.parent))  # ancestor
    # Custom/SD install locations must be deletable (the old bug).
    assert safe_delete.is_safe_to_delete(str(home / "Games"))
    assert safe_delete.is_safe_to_delete(
        "/run/media/deck/SD/MyLibrary/SomeGame",
    )


def test_safe_rmtree_refuses_home_but_deletes_deep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    (home / "keep.txt").write_text("x")
    assert safe_delete.safe_rmtree(home) is False  # guard refused
    assert (home / "keep.txt").exists()

    deep = home / "a/b/c/game"
    deep.mkdir(parents=True)
    assert safe_delete.safe_rmtree(deep) is True
    assert not deep.exists()


# --------------------------------------------------------------------------
# _wipe_data_dir — two-tier
# --------------------------------------------------------------------------
def _populate_data_dir(home: Path) -> Path:
    data = home / ".local/share/unifideck"
    (data / "prefixes/gameA").mkdir(parents=True)
    (data / "saves/x").mkdir(parents=True)
    (data / "save_backups/x").mkdir(parents=True)
    (data / "ubisoft_installer_cache").mkdir()
    (data / "edge-auth").mkdir()
    for f in ("library_cache.json", "shortcuts_registry.json",
              "download_history.json", "playtime.db", "games.map",
              "ubisoft_id_map.json"):
        (data / f).write_text("x")
    return data


@pytest.mark.asyncio
async def test_wipe_data_dir_keeps_games_when_non_destructive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    data = _populate_data_dir(home)
    m = _mixin()

    await m._wipe_data_dir(delete_files=False)

    # Playable-game data kept so games stay re-syncable.
    assert (data / "prefixes").is_dir()
    assert (data / "saves").is_dir()
    assert (data / "save_backups").is_dir()
    # Everything else (state + caches) gone.
    assert not (data / "library_cache.json").exists()
    assert not (data / "shortcuts_registry.json").exists()
    assert not (data / "ubisoft_installer_cache").exists()
    assert not (data / "edge-auth").exists()


@pytest.mark.asyncio
async def test_wipe_data_dir_removes_everything_when_destructive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    data = _populate_data_dir(home)
    m = _mixin()

    await m._wipe_data_dir(delete_files=True)

    assert not (data / "prefixes").exists()
    assert not (data / "saves").exists()
    assert not (data / "save_backups").exists()
    assert not (data / "library_cache.json").exists()
    # The data dir itself survives (contents only) for the plugin to reuse.
    assert data.is_dir()


# --------------------------------------------------------------------------
# _delete_external_prefixes — SD/custom Ubisoft prefixes
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_external_prefixes_reaches_out_of_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    data = home / ".local/share/unifideck"
    data.mkdir(parents=True)
    # External (custom storage) prefix + an internal one (handled elsewhere).
    external = home / "Games/prefixes/ubisoft/46"
    external.mkdir(parents=True)
    internal = data / "prefixes/ubisoft/109"
    internal.mkdir(parents=True)
    (data / "ubisoft_id_map.json").write_text(json.dumps({
        "46": {"prefix_path": str(external)},
        "109": {"prefix_path": str(internal)},
        "4": {"name": "no-prefix"},
    }))
    m = _mixin()

    count = await m._delete_external_prefixes()

    assert count == 1
    assert not external.exists()      # external one removed
    assert internal.exists()          # internal left to the data-dir wipe


@pytest.mark.asyncio
async def test_delete_external_prefixes_covers_every_wrapper_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A store missing from the sweep leaks whole Wine prefixes.

    Each is gigabytes — the Battle.net one measured 2.5 GB on-device — and
    an id map left unread is the only record of where they went, so nothing
    later can find them either.
    """
    home = _fake_home(monkeypatch, tmp_path)
    data = home / ".local/share/unifideck"
    data.mkdir(parents=True)
    ubi = home / "Games/prefixes/ubisoft/46"
    bnet = home / "Games/prefixes/battlenet/D1"
    for d in (ubi, bnet):
        d.mkdir(parents=True)
    (data / "ubisoft_id_map.json").write_text(
        json.dumps({"46": {"prefix_path": str(ubi)}}),
    )
    (data / "battlenet_id_map.json").write_text(
        json.dumps({"D1": {"family": "D1", "prefix_path": str(bnet)}}),
    )
    m = _mixin()

    count = await m._delete_external_prefixes()

    assert count == 2
    assert not ubi.exists()
    assert not bnet.exists()


@pytest.mark.asyncio
async def test_keeping_games_keeps_the_index_into_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The id map is what maps a uid to the prefix holding its files.

    The resolvers deliberately never reconstruct a path from a uid, so a
    non-destructive cleanup that kept ``prefixes/`` but deleted the map
    turned every installed wrapper-store game into an unreachable orphan.
    """
    home = _fake_home(monkeypatch, tmp_path)
    data = home / ".local/share/unifideck"
    (data / "prefixes/battlenet/D1").mkdir(parents=True)
    (data / "battlenet_id_map.json").write_text("{}")
    (data / "ubisoft_id_map.json").write_text("{}")
    (data / "library_cache.json").write_text("{}")
    m = _mixin()

    await m._wipe_data_dir(delete_files=False)

    assert (data / "prefixes/battlenet/D1").exists()
    assert (data / "battlenet_id_map.json").exists()
    assert (data / "ubisoft_id_map.json").exists()
    assert not (data / "library_cache.json").exists(), "caches must still go"


@pytest.mark.asyncio
async def test_destructive_cleanup_keeps_no_id_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """"Delete all data" means the maps go with the prefixes they index."""
    home = _fake_home(monkeypatch, tmp_path)
    data = home / ".local/share/unifideck"
    (data / "prefixes/battlenet/D1").mkdir(parents=True)
    (data / "battlenet_id_map.json").write_text("{}")
    m = _mixin()

    await m._wipe_data_dir(delete_files=True)

    assert not (data / "battlenet_id_map.json").exists()
    assert not (data / "prefixes").exists()


# --------------------------------------------------------------------------
# _wipe_config_auth — GOG creds vs preserved files
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wipe_config_auth_deletes_gog_creds_keeps_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    cfg = home / ".config/unifideck"
    cfg.mkdir(parents=True)
    for f in ("gog_credentials.json", "gogdl_auth.json", "gog_token.json",
              "config.json"):
        (cfg / f).write_text("x")
    (cfg / "gogdl").mkdir()
    (cfg / "heroic_gogdl").mkdir()
    m = _mixin()

    await m._wipe_config_auth()

    assert not (cfg / "gog_credentials.json").exists()
    assert not (cfg / "gogdl_auth.json").exists()
    assert not (cfg / "gogdl").exists()
    # User prefs preserved. ``heroic_gogdl`` is ours (gogdl names it, we set
    # GOGDL_CONFIG_PATH) but only its ``manifests/`` are pruned, and by
    # ``sweep_gogdl_manifests`` — not here.
    assert (cfg / "config.json").exists()
    assert (cfg / "heroic_gogdl").exists()


# --------------------------------------------------------------------------
# _delete_install_dir — robust guard
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_install_dir_handles_custom_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _fake_home(monkeypatch, tmp_path)
    custom = tmp_path / "mnt/ssd/MyLibrary/SomeGame"
    custom.mkdir(parents=True)
    (custom / "game.exe").write_text("x")
    m = _mixin()

    assert await m._delete_install_dir(str(custom)) is True
    assert not custom.exists()


@pytest.mark.asyncio
async def test_delete_install_dir_refuses_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    m = _mixin()
    assert await m._delete_install_dir(str(home)) is False
    assert home.exists()


# --------------------------------------------------------------------------
# sweep_gogdl_manifests — GOG's only install-side record
# --------------------------------------------------------------------------
def _gogdl_manifests(home: Path, *game_ids: str) -> Path:
    d = home / ".config/unifideck/heroic_gogdl/manifests"
    d.mkdir(parents=True)
    for gid in game_ids:
        (d / gid).write_text("{}")
    return d


def test_sweep_gogdl_manifests_clears_our_manifest_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    home = _fake_home(monkeypatch, tmp_path)
    d = _gogdl_manifests(home, "1434021265", "1207658755")
    # A sibling gogdl cache that is NOT a manifest must survive.
    support = home / ".config/unifideck/heroic_gogdl/gog-support"
    support.mkdir(parents=True)
    (support / "keep").write_text("x")

    assert cleanup_sweeps.sweep_gogdl_manifests() == 2

    assert list(d.iterdir()) == []
    assert d.is_dir()          # the dir itself stays, gogdl reuses it
    assert (support / "keep").exists()


def test_sweep_gogdl_manifests_never_touches_heroics_own_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``~/.config/heroic`` is a different app's data — never in scope.

    Only ``~/.config/unifideck/heroic_gogdl`` is ours (gogdl picks the
    directory name; we point ``GOGDL_CONFIG_PATH`` at its parent).
    """
    home = _fake_home(monkeypatch, tmp_path)
    _gogdl_manifests(home, "1434021265")
    heroic = home / ".config/heroic/heroic_gogdl/manifests"
    heroic.mkdir(parents=True)
    (heroic / "1434021265").write_text("{}")

    assert cleanup_sweeps.sweep_gogdl_manifests() == 1
    assert (heroic / "1434021265").exists()


def test_sweep_gogdl_manifests_noop_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _fake_home(monkeypatch, tmp_path)
    assert cleanup_sweeps.sweep_gogdl_manifests() == 0


# --------------------------------------------------------------------------
# sweep_cache_backups — the clear's own .bak snapshots
# --------------------------------------------------------------------------
def test_sweep_cache_backups_removes_bak_and_keeps_live_caches(
    tmp_path: Path,
) -> None:
    """Clearing a namespace snapshots its old contents to ``.bak``.

    So a wipe left the pre-wipe caches sitting next to the emptied files —
    and ``CacheStore._load`` restores from ``.bak`` when the live file fails
    to parse, which would bring the whole wiped cache back.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "metadata_cache.json").write_text("{}")
    (cache / "metadata_cache.json.bak").write_text('{"stale": 1}')
    (cache / "compat_cache.json.bak").write_text('{"stale": 1}')
    (cache / "notes.txt").write_text("keep me")

    assert cleanup_sweeps.sweep_cache_backups(str(cache)) == 2

    assert (cache / "metadata_cache.json").exists()
    assert not (cache / "metadata_cache.json.bak").exists()
    assert not (cache / "compat_cache.json.bak").exists()
    assert (cache / "notes.txt").exists()


def test_sweep_cache_backups_tolerates_missing_dir(tmp_path: Path) -> None:
    assert cleanup_sweeps.sweep_cache_backups(str(tmp_path / "nope")) == 0


# --------------------------------------------------------------------------
# sweep_stale_install_records — dangling CLI rows, post-sweep
# --------------------------------------------------------------------------
def _cli_records(home: Path, *, legendary: Any, nile: Any) -> None:
    (home / ".config/legendary").mkdir(parents=True, exist_ok=True)
    (home / ".config/legendary/installed.json").write_text(
        json.dumps(legendary),
    )
    (home / ".config/nile").mkdir(parents=True, exist_ok=True)
    (home / ".config/nile/installed.json").write_text(json.dumps(nile))


def test_sweep_stale_install_records_drops_only_dangling_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A row whose dir survives is never touched — safe in both modes."""
    home = _fake_home(monkeypatch, tmp_path)
    live_epic = home / "Games/Frostpunk"
    live_epic.mkdir(parents=True)
    live_amazon = home / "Games/Grime"
    live_amazon.mkdir(parents=True)
    _cli_records(
        home,
        legendary={
            "b2e0": {"install_path": str(live_epic)},
            "5ab7": {"install_path": str(home / "Games/WeirdWest")},  # gone
        },
        nile=[
            {"id": "amzn1.live", "path": str(live_amazon)},
            {"id": "amzn1.gone", "path": str(home / "Games/BangBang")},
        ],
    )

    assert cleanup_sweeps.sweep_stale_install_records(False) == 2

    legendary = json.loads(
        (home / ".config/legendary/installed.json").read_text(),
    )
    nile = json.loads((home / ".config/nile/installed.json").read_text())
    assert set(legendary) == {"b2e0"}
    assert [e["id"] for e in nile] == ["amzn1.live"]


def test_sweep_stale_install_records_drops_nile_manifest_with_the_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """nile's manifest cache is what actually vetoes a re-install."""
    home = _fake_home(monkeypatch, tmp_path)
    manifests = home / ".config/nile/manifests"
    manifests.mkdir(parents=True)
    (manifests / "amzn1.gone.raw").write_text("x")
    _cli_records(
        home,
        legendary={},
        nile=[{"id": "amzn1.gone", "path": str(home / "Games/BangBang")}],
    )

    assert cleanup_sweeps.sweep_stale_install_records(False) >= 1
    assert not (manifests / "amzn1.gone.raw").exists()


def test_sweep_stale_install_records_gates_gog_manifests_on_destructive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Non-destructive keeps the games, so the manifest is still accurate.

    Dropping it there would turn the next update into a full re-download
    instead of a delta.
    """
    home = _fake_home(monkeypatch, tmp_path)
    d = _gogdl_manifests(home, "1434021265")
    _cli_records(home, legendary={}, nile=[])

    assert cleanup_sweeps.sweep_stale_install_records(False) == 0
    assert (d / "1434021265").exists()

    assert cleanup_sweeps.sweep_stale_install_records(True) == 1
    assert not (d / "1434021265").exists()


def test_pruning_must_run_after_the_marker_sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Ordering regression: the records ARE the sweep's root index.

    ``collect_install_roots`` derives its roots from the very
    ``installed.json`` rows the prune removes, so pruning first would blind
    ``sweep_all`` to every install dir.
    """
    home = _fake_home(monkeypatch, tmp_path)
    game = home / "Games/WeirdWest"
    game.mkdir(parents=True)
    (game / ".unifideck_manifest.json").write_text(
        json.dumps({"store": "epic", "store_id": "5ab7"}),
    )
    _cli_records(
        home,
        legendary={"5ab7": {"install_path": str(game)}},
        nile=[],
    )

    roots = marker_sweep.collect_install_roots()
    assert roots  # the record gave us the root

    assert marker_sweep.sweep_all(roots) == 1
    assert not game.exists()

    # Only now is the row dangling — and now it can be pruned.
    assert cleanup_sweeps.sweep_stale_install_records(False) == 1
    assert marker_sweep.collect_install_roots() == set()
