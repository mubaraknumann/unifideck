"""Tests for general per-game environment-variable overrides.

Covers the two layers:

* ``launcher.dispatcher._resolve_game_env_overrides`` — reads
  ``games.<store>:<game_id>.env_overrides`` from user config into a flat
  ``{NAME: value}`` dict, tolerating malformed entries.
* ``GameEnvRPCMixin`` — get/set persistence for the "Environment
  variables…" modal.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from unifideck.launcher.dispatcher import _resolve_game_env_overrides
from unifideck.rpc import RpcError
from unifideck.rpc.mixins.game_env import GameEnvRPCMixin


# ── dispatcher._resolve_game_env_overrides ────────────────────────────────
def test_resolve_game_env_overrides_reads_config(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({
        "games": {
            "gog:123": {
                "env_overrides": {"MANGOHUD": "1", "DXVK_HUD": "fps"},
            },
        },
    }))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    result = _resolve_game_env_overrides("gog", "123")

    assert result == {"MANGOHUD": "1", "DXVK_HUD": "fps"}


def test_resolve_game_env_overrides_empty_without_config(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({"games": {}}))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    assert _resolve_game_env_overrides("gog", "unknown") == {}


def test_resolve_game_env_overrides_skips_malformed_entries(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({
        "games": {
            "gog:123": {
                "env_overrides": {
                    "OK": "1",
                    "": "ignored-blank-key",
                    "LIST": ["not", "a", "string"],
                },
            },
        },
    }))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    result = _resolve_game_env_overrides("gog", "123")

    assert result == {"OK": "1"}


def test_resolve_game_env_overrides_tolerates_non_dict_value(tmp_path, monkeypatch):
    user_cfg = tmp_path / "config.json"
    user_cfg.write_text(json.dumps({
        "games": {"gog:123": {"env_overrides": "not-a-dict"}},
    }))
    monkeypatch.setenv("UNIFIDECK_USER_CONFIG", str(user_cfg))

    assert _resolve_game_env_overrides("gog", "123") == {}


# ── GameEnvRPCMixin ────────────────────────────────────────────────────────
class _FakeConfig:
    def __init__(self):
        self.d: dict = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def _make_host() -> GameEnvRPCMixin:
    host = GameEnvRPCMixin()
    host.config = _FakeConfig()
    return host


def test_get_game_env_empty_by_default():
    host = _make_host()
    out = asyncio.run(host.get_game_env("gog", "123"))
    assert out == {"env": {}}


def test_get_game_env_rejects_missing_args():
    host = _make_host()
    with pytest.raises(RpcError):
        asyncio.run(host.get_game_env("", "123"))


def test_set_game_env_persists_and_returns_clean_dict():
    host = _make_host()

    res = asyncio.run(
        host.set_game_env("gog", "123", {"MANGOHUD": "1", "DXVK_HUD": "fps"}),
    )

    assert res["success"] is True
    assert res["env"] == {"MANGOHUD": "1", "DXVK_HUD": "fps"}
    assert host.config.get("games.gog:123.env_overrides") == {
        "MANGOHUD": "1", "DXVK_HUD": "fps",
    }

    out = asyncio.run(host.get_game_env("gog", "123"))
    assert out["env"] == {"MANGOHUD": "1", "DXVK_HUD": "fps"}


def test_set_game_env_strips_blank_keys():
    host = _make_host()

    res = asyncio.run(
        host.set_game_env("gog", "123", {"  ": "ignored", "OK": "1"}),
    )

    assert res["env"] == {"OK": "1"}


def test_set_game_env_replaces_rather_than_merges():
    host = _make_host()
    asyncio.run(host.set_game_env("gog", "123", {"A": "1", "B": "2"}))

    res = asyncio.run(host.set_game_env("gog", "123", {"C": "3"}))

    assert res["env"] == {"C": "3"}
    out = asyncio.run(host.get_game_env("gog", "123"))
    assert out["env"] == {"C": "3"}


def test_set_game_env_rejects_non_dict():
    host = _make_host()
    with pytest.raises(RpcError):
        asyncio.run(host.set_game_env("gog", "123", "not-a-dict"))  # type: ignore[arg-type]


def test_set_game_env_rejects_missing_args():
    host = _make_host()
    with pytest.raises(RpcError):
        asyncio.run(host.set_game_env("", "123", {}))
