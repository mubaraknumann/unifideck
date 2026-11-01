"""Tests for the auth-clearing side of ``perform_full_cleanup``.

Covers the three pieces hardened so "Delete all Unifideck data" is an
authoritative sign-out:

* ``_logout_all_stores`` counts only stores that *actually* reported a
  successful logout (the registry maps each store to a
  ``{"success", "error"}`` dict — a naive ``if v`` over-counts every
  store).
* ``_delete_auth_data`` unlinks each store's persisted credential file.
* ``_reset_store_availability`` clears the in-memory ``_cached_available``
  flag on every registered store.

Plus the rest of the live in-process state the wipe used to leave behind —
the reason a destructive cleanup still listed the games it had just deleted:
``reset_library_state`` (in-memory library + its cache file, and the
resurrect-on-next-save regression), and ``_finalize_wipe``'s ordering,
per-game announce, memo clear, and best-effort contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unifideck.core.sync_cache_mixin import _SyncCacheMixin
from unifideck.core.types.domain import Game
from unifideck.core.types.events import Events
from unifideck.rpc.mixins.sync import SyncRPCMixin
from unifideck.services import installed_disk_info
from unifideck.services.artwork.event_handlers import _EventHandlersMixin
from unifideck.services.artwork.fetcher import delete_artwork_files


def _mixin(**attrs: Any) -> SyncRPCMixin:
    m = SyncRPCMixin()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _Registry:
    def __init__(self, results: dict[str, Any], stores: dict[str, Any]):
        self._results = results
        self._stores = stores

    async def logout_all(self) -> dict[str, Any]:
        return self._results


@pytest.mark.asyncio
async def test_logout_all_stores_counts_only_successes() -> None:
    registry = _Registry(
        results={
            "epic": {"success": True, "error": None},
            "gog": {"success": False, "error": "boom"},
            "amazon": {"success": True, "error": None},
        },
        stores={},
    )
    m = _mixin(registry=registry)

    assert await m._logout_all_stores() == 2


@pytest.mark.asyncio
async def test_logout_all_stores_handles_missing_registry() -> None:
    m = _mixin(registry=None)
    assert await m._logout_all_stores() == 0


@pytest.mark.asyncio
async def test_delete_auth_data_unlinks_credential_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    # The credential files each store's ``is_available`` probe reads.
    creds = [
        tmp_path / ".config/legendary/user.json",
        tmp_path / ".config/nile/user.json",
        tmp_path / ".config/unifideck/gog_token.json",
        tmp_path / ".config/unifideck/gogdl/gog_credentials.json",
        tmp_path / ".config/unifideck/microsoft_tokens.json",
        tmp_path / ".local/share/unifideck/microsoft_tokens.json",
    ]
    for f in creds:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("secret")

    m = _mixin()
    count = await m._delete_auth_data()

    assert count == len(creds)
    for f in creds:
        assert not f.exists()


@pytest.mark.asyncio
async def test_delete_auth_data_is_safe_when_nothing_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    m = _mixin()
    assert await m._delete_auth_data() == 0


def test_reset_store_availability_clears_cached_flag() -> None:
    stores = {
        "epic": SimpleNamespace(_cached_available=True, store_name="epic"),
        "gog": SimpleNamespace(_cached_available=True, store_name="gog"),
    }
    m = _mixin(registry=SimpleNamespace(_stores=stores))

    m._reset_store_availability()

    assert all(not s._cached_available for s in stores.values())


def test_reset_store_availability_handles_missing_registry() -> None:
    # Should not raise when the registry or its store map is absent.
    _mixin(registry=None)._reset_store_availability()
    _mixin(registry=SimpleNamespace())._reset_store_availability()


# --- artwork deletion -------------------------------------------------

# bit 0x80000000 set → every Unifideck shortcut appid is ≥ 2³¹.
_UNSIGNED = 0x80000000 + 12345  # 2147495993


def _grid_mixin(grid_dir: Path) -> SyncRPCMixin:
    artwork = SimpleNamespace(grid_dir=str(grid_dir))
    return _mixin(services=SimpleNamespace(artwork=artwork))


def _write_all_art(grid_dir: Path, unsigned: int) -> list[Path]:
    """Create one file per artwork kind, named as the fetcher names them."""
    names = [
        f"{unsigned}p.jpg",      # grid (portrait)
        f"{unsigned}.jpg",       # grid_l (landscape header)
        f"{unsigned}_hero.jpg",  # hero banner
        f"{unsigned}_logo.png",  # logo
        f"{unsigned}_icon.jpg",  # icon
    ]
    files = []
    for n in names:
        p = grid_dir / n
        p.write_bytes(b"img")
        files.append(p)
    return files


# --- fetcher.delete_artwork_files (single appid, unconditional) -------

@pytest.mark.asyncio
async def test_delete_artwork_files_removes_every_kind(tmp_path: Path) -> None:
    grid = tmp_path / "grid"
    grid.mkdir()
    art = _write_all_art(grid, _UNSIGNED)
    bystander = grid / "730p.jpg"  # real Steam appid (< 2³¹)
    bystander.write_bytes(b"cs2")

    count = await delete_artwork_files(str(grid), _UNSIGNED)

    assert count == len(art)
    assert all(not p.exists() for p in art)
    assert bystander.exists()


@pytest.mark.asyncio
async def test_delete_artwork_files_accepts_signed_appid(tmp_path: Path) -> None:
    """Art is named with the unsigned id; a signed (negative) input must
    resolve to the same prefix and still delete it."""
    grid = tmp_path / "grid"
    grid.mkdir()
    art = _write_all_art(grid, _UNSIGNED)
    signed = _UNSIGNED - 0x100000000

    assert await delete_artwork_files(str(grid), signed) == len(art)
    assert all(not p.exists() for p in art)


@pytest.mark.asyncio
async def test_delete_artwork_files_noop_when_absent(tmp_path: Path) -> None:
    grid = tmp_path / "grid"
    grid.mkdir()
    # No files for this appid → "try to delete" yields 0, no error.
    assert await delete_artwork_files(str(grid), _UNSIGNED) == 0
    assert await delete_artwork_files(str(tmp_path / "missing"), _UNSIGNED) == 0


# --- Fix A: artwork cleanup on SHORTCUT_REMOVED -----------------------

@pytest.mark.asyncio
async def test_on_shortcut_removed_deletes_artwork(tmp_path: Path) -> None:
    grid = tmp_path / "grid"
    grid.mkdir()
    art = _write_all_art(grid, _UNSIGNED)
    stub = SimpleNamespace(_grid_dir=str(grid))

    await _EventHandlersMixin._on_shortcut_removed(stub, app_id=_UNSIGNED)

    assert all(not p.exists() for p in art)


@pytest.mark.asyncio
async def test_on_shortcut_removed_suppressed_during_bulk(tmp_path: Path) -> None:
    """Bulk 'delete all data' sets a flag and sweeps the grid itself, so
    the per-game handler must skip (no redundant per-shortcut globbing)."""
    grid = tmp_path / "grid"
    grid.mkdir()
    art = _write_all_art(grid, _UNSIGNED)
    stub = SimpleNamespace(_grid_dir=str(grid), _suppress_removal_cleanup=True)

    await _EventHandlersMixin._on_shortcut_removed(stub, app_id=_UNSIGNED)

    assert all(p.exists() for p in art)  # untouched — sweep will handle it


@pytest.mark.asyncio
async def test_on_shortcut_removed_ignores_bad_payload(tmp_path: Path) -> None:
    grid = tmp_path / "grid"
    grid.mkdir()
    art = _write_all_art(grid, _UNSIGNED)
    stub = SimpleNamespace(_grid_dir=str(grid))

    # Missing / non-int app_id must be a no-op (no crash, art untouched).
    await _EventHandlersMixin._on_shortcut_removed(stub)
    await _EventHandlersMixin._on_shortcut_removed(stub, app_id="oops")
    assert all(p.exists() for p in art)


# --- Fix B: full-delete sweep (all non-Steam art except keep set) -----

@pytest.mark.asyncio
async def test_delete_nonsteam_artwork_wipes_orphans_keeps_others(
    tmp_path: Path,
) -> None:
    grid = tmp_path / "grid"
    grid.mkdir()
    unifideck = _write_all_art(grid, _UNSIGNED)          # our current art
    orphan = _write_all_art(grid, 0x80000000 + 99999)    # no shortcut left
    foreign_unsigned = 0x80000000 + 555                  # a Heroic shortcut
    foreign = _write_all_art(grid, foreign_unsigned)
    steam = grid / "730p.jpg"                            # real Steam art
    steam.write_bytes(b"cs2")

    m = _grid_mixin(grid)
    count = await m._delete_nonsteam_artwork(keep_appids={foreign_unsigned})

    # Unifideck + orphan art gone; foreign + Steam art preserved.
    assert count == len(unifideck) + len(orphan)
    assert all(not p.exists() for p in unifideck)
    assert all(not p.exists() for p in orphan)
    assert all(p.exists() for p in foreign)
    assert steam.exists()


@pytest.mark.asyncio
async def test_delete_nonsteam_artwork_noop_without_grid() -> None:
    m = _mixin(services=SimpleNamespace(artwork=None))
    assert await m._delete_nonsteam_artwork(keep_appids=set()) == 0


_CLEANUP_LAUNCHER = "/home/deck/homebrew/plugins/Unifideck/bin/unifideck-launcher"


def _cleanup_shortcuts() -> dict:
    """One of ours plus two of the user's, including the trap case.

    Entry "2" is the shape that made this dangerous: NonSteamLaunchers
    writes a ``battlenet:<id>`` token into LaunchOptions, and 0.7.4 added
    ``battlenet`` to ``STORE_ID_PATTERN`` — so a LaunchOptions-only
    ownership test claims it as ours and "Delete all Unifideck data"
    removes the user's Battle.net shortcut and sweeps its artwork.
    """
    from unifideck.services.shortcut.games_map import UNIFIDECK_TAG

    return {
        "shortcuts": {
            # Foreign, no Unifideck signals at all.
            "0": {"appid": -11936521, "tags": {"0": "Heroic"},
                  "Exe": '"/usr/bin/heroic"', "LaunchOptions": ""},
            # Ours: Exe is the launcher.
            "1": {"appid": -1379918704, "tags": {"0": UNIFIDECK_TAG},
                  "Exe": f'"{_CLEANUP_LAUNCHER}"',
                  "LaunchOptions": "amazon:amzn1.adg.product.x"},
            # Foreign, but carries a store token AND our tag.
            "2": {"appid": -55555555, "tags": {"0": UNIFIDECK_TAG},
                  "Exe": '"/home/deck/NonSteamLaunchers/Battle.net.sh"',
                  "LaunchOptions": "battlenet:s1"},
        },
    }


def test_nonunifideck_unsigned_appids_filters_owned() -> None:
    """The artwork keep-set holds every shortcut that isn't ours."""
    svc = SimpleNamespace(
        _shortcuts=_cleanup_shortcuts(), _launcher_path=_CLEANUP_LAUNCHER,
    )
    keep = SyncRPCMixin._nonunifideck_unsigned_appids(svc)

    assert keep == {
        (-11936521) + 0x100000000,
        (-55555555) + 0x100000000,
    }


def test_cleanup_never_collects_a_foreign_shortcut_for_deletion() -> None:
    """UD-006 class: only launcher-Exe entries are deletion candidates.

    Guards the 0.7.4 regression specifically — a NonSteamLaunchers
    ``battlenet:`` row must survive "Delete all Unifideck data" even
    though it matches both the LaunchOptions pattern and our tag.
    """
    svc = SimpleNamespace(
        _shortcuts=_cleanup_shortcuts(), _launcher_path=_CLEANUP_LAUNCHER,
    )
    ids = SyncRPCMixin._collect_ids_from_shortcuts_vdf(svc)

    assert ids == {-1379918704}
    assert -55555555 not in ids
    assert -11936521 not in ids


@pytest.mark.asyncio
async def test_microsoft_tokens_legacy_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
    from unifideck.stores.microsoft.tokens.persistence import PersistenceMixin
    from unifideck.security import SecureTokenStore

    monkeypatch.setenv("HOME", str(tmp_path))

    legacy_file = tmp_path / ".local/share/unifideck/microsoft_tokens.json"
    new_file = tmp_path / ".config/unifideck/microsoft_tokens.json"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "access_token": "mock_access",
        "refresh_token": "mock_refresh",
        "saved_at": 12345.0,
    }
    legacy_file.write_text(json.dumps(payload))

    config = MicrosoftConfig(token_file=str(new_file))
    secure_store = SecureTokenStore()

    pm = PersistenceMixin()
    pm._config = config
    pm._secure_store = secure_store
    pm._bus = None
    pm._ms_access_token = None
    pm._ms_refresh_token = None
    pm._token_saved_at = 0.0

    loaded = await pm.load()

    assert loaded is True
    assert pm._ms_access_token == "mock_access"
    assert pm._ms_refresh_token == "mock_refresh"
    assert new_file.exists()
    assert not legacy_file.exists()


# --------------------------------------------------------------------------
# Live in-process state: the reset that "Delete all data" was missing
# --------------------------------------------------------------------------
class _CacheHost(_SyncCacheMixin):
    """Minimal ``SyncService`` stand-in owning just the cache round trip."""

    def __init__(self) -> None:
        self._config = None
        self._all_games: dict[str, list[Game]] = {}
        self._last_sync_time: float | None = None
        self._store_sync_times: dict[str, float] = {}


def _installed_game(store: str, game_id: str, app_id: int) -> Game:
    return Game(
        app_id=app_id,
        store=store,
        store_game_id=game_id,
        title=game_id,
        installed=True,
        install_path=f"/run/media/deck/SD/Games/{game_id}",
        exe_path=f"/run/media/deck/SD/Games/{game_id}/game.exe",
    )


def test_reset_library_state_clears_memory_and_cache_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    host = _CacheHost()
    host._all_games = {"gog": [_installed_game("gog", "1434021265", -209853636)]}
    host._last_sync_time = 1234.0
    host._save_library_cache()
    cache_file = host._get_library_cache_path()
    assert cache_file.is_file()

    host.reset_library_state()

    assert host._all_games == {}
    assert host._last_sync_time is None
    assert host._store_sync_times == {}
    assert not cache_file.exists()


def test_save_after_reset_cannot_resurrect_the_wiped_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The actual bug: the wipe deleted the file, memory rewrote it.

    ``_save_library_cache`` fires on any finalize or install-state flip, so
    a wipe that cleared only the file put all 245 games — two of them still
    flagged installed — straight back on disk.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    host = _CacheHost()
    host._all_games = {"gog": [_installed_game("gog", "1434021265", -209853636)]}
    host._last_sync_time = 1234.0

    host.reset_library_state()
    host._save_library_cache()

    payload = json.loads(host._get_library_cache_path().read_text())
    assert payload == {
        "last_sync_time": None, "store_sync_times": {}, "libraries": {},
    }


class _RecordingBus:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: str, **kwargs: Any) -> None:
        self.emitted.append((event, kwargs))


class _FakeSync:
    """Records the order the finalize step touches it in."""

    def __init__(self, games: list[Game], calls: list[str]) -> None:
        self._games = games
        self._calls = calls

    def get_all_games(self) -> list[Game]:
        self._calls.append("get_all_games")
        return self._games

    def reset_library_state(self) -> None:
        self._calls.append("reset_library_state")
        self._games = []


@pytest.mark.asyncio
async def test_finalize_wipe_resets_before_announcing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Announce-after-reset, or SyncService re-saves the wiped cache.

    Its ``SHORTCUT_INSTALL_STATE_CHANGED`` handler calls
    ``_save_library_cache()`` on a match, so emitting while the library is
    still populated would put it right back on disk.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[str] = []
    bus = _RecordingBus()
    sync = _FakeSync([_installed_game("gog", "1434021265", -209853636)], calls)

    async def _emit(event: str, **kwargs: Any) -> None:
        calls.append("emit")
        bus.emitted.append((event, kwargs))

    bus.emit = _emit  # type: ignore[method-assign]
    m = _mixin(sync_service=sync, bus=bus, cache=None)

    await m._finalize_wipe(delete_files=True)

    assert calls == ["get_all_games", "reset_library_state", "emit"]


@pytest.mark.asyncio
async def test_finalize_wipe_announces_every_cleared_game(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    bus = _RecordingBus()
    not_installed = Game(
        app_id=-1, store="epic", store_game_id="Potoo", title="Overcooked 2",
    )
    sync = _FakeSync(
        [
            _installed_game("gog", "1434021265", -209853636),
            _installed_game("battlenet", "w3_legacy", -127585543),
            not_installed,
        ],
        [],
    )
    m = _mixin(sync_service=sync, bus=bus, cache=None)

    await m._finalize_wipe(delete_files=True)

    assert len(bus.emitted) == 2          # the not-installed game is skipped
    names = {e for e, _ in bus.emitted}
    assert names == {Events.SHORTCUT_INSTALL_STATE_CHANGED}
    payloads = {kw["store_game_id"]: kw for _, kw in bus.emitted}
    assert set(payloads) == {"1434021265", "w3_legacy"}
    gog = payloads["1434021265"]
    # Exactly the kwargs the event's schema declares, matching the payload
    # ``ShortcutService.mark_uninstalled`` emits.
    assert set(gog) == {
        "store", "store_game_id", "app_id", "installed", "exe_path",
        "install_path",
    }
    assert gog["installed"] is False
    assert gog["app_id"] == -209853636
    assert gog["exe_path"] == ""
    assert gog["install_path"] == ""


@pytest.mark.asyncio
async def test_finalize_wipe_clears_the_installed_disk_info_memo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The 300 s memo served "13.36 GB · External" for a deleted directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    installed_disk_info._memo[("gog", "1434021265", "/sd/Shadow Warrior 2")] = (
        0.0, {"size_bytes": 14349000000, "location": "external"},
    )
    m = _mixin(sync_service=_FakeSync([], []), bus=_RecordingBus(), cache=None)

    await m._finalize_wipe(delete_files=True)

    assert installed_disk_info._memo == {}


@pytest.mark.asyncio
async def test_finalize_wipe_is_best_effort_without_collaborators(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A wipe that already succeeded must not report failure."""
    monkeypatch.setenv("HOME", str(tmp_path))
    m = _mixin()
    assert await m._finalize_wipe(delete_files=True) == 0


@pytest.mark.asyncio
async def test_finalize_wipe_never_raises_when_sync_service_blows_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    class _Exploding:
        def get_all_games(self) -> list[Game]:
            raise RuntimeError("boom")

        def reset_library_state(self) -> None:
            raise RuntimeError("boom")

    m = _mixin(sync_service=_Exploding(), bus=_RecordingBus(), cache=None)

    assert await m._finalize_wipe(delete_files=True) == 0
