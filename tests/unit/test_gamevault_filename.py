"""Tests for ``stores.gamevault.filename`` — the naming grammar.

One parser serves both modes: remote reaches for it when the server's
metadata lookup found nothing and only a ``file_path`` came back, and local
mode has nothing *but* filenames, so the grammar is the whole identity of a
game there. The examples below are taken from GameVault's own structure docs
so a vault folder stays portable to a real server.
"""
from __future__ import annotations

import pytest

from unifideck.stores.gamevault.filename import (
    ARCHIVE_EXTENSIONS,
    is_indexable,
    parse_archive_name,
    strip_extension,
    version_sort_key,
)


# ── The documented examples ──────────────────────────────────────────
@pytest.mark.parametrize(
    ("name", "title", "version", "year"),
    [
        ("Stray (2022).7z", "Stray", None, 2022),
        (
            "Star Wars Jedi - Fallen Order (v1.0.10.0) (2019).zip",
            "Star Wars Jedi Fallen Order",
            "v1.0.10.0",
            2019,
        ),
        ("Minecraft (EA) (v1.8b) (2011).zip", "Minecraft", "v1.8b", 2011),
        ("Minecraft (v1.20.1) (2011).7z", "Minecraft", "v1.20.1", 2011),
        ("Stardew Valley.zip", "Stardew Valley", None, None),
    ],
)
def test_documented_examples(name, title, version, year):
    parsed = parse_archive_name(name)
    assert parsed.title == title
    assert parsed.version == version
    assert parsed.year == year


def test_every_token_at_once():
    parsed = parse_archive_name("Far Cry 6 (v1.5.0) (EA) (W_P) (NC) (2021).zip")
    assert parsed.title == "Far Cry 6"
    assert parsed.version == "v1.5.0"
    assert parsed.early_access is True
    assert parsed.game_type == "W_P"
    assert parsed.year == 2021


def test_tokens_are_order_tolerant():
    """The docs fix an order; users will not. Classification is per token."""
    parsed = parse_archive_name("Game (2021) (W_P) (v2.0).zip")
    assert parsed.title == "Game"
    assert parsed.version == "v2.0"
    assert parsed.game_type == "W_P"
    assert parsed.year == 2021


def test_directory_prefix_is_dropped():
    assert parse_archive_name("/mnt/games/Doom Eternal (2020).7z").title == (
        "Doom Eternal"
    )


# ── Paths written by the other OS ────────────────────────────────────
def test_windows_drive_path_is_reduced_to_its_filename():
    """A self-hosted Windows server hands back its own path spelling.

    ``pathlib.Path`` on the Deck is ``PosixPath``, which does not split on
    ``\\``, so this used to yield the whole string as the shortcut name.
    """
    parsed = parse_archive_name(
        r"C:\Users\numan\Vault\files"
        r"\Brogue Community Edition (v1.15.1) (W_P) (2024).zip",
    )
    assert parsed.title == "Brogue Community Edition"
    assert parsed.version == "v1.15.1"
    assert parsed.game_type == "W_P"
    assert parsed.year == 2024


def test_windows_forward_slash_path_is_reduced_to_its_filename():
    assert parse_archive_name(
        "C:/Users/numan/Vault/files/Warzone 2100 (4.7.0).zip",
    ).title == "Warzone 2100 (4.7.0)"


def test_unc_path_is_reduced_to_its_filename():
    assert parse_archive_name(
        r"\\nas\vault\Endless Sky (2019).zip",
    ).title == "Endless Sky"


def test_a_bare_name_containing_a_backslash_is_left_alone():
    """The guard on the ``lv_`` ids.

    A backslash is a legal character in a Linux filename, and local mode
    parses bare names straight off the vault folder. Splitting on ``\\``
    unconditionally would change ``ParsedName.identity``, which re-keys the
    ``lv_<sha1>`` game id and orphans an existing shortcut's appId, playtime
    and artwork. Only a drive-letter or UNC *prefix* triggers Windows
    semantics.
    """
    assert parse_archive_name(r"My\Game (2021).zip").title == r"My\Game"


def test_a_bare_backslash_name_keeps_its_identity():
    """Belt and braces on the same hazard, at the level ids derive from."""
    assert parse_archive_name(r"My\Game (2021).zip").identity == "my game|2021"


# ── Game type ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("token", "is_linux", "is_installer"),
    [
        ("W_P", False, False),
        ("W_S", False, True),
        ("L_P", True, False),
        ("L_SW", True, True),
    ],
)
def test_game_type_flags(token, is_linux, is_installer):
    parsed = parse_archive_name(f"Game ({token}) (2020).zip")
    assert parsed.game_type == token
    assert parsed.is_linux is is_linux
    assert parsed.is_installer is is_installer


def test_unlabelled_archive_claims_nothing():
    parsed = parse_archive_name("Game (2020).zip")
    assert parsed.game_type is None
    assert parsed.is_linux is False
    assert parsed.is_installer is False


# ── What stays part of the title ─────────────────────────────────────
def test_a_parenthesised_edition_is_part_of_the_title():
    """Dropping it would merge two genuinely different games."""
    parsed = parse_archive_name("Game (Deluxe Edition) (2019).zip")
    assert parsed.title == "Game (Deluxe Edition)"
    assert parsed.year == 2019


def test_an_unclassified_token_stops_the_scan():
    """Tokens are consumed right-to-left, so ``(v1.0)`` is read first.

    ``(Something)`` then ends the scan and everything to its left, itself
    included, stays in the title. A token further left is therefore never
    reached — which is the intended tolerance: the parser gives up rather
    than guessing at the middle of a name it does not recognise.
    """
    parsed = parse_archive_name("Game (Something) (v1.0).zip")
    assert parsed.title == "Game (Something)"
    assert parsed.version == "v1.0"

    # The mirror case: a stopper to the right hides the token behind it.
    hidden = parse_archive_name("Game (v1.0) (Something).zip")
    assert hidden.title == "Game (v1.0) (Something)"
    assert hidden.version is None


def test_a_square_bracket_repack_tag_is_dropped():
    """Square brackets are scene tags by convention; parentheses are not."""
    assert parse_archive_name("Ghost of Tsushima [DODI Repack].7z").title == (
        "Ghost of Tsushima"
    )
    assert parse_archive_name("Portal 2 [FitGirl] (2011).zip").title == "Portal 2"


def test_bracket_year_variant():
    assert parse_archive_name("Cyberpunk 2077 [2020].zip").title == "Cyberpunk 2077"


def test_separators_become_spaces_and_collapse():
    assert parse_archive_name("The_Witcher-3_Wild-Hunt.rar").title == (
        "The Witcher 3 Wild Hunt"
    )
    assert parse_archive_name("Game   Title.zip").title == "Game Title"


def test_a_name_that_is_only_tokens_yields_no_title():
    """The caller decides the fallback; the parser does not invent one."""
    assert parse_archive_name("(2020).zip").title == ""


# ── Identity: stable across a version bump ───────────────────────────
def test_identity_ignores_the_version():
    a = parse_archive_name("Stardew Valley (v1.5) (2016).zip")
    b = parse_archive_name("Stardew Valley (v1.6) (2016).zip")
    assert a.identity == b.identity


def test_identity_ignores_separator_and_case_spelling():
    a = parse_archive_name("The_Witcher 3 (2015).zip")
    b = parse_archive_name("the witcher 3 (2015).7z")
    assert a.identity == b.identity


def test_identity_separates_different_years():
    a = parse_archive_name("Doom (1993).zip")
    b = parse_archive_name("Doom (2016).zip")
    assert a.identity != b.identity


def test_identity_separates_different_editions():
    a = parse_archive_name("Game (2019).zip")
    b = parse_archive_name("Game (Deluxe Edition) (2019).zip")
    assert a.identity != b.identity


# ── Version ordering ─────────────────────────────────────────────────
def test_version_sort_key_orders_double_digits_correctly():
    """A string compare would put v1.9 above v1.10 and pick the older file."""
    assert version_sort_key("v1.10") > version_sort_key("v1.9")


def test_version_sort_key_handles_a_suffixed_version():
    assert version_sort_key("v1.8b") == (1, 8)


def test_version_sort_key_treats_absent_as_lowest():
    assert version_sort_key(None) < version_sort_key("v0.1")


# ── Extensions ───────────────────────────────────────────────────────
def test_strip_extension_prefers_the_longest_match():
    assert strip_extension("Game.tar.gz") == "Game"
    assert strip_extension("Game.gz") == "Game"


def test_strip_extension_leaves_an_unknown_suffix_alone():
    assert strip_extension("Game.qqq") == "Game.qqq"


def test_is_indexable_accepts_every_extension_the_extractor_handles():
    for ext in ARCHIVE_EXTENSIONS:
        assert is_indexable(f"Game{ext}") is True


def test_is_indexable_is_case_insensitive():
    assert is_indexable("Game.ZIP") is True


def test_is_indexable_rejects_bare_executables_and_junk():
    """The vault holds archives. A bare .exe is a deliberate exclusion."""
    for name in ("Game.exe", "Game.sh", "Game.AppImage", "notes.txt", "cover.png"):
        assert is_indexable(name) is False
