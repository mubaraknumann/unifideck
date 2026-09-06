"""Local-vault mode, at the two seams where it actually differs.

Everything downstream of those seams is the remote store's code, so the
tests worth writing are the ones that pin *which* pipeline gets built and
*when* the store considers itself usable. The pipeline itself is covered by
``test_gamevault_install.py``, the folder by
``test_gamevault_local_catalog.py``.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.stores.gamevault.auth import MODE_LOCAL, MODE_REMOTE, GameVaultAuth
from unifideck.stores.gamevault.library import RemoteCatalog
from unifideck.stores.gamevault.local_catalog import (
    SENTINEL_NAME,
    LocalVaultCatalog,
)
from unifideck.stores.gamevault.sources import LocalArchiveSource, RemoteArchiveSource
from unifideck.stores.gamevault.store import GameVaultStore


def _store(tmp_path, **cfg) -> GameVaultStore:
    bus = MagicMock()
    bus.emit = AsyncMock()
    config = MagicMock()
    config.get.return_value = {
        "config_file": str(tmp_path / "gamevault_config.json"),
        "default_install_root": str(tmp_path / "installs"),
        "download_dir": str(tmp_path / "downloads"),
        "default_vault_dir": str(tmp_path / "vault"),
        **cfg,
    }
    return GameVaultStore(bus, MagicMock(), plugin_dir=None, config=config)


def _auth(tmp_path) -> GameVaultAuth:
    return GameVaultAuth(config_file=str(tmp_path / "gamevault_config.json"))


# ── The config file carries the mode, for both modes ─────────────────
def test_a_config_written_before_local_mode_existed_reads_as_remote(tmp_path):
    """Upgrade safety: no ``mode`` key means the old, only mode."""
    path = tmp_path / "gamevault_config.json"
    path.write_text(json.dumps({"server_url": "https://gv.example.com"}))

    auth = GameVaultAuth(config_file=str(path))

    assert auth.mode == MODE_REMOTE
    assert auth.is_local is False


async def test_local_connect_persists_the_mode_and_both_paths(tmp_path):
    auth = _auth(tmp_path)

    result = await auth.start_local_auth(vault_dir=str(tmp_path / "vault"))

    assert result.success is True
    assert auth.is_local is True
    assert auth.vault_dir == str(tmp_path / "vault")
    assert (tmp_path / "vault" / SENTINEL_NAME).exists()


async def test_local_connect_rejects_an_unusable_path(tmp_path):
    """A bad path fails the connect instead of half-configuring the store."""
    auth = _auth(tmp_path)

    result = await auth.start_local_auth(vault_dir="relative/path")

    assert result.success is False
    assert "absolute" in (result.error or "")
    assert auth.is_local is False


async def test_switching_to_local_drops_the_stored_password(tmp_path):
    """The modes are exclusive, so a fresh config replaces the old one.

    A leftover ``password`` would be a secret on disk that nothing can any
    longer use, and a leftover ``server_url`` would make ``is_local`` lie
    the next time the file was read.
    """
    path = tmp_path / "gamevault_config.json"
    path.write_text(
        json.dumps(
            {
                "mode": MODE_REMOTE,
                "server_url": "https://gv.example.com",
                "username": "alice",
                "password": "hunter2",
            },
        ),
    )
    auth = GameVaultAuth(config_file=str(path))

    await auth.start_local_auth(vault_dir=str(tmp_path / "vault"))

    on_disk = json.loads(path.read_text())
    assert on_disk["mode"] == MODE_LOCAL
    assert "password" not in on_disk
    assert "server_url" not in on_disk


async def test_local_mode_is_authenticated_without_any_secret(tmp_path):
    auth = _auth(tmp_path)
    await auth.start_local_auth(vault_dir=str(tmp_path / "vault"))
    assert auth.is_authenticated() is True


def test_local_mode_stays_authenticated_when_the_drive_is_absent(tmp_path):
    """An unmounted card must not read as "signed out".

    Otherwise the user is asked to reconnect every time they boot undocked.
    Whether the vault is reachable *right now* is ``is_available``'s
    question, not this one.
    """
    path = tmp_path / "gamevault_config.json"
    path.write_text(
        json.dumps({"mode": MODE_LOCAL, "vault_dir": "/run/media/gone"}),
    )
    assert GameVaultAuth(config_file=str(path)).is_authenticated() is True


# ── The store builds one pipeline or the other ───────────────────────
def test_a_fresh_store_defaults_to_the_remote_pipeline(tmp_path):
    store = _store(tmp_path)
    assert isinstance(store._library_reader._catalog, RemoteCatalog)
    assert isinstance(store._installer._source, RemoteArchiveSource)


async def test_connecting_locally_rebuilds_the_pipeline(tmp_path):
    """The store object outlives a connect, so the sources must be rewired."""
    store = _store(tmp_path)

    result = await store.start_auth(
        mode="local", vault_dir=str(tmp_path / "vault"),
    )

    assert result.success is True
    assert isinstance(store._library_reader._catalog, LocalVaultCatalog)
    assert isinstance(store._installer._source, LocalArchiveSource)


async def test_local_connect_falls_back_to_the_configured_default(tmp_path):
    """Connecting with an empty form creates the folder for the user."""
    store = _store(tmp_path)

    result = await store.start_auth(mode="local")

    assert result.success is True
    assert (tmp_path / "vault" / SENTINEL_NAME).exists()


async def test_local_mode_keeps_the_shared_install_root_fallback(tmp_path):
    """No per-store install location: both modes use the same fallback.

    It is only a fallback — ``useInstallFlow`` runs the shared storage
    picker before every install and passes an explicit ``install_path`` —
    but when one is not supplied, local and remote must land in the same
    place, because there is only one such setting.
    """
    store = _store(tmp_path)
    remote_root = store._installer._default_install_root

    await store.start_auth(mode="local", vault_dir=str(tmp_path / "vault"))

    assert store._installer._default_install_root == remote_root
    assert remote_root == tmp_path / "installs"


async def test_local_connect_stores_no_install_location(tmp_path):
    """The redundant setting must not come back through the config file."""
    store = _store(tmp_path)
    await store.start_auth(mode="local", vault_dir=str(tmp_path / "vault"))

    on_disk = json.loads((tmp_path / "gamevault_config.json").read_text())
    assert "install_root" not in on_disk


async def test_logout_returns_the_store_to_the_remote_pipeline(tmp_path):
    """A stale local catalog must not keep answering after disconnect."""
    store = _store(tmp_path)
    await store.start_auth(mode="local", vault_dir=str(tmp_path / "vault"))

    await store.logout()

    assert isinstance(store._library_reader._catalog, RemoteCatalog)
    assert store._local_catalog is None


# ── is_available: the unmounted-card case ────────────────────────────
async def test_is_available_false_when_the_vault_is_not_mounted(tmp_path):
    """Keeps the store out of the sync's store set entirely.

    A store that is never fetched is never swept, so the user's shortcuts
    survive a boot with the card missing.
    """
    store = _store(tmp_path)
    await store.start_auth(mode="local", vault_dir=str(tmp_path / "vault"))
    (tmp_path / "vault" / SENTINEL_NAME).unlink()

    assert await store.is_available() is False


async def test_is_available_true_for_a_present_vault(tmp_path):
    store = _store(tmp_path)
    await store.start_auth(mode="local", vault_dir=str(tmp_path / "vault"))
    assert await store.is_available() is True


# ── get_library keeps the invariant in local mode ────────────────────
async def test_get_library_is_none_when_the_vault_is_unreadable(tmp_path):
    """None, not []. An empty list is a real answer the reconcile acts on."""
    store = _store(tmp_path)
    await store.start_auth(mode="local", vault_dir=str(tmp_path / "vault"))
    (tmp_path / "vault" / SENTINEL_NAME).unlink()

    assert await store.get_library() is None


async def test_get_library_lists_the_vault(tmp_path):
    store = _store(tmp_path)
    await store.start_auth(mode="local", vault_dir=str(tmp_path / "vault"))
    (tmp_path / "vault" / "Celeste (2018).zip").write_bytes(b"PK")

    games = await store.get_library()

    assert games is not None
    assert [g.title for g in games] == ["Celeste"]


@pytest.mark.parametrize("mode", [MODE_REMOTE, MODE_LOCAL])
async def test_check_for_updates_is_empty_in_both_modes(tmp_path, mode):
    store = _store(tmp_path)
    if mode == MODE_LOCAL:
        await store.start_auth(mode="local", vault_dir=str(tmp_path / "vault"))
    assert await store.check_for_updates() == []
