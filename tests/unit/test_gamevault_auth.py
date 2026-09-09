"""Tests for ``stores.gamevault.auth`` — JWT expiry parsing, login,
credential persistence, and header generation."""
from __future__ import annotations

import asyncio
import base64
import json
import time

from unifideck.stores.gamevault.auth import GameVaultAuth


def _make_jwt(exp: float) -> str:
    """Build a syntactically-valid (unsigned) JWT with a given ``exp`` claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_json = json.dumps({"exp": exp}).encode()
    payload = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


# ── _parse_jwt_expiry ───────────────────────────────────────────────
def test_parse_jwt_expiry_valid_token():
    token = _make_jwt(1234567890.0)
    assert GameVaultAuth._parse_jwt_expiry(token) == 1234567890.0


def test_parse_jwt_expiry_malformed_token_returns_none():
    assert GameVaultAuth._parse_jwt_expiry("not-a-jwt") is None


def test_parse_jwt_expiry_missing_exp_claim_returns_none():
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(b"{}").rstrip(b"=").decode()
    token = f"{header}.{payload}.sig"
    assert GameVaultAuth._parse_jwt_expiry(token) is None


def test_parse_jwt_expiry_empty_string_returns_none():
    assert GameVaultAuth._parse_jwt_expiry("") is None


# ── config persistence ────────────────────────────────────────────────
def test_auth_starts_with_empty_config_when_file_missing(tmp_path):
    auth = GameVaultAuth(config_file=str(tmp_path / "does-not-exist.json"))
    assert auth.server_url is None
    assert auth.is_authenticated() is False


def test_auth_loads_existing_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server_url": "https://gv.example.com",
        "verify_ssl": False,
        "download_dir": "/mnt/downloads",
    }))
    auth = GameVaultAuth(config_file=str(cfg_path))
    assert auth.server_url == "https://gv.example.com"
    assert auth.verify_ssl is False
    assert auth.download_dir == "/mnt/downloads"


def test_auth_corrupt_config_file_falls_back_to_empty(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{not valid json")
    auth = GameVaultAuth(config_file=str(cfg_path))
    assert auth.server_url is None


def test_verify_ssl_defaults_to_true_when_unset(tmp_path):
    auth = GameVaultAuth(config_file=str(tmp_path / "missing.json"))
    assert auth.verify_ssl is True


def test_download_dir_defaults_to_none(tmp_path):
    auth = GameVaultAuth(config_file=str(tmp_path / "missing.json"))
    assert auth.download_dir is None


# ── is_authenticated ────────────────────────────────────────────────────
def test_is_authenticated_false_without_server_url(tmp_path):
    auth = GameVaultAuth(config_file=str(tmp_path / "missing.json"))
    assert auth.is_authenticated() is False


def test_is_authenticated_true_with_valid_token(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server_url": "https://gv.example.com",
        "access_token": _make_jwt(time.time() + 3600),
    }))
    auth = GameVaultAuth(config_file=str(cfg_path))
    assert auth.is_authenticated() is True


def test_is_authenticated_true_with_expired_token_but_saved_credentials(tmp_path):
    """Expired token + stored username/password → still "connected"
    (get_auth_headers refreshes transparently later)."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server_url": "https://gv.example.com",
        "access_token": _make_jwt(time.time() - 3600),
        "username": "alice",
        "password": "secret",
    }))
    auth = GameVaultAuth(config_file=str(cfg_path))
    assert auth.is_authenticated() is True


def test_is_authenticated_false_with_expired_token_and_no_credentials(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server_url": "https://gv.example.com",
        "access_token": _make_jwt(time.time() - 3600),
    }))
    auth = GameVaultAuth(config_file=str(cfg_path))
    assert auth.is_authenticated() is False


# ── get_auth_headers ─────────────────────────────────────────────────────
def test_get_auth_headers_returns_bearer_for_valid_token(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server_url": "https://gv.example.com",
        "access_token": _make_jwt(time.time() + 3600),
    }))
    auth = GameVaultAuth(config_file=str(cfg_path))
    headers = asyncio.run(auth.get_auth_headers())
    assert headers["Authorization"].startswith("Bearer ")


def test_get_auth_headers_none_when_refresh_has_no_credentials(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server_url": "https://gv.example.com",
        "access_token": _make_jwt(time.time() - 3600),
        # no username/password to refresh with
    }))
    auth = GameVaultAuth(config_file=str(cfg_path))
    headers = asyncio.run(auth.get_auth_headers())
    assert headers is None


# ── logout ───────────────────────────────────────────────────────────────
def test_logout_clears_config_and_deletes_file(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"server_url": "https://gv.example.com"}))
    auth = GameVaultAuth(config_file=str(cfg_path))

    result = asyncio.run(auth.logout())

    assert result.success is True
    assert auth.server_url is None
    assert not cfg_path.exists()


def test_logout_when_no_config_file_exists_is_safe(tmp_path):
    auth = GameVaultAuth(config_file=str(tmp_path / "never-existed.json"))
    result = asyncio.run(auth.logout())
    assert result.success is True


# ── start_auth persistence (login itself is network-bound; test the
#    persistence/serialization contract via a monkeypatched _do_login) ──
def test_start_auth_persists_credentials_on_success(tmp_path, monkeypatch):
    from unifideck.core.types import AuthResult

    cfg_path = tmp_path / "config.json"
    auth = GameVaultAuth(config_file=str(cfg_path))

    async def _fake_login(server_url, username, password, verify_ssl):
        auth._cfg["access_token"] = _make_jwt(time.time() + 3600)
        return AuthResult(success=True, action="authenticated", tokens_cached=True, store="gamevault")

    monkeypatch.setattr(auth, "_do_login", _fake_login)

    result = asyncio.run(auth.start_auth(
        server_url="https://gv.example.com/",
        username="alice",
        password="secret",
        verify_ssl=False,
        download_dir="/mnt/dl",
    ))

    assert result.success is True
    assert cfg_path.exists()
    saved = json.loads(cfg_path.read_text())
    assert saved["server_url"] == "https://gv.example.com"  # trailing slash stripped
    assert saved["username"] == "alice"
    assert saved["verify_ssl"] is False
    assert saved["download_dir"] == "/mnt/dl"


def test_start_auth_does_not_persist_on_failure(tmp_path, monkeypatch):
    from unifideck.core.types import AuthResult

    cfg_path = tmp_path / "config.json"
    auth = GameVaultAuth(config_file=str(cfg_path))

    async def _fake_login(server_url, username, password, verify_ssl):
        return AuthResult(success=False, error="bad credentials", store="gamevault")

    monkeypatch.setattr(auth, "_do_login", _fake_login)

    result = asyncio.run(auth.start_auth(
        server_url="https://gv.example.com",
        username="alice",
        password="wrong",
    ))

    assert result.success is False
    assert not cfg_path.exists()
