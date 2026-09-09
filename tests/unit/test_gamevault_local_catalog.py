"""Tests for ``stores.gamevault.local_catalog`` — the vault folder.

Two things here are load-bearing and the rest is bookkeeping:

* an unreadable vault must **raise**, never answer ``[]`` — an empty list is
  a real answer the shortcut reconcile acts on, and an SD card that has not
  finished mounting leaves a real, empty directory at the mount point;
* the vault and the install root must be disjoint, which is what makes
  "uninstall deletes the extracted game, never your archive" true by
  construction rather than by care.
"""
from __future__ import annotations

import inspect

import pytest

from unifideck.stores.gamevault.library import GameVaultFetchError
from unifideck.stores.gamevault.local_catalog import (
    SENTINEL_NAME,
    LocalVaultCatalog,
    initialise_vault,
    prepare_vault,
)


def _vault(tmp_path, *names: str):
    """A prepared vault directory holding *names* as empty archives."""
    vault = tmp_path / "vault"
    initialise_vault(vault)
    for name in names:
        (vault / name).write_bytes(b"PK\x03\x04")
    return vault


# ── initialise_vault ─────────────────────────────────────────────────
def test_initialise_creates_the_folder_marker_and_readme(tmp_path):
    vault = tmp_path / "vault"

    initialise_vault(vault)

    assert vault.is_dir()
    assert (vault / SENTINEL_NAME).exists()
    assert "Naming" in (vault / "README.txt").read_text()


def test_initialise_is_idempotent_and_refreshes_the_readme(tmp_path):
    vault = tmp_path / "vault"
    initialise_vault(vault)
    (vault / "README.txt").write_text("stale")
    (vault / "Keep Me (2020).zip").write_bytes(b"PK")

    initialise_vault(vault)

    assert "Naming" in (vault / "README.txt").read_text()
    assert (vault / "Keep Me (2020).zip").exists()


# ── prepare_vault ────────────────────────────────────────────────────
def test_prepare_creates_the_vault(tmp_path):
    vault = prepare_vault(str(tmp_path / "vault"))
    assert vault.is_dir()
    assert (vault / SENTINEL_NAME).exists()


def test_prepare_takes_no_install_location(tmp_path):
    """The redundancy this signature exists to prevent.

    Where a game installs is asked per install by the shared storage picker,
    which already handles SD cards and USB drives for all seven stores.
    A GameVault-only copy of that setting would disagree with it the first
    time a user changed one of them.
    """
    assert list(inspect.signature(prepare_vault).parameters) == ["vault_dir"]


def test_prepare_rejects_an_empty_path(tmp_path):
    with pytest.raises(ValueError, match="archives"):
        prepare_vault("   ")


def test_prepare_rejects_a_relative_path(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        prepare_vault("Games/Vault")


def test_prepare_expands_user_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "~" not in str(prepare_vault("~/vault"))


# ── is_present / the sentinel ────────────────────────────────────────
async def test_is_present_true_for_a_prepared_vault(tmp_path):
    catalog = LocalVaultCatalog(str(_vault(tmp_path)))
    assert await catalog.is_present() is True


async def test_is_present_false_when_the_folder_is_missing(tmp_path):
    catalog = LocalVaultCatalog(str(tmp_path / "not-mounted"))
    assert await catalog.is_present() is False


async def test_is_present_false_for_an_empty_mount_point(tmp_path):
    """An SD card that has not mounted yet: the directory exists, empty."""
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()
    assert await LocalVaultCatalog(str(mount_point)).is_present() is False


# ── fetch: raise, never truncate ─────────────────────────────────────
async def test_fetch_raises_when_the_folder_is_missing(tmp_path):
    catalog = LocalVaultCatalog(str(tmp_path / "gone"))

    with pytest.raises(GameVaultFetchError, match="not there"):
        await catalog.fetch()


async def test_fetch_raises_when_the_sentinel_is_missing(tmp_path):
    """The case an existence check alone would get wrong.

    Answering ``[]`` from an unmounted mount point would tell the reconcile
    the user's whole library had been deleted.
    """
    mount_point = tmp_path / "mnt"
    mount_point.mkdir()
    (mount_point / "stray.txt").write_text("not a vault")
    catalog = LocalVaultCatalog(str(mount_point))

    with pytest.raises(GameVaultFetchError, match="marker"):
        await catalog.fetch()


async def test_fetch_returns_empty_for_a_real_but_empty_vault(tmp_path):
    """The user really did delete their games. That is a real answer."""
    catalog = LocalVaultCatalog(str(_vault(tmp_path)))
    assert await catalog.fetch() == []


# ── fetch: what it finds ─────────────────────────────────────────────
async def test_fetch_lists_archives_with_parsed_titles(tmp_path):
    catalog = LocalVaultCatalog(
        str(_vault(tmp_path, "Stardew Valley (v1.6) (2016).zip", "Celeste (2018).7z")),
    )

    games = await catalog.fetch()

    assert {g.title for g in games} == {"Stardew Valley", "Celeste"}
    assert all(g.store == "gamevault" for g in games)
    assert all(g.store_game_id.startswith("lv_") for g in games)
    assert all(g.installed is False for g in games)


async def test_fetch_carries_the_parsed_metadata(tmp_path):
    catalog = LocalVaultCatalog(
        str(_vault(tmp_path, "Celeste (EA) (L_P) (v1.4) (2018).zip")),
    )

    game = (await catalog.fetch())[0]

    assert game.metadata["release_date"] == "2018"
    assert game.metadata["early_access"] is True
    assert game.metadata["game_type"] == "L_P"
    assert game.metadata["version"] == "v1.4"
    assert game.metadata["is_installer"] is False


async def test_fetch_ignores_non_archives(tmp_path):
    vault = _vault(tmp_path, "Celeste (2018).zip")
    (vault / "notes.txt").write_text("hi")
    (vault / "cover.png").write_bytes(b"\x89PNG")
    (vault / "Game.exe").write_bytes(b"MZ")

    games = await LocalVaultCatalog(str(vault)).fetch()

    assert [g.title for g in games] == ["Celeste"]


async def test_fetch_scans_one_level_of_subfolders(tmp_path):
    """For an SD card mounted inside the vault."""
    vault = _vault(tmp_path, "Celeste (2018).zip")
    disk2 = vault / "disk2"
    disk2.mkdir()
    (disk2 / "Hollow Knight (2017).zip").write_bytes(b"PK")

    games = await LocalVaultCatalog(str(vault)).fetch()

    assert {g.title for g in games} == {"Celeste", "Hollow Knight"}


async def test_fetch_does_not_descend_further_than_one_level(tmp_path):
    vault = _vault(tmp_path)
    deep = vault / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "Too Deep (2020).zip").write_bytes(b"PK")

    assert await LocalVaultCatalog(str(vault)).fetch() == []


# ── Identity and versions ────────────────────────────────────────────
async def test_a_version_bump_keeps_the_same_game_id(tmp_path):
    """Replacing the archive must not orphan the shortcut and its artwork."""
    vault = _vault(tmp_path, "Stardew Valley (v1.5) (2016).zip")
    before = (await LocalVaultCatalog(str(vault)).fetch())[0].store_game_id

    (vault / "Stardew Valley (v1.5) (2016).zip").unlink()
    (vault / "Stardew Valley (v1.6) (2016).zip").write_bytes(b"PK")
    after = (await LocalVaultCatalog(str(vault)).fetch())[0].store_game_id

    assert before == after


async def test_two_versions_group_into_one_game(tmp_path):
    catalog = LocalVaultCatalog(
        str(
            _vault(
                tmp_path,
                "Stardew Valley (v1.5) (2016).zip",
                "Stardew Valley (v1.10) (2016).zip",
            ),
        ),
    )

    games = await catalog.fetch()

    assert len(games) == 1
    assert set(games[0].metadata["versions"]) == {"v1.5", "v1.10"}


async def test_the_highest_version_is_the_install_target(tmp_path):
    """v1.10 beats v1.9 — a string compare would pick the older archive."""
    vault = _vault(
        tmp_path,
        "Stardew Valley (v1.9) (2016).zip",
        "Stardew Valley (v1.10) (2016).zip",
    )
    catalog = LocalVaultCatalog(str(vault))

    game = (await catalog.fetch())[0]
    acquired = catalog.resolve(game.store_game_id)

    assert acquired is not None
    assert acquired.path.name == "Stardew Valley (v1.10) (2016).zip"


async def test_different_games_get_different_ids(tmp_path):
    catalog = LocalVaultCatalog(
        str(_vault(tmp_path, "Doom (1993).zip", "Doom (2016).zip")),
    )
    games = await catalog.fetch()
    assert len({g.store_game_id for g in games}) == 2


# ── resolve ──────────────────────────────────────────────────────────
async def test_resolve_names_the_directory_after_the_title(tmp_path):
    """Not after the filename, or the install dir keeps the version tokens."""
    vault = _vault(tmp_path, "Stardew Valley (v1.6) (W_P) (2016).zip")
    catalog = LocalVaultCatalog(str(vault))
    game = (await catalog.fetch())[0]

    acquired = catalog.resolve(game.store_game_id)

    assert acquired is not None
    assert acquired.dir_name == "Stardew Valley"
    assert acquired.title == "Stardew Valley"


async def test_resolve_carries_the_native_and_installer_hints(tmp_path):
    vault = _vault(tmp_path, "Celeste (L_SW) (2018).zip")
    catalog = LocalVaultCatalog(str(vault))
    game = (await catalog.fetch())[0]

    acquired = catalog.resolve(game.store_game_id)

    assert acquired is not None
    assert acquired.prefer_native is True
    assert acquired.is_installer is True


def test_resolve_returns_none_for_an_unknown_id(tmp_path):
    catalog = LocalVaultCatalog(str(_vault(tmp_path, "Celeste (2018).zip")))
    assert catalog.resolve("lv_deadbeefdeadbeef") is None


def test_resolve_returns_none_when_the_vault_is_gone(tmp_path):
    """An install queued before the card was ejected must fail, not raise."""
    catalog = LocalVaultCatalog(str(tmp_path / "gone"))
    assert catalog.resolve("lv_deadbeefdeadbeef") is None


async def test_resolve_rereads_the_folder(tmp_path):
    """An install can start minutes after the sync that listed the game."""
    vault = _vault(tmp_path, "Stardew Valley (v1.5) (2016).zip")
    catalog = LocalVaultCatalog(str(vault))
    game = (await catalog.fetch())[0]

    (vault / "Stardew Valley (v1.5) (2016).zip").unlink()
    (vault / "Stardew Valley (v1.6) (2016).zip").write_bytes(b"PK")

    acquired = catalog.resolve(game.store_game_id)

    assert acquired is not None
    assert acquired.path.name == "Stardew Valley (v1.6) (2016).zip"
