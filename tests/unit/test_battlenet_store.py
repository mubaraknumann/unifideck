"""The Battle.net store: contract, library join, and non-destructive logout.

The join tested here is the one that silently broke first: install state is
keyed on the product CODE (``hsb``) while the catalog addresses titles by
uid (``hs_beta``). Matching on code reports every installed game as not
installed, and the library still looks plausible — 22 titles, all named,
none playable.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from _wine_session import token_of, write_registry

from unifideck.launcher import wrapper_session as ws
from unifideck.launcher.wrapper_stores import is_wrapper_store
from unifideck.stores.battlenet import BattlenetStore
from unifideck.stores.battlenet import paths as bpaths
from unifideck.stores.battlenet.library import build_library
from unifideck.stores.battlenet.ownership import (
    AccountFacts,
    InstalledGame,
    merge_fragments,
)
from unifideck.stores.battlenet.prefix import MARKER_FILENAME
from unifideck.stores.shared import prefix_clone as pc
from unifideck.stores.shared.store_base import StoreBase
from unifideck.core.store_capabilities import capability_flags

FIXTURES = Path(__file__).parent.parent / "fixtures" / "battlenet"
LAUNCHER = "/plugin/bin/unifideck-launcher"


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[Any, dict]] = []
        self.subscriptions: list[str] = []

    async def emit(self, event: Any, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def on(self, event: Any, handler: Any) -> None:
        """Record what ``auto_wire`` subscribed.

        Without this the bus silently absorbed the subscription, so a store
        that stopped wiring its session capture would still pass every test.
        """
        self.subscriptions.append(getattr(event, "value", str(event)))


class _Cache:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    def get(self, *_a: Any, **_k: Any) -> None:
        return None

    def clear(self, name: str) -> None:
        self.cleared.append(name)


class _Config:
    def __init__(self, data_dir: Path, prefixes_dir: Path) -> None:
        self._values = {
            "data_dir": str(data_dir),
            "prefixes_dir": str(prefixes_dir),
            # Isolated: the real cache may hold a genuine installer, which
            # would make this test depend on the developer's machine.
            "installer_cache_dir": str(data_dir / "installer-cache"),
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._values if key == "stores.battlenet" else default


@pytest.fixture
def store(tmp_path: Path) -> BattlenetStore:
    prefixes = tmp_path / "prefixes"
    prefixes.mkdir(parents=True)
    return BattlenetStore(
        _Bus(), _Cache(), plugin_dir="/plugin",
        config=_Config(tmp_path, prefixes),
    )


def _sign_in(store: BattlenetStore, licences: list[int]) -> Path:
    """Create an auth prefix carrying a licence ledger."""
    prefix = store.prefixes.auth_prefix
    drive_c = prefix / "drive_c"
    db = drive_c / "users/steamuser/AppData/Local/Battle.net/CachedData.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE key_value_store (key TEXT, value TEXT)")
    con.execute("CREATE TABLE login_cache (battle_tag TEXT)")
    con.execute(
        "INSERT INTO key_value_store VALUES ('features_cached_data_points', ?)",
        (json.dumps({"licenses": licences, "account_id": 1}),),
    )
    con.commit()
    con.close()
    return prefix


# --------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------


def test_satisfies_the_storebase_contract() -> None:
    assert issubclass(BattlenetStore, StoreBase)
    assert not inspect.isabstract(BattlenetStore)


def test_store_info_declares_a_wine_wrapper_store() -> None:
    info = BattlenetStore.store_info
    assert info.name == "battlenet"
    assert info.supports_install is True
    # No cloud-save strategy exists: Blizzard progress is server-side.
    # Asserted against the derived capability rather than a StoreInfo field.
    # It WAS a field, and Battle.net was the only store that ever set it —
    # so GOG and Epic, the two stores that do have cloud saves, both took the
    # `False` default and advertised that they had none. The field is gone;
    # a re-added literal now raises TypeError (audit register item 26).
    assert capability_flags("battlenet")["supports_cloud_saves"] is False
    # Wrapper-ness is NOT declared here. It is owned by WRAPPER_STORES and
    # derived into the payload by get_store_infos (audit §3.1), so asserting
    # it off the descriptor would restore the second copy this removed.
    assert not hasattr(info, "uses_wine")
    assert is_wrapper_store(info.name)


def test_module_layout_is_auto_discoverable() -> None:
    """``stores/<name>/store.py`` needs no registry edit."""
    assert BattlenetStore.__module__ == "unifideck.stores.battlenet.store"


def test_get_installed_path_is_async_like_the_base() -> None:
    assert inspect.iscoroutinefunction(BattlenetStore.get_installed_path)


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------


def test_not_available_without_a_client_prefix(store: BattlenetStore) -> None:
    assert asyncio.run(store.is_available()) is False


def test_available_once_the_client_holds_a_licence_ledger(store: BattlenetStore) -> None:
    _sign_in(store, [1, 2, 3])
    assert asyncio.run(store.is_available()) is True


def test_unknown_library_without_a_prefix_not_an_empty_one(
    store: BattlenetStore,
) -> None:
    """``None``, not ``[]`` — the distinction is a whole library.

    Every fact this store's library is built from lives in the client's
    Wine prefix, so "no prefix" means *we don't know what you own*. The
    sync layer treats ``[]`` as authoritative and lets the shortcut
    reconcile delete every Battle.net shortcut the user has; ``None``
    arrives as ``library_unreadable`` and keeps them. This assertion used
    to read ``== []`` and pinned the defect (audit §3.5, finding B).
    """
    assert asyncio.run(store.get_library()) is None


def test_unknown_library_when_the_catalog_cache_is_empty(
    store: BattlenetStore,
) -> None:
    """Signed in, but the client has never populated its PUB catalog.

    Every ownership rule is keyed on that catalog, so without it the
    build can only produce an empty list — which downstream reads as
    "you own nothing" and sweeps the shortcuts.
    """
    _sign_in(store, [1, 2, 3])
    assert asyncio.run(store.get_library()) is None


def test_start_auth_does_not_install_the_client_itself(
    store: BattlenetStore,
) -> None:
    """The install moved behind RunGame — see the launcher handler.

    ``AuthDispatcher.kickAndLaunch`` awaits this RPC before RunGame-ing the
    auth shortcut, so installing here blocked the launcher from ever
    starting. Measured: the wizard opened with no gamescope session, the RPC
    never returned, and Sign In did nothing.
    """
    result = asyncio.run(store.start_auth())

    assert result.success is True
    assert result.metadata["pending"] is True
    assert result.metadata["needs_bootstrap"] is True
    assert not store.prefixes.auth_prefix.exists(), (
        "start_auth must not build the prefix"
    )


def test_start_auth_reports_no_bootstrap_needed_once_the_client_exists(
    store: BattlenetStore,
) -> None:
    prefix = store.prefixes.auth_prefix
    client = prefix / "drive_c" / bpaths.CLIENT_DIR
    client.mkdir(parents=True)
    (client / bpaths.CLIENT_EXE).write_bytes(b"MZ")
    (client / bpaths.LAUNCHER_EXE).write_bytes(b"MZ")
    build = client / "Battle.net.17651"
    build.mkdir()
    (build / bpaths.CLIENT_DLL).write_bytes(b"MZ")

    result = asyncio.run(store.start_auth())

    assert result.metadata["needs_bootstrap"] is False


def test_start_auth_still_needs_bootstrap_when_only_the_shim_is_there(
    store: BattlenetStore,
) -> None:
    """An interrupted install leaves the shim; sign-in must still repair it."""
    prefix = store.prefixes.auth_prefix
    client = prefix / "drive_c" / bpaths.CLIENT_DIR
    client.mkdir(parents=True)
    (client / bpaths.CLIENT_EXE).write_bytes(b"MZ")
    (client / bpaths.LAUNCHER_EXE).write_bytes(b"MZ")

    result = asyncio.run(store.start_auth())

    assert result.metadata["needs_bootstrap"] is True


def test_start_auth_does_not_clear_the_signed_out_marker(store: BattlenetStore) -> None:
    """The marker persists until the monitor confirms a completed sign-in.

    Clearing it up front and then timing out would leave the store reporting
    "available" when no sign-in happened — the marker gone but the previous
    session's licence ledger still present. The monitor's ``on_captured``
    hook clears it once the session actually lands.
    """
    asyncio.run(store.logout())
    assert store._signed_out_marker.exists()

    asyncio.run(store.start_auth())

    assert store._signed_out_marker.exists()
    asyncio.run(store._on_auth_captured())
    assert not store._signed_out_marker.exists()


# --------------------------------------------------------------------------
# the library join
# --------------------------------------------------------------------------


def _catalog():
    return merge_fragments(iter([json.loads((FIXTURES / "pub_catalog_fragment.json").read_bytes())]))


def test_installed_state_joins_on_uid_not_product_code() -> None:
    """The regression: 'hsb' vs 'hs_beta' made every game look uninstalled."""
    catalog = _catalog()
    entry = catalog.entry_for("ARK")
    uid = entry.uid_for()
    installed = {
        # Keyed by CODE, as aggregate.json/product.db are.
        "arkcode": InstalledGame(code="arkcode", uid=uid, name="ARK", is_ready=True),
    }
    games = build_library(
        catalog,
        AccountFacts(licence_ids=frozenset({1105059})),
        installed,
        launcher_path=LAUNCHER,
    )
    ark = next(g for g in games if g.store_game_id == uid)
    assert ark.installed is True


def test_installed_entry_without_a_uid_does_not_join_a_catalog_title() -> None:
    """No uid means no join key — marking ARK installed would be a guess.

    It still surfaces separately under its own code, because losing an
    installed game is worse than showing it unmatched.
    """
    catalog = _catalog()
    installed = {"arkcode": InstalledGame(code="arkcode", uid=None, is_ready=True)}
    games = build_library(
        catalog, AccountFacts(licence_ids=frozenset({1105059})), installed,
        launcher_path=LAUNCHER,
    )
    ark = next(g for g in games if g.store_game_id == "ark")
    assert ark.installed is False
    orphan = next(g for g in games if g.store_game_id == "arkcode")
    assert orphan.installed is True


def test_library_is_keyed_on_uid_so_family_renames_cannot_orphan_shortcuts() -> None:
    """Blizzard renamed Diablo IV D4 -> Fen; uids never change."""
    catalog = _catalog()
    games = build_library(
        catalog, AccountFacts(licence_ids=frozenset({1105059})), {},
        launcher_path=LAUNCHER,
    )
    ark = next(g for g in games if g.title == "The Outer Worlds 2")
    assert ark.store_game_id == "ark"
    assert ark.metadata["family"] == "ARK"


def test_app_id_is_derived_from_the_uid() -> None:
    from unifideck.services.shortcut.games_map import generate_app_id

    catalog = _catalog()
    games = build_library(
        catalog, AccountFacts(licence_ids=frozenset({1105059})), {},
        launcher_path=LAUNCHER,
    )
    ark = next(g for g in games if g.store_game_id == "ark")
    assert ark.app_id == generate_app_id(LAUNCHER, "battlenet:ark")


def test_an_installed_game_the_rules_did_not_grant_is_kept() -> None:
    """An ownership hiccup must not make an installed game disappear."""
    games = build_library(
        _catalog(),
        AccountFacts(),
        {"zzz": InstalledGame(code="zzz", uid="zzz", name="Mystery", is_ready=True)},
        launcher_path=LAUNCHER,
    )
    assert [g.store_game_id for g in games] == ["zzz"]
    assert games[0].installed is True


def test_mid_download_titles_are_not_reported_installed() -> None:
    games = build_library(
        _catalog(),
        AccountFacts(licence_ids=frozenset({1105059})),
        {"arkcode": InstalledGame(code="arkcode", uid="ark", is_ready=False)},
        launcher_path=LAUNCHER,
    )
    assert next(g for g in games if g.store_game_id == "ark").installed is False


def test_free_to_play_and_handheld_status_become_tags() -> None:
    config = {
        "WTCG": {"run_each_rule": [{
            "match": {"game_account": {"program_id": "WTCG"}},
            "actions": [
                {"add_product": {"product_id": {"id": "WTCG", "type": "retail"}}},
                {"add_tag": {"name": "play_for_free"}},
            ],
        }]},
    }
    catalog = merge_fragments(iter([{
        "fragment_id": "hs",
        "program_configuration": config,
        "products": [{"id": "WTCG", "base": {
            "program_id": "WTCG", "name": "hs#N",
            "handheld_status": ["handheld_unsupported"],
            "types": {"retail": {"uid": "hs_beta"}}}}],
        "strings": {"default": {"hs#N": "Hearthstone"}},
    }]))
    games = build_library(
        catalog, AccountFacts(game_account_programs=frozenset({"WTCG"})), {},
        launcher_path=LAUNCHER,
    )
    assert set(games[0].tags) == {"free_to_play", "handheld_unsupported"}
    assert games[0].title == "Hearthstone"


def test_titles_without_an_install_uid_are_skipped() -> None:
    """A tile that cannot be installed or launched is a dead tile."""
    catalog = merge_fragments(iter([{
        "fragment_id": "x",
        "program_configuration": {"X": {"run_each_rule": [{
            "match": {"license_id": [7]},
            "actions": [{"add_product": {"product_id": {"id": "X", "type": "retail"}}}]}]}},
        "products": [{"id": "X", "base": {"program_id": "X"}}],
    }]))
    assert build_library(
        catalog, AccountFacts(licence_ids=frozenset({7})), {}, launcher_path=LAUNCHER,
    ) == []


# --------------------------------------------------------------------------
# destructive-operation guards
# --------------------------------------------------------------------------


def test_logout_never_touches_a_prefix(store: BattlenetStore) -> None:
    """Opposite of Ubisoft: here the prefix holds the game."""
    prefix = _sign_in(store, [1])
    result = asyncio.run(store.logout())
    assert result.success is True
    assert prefix.is_dir()
    assert bpaths.drive_c(prefix) is not None


def test_uninstall_refuses_when_no_prefix_was_recorded(store: BattlenetStore) -> None:
    result = asyncio.run(store.uninstall_game("wow"))
    assert result.success is False
    assert result.error_code == "prefix_unknown"


def test_uninstall_refuses_a_prefix_we_did_not_create(store: BattlenetStore, tmp_path: Path) -> None:
    stranger = tmp_path / "someone-elses-prefix"
    (stranger / "drive_c").mkdir(parents=True)
    store.id_map.merge("wow", prefix_path=str(stranger))
    result = asyncio.run(store.uninstall_game("wow"))
    assert result.success is False
    assert result.error_code == "prefix_not_owned"
    assert stranger.is_dir()


def test_install_refuses_when_the_family_code_is_unknown(store: BattlenetStore) -> None:
    """A missing family fails the install rather than the launch.

    ``--exec`` needs it, and Battle.net's response to a missing or obsolete
    family is silent — the client opens and nothing happens. Refusing here
    is the only place the user can be told to re-sync.
    """
    result = asyncio.run(store.install_game("wow"))
    assert result.success is False
    assert result.error_code == "family_unknown"


def test_install_refuses_when_not_signed_in(store: BattlenetStore) -> None:
    """No auth prefix means no session for the game prefix to inherit."""
    store.id_map.merge("wow", family="WoW")
    result = asyncio.run(store.install_game("wow"))
    assert result.success is False
    assert result.error_code == "not_signed_in"


def test_check_for_updates_reports_nothing_rather_than_guessing(store: BattlenetStore) -> None:
    assert asyncio.run(store.check_for_updates()) == []


# --------------------------------------------------------------------------
# family codes reach the id map
# --------------------------------------------------------------------------


def test_a_library_read_records_every_family_code(
    store: BattlenetStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync is the writer that makes a title launchable before installing.

    The launcher runs out-of-process and cannot reach the catalog, so a
    family it is never told is one it can never use — and Battle.net's
    failure for a missing family is silent. Before this, the id map was
    never written at all and every launch aborted ``battlenetFamilyMissing``.
    """
    _sign_in(store, [1105059])
    monkeypatch.setattr(
        "unifideck.stores.battlenet.library.read_catalog", lambda _dc: _catalog(),
    )

    games = asyncio.run(store.get_library())

    assert games, "fixture account should own something"
    for game in games:
        family = game.metadata.get("family")
        if not family:
            continue
        assert store.id_map.resolve_family(game.store_game_id) == family


def test_recorded_families_are_readable_by_the_launcher(
    store: BattlenetStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross the process boundary the real launcher crosses."""
    from unifideck.launcher.proton.handlers import battlenet_client as client

    _sign_in(store, [1105059])
    monkeypatch.setattr(
        "unifideck.stores.battlenet.library.read_catalog", lambda _dc: _catalog(),
    )
    monkeypatch.setattr(client, "id_map_path", lambda p=store.id_map.path: p)

    games = asyncio.run(store.get_library())
    launchable = next(g for g in games if g.metadata.get("family"))

    assert client.resolve_family(launchable.store_game_id) == (
        launchable.metadata["family"]
    )


def test_install_resolves_a_family_from_the_catalog_when_sync_has_not_run(
    store: BattlenetStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installing without a recent sync must not dead-end.

    ``install_game`` refuses on an unknown family rather than handing the
    launcher a game it cannot start, so it has to be able to resolve one
    itself — otherwise a fresh profile can never install anything.
    """
    _sign_in(store, [1105059])
    monkeypatch.setattr(
        "unifideck.stores.battlenet.install.read_catalog", lambda _dc: _catalog(),
    )
    uid = _catalog().entry_for("ARK").uid_for()
    assert store.id_map.resolve_family(uid) is None

    # Fails later, on the missing client — but past the family gate,
    # which is what this pins.
    result = asyncio.run(store.install_game(uid))

    assert result.error_code == "not_signed_in"
    assert store.id_map.resolve_family(uid) == "ARK"


# --------------------------------------------------------------------------
# session lifecycle
# --------------------------------------------------------------------------
#
# Battle.net shipped the three prefix tiers without the lifecycle that keeps
# them current. Measured on-device 2026-08-11: `.bnet-auth` and `.template`
# byte-identical and frozen at 08:57, while the game prefix's client had
# rewritten every session file at 21:15 — twelve hours of token rotation that
# never came back, so the user saw BLZBNTBGS80000023 on every install.


def _write_session(
    prefix: Path, *, mtime: float, vault: bytes = b"vault", token: str = "tok",
) -> None:
    """Put a signed-in session into ``prefix`` at a fixed mtime.

    Includes the registry token: the login token is a Wine registry key, so a
    files-only prefix reads as signed OUT however complete its AppData looks.
    """
    write_registry(prefix, stamp=int(mtime), token=token)
    local = prefix / "drive_c/users/steamuser/AppData/Local/Battle.net"
    vault_path = local / "Account/309859116/account.db"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_bytes(vault)
    os.utime(vault_path, (mtime, mtime))
    config = prefix / "drive_c/users/steamuser/AppData/Roaming/Battle.net/Battle.net.config"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"Client": {"GaClientId": "GUID-A"}}))


def _owned_prefix(store: BattlenetStore, path: Path) -> Path:
    """A prefix marked as ours, so the destructive guards allow it."""
    (path / "drive_c").mkdir(parents=True, exist_ok=True)
    pc.write_marker(
        path, MARKER_FILENAME,
        pc.PrefixMarker(store="battlenet", created_at=1.0),
    )
    return path


def test_the_launcher_is_told_where_the_shared_prefixes_are(
    store: BattlenetStore,
) -> None:
    """The launcher runs under the system Python and cannot read our config.

    ``prefixes_dir`` is user-configurable, so a path it is never told is a
    path it can never use — the same reason family codes go to the id map.
    """
    assert ws.auth_prefix("battlenet") == store.prefixes.auth_prefix
    assert ws.template_prefix("battlenet") == store.prefixes.template_prefix


def test_game_stopped_captures_the_rotated_session(store: BattlenetStore) -> None:
    """The one hook that always fires.

    A launch runs in the launcher subprocess, which the Steam stop button and
    the QAM "X" both SIGKILL, so its own capture cannot be relied on.
    """
    _write_session(
        store.prefixes.auth_prefix, mtime=1000.0, vault=b"stale", token="stale",
    )
    game = _owned_prefix(store, store.prefixes.game_prefix("osi"))
    _write_session(game, mtime=2000.0, vault=b"rotated", token="rotated")
    store.id_map.merge("osi", prefix_path=str(game))

    asyncio.run(store._capture_wrapper_session_on_stop(store="battlenet", game_id="osi"))
    assert token_of(store.prefixes.auth_prefix) == "rotated"

    vault = store.prefixes.auth_prefix / (
        "drive_c/users/steamuser/AppData/Local/Battle.net/Account/309859116/account.db"
    )
    assert vault.read_bytes() == b"rotated"


def test_game_stopped_ignores_other_stores(store: BattlenetStore) -> None:
    _write_session(store.prefixes.auth_prefix, mtime=1000.0, vault=b"stale")
    game = _owned_prefix(store, store.prefixes.game_prefix("osi"))
    _write_session(game, mtime=2000.0, vault=b"rotated")
    store.id_map.merge("osi", prefix_path=str(game))

    asyncio.run(store._capture_wrapper_session_on_stop(store="ubisoft", game_id="osi"))

    vault = store.prefixes.auth_prefix / (
        "drive_c/users/steamuser/AppData/Local/Battle.net/Account/309859116/account.db"
    )
    assert vault.read_bytes() == b"stale"


def test_uninstall_captures_the_session_before_deleting_the_prefix(
    store: BattlenetStore,
) -> None:
    """The prefix usually holds a NEWER token than auth.

    The vendor rotates on every run, so deleting a played game's prefix
    uncaptured strands auth on a server-stale token and the next install opens
    signed-out. Ubisoft earned this one as a measured incident.
    """
    _write_session(store.prefixes.auth_prefix, mtime=1000.0, vault=b"stale")
    game = _owned_prefix(store, store.prefixes.game_prefix("osi"))
    _write_session(game, mtime=2000.0, vault=b"rotated")
    store.id_map.merge("osi", prefix_path=str(game))

    result = asyncio.run(store.uninstall_game("osi"))

    assert result.success is True
    assert not game.is_dir()
    vault = store.prefixes.auth_prefix / (
        "drive_c/users/steamuser/AppData/Local/Battle.net/Account/309859116/account.db"
    )
    assert vault.read_bytes() == b"rotated"


def test_logout_purges_the_session_from_every_game_prefix(
    store: BattlenetStore,
) -> None:
    """Without this the next launch quietly signs the user back in."""
    _sign_in(store, [1])
    game = _owned_prefix(store, store.prefixes.game_prefix("osi"))
    _write_session(game, mtime=2000.0)
    store.id_map.merge("osi", prefix_path=str(game))

    assert asyncio.run(store.logout()).success is True

    spec = ws.SPECS["battlenet"]
    assert ws.has_session(spec, game) is False


def test_logout_still_leaves_the_games_alone(store: BattlenetStore) -> None:
    """Purging a session must never reach the install inside the prefix."""
    _sign_in(store, [1])
    game = _owned_prefix(store, store.prefixes.game_prefix("osi"))
    _write_session(game, mtime=2000.0)
    payload = game / "drive_c" / "games" / "Overwatch" / "data.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"90GB")
    store.id_map.merge("osi", prefix_path=str(game))

    asyncio.run(store.logout())

    assert payload.exists()
    assert game.is_dir()


def test_the_session_capture_is_actually_subscribed_to_the_bus(
    tmp_path: Path,
) -> None:
    """Pins the wiring, not just the handler body.

    The handler is only reachable because ``__init__`` calls ``auto_wire``. A
    store that dropped that call would keep passing a test that invokes the
    method directly, while in production the rotated token would once again
    never come back.
    """
    prefixes = tmp_path / "prefixes"
    prefixes.mkdir(parents=True)
    bus = _Bus()
    BattlenetStore(
        bus, _Cache(), plugin_dir="/plugin",
        config=_Config(tmp_path, prefixes),
    )
    assert "game_stopped" in bus.subscriptions


# ---------------------------------------------------------------------------
# the sign-in monitor's probe
# ---------------------------------------------------------------------------

_BNET_LOCAL = "drive_c/users/steamuser/AppData/Local/Battle.net"
_ACCOUNT_DB = f"{_BNET_LOCAL}/Account/1234/account.db"
_CLIENT_LOGS = f"{_BNET_LOCAL}/Logs"

# Verbatim from a device log — the sign-in the tester reported.
_SIGNED_OUT_LINE = (
    "E 2026-08-11 08:34:21.914905 [BNLogin] {Main} "
    "Login failed. error=ERROR_TOKEN_NOT_FOUND (49)\n"
)
_SIGNED_IN_LINE = (
    "I 2026-08-10 23:01:59.474197 [BNLogin] {Main} "
    "Logged into Battle.net successfully. |bnet=1:0:1278132c\n"
)


def _place_session(store: BattlenetStore, *, log_body: str) -> None:
    """Give the auth prefix credential material plus a client log."""
    prefix = store.prefixes.auth_prefix
    account = prefix / _ACCOUNT_DB
    account.parent.mkdir(parents=True, exist_ok=True)
    account.write_text("token-material")
    # ``has_session`` also requires a registry section for this store — the
    # token is a registry key, not a file.
    (prefix / "user.reg").write_text(
        "WINE REGISTRY Version 2\n\n"
        "[Software\\\\Blizzard Entertainment\\\\Battle.net\\\\UnifiedAuth] 1786438212\n"
        '"97C2054C"=hex:01,00\n',
    )
    logs = prefix / _CLIENT_LOGS
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "battle.net-1.log").write_text(log_body)


def test_probe_reports_signed_in_once_the_session_lands(store: BattlenetStore) -> None:
    """The success path: new credential material and a log that confirms it."""
    _place_session(store, log_body=_SIGNED_IN_LINE)
    assert asyncio.run(store._auth_session_landed()) is True


def test_probe_does_not_report_a_login_the_user_never_completed(
    store: BattlenetStore,
) -> None:
    """Credential material moved, but the client is still on the login page.

    ``Identity`` and ``EncryptionKey`` survive a failed login, and the client
    rewrites ``account.db`` to remember the account it is asking the user to
    log into — so material alone is not proof. Telling the user they are
    connected when they are staring at a password prompt is the one answer
    worse than saying nothing.
    """
    _place_session(store, log_body=_SIGNED_OUT_LINE)
    assert asyncio.run(store._auth_session_landed()) is False


def test_probe_is_quiet_until_the_material_actually_changes(
    store: BattlenetStore,
) -> None:
    """Re-authenticating over a live session must not resolve instantly."""
    _place_session(store, log_body=_SIGNED_IN_LINE)
    store._auth_baseline = ws.fingerprint(
        ws.spec_for("battlenet"), store.prefixes.auth_prefix,
    )
    assert asyncio.run(store._auth_session_landed()) is False
