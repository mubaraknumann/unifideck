"""Tests for the re-implemented Ubisoft Steam-owned filter.

The original filter was pulled for flakiness; these lock in the
anti-flakiness contract: exact-match only, never hide on an empty scan,
and never hide an installed game.
"""
from __future__ import annotations

from unifideck.core.types import Game
from unifideck.stores.ubisoft.library.steam_filter import (
    apply_steam_owned_filter,
)

def _game(title: str, *, installed: bool = False) -> Game:
    return Game(
        app_id=0,
        store="ubisoft",
        store_game_id=title.lower().replace(" ", "-"),
        title=title,
        installed=installed,
    )

def test_exact_match_is_hidden():
    games = [_game("Far Cry 6"), _game("Anno 1800")]
    kept, hidden = apply_steam_owned_filter(games, frozenset({"far cry 6"}))
    assert [g.title for g in kept] == ["Anno 1800"]
    assert hidden == ["Far Cry 6"]

def test_near_match_is_kept():
    """Short substrings never match (length-guarded), so no false hide."""
    games = [_game("Far Cry 6")]
    kept, hidden = apply_steam_owned_filter(
        games, frozenset({"far cry"}),  # short substring, must NOT hide
    )
    assert [g.title for g in kept] == ["Far Cry 6"]
    assert hidden == []

def test_publisher_prefix_is_hidden():
    """Steam's "Tom Clancy's Rainbow Six Siege" hides Ubisoft "Rainbow Six Siege"."""
    games = [_game("Rainbow Six Siege")]
    kept, hidden = apply_steam_owned_filter(
        games, frozenset({"tom clancys rainbow six siege"}),
    )
    assert kept == []
    assert hidden == ["Rainbow Six Siege"]

def test_reverse_publisher_prefix_is_hidden():
    """Ubisoft "Tom Clancy's The Division 2" hides against Steam "The Division 2"."""
    games = [_game("Tom Clancy's The Division 2")]
    _kept, hidden = apply_steam_owned_filter(
        games, frozenset({"the division 2"}),
    )
    assert hidden == ["Tom Clancy's The Division 2"]

def test_whitespace_insensitive_match_is_hidden():
    """Steam "Watch Dogs" hides Ubisoft "Watch_Dogs" (underscore stripped)."""
    games = [_game("Watch_Dogs")]
    _kept, hidden = apply_steam_owned_filter(
        games, frozenset({"watch dogs"}),
    )
    assert hidden == ["Watch_Dogs"]

def test_sequel_not_hidden_by_prefix_match():
    """Owning "Far Cry 6" on Steam must not hide a different Ubisoft game."""
    games = [_game("Far Cry 5")]
    kept, hidden = apply_steam_owned_filter(games, frozenset({"far cry 6"}))
    assert [g.title for g in kept] == ["Far Cry 5"]
    assert hidden == []

def test_empty_steam_set_hides_nothing():
    games = [_game("Far Cry 6"), _game("Anno 1800")]
    kept, hidden = apply_steam_owned_filter(games, frozenset())
    assert kept == games
    assert hidden == []

def test_installed_game_is_never_hidden():
    """A game installed through us stays even if owned on Steam."""
    games = [_game("Far Cry 6", installed=True)]
    kept, hidden = apply_steam_owned_filter(games, frozenset({"far cry 6"}))
    assert [g.title for g in kept] == ["Far Cry 6"]
    assert hidden == []

def test_punctuation_and_trademark_normalised():
    """Matching ignores ™/® and punctuation (shared normaliser)."""
    games = [_game("Tom Clancy's Rainbow Six® Siege")]
    kept, hidden = apply_steam_owned_filter(
        games, frozenset({"tom clancys rainbow six siege"}),
    )
    assert kept == []
    assert hidden == ["Tom Clancy's Rainbow Six® Siege"]
