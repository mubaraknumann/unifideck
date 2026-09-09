"""Tests for the user-settable launch-executable override.

Covers the three layers of the "Change executable" feature:

* ``ShortcutService.set_executable`` — rewrites ONLY the games.map exe column,
  carrying ``work_dir`` + ``app_id`` over verbatim (the separation that keeps
  cloud saves / the launch CWD intact).
* ``ExecutableRPCMixin`` — list/set/reset behaviour: ground-truth override
  detection, path-traversal rejection, "picking the default = reset", and the
  store split (gog → games.map, epic → config key).
* ``game_fixes.get_exe_override`` — Epic's legendary ``--override-exe`` honors
  the user config override ahead of the curated ``MANUAL_FIXES``.
"""
import asyncio
import json
import os

import pytest

from unifideck.rpc import RpcError
from unifideck.rpc.mixins.executable import (
    ExecutableRPCMixin,
    _is_noise,
    _safe_resolve,
    _scan_executables,
)
from unifideck.services.shortcut.games_map import GameMapEntry
from unifideck.services.shortcut.games_map_mixin import _GamesMapMixin


# ── ShortcutService.set_executable: exe-only, work_dir/app_id preserved ──
class _FakeShortcutHost:
    def __init__(self, games_map):
        self._games_map = games_map
        self.saved = False

    async def _load_games_map(self):
        return None

    async def _save_all(self):
        self.saved = True


def test_set_executable_preserves_work_dir_and_app_id():
    gm = {"gog:123": GameMapEntry(exe="/install/old.exe", work_dir="/install", app_id=-42)}
    host = _FakeShortcutHost(gm)

    ok = asyncio.run(
        _GamesMapMixin.set_executable(host, "gog", "123", "/install/new.exe"),
    )

    assert ok is True
    entry = gm["gog:123"]
    assert entry.exe == "/install/new.exe"   # exe changed
    assert entry.work_dir == "/install"      # work_dir UNTOUCHED
    assert entry.app_id == -42               # app_id preserved
    assert host.saved is True


def test_set_executable_no_row_returns_false():
    host = _FakeShortcutHost({})
    ok = asyncio.run(
        _GamesMapMixin.set_executable(host, "gog", "missing", "/x/y.exe"),
    )
    assert ok is False
    assert host.saved is False


# ── pure helpers ─────────────────────────────────────────────────────
def test_safe_resolve_rejects_escapes(tmp_path):
    inst = tmp_path / "game"
    (inst / "bin").mkdir(parents=True)
    exe = inst / "bin" / "game.exe"
    exe.write_text("")
    assert _safe_resolve(str(inst), "bin/game.exe") == os.path.realpath(str(exe))
    assert _safe_resolve(str(inst), "../escape.exe") is None
    assert _safe_resolve(str(inst), "/etc/passwd") is None
    assert _safe_resolve(str(inst), ".") is None  # the install dir itself


def test_noise_filter():
    assert _is_noise("unins000.exe")
    assert _is_noise("vc_redist.x64.exe")
    assert _is_noise("DXSETUP.exe")
    assert not _is_noise("FalloutNV.exe")
    assert not _is_noise("setup.exe")  # "setup" alone isn't noise; only redist/etc.


def test_scan_executables_filters_and_sorts(tmp_path):
    inst = tmp_path / "game"
    (inst / "config").mkdir(parents=True)
    for rel in ("game.exe", "config/setup.exe", "unins000.exe", "vc_redist.x64.exe"):
        (inst / rel).write_text("")
    rels = _scan_executables(str(inst))
    assert rels == ["config/setup.exe", "game.exe"]


# ── ExecutableRPCMixin ───────────────────────────────────────────────
class _FakeShortcutSvc:
    def __init__(self, entry):
        self._entry = entry
        self.set_calls = []

    async def get_entry_for_game_key(self, store, game_id):
        return self._entry

    async def set_executable(
        self, store, game_id, exe_abs, *, work_dir="", app_id=None,
    ):
        # ``work_dir``/``app_id`` are what the real one builds a MISSING row
        # from, so a game whose row never existed is still fixable from the
        # picker. Recorded here so a test can assert they were supplied.
        self.set_calls.append((store, game_id, exe_abs))
        self.last_row_args = (work_dir, app_id)
        if self._entry is not None:
            self._entry = GameMapEntry(
                exe=exe_abs, work_dir=self._entry.work_dir, app_id=self._entry.app_id,
            )
            return True
        return bool(work_dir and app_id is not None)


class _FakeStore:
    def __init__(self, default_abs):
        self._default_abs = default_abs

    def find_installed_exe(self, install_path, game_id=None):
        return self._default_abs


class _FakeConfig:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def _make_host(tmp_path, store="gog"):
    inst = tmp_path / "game"
    (inst / "config").mkdir(parents=True)
    for rel in ("game.exe", "config/setup.exe", "unins000.exe"):
        (inst / rel).write_text("")
    default_abs = str(inst / "game.exe")
    entry = GameMapEntry(exe=default_abs, work_dir=str(inst), app_id=-1)
    shortcut = _FakeShortcutSvc(entry)

    host = ExecutableRPCMixin()
    host.config = _FakeConfig()
    host.services = type("S", (), {"shortcut": shortcut})()
    host.registry = type("R", (), {"get_store": staticmethod(lambda n: _FakeStore(default_abs))})()
    # Bypass the games.map-file lookup; point at our tmp install dir.
    # Async since the resolver now awaits the store's ``get_installed_path``
    # when games.map has no row (that fallback is what makes the picker
    # usable on a game whose row is missing — see ``_install_dir``).
    async def _install_dir(s: str, g: str) -> str:
        return str(inst)

    host._install_dir = _install_dir  # type: ignore[method-assign]
    return host, shortcut, str(inst)


def test_list_default_and_candidates(tmp_path):
    host, _shortcut, _inst = _make_host(tmp_path)
    out = asyncio.run(host.list_game_executables("gog", "123"))
    rels = {c["rel"]: c for c in out["candidates"]}
    assert set(rels) == {"game.exe", "config/setup.exe"}  # noise filtered
    assert rels["game.exe"]["is_default"] is True
    assert rels["game.exe"]["is_current"] is True         # matches games.map exe
    assert out["override_active"] is False                # current == default


def test_set_gog_writes_games_map_and_marks_override(tmp_path):
    host, shortcut, inst = _make_host(tmp_path)
    res = asyncio.run(host.set_game_executable("gog", "123", "config/setup.exe"))
    assert res["executable"] == "config/setup.exe"
    assert shortcut.set_calls[-1] == (
        "gog", "123", os.path.realpath(os.path.join(inst, "config/setup.exe")),
    )
    # games.map is now the source of truth → override_active reflects it.
    out = asyncio.run(host.list_game_executables("gog", "123"))
    assert out["override_active"] is True
    assert out["current_rel"] == "config/setup.exe"


def test_set_rejects_escape(tmp_path):
    host, _shortcut, _inst = _make_host(tmp_path)
    with pytest.raises(RpcError):
        asyncio.run(host.set_game_executable("gog", "123", "../evil.exe"))


def test_picking_default_is_a_reset(tmp_path):
    host, _shortcut, _inst = _make_host(tmp_path)
    # First set a custom exe, then "pick the default" → reset.
    asyncio.run(host.set_game_executable("gog", "123", "config/setup.exe"))
    res = asyncio.run(host.set_game_executable("gog", "123", "game.exe"))
    assert res["executable"] == "game.exe"   # restored default
    out = asyncio.run(host.list_game_executables("gog", "123"))
    assert out["override_active"] is False


def test_reset_gog_restores_default(tmp_path):
    host, shortcut, inst = _make_host(tmp_path)
    asyncio.run(host.set_game_executable("gog", "123", "config/setup.exe"))
    res = asyncio.run(host.reset_game_executable("gog", "123"))
    assert res["executable"] == "game.exe"
    assert shortcut.set_calls[-1] == ("gog", "123", os.path.realpath(os.path.join(inst, "game.exe")))


def test_epic_uses_config_key_not_games_map(tmp_path):
    host, shortcut, _inst = _make_host(tmp_path, store="epic")
    res = asyncio.run(host.set_game_executable("epic", "eid", "config/setup.exe"))
    assert res["executable"] == "config/setup.exe"
    # Epic writes the config override and does NOT touch games.map.
    assert host.config.get("games.eid.executable") == "config/setup.exe"
    assert shortcut.set_calls == []
    # reset clears the config key.
    asyncio.run(host.reset_game_executable("epic", "eid"))
    assert not host.config.get("games.eid.executable")


# ── game_fixes.get_exe_override: user override beats MANUAL_FIXES ─────
def test_get_exe_override_prefers_user_config(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({"games": {"eid": {"executable": "Bin/Game.exe"}}}))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    # Re-import fresh so no cached config interferes.
    from unifideck.launcher.proton.fixes.game_fixes import get_exe_override

    assert get_exe_override("eid") == "Bin/Game.exe"


def test_get_exe_override_none_without_override(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({"games": {}}))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    from unifideck.launcher.proton.fixes.game_fixes import get_exe_override

    assert get_exe_override("unknown-game-id") is None


# ── The user's choice must outlive a sync ─────────────────────────────
#
# For the direct-launch stores the games.map exe column IS where "Change
# executable" stores the choice, and reconcile rewrites that row from the
# synced game. GOG carries an ``exe_path`` on every library read — a fresh
# disk scan (``exe_key="executable"``) it needs in order to discover installs
# Unifideck did not perform — so a GOG user's pick was silently reverted on
# the next sync. Amazon and Epic were unaffected only because neither carries
# an exe at all, which is what kept the bug invisible: the guard in
# ``_update_games_map_row`` was written on the docstring's claim that *no*
# library-sourced game carries one.
from unifideck.services.shortcut.reconcile_phases import (
    _ReconcilePhasesMixin as _Phases,
)


class _MapHost(_Phases):
    """Just enough of ShortcutService for the row-maintenance phase.

    Subclasses the mixin so ``_update_games_map_row`` can reach its sibling
    ``_durable_exe`` the way the real service does.
    """

    def __init__(self, games_map):
        self._games_map = games_map


def _update_row(games_map, game, key, app_id):
    host = _MapHost(dict(games_map))
    host._update_games_map_row(game, key, app_id)
    return host._games_map


def _installed_game(store, gid, install_path, exe):
    from unifideck.core.types import Game

    return Game(
        app_id=-1, store=store, store_game_id=gid, title="T",
        installed=True, install_path=install_path, exe_path=exe,
    )


def test_a_sync_does_not_revert_the_users_choice(tmp_path):
    """GOG offers its scanned exe; the user picked another that still exists."""
    chosen = tmp_path / "Game.exe"
    chosen.write_bytes(b"x")
    scanned = tmp_path / "Launcher.exe"
    scanned.write_bytes(b"x")

    games_map = {"gog:1": GameMapEntry(
        exe=str(chosen), work_dir=str(tmp_path), app_id=-7,
    )}
    game = _installed_game("gog", "1", str(tmp_path), str(scanned))

    after = _update_row(games_map, game, "gog:1", -7)

    assert after["gog:1"].exe == str(chosen)


def test_a_recorded_exe_that_is_gone_yields_to_the_store(tmp_path):
    """The escape hatch: a moved or replaced install must still refresh."""
    scanned = tmp_path / "Game.exe"
    scanned.write_bytes(b"x")

    games_map = {"gog:1": GameMapEntry(
        exe=str(tmp_path / "old" / "Gone.exe"), work_dir="/old", app_id=-7,
    )}
    game = _installed_game("gog", "1", str(tmp_path), str(scanned))

    after = _update_row(games_map, game, "gog:1", -7)

    assert after["gog:1"].exe == str(scanned)
    assert after["gog:1"].work_dir == str(tmp_path)


def test_a_game_with_no_row_takes_the_stores_exe(tmp_path):
    """First establishment is unaffected — this is how rows are born."""
    scanned = tmp_path / "Game.exe"
    scanned.write_bytes(b"x")
    game = _installed_game("gog", "1", str(tmp_path), str(scanned))

    after = _update_row({}, game, "gog:1", -7)

    assert after["gog:1"].exe == str(scanned)
