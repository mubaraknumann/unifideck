"""Unit tests for the single-source-of-truth active-user resolver.

Covers :mod:`unifideck.steam.current_user` — the module that fixes
"synced N games but Steam shows 0" caused by writing shortcuts.vdf to the
wrong ``userdata/<id>`` folder. The headline regression: a decoy userdata
dir with a newer *directory* mtime must NOT win over the real logged-in
user (identified by registry.vdf / localconfig.vdf), because our own writes
bump the directory mtime and would otherwise lock in the wrong guess.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from unifideck.steam import current_user as cu

# Real values from the reporter's device (see the bug investigation):
#   nightfury782  = steam64 76561198185895782 = account id 225630054  (correct)
#   decoy second account = steam64 76561199860404698 = account id 1900138970
_REAL_STEAM64 = "76561198185895782"
_REAL_ACCOUNT = "225630054"
_DECOY_ACCOUNT = "1900138970"


def _mk_userdata(root: Path, account_id: str, *, localconfig: bool = False) -> Path:
    cfg = root / "userdata" / account_id / "config"
    cfg.mkdir(parents=True)
    if localconfig:
        (cfg / "localconfig.vdf").write_text("x", encoding="utf-8")
    return cfg


def _write_loginusers(root: Path, steam64: str, name: str, *, most_recent: str = "1") -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "loginusers.vdf").write_text(
        '"users"\n{\n'
        f'\t"{steam64}"\n\t{{\n'
        f'\t\t"AccountName"\t\t"{name}"\n'
        f'\t\t"MostRecent"\t\t"{most_recent}"\n'
        '\t\t"Timestamp"\t\t"1784790553"\n'
        '\t}\n}\n',
        encoding="utf-8",
    )


# ── account-id conversion ──────────────────────────────────────────

def test_account_id_from_steam64():
    assert cu.account_id_from_steam64(_REAL_STEAM64) == _REAL_ACCOUNT
    assert cu.account_id_from_steam64("nonsense") is None
    assert cu.account_id_from_steam64("") is None


# ── the headline regression: localconfig beats a newer directory mtime ──

def test_localconfig_mtime_beats_newer_directory_mtime(tmp_path):
    """Real user (fresh localconfig) wins over a decoy with newer dir mtime."""
    _mk_userdata(tmp_path, _REAL_ACCOUNT, localconfig=True)
    _mk_userdata(tmp_path, _DECOY_ACCOUNT, localconfig=False)
    # Make the DECOY the most-recently-touched *directory* (the trap).
    time.sleep(0.02)
    os.utime(tmp_path / "userdata" / _DECOY_ACCOUNT, None)

    assert cu._from_localconfig_mtime(tmp_path) == _REAL_ACCOUNT
    # And prove the OLD heuristic would have picked the decoy.
    assert cu._from_dir_mtime(tmp_path) == _DECOY_ACCOUNT


def test_loginusers_most_recent_resolves(tmp_path):
    _mk_userdata(tmp_path, _REAL_ACCOUNT)
    _write_loginusers(tmp_path, _REAL_STEAM64, "nightfury782")
    assert cu._from_loginusers(tmp_path) == _REAL_ACCOUNT


def test_registry_autologin_maps_name_to_account(tmp_path, monkeypatch):
    _mk_userdata(tmp_path, _REAL_ACCOUNT)
    _write_loginusers(tmp_path, _REAL_STEAM64, "nightfury782", most_recent="0")
    reg = tmp_path / "registry.vdf"
    reg.write_text('"Registry"\n{\n\t"AutoLoginUser"\t\t"nightfury782"\n}\n', encoding="utf-8")
    # Point the module's registry probe at our temp file.
    monkeypatch.setattr(cu, "_REGISTRY_CANDIDATES", (str(reg),))
    assert cu._from_registry_autologin(tmp_path) == _REAL_ACCOUNT


# ── resolution order + fail-safe ───────────────────────────────────

def test_resolve_prefers_persisted_config(tmp_path):
    _mk_userdata(tmp_path, _REAL_ACCOUNT)
    _mk_userdata(tmp_path, _DECOY_ACCOUNT, localconfig=True)  # would win on mtime

    class _Cfg:
        def get(self, key, default=None):
            return _REAL_ACCOUNT if key == cu.CONFIG_ACTIVE_USER_KEY else default

    assert cu.resolve(tmp_path, _Cfg()) == _REAL_ACCOUNT


def test_resolve_ignores_invalid_persisted_and_falls_through(tmp_path):
    _mk_userdata(tmp_path, _REAL_ACCOUNT, localconfig=True)

    class _Cfg:
        def get(self, key, default=None):
            # A stale/invalid persisted id whose userdata dir does not exist.
            return "999999" if key == cu.CONFIG_ACTIVE_USER_KEY else default

    assert cu.resolve(tmp_path, _Cfg()) == _REAL_ACCOUNT


def test_resolve_returns_none_when_only_guest(tmp_path):
    """No real user → None (caller must defer, never write to guest '0')."""
    (tmp_path / "userdata" / "0" / "config").mkdir(parents=True)

    class _Cfg:
        def get(self, key, default=None):
            return default

    assert cu.resolve(tmp_path, _Cfg()) is None


def test_reserved_dirs_never_selected(tmp_path):
    for reserved in ("0", "anonymous", "ac"):
        (tmp_path / "userdata" / reserved / "config").mkdir(parents=True)
    assert cu._from_dir_mtime(tmp_path) is None


# ── path helpers ───────────────────────────────────────────────────

def test_path_helpers(tmp_path):
    assert cu.shortcuts_path(tmp_path, _REAL_ACCOUNT).endswith(
        f"userdata/{_REAL_ACCOUNT}/config/shortcuts.vdf",
    )
    assert cu.grid_dir(tmp_path, _REAL_ACCOUNT).endswith(
        f"userdata/{_REAL_ACCOUNT}/config/grid",
    )
    assert cu.localconfig_path(tmp_path, _REAL_ACCOUNT).endswith(
        f"userdata/{_REAL_ACCOUNT}/config/localconfig.vdf",
    )


# ── runtime re-bind coordinator helper ─────────────────────────────

def test_rebind_user_paths_calls_setters_and_resets_cache():
    class _Shortcut:
        def __init__(self):
            self.path = None
        def set_shortcuts_path(self, p):
            self.path = p

    class _Artwork:
        def __init__(self):
            self.grid = None
        def set_grid_dir(self, p):
            self.grid = p

    class _Services:
        def __init__(self):
            self.shortcut = _Shortcut()
            self.artwork = _Artwork()

    svc = _Services()
    cu.rebind_user_paths(svc, Path("/steam"), _REAL_ACCOUNT)
    assert svc.shortcut.path.endswith(f"userdata/{_REAL_ACCOUNT}/config/shortcuts.vdf")
    assert svc.artwork.grid.endswith(f"userdata/{_REAL_ACCOUNT}/config/grid")


def test_rebind_tolerates_missing_services():
    class _Empty:
        pass
    # No shortcut/artwork attrs — must not raise.
    cu.rebind_user_paths(_Empty(), Path("/steam"), _REAL_ACCOUNT)
