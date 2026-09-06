"""Tests for ``stores.gamevault.library`` — title/cover extraction and
raw-API-item-to-``Game`` mapping. Pagination itself is network-bound and
left uncovered here (would need an aiohttp test server); this focuses on
the pure, easily-broken parsing helpers.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from unifideck.core.types import Game

from unifideck.stores.gamevault.library import (
    GameVaultFetchError,
    GameVaultLibraryReader,
    RemoteCatalog,
    _parse_title_from_filename,
)


def _reader() -> RemoteCatalog:
    """The remote catalog — owner of the API-item parsing helpers below."""
    return RemoteCatalog(MagicMock())


class _FakeCatalog:
    """A ``CatalogSource`` that answers with whatever it was handed.

    The reader is mode-agnostic now, so its tests describe the overlay and
    the never-truncate rule rather than any particular transport.
    """

    def __init__(self, games=None, error: Exception | None = None) -> None:
        self._games = games or []
        self._error = error

    async def fetch(self):
        if self._error is not None:
            raise self._error
        return list(self._games)


def _game(game_id: str = "1", title: str = "My Game") -> Game:
    return Game(
        app_id=0,
        store="gamevault",
        store_game_id=game_id,
        title=title,
        installed=False,
    )


# ── _parse_title_from_filename ───────────────────────────────────────
def test_parse_title_strips_extension_and_year():
    assert _parse_title_from_filename("Half-Life 2 (2004).zip") == "Half Life 2"


def test_parse_title_no_year_suffix():
    assert _parse_title_from_filename("Portal.exe") == "Portal"


def test_parse_title_underscores_and_dashes_become_spaces():
    assert _parse_title_from_filename("The_Witcher-3_Wild-Hunt.rar") == "The Witcher 3 Wild Hunt"


def test_parse_title_with_directory_prefix():
    assert _parse_title_from_filename("/mnt/games/Doom Eternal (2020).7z") == "Doom Eternal"


def test_parse_title_collapses_multiple_spaces():
    assert _parse_title_from_filename("Game   Title.zip") == "Game Title"


def test_parse_title_bracket_year_variant():
    assert _parse_title_from_filename("Cyberpunk 2077 [2020].zip") == "Cyberpunk 2077"


def test_parse_title_falls_back_to_filename_when_result_empty():
    # An input that becomes empty after stripping should fall back to the
    # original file_path rather than returning "".
    assert _parse_title_from_filename("(2020).zip") == "(2020).zip"


def test_parse_title_fallback_never_returns_a_whole_windows_path():
    """Even the last-ditch branch must not ship a path as the AppName."""
    assert _parse_title_from_filename(
        r"C:\Users\numan\Vault\files\(2020).zip",
    ) == "(2020).zip"


# ── _extract_title ────────────────────────────────────────────────────
def test_extract_title_prefers_metadata_title():
    reader = _reader()
    item = {"metadata": {"title": "Explicit Title"}, "file_path": "ignored.zip"}
    assert reader._extract_title(item) == "Explicit Title"


def test_extract_title_falls_back_to_metadata_name():
    reader = _reader()
    item = {"metadata": {"name": "Named Title"}}
    assert reader._extract_title(item) == "Named Title"


def test_extract_title_falls_back_to_file_path_parsing():
    reader = _reader()
    item = {"file_path": "My Game (2021).zip"}
    assert reader._extract_title(item) == "My Game"


def test_extract_title_falls_back_to_id_placeholder():
    reader = _reader()
    item = {"id": 42}
    assert reader._extract_title(item) == "GameVault Game #42"


def test_extract_title_prefers_top_level_over_file_path():
    """The field that was missing, and the whole of the reported bug.

    ``metadata`` is null until a provider has enriched the game, which on a
    self-hosted server with no IGDB credentials is every game — but the
    server's own indexer always fills the top-level ``title``.
    """
    reader = _reader()
    item = {
        "id": 3,
        "metadata": None,
        "title": "Endless Sky",
        "file_path": r"C:\Users\numan\Vault\files\Endless Sky.zip",
    }
    assert reader._extract_title(item) == "Endless Sky"


def test_extract_title_prefers_metadata_over_top_level():
    """Pins the precedence: curated metadata beats the server's own guess."""
    reader = _reader()
    item = {"metadata": {"title": "Curated"}, "title": "Server Guess"}
    assert reader._extract_title(item) == "Curated"


def test_extract_title_of_a_windows_path_is_never_the_whole_path():
    """No ``title`` at all, so the filename parser is the only source left.

    It must still not produce ``C:\\Users\\numan\\Vault\\files\\Warzone 2100``
    — a path-shaped AppName also starves every title-matched enrichment
    source we have (SGDB, the Steam CDN, unifiDB, Metacritic).
    """
    reader = _reader()
    item = {
        "id": 10,
        "metadata": None,
        "file_path": (
            r"C:\Users\numan\Vault\files\Warzone 2100 (W_P) (2024).zip"
        ),
    }
    result = reader._extract_title(item)
    assert result == "Warzone 2100"
    assert "\\" not in result
    assert "C:" not in result


def test_extract_title_of_a_real_server_item():
    """Verbatim from a GameVault v17.0.0 server with no metadata provider."""
    reader = _reader()
    item = {
        "id": 1,
        "file_path": (
            r"C:\Users\numan\Vault\files"
            r"\Brogue Community Edition (v1.15.1) (W_P) (2024).zip"
        ),
        "size": "1746464",
        "title": "Brogue Community Edition",
        "sort_title": "brogue community edition",
        "version": "v1.15.1",
        "versions": [],
        "release_date": "2024-01-01T00:00:00.000Z",
        "early_access": False,
        "download_count": 0,
        "type": "WINDOWS_PORTABLE",
        "provider_metadata": [],
        "metadata": None,
    }
    assert reader._extract_title(item) == "Brogue Community Edition"


def test_extract_title_ignores_sort_title():
    """``sort_title`` is lowercased, so it can only ever be a worse AppName."""
    reader = _reader()
    item = {"id": 1, "metadata": None, "sort_title": "endless sky"}
    assert reader._extract_title(item) == "GameVault Game #1"


# ── _extract_cover_url ───────────────────────────────────────────────
def test_extract_cover_url_top_level_field():
    reader = _reader()
    item = {"cover_image": "https://example.com/cover.jpg"}
    assert reader._extract_cover_url(item) == "https://example.com/cover.jpg"


def test_extract_cover_url_thumbnail_field():
    reader = _reader()
    item = {"thumbnail": "https://example.com/thumb.jpg"}
    assert reader._extract_cover_url(item) == "https://example.com/thumb.jpg"


def test_extract_cover_url_structured_boxart():
    reader = _reader()
    item = {"boxart": {"url": "https://example.com/box.jpg"}}
    assert reader._extract_cover_url(item) == "https://example.com/box.jpg"


def test_extract_cover_url_none_when_nothing_present():
    reader = _reader()
    assert reader._extract_cover_url({}) is None


# ── _map_to_game ──────────────────────────────────────────────────────
def test_map_to_game_builds_expected_record():
    reader = _reader()
    item = {
        "id": 5,
        "metadata": {"title": "My Game"},
        "cover_image": "https://x/cover.jpg",
        "file_path": "/games/mygame.zip",
        "release_date": "2020-01-01",
        "early_access": True,
    }
    game = reader._map_to_game(item)
    assert game is not None
    assert game.store == "gamevault"
    assert game.store_game_id == "5"
    assert game.title == "My Game"
    assert game.icon_url == "https://x/cover.jpg"
    assert game.installed is False
    assert game.metadata["early_access"] is True


def test_map_to_game_missing_id_returns_none():
    reader = _reader()
    assert reader._map_to_game({"metadata": {"title": "No ID"}}) is None


def test_map_to_game_malformed_item_returns_none_not_raises():
    reader = _reader()
    # metadata is a non-dict, exercising the try/except path
    item = {"id": 1, "metadata": None, "cover_image": None}
    game = reader._map_to_game(item)
    # Should not raise; result may be a valid Game with fallback title.
    assert game is None or game.store_game_id == "1"


# ── get_library() install-state overlay ───────────────────────────────
async def test_get_library_marks_installed_games():
    installer = MagicMock()
    installer.get_install_info.return_value = {
        "install_path": "/games/mygame",
        "exe_path": "/games/mygame/Game.exe",
    }
    reader = GameVaultLibraryReader(
        installer=installer, catalog=_FakeCatalog([_game()]),
    )

    games = await reader.get_library()

    assert len(games) == 1
    assert games[0].installed is True
    assert games[0].install_path == "/games/mygame"
    # The exe is deliberately NOT carried, even though the marker has one.
    # Reconcile only rewrites a games.map row when the synced game brings an
    # exe, so carrying it would revert the user's Change Executable choice on
    # every sync — which matters more for this store than any other, because
    # a GameVault archive is whatever its owner uploaded and the
    # auto-detected target is a guess. The launch target is written once at
    # install time and belongs to the user after that.
    assert not games[0].exe_path


async def test_get_library_uninstalled_games_stay_uninstalled():
    installer = MagicMock()
    installer.get_install_info.return_value = None
    reader = GameVaultLibraryReader(
        installer=installer, catalog=_FakeCatalog([_game("2", "Not Installed")]),
    )

    games = await reader.get_library()

    assert games[0].installed is False


# ── A failed fetch must not read as an empty library ──────────────────
#
# This is the sharpest form of stores.md invariant #2. The sync replaces its
# whole game list with what ``get_library`` returns and the shortcut
# reconcile sweeps whatever is missing, so a store that answers a failed
# fetch with the games it managed to read — or with none — is indistinguishable
# from a library that shrank, and the user's shortcuts are deleted. GameVault
# talks to a self-hosted server that is offline routinely, so this is the
# expected case, not the exotic one.
async def test_a_failed_page_aborts_the_fetch_instead_of_returning_partial():
    reader = GameVaultLibraryReader(
        installer=MagicMock(),
        catalog=_FakeCatalog(
            error=GameVaultFetchError("server returned HTTP 502 for offset=500"),
        ),
    )

    with pytest.raises(GameVaultFetchError):
        await reader.get_library()


async def test_store_turns_a_failed_fetch_into_none_never_empty():
    from unittest.mock import AsyncMock

    from unifideck.stores.gamevault.store import GameVaultStore

    bus = MagicMock()
    bus.emit = AsyncMock()
    store = GameVaultStore(bus, MagicMock(), plugin_dir=None, config=None)
    store._auth = MagicMock()
    store._auth.get_auth_headers = AsyncMock(return_value={"Authorization": "Bearer x"})
    store._auth.server_url = "https://gv.example.com"
    store._auth.verify_ssl = True
    store._library_reader = MagicMock()
    store._library_reader.get_library = AsyncMock(
        side_effect=GameVaultFetchError("connection refused"),
    )

    result = await store.get_library()

    # None, not []. An empty list is a real answer the reconcile acts on.
    assert result is None
