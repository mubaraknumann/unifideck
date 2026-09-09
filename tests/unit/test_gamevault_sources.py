"""Tests for ``stores.gamevault.sources`` — the two ends of one pipeline.

The seam that lets remote and local share every downstream step. Most of
what used to be tested through ``GameVaultStore.install_game`` lives here
now: the staging-directory precedence, the ``Content-Disposition`` handling,
and — the important one — the fact that ``release`` means "delete" for a
staged download and "do nothing" for a file the user owns.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from unifideck.stores.gamevault.sources import (
    AcquiredArchive,
    LocalArchiveSource,
    RemoteArchiveSource,
    _parse_filename_from_cd,
    _safe_archive_name,
    safe_dir_name,
)


def _auth(**overrides) -> MagicMock:
    auth = MagicMock()
    auth.get_auth_headers = AsyncMock(
        return_value=overrides.get("headers", {"Authorization": "Bearer x"}),
    )
    auth.server_url = overrides.get("server_url", "https://gv.example.com")
    auth.verify_ssl = overrides.get("verify_ssl", True)
    auth.download_dir = overrides.get("download_dir", None)
    return auth


class _Locator:
    def __init__(self, acquired: AcquiredArchive | None) -> None:
        self._acquired = acquired

    def resolve(self, game_id: str) -> AcquiredArchive | None:
        return self._acquired


# ── RemoteArchiveSource: staging directory precedence ────────────────
def test_saved_download_dir_wins_over_the_configured_default():
    source = RemoteArchiveSource(
        _auth(download_dir="/saved/dir"), download_dir="/configured/dir",
    )
    assert source._staging_dir() == Path("/saved/dir")


def test_configured_default_is_used_when_nothing_was_saved():
    source = RemoteArchiveSource(_auth(), download_dir="/configured/dir")
    assert source._staging_dir() == Path("/configured/dir")


def test_staging_dir_expands_user_paths():
    source = RemoteArchiveSource(_auth(download_dir="~/dl"), download_dir="~/x")
    assert "~" not in str(source._staging_dir())


# ── RemoteArchiveSource: auth gating ─────────────────────────────────
async def test_acquire_without_auth_raises_not_authenticated():
    source = RemoteArchiveSource(_auth(headers=None), download_dir="/dl")

    with pytest.raises(RuntimeError, match="Not authenticated"):
        await source.acquire("1", progress_callback=None)


async def test_size_without_auth_is_none_not_an_error():
    """Size is decorative; a signed-out store must not raise from it."""
    source = RemoteArchiveSource(_auth(headers=None), download_dir="/dl")
    assert await source.size("1") is None


# ── RemoteArchiveSource: release deletes the staged copy ─────────────
def test_release_deletes_a_staged_download(tmp_path):
    staged = tmp_path / "Game.zip"
    staged.write_bytes(b"x")
    source = RemoteArchiveSource(_auth(), download_dir=str(tmp_path))

    source.release(AcquiredArchive(path=staged, title="G", dir_name="G"))

    assert not staged.exists()


def test_release_tolerates_a_missing_file_and_none(tmp_path):
    """Runs from a ``finally``; raising here would mask the real error."""
    source = RemoteArchiveSource(_auth(), download_dir=str(tmp_path))
    source.release(None)
    source.release(
        AcquiredArchive(path=tmp_path / "gone.zip", title="G", dir_name="G"),
    )


# ── LocalArchiveSource ───────────────────────────────────────────────
async def test_local_acquire_returns_the_users_file(tmp_path):
    archive = tmp_path / "My Game (2016).zip"
    archive.write_bytes(b"PK\x03\x04")
    acquired = AcquiredArchive(path=archive, title="My Game", dir_name="My Game")
    source = LocalArchiveSource(_Locator(acquired))

    result = await source.acquire("lv_1", progress_callback=None)

    assert result.path == archive


async def test_local_acquire_reports_the_extracting_phase(tmp_path):
    """Local installs never emit "downloading", so the UI needs a first tick."""
    archive = tmp_path / "g.zip"
    archive.write_bytes(b"PK")
    seen: list[dict] = []

    async def progress(payload):
        seen.append(payload)

    source = LocalArchiveSource(
        _Locator(AcquiredArchive(path=archive, title="G", dir_name="G")),
    )
    await source.acquire("lv_1", progress_callback=progress)

    assert seen and seen[0]["phase"] == "extracting"


async def test_local_acquire_raises_when_the_archive_is_gone():
    source = LocalArchiveSource(_Locator(None))

    with pytest.raises(RuntimeError, match="No archive for"):
        await source.acquire("lv_1", progress_callback=None)


def test_local_release_keeps_the_file(tmp_path):
    """The whole safety story for "uninstall must not eat my zip".

    The pipeline calls ``release`` on every path, success or failure. Remote
    unlinks; local declines. Nothing downstream has to remember which mode
    it is in.
    """
    archive = tmp_path / "My Game.zip"
    archive.write_bytes(b"PK\x03\x04")
    source = LocalArchiveSource(
        _Locator(AcquiredArchive(path=archive, title="G", dir_name="G")),
    )

    source.release(AcquiredArchive(path=archive, title="G", dir_name="G"))

    assert archive.exists()


async def test_local_size_is_the_archive_size_on_disk(tmp_path):
    archive = tmp_path / "g.zip"
    archive.write_bytes(b"x" * 1234)
    source = LocalArchiveSource(
        _Locator(AcquiredArchive(path=archive, title="G", dir_name="G")),
    )

    assert await source.size("lv_1") == 1234


async def test_local_size_is_none_when_the_archive_is_gone():
    source = LocalArchiveSource(_Locator(None))
    assert await source.size("lv_1") is None


# ── Content-Disposition parsing ──────────────────────────────────────
def test_parse_filename_from_cd_simple():
    assert _parse_filename_from_cd('attachment; filename="Game.zip"') == "Game.zip"


def test_parse_filename_from_cd_no_quotes():
    assert _parse_filename_from_cd("attachment; filename=Game.zip") == "Game.zip"


def test_parse_filename_from_cd_rfc5987_charset_prefix():
    """KNOWN GAP: the capture regex ``[^"\\';\\r\\n]+`` stops at the FIRST
    apostrophe, so it never actually reaches the ``''`` split branch for a
    real RFC 5987 ``filename*=UTF-8''...`` value — it captures only the
    charset token. Pinned as documentation of current behaviour, not the
    intended one.
    """
    assert _parse_filename_from_cd("attachment; filename*=UTF-8''My%20Game.zip") == "UTF-8"


def test_parse_filename_from_cd_missing_returns_none():
    assert _parse_filename_from_cd("attachment") is None


def test_parse_filename_from_cd_empty_string_returns_none():
    assert _parse_filename_from_cd("") is None


# ── Names from remote input cannot escape their directory ────────────
def test_safe_archive_name_strips_directory_components():
    assert _safe_archive_name("../../.ssh/authorized_keys", "1") == "authorized_keys"


def test_safe_archive_name_rejects_dot_only_names():
    assert _safe_archive_name("..", "7") == "gamevault_7.bin"
    assert _safe_archive_name("", "7") == "gamevault_7.bin"
    assert _safe_archive_name(None, "7") == "gamevault_7.bin"


def test_safe_dir_name_strips_path_separators_and_reserved_characters():
    assert safe_dir_name("Rick/Morty: The Game?") == "RickMorty The Game"


def test_safe_dir_name_never_returns_empty():
    assert safe_dir_name("///") == "game"
    assert safe_dir_name("...") == "game"
