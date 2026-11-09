"""``pc_library/title_pairs``: which Microsoft titles name the same Xbox game.

The cases are real titles from the dev account's PC and xCloud lists
(2026-10-21): the platform words differ, the game does not.
"""
from __future__ import annotations

import pytest

from unifideck.stores.microsoft.pc_library.title_pairs import game_key


@pytest.mark.parametrize(
    ("pc", "cloud"),
    [
        ("Wolfenstein: The New Order (PC)", "Wolfenstein: The New Order"),
        ("Aliens: Dark Descent (Windows)", "Aliens: Dark Descent"),
        ("Genesis Noir: Windows Edition", "Genesis Noir"),
        ("Minecraft Dungeons for Windows", "Minecraft Dungeons"),
        ("Minecraft Legends - Windows", "Minecraft Legends"),
        ("RESIDENT EVIL 3 for Windows", "RESIDENT EVIL 3 for Xbox"),
        ("Psychonauts (Windows 10)", "Psychonauts"),
        ("Shadowrun Returns PC", "Shadowrun Returns"),
        ("Shadowrun: Hong Kong - Extended Edition PC", "Shadowrun: Hong Kong - Extended Edition"),
        ("DOOM Eternal (BATTLEMODE - PC)", "DOOM Eternal (BATTLEMODE)"),
        ("Monster Sanctuary (Windows Version)", "Monster Sanctuary"),
        ("Call of Duty®: Modern Warfare® - Digital Standard Edition (Windows)", "Call of Duty®: Modern Warfare®"),
        ("Call of Duty®: Vanguard - Standard Edition", "Call of Duty®: Vanguard"),
        ("Control Ultimate Edition", "Control Ultimate Edition - Xbox Series X|S"),
        ("Pillars of Eternity 2: Deadfire - Ultimate Edition (PC)", "Pillars of Eternity II: Deadfire - Ultimate Edition"),
        ("The Outer Worlds: Spacer\u2019s Choice Edition", "The Outer Worlds: Spacer's Choice Edition"),
        ("Wolfenstein II: The New Colossus", "Wolfenstein® II: The New Colossus™"),
        ("FAR CRY 4", "Far Cry® 4"),
    ],
)
def test_platform_words_name_the_same_game(pc: str, cloud: str) -> None:
    assert game_key(pc) == game_key(cloud) != ""


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Fallout 3: Game of the Year Edition", "Fallout 3"),
        ("Fallout: New Vegas Ultimate Edition", "Fallout: New Vegas"),
        ("FAR CRY 3", "Far Cry 3 Classic Edition"),
        ("Control Ultimate Edition", "Control"),
        ("Minecraft Dungeons II", "Minecraft Dungeons"),
        ("Cities: Skylines II - PC Edition", "Cities: Skylines - Xbox One Edition"),
        ("Microsoft Flight Simulator (2020)", "Microsoft Flight Simulator 2024"),
    ],
)
def test_editions_sequels_and_years_stay_apart(a: str, b: str) -> None:
    assert game_key(a) != game_key(b)


def test_a_title_that_is_only_a_platform_word_keeps_its_name() -> None:
    # Stripping never empties a title: "Xbox" alone is a name, not a suffix.
    assert game_key("Xbox") == "xbox"
    assert game_key("PC Building Simulator") == "pc building simulator"
